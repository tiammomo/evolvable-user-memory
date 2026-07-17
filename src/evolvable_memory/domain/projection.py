from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from evolvable_memory.domain.common import DomainError, Scope, require_text, require_utc


class ContextCompressionAlgorithm(StrEnum):
    """Safe, deterministic algorithms for disposable recall-context projections."""

    RANKED_EXTRACTIVE = "ranked-extractive-v1"
    EXACT_DEDUPLICATED = "exact-deduplicated-v1"

    @property
    def version(self) -> int:
        return 1


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
class ContextProjectionSource:
    record_id: UUID
    revision_id: UUID
    rank: int
    score: float

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise DomainError("projection source rank must be positive")
        if not 0.0 <= self.score <= 1.0:
            raise DomainError("projection source score must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ContextProjectionSegment:
    """One extractive JSON object and every revision that it represents."""

    content: str
    sources: tuple[ContextProjectionSource, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", require_text(self.content, "segment content"))
        if not self.sources:
            raise DomainError("projection segment must reference at least one source")
        revision_ids = {source.revision_id for source in self.sources}
        if len(revision_ids) != len(self.sources):
            raise DomainError("projection segment sources must use distinct revisions")
        object.__setattr__(
            self,
            "sources",
            tuple(sorted(self.sources, key=lambda source: (source.rank, str(source.revision_id)))),
        )


@dataclass(frozen=True, slots=True)
class RecallContextProjection:
    """Disposable, attributable context derived from one immutable RecallTrace."""

    scope: Scope
    trace_id: UUID
    policy_id: UUID
    policy_version: int
    algorithm: ContextCompressionAlgorithm
    budget_characters: int
    valid_at: datetime
    known_at: datetime
    trace_created_at: datetime
    content: str
    segments: tuple[ContextProjectionSegment, ...]
    source_item_count: int
    omitted_item_count: int
    original_character_count: int
    configuration_sha256: str
    source_sha256: str
    projection_sha256: str

    def __post_init__(self) -> None:
        if self.policy_version < 1:
            raise DomainError("projection policy version must be positive")
        if not 64 <= self.budget_characters <= 100_000:
            raise DomainError("projection budget must be in [64, 100000] characters")
        object.__setattr__(self, "valid_at", require_utc(self.valid_at, "valid_at"))
        object.__setattr__(self, "known_at", require_utc(self.known_at, "known_at"))
        object.__setattr__(
            self,
            "trace_created_at",
            require_utc(self.trace_created_at, "trace_created_at"),
        )
        if self.known_at > self.trace_created_at:
            raise DomainError("projection knowledge time must not follow trace creation")
        if len(self.content) > self.budget_characters:
            raise DomainError("projection content exceeds its character budget")
        if (
            min(
                self.source_item_count,
                self.omitted_item_count,
                self.original_character_count,
            )
            < 0
        ):
            raise DomainError("projection counts must be non-negative")
        expected_content = (
            f'{{"memories":[{",".join(segment.content for segment in self.segments)}]}}'
        )
        if self.content != expected_content:
            raise DomainError("projection content must exactly represent its segments")
        if self.original_character_count < len(self.content):
            raise DomainError("projection cannot exceed its original source representation")
        included_sources = tuple(source for segment in self.segments for source in segment.sources)
        if len({source.revision_id for source in included_sources}) != len(included_sources):
            raise DomainError("a revision cannot appear in multiple projection segments")
        if len(included_sources) + self.omitted_item_count != self.source_item_count:
            raise DomainError("projection included and omitted counts must cover all sources")
        object.__setattr__(
            self,
            "configuration_sha256",
            _require_sha256(self.configuration_sha256, "configuration_sha256"),
        )
        object.__setattr__(
            self,
            "source_sha256",
            _require_sha256(self.source_sha256, "source_sha256"),
        )
        object.__setattr__(
            self,
            "projection_sha256",
            _require_sha256(self.projection_sha256, "projection_sha256"),
        )

    @property
    def included_item_count(self) -> int:
        return self.source_item_count - self.omitted_item_count

    @property
    def compression_ratio(self) -> float:
        return len(self.content) / self.original_character_count

    @property
    def source_revision_ids(self) -> tuple[UUID, ...]:
        return tuple(source.revision_id for segment in self.segments for source in segment.sources)
