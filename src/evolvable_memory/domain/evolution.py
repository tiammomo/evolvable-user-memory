from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from math import isclose
from uuid import UUID

from evolvable_memory.domain.common import DomainError, require_utc


@dataclass(frozen=True, slots=True)
class RetrievalWeights:
    semantic: float = 0.35
    context: float = 0.25
    belief: float = 0.20
    utility: float = 0.15
    recency: float = 0.05

    def __post_init__(self) -> None:
        values = (self.semantic, self.context, self.belief, self.utility, self.recency)
        if any(value < 0.05 or value > 0.60 for value in values):
            raise DomainError("retrieval weights must each be in [0.05, 0.60]")
        if not isclose(sum(values), 1.0, abs_tol=1e-9):
            raise DomainError("retrieval weights must sum to 1")


@dataclass(frozen=True, slots=True)
class StrategySnapshot:
    id: UUID
    version: int
    weights: RetrievalWeights
    min_score: float
    recency_half_life_days: float
    created_at: datetime
    parent_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise DomainError("strategy version must be positive")
        if not 0.0 <= self.min_score <= 1.0:
            raise DomainError("min_score must be between 0 and 1")
        if not 1.0 <= self.recency_half_life_days <= 3650.0:
            raise DomainError("recency half-life must be in [1, 3650] days")
        object.__setattr__(self, "created_at", require_utc(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class FailureDiagnosis:
    irrelevant_results: int = 0
    context_mismatches: int = 0
    low_belief_results: int = 0
    harmful_results: int = 0
    stale_results: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.irrelevant_results,
                self.context_mismatches,
                self.low_belief_results,
                self.harmful_results,
                self.stale_results,
            )
            < 0
        ):
            raise DomainError("failure counts must be non-negative")

    def dominant_component(self) -> str | None:
        counts = {
            "semantic": self.irrelevant_results,
            "context": self.context_mismatches,
            "belief": self.low_belief_results,
            "utility": self.harmful_results,
            "recency": self.stale_results,
        }
        component, count = max(counts.items(), key=lambda item: item[1])
        return component if count > 0 else None


class ExperimentStage(StrEnum):
    PROPOSED = "proposed"
    OFFLINE_PASSED = "offline_passed"
    SHADOW = "shadow"
    CANARY = "canary"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class EvolutionExperiment:
    id: UUID
    baseline_id: UUID
    candidate_id: UUID
    stage: ExperimentStage
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", require_utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", require_utc(self.updated_at, "updated_at"))

    def transition(self, target: ExperimentStage, at: datetime) -> EvolutionExperiment:
        allowed = {
            ExperimentStage.PROPOSED: {
                ExperimentStage.OFFLINE_PASSED,
                ExperimentStage.REJECTED,
            },
            ExperimentStage.OFFLINE_PASSED: {
                ExperimentStage.SHADOW,
                ExperimentStage.REJECTED,
            },
            ExperimentStage.SHADOW: {
                ExperimentStage.CANARY,
                ExperimentStage.ROLLED_BACK,
            },
            ExperimentStage.CANARY: {
                ExperimentStage.PROMOTED,
                ExperimentStage.ROLLED_BACK,
            },
        }
        if target not in allowed.get(self.stage, set()):
            raise DomainError(f"illegal experiment transition: {self.stage} -> {target}")
        return replace(self, stage=target, updated_at=at)


class PolicyEvolution:
    """Creates a bounded proposal; promotion remains an external gated decision."""

    _MAX_DELTA = 0.03

    def propose(
        self,
        *,
        parent: StrategySnapshot,
        diagnosis: FailureDiagnosis,
        proposal_id: UUID,
        at: datetime,
    ) -> StrategySnapshot | None:
        target = diagnosis.dominant_component()
        if target is None:
            return None

        values = {
            "semantic": parent.weights.semantic,
            "context": parent.weights.context,
            "belief": parent.weights.belief,
            "utility": parent.weights.utility,
            "recency": parent.weights.recency,
        }
        donors = sorted(
            (name for name in values if name != target),
            key=lambda name: values[name],
            reverse=True,
        )
        donor = donors[0]
        delta = min(
            self._MAX_DELTA,
            0.60 - values[target],
            values[donor] - 0.05,
        )
        if delta <= 0.0:
            return None
        values[target] += delta
        values[donor] -= delta
        weights = RetrievalWeights(**values)
        return StrategySnapshot(
            id=proposal_id,
            version=parent.version + 1,
            weights=weights,
            min_score=parent.min_score,
            recency_half_life_days=parent.recency_half_life_days,
            created_at=at,
            parent_id=parent.id,
        )
