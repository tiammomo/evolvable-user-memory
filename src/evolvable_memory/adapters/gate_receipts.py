from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from evolvable_memory.application.gate_receipts import canonical_gate_receipt_payload
from evolvable_memory.domain.common import DomainError, require_text, require_utc
from evolvable_memory.domain.evolution import (
    EvolutionExperiment,
    ExperimentStage,
    GateDecision,
    GateReceipt,
)

_MINIMUM_SECRET_BYTES = 32
_PLACEHOLDER_SIGNATURE = "0" * 64


class HmacGateReceiptSigner:
    """Issues internal gate receipts using a secret held outside persisted state."""

    def __init__(self, *, issuer: str, key_id: str, secret: bytes) -> None:
        self._issuer = require_text(issuer, "gate receipt issuer")
        self._key_id = require_text(key_id, "gate receipt key_id")
        self._secret = _validated_secret(secret)

    def issue(
        self,
        *,
        receipt_id: UUID,
        experiment: EvolutionExperiment,
        target: ExperimentStage,
        decision: GateDecision,
        artifact_ref: str,
        artifact_sha256: str,
        issued_at: datetime,
        expires_at: datetime,
        hard_gates_passed: bool,
        reason: str,
    ) -> GateReceipt:
        unsigned = GateReceipt(
            id=receipt_id,
            experiment_id=experiment.id,
            baseline_id=experiment.baseline_id,
            candidate_id=experiment.candidate_id,
            from_stage=experiment.stage,
            to_stage=target,
            decision=decision,
            artifact_ref=artifact_ref,
            artifact_sha256=artifact_sha256,
            issuer=self._issuer,
            key_id=self._key_id,
            issued_at=issued_at,
            expires_at=expires_at,
            hard_gates_passed=hard_gates_passed,
            reason=reason,
            signature=_PLACEHOLDER_SIGNATURE,
        )
        signature = hmac.new(
            self._secret,
            canonical_gate_receipt_payload(unsigned),
            sha256,
        ).hexdigest()
        return replace(unsigned, signature=signature)


class HmacGateReceiptVerifier:
    """Verifies a rotating set of trusted (issuer, key ID) HMAC keys."""

    def __init__(self, trusted_keys: Mapping[tuple[str, str], bytes]) -> None:
        if not trusted_keys:
            raise DomainError("at least one gate receipt verification key is required")
        self._trusted_keys = {
            (
                require_text(issuer, "gate receipt issuer"),
                require_text(key_id, "gate receipt key_id"),
            ): _validated_secret(secret)
            for (issuer, key_id), secret in trusted_keys.items()
        }

    def verify(self, receipt: GateReceipt, *, at: datetime) -> None:
        verified_at = require_utc(at, "gate receipt verification time")
        secret = self._trusted_keys.get((receipt.issuer, receipt.key_id))
        if secret is None:
            raise DomainError("gate receipt signing key is not trusted")
        expected = hmac.new(
            secret,
            canonical_gate_receipt_payload(receipt),
            sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, receipt.signature):
            raise DomainError("gate receipt signature is invalid")
        if receipt.issued_at > verified_at:
            raise DomainError("gate receipt is not yet valid")
        if receipt.expires_at <= verified_at:
            raise DomainError("gate receipt has expired")


def _validated_secret(secret: bytes) -> bytes:
    if len(secret) < _MINIMUM_SECRET_BYTES:
        raise DomainError("gate receipt HMAC secret must contain at least 32 bytes")
    return bytes(secret)
