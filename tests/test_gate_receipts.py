from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

import pytest

from conftest import FixedClock, SequentialIds
from evolvable_memory.adapters.gate_receipts import (
    HmacGateReceiptSigner,
    HmacGateReceiptVerifier,
)
from evolvable_memory.adapters.in_memory import InMemoryMemoryStore
from evolvable_memory.application.evolution import EvolutionApplication, EvolutionProposal
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.domain.common import ConflictError, DomainError
from evolvable_memory.domain.evolution import (
    EvolutionExperiment,
    ExperimentStage,
    FailureDiagnosis,
    GateDecision,
    GateReceipt,
)

NOW = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
ISSUER = "trusted-evaluation-service"
KEY_ID = "evaluation-key-2026-07"
SECRET = b"gate-receipt-test-secret-with-at-least-32-bytes"
OTHER_SECRET = b"different-gate-receipt-secret-at-least-32-bytes"


def _experiment(*, identity: int = 3) -> EvolutionExperiment:
    return EvolutionExperiment(
        id=UUID(int=identity),
        baseline_id=UUID(int=1),
        candidate_id=UUID(int=identity + 10),
        stage=ExperimentStage.PROPOSED,
        created_at=NOW,
        updated_at=NOW,
    )


def _signer(*, issuer: str = ISSUER, key_id: str = KEY_ID) -> HmacGateReceiptSigner:
    return HmacGateReceiptSigner(issuer=issuer, key_id=key_id, secret=SECRET)


def _receipt(
    experiment: EvolutionExperiment,
    *,
    target: ExperimentStage = ExperimentStage.OFFLINE_PASSED,
    decision: GateDecision = GateDecision.PASS,
    issued_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(minutes=5),
    hard_gates_passed: bool = True,
    receipt_id: int = 20,
) -> GateReceipt:
    artifact_ref = f"artifact://evaluation/{experiment.id}/{target.value}"
    return _signer().issue(
        receipt_id=UUID(int=receipt_id),
        experiment=experiment,
        target=target,
        decision=decision,
        artifact_ref=artifact_ref,
        artifact_sha256=sha256(artifact_ref.encode()).hexdigest(),
        issued_at=issued_at,
        expires_at=expires_at,
        hard_gates_passed=hard_gates_passed,
        reason=f"verified {target.value}",
    )


def _application() -> tuple[
    InMemoryMemoryStore,
    FixedClock,
    EvolutionApplication,
    EvolutionProposal,
]:
    store = InMemoryMemoryStore()
    clock = FixedClock(current=NOW)
    ids = SequentialIds()
    MemoryApplication(store=store, clock=clock, ids=ids)
    application = EvolutionApplication(
        store=store,
        clock=clock,
        ids=ids,
        gate_verifier=HmacGateReceiptVerifier({(ISSUER, KEY_ID): SECRET}),
    )
    proposal = application.propose(
        FailureDiagnosis(context_mismatches=4),
        reason="evaluation diagnosis",
        evidence_ref="artifact://evaluation/diagnosis",
        idempotency_key="gate-receipt:proposal",
    )
    assert proposal is not None
    return store, clock, application, proposal


def test_hmac_gate_receipt_accepts_authentic_claims_and_rotating_keys() -> None:
    receipt = _receipt(_experiment())
    verifier = HmacGateReceiptVerifier(
        {
            (ISSUER, "retired-key"): OTHER_SECRET,
            (ISSUER, KEY_ID): SECRET,
        }
    )

    assert verifier.verify(receipt, at=NOW) is None


def test_hmac_gate_receipt_fails_closed_for_tampering_and_untrusted_key() -> None:
    receipt = _receipt(_experiment())
    verifier = HmacGateReceiptVerifier({(ISSUER, KEY_ID): SECRET})

    with pytest.raises(DomainError, match="signature is invalid"):
        verifier.verify(replace(receipt, reason="tampered gate result"), at=NOW)

    untrusted = _signer(key_id="unknown-key").issue(
        receipt_id=UUID(int=21),
        experiment=_experiment(),
        target=ExperimentStage.OFFLINE_PASSED,
        decision=GateDecision.PASS,
        artifact_ref="artifact://evaluation/untrusted",
        artifact_sha256="a" * 64,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        hard_gates_passed=True,
        reason="untrusted issuer key",
    )
    with pytest.raises(DomainError, match="not trusted"):
        verifier.verify(untrusted, at=NOW)


def test_hmac_gate_receipt_enforces_not_before_and_expiry() -> None:
    verifier = HmacGateReceiptVerifier({(ISSUER, KEY_ID): SECRET})
    future = _receipt(
        _experiment(),
        issued_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    expired = _receipt(
        _experiment(),
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW,
        receipt_id=22,
    )

    with pytest.raises(DomainError, match="not yet valid"):
        verifier.verify(future, at=NOW)
    with pytest.raises(DomainError, match="expired"):
        verifier.verify(expired, at=NOW)


def test_gate_receipt_configuration_and_domain_shape_fail_closed() -> None:
    with pytest.raises(DomainError, match="at least one"):
        HmacGateReceiptVerifier({})
    with pytest.raises(DomainError, match="at least 32 bytes"):
        HmacGateReceiptSigner(issuer=ISSUER, key_id=KEY_ID, secret=b"short")
    with pytest.raises(DomainError, match="expires_at must follow"):
        _receipt(_experiment(), expires_at=NOW)
    with pytest.raises(DomainError, match="SHA-256"):
        replace(_receipt(_experiment()), artifact_sha256="not-a-digest")
    with pytest.raises(DomainError, match="legal experiment transition"):
        _receipt(_experiment(), target=ExperimentStage.PROMOTED)


def test_application_rejects_tampered_expired_and_cross_experiment_receipts() -> None:
    store, clock, application, proposal = _application()
    authentic = _receipt(proposal.experiment)
    history_before = store.experiment_transition_history(proposal.experiment.id)

    with pytest.raises(DomainError, match="signature is invalid"):
        application.advance(
            proposal.experiment.id,
            ExperimentStage.OFFLINE_PASSED,
            receipt=replace(authentic, artifact_ref="artifact://tampered"),
            idempotency_key="gate-receipt:tampered",
        )

    clock.advance(minutes=6)
    with pytest.raises(DomainError, match="expired"):
        application.advance(
            proposal.experiment.id,
            ExperimentStage.OFFLINE_PASSED,
            receipt=authentic,
            idempotency_key="gate-receipt:expired",
        )
    clock.set(NOW)

    other_receipt = _receipt(_experiment(identity=99), receipt_id=23)
    with pytest.raises(DomainError, match="does not match the evolution experiment"):
        application.advance(
            proposal.experiment.id,
            ExperimentStage.OFFLINE_PASSED,
            receipt=other_receipt,
            idempotency_key="gate-receipt:cross-experiment",
        )

    assert store.evolution_experiment(proposal.experiment.id) == proposal.experiment
    assert store.experiment_transition_history(proposal.experiment.id) == history_before


def test_application_rejects_wrong_target_decision_and_failed_hard_gates() -> None:
    store, _, application, proposal = _application()
    history_before = store.experiment_transition_history(proposal.experiment.id)

    with pytest.raises(DomainError, match="requested experiment transition"):
        application.advance(
            proposal.experiment.id,
            ExperimentStage.REJECTED,
            receipt=_receipt(proposal.experiment),
            idempotency_key="gate-receipt:wrong-target",
        )
    with pytest.raises(DomainError, match="decision does not authorize"):
        application.advance(
            proposal.experiment.id,
            ExperimentStage.OFFLINE_PASSED,
            receipt=_receipt(
                proposal.experiment,
                decision=GateDecision.REJECT,
                receipt_id=24,
            ),
            idempotency_key="gate-receipt:wrong-decision",
        )
    with pytest.raises(DomainError, match="hard gates passed"):
        application.advance(
            proposal.experiment.id,
            ExperimentStage.OFFLINE_PASSED,
            receipt=_receipt(
                proposal.experiment,
                hard_gates_passed=False,
                receipt_id=25,
            ),
            idempotency_key="gate-receipt:hard-gate-failed",
        )

    assert store.experiment_transition_history(proposal.experiment.id) == history_before


def test_exact_successful_retry_remains_idempotent_after_receipt_expiry() -> None:
    store, clock, application, proposal = _application()
    receipt = _receipt(
        proposal.experiment,
        expires_at=NOW + timedelta(seconds=1),
        receipt_id=26,
    )
    first = application.advance(
        proposal.experiment.id,
        ExperimentStage.OFFLINE_PASSED,
        receipt=receipt,
        idempotency_key="gate-receipt:expiry-replay",
    )
    clock.advance(seconds=2)

    replay = application.advance(
        proposal.experiment.id,
        ExperimentStage.OFFLINE_PASSED,
        receipt=receipt,
        idempotency_key="gate-receipt:expiry-replay",
    )

    assert replay.experiment == first.experiment
    assert replay.idempotent_replay is True
    assert len(store.experiment_transition_history(proposal.experiment.id)) == 2

    with pytest.raises(ConflictError, match="transition idempotency"):
        application.advance(
            proposal.experiment.id,
            ExperimentStage.OFFLINE_PASSED,
            receipt=replace(receipt, signature="f" * 64),
            idempotency_key="gate-receipt:expiry-replay",
        )
