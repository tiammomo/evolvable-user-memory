from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from evolvable_memory.domain.common import (
    ContextSignature,
    Scope,
    require_text,
    require_utc,
)
from evolvable_memory.domain.experience import (
    OutcomeEvent,
    OutcomeKind,
    RecallTrace,
    UtilityEstimate,
)


@dataclass(frozen=True, slots=True)
class RememberPreference:
    scope: Scope
    source: str
    idempotency_key: str
    key: str
    value: str
    context: ContextSignature
    evidence_text: str
    confidence: float
    occurred_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(
            self,
            "idempotency_key",
            require_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "key", require_text(self.key, "key"))
        object.__setattr__(self, "value", require_text(self.value, "value"))
        object.__setattr__(
            self,
            "evidence_text",
            require_text(self.evidence_text, "evidence_text"),
        )
        object.__setattr__(
            self,
            "occurred_at",
            require_utc(self.occurred_at, "occurred_at"),
        )


@dataclass(frozen=True, slots=True)
class CorrectPreference:
    scope: Scope
    record_id: UUID
    source: str
    idempotency_key: str
    value: str
    evidence_text: str
    reason: str
    occurred_at: datetime
    expected_revision_id: UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(
            self,
            "idempotency_key",
            require_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "value", require_text(self.value, "value"))
        object.__setattr__(
            self,
            "evidence_text",
            require_text(self.evidence_text, "evidence_text"),
        )
        object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        object.__setattr__(
            self,
            "occurred_at",
            require_utc(self.occurred_at, "occurred_at"),
        )


@dataclass(frozen=True, slots=True)
class RecallMemory:
    scope: Scope
    query: str
    context: ContextSignature
    limit: int = 10

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", require_text(self.query, "query"))


@dataclass(frozen=True, slots=True)
class RecordOutcome:
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
            require_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(
            self,
            "occurred_at",
            require_utc(self.occurred_at, "occurred_at"),
        )
        if self.note is not None:
            normalized = self.note.strip()
            object.__setattr__(self, "note", normalized or None)


@dataclass(frozen=True, slots=True)
class PreferenceResult:
    observation_id: UUID
    candidate_id: UUID
    record_id: UUID
    revision_id: UUID
    sequence: int
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class OutcomeResult:
    outcome: OutcomeEvent
    utility: UtilityEstimate
    idempotent_replay: bool


RecallResult = RecallTrace
