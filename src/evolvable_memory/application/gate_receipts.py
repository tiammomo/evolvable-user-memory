from __future__ import annotations

import json

from evolvable_memory.domain.evolution import GateReceipt


def gate_receipt_claims(receipt: GateReceipt) -> dict[str, object]:
    """Return the versioned claims covered by a gate receipt signature."""
    return {
        "schema": "evolvable-memory.gate-receipt.v1",
        "id": str(receipt.id),
        "experiment_id": str(receipt.experiment_id),
        "baseline_id": str(receipt.baseline_id),
        "candidate_id": str(receipt.candidate_id),
        "from_stage": receipt.from_stage.value,
        "to_stage": receipt.to_stage.value,
        "decision": receipt.decision.value,
        "artifact_ref": receipt.artifact_ref,
        "artifact_sha256": receipt.artifact_sha256,
        "issuer": receipt.issuer,
        "key_id": receipt.key_id,
        "issued_at": receipt.issued_at.isoformat(),
        "expires_at": receipt.expires_at.isoformat(),
        "hard_gates_passed": receipt.hard_gates_passed,
        "reason": receipt.reason,
    }


def canonical_gate_receipt_payload(receipt: GateReceipt) -> bytes:
    return json.dumps(
        gate_receipt_claims(receipt),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
