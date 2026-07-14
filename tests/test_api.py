from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import Harness
from evolvable_memory.api.app import create_app


def test_http_vertical_slice(harness: Harness) -> None:
    client = TestClient(create_app(harness.app))
    create = client.post(
        "/v1/preferences",
        json={
            "tenant_id": "tenant-a",
            "subject_id": "alice",
            "source": "conversation",
            "idempotency_key": "api-turn-1",
            "key": "drink.preference",
            "value": "decaf coffee",
            "context": {"time_of_day": "evening"},
            "evidence_text": "晚上我只喝低因咖啡",
            "confidence": 0.92,
        },
    )
    assert create.status_code == 201
    memory = create.json()

    recall = client.post(
        "/v1/recall",
        json={
            "tenant_id": "tenant-a",
            "subject_id": "alice",
            "query": "晚上喝什么饮料",
            "context": {"time_of_day": "evening"},
            "limit": 5,
        },
    )
    assert recall.status_code == 200
    trace = recall.json()
    assert trace["items"][0]["revision_id"] == memory["revision_id"]

    outcome = client.post(
        "/v1/outcomes",
        json={
            "tenant_id": "tenant-a",
            "subject_id": "alice",
            "trace_id": trace["trace_id"],
            "revision_id": memory["revision_id"],
            "kind": "helpful",
            "idempotency_key": "api-task-1:outcome",
        },
    )
    assert outcome.status_code == 201
    assert outcome.json()["utility"]["mean"] > 0.5

    correction = client.post(
        f"/v1/preferences/{memory['record_id']}/corrections",
        json={
            "tenant_id": "tenant-a",
            "subject_id": "alice",
            "source": "explicit-feedback",
            "idempotency_key": "api-turn-2:correction",
            "value": "herbal tea",
            "evidence_text": "其实晚上改喝花草茶",
            "reason": "user correction",
        },
    )
    assert correction.status_code == 201
    assert correction.json()["sequence"] == 2

    history = client.get(
        f"/v1/preferences/{memory['record_id']}/revisions",
        params={"tenant_id": "tenant-a", "subject_id": "alice"},
    )
    assert history.status_code == 200
    assert [revision["value"] for revision in history.json()] == [
        "decaf coffee",
        "herbal tea",
    ]

    current = client.get(
        "/v1/preferences",
        params={"tenant_id": "tenant-a", "subject_id": "alice"},
    )
    assert current.status_code == 200
    assert [(item["key"], item["value"], item["sequence"]) for item in current.json()] == [
        ("drink.preference", "herbal tea", 2)
    ]


def test_http_maps_domain_errors_and_validates_payload(harness: Harness) -> None:
    client = TestClient(create_app(harness.app))
    missing = client.get(
        "/v1/preferences/00000000-0000-0000-0000-000000000099/revisions",
        params={"tenant_id": "tenant-a", "subject_id": "alice"},
    )
    invalid = client.post(
        "/v1/recall",
        json={
            "tenant_id": "tenant-a",
            "subject_id": "alice",
            "query": "",
            "limit": 0,
        },
    )

    assert missing.status_code == 404
    assert missing.json()["error"] == "NotFoundError"
    assert invalid.status_code == 422


def test_health(harness: Harness) -> None:
    client = TestClient(create_app(harness.app))
    assert client.get("/health").json() == {"status": "ok", "version": "0.1.0"}


def test_service_discovery_and_openapi_explain_the_first_workflow(harness: Harness) -> None:
    client = TestClient(create_app(harness.app))

    service = client.get("/")
    schema = client.get("/openapi.json").json()

    assert service.status_code == 200
    assert service.json()["frontend_url"] == "http://127.0.0.1:33009"
    assert service.json()["production_ready"] is False
    assert "/v1/preferences" in schema["paths"]
    assert schema["paths"]["/v1/recall"]["post"]["summary"] == "执行上下文记忆召回"
    outcome_schema = schema["components"]["schemas"]["OutcomeWriteRequest"]
    assert outcome_schema["example"]["kind"] == "helpful"


def test_frontend_origin_is_allowed_by_cors(harness: Harness) -> None:
    client = TestClient(create_app(harness.app))

    preflight = client.options(
        "/v1/recall",
        headers={
            "Origin": "http://127.0.0.1:33009",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    disallowed = client.get(
        "/health",
        headers={"Origin": "https://untrusted.example"},
    )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:33009"
    assert "access-control-allow-origin" not in disallowed.headers
