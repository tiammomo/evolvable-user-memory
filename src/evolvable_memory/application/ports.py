from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from evolvable_memory.application.projection_types import (
    ProjectionDocument,
    ProjectionSearchResult,
    ProjectionWorkItem,
)
from evolvable_memory.application.security import (
    AuthorizationAuditEvent,
    AuthorizationDecision,
    AuthorizationRequest,
)
from evolvable_memory.domain.common import ContextSignature, Scope
from evolvable_memory.domain.evidence import Candidate, EvidenceSpan, Observation
from evolvable_memory.domain.evolution import (
    EvolutionExperiment,
    ExperimentTransition,
    GateReceipt,
    StrategyActivation,
    StrategySnapshot,
)
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


class EmbeddingPort(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, text: str) -> tuple[float, ...]: ...


class RecallProjectionPort(Protocol):
    def close(self) -> None: ...

    def is_ready(self) -> bool: ...

    def search(
        self,
        scope: Scope,
        *,
        query: str,
        limit: int,
        valid_at: datetime,
        known_at: datetime,
    ) -> ProjectionSearchResult: ...


class ProjectionSinkPort(Protocol):
    def close(self) -> None: ...

    def is_ready(self) -> bool: ...

    def upsert(self, document: ProjectionDocument) -> None: ...

    def reset(self) -> None: ...


class ProjectionEventSourcePort(Protocol):
    def close(self) -> None: ...

    def is_ready(self) -> bool: ...

    def discover(self, projection_name: str) -> int: ...

    def claim(
        self,
        projection_name: str,
        *,
        worker_id: str,
        limit: int,
        lease_until: datetime,
    ) -> tuple[ProjectionWorkItem, ...]: ...

    def load_document(self, item: ProjectionWorkItem) -> ProjectionDocument: ...

    def complete(
        self,
        projection_name: str,
        *,
        item: ProjectionWorkItem,
        worker_id: str,
        completed_at: datetime,
    ) -> None: ...

    def fail(
        self,
        projection_name: str,
        *,
        item: ProjectionWorkItem,
        worker_id: str,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
        dead_letter: bool,
    ) -> None: ...

    def requeue_all(self, projection_name: str, *, at: datetime) -> int: ...


class GateReceiptVerifierPort(Protocol):
    def verify(self, receipt: GateReceipt, *, at: datetime) -> None:
        """Fail closed unless the receipt is authentic and currently valid."""
        ...


class AuthorizationPort(Protocol):
    def decide(self, request: AuthorizationRequest) -> AuthorizationDecision: ...


class AuthorizationAuditPort(Protocol):
    def record(self, event: AuthorizationAuditEvent) -> None: ...


class StrategyRegistryPort(Protocol):
    def save_strategy(self, strategy: StrategySnapshot) -> None: ...

    def strategy(self, strategy_id: UUID) -> StrategySnapshot | None: ...

    def ensure_active_strategy(
        self,
        strategy: StrategySnapshot,
        activation: StrategyActivation,
    ) -> StrategySnapshot:
        """Atomically bootstrap once, otherwise return the authoritative strategy."""
        ...

    def active_strategy(self) -> StrategySnapshot | None: ...

    def strategy_activation_history(self) -> tuple[StrategyActivation, ...]: ...

    def register_evolution_experiment(
        self,
        candidate: StrategySnapshot,
        experiment: EvolutionExperiment,
        transition: ExperimentTransition,
    ) -> None: ...

    def evolution_experiment(self, experiment_id: UUID) -> EvolutionExperiment | None: ...

    def experiment_transition_history(
        self,
        experiment_id: UUID,
    ) -> tuple[ExperimentTransition, ...]: ...

    def experiment_transition_by_idempotency(
        self,
        idempotency_key: str,
    ) -> ExperimentTransition | None: ...

    def advance_evolution_experiment(
        self,
        experiment: EvolutionExperiment,
        transition: ExperimentTransition,
        activation: StrategyActivation | None = None,
    ) -> None:
        """Atomically persist a stage change and any required active-policy switch."""
        ...


class MemoryStore(StrategyRegistryPort, Protocol):
    def close(self) -> None: ...

    def is_ready(self) -> bool: ...

    def transaction(self) -> AbstractContextManager[None]: ...

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

    def memories_as_of(
        self,
        scope: Scope,
        *,
        valid_at: datetime,
        known_at: datetime,
    ) -> tuple[MemorySnapshot, ...]:
        """Return each transaction-latest revision eligible on both time axes."""
        ...

    def revision_history(self, scope: Scope, record_id: UUID) -> tuple[MemoryRevision, ...]: ...

    def save_trace(self, trace: RecallTrace) -> None: ...

    def trace(self, scope: Scope, trace_id: UUID) -> RecallTrace | None: ...

    def utility_for(
        self,
        scope: Scope,
        revision_id: UUID,
        context: ContextSignature,
    ) -> UtilityEstimate: ...

    def utility_for_as_of(
        self,
        scope: Scope,
        revision_id: UUID,
        context: ContextSignature,
        *,
        known_at: datetime,
    ) -> UtilityEstimate:
        """Rebuild utility only from attributable outcomes recorded by known_at."""
        ...

    def apply_outcome(
        self,
        outcome: OutcomeEvent,
        context: ContextSignature,
    ) -> tuple[OutcomeEvent, UtilityEstimate, bool]: ...
