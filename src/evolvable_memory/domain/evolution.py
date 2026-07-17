from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from math import isclose
from uuid import UUID

from evolvable_memory.domain.common import DomainError, require_text, require_utc


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


class StrategyActivationKind(StrEnum):
    BOOTSTRAP = "bootstrap"
    PROMOTION = "promotion"
    ROLLBACK = "rollback"


@dataclass(frozen=True, slots=True)
class StrategyActivation:
    """Append-only evidence that a registered strategy became authoritative."""

    id: UUID
    strategy_id: UUID
    kind: StrategyActivationKind
    activated_at: datetime
    reason: str
    previous_strategy_id: UUID | None = None
    experiment_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, StrategyActivationKind):
            raise DomainError("strategy activation kind is invalid")
        object.__setattr__(
            self,
            "activated_at",
            require_utc(self.activated_at, "activated_at"),
        )
        object.__setattr__(self, "reason", require_text(self.reason, "activation reason"))
        if self.previous_strategy_id == self.strategy_id:
            raise DomainError("strategy activation must change the active strategy")
        if self.kind is StrategyActivationKind.BOOTSTRAP:
            if self.previous_strategy_id is not None or self.experiment_id is not None:
                raise DomainError(
                    "bootstrap activation cannot reference prior strategy or experiment"
                )
        elif self.previous_strategy_id is None or self.experiment_id is None:
            raise DomainError("promotion and rollback require prior strategy and experiment")


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


class GateDecision(StrEnum):
    PASS = "pass"
    REJECT = "reject"
    ROLLBACK = "rollback"


_EXPERIMENT_TRANSITIONS: dict[ExperimentStage, frozenset[ExperimentStage]] = {
    ExperimentStage.PROPOSED: frozenset({ExperimentStage.OFFLINE_PASSED, ExperimentStage.REJECTED}),
    ExperimentStage.OFFLINE_PASSED: frozenset({ExperimentStage.SHADOW, ExperimentStage.REJECTED}),
    ExperimentStage.SHADOW: frozenset({ExperimentStage.CANARY, ExperimentStage.ROLLED_BACK}),
    ExperimentStage.CANARY: frozenset({ExperimentStage.PROMOTED, ExperimentStage.ROLLED_BACK}),
    ExperimentStage.PROMOTED: frozenset({ExperimentStage.ROLLED_BACK}),
}


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
class GateReceipt:
    """Signed, short-lived authority to traverse one experiment stage edge."""

    id: UUID
    experiment_id: UUID
    baseline_id: UUID
    candidate_id: UUID
    from_stage: ExperimentStage
    to_stage: ExperimentStage
    decision: GateDecision
    artifact_ref: str
    artifact_sha256: str
    issuer: str
    key_id: str
    issued_at: datetime
    expires_at: datetime
    hard_gates_passed: bool
    reason: str
    signature: str

    def __post_init__(self) -> None:
        if self.baseline_id == self.candidate_id:
            raise DomainError("gate receipt candidate must differ from baseline")
        if not isinstance(self.from_stage, ExperimentStage) or not isinstance(
            self.to_stage, ExperimentStage
        ):
            raise DomainError("gate receipt stage is invalid")
        if self.to_stage not in _EXPERIMENT_TRANSITIONS.get(self.from_stage, frozenset()):
            raise DomainError("gate receipt must authorize a legal experiment transition")
        if not isinstance(self.decision, GateDecision):
            raise DomainError("gate receipt decision is invalid")
        if not isinstance(self.hard_gates_passed, bool):
            raise DomainError("gate receipt hard_gates_passed must be boolean")
        object.__setattr__(
            self,
            "artifact_ref",
            require_text(self.artifact_ref, "gate receipt artifact_ref"),
        )
        object.__setattr__(
            self,
            "artifact_sha256",
            _require_sha256(self.artifact_sha256, "gate receipt artifact_sha256"),
        )
        object.__setattr__(self, "issuer", require_text(self.issuer, "gate receipt issuer"))
        object.__setattr__(self, "key_id", require_text(self.key_id, "gate receipt key_id"))
        object.__setattr__(
            self,
            "issued_at",
            require_utc(self.issued_at, "gate receipt issued_at"),
        )
        object.__setattr__(
            self,
            "expires_at",
            require_utc(self.expires_at, "gate receipt expires_at"),
        )
        if self.expires_at <= self.issued_at:
            raise DomainError("gate receipt expires_at must follow issued_at")
        object.__setattr__(self, "reason", require_text(self.reason, "gate receipt reason"))
        object.__setattr__(
            self,
            "signature",
            _require_sha256(self.signature, "gate receipt signature"),
        )


@dataclass(frozen=True, slots=True)
class EvolutionExperiment:
    id: UUID
    baseline_id: UUID
    candidate_id: UUID
    stage: ExperimentStage
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.baseline_id == self.candidate_id:
            raise DomainError("experiment candidate must differ from baseline")
        if not isinstance(self.stage, ExperimentStage):
            raise DomainError("experiment stage is invalid")
        object.__setattr__(self, "created_at", require_utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", require_utc(self.updated_at, "updated_at"))
        if self.updated_at < self.created_at:
            raise DomainError("experiment updated_at must not precede created_at")

    def transition(self, target: ExperimentStage, at: datetime) -> EvolutionExperiment:
        transitioned_at = require_utc(at, "experiment transition time")
        if transitioned_at < self.updated_at:
            raise DomainError("experiment transition time must be non-decreasing")
        if target not in _EXPERIMENT_TRANSITIONS.get(self.stage, frozenset()):
            raise DomainError(f"illegal experiment transition: {self.stage} -> {target}")
        return replace(self, stage=target, updated_at=transitioned_at)


@dataclass(frozen=True, slots=True)
class ExperimentTransition:
    """Append-only evidence for creating or advancing one experiment."""

    id: UUID
    experiment_id: UUID
    to_stage: ExperimentStage
    transitioned_at: datetime
    reason: str
    evidence_ref: str
    idempotency_key: str
    request_fingerprint: str
    from_stage: ExperimentStage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.to_stage, ExperimentStage) or (
            self.from_stage is not None and not isinstance(self.from_stage, ExperimentStage)
        ):
            raise DomainError("experiment transition stage is invalid")
        object.__setattr__(
            self,
            "transitioned_at",
            require_utc(self.transitioned_at, "transitioned_at"),
        )
        object.__setattr__(self, "reason", require_text(self.reason, "transition reason"))
        object.__setattr__(
            self,
            "evidence_ref",
            require_text(self.evidence_ref, "transition evidence_ref"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            require_text(self.idempotency_key, "transition idempotency_key"),
        )
        object.__setattr__(
            self,
            "request_fingerprint",
            _require_sha256(
                self.request_fingerprint,
                "transition request_fingerprint",
            ),
        )
        if self.from_stage is None:
            if self.to_stage is not ExperimentStage.PROPOSED:
                raise DomainError("experiment creation must transition to proposed")
        elif self.to_stage not in _EXPERIMENT_TRANSITIONS.get(self.from_stage, frozenset()):
            raise DomainError(
                f"illegal experiment transition evidence: {self.from_stage} -> {self.to_stage}"
            )


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
