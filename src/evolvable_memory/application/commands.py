from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from evolvable_memory.domain.common import ContextSignature, Scope
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


@dataclass(frozen=True, slots=True)
class RecallMemory:
    scope: Scope
    query: str
    context: ContextSignature
    limit: int = 10


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
