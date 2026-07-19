from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from conftest import FixedClock, Harness, SequentialIds
from evolvable_memory.adapters.in_memory import InMemoryMemoryStore
from evolvable_memory.adapters.in_memory_governance import InMemoryPrivacyGovernance
from evolvable_memory.api.app import create_app
from evolvable_memory.api.contract import production_blockers
from evolvable_memory.application.governance import EraseSubject, PrivacyApplication
from evolvable_memory.application.projection_types import ProjectionSearchResult
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.domain.common import Scope
from evolvable_memory.domain.governance import (
    ErasureStatus,
    GovernanceUnavailableError,
    ProcessingDeniedError,
)

ALICE = Scope("tenant-a", "alice")
BOB = Scope("tenant-a", "bob")


def _client(harness: Harness, governance: InMemoryPrivacyGovernance) -> TestClient:
    return TestClient(
        create_app(
            harness.app,
            clock=harness.clock,
            privacy_governance=governance,
        )
    )


def _grant_payload(
    scope: Scope,
    *,
    key: str,
    purposes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "tenant_id": scope.tenant_id,
        "subject_id": scope.subject_id,
        "purposes": purposes or ["personalization"],
        "lawful_basis": "explicit-consent",
        "idempotency_key": key,
        "valid_from": "2026-07-01T00:00:00Z",
        "valid_until": "2026-08-01T00:00:00Z",
    }


def _preference_payload(
    scope: Scope,
    *,
    key: str,
    purpose: str = "personalization",
) -> dict[str, object]:
    return {
        "tenant_id": scope.tenant_id,
        "subject_id": scope.subject_id,
        "source": "conversation",
        "idempotency_key": key,
        "key": "drink.preference",
        "value": "decaf coffee",
        "context": {"time_of_day": "evening"},
        "evidence_text": "sensitive evidence must not survive erasure",
        "confidence": 0.9,
        "purpose": purpose,
    }


def _issue_grant(client: TestClient, scope: Scope, key: str) -> dict[str, object]:
    response = client.post(
        "/v1/governance/processing-grants",
        json=_grant_payload(scope, key=key),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_processing_grant_is_a_fail_closed_purpose_bound_gate(harness: Harness) -> None:
    governance = InMemoryPrivacyGovernance()
    client = _client(harness, governance)

    denied = client.post(
        "/v1/preferences",
        json=_preference_payload(ALICE, key="denied-without-grant"),
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "processing_not_granted"
    assert harness.store.observation_count == 0

    expired_grant = client.post(
        "/v1/governance/processing-grants",
        json={
            **_grant_payload(BOB, key="grant:bob:expired"),
            "valid_until": "2026-07-02T00:00:00Z",
        },
    )
    assert expired_grant.status_code == 201
    expired_write = client.post(
        "/v1/preferences",
        json=_preference_payload(BOB, key="denied-expired-grant"),
    )
    assert expired_write.status_code == 403
    assert expired_write.json()["detail"] == "processing_not_granted"

    grant = _issue_grant(client, ALICE, "grant:alice:1")
    replay = client.post(
        "/v1/governance/processing-grants",
        json=_grant_payload(ALICE, key="grant:alice:1"),
    )
    assert replay.status_code == 201
    assert replay.json()["grant_id"] == grant["grant_id"]

    wrong_purpose = client.post(
        "/v1/preferences",
        json=_preference_payload(
            ALICE,
            key="denied-wrong-purpose",
            purpose="model-training",
        ),
    )
    assert wrong_purpose.status_code == 403
    assert wrong_purpose.json()["detail"] == "processing_not_granted"
    assert harness.store.observation_count == 0

    allowed = client.post(
        "/v1/preferences",
        json=_preference_payload(ALICE, key="allowed"),
    )
    assert allowed.status_code == 201
    assert harness.store.observation_count == 1

    revoked = client.post(
        f"/v1/governance/processing-grants/{grant['grant_id']}/revocation",
        json={"tenant_id": ALICE.tenant_id, "subject_id": ALICE.subject_id},
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    revoked_replay = client.post(
        f"/v1/governance/processing-grants/{grant['grant_id']}/revocation",
        json={"tenant_id": ALICE.tenant_id, "subject_id": ALICE.subject_id},
    )
    assert revoked_replay.json() == revoked.json()

    read_after_revocation = client.get(
        "/v1/preferences",
        params={"tenant_id": ALICE.tenant_id, "subject_id": ALICE.subject_id},
    )
    assert read_after_revocation.status_code == 403
    assert read_after_revocation.json()["detail"] == "processing_not_granted"


def test_suppression_overrides_historical_recall_and_erasure_is_complete_and_idempotent(
    harness: Harness,
) -> None:
    governance = InMemoryPrivacyGovernance()
    client = _client(harness, governance)
    _issue_grant(client, ALICE, "grant:alice")
    _issue_grant(client, BOB, "grant:bob")
    alice_memory = client.post(
        "/v1/preferences",
        json=_preference_payload(ALICE, key="alice:preference"),
    )
    bob_memory = client.post(
        "/v1/preferences",
        json=_preference_payload(BOB, key="bob:preference"),
    )
    assert alice_memory.status_code == bob_memory.status_code == 201
    recall = client.post(
        "/v1/recall",
        json={
            "tenant_id": ALICE.tenant_id,
            "subject_id": ALICE.subject_id,
            "query": "secret evening drink",
            "context": {"time_of_day": "evening"},
        },
    )
    assert recall.status_code == 200
    outcome = client.post(
        "/v1/outcomes",
        json={
            "tenant_id": ALICE.tenant_id,
            "subject_id": ALICE.subject_id,
            "trace_id": recall.json()["trace_id"],
            "revision_id": alice_memory.json()["revision_id"],
            "kind": "helpful",
            "idempotency_key": "alice:outcome",
            "note": "sensitive outcome note",
        },
    )
    assert outcome.status_code == 201

    suppression = client.post(
        "/v1/governance/suppressions",
        json={
            "tenant_id": ALICE.tenant_id,
            "subject_id": ALICE.subject_id,
            "reason_code": "subject-request",
            "idempotency_key": "alice:suppress",
        },
    )
    assert suppression.status_code == 201
    historical_recall = client.post(
        "/v1/recall",
        json={
            "tenant_id": ALICE.tenant_id,
            "subject_id": ALICE.subject_id,
            "query": "secret evening drink",
            "context": {},
            "valid_at": "2026-07-01T00:00:00Z",
            "known_at": "2026-07-01T00:00:00Z",
        },
    )
    assert historical_recall.status_code == 403
    assert historical_recall.json()["detail"] == "processing_suppressed"

    erasure_payload = {
        "tenant_id": ALICE.tenant_id,
        "subject_id": ALICE.subject_id,
        "reason_code": "subject-request",
        "idempotency_key": "alice:erase",
    }
    erased = client.post("/v1/governance/erasures", json=erasure_payload)
    replay = client.post("/v1/governance/erasures", json=erasure_payload)
    assert erased.status_code == replay.status_code == 201
    receipt = erased.json()
    assert replay.json() == receipt
    assert receipt["status"] == "completed"
    assert receipt["summary"]["observations"] == 1
    assert receipt["summary"]["memory_records"] == 1
    assert receipt["summary"]["recall_traces"] == 1
    assert receipt["summary"]["outcomes"] == 1
    assert receipt["handler_results"] == {
        "authority": "completed",
        "outbox": "completed",
        "projection": "not_applicable",
    }
    serialized = json.dumps(receipt)
    assert all(
        secret not in serialized
        for secret in (
            ALICE.tenant_id,
            ALICE.subject_id,
            "decaf coffee",
            "sensitive evidence",
            "secret evening drink",
            "sensitive outcome note",
        )
    )

    cross_scope_receipt = client.get(
        f"/v1/governance/erasures/{receipt['request_id']}",
        params={"tenant_id": BOB.tenant_id, "subject_id": BOB.subject_id},
    )
    assert cross_scope_receipt.status_code == 404
    own_receipt = client.get(
        f"/v1/governance/erasures/{receipt['request_id']}",
        params={"tenant_id": ALICE.tenant_id, "subject_id": ALICE.subject_id},
    )
    assert own_receipt.json() == receipt
    assert harness.app.list_preferences(ALICE) == ()
    assert len(harness.app.list_preferences(BOB)) == 1
    assert harness.store.trace(ALICE, UUID(recall.json()["trace_id"])) is None
    assert harness.store.observation_count == 1


def test_scope_lock_orders_in_flight_processing_before_suppression() -> None:
    governance = InMemoryPrivacyGovernance()
    now = datetime(2026, 7, 19, tzinfo=UTC)
    governance.issue_processing_grant(
        grant_id=UUID(int=1),
        scope=ALICE,
        purposes=("personalization",),
        lawful_basis="explicit-consent",
        policy_version="privacy-v1",
        issued_by="privacy-officer",
        idempotency_key="grant:alice",
        valid_from=now,
        valid_until=now + timedelta(days=1),
        created_at=now,
    )
    processing_entered = Event()
    release_processing = Event()
    suppression_started = Event()

    def processing() -> None:
        with governance.processing_context(ALICE, purpose="personalization", at=now):
            processing_entered.set()
            assert release_processing.wait(timeout=2)

    def suppress() -> None:
        suppression_started.set()
        governance.suppress(
            fence_id=UUID(int=2),
            scope=ALICE,
            reason_code="subject-request",
            policy_version="privacy-v1",
            requested_by="privacy-officer",
            idempotency_key="suppress:alice",
            created_at=now,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        processing_future = executor.submit(processing)
        assert processing_entered.wait(timeout=2)
        suppression_future = executor.submit(suppress)
        assert suppression_started.wait(timeout=2)
        with pytest.raises(TimeoutError):
            suppression_future.result(timeout=0.05)
        release_processing.set()
        processing_future.result(timeout=2)
        suppression_future.result(timeout=2)

    with (
        pytest.raises(ProcessingDeniedError, match="processing_suppressed"),
        governance.processing_context(ALICE, purpose="personalization", at=now),
    ):
        pass


class _FailingErasureProjection:
    def close(self) -> None:
        return

    def is_ready(self) -> bool:
        return True

    def search(self, *_args: object, **_kwargs: object) -> ProjectionSearchResult:
        return ProjectionSearchResult()

    def delete_scope(self, _scope: Scope) -> int:
        raise OSError("projection unavailable")


def test_erasure_handler_failure_stays_pending_and_suppressed() -> None:
    clock = FixedClock()
    governance = InMemoryPrivacyGovernance()
    memory = MemoryApplication(
        store=InMemoryMemoryStore(),
        clock=clock,
        ids=SequentialIds(),
        recall_projection=_FailingErasureProjection(),
    )
    privacy = PrivacyApplication(
        governance=governance,
        memory=memory,
        clock=clock,
        ids=SequentialIds(),
        policy_version="privacy-v1",
    )

    with pytest.raises(GovernanceUnavailableError, match="remains pending"):
        privacy.erase(
            EraseSubject(
                scope=ALICE,
                reason_code="subject-request",
                idempotency_key="erase:dependency-failure",
            ),
            requested_by="privacy-officer",
        )

    request = governance.erasure(ALICE, UUID(int=1))
    assert request is not None
    assert request.status is ErasureStatus.PENDING
    assert request.error_code == "OSError"
    with (
        pytest.raises(ProcessingDeniedError, match="processing_suppressed"),
        governance.processing_context(
            ALICE,
            purpose="personalization",
            at=clock.now(),
        ),
    ):
        pass


def test_all_governance_blockers_clear_only_for_ready_persistent_runtime() -> None:
    assert production_blockers(
        store="postgres",
        auth_mode="jwt",
        governance_mode="postgres",
        governance_ready=False,
        audit_sink="postgres",
        audit_ready=False,
    ) == (
        "privacy.processing-grants",
        "privacy.suppression-erasure",
        "audit.durable-sink",
    )
    assert (
        production_blockers(
            store="postgres",
            auth_mode="jwt",
            governance_mode="postgres",
            governance_ready=True,
            audit_sink="postgres",
            audit_ready=True,
        )
        == ()
    )
