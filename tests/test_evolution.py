from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from conftest import FixedClock, SequentialIds
from evolvable_memory.adapters.in_memory import InMemoryMemoryStore
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.domain.common import ConflictError, DomainError
from evolvable_memory.domain.evolution import (
    EvolutionExperiment,
    ExperimentStage,
    ExperimentTransition,
    FailureDiagnosis,
    PolicyEvolution,
    RetrievalWeights,
    StrategyActivation,
    StrategyActivationKind,
    StrategySnapshot,
)

NOW = datetime(2026, 7, 14, tzinfo=UTC)


def test_policy_evolution_proposes_bounded_immutable_child() -> None:
    parent = StrategySnapshot(
        id=UUID(int=1),
        version=1,
        weights=RetrievalWeights(),
        min_score=0.2,
        recency_half_life_days=180,
        created_at=NOW,
    )
    child = PolicyEvolution().propose(
        parent=parent,
        diagnosis=FailureDiagnosis(context_mismatches=12, irrelevant_results=3),
        proposal_id=UUID(int=2),
        at=NOW,
    )

    assert child is not None
    assert child.parent_id == parent.id
    assert child.version == 2
    assert child.weights.context == pytest.approx(parent.weights.context + 0.03)
    assert child.weights.semantic == pytest.approx(parent.weights.semantic - 0.03)
    assert parent.weights == RetrievalWeights()


def test_policy_evolution_does_nothing_without_failure_evidence() -> None:
    parent = StrategySnapshot(
        id=UUID(int=1),
        version=1,
        weights=RetrievalWeights(),
        min_score=0.2,
        recency_half_life_days=180,
        created_at=NOW,
    )
    assert (
        PolicyEvolution().propose(
            parent=parent,
            diagnosis=FailureDiagnosis(),
            proposal_id=UUID(int=2),
            at=NOW,
        )
        is None
    )


def test_experiment_enforces_progressive_gates_and_rollback() -> None:
    experiment = EvolutionExperiment(
        id=UUID(int=3),
        baseline_id=UUID(int=1),
        candidate_id=UUID(int=2),
        stage=ExperimentStage.PROPOSED,
        created_at=NOW,
        updated_at=NOW,
    )

    offline = experiment.transition(ExperimentStage.OFFLINE_PASSED, NOW)
    shadow = offline.transition(ExperimentStage.SHADOW, NOW)
    canary = shadow.transition(ExperimentStage.CANARY, NOW)
    promoted = canary.transition(ExperimentStage.PROMOTED, NOW)

    assert promoted.stage is ExperimentStage.PROMOTED
    assert (
        promoted.transition(ExperimentStage.ROLLED_BACK, NOW).stage is ExperimentStage.ROLLED_BACK
    )
    assert shadow.transition(ExperimentStage.ROLLED_BACK, NOW).stage is ExperimentStage.ROLLED_BACK
    with pytest.raises(DomainError, match="illegal experiment transition"):
        experiment.transition(ExperimentStage.PROMOTED, NOW)


def test_strategy_activation_requires_auditable_gate_evidence() -> None:
    bootstrap = StrategyActivation(
        id=UUID(int=10),
        strategy_id=UUID(int=1),
        kind=StrategyActivationKind.BOOTSTRAP,
        activated_at=NOW,
        reason="initial default",
    )
    assert bootstrap.previous_strategy_id is None

    with pytest.raises(DomainError, match="prior strategy and experiment"):
        StrategyActivation(
            id=UUID(int=11),
            strategy_id=UUID(int=2),
            kind=StrategyActivationKind.PROMOTION,
            activated_at=NOW,
            reason="candidate passed",
        )

    with pytest.raises(DomainError, match="bootstrap activation"):
        StrategyActivation(
            id=UUID(int=13),
            strategy_id=UUID(int=2),
            previous_strategy_id=UUID(int=1),
            kind=StrategyActivationKind.BOOTSTRAP,
            activated_at=NOW,
            reason="invalid bootstrap",
        )

    with pytest.raises(DomainError, match="must change"):
        StrategyActivation(
            id=UUID(int=12),
            strategy_id=UUID(int=1),
            previous_strategy_id=UUID(int=1),
            experiment_id=UUID(int=20),
            kind=StrategyActivationKind.ROLLBACK,
            activated_at=NOW,
            reason="rollback",
        )


def test_registered_candidate_does_not_replace_the_active_strategy() -> None:
    store = InMemoryMemoryStore()
    clock = FixedClock()
    active_application = MemoryApplication(store=store, clock=clock, ids=SequentialIds())
    active = active_application.retrieval_policy
    candidate = PolicyEvolution().propose(
        parent=active,
        diagnosis=FailureDiagnosis(context_mismatches=5),
        proposal_id=UUID(int=100),
        at=NOW,
    )
    assert candidate is not None

    pinned_candidate_application = MemoryApplication(
        store=store,
        clock=clock,
        ids=SequentialIds(),
        retrieval_policy=candidate,
    )

    assert pinned_candidate_application.retrieval_policy == candidate
    assert active_application.retrieval_policy == active
    assert store.active_strategy() == active
    assert store.strategy_activation_history() == (
        StrategyActivation(
            id=UUID(int=2),
            strategy_id=active.id,
            kind=StrategyActivationKind.BOOTSTRAP,
            activated_at=clock.now(),
            reason="initial default strategy",
        ),
    )


def test_reopening_an_application_reuses_the_authoritative_active_strategy() -> None:
    store = InMemoryMemoryStore()
    clock = FixedClock()
    first = MemoryApplication(store=store, clock=clock, ids=SequentialIds())
    active = first.retrieval_policy

    reopened = MemoryApplication(store=store, clock=clock, ids=SequentialIds())

    assert reopened.retrieval_policy == active
    assert len(store.strategy_activation_history()) == 1


def test_strategy_registry_rejects_invalid_bootstrap_and_lineage() -> None:
    clock = FixedClock()
    root = StrategySnapshot(
        id=UUID(int=200),
        version=1,
        weights=RetrievalWeights(),
        min_score=0.2,
        recency_half_life_days=180,
        created_at=clock.now(),
    )
    promotion = StrategyActivation(
        id=UUID(int=201),
        strategy_id=root.id,
        previous_strategy_id=UUID(int=199),
        experiment_id=UUID(int=300),
        kind=StrategyActivationKind.PROMOTION,
        activated_at=clock.now(),
        reason="not a bootstrap",
    )
    empty_store = InMemoryMemoryStore()
    with pytest.raises(ConflictError, match="root bootstrap"):
        empty_store.ensure_active_strategy(root, promotion)

    with pytest.raises(ConflictError, match="root strategy version"):
        empty_store.save_strategy(replace(root, id=UUID(int=202), version=2))

    empty_store.save_strategy(root)
    empty_store.save_strategy(root)
    with pytest.raises(ConflictError, match="different snapshot"):
        empty_store.save_strategy(replace(root, min_score=0.3))
    with pytest.raises(ConflictError, match="parent and version"):
        empty_store.save_strategy(
            replace(
                root,
                id=UUID(int=203),
                version=2,
                parent_id=UUID(int=999),
            )
        )


def test_experiment_and_transition_evidence_reject_invalid_identity_time_and_digest() -> None:
    with pytest.raises(DomainError, match="differ from baseline"):
        EvolutionExperiment(
            id=UUID(int=400),
            baseline_id=UUID(int=1),
            candidate_id=UUID(int=1),
            stage=ExperimentStage.PROPOSED,
            created_at=NOW,
            updated_at=NOW,
        )
    with pytest.raises(DomainError, match="must not precede"):
        EvolutionExperiment(
            id=UUID(int=401),
            baseline_id=UUID(int=1),
            candidate_id=UUID(int=2),
            stage=ExperimentStage.PROPOSED,
            created_at=NOW,
            updated_at=NOW.replace(year=2025),
        )
    with pytest.raises(DomainError, match="SHA-256"):
        ExperimentTransition(
            id=UUID(int=402),
            experiment_id=UUID(int=401),
            to_stage=ExperimentStage.PROPOSED,
            transitioned_at=NOW,
            reason="proposal",
            evidence_ref="artifact://proposal",
            idempotency_key="proposal:invalid-digest",
            request_fingerprint="not-a-digest",
        )
    with pytest.raises(DomainError, match="creation must transition"):
        ExperimentTransition(
            id=UUID(int=403),
            experiment_id=UUID(int=401),
            to_stage=ExperimentStage.SHADOW,
            transitioned_at=NOW,
            reason="invalid creation",
            evidence_ref="artifact://shadow",
            idempotency_key="transition:invalid-creation",
            request_fingerprint="0" * 64,
        )
    with pytest.raises(DomainError, match="illegal experiment transition evidence"):
        ExperimentTransition(
            id=UUID(int=404),
            experiment_id=UUID(int=401),
            from_stage=ExperimentStage.PROPOSED,
            to_stage=ExperimentStage.CANARY,
            transitioned_at=NOW,
            reason="skip gates",
            evidence_ref="artifact://canary",
            idempotency_key="transition:skip-gates",
            request_fingerprint="a" * 64,
        )
