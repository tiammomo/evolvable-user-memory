from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import Error as PsycopgError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from evolvable_memory.domain.common import ConflictError, NotFoundError, Scope
from evolvable_memory.domain.governance import (
    ErasureRequest,
    ErasureStatus,
    ErasureSummary,
    GovernanceUnavailableError,
    ProcessingDeniedError,
    ProcessingGrant,
    SuppressionFence,
)


class PostgresPrivacyGovernance:
    """Persistent, pseudonymous privacy control plane with fail-closed scope locks."""

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
            raise ValueError("persistent governance HMAC key must contain at least 32 bytes")
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
                    "SELECT to_regclass('processing_grants') AS grants, "
                    "to_regclass('suppression_fences') AS fences, "
                    "to_regclass('erasure_requests') AS erasures, "
                    "EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'trg_suppression_fences_append_only' "
                    "AND NOT tgisinternal) AS suppression_guard, "
                    "EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgname = 'trg_completed_erasure_receipts_immutable' "
                    "AND NOT tgisinternal) AS erasure_guard"
                ).fetchone()
            return bool(row and all(row.values()))
        except (OSError, PoolClosed, PoolTimeout, PsycopgError):
            return False

    @contextmanager
    def processing_context(
        self,
        scope: Scope,
        *,
        purpose: str,
        at: datetime,
    ) -> Iterator[None]:
        refs = self._scope_refs(scope)
        lock_id = self._scope_lock_id(scope)
        try:
            with self._pool.connection() as connection:
                connection.execute("SELECT pg_advisory_lock_shared(%s)", (lock_id,))
                try:
                    suppressed = connection.execute(
                        """
                        SELECT 1 FROM suppression_fences
                        WHERE pseudonym_key_id = %s AND tenant_ref = %s AND subject_ref = %s
                        """,
                        (self._pseudonym_key_id, *refs),
                    ).fetchone()
                    if suppressed is not None:
                        raise ProcessingDeniedError("processing_suppressed")
                    grant = connection.execute(
                        """
                        SELECT 1 FROM processing_grants
                        WHERE pseudonym_key_id = %s AND tenant_ref = %s AND subject_ref = %s
                          AND %s = ANY(purposes) AND revoked_at IS NULL
                          AND valid_from <= %s AND (valid_until IS NULL OR %s < valid_until)
                        LIMIT 1
                        """,
                        (self._pseudonym_key_id, *refs, purpose, at, at),
                    ).fetchone()
                    if grant is None:
                        raise ProcessingDeniedError("processing_not_granted")
                    yield
                finally:
                    connection.execute("SELECT pg_advisory_unlock_shared(%s)", (lock_id,))
        except ProcessingDeniedError:
            raise
        except (OSError, PoolClosed, PoolTimeout, PsycopgError) as exc:
            raise GovernanceUnavailableError("privacy governance is unavailable") from exc

    @contextmanager
    def projection_context(self, scope: Scope, *, at: datetime) -> Iterator[None]:
        del at
        refs = self._scope_refs(scope)
        lock_id = self._scope_lock_id(scope)
        try:
            with self._pool.connection() as connection:
                connection.execute("SELECT pg_advisory_lock_shared(%s)", (lock_id,))
                try:
                    suppressed = connection.execute(
                        """
                        SELECT 1 FROM suppression_fences
                        WHERE pseudonym_key_id = %s AND tenant_ref = %s AND subject_ref = %s
                        """,
                        (self._pseudonym_key_id, *refs),
                    ).fetchone()
                    if suppressed is not None:
                        raise ProcessingDeniedError("processing_suppressed")
                    yield
                finally:
                    connection.execute("SELECT pg_advisory_unlock_shared(%s)", (lock_id,))
        except ProcessingDeniedError:
            raise
        except (OSError, PoolClosed, PoolTimeout, PsycopgError) as exc:
            raise GovernanceUnavailableError("privacy governance is unavailable") from exc

    def issue_processing_grant(
        self,
        *,
        grant_id: UUID,
        scope: Scope,
        purposes: tuple[str, ...],
        lawful_basis: str,
        policy_version: str,
        issued_by: str,
        idempotency_key: str,
        valid_from: datetime,
        valid_until: datetime | None,
        created_at: datetime,
    ) -> ProcessingGrant:
        tenant_ref, subject_ref = self._scope_refs(scope)
        proposed = ProcessingGrant(
            id=grant_id,
            tenant_ref=tenant_ref,
            subject_ref=subject_ref,
            purposes=purposes,
            lawful_basis=lawful_basis,
            policy_version=policy_version,
            issued_by_ref=self._reference("principal", issued_by),
            idempotency_key=idempotency_key,
            valid_from=valid_from,
            valid_until=valid_until,
            created_at=created_at,
        )
        try:
            with self._pool.connection() as connection, connection.transaction():
                existing = connection.execute(
                    """
                    SELECT * FROM processing_grants
                    WHERE pseudonym_key_id = %s AND tenant_ref = %s AND subject_ref = %s
                      AND idempotency_key = %s
                    FOR UPDATE
                    """,
                    (self._pseudonym_key_id, tenant_ref, subject_ref, idempotency_key),
                ).fetchone()
                if existing is not None:
                    result = _processing_grant(existing)
                    if _grant_business_values(result) != _grant_business_values(proposed):
                        raise ConflictError(
                            "processing grant idempotency key was reused with different data"
                        )
                    return result
                connection.execute(
                    """
                    INSERT INTO processing_grants (
                        id, pseudonym_key_id, tenant_ref, subject_ref, purposes,
                        lawful_basis, policy_version, issued_by_ref, idempotency_key,
                        valid_from, valid_until, created_at, revoked_at, revoked_by_ref
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL)
                    """,
                    (
                        proposed.id,
                        self._pseudonym_key_id,
                        proposed.tenant_ref,
                        proposed.subject_ref,
                        list(proposed.purposes),
                        proposed.lawful_basis,
                        proposed.policy_version,
                        proposed.issued_by_ref,
                        proposed.idempotency_key,
                        proposed.valid_from,
                        proposed.valid_until,
                        proposed.created_at,
                    ),
                )
            return proposed
        except ConflictError:
            raise
        except (OSError, PoolClosed, PoolTimeout, PsycopgError) as exc:
            raise GovernanceUnavailableError("privacy governance is unavailable") from exc

    def revoke_processing_grant(
        self,
        *,
        scope: Scope,
        grant_id: UUID,
        revoked_by: str,
        revoked_at: datetime,
    ) -> ProcessingGrant:
        refs = self._scope_refs(scope)
        try:
            with self._pool.connection() as connection, connection.transaction():
                row = connection.execute(
                    """
                    SELECT * FROM processing_grants
                    WHERE id = %s AND pseudonym_key_id = %s
                      AND tenant_ref = %s AND subject_ref = %s
                    FOR UPDATE
                    """,
                    (grant_id, self._pseudonym_key_id, *refs),
                ).fetchone()
                if row is None:
                    raise NotFoundError("processing grant not found in scope")
                current = _processing_grant(row)
                if current.revoked_at is not None:
                    return current
                row = connection.execute(
                    """
                    UPDATE processing_grants
                    SET revoked_at = %s, revoked_by_ref = %s
                    WHERE id = %s
                    RETURNING *
                    """,
                    (revoked_at, self._reference("principal", revoked_by), grant_id),
                ).fetchone()
            if row is None:
                raise GovernanceUnavailableError("processing grant revocation was not persisted")
            return _processing_grant(row)
        except (ConflictError, NotFoundError, GovernanceUnavailableError):
            raise
        except (OSError, PoolClosed, PoolTimeout, PsycopgError) as exc:
            raise GovernanceUnavailableError("privacy governance is unavailable") from exc

    def suppress(
        self,
        *,
        fence_id: UUID,
        scope: Scope,
        reason_code: str,
        policy_version: str,
        requested_by: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> SuppressionFence:
        return self._suppress(
            fence_id=fence_id,
            scope=scope,
            reason_code=reason_code,
            policy_version=policy_version,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            created_at=created_at,
        )

    def _suppress(
        self,
        *,
        fence_id: UUID,
        scope: Scope,
        reason_code: str,
        policy_version: str,
        requested_by: str,
        idempotency_key: str,
        created_at: datetime,
        connection: Any | None = None,
    ) -> SuppressionFence:
        refs = self._scope_refs(scope)
        proposed = SuppressionFence(
            id=fence_id,
            tenant_ref=refs[0],
            subject_ref=refs[1],
            reason_code=reason_code,
            policy_version=policy_version,
            requested_by_ref=self._reference("principal", requested_by),
            idempotency_key=idempotency_key,
            created_at=created_at,
        )
        if connection is None:
            try:
                with self._pool.connection() as owned, owned.transaction():
                    self._lock_scope(owned, scope)
                    return self._persist_suppression(owned, proposed)
            except (OSError, PoolClosed, PoolTimeout, PsycopgError) as exc:
                raise GovernanceUnavailableError("privacy governance is unavailable") from exc
        return self._persist_suppression(connection, proposed)

    def _persist_suppression(self, connection: Any, proposed: SuppressionFence) -> SuppressionFence:
        row = connection.execute(
            """
            SELECT * FROM suppression_fences
            WHERE pseudonym_key_id = %s AND tenant_ref = %s AND subject_ref = %s
            FOR UPDATE
            """,
            (self._pseudonym_key_id, proposed.tenant_ref, proposed.subject_ref),
        ).fetchone()
        if row is not None:
            return _suppression_fence(row)
        connection.execute(
            """
            INSERT INTO suppression_fences (
                id, pseudonym_key_id, tenant_ref, subject_ref, reason_code,
                policy_version, requested_by_ref, idempotency_key, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                proposed.id,
                self._pseudonym_key_id,
                proposed.tenant_ref,
                proposed.subject_ref,
                proposed.reason_code,
                proposed.policy_version,
                proposed.requested_by_ref,
                proposed.idempotency_key,
                proposed.created_at,
            ),
        )
        return proposed

    def begin_erasure(
        self,
        *,
        request_id: UUID,
        fence_id: UUID,
        scope: Scope,
        reason_code: str,
        policy_version: str,
        requested_by: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> ErasureRequest:
        refs = self._scope_refs(scope)
        try:
            with self._pool.connection() as connection, connection.transaction():
                self._lock_scope(connection, scope)
                row = connection.execute(
                    """
                    SELECT * FROM erasure_requests
                    WHERE pseudonym_key_id = %s AND tenant_ref = %s AND subject_ref = %s
                      AND idempotency_key = %s
                    FOR UPDATE
                    """,
                    (self._pseudonym_key_id, *refs, idempotency_key),
                ).fetchone()
                if row is not None:
                    existing = _erasure_request(row)
                    if (
                        existing.reason_code != reason_code
                        or existing.policy_version != policy_version
                    ):
                        raise ConflictError(
                            "erasure idempotency key was reused with different data"
                        )
                    return existing
                self._suppress(
                    fence_id=fence_id,
                    scope=scope,
                    reason_code=reason_code,
                    policy_version=policy_version,
                    requested_by=requested_by,
                    idempotency_key=f"erasure:{idempotency_key}",
                    created_at=created_at,
                    connection=connection,
                )
                request = ErasureRequest(
                    id=request_id,
                    tenant_ref=refs[0],
                    subject_ref=refs[1],
                    scope_digest=self._scope_digest(scope),
                    reason_code=reason_code,
                    policy_version=policy_version,
                    requested_by_ref=self._reference("principal", requested_by),
                    idempotency_key=idempotency_key,
                    status=ErasureStatus.PENDING,
                    created_at=created_at,
                )
                connection.execute(
                    """
                    INSERT INTO erasure_requests (
                        id, pseudonym_key_id, tenant_ref, subject_ref, scope_digest,
                        reason_code, policy_version, requested_by_ref, idempotency_key,
                        status, created_at, completed_at, summary, handler_results, error_code
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              NULL, NULL, %s, NULL)
                    """,
                    (
                        request.id,
                        self._pseudonym_key_id,
                        request.tenant_ref,
                        request.subject_ref,
                        request.scope_digest,
                        request.reason_code,
                        request.policy_version,
                        request.requested_by_ref,
                        request.idempotency_key,
                        request.status.value,
                        request.created_at,
                        Jsonb({}),
                    ),
                )
            return request
        except (ConflictError, NotFoundError):
            raise
        except (OSError, PoolClosed, PoolTimeout, PsycopgError) as exc:
            raise GovernanceUnavailableError("privacy governance is unavailable") from exc

    def complete_erasure(
        self,
        *,
        scope: Scope,
        request_id: UUID,
        summary: ErasureSummary,
        handler_results: tuple[tuple[str, str], ...],
        completed_at: datetime,
    ) -> ErasureRequest:
        current = self.erasure(scope, request_id)
        if current is None:
            raise NotFoundError("erasure request not found in scope")
        if current.status is ErasureStatus.COMPLETED:
            return current
        try:
            with self._pool.connection() as connection, connection.transaction():
                row = connection.execute(
                    """
                    UPDATE erasure_requests
                    SET status = 'completed', completed_at = %s, summary = %s,
                        handler_results = %s, error_code = NULL
                    WHERE id = %s AND pseudonym_key_id = %s
                      AND tenant_ref = %s AND subject_ref = %s
                      AND status <> 'completed'
                    RETURNING *
                    """,
                    (
                        completed_at,
                        Jsonb(summary.as_dict()),
                        Jsonb(dict(handler_results)),
                        request_id,
                        self._pseudonym_key_id,
                        *self._scope_refs(scope),
                    ),
                ).fetchone()
            if row is None:
                replay = self.erasure(scope, request_id)
                if replay is None:
                    raise NotFoundError("erasure request not found in scope")
                return replay
            return _erasure_request(row)
        except NotFoundError:
            raise
        except (OSError, PoolClosed, PoolTimeout, PsycopgError) as exc:
            raise GovernanceUnavailableError("privacy governance is unavailable") from exc

    def fail_erasure(
        self,
        *,
        scope: Scope,
        request_id: UUID,
        error_code: str,
    ) -> ErasureRequest:
        try:
            with self._pool.connection() as connection, connection.transaction():
                row = connection.execute(
                    """
                    UPDATE erasure_requests
                    SET status = 'pending', error_code = %s
                    WHERE id = %s AND pseudonym_key_id = %s
                      AND tenant_ref = %s AND subject_ref = %s AND status <> 'completed'
                    RETURNING *
                    """,
                    (
                        error_code,
                        request_id,
                        self._pseudonym_key_id,
                        *self._scope_refs(scope),
                    ),
                ).fetchone()
            if row is None:
                current = self.erasure(scope, request_id)
                if current is None:
                    raise NotFoundError("erasure request not found in scope")
                return current
            return _erasure_request(row)
        except NotFoundError:
            raise
        except (OSError, PoolClosed, PoolTimeout, PsycopgError) as exc:
            raise GovernanceUnavailableError("privacy governance is unavailable") from exc

    def erasure(self, scope: Scope, request_id: UUID) -> ErasureRequest | None:
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM erasure_requests
                    WHERE id = %s AND pseudonym_key_id = %s
                      AND tenant_ref = %s AND subject_ref = %s
                    """,
                    (request_id, self._pseudonym_key_id, *self._scope_refs(scope)),
                ).fetchone()
            return _erasure_request(row) if row is not None else None
        except (OSError, PoolClosed, PoolTimeout, PsycopgError) as exc:
            raise GovernanceUnavailableError("privacy governance is unavailable") from exc

    def _lock_scope(self, connection: Any, scope: Scope) -> None:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (self._scope_lock_id(scope),))

    def _scope_lock_id(self, scope: Scope) -> int:
        digest = hmac.new(
            self._hmac_key,
            f"scope-lock:{scope.tenant_id}\0{scope.subject_id}".encode(),
            hashlib.sha256,
        ).digest()
        return int.from_bytes(digest[:8], byteorder="big", signed=True)

    def _scope_refs(self, scope: Scope) -> tuple[str, str]:
        return (
            self._reference("tenant", scope.tenant_id),
            self._reference("subject", f"{scope.tenant_id}\0{scope.subject_id}"),
        )

    def _scope_digest(self, scope: Scope) -> str:
        return self._reference("scope", f"{scope.tenant_id}\0{scope.subject_id}")

    def _reference(self, kind: str, value: str) -> str:
        return hmac.new(
            self._hmac_key,
            f"{kind}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()


def _grant_business_values(grant: ProcessingGrant) -> tuple[object, ...]:
    return (
        grant.tenant_ref,
        grant.subject_ref,
        grant.purposes,
        grant.lawful_basis,
        grant.policy_version,
        grant.valid_from,
        grant.valid_until,
    )


def _processing_grant(row: Mapping[str, Any]) -> ProcessingGrant:
    return ProcessingGrant(
        id=row["id"],
        tenant_ref=row["tenant_ref"],
        subject_ref=row["subject_ref"],
        purposes=tuple(row["purposes"]),
        lawful_basis=row["lawful_basis"],
        policy_version=row["policy_version"],
        issued_by_ref=row["issued_by_ref"],
        idempotency_key=row["idempotency_key"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
        revoked_by_ref=row["revoked_by_ref"],
    )


def _suppression_fence(row: Mapping[str, Any]) -> SuppressionFence:
    return SuppressionFence(
        id=row["id"],
        tenant_ref=row["tenant_ref"],
        subject_ref=row["subject_ref"],
        reason_code=row["reason_code"],
        policy_version=row["policy_version"],
        requested_by_ref=row["requested_by_ref"],
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
    )


def _erasure_request(row: Mapping[str, Any]) -> ErasureRequest:
    raw_summary = row.get("summary")
    summary = ErasureSummary(**raw_summary) if raw_summary else None
    raw_handlers = row.get("handler_results") or {}
    return ErasureRequest(
        id=row["id"],
        tenant_ref=row["tenant_ref"],
        subject_ref=row["subject_ref"],
        scope_digest=row["scope_digest"],
        reason_code=row["reason_code"],
        policy_version=row["policy_version"],
        requested_by_ref=row["requested_by_ref"],
        idempotency_key=row["idempotency_key"],
        status=ErasureStatus(row["status"]),
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        summary=summary,
        handler_results=tuple(sorted(raw_handlers.items())),
        error_code=row["error_code"],
    )
