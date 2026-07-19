from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from evolvable_memory.domain.common import (
    ContextSignature,
    DomainError,
    Scope,
    require_text,
    require_utc,
)
from evolvable_memory.domain.projection import ContextCompressionAlgorithm


def _require_sha256(value: str, field: str) -> str:
    digest = require_text(value, field).lower()
    if len(digest) != 64:
        raise DomainError(f"{field} must be a SHA-256 hex digest")
    try:
        int(digest, 16)
    except ValueError as error:
        raise DomainError(f"{field} must be a SHA-256 hex digest") from error
    return digest


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    semantic: float
    context: float
    belief: float
    utility: float
    recency: float
    lexical: float | None = None
    vector: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("semantic", self.semantic),
            ("context", self.context),
            ("belief", self.belief),
            ("utility", self.utility),
            ("recency", self.recency),
            ("lexical", self.lexical),
            ("vector", self.vector),
        ):
            if value is not None and not 0.0 <= value <= 1.0:
                raise DomainError(f"{name} score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RecalledItem:
    record_id: UUID
    revision_id: UUID
    key: str
    value: str
    context: ContextSignature
    revision_valid_from: datetime
    revision_recorded_at: datetime
    rank: int
    score: float
    breakdown: ScoreBreakdown
    evidence_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "revision_valid_from",
            require_utc(self.revision_valid_from, "revision_valid_from"),
        )
        object.__setattr__(
            self,
            "revision_recorded_at",
            require_utc(self.revision_recorded_at, "revision_recorded_at"),
        )
        if self.rank < 1:
            raise DomainError("recall rank must be positive")
        if not 0.0 <= self.score <= 1.0:
            raise DomainError("recall score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RecallTrace:
    id: UUID
    scope: Scope
    query: str
    context: ContextSignature
    policy_id: UUID
    policy_version: int
    items: tuple[RecalledItem, ...]
    valid_at: datetime
    known_at: datetime
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise DomainError("recall query must not be blank")
        object.__setattr__(self, "valid_at", require_utc(self.valid_at, "valid_at"))
        object.__setattr__(self, "known_at", require_utc(self.known_at, "known_at"))
        object.__setattr__(self, "created_at", require_utc(self.created_at, "created_at"))
        if self.known_at > self.created_at:
            raise DomainError("known_at must not be later than trace creation")
        for item in self.items:
            if item.revision_valid_from > self.valid_at:
                raise DomainError("recalled revision was not valid at valid_at")
            if item.revision_recorded_at > self.known_at:
                raise DomainError("recalled revision was not known at known_at")


@dataclass(frozen=True, slots=True)
class MemoryUsage:
    """Immutable evidence that a bounded recall projection reached a consumer."""

    id: UUID
    scope: Scope
    trace_id: UUID
    algorithm: ContextCompressionAlgorithm
    budget_characters: int
    source_projection_sha256: str
    delivered_context_sha256: str
    revision_ids: tuple[UUID, ...]
    idempotency_key: str
    occurred_at: datetime
    recorded_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.algorithm, ContextCompressionAlgorithm):
            raise DomainError("memory usage projection algorithm is invalid")
        if not 64 <= self.budget_characters <= 100_000:
            raise DomainError("memory usage projection budget must be in [64, 100000]")
        object.__setattr__(
            self,
            "source_projection_sha256",
            _require_sha256(self.source_projection_sha256, "source_projection_sha256"),
        )
        object.__setattr__(
            self,
            "delivered_context_sha256",
            _require_sha256(self.delivered_context_sha256, "delivered_context_sha256"),
        )
        if not self.revision_ids:
            raise DomainError("memory usage must cite at least one revision")
        if len(set(self.revision_ids)) != len(self.revision_ids):
            raise DomainError("memory usage revisions must be distinct")
        object.__setattr__(
            self,
            "idempotency_key",
            require_text(self.idempotency_key, "usage idempotency_key"),
        )
        object.__setattr__(self, "occurred_at", require_utc(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "recorded_at", require_utc(self.recorded_at, "recorded_at"))


class OutcomeKind(StrEnum):
    HELPFUL = "helpful"
    ACCEPTED = "accepted"
    HARMFUL = "harmful"
    REJECTED = "rejected"
    CORRECTED = "corrected"

    @property
    def success_value(self) -> float:
        if self in {OutcomeKind.HELPFUL, OutcomeKind.ACCEPTED}:
            return 1.0
        return 0.0


@dataclass(frozen=True, slots=True)
class OutcomeEvent:
    id: UUID
    scope: Scope
    trace_id: UUID
    revision_id: UUID
    kind: OutcomeKind
    idempotency_key: str
    occurred_at: datetime
    recorded_at: datetime
    usage_id: UUID | None = None
    weight: float = 1.0
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "idempotency_key",
            require_text(self.idempotency_key, "outcome idempotency_key"),
        )
        if not 0.0 < self.weight <= 10.0:
            raise DomainError("outcome weight must be in (0, 10]")
        object.__setattr__(self, "occurred_at", require_utc(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "recorded_at", require_utc(self.recorded_at, "recorded_at"))
        if self.note is not None:
            normalized = self.note.strip()
            object.__setattr__(self, "note", normalized or None)


@dataclass(frozen=True, slots=True)
class UtilityEstimate:
    revision_id: UUID
    context_fingerprint: str
    positive_weight: float = 0.0
    negative_weight: float = 0.0
    last_outcome_at: datetime | None = None

    def __post_init__(self) -> None:
        if min(self.positive_weight, self.negative_weight) < 0.0:
            raise DomainError("utility weights must be non-negative")
        if self.last_outcome_at is not None:
            object.__setattr__(
                self,
                "last_outcome_at",
                require_utc(self.last_outcome_at, "last_outcome_at"),
            )

    @property
    def mean(self) -> float:
        return (1.0 + self.positive_weight) / (2.0 + self.positive_weight + self.negative_weight)

    @property
    def sample_weight(self) -> float:
        return self.positive_weight + self.negative_weight

    def updated(self, success: float, weight: float, at: datetime) -> UtilityEstimate:
        if not 0.0 <= success <= 1.0:
            raise DomainError("success must be between 0 and 1")
        if weight <= 0.0:
            raise DomainError("utility update weight must be positive")
        outcome_at = require_utc(at, "utility outcome time")
        return UtilityEstimate(
            revision_id=self.revision_id,
            context_fingerprint=self.context_fingerprint,
            positive_weight=self.positive_weight + (success * weight),
            negative_weight=self.negative_weight + ((1.0 - success) * weight),
            last_outcome_at=(
                outcome_at
                if self.last_outcome_at is None
                else max(self.last_outcome_at, outcome_at)
            ),
        )
