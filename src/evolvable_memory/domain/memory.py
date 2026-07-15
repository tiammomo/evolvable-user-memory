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


class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PROSPECTIVE = "prospective"


class TransitionKind(StrEnum):
    CREATED = "created"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    SUPPRESSED = "suppressed"
    RESTORED = "restored"


@dataclass(frozen=True, slots=True)
class BeliefState:
    confidence: float
    support_count: int
    contradiction_count: int
    source_diversity: int
    last_evidence_at: datetime
    source_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise DomainError("belief confidence must be between 0 and 1")
        if min(self.support_count, self.contradiction_count, self.source_diversity) < 0:
            raise DomainError("belief counts must be non-negative")
        object.__setattr__(
            self, "last_evidence_at", require_utc(self.last_evidence_at, "last_evidence_at")
        )
        canonical_sources = tuple(
            sorted({require_text(source, "belief source") for source in self.source_keys})
        )
        if canonical_sources and self.source_diversity != len(canonical_sources):
            raise DomainError("source_diversity must match the number of known sources")
        object.__setattr__(self, "source_keys", canonical_sources)

    def reinforced(
        self,
        evidence_confidence: float,
        at: datetime,
        *,
        source: str | None = None,
    ) -> BeliefState:
        if not 0.0 <= evidence_confidence <= 1.0:
            raise DomainError("evidence confidence must be between 0 and 1")
        evidence_at = require_utc(at, "evidence_at")
        combined = 1.0 - ((1.0 - self.confidence) * (1.0 - evidence_confidence))
        sources = set(self.source_keys)
        if source is not None:
            sources.add(require_text(source, "belief source"))
        canonical_sources = tuple(sorted(sources))
        # Legacy states may only carry the aggregate count. In that case, retain the
        # count rather than guessing that an untracked source is new.
        source_diversity = (
            len(canonical_sources)
            if self.source_keys
            else max(self.source_diversity, len(canonical_sources))
        )
        return BeliefState(
            confidence=min(0.995, combined),
            support_count=self.support_count + 1,
            contradiction_count=self.contradiction_count,
            source_diversity=source_diversity,
            last_evidence_at=max(self.last_evidence_at, evidence_at),
            source_keys=canonical_sources if self.source_keys else (),
        )


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: UUID
    scope: Scope
    kind: MemoryKind
    key: str
    context: ContextSignature
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", require_text(self.key, "key"))
        object.__setattr__(self, "created_at", require_utc(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class MemoryRevision:
    id: UUID
    record_id: UUID
    sequence: int
    value: str
    belief: BeliefState
    evidence_ids: tuple[UUID, ...]
    valid_from: datetime
    recorded_at: datetime
    supersedes_revision_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise DomainError("revision sequence must be positive")
        object.__setattr__(self, "value", require_text(self.value, "value"))
        if not self.evidence_ids:
            raise DomainError("revision must reference evidence")
        object.__setattr__(self, "valid_from", require_utc(self.valid_from, "valid_from"))
        object.__setattr__(self, "recorded_at", require_utc(self.recorded_at, "recorded_at"))


@dataclass(frozen=True, slots=True)
class RevisionTransition:
    id: UUID
    record_id: UUID
    kind: TransitionKind
    occurred_at: datetime
    to_revision_id: UUID | None
    from_revision_id: UUID | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", require_utc(self.occurred_at, "occurred_at"))
        if self.reason is not None:
            object.__setattr__(self, "reason", require_text(self.reason, "reason"))


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    record: MemoryRecord
    revision: MemoryRevision

    def __post_init__(self) -> None:
        if self.record.id != self.revision.record_id:
            raise DomainError("snapshot record and revision do not match")
