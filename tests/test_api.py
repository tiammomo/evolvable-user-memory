from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import Harness
from evolvable_memory.api.app import create_app
from evolvable_memory.api.schemas import (
    MAX_CONTEXT_FACETS,
    MAX_CONTEXT_KEY_LENGTH,
    MAX_CONTEXT_VALUE_LENGTH,
    MAX_MEMORY_VALUE_LENGTH,
    MAX_NOTE_LENGTH,
    MAX_QUERY_LENGTH,
)
from evolvable_memory.config import Settings


def test_http_vertical_slice(harness: Harness) -> None:
    client = TestClient(create_app(harness.app, clock=harness.clock))
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
    assert trace["valid_at"] == trace["known_at"] == trace["created_at"]
    assert trace["items"][0]["revision_valid_from"] == trace["items"][0]["revision_recorded_at"]

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
    client = TestClient(create_app(harness.app, clock=harness.clock))
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
    assert missing.json()["request_id"] == missing.headers["x-request-id"]
    assert invalid.status_code == 422


def test_http_recall_exposes_bitemporal_boundaries_and_fails_closed(
    harness: Harness,
) -> None:
    client = TestClient(create_app(harness.app, clock=harness.clock))
    original_valid_at = harness.clock.current - timedelta(days=30)
    created = client.post(
        "/v1/preferences",
        json={
            "tenant_id": "tenant-time",
            "subject_id": "alice",
            "source": "conversation",
            "idempotency_key": "time/write",
            "key": "drink.preference",
            "value": "coffee",
            "context": {"time_of_day": "morning"},
            "evidence_text": "I drank coffee last month",
            "confidence": 0.9,
            "occurred_at": original_valid_at.isoformat(),
        },
    ).json()
    known_before_correction = harness.clock.current
    harness.clock.advance(days=1)
    corrected = client.post(
        f"/v1/preferences/{created['record_id']}/corrections",
        json={
            "tenant_id": "tenant-time",
            "subject_id": "alice",
            "source": "explicit-feedback",
            "idempotency_key": "time/correction",
            "value": "tea",
            "evidence_text": "I switched to tea three weeks ago",
            "reason": "late correction",
            "occurred_at": (original_valid_at + timedelta(days=7)).isoformat(),
        },
    ).json()

    historical = client.post(
        "/v1/recall",
        json={
            "tenant_id": "tenant-time",
            "subject_id": "alice",
            "query": "drink preference coffee tea",
            "context": {"time_of_day": "morning"},
            "valid_at": harness.clock.current.isoformat(),
            "known_at": known_before_correction.isoformat(),
        },
    )
    current = client.post(
        "/v1/recall",
        json={
            "tenant_id": "tenant-time",
            "subject_id": "alice",
            "query": "drink preference coffee tea",
            "context": {"time_of_day": "morning"},
        },
    )
    naive = client.post(
        "/v1/recall",
        json={
            "tenant_id": "tenant-time",
            "subject_id": "alice",
            "query": "drink",
            "known_at": "2026-07-14T04:00:00",
        },
    )
    future_known = client.post(
        "/v1/recall",
        json={
            "tenant_id": "tenant-time",
            "subject_id": "alice",
            "query": "drink",
            "known_at": (harness.clock.current + timedelta(seconds=1)).isoformat(),
        },
    )

    assert historical.status_code == current.status_code == 200
    assert historical.json()["items"][0]["revision_id"] == created["revision_id"]
    assert current.json()["items"][0]["revision_id"] == corrected["revision_id"]
    assert historical.json()["known_at"] == known_before_correction.isoformat().replace(
        "+00:00", "Z"
    )
    assert naive.status_code == 422
    assert future_known.status_code == 400
    assert future_known.json()["error"] == "DomainError"


def test_http_rejects_oversized_text_and_context_inputs(harness: Harness) -> None:
    client = TestClient(create_app(harness.app, clock=harness.clock))
    preference_payload = {
        "tenant_id": "tenant-a",
        "subject_id": "alice",
        "source": "conversation",
        "idempotency_key": "bounded-input",
        "key": "drink.preference",
        "value": "decaf coffee",
        "context": {"time_of_day": "evening"},
        "evidence_text": "I prefer decaf coffee",
        "confidence": 0.8,
    }

    oversized_value = client.post(
        "/v1/preferences",
        json={**preference_payload, "value": "x" * (MAX_MEMORY_VALUE_LENGTH + 1)},
    )
    too_many_facets = client.post(
        "/v1/preferences",
        json={
            **preference_payload,
            "context": {f"facet-{index}": "value" for index in range(MAX_CONTEXT_FACETS + 1)},
        },
    )
    oversized_context_key = client.post(
        "/v1/preferences",
        json={
            **preference_payload,
            "context": {"k" * (MAX_CONTEXT_KEY_LENGTH + 1): "value"},
        },
    )
    oversized_context_value = client.post(
        "/v1/preferences",
        json={
            **preference_payload,
            "context": {"facet": "v" * (MAX_CONTEXT_VALUE_LENGTH + 1)},
        },
    )
    oversized_query = client.post(
        "/v1/recall",
        json={
            "tenant_id": "tenant-a",
            "subject_id": "alice",
            "query": "q" * (MAX_QUERY_LENGTH + 1),
        },
    )
    oversized_note = client.post(
        "/v1/outcomes",
        json={
            "tenant_id": "tenant-a",
            "subject_id": "alice",
            "trace_id": "00000000-0000-0000-0000-000000000001",
            "revision_id": "00000000-0000-0000-0000-000000000002",
            "kind": "helpful",
            "idempotency_key": "oversized-note",
            "note": "n" * (MAX_NOTE_LENGTH + 1),
        },
    )

    assert {
        oversized_value.status_code,
        too_many_facets.status_code,
        oversized_context_key.status_code,
        oversized_context_value.status_code,
        oversized_query.status_code,
        oversized_note.status_code,
    } == {422}


def test_health(harness: Harness) -> None:
    client = TestClient(create_app(harness.app, clock=harness.clock))
    assert client.get("/health").json() == {
        "status": "ok",
        "version": "0.1.0",
        "storage": "memory",
        "auth_mode": "development",
        "scope_source": "request",
    }
    assert client.get("/livez").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready", "storage": "memory"}


def test_service_discovery_and_openapi_explain_the_first_workflow(harness: Harness) -> None:
    client = TestClient(create_app(harness.app, clock=harness.clock))

    service = client.get("/")
    schema = client.get("/openapi.json").json()

    assert service.status_code == 200
    assert service.json()["frontend_url"] == "http://127.0.0.1:33009"
    assert service.json()["production_ready"] is False
    assert "/v1/preferences" in schema["paths"]
    assert schema["paths"]["/v1/recall"]["post"]["summary"] == "执行上下文记忆召回"
    bearer = schema["components"]["securitySchemes"]["OAuth2AccessToken"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    for path, method in (
        ("/v1/preferences", "post"),
        ("/v1/preferences", "get"),
        ("/v1/preferences/{record_id}/corrections", "post"),
        ("/v1/preferences/{record_id}/revisions", "get"),
        ("/v1/recall", "post"),
        ("/v1/outcomes", "post"),
    ):
        assert schema["paths"][path][method]["security"] == [{"OAuth2AccessToken": []}]
    outcome_schema = schema["components"]["schemas"]["OutcomeWriteRequest"]
    assert outcome_schema["example"]["kind"] == "helpful"
    correction_schema = schema["components"]["schemas"]["PreferenceCorrectionRequest"]
    assert "expected_revision_id" in correction_schema["properties"]
    recall_schema = schema["components"]["schemas"]["RecallRequest"]
    assert recall_schema["properties"]["valid_at"]["anyOf"][0]["format"] == "date-time"
    assert recall_schema["properties"]["known_at"]["anyOf"][0]["format"] == "date-time"
    recall_response = schema["components"]["schemas"]["RecallResponse"]
    assert {"valid_at", "known_at"} <= set(recall_response["required"])


def test_frontend_origin_is_allowed_by_cors(harness: Harness) -> None:
    client = TestClient(create_app(harness.app, clock=harness.clock))

    preflight = client.options(
        "/v1/recall",
        headers={
            "Origin": "http://127.0.0.1:33009",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-request-id",
        },
    )
    allowed = client.get(
        "/health",
        headers={"Origin": "http://127.0.0.1:33009"},
    )
    disallowed = client.get(
        "/health",
        headers={"Origin": "https://untrusted.example"},
    )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:33009"
    allowed_headers = preflight.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers
    assert "x-request-id" in allowed_headers
    assert allowed.headers["access-control-expose-headers"].lower() == "x-request-id"
    assert "access-control-allow-origin" not in disallowed.headers


def test_request_ids_security_headers_and_metadata_only_access_logs(
    harness: Harness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(create_app(harness.app, clock=harness.clock))
    request_id = "caller.request-42"
    secret_evidence = "raw-evidence-must-not-appear-in-logs"

    with caplog.at_level(logging.INFO, logger="evolvable_memory.access"):
        response = client.post(
            "/v1/preferences?query-secret=must-not-appear",
            headers={"X-Request-ID": request_id},
            json={
                "tenant_id": "tenant-a",
                "subject_id": "alice",
                "source": "conversation",
                "idempotency_key": "request-metadata-test",
                "key": "logging.preference",
                "value": "quiet",
                "context": {},
                "evidence_text": secret_evidence,
                "confidence": 0.8,
            },
        )

    assert response.status_code == 201
    assert response.headers["x-request-id"] == request_id
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"

    access_records = [
        record for record in caplog.records if record.name == "evolvable_memory.access"
    ]
    assert access_records
    access_log = json.loads(access_records[-1].getMessage())
    assert access_log["event"] == "http_request"
    assert access_log["request_id"] == request_id
    assert access_log["method"] == "POST"
    assert access_log["route"] == "/v1/preferences"
    assert access_log["status_code"] == 201
    assert "query" not in access_log
    assert secret_evidence not in access_records[-1].getMessage()
    assert "query-secret" not in access_records[-1].getMessage()

    generated = client.get("/health", headers={"X-Request-ID": "not valid spaces"})
    generated_id = generated.headers["x-request-id"]
    assert generated_id != "not valid spaces"
    assert len(generated_id) == 32
    assert generated_id.isalnum()


def test_request_body_limit_checks_content_length_and_streamed_bytes(
    harness: Harness,
) -> None:
    settings = Settings(max_request_body_bytes=128)
    client = TestClient(create_app(harness.app, settings=settings, clock=harness.clock))

    normal = client.post(
        "/v1/recall",
        headers={"X-Request-ID": "normal-body"},
        json={"tenant_id": "t", "subject_id": "s", "query": "q"},
    )
    declared_too_large = client.post(
        "/v1/recall",
        headers={
            "Content-Length": "129",
            "Content-Type": "application/json",
            "X-Request-ID": "declared-too-large",
        },
        content=b"{}",
    )

    def oversized_chunks() -> Iterator[bytes]:
        yield b'{"tenant_id":"t","subject_id":"s","query":"'
        yield b"x" * 100
        yield b'"}'

    streamed_too_large = client.post(
        "/v1/recall",
        headers={
            "Content-Type": "application/json",
            "X-Request-ID": "streamed-too-large",
        },
        content=oversized_chunks(),
    )

    assert normal.status_code == 200
    for response, request_id in (
        (declared_too_large, "declared-too-large"),
        (streamed_too_large, "streamed-too-large"),
    ):
        assert response.status_code == 413
        assert response.headers["x-request-id"] == request_id
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.json() == {
            "error": "RequestBodyTooLargeError",
            "detail": "Request body exceeds the configured limit of 128 bytes.",
            "request_id": request_id,
        }


def test_unhandled_errors_return_safe_correlated_responses_and_logs(
    harness: Harness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = create_app(harness.app, clock=harness.clock)
    leaked_detail = "raw-evidence-must-never-escape"

    @application.get("/_test/unhandled")
    def unhandled() -> None:
        raise RuntimeError(leaked_detail)

    client = TestClient(application)
    with caplog.at_level(logging.INFO):
        response = client.get(
            "/_test/unhandled?private-query=do-not-log",
            headers={"X-Request-ID": "failed-request"},
        )

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "failed-request"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.json() == {
        "error": "InternalServerError",
        "detail": "An unexpected server error occurred.",
        "request_id": "failed-request",
    }
    runtime_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("evolvable_memory.")
    )
    assert leaked_detail not in runtime_logs
    assert "private-query" not in runtime_logs
    assert '"exception_type":"RuntimeError"' in runtime_logs
