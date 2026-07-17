from __future__ import annotations

from contextlib import AbstractContextManager
from copy import deepcopy
from datetime import datetime
from threading import RLock
from types import TracebackType
from uuid import UUID

from evolvable_memory.domain.common import ConflictError, ContextSignature, Scope
from evolvable_memory.domain.evidence import Candidate, EvidenceSpan, Observation
from evolvable_memory.domain.evolution import (
    EvolutionExperiment,
    ExperimentStage,
    ExperimentTransition,
    StrategyActivation,
    StrategyActivationKind,
    StrategySnapshot,
)
from evolvable_memory.domain.experience import OutcomeEvent, RecallTrace, UtilityEstimate
from evolvable_memory.domain.memory import (
    MemoryRecord,
    MemoryRevision,
    MemorySnapshot,
    RevisionTransition,
)

_TRANSACTIONAL_STATE = (
    "_observations",
    "_observation_keys",
    "_evidence",
    "_candidates",
    "_candidate_by_observation",
    "_records",
    "_record_identity",
    "_revisions",
    "_revision_ids_by_record",
    "_active_revision",
    "_transitions",
    "_traces",
    "_outcomes",
    "_outcome_keys",
    "_utilities",
    "_strategies",
    "_strategy_activations",
    "_experiments",
    "_experiment_transitions",
    "_experiment_transition_ids",
    "_experiment_transition_keys",
)


class _LockedTransaction(AbstractContextManager[None]):
    def __init__(self, store: InMemoryMemoryStore) -> None:
        self._store = store

    def __enter__(self) -> None:
        self._store._lock.acquire()
        try:
            self._store._transaction_snapshots.append(
                self._store._snapshot_transaction_state_unlocked()
            )
        except BaseException:
            self._store._lock.release()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            snapshot = self._store._transaction_snapshots.pop()
            if exc_type is not None:
                self._store._restore_transaction_state_unlocked(snapshot)
        finally:
            self._store._lock.release()


class InMemoryMemoryStore:
    """Thread-safe executable adapter; intentionally not a production persistence layer."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._observations: dict[UUID, Observation] = {}
        self._observation_keys: dict[tuple[Scope, str], UUID] = {}
        self._evidence: dict[UUID, EvidenceSpan] = {}
        self._candidates: dict[UUID, Candidate] = {}
        self._candidate_by_observation: dict[UUID, UUID] = {}
        self._records: dict[UUID, MemoryRecord] = {}
        self._record_identity: dict[tuple[Scope, str, str], UUID] = {}
        self._revisions: dict[UUID, MemoryRevision] = {}
        self._revision_ids_by_record: dict[UUID, list[UUID]] = {}
        self._active_revision: dict[UUID, UUID] = {}
        self._transitions: list[RevisionTransition] = []
        self._traces: dict[UUID, RecallTrace] = {}
        self._outcomes: dict[UUID, OutcomeEvent] = {}
        self._outcome_keys: dict[tuple[Scope, str], UUID] = {}
        self._utilities: dict[tuple[Scope, UUID, str], UtilityEstimate] = {}
        self._strategies: dict[UUID, StrategySnapshot] = {}
        self._strategy_activations: list[StrategyActivation] = []
        self._experiments: dict[UUID, EvolutionExperiment] = {}
        self._experiment_transitions: dict[UUID, list[ExperimentTransition]] = {}
        self._experiment_transition_ids: set[UUID] = set()
        self._experiment_transition_keys: dict[str, ExperimentTransition] = {}
        self._transaction_snapshots: list[dict[str, object]] = []

    def close(self) -> None:
        """The in-process adapter owns no external resources."""

    def is_ready(self) -> bool:
        return True

    def save_strategy(self, strategy: StrategySnapshot) -> None:
        with self._lock:
            self._save_strategy_unlocked(strategy)

    def strategy(self, strategy_id: UUID) -> StrategySnapshot | None:
        with self._lock:
            return self._strategies.get(strategy_id)

    def ensure_active_strategy(
        self,
        strategy: StrategySnapshot,
        activation: StrategyActivation,
    ) -> StrategySnapshot:
        with self._lock:
            active = self._active_strategy_unlocked()
            if active is not None:
                return active
            if (
                activation.kind is not StrategyActivationKind.BOOTSTRAP
                or activation.strategy_id != strategy.id
                or strategy.parent_id is not None
                or strategy.version != 1
            ):
                raise ConflictError("initial active strategy requires a root bootstrap activation")
            self._save_strategy_unlocked(strategy)
            self._strategy_activations.append(activation)
            return strategy

    def active_strategy(self) -> StrategySnapshot | None:
        with self._lock:
            return self._active_strategy_unlocked()

    def strategy_activation_history(self) -> tuple[StrategyActivation, ...]:
        with self._lock:
            return tuple(self._strategy_activations)

    def register_evolution_experiment(
        self,
        candidate: StrategySnapshot,
        experiment: EvolutionExperiment,
        transition: ExperimentTransition,
    ) -> None:
        with self._lock:
            active = self._active_strategy_unlocked()
            if active is None or active.id != experiment.baseline_id:
                raise ConflictError("experiment baseline is not the active strategy")
            if (
                candidate.id != experiment.candidate_id
                or candidate.parent_id != active.id
                or candidate.version != active.version + 1
            ):
                raise ConflictError("experiment candidate does not extend the active baseline")
            if experiment.id in self._experiments:
                raise ConflictError("evolution experiment already exists")
            if (
                experiment.stage is not ExperimentStage.PROPOSED
                or experiment.created_at != experiment.updated_at
                or transition.id in self._experiment_transition_ids
                or transition.idempotency_key in self._experiment_transition_keys
                or transition.experiment_id != experiment.id
                or transition.from_stage is not None
                or transition.to_stage is not ExperimentStage.PROPOSED
                or transition.transitioned_at != experiment.created_at
            ):
                raise ConflictError("experiment creation evidence is inconsistent")
            self._save_strategy_unlocked(candidate)
            self._experiments[experiment.id] = experiment
            self._experiment_transitions[experiment.id] = [transition]
            self._experiment_transition_ids.add(transition.id)
            self._experiment_transition_keys[transition.idempotency_key] = transition

    def evolution_experiment(self, experiment_id: UUID) -> EvolutionExperiment | None:
        with self._lock:
            return self._experiments.get(experiment_id)

    def experiment_transition_history(
        self,
        experiment_id: UUID,
    ) -> tuple[ExperimentTransition, ...]:
        with self._lock:
            return tuple(self._experiment_transitions.get(experiment_id, ()))

    def experiment_transition_by_idempotency(
        self,
        idempotency_key: str,
    ) -> ExperimentTransition | None:
        with self._lock:
            return self._experiment_transition_keys.get(idempotency_key.strip())

    def advance_evolution_experiment(
        self,
        experiment: EvolutionExperiment,
        transition: ExperimentTransition,
        activation: StrategyActivation | None = None,
    ) -> None:
        with self._lock:
            current = self._experiments.get(experiment.id)
            if current is None:
                raise ConflictError("evolution experiment does not exist")
            if (
                experiment.baseline_id != current.baseline_id
                or experiment.candidate_id != current.candidate_id
                or experiment.created_at != current.created_at
                or transition.id in self._experiment_transition_ids
                or transition.idempotency_key in self._experiment_transition_keys
                or transition.experiment_id != current.id
                or transition.from_stage is not current.stage
                or transition.to_stage is not experiment.stage
                or transition.transitioned_at != experiment.updated_at
            ):
                raise ConflictError("experiment transition evidence is inconsistent")
            expected = current.transition(experiment.stage, transition.transitioned_at)
            if experiment != expected:
                raise ConflictError("experiment transition does not match current state")
            self._validate_activation_unlocked(current, experiment, transition, activation)
            if activation is not None:
                self._strategy_activations.append(activation)
            self._experiments[experiment.id] = experiment
            self._experiment_transitions[experiment.id].append(transition)
            self._experiment_transition_ids.add(transition.id)
            self._experiment_transition_keys[transition.idempotency_key] = transition

    def _validate_activation_unlocked(
        self,
        current: EvolutionExperiment,
        updated: EvolutionExperiment,
        transition: ExperimentTransition,
        activation: StrategyActivation | None,
    ) -> None:
        promotion = (
            current.stage is ExperimentStage.CANARY and updated.stage is ExperimentStage.PROMOTED
        )
        rollback = (
            current.stage is ExperimentStage.PROMOTED
            and updated.stage is ExperimentStage.ROLLED_BACK
        )
        if not promotion and not rollback:
            if activation is not None:
                raise ConflictError("this experiment transition cannot activate a strategy")
            return
        if activation is None or activation.id in {item.id for item in self._strategy_activations}:
            raise ConflictError("strategy activation evidence is missing or duplicated")
        expected_kind = (
            StrategyActivationKind.PROMOTION if promotion else StrategyActivationKind.ROLLBACK
        )
        expected_target = current.candidate_id if promotion else current.baseline_id
        expected_previous = current.baseline_id if promotion else current.candidate_id
        active = self._active_strategy_unlocked()
        if (
            activation.kind is not expected_kind
            or activation.strategy_id != expected_target
            or activation.previous_strategy_id != expected_previous
            or activation.experiment_id != current.id
            or activation.activated_at != transition.transitioned_at
            or activation.reason != transition.reason
            or active is None
            or active.id != expected_previous
            or expected_target not in self._strategies
        ):
            raise ConflictError("strategy activation does not match experiment state")

    def _save_strategy_unlocked(self, strategy: StrategySnapshot) -> None:
        existing = self._strategies.get(strategy.id)
        if existing is not None:
            if existing != strategy:
                raise ConflictError("strategy id already belongs to a different snapshot")
            return
        if strategy.parent_id is None:
            if strategy.version != 1:
                raise ConflictError("root strategy version must be 1")
        else:
            parent = self._strategies.get(strategy.parent_id)
            if parent is None or strategy.version != parent.version + 1:
                raise ConflictError("strategy parent and version are inconsistent")
        self._strategies[strategy.id] = strategy

    def _active_strategy_unlocked(self) -> StrategySnapshot | None:
        if not self._strategy_activations:
            return None
        strategy_id = self._strategy_activations[-1].strategy_id
        return self._strategies[strategy_id]

    def transaction(self) -> AbstractContextManager[None]:
        return _LockedTransaction(self)

    def _snapshot_transaction_state_unlocked(self) -> dict[str, object]:
        return {name: deepcopy(getattr(self, name)) for name in _TRANSACTIONAL_STATE}

    def _restore_transaction_state_unlocked(self, snapshot: dict[str, object]) -> None:
        for name in _TRANSACTIONAL_STATE:
            setattr(self, name, snapshot[name])

    def observation_by_idempotency(self, scope: Scope, idempotency_key: str) -> Observation | None:
        with self._lock:
            observation_id = self._observation_keys.get((scope, idempotency_key))
            return self._observations.get(observation_id) if observation_id else None

    def save_ingestion(
        self,
        observation: Observation,
        evidence: EvidenceSpan,
        candidate: Candidate,
    ) -> None:
        with self._lock:
            key = (observation.scope, observation.idempotency_key)
            if key in self._observation_keys:
                raise ConflictError("observation idempotency key already exists")
            if evidence.observation_id != observation.id:
                raise ConflictError("evidence does not belong to observation")
            if candidate.observation_id != observation.id or candidate.scope != observation.scope:
                raise ConflictError("candidate does not belong to observation scope")
            self._observations[observation.id] = observation
            self._observation_keys[key] = observation.id
            self._evidence[evidence.id] = evidence
            self._candidates[candidate.id] = candidate
            self._candidate_by_observation[observation.id] = candidate.id

    def candidate_for_observation(
        self,
        scope: Scope,
        observation_id: UUID,
    ) -> Candidate | None:
        with self._lock:
            candidate_id = self._candidate_by_observation.get(observation_id)
            candidate = self._candidates.get(candidate_id) if candidate_id else None
            return candidate if candidate is not None and candidate.scope == scope else None

    def update_candidate(self, candidate: Candidate) -> None:
        with self._lock:
            if candidate.id not in self._candidates:
                raise ConflictError("candidate does not exist")
            self._candidates[candidate.id] = candidate

    def current_by_identity(
        self,
        scope: Scope,
        key: str,
        context: ContextSignature,
    ) -> MemorySnapshot | None:
        with self._lock:
            record_id = self._record_identity.get((scope, key, context.fingerprint))
            return self._snapshot_unlocked(scope, record_id) if record_id else None

    def snapshot(self, scope: Scope, record_id: UUID) -> MemorySnapshot | None:
        with self._lock:
            return self._snapshot_unlocked(scope, record_id)

    def add_memory(
        self,
        record: MemoryRecord,
        revision: MemoryRevision,
        transition: RevisionTransition,
    ) -> None:
        with self._lock:
            identity = (record.scope, record.key, record.context.fingerprint)
            if identity in self._record_identity:
                raise ConflictError("memory identity already exists")
            if revision.record_id != record.id or transition.record_id != record.id:
                raise ConflictError("initial revision does not belong to record")
            self._records[record.id] = record
            self._record_identity[identity] = record.id
            self._revisions[revision.id] = revision
            self._revision_ids_by_record[record.id] = [revision.id]
            self._active_revision[record.id] = revision.id
            self._transitions.append(transition)

    def append_revision(
        self,
        *,
        expected_revision_id: UUID,
        revision: MemoryRevision,
        transition: RevisionTransition,
    ) -> None:
        with self._lock:
            current_id = self._active_revision.get(revision.record_id)
            if current_id != expected_revision_id:
                raise ConflictError("active revision changed concurrently")
            current = self._revisions[current_id]
            if revision.sequence != current.sequence + 1:
                raise ConflictError("revision sequence is not contiguous")
            if revision.supersedes_revision_id != current_id:
                raise ConflictError("revision does not supersede active revision")
            if (
                transition.record_id != revision.record_id
                or transition.from_revision_id != current_id
                or transition.to_revision_id != revision.id
            ):
                raise ConflictError("revision transition is inconsistent")
            self._revisions[revision.id] = revision
            self._revision_ids_by_record[revision.record_id].append(revision.id)
            self._active_revision[revision.record_id] = revision.id
            self._transitions.append(transition)

    def active_memories(self, scope: Scope) -> tuple[MemorySnapshot, ...]:
        with self._lock:
            snapshots = [
                snapshot
                for record_id in self._active_revision
                if (snapshot := self._snapshot_unlocked(scope, record_id)) is not None
            ]
            return tuple(snapshots)

    def memories_as_of(
        self,
        scope: Scope,
        *,
        valid_at: datetime,
        known_at: datetime,
    ) -> tuple[MemorySnapshot, ...]:
        """Reconstruct each transaction head that was effective on both time axes."""
        with self._lock:
            snapshots: list[MemorySnapshot] = []
            for record in self._records.values():
                if record.scope != scope or record.created_at > known_at:
                    continue
                eligible = (
                    self._revisions[revision_id]
                    for revision_id in self._revision_ids_by_record.get(record.id, ())
                    if self._revisions[revision_id].recorded_at <= known_at
                    and self._revisions[revision_id].valid_from <= valid_at
                )
                revision = max(
                    eligible,
                    key=lambda item: (item.recorded_at, item.sequence, str(item.id)),
                    default=None,
                )
                if revision is not None:
                    snapshots.append(MemorySnapshot(record=record, revision=revision))
            return tuple(
                sorted(
                    snapshots,
                    key=lambda snapshot: (
                        snapshot.record.key,
                        snapshot.record.context.fingerprint,
                        str(snapshot.record.id),
                    ),
                )
            )

    def revision_history(self, scope: Scope, record_id: UUID) -> tuple[MemoryRevision, ...]:
        with self._lock:
            record = self._records.get(record_id)
            if record is None or record.scope != scope:
                return ()
            return tuple(
                self._revisions[revision_id]
                for revision_id in self._revision_ids_by_record.get(record_id, [])
            )

    def save_trace(self, trace: RecallTrace) -> None:
        with self._lock:
            self._traces[trace.id] = trace

    def trace(self, scope: Scope, trace_id: UUID) -> RecallTrace | None:
        with self._lock:
            trace = self._traces.get(trace_id)
            return trace if trace is not None and trace.scope == scope else None

    def utility_for(
        self,
        scope: Scope,
        revision_id: UUID,
        context: ContextSignature,
    ) -> UtilityEstimate:
        with self._lock:
            key = (scope, revision_id, context.fingerprint)
            return self._utilities.get(
                key,
                UtilityEstimate(
                    revision_id=revision_id,
                    context_fingerprint=context.fingerprint,
                ),
            )

    def utility_for_as_of(
        self,
        scope: Scope,
        revision_id: UUID,
        context: ContextSignature,
        *,
        known_at: datetime,
    ) -> UtilityEstimate:
        with self._lock:
            outcomes = tuple(
                outcome
                for outcome in self._outcomes.values()
                if outcome.scope == scope
                and outcome.revision_id == revision_id
                and outcome.recorded_at <= known_at
                and (trace := self._traces.get(outcome.trace_id)) is not None
                and trace.scope == scope
                and trace.context.fingerprint == context.fingerprint
            )
            return UtilityEstimate(
                revision_id=revision_id,
                context_fingerprint=context.fingerprint,
                positive_weight=sum(
                    outcome.kind.success_value * outcome.weight for outcome in outcomes
                ),
                negative_weight=sum(
                    (1.0 - outcome.kind.success_value) * outcome.weight for outcome in outcomes
                ),
                last_outcome_at=max(
                    (outcome.occurred_at for outcome in outcomes),
                    default=None,
                ),
            )

    def apply_outcome(
        self,
        outcome: OutcomeEvent,
        context: ContextSignature,
    ) -> tuple[OutcomeEvent, UtilityEstimate, bool]:
        with self._lock:
            idempotency = (outcome.scope, outcome.idempotency_key)
            existing_id = self._outcome_keys.get(idempotency)
            if existing_id is not None:
                existing = self._outcomes[existing_id]
                # occurred_at may be generated independently for otherwise identical
                # HTTP retries; compare the stable business payload instead.
                if (
                    existing.trace_id != outcome.trace_id
                    or existing.revision_id != outcome.revision_id
                    or existing.kind != outcome.kind
                    or existing.weight != outcome.weight
                    or existing.note != outcome.note
                ):
                    raise ConflictError("outcome idempotency key was reused with different data")
                utility = self.utility_for(outcome.scope, existing.revision_id, context)
                return existing, utility, False

            current = self.utility_for(outcome.scope, outcome.revision_id, context)
            updated = current.updated(
                success=outcome.kind.success_value,
                weight=outcome.weight,
                at=outcome.occurred_at,
            )
            self._outcomes[outcome.id] = outcome
            self._outcome_keys[idempotency] = outcome.id
            self._utilities[(outcome.scope, outcome.revision_id, context.fingerprint)] = updated
            return outcome, updated, True

    @property
    def observation_count(self) -> int:
        with self._lock:
            return len(self._observations)

    @property
    def outcome_count(self) -> int:
        with self._lock:
            return len(self._outcomes)

    @property
    def transition_count(self) -> int:
        with self._lock:
            return len(self._transitions)

    def _snapshot_unlocked(self, scope: Scope, record_id: UUID) -> MemorySnapshot | None:
        record = self._records.get(record_id)
        if record is None or record.scope != scope:
            return None
        revision_id = self._active_revision.get(record_id)
        if revision_id is None:
            return None
        return MemorySnapshot(record=record, revision=self._revisions[revision_id])
