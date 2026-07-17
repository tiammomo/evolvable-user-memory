from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from evolvable_memory.domain.common import ContextSignature, Scope, require_text, require_utc


@dataclass(frozen=True, slots=True)
class ProjectionHit:
    revision_id: UUID
    score: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("projection score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ProjectionSearchResult:
    hits: tuple[ProjectionHit, ...] = ()
    available: bool = True
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.available and self.reason is not None:
            raise ValueError("an available projection search cannot have a failure reason")
        if not self.available and self.hits:
            raise ValueError("an unavailable projection search cannot return hits")


@dataclass(frozen=True, slots=True)
class ProjectionDocument:
    source_event_id: UUID
    scope: Scope
    record_id: UUID
    revision_id: UUID
    key: str
    value: str
    context: ContextSignature
    valid_from: datetime
    recorded_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", require_text(self.key, "projection key"))
        object.__setattr__(self, "value", require_text(self.value, "projection value"))
        object.__setattr__(self, "valid_from", require_utc(self.valid_from, "valid_from"))
        object.__setattr__(self, "recorded_at", require_utc(self.recorded_at, "recorded_at"))

    @property
    def search_text(self) -> str:
        context = " ".join(f"{key} {value}" for key, value in self.context.facets)
        return " ".join(part for part in (self.key, self.value, context) if part)


@dataclass(frozen=True, slots=True)
class ProjectionWorkItem:
    event_id: UUID
    event_type: str
    scope: Scope
    record_id: UUID
    revision_id: UUID
    occurred_at: datetime
    attempts: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", require_text(self.event_type, "event_type"))
        object.__setattr__(self, "occurred_at", require_utc(self.occurred_at, "occurred_at"))
        if self.attempts < 1:
            raise ValueError("projection work attempts must be positive")


@dataclass(frozen=True, slots=True)
class ProjectionRunResult:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    dead_lettered: int = 0

    @property
    def idle(self) -> bool:
        return self.claimed == 0
