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


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    semantic: float
    context: float
    belief: float
    utility: float
    recency: float

    def __post_init__(self) -> None:
        for name, value in (
            ("semantic", self.semantic),
            ("context", self.context),
            ("belief", self.belief),
            ("utility", self.utility),
            ("recency", self.recency),
        ):
            if not 0.0 <= value <= 1.0:
                raise DomainError(f"{name} score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RecalledItem:
    record_id: UUID
    revision_id: UUID
    key: str
    value: str
    context: ContextSignature
    rank: int
    score: float
    breakdown: ScoreBreakdown
    evidence_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
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
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise DomainError("recall query must not be blank")
        object.__setattr__(self, "created_at", require_utc(self.created_at, "created_at"))


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
        return UtilityEstimate(
            revision_id=self.revision_id,
            context_fingerprint=self.context_fingerprint,
            positive_weight=self.positive_weight + (success * weight),
            negative_weight=self.negative_weight + ((1.0 - success) * weight),
            last_outcome_at=at,
        )
