from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from evolvable_memory.domain.common import Scope, require_text, require_utc


class PrincipalKind(StrEnum):
    USER = "user"
    SERVICE = "service"


class MemoryPlane(StrEnum):
    EVIDENCE = "evidence"
    BELIEF = "belief"
    EXPERIENCE = "experience"
    PROJECTION = "projection"
    EVOLUTION = "evolution"
    GOVERNANCE = "governance"


class MemoryAction(StrEnum):
    EVIDENCE_INGEST = "evidence.ingest"
    EVIDENCE_READ_RAW = "evidence.read_raw"
    EVIDENCE_EXPORT = "evidence.export"
    BELIEF_READ_CURRENT = "belief.read_current"
    BELIEF_READ_HISTORY = "belief.read_history"
    BELIEF_CORRECT = "belief.correct"
    EXPERIENCE_TRACE_READ = "experience.trace_read"
    EXPERIENCE_OUTCOME_WRITE = "experience.outcome_write"
    EXPERIENCE_UTILITY_READ = "experience.utility_read"
    PROJECTION_RECALL = "projection.recall"
    PROJECTION_COMPRESS = "projection.compress"
    PROJECTION_REBUILD = "projection.rebuild"
    EVOLUTION_STRATEGY_PROPOSE = "evolution.strategy_propose"
    EVOLUTION_STRATEGY_PROMOTE = "evolution.strategy_promote"
    EVOLUTION_STRATEGY_ROLLBACK = "evolution.strategy_rollback"
    GOVERNANCE_ROLE_MANAGE = "governance.role_manage"
    GOVERNANCE_POLICY_MANAGE = "governance.policy_manage"
    GOVERNANCE_PRIVACY_SUPPRESS = "governance.privacy_suppress"
    GOVERNANCE_ERASURE_APPROVE = "governance.erasure_approve"
    GOVERNANCE_AUDIT_READ = "governance.audit_read"
    GOVERNANCE_OUTBOX_REPLAY = "governance.outbox_replay"

    @property
    def plane(self) -> MemoryPlane:
        return _ACTION_PLANES[self]


_ACTION_PLANES: dict[MemoryAction, MemoryPlane] = {
    MemoryAction.EVIDENCE_INGEST: MemoryPlane.EVIDENCE,
    MemoryAction.EVIDENCE_READ_RAW: MemoryPlane.EVIDENCE,
    MemoryAction.EVIDENCE_EXPORT: MemoryPlane.EVIDENCE,
    MemoryAction.BELIEF_READ_CURRENT: MemoryPlane.BELIEF,
    MemoryAction.BELIEF_READ_HISTORY: MemoryPlane.BELIEF,
    MemoryAction.BELIEF_CORRECT: MemoryPlane.BELIEF,
    MemoryAction.EXPERIENCE_TRACE_READ: MemoryPlane.EXPERIENCE,
    MemoryAction.EXPERIENCE_OUTCOME_WRITE: MemoryPlane.EXPERIENCE,
    MemoryAction.EXPERIENCE_UTILITY_READ: MemoryPlane.EXPERIENCE,
    MemoryAction.PROJECTION_RECALL: MemoryPlane.PROJECTION,
    MemoryAction.PROJECTION_COMPRESS: MemoryPlane.PROJECTION,
    MemoryAction.PROJECTION_REBUILD: MemoryPlane.PROJECTION,
    MemoryAction.EVOLUTION_STRATEGY_PROPOSE: MemoryPlane.EVOLUTION,
    MemoryAction.EVOLUTION_STRATEGY_PROMOTE: MemoryPlane.EVOLUTION,
    MemoryAction.EVOLUTION_STRATEGY_ROLLBACK: MemoryPlane.EVOLUTION,
    MemoryAction.GOVERNANCE_ROLE_MANAGE: MemoryPlane.GOVERNANCE,
    MemoryAction.GOVERNANCE_POLICY_MANAGE: MemoryPlane.GOVERNANCE,
    MemoryAction.GOVERNANCE_PRIVACY_SUPPRESS: MemoryPlane.GOVERNANCE,
    MemoryAction.GOVERNANCE_ERASURE_APPROVE: MemoryPlane.GOVERNANCE,
    MemoryAction.GOVERNANCE_AUDIT_READ: MemoryPlane.GOVERNANCE,
    MemoryAction.GOVERNANCE_OUTBOX_REPLAY: MemoryPlane.GOVERNANCE,
}


@dataclass(frozen=True, slots=True)
class AccessGrant:
    """A trusted, tenant-local role binding carried by an authenticated principal."""

    tenant_id: str
    subject_ids: tuple[str, ...]
    roles: tuple[str, ...]
    purposes: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", require_text(self.tenant_id, "tenant_id"))
        object.__setattr__(
            self,
            "subject_ids",
            _normalized_values(self.subject_ids, "subject_ids"),
        )
        object.__setattr__(self, "roles", _normalized_values(self.roles, "roles"))
        object.__setattr__(
            self,
            "purposes",
            _normalized_values(self.purposes, "purposes"),
        )

    def covers(self, scope: Scope) -> bool:
        return self.tenant_id in {"*", scope.tenant_id} and (
            "*" in self.subject_ids or scope.subject_id in self.subject_ids
        )

    def permits_purpose(self, purpose: str) -> bool:
        return "*" in self.purposes or purpose in self.purposes


@dataclass(frozen=True, slots=True)
class ActorContext:
    principal_id: str
    kind: PrincipalKind
    grants: tuple[AccessGrant, ...]
    authentication_method: str
    client_id: str | None = None
    token_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "principal_id",
            require_text(self.principal_id, "principal_id"),
        )
        object.__setattr__(
            self,
            "authentication_method",
            require_text(self.authentication_method, "authentication_method"),
        )
        if not self.grants:
            raise ValueError("actor must contain at least one access grant")
        if self.client_id is not None:
            object.__setattr__(self, "client_id", require_text(self.client_id, "client_id"))
        if self.token_id is not None:
            object.__setattr__(self, "token_id", require_text(self.token_id, "token_id"))


@dataclass(frozen=True, slots=True)
class InvocationContext:
    actor: ActorContext
    purpose: str
    request_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "purpose", require_text(self.purpose, "purpose"))
        object.__setattr__(self, "request_id", require_text(self.request_id, "request_id"))


@dataclass(frozen=True, slots=True)
class ProtectedResource:
    scope: Scope
    plane: MemoryPlane
    resource_id: str | None = None

    def __post_init__(self) -> None:
        if self.resource_id is not None:
            object.__setattr__(
                self,
                "resource_id",
                require_text(self.resource_id, "resource_id"),
            )


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    actor: ActorContext
    action: MemoryAction
    resource: ProtectedResource
    purpose: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "purpose", require_text(self.purpose, "purpose"))
        if self.action.plane is not self.resource.plane:
            raise ValueError("authorization action and resource plane do not match")


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    id: UUID
    allowed: bool
    reason: str
    policy_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        object.__setattr__(
            self,
            "policy_version",
            require_text(self.policy_version, "policy_version"),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationAuditEvent:
    decision: AuthorizationDecision
    actor: ActorContext
    action: MemoryAction
    resource: ProtectedResource
    purpose: str
    request_id: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "purpose", require_text(self.purpose, "purpose"))
        object.__setattr__(self, "request_id", require_text(self.request_id, "request_id"))
        object.__setattr__(
            self,
            "recorded_at",
            require_utc(self.recorded_at, "recorded_at"),
        )


class AuthenticationError(Exception):
    """The request did not establish a trusted caller identity."""


class AuthorizationDeniedError(Exception):
    """The authenticated caller is not permitted to perform the requested action."""

    def __init__(self, *, conceal_resource: bool) -> None:
        self.conceal_resource = conceal_resource
        super().__init__("resource not found" if conceal_resource else "action is not permitted")


def _normalized_values(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    normalized = tuple(sorted({require_text(value, field) for value in values}))
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized
