from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from evolvable_memory.domain.common import DomainError
from evolvable_memory.domain.evolution import (
    EvolutionExperiment,
    ExperimentStage,
    FailureDiagnosis,
    PolicyEvolution,
    RetrievalWeights,
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
    assert shadow.transition(ExperimentStage.ROLLED_BACK, NOW).stage is ExperimentStage.ROLLED_BACK
    with pytest.raises(DomainError, match="illegal experiment transition"):
        experiment.transition(ExperimentStage.PROMOTED, NOW)
