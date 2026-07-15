from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from evolvable_memory.domain.common import ContextSignature, Scope
from evolvable_memory.domain.evidence import Candidate, EvidenceSpan, Observation
from evolvable_memory.domain.evolution import StrategySnapshot
from evolvable_memory.domain.experience import (
    OutcomeEvent,
    RecallTrace,
    UtilityEstimate,
)
from evolvable_memory.domain.memory import (
    MemoryRecord,
    MemoryRevision,
    MemorySnapshot,
    RevisionTransition,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new(self) -> UUID: ...


class MemoryStore(Protocol):
    def close(self) -> None: ...

    def is_ready(self) -> bool: ...

    def transaction(self) -> AbstractContextManager[None]: ...

    def save_strategy(self, strategy: StrategySnapshot) -> None: ...

    def observation_by_idempotency(
        self, scope: Scope, idempotency_key: str
    ) -> Observation | None: ...

    def save_ingestion(
        self,
        observation: Observation,
        evidence: EvidenceSpan,
        candidate: Candidate,
    ) -> None: ...

    def candidate_for_observation(
        self,
        scope: Scope,
        observation_id: UUID,
    ) -> Candidate | None: ...

    def update_candidate(self, candidate: Candidate) -> None: ...

    def current_by_identity(
        self,
        scope: Scope,
        key: str,
        context: ContextSignature,
    ) -> MemorySnapshot | None: ...

    def snapshot(self, scope: Scope, record_id: UUID) -> MemorySnapshot | None: ...

    def add_memory(
        self,
        record: MemoryRecord,
        revision: MemoryRevision,
        transition: RevisionTransition,
    ) -> None: ...

    def append_revision(
        self,
        *,
        expected_revision_id: UUID,
        revision: MemoryRevision,
        transition: RevisionTransition,
    ) -> None: ...

    def active_memories(self, scope: Scope) -> tuple[MemorySnapshot, ...]: ...

    def revision_history(self, scope: Scope, record_id: UUID) -> tuple[MemoryRevision, ...]: ...

    def save_trace(self, trace: RecallTrace) -> None: ...

    def trace(self, scope: Scope, trace_id: UUID) -> RecallTrace | None: ...

    def utility_for(
        self,
        scope: Scope,
        revision_id: UUID,
        context: ContextSignature,
    ) -> UtilityEstimate: ...

    def apply_outcome(
        self,
        outcome: OutcomeEvent,
        context: ContextSignature,
    ) -> tuple[OutcomeEvent, UtilityEstimate, bool]: ...
