from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from psycopg import Error as PsycopgError
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from evolvable_memory.application.security import (
    AuthorizationAuditEvent,
    AuthorizationDecision,
    AuthorizationRequest,
    MemoryAction,
)

ROLE_PERMISSIONS: Mapping[str, frozenset[MemoryAction]] = {
    "subject_self": frozenset(
        {
            MemoryAction.EVIDENCE_INGEST,
            MemoryAction.BELIEF_READ_CURRENT,
            MemoryAction.BELIEF_READ_HISTORY,
            MemoryAction.BELIEF_CORRECT,
            MemoryAction.EXPERIENCE_USAGE_WRITE,
            MemoryAction.EXPERIENCE_OUTCOME_WRITE,
            MemoryAction.EXPERIENCE_UTILITY_READ,
            MemoryAction.PROJECTION_COMPRESS,
            MemoryAction.PROJECTION_RECALL,
        }
    ),
    "memory_reader": frozenset(
        {
            MemoryAction.BELIEF_READ_CURRENT,
            MemoryAction.BELIEF_READ_HISTORY,
            MemoryAction.PROJECTION_COMPRESS,
            MemoryAction.PROJECTION_RECALL,
        }
    ),
    "memory_operator": frozenset(
        {
            MemoryAction.EVIDENCE_INGEST,
            MemoryAction.BELIEF_READ_CURRENT,
            MemoryAction.BELIEF_READ_HISTORY,
            MemoryAction.BELIEF_CORRECT,
            MemoryAction.EXPERIENCE_USAGE_WRITE,
            MemoryAction.EXPERIENCE_OUTCOME_WRITE,
            MemoryAction.EXPERIENCE_UTILITY_READ,
            MemoryAction.PROJECTION_COMPRESS,
            MemoryAction.PROJECTION_RECALL,
        }
    ),
    "service_agent": frozenset(
        {
            MemoryAction.EVIDENCE_INGEST,
            MemoryAction.BELIEF_READ_CURRENT,
            MemoryAction.EXPERIENCE_USAGE_WRITE,
            MemoryAction.EXPERIENCE_OUTCOME_WRITE,
            MemoryAction.PROJECTION_COMPRESS,
            MemoryAction.PROJECTION_RECALL,
        }
    ),
    "privacy_officer": frozenset(
        {
            MemoryAction.EVIDENCE_READ_RAW,
            MemoryAction.EVIDENCE_EXPORT,
            MemoryAction.GOVERNANCE_PRIVACY_SUPPRESS,
            MemoryAction.GOVERNANCE_ERASURE_APPROVE,
        }
    ),
    "auditor": frozenset({MemoryAction.GOVERNANCE_AUDIT_READ}),
    "tenant_admin": frozenset(
        {
            MemoryAction.GOVERNANCE_ROLE_MANAGE,
            MemoryAction.GOVERNANCE_POLICY_MANAGE,
        }
    ),
    "strategy_operator": frozenset(
        {
            MemoryAction.EVOLUTION_STRATEGY_PROPOSE,
            MemoryAction.EVOLUTION_STRATEGY_PROMOTE,
            MemoryAction.EVOLUTION_STRATEGY_ROLLBACK,
        }
    ),
    "platform_operator": frozenset(
        {
            MemoryAction.PROJECTION_REBUILD,
            MemoryAction.GOVERNANCE_OUTBOX_REPLAY,
        }
    ),
    "development_admin": frozenset(MemoryAction),
}


class RolePolicyAuthorizer:
    """Default-deny RBAC + tenant/subject/purpose policy decision point."""

    def __init__(
        self,
        *,
        policy_version: str = "builtin-v1",
        permissions: Mapping[str, frozenset[MemoryAction]] = ROLE_PERMISSIONS,
        decision_ids: Callable[[], UUID] = uuid4,
    ) -> None:
        self._policy_version = policy_version
        self._permissions = dict(permissions)
        self._decision_ids = decision_ids

    def decide(self, request: AuthorizationRequest) -> AuthorizationDecision:
        tenant_grants = tuple(
            grant
            for grant in request.actor.grants
            if grant.tenant_id in {"*", request.resource.scope.tenant_id}
        )
        if not tenant_grants:
            return self._decision(False, "scope_not_granted")

        subject_grants = tuple(
            grant for grant in tenant_grants if grant.covers(request.resource.scope)
        )
        if not subject_grants:
            return self._decision(False, "scope_not_granted")

        action_grants = tuple(
            grant
            for grant in subject_grants
            if any(
                request.action in self._permissions.get(role, frozenset()) for role in grant.roles
            )
        )
        if not action_grants:
            return self._decision(False, "action_not_granted")

        if not any(grant.permits_purpose(request.purpose) for grant in action_grants):
            return self._decision(False, "purpose_not_granted")

        return self._decision(True, "explicit_grant")

    def _decision(self, allowed: bool, reason: str) -> AuthorizationDecision:
        return AuthorizationDecision(
            id=self._decision_ids(),
            allowed=allowed,
            reason=reason,
            policy_version=self._policy_version,
        )


class LoggingAuthorizationAuditSink:
    """Emit pseudonymous, metadata-only authorization decisions."""

    def __init__(self, hmac_key: bytes) -> None:
        if len(hmac_key) < 16:
            raise ValueError("authorization audit HMAC key must contain at least 16 bytes")
        self._hmac_key = hmac_key
        self._logger = logging.getLogger("evolvable_memory.authorization")

    def record(self, event: AuthorizationAuditEvent) -> None:
        values: dict[str, object] = {
            "event": "authorization_decision",
            "decision_id": str(event.decision.id),
            "allowed": event.decision.allowed,
            "reason": event.decision.reason,
            "policy_version": event.decision.policy_version,
            "action": event.action.value,
            "plane": event.resource.plane.value,
            "purpose": event.purpose,
            "request_id": event.request_id,
            "recorded_at": event.recorded_at.isoformat(),
            "principal_kind": event.actor.kind.value,
            "authentication_method": event.actor.authentication_method,
            "principal_ref": self._reference("principal", event.actor.principal_id),
            "tenant_ref": self._reference("tenant", event.resource.scope.tenant_id),
            "subject_ref": self._reference("subject", event.resource.scope.subject_id),
        }
        if event.actor.client_id is not None:
            values["client_ref"] = self._reference("client", event.actor.client_id)
        if event.resource.resource_id is not None:
            values["resource_ref"] = self._reference(
                "resource",
                event.resource.resource_id,
            )
        self._logger.info(
            json.dumps(
                values,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def _reference(self, kind: str, value: str) -> str:
        digest = hmac.new(
            self._hmac_key,
            f"{kind}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return digest[:24]


class InMemoryAuthorizationAuditSink:
    """Thread-safe audit collector for deterministic tests and local diagnostics."""

    def __init__(self) -> None:
        self._events: list[AuthorizationAuditEvent] = []
        self._lock = Lock()

    @property
    def events(self) -> tuple[AuthorizationAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def record(self, event: AuthorizationAuditEvent) -> None:
        with self._lock:
            self._events.append(event)


class PostgresAuthorizationAuditSink:
    """Durable, metadata-only authorization evidence; failures block the operation."""

    def __init__(
        self,
        database_url: str,
        *,
        hmac_key: bytes,
        pseudonym_key_id: str,
        min_size: int = 1,
        max_size: int = 4,
        open_timeout: float = 10.0,
        readiness_timeout: float = 1.0,
    ) -> None:
        if len(hmac_key) < 32:
            raise ValueError("persistent audit HMAC key must contain at least 32 bytes")
        if not pseudonym_key_id.strip():
            raise ValueError("pseudonym_key_id must not be blank")
        conninfo = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._pool: ConnectionPool[Any] = ConnectionPool(
            conninfo,
            kwargs={"row_factory": dict_row},
            min_size=min_size,
            max_size=max_size,
            open=True,
            check=ConnectionPool.check_connection,
        )
        self._pool.wait(timeout=open_timeout)
        self._hmac_key = hmac_key
        self._pseudonym_key_id = pseudonym_key_id.strip()
        self._readiness_timeout = readiness_timeout

    def close(self) -> None:
        self._pool.close()

    def is_ready(self) -> bool:
        try:
            with self._pool.connection(timeout=self._readiness_timeout) as connection:
                row = connection.execute(
                    "SELECT to_regclass('authorization_audit_events') AS audit_table, "
                    "EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'trg_authorization_audit_append_only' "
                    "AND NOT tgisinternal) AS append_only_guard"
                ).fetchone()
            return bool(row and row["audit_table"] and row["append_only_guard"])
        except (OSError, PoolClosed, PoolTimeout, PsycopgError):
            return False

    def record(self, event: AuthorizationAuditEvent) -> None:
        scope = event.resource.scope
        with self._pool.connection() as connection, connection.transaction():
            connection.execute(
                """
                INSERT INTO authorization_audit_events (
                    decision_id, allowed, reason, policy_version, action, plane,
                    purpose, request_id, recorded_at, principal_kind,
                    authentication_method, pseudonym_key_id, principal_ref,
                    tenant_ref, subject_ref, client_ref, resource_ref, token_ref
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          %s, %s, %s, %s, %s, %s)
                ON CONFLICT (decision_id) DO NOTHING
                """,
                (
                    event.decision.id,
                    event.decision.allowed,
                    event.decision.reason,
                    event.decision.policy_version,
                    event.action.value,
                    event.resource.plane.value,
                    event.purpose,
                    event.request_id,
                    event.recorded_at,
                    event.actor.kind.value,
                    event.actor.authentication_method,
                    self._pseudonym_key_id,
                    self._reference("principal", event.actor.principal_id),
                    self._reference("tenant", scope.tenant_id),
                    self._reference("subject", f"{scope.tenant_id}\0{scope.subject_id}"),
                    (
                        self._reference("client", event.actor.client_id)
                        if event.actor.client_id is not None
                        else None
                    ),
                    (
                        self._reference("resource", event.resource.resource_id)
                        if event.resource.resource_id is not None
                        else None
                    ),
                    (
                        self._reference("token", event.actor.token_id)
                        if event.actor.token_id is not None
                        else None
                    ),
                ),
            )

    def _reference(self, kind: str, value: str) -> str:
        return hmac.new(
            self._hmac_key,
            f"{kind}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()
