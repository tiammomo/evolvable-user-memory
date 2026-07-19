from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from evolvable_memory.domain.common import require_text, require_utc


class ErasureStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProcessingGrant:
    id: UUID
    tenant_ref: str
    subject_ref: str
    purposes: tuple[str, ...]
    lawful_basis: str
    policy_version: str
    issued_by_ref: str
    idempotency_key: str
    valid_from: datetime
    valid_until: datetime | None
    created_at: datetime
    revoked_at: datetime | None = None
    revoked_by_ref: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "tenant_ref",
            "subject_ref",
            "lawful_basis",
            "policy_version",
            "issued_by_ref",
            "idempotency_key",
        ):
            object.__setattr__(self, field, require_text(getattr(self, field), field))
        purposes = tuple(sorted({require_text(value, "purposes") for value in self.purposes}))
        if not purposes or "*" in purposes:
            raise ValueError("processing grant purposes must be explicit")
        object.__setattr__(self, "purposes", purposes)
        object.__setattr__(self, "valid_from", require_utc(self.valid_from, "valid_from"))
        object.__setattr__(self, "created_at", require_utc(self.created_at, "created_at"))
        if self.valid_until is not None:
            valid_until = require_utc(self.valid_until, "valid_until")
            if valid_until <= self.valid_from:
                raise ValueError("valid_until must be later than valid_from")
            object.__setattr__(self, "valid_until", valid_until)
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", require_utc(self.revoked_at, "revoked_at"))
            if self.revoked_by_ref is None:
                raise ValueError("revoked_by_ref is required for a revoked grant")
        if self.revoked_by_ref is not None:
            object.__setattr__(
                self,
                "revoked_by_ref",
                require_text(self.revoked_by_ref, "revoked_by_ref"),
            )


@dataclass(frozen=True, slots=True)
class SuppressionFence:
    id: UUID
    tenant_ref: str
    subject_ref: str
    reason_code: str
    policy_version: str
    requested_by_ref: str
    idempotency_key: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field in (
            "tenant_ref",
            "subject_ref",
            "reason_code",
            "policy_version",
            "requested_by_ref",
            "idempotency_key",
        ):
            object.__setattr__(self, field, require_text(getattr(self, field), field))
        object.__setattr__(self, "created_at", require_utc(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class ErasureSummary:
    observations: int = 0
    evidence_spans: int = 0
    candidates: int = 0
    memory_records: int = 0
    memory_revisions: int = 0
    revision_transitions: int = 0
    recall_traces: int = 0
    recall_trace_items: int = 0
    memory_usages: int = 0
    memory_usage_items: int = 0
    outcomes: int = 0
    utility_estimates: int = 0
    outbox_events: int = 0
    projection_jobs: int = 0
    projection_documents: int = 0

    def as_dict(self) -> dict[str, int]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    def with_projection_documents(self, count: int) -> ErasureSummary:
        if count < 0:
            raise ValueError("projection deletion count must not be negative")
        values = self.as_dict()
        values["projection_documents"] = count
        return ErasureSummary(**values)


@dataclass(frozen=True, slots=True)
class ErasureRequest:
    id: UUID
    tenant_ref: str
    subject_ref: str
    scope_digest: str
    reason_code: str
    policy_version: str
    requested_by_ref: str
    idempotency_key: str
    status: ErasureStatus
    created_at: datetime
    completed_at: datetime | None = None
    summary: ErasureSummary | None = None
    handler_results: tuple[tuple[str, str], ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        for field in (
            "tenant_ref",
            "subject_ref",
            "scope_digest",
            "reason_code",
            "policy_version",
            "requested_by_ref",
            "idempotency_key",
        ):
            object.__setattr__(self, field, require_text(getattr(self, field), field))
        object.__setattr__(self, "created_at", require_utc(self.created_at, "created_at"))
        if self.completed_at is not None:
            object.__setattr__(
                self,
                "completed_at",
                require_utc(self.completed_at, "completed_at"),
            )
        if self.status is ErasureStatus.COMPLETED:
            if self.completed_at is None or self.summary is None:
                raise ValueError("completed erasure requires completion evidence")
            if any(
                result not in {"completed", "not_applicable"} for _, result in self.handler_results
            ):
                raise ValueError("all required erasure handlers must be completed")
        if self.error_code is not None:
            object.__setattr__(self, "error_code", require_text(self.error_code, "error_code"))


class ProcessingDeniedError(Exception):
    """Current privacy governance does not permit ordinary processing."""

    def __init__(self, reason: str) -> None:
        self.reason = require_text(reason, "reason")
        super().__init__(reason)


class GovernanceUnavailableError(Exception):
    """A required governance dependency failed closed."""
