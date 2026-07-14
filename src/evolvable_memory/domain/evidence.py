from __future__ import annotations

from dataclasses import dataclass, replace
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


class ObservationKind(StrEnum):
    MESSAGE = "message"
    ACTION = "action"
    TOOL_RESULT = "tool_result"
    USER_FEEDBACK = "user_feedback"
    OUTCOME = "outcome"


class EvidenceStance(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class CandidateState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class Observation:
    id: UUID
    scope: Scope
    kind: ObservationKind
    source: str
    content: str
    idempotency_key: str
    occurred_at: datetime
    ingested_at: datetime
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(self, "content", require_text(self.content, "content"))
        object.__setattr__(
            self, "idempotency_key", require_text(self.idempotency_key, "idempotency_key")
        )
        object.__setattr__(self, "occurred_at", require_utc(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "ingested_at", require_utc(self.ingested_at, "ingested_at"))
        object.__setattr__(self, "metadata", tuple(sorted(self.metadata)))


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    id: UUID
    observation_id: UUID
    quote: str
    stance: EvidenceStance
    start_offset: int = 0
    end_offset: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "quote", require_text(self.quote, "quote"))
        if self.start_offset < 0:
            raise DomainError("start_offset must be non-negative")
        end = len(self.quote) if self.end_offset is None else self.end_offset
        if end <= self.start_offset:
            raise DomainError("end_offset must be greater than start_offset")
        object.__setattr__(self, "end_offset", end)


@dataclass(frozen=True, slots=True)
class Candidate:
    id: UUID
    scope: Scope
    observation_id: UUID
    key: str
    value: str
    context: ContextSignature
    evidence_ids: tuple[UUID, ...]
    confidence: float
    proposed_at: datetime
    state: CandidateState = CandidateState.PROPOSED
    accepted_record_id: UUID | None = None
    accepted_revision_id: UUID | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", require_text(self.key, "key"))
        object.__setattr__(self, "value", require_text(self.value, "value"))
        object.__setattr__(self, "proposed_at", require_utc(self.proposed_at, "proposed_at"))
        if not self.evidence_ids:
            raise DomainError("candidate must reference at least one evidence span")
        if not 0.0 <= self.confidence <= 1.0:
            raise DomainError("confidence must be between 0 and 1")

    def accept(self, record_id: UUID, revision_id: UUID) -> Candidate:
        if self.state is not CandidateState.PROPOSED:
            raise DomainError("only proposed candidates can be accepted")
        return replace(
            self,
            state=CandidateState.ACCEPTED,
            accepted_record_id=record_id,
            accepted_revision_id=revision_id,
        )
