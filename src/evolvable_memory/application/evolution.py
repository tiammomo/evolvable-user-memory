from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from evolvable_memory.application.gate_receipts import gate_receipt_claims
from evolvable_memory.application.ports import (
    Clock,
    GateReceiptVerifierPort,
    IdGenerator,
    StrategyRegistryPort,
)
from evolvable_memory.domain.common import (
    ConflictError,
    DomainError,
    NotFoundError,
    require_text,
)
from evolvable_memory.domain.evolution import (
    EvolutionExperiment,
    ExperimentStage,
    ExperimentTransition,
    FailureDiagnosis,
    GateDecision,
    GateReceipt,
    PolicyEvolution,
    StrategyActivation,
    StrategyActivationKind,
    StrategySnapshot,
)


@dataclass(frozen=True, slots=True)
class EvolutionProposal:
    candidate: StrategySnapshot
    experiment: EvolutionExperiment
    idempotent_replay: bool


@dataclass(frozen=True, slots=True)
class EvolutionTransitionResult:
    experiment: EvolutionExperiment
    idempotent_replay: bool


class EvolutionApplication:
    """Trusted orchestration core; HTTP exposure requires a separate authorization boundary."""

    def __init__(
        self,
        *,
        store: StrategyRegistryPort,
        clock: Clock,
        ids: IdGenerator,
        gate_verifier: GateReceiptVerifierPort,
        evolution: PolicyEvolution | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._gate_verifier = gate_verifier
        self._evolution = evolution or PolicyEvolution()

    def propose(
        self,
        diagnosis: FailureDiagnosis,
        *,
        reason: str,
        evidence_ref: str,
        idempotency_key: str,
    ) -> EvolutionProposal | None:
        normalized_reason = require_text(reason, "proposal reason")
        normalized_evidence = require_text(evidence_ref, "proposal evidence_ref")
        normalized_key = require_text(idempotency_key, "proposal idempotency_key")
        request_fingerprint = _proposal_fingerprint(
            diagnosis,
            reason=normalized_reason,
            evidence_ref=normalized_evidence,
        )
        existing = self._store.experiment_transition_by_idempotency(normalized_key)
        if existing is not None:
            return self._replayed_proposal(existing, request_fingerprint)
        baseline = self._store.active_strategy()
        if baseline is None:
            raise DomainError("active strategy is unavailable")
        now = self._clock.now()
        candidate = self._evolution.propose(
            parent=baseline,
            diagnosis=diagnosis,
            proposal_id=self._ids.new(),
            at=now,
        )
        if candidate is None:
            return None
        experiment = EvolutionExperiment(
            id=self._ids.new(),
            baseline_id=baseline.id,
            candidate_id=candidate.id,
            stage=ExperimentStage.PROPOSED,
            created_at=now,
            updated_at=now,
        )
        transition = ExperimentTransition(
            id=self._ids.new(),
            experiment_id=experiment.id,
            from_stage=None,
            to_stage=ExperimentStage.PROPOSED,
            transitioned_at=now,
            reason=normalized_reason,
            evidence_ref=normalized_evidence,
            idempotency_key=normalized_key,
            request_fingerprint=request_fingerprint,
        )
        try:
            self._store.register_evolution_experiment(candidate, experiment, transition)
        except ConflictError:
            concurrent = self._store.experiment_transition_by_idempotency(normalized_key)
            if concurrent is not None:
                return self._replayed_proposal(concurrent, request_fingerprint)
            raise
        return EvolutionProposal(
            candidate=candidate,
            experiment=experiment,
            idempotent_replay=False,
        )

    def advance(
        self,
        experiment_id: UUID,
        target: ExperimentStage,
        *,
        receipt: GateReceipt,
        idempotency_key: str,
    ) -> EvolutionTransitionResult:
        normalized_key = require_text(idempotency_key, "transition idempotency_key")
        request_fingerprint = _advance_fingerprint(
            experiment_id,
            target,
            receipt=receipt,
        )
        existing = self._store.experiment_transition_by_idempotency(normalized_key)
        if existing is not None:
            return self._replayed_advance(existing, request_fingerprint)
        current = self._store.evolution_experiment(experiment_id)
        if current is None:
            raise NotFoundError("evolution experiment not found")
        now = self._clock.now()
        self._gate_verifier.verify(receipt, at=now)
        _require_matching_gate_receipt(current, target, receipt)
        updated = current.transition(target, now)
        transition = ExperimentTransition(
            id=self._ids.new(),
            experiment_id=current.id,
            from_stage=current.stage,
            to_stage=target,
            transitioned_at=now,
            reason=receipt.reason,
            evidence_ref=receipt.artifact_ref,
            idempotency_key=normalized_key,
            request_fingerprint=request_fingerprint,
        )
        activation = self._activation_for(current, updated, reason=receipt.reason, at=now)
        try:
            self._store.advance_evolution_experiment(updated, transition, activation)
        except ConflictError:
            concurrent = self._store.experiment_transition_by_idempotency(normalized_key)
            if concurrent is not None:
                return self._replayed_advance(concurrent, request_fingerprint)
            raise
        return EvolutionTransitionResult(experiment=updated, idempotent_replay=False)

    def _replayed_proposal(
        self,
        transition: ExperimentTransition,
        request_fingerprint: str,
    ) -> EvolutionProposal:
        if (
            transition.from_stage is not None
            or transition.to_stage is not ExperimentStage.PROPOSED
            or transition.request_fingerprint != request_fingerprint
        ):
            raise ConflictError("proposal idempotency key was reused with different data")
        current = self._store.evolution_experiment(transition.experiment_id)
        if current is None:
            raise ConflictError("proposal idempotency key belongs to incomplete state")
        candidate = self._store.strategy(current.candidate_id)
        if candidate is None:
            raise ConflictError("proposal candidate is unavailable")
        experiment = EvolutionExperiment(
            id=current.id,
            baseline_id=current.baseline_id,
            candidate_id=current.candidate_id,
            stage=ExperimentStage.PROPOSED,
            created_at=current.created_at,
            updated_at=transition.transitioned_at,
        )
        return EvolutionProposal(
            candidate=candidate,
            experiment=experiment,
            idempotent_replay=True,
        )

    def _replayed_advance(
        self,
        transition: ExperimentTransition,
        request_fingerprint: str,
    ) -> EvolutionTransitionResult:
        if transition.request_fingerprint != request_fingerprint:
            raise ConflictError("transition idempotency key was reused with different data")
        current = self._store.evolution_experiment(transition.experiment_id)
        if current is None:
            raise ConflictError("transition idempotency key belongs to incomplete state")
        historical = EvolutionExperiment(
            id=current.id,
            baseline_id=current.baseline_id,
            candidate_id=current.candidate_id,
            stage=transition.to_stage,
            created_at=current.created_at,
            updated_at=transition.transitioned_at,
        )
        return EvolutionTransitionResult(experiment=historical, idempotent_replay=True)

    def _activation_for(
        self,
        current: EvolutionExperiment,
        updated: EvolutionExperiment,
        *,
        reason: str,
        at: datetime,
    ) -> StrategyActivation | None:
        if current.stage is ExperimentStage.CANARY and updated.stage is ExperimentStage.PROMOTED:
            return StrategyActivation(
                id=self._ids.new(),
                strategy_id=current.candidate_id,
                previous_strategy_id=current.baseline_id,
                experiment_id=current.id,
                kind=StrategyActivationKind.PROMOTION,
                activated_at=at,
                reason=reason,
            )
        if (
            current.stage is ExperimentStage.PROMOTED
            and updated.stage is ExperimentStage.ROLLED_BACK
        ):
            return StrategyActivation(
                id=self._ids.new(),
                strategy_id=current.baseline_id,
                previous_strategy_id=current.candidate_id,
                experiment_id=current.id,
                kind=StrategyActivationKind.ROLLBACK,
                activated_at=at,
                reason=reason,
            )
        return None


def _proposal_fingerprint(
    diagnosis: FailureDiagnosis,
    *,
    reason: str,
    evidence_ref: str,
) -> str:
    return _fingerprint(
        {
            "operation": "proposal",
            "diagnosis": {
                "irrelevant_results": diagnosis.irrelevant_results,
                "context_mismatches": diagnosis.context_mismatches,
                "low_belief_results": diagnosis.low_belief_results,
                "harmful_results": diagnosis.harmful_results,
                "stale_results": diagnosis.stale_results,
            },
            "reason": reason,
            "evidence_ref": evidence_ref,
        }
    )


def _advance_fingerprint(
    experiment_id: UUID,
    target: ExperimentStage,
    *,
    receipt: GateReceipt,
) -> str:
    return _fingerprint(
        {
            "operation": "advance",
            "experiment_id": str(experiment_id),
            "target": target.value,
            "gate_receipt": {
                **gate_receipt_claims(receipt),
                "signature": receipt.signature,
            },
        }
    )


def _require_matching_gate_receipt(
    experiment: EvolutionExperiment,
    target: ExperimentStage,
    receipt: GateReceipt,
) -> None:
    if (
        receipt.experiment_id != experiment.id
        or receipt.baseline_id != experiment.baseline_id
        or receipt.candidate_id != experiment.candidate_id
    ):
        raise DomainError("gate receipt does not match the evolution experiment")
    if receipt.from_stage is not experiment.stage or receipt.to_stage is not target:
        raise DomainError("gate receipt does not match the requested experiment transition")

    if target is ExperimentStage.REJECTED:
        expected_decision = GateDecision.REJECT
    elif target is ExperimentStage.ROLLED_BACK:
        expected_decision = GateDecision.ROLLBACK
    else:
        expected_decision = GateDecision.PASS
    if receipt.decision is not expected_decision:
        raise DomainError("gate receipt decision does not authorize the target stage")
    if expected_decision is GateDecision.PASS and not receipt.hard_gates_passed:
        raise DomainError("passing gate receipt must attest that hard gates passed")


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
