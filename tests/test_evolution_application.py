from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from conftest import FixedClock, SequentialIds
from evolvable_memory.adapters.gate_receipts import (
    HmacGateReceiptSigner,
    HmacGateReceiptVerifier,
)
from evolvable_memory.adapters.in_memory import InMemoryMemoryStore
from evolvable_memory.application.evolution import (
    EvolutionApplication,
    EvolutionProposal,
    EvolutionTransitionResult,
)
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.domain.common import ConflictError, DomainError, NotFoundError
from evolvable_memory.domain.evolution import (
    EvolutionExperiment,
    ExperimentStage,
    FailureDiagnosis,
    GateDecision,
    GateReceipt,
    StrategyActivationKind,
)

_GATE_ISSUER = "test-evaluator"
_GATE_KEY_ID = "test-key-v1"
_GATE_SECRET = b"test-only-gate-receipt-secret-32-bytes-minimum"
_GATE_SIGNER = HmacGateReceiptSigner(
    issuer=_GATE_ISSUER,
    key_id=_GATE_KEY_ID,
    secret=_GATE_SECRET,
)


def _applications() -> tuple[
    InMemoryMemoryStore,
    FixedClock,
    MemoryApplication,
    EvolutionApplication,
]:
    store = InMemoryMemoryStore()
    clock = FixedClock()
    ids = SequentialIds()
    memory = MemoryApplication(store=store, clock=clock, ids=ids)
    evolution = EvolutionApplication(
        store=store,
        clock=clock,
        ids=ids,
        gate_verifier=HmacGateReceiptVerifier({(_GATE_ISSUER, _GATE_KEY_ID): _GATE_SECRET}),
    )
    return store, clock, memory, evolution


def _propose(evolution: EvolutionApplication, *, evidence: str) -> EvolutionProposal:
    proposal = evolution.propose(
        FailureDiagnosis(context_mismatches=5),
        reason="context mismatch diagnosis",
        evidence_ref=evidence,
        idempotency_key=f"proposal:{evidence}",
    )
    assert proposal is not None
    return proposal


def _advance_to_canary(
    evolution: EvolutionApplication,
    store: InMemoryMemoryStore,
    clock: FixedClock,
    experiment_id: UUID,
) -> None:
    for stage in (
        ExperimentStage.OFFLINE_PASSED,
        ExperimentStage.SHADOW,
        ExperimentStage.CANARY,
    ):
        clock.advance(seconds=1)
        _advance(
            evolution,
            store,
            clock,
            experiment_id,
            stage,
            reason=f"passed {stage.value}",
            evidence_ref=f"artifact://{experiment_id}/{stage.value}",
            idempotency_key=f"advance:{experiment_id}:{stage.value}",
        )


def _receipt(
    experiment: EvolutionExperiment,
    clock: FixedClock,
    target: ExperimentStage,
    *,
    reason: str,
    evidence_ref: str,
    decision: GateDecision | None = None,
    hard_gates_passed: bool | None = None,
) -> GateReceipt:
    resolved_decision = decision
    if resolved_decision is None:
        if target is ExperimentStage.REJECTED:
            resolved_decision = GateDecision.REJECT
        elif target is ExperimentStage.ROLLED_BACK:
            resolved_decision = GateDecision.ROLLBACK
        else:
            resolved_decision = GateDecision.PASS
    resolved_hard_gates = (
        resolved_decision is GateDecision.PASS if hard_gates_passed is None else hard_gates_passed
    )
    identity = "|".join(
        (
            str(experiment.id),
            experiment.stage.value,
            target.value,
            reason,
            evidence_ref,
            clock.now().isoformat(),
        )
    )
    return _GATE_SIGNER.issue(
        receipt_id=uuid5(NAMESPACE_URL, identity),
        experiment=experiment,
        target=target,
        decision=resolved_decision,
        artifact_ref=evidence_ref,
        artifact_sha256=sha256(evidence_ref.encode()).hexdigest(),
        issued_at=clock.now(),
        expires_at=clock.now() + timedelta(minutes=5),
        hard_gates_passed=resolved_hard_gates,
        reason=reason,
    )


def _advance(
    evolution: EvolutionApplication,
    store: InMemoryMemoryStore,
    clock: FixedClock,
    experiment_id: UUID,
    target: ExperimentStage,
    *,
    reason: str,
    evidence_ref: str,
    idempotency_key: str,
) -> EvolutionTransitionResult:
    current = store.evolution_experiment(experiment_id)
    assert current is not None
    existing = store.experiment_transition_by_idempotency(idempotency_key.strip())
    receipt_experiment = current
    if (
        existing is not None
        and existing.experiment_id == experiment_id
        and existing.to_stage is target
        and existing.from_stage is not None
    ):
        receipt_experiment = replace(current, stage=existing.from_stage)
    return evolution.advance(
        experiment_id,
        target,
        receipt=_receipt(
            receipt_experiment,
            clock,
            target,
            reason=reason,
            evidence_ref=evidence_ref,
        ),
        idempotency_key=idempotency_key,
    )


def test_gated_experiment_promotes_and_rolls_back_atomically() -> None:
    store, clock, memory, evolution = _applications()
    baseline = memory.retrieval_policy
    proposal = _propose(evolution, evidence="artifact://diagnosis/1")

    assert store.active_strategy() == baseline
    assert proposal.candidate.parent_id == baseline.id
    assert store.evolution_experiment(proposal.experiment.id) == proposal.experiment
    assert [
        item.to_stage for item in store.experiment_transition_history(proposal.experiment.id)
    ] == [ExperimentStage.PROPOSED]

    _advance_to_canary(evolution, store, clock, proposal.experiment.id)
    clock.advance(seconds=1)
    promoted = _advance(
        evolution,
        store,
        clock,
        proposal.experiment.id,
        ExperimentStage.PROMOTED,
        reason="approved candidate promotion",
        evidence_ref="approval://strategy-operator/promotion-1",
        idempotency_key=f"advance:{proposal.experiment.id}:promoted",
    )

    assert promoted.experiment.stage is ExperimentStage.PROMOTED
    assert promoted.idempotent_replay is False
    assert store.active_strategy() == proposal.candidate
    assert memory.retrieval_policy == proposal.candidate
    assert [item.kind for item in store.strategy_activation_history()] == [
        StrategyActivationKind.BOOTSTRAP,
        StrategyActivationKind.PROMOTION,
    ]

    clock.advance(seconds=1)
    rolled_back = _advance(
        evolution,
        store,
        clock,
        proposal.experiment.id,
        ExperimentStage.ROLLED_BACK,
        reason="canary regression rollback",
        evidence_ref="alert://harmful-rate/rollback-1",
        idempotency_key=f"advance:{proposal.experiment.id}:rolled-back",
    )

    assert rolled_back.experiment.stage is ExperimentStage.ROLLED_BACK
    assert store.active_strategy() == baseline
    assert memory.retrieval_policy == baseline
    assert [item.kind for item in store.strategy_activation_history()] == [
        StrategyActivationKind.BOOTSTRAP,
        StrategyActivationKind.PROMOTION,
        StrategyActivationKind.ROLLBACK,
    ]
    assert [
        item.to_stage for item in store.experiment_transition_history(proposal.experiment.id)
    ] == [
        ExperimentStage.PROPOSED,
        ExperimentStage.OFFLINE_PASSED,
        ExperimentStage.SHADOW,
        ExperimentStage.CANARY,
        ExperimentStage.PROMOTED,
        ExperimentStage.ROLLED_BACK,
    ]


def test_experiment_cannot_skip_gates_or_move_time_backwards() -> None:
    store, clock, _, evolution = _applications()
    proposal = _propose(evolution, evidence="artifact://diagnosis/2")

    with pytest.raises(DomainError, match="legal experiment transition"):
        _advance(
            evolution,
            store,
            clock,
            proposal.experiment.id,
            ExperimentStage.PROMOTED,
            reason="skip gates",
            evidence_ref="approval://invalid",
            idempotency_key=f"advance:{proposal.experiment.id}:invalid-promotion",
        )
    assert store.evolution_experiment(proposal.experiment.id) == proposal.experiment
    assert len(store.experiment_transition_history(proposal.experiment.id)) == 1

    clock.current = proposal.experiment.updated_at.replace(year=2025)
    with pytest.raises(DomainError, match="non-decreasing"):
        _advance(
            evolution,
            store,
            clock,
            proposal.experiment.id,
            ExperimentStage.OFFLINE_PASSED,
            reason="stale report",
            evidence_ref="artifact://stale",
            idempotency_key=f"advance:{proposal.experiment.id}:stale",
        )
    assert len(store.experiment_transition_history(proposal.experiment.id)) == 1


def test_competing_promotions_leave_loser_at_canary_without_partial_audit() -> None:
    store, clock, _, evolution = _applications()
    first = _propose(evolution, evidence="artifact://diagnosis/first")
    second = _propose(evolution, evidence="artifact://diagnosis/second")
    _advance_to_canary(evolution, store, clock, first.experiment.id)
    _advance_to_canary(evolution, store, clock, second.experiment.id)

    clock.advance(seconds=1)
    _advance(
        evolution,
        store,
        clock,
        first.experiment.id,
        ExperimentStage.PROMOTED,
        reason="first candidate won",
        evidence_ref="approval://promotion/first",
        idempotency_key=f"advance:{first.experiment.id}:promoted",
    )
    second_history_before = store.experiment_transition_history(second.experiment.id)

    clock.advance(seconds=1)
    with pytest.raises(ConflictError, match="does not match experiment state"):
        _advance(
            evolution,
            store,
            clock,
            second.experiment.id,
            ExperimentStage.PROMOTED,
            reason="stale baseline promotion",
            evidence_ref="approval://promotion/second",
            idempotency_key=f"advance:{second.experiment.id}:promoted",
        )

    assert store.active_strategy() == first.candidate
    persisted_second = store.evolution_experiment(second.experiment.id)
    assert persisted_second is not None
    assert persisted_second.stage is ExperimentStage.CANARY
    assert store.experiment_transition_history(second.experiment.id) == second_history_before
    assert len(store.strategy_activation_history()) == 2


def test_proposal_requires_failure_signal_and_unknown_experiment_fails_closed() -> None:
    store, clock, _, evolution = _applications()

    assert (
        evolution.propose(
            FailureDiagnosis(),
            reason="no failures",
            evidence_ref="artifact://diagnosis/empty",
            idempotency_key="proposal:empty",
        )
        is None
    )
    assert store.strategy_activation_history()[0].kind is StrategyActivationKind.BOOTSTRAP

    with pytest.raises(NotFoundError, match="not found"):
        unknown = EvolutionExperiment(
            id=UUID(int=999),
            baseline_id=UUID(int=997),
            candidate_id=UUID(int=998),
            stage=ExperimentStage.PROPOSED,
            created_at=clock.now(),
            updated_at=clock.now(),
        )
        evolution.advance(
            unknown.id,
            ExperimentStage.REJECTED,
            receipt=_receipt(
                unknown,
                clock,
                ExperimentStage.REJECTED,
                reason="unknown experiment",
                evidence_ref="ticket://missing",
            ),
            idempotency_key="advance:unknown:rejected",
        )


def test_evolution_writes_are_idempotent_and_conflicting_reuse_fails() -> None:
    store, clock, _, evolution = _applications()
    first = evolution.propose(
        FailureDiagnosis(context_mismatches=3),
        reason="stable diagnosis",
        evidence_ref="artifact://idempotency/diagnosis",
        idempotency_key="proposal:stable",
    )
    replay = evolution.propose(
        FailureDiagnosis(context_mismatches=3),
        reason="stable diagnosis",
        evidence_ref="artifact://idempotency/diagnosis",
        idempotency_key=" proposal:stable ",
    )
    assert first is not None and replay is not None
    assert replay.candidate == first.candidate
    assert replay.experiment == first.experiment
    assert replay.idempotent_replay is True
    assert len(store.experiment_transition_history(first.experiment.id)) == 1

    with pytest.raises(ConflictError, match="proposal idempotency"):
        evolution.propose(
            FailureDiagnosis(harmful_results=3),
            reason="different diagnosis",
            evidence_ref="artifact://idempotency/diagnosis",
            idempotency_key="proposal:stable",
        )

    clock.advance(seconds=1)
    advanced = _advance(
        evolution,
        store,
        clock,
        first.experiment.id,
        ExperimentStage.OFFLINE_PASSED,
        reason="offline passed",
        evidence_ref="artifact://idempotency/offline",
        idempotency_key="advance:stable:offline",
    )
    replayed_advance = _advance(
        evolution,
        store,
        clock,
        first.experiment.id,
        ExperimentStage.OFFLINE_PASSED,
        reason="offline passed",
        evidence_ref="artifact://idempotency/offline",
        idempotency_key="advance:stable:offline",
    )
    assert replayed_advance.experiment == advanced.experiment
    assert replayed_advance.idempotent_replay is True
    assert len(store.experiment_transition_history(first.experiment.id)) == 2

    with pytest.raises(ConflictError, match="transition idempotency"):
        _advance(
            evolution,
            store,
            clock,
            first.experiment.id,
            ExperimentStage.SHADOW,
            reason="different target",
            evidence_ref="artifact://idempotency/offline",
            idempotency_key="advance:stable:offline",
        )
