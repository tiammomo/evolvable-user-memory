from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from pydantic import ValidationError

from conftest import Harness
from evolvable_memory.api.app import create_app
from evolvable_memory.api.schemas import (
    RecallContextProjectionRequest,
    RecallContextProjectionResponse,
)
from evolvable_memory.api.security import DevelopmentIdentityResolver
from evolvable_memory.application.commands import RecallMemory, RememberPreference
from evolvable_memory.domain.common import ContextSignature, NotFoundError, Scope

ALICE = Scope("tenant-a", "alice")
EVENING = ContextSignature.from_mapping({"time_of_day": "evening"})


def _projection_endpoint(harness: Harness) -> Callable[..., object]:
    application = create_app(harness.app, clock=harness.clock)
    route = next(
        route
        for route in application.routes
        if isinstance(route, APIRoute) and route.path == "/v1/recall-contexts"
    )
    return route.endpoint


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/recall-contexts",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
        }
    )
    request.state.request_id = "compression-api-test"
    return request


def _trace(harness: Harness) -> tuple[str, str]:
    memory = harness.app.remember_preference(
        RememberPreference(
            scope=ALICE,
            source="conversation",
            idempotency_key="api-compression:write",
            key="drink.preference",
            value="decaf coffee",
            context=EVENING,
            evidence_text="I prefer decaf coffee",
            confidence=0.9,
            occurred_at=harness.clock.current,
        )
    )
    trace = harness.app.recall(RecallMemory(scope=ALICE, query="drink preference", context=EVENING))
    return str(memory.revision_id), str(trace.id)


def test_api_route_builds_deterministic_attributable_recall_context(harness: Harness) -> None:
    revision_id, trace_id = _trace(harness)
    endpoint = _projection_endpoint(harness)
    payload = RecallContextProjectionRequest(
        tenant_id="tenant-a",
        subject_id="alice",
        trace_id=trace_id,
        algorithm="ranked-extractive-v1",
        max_characters=2_000,
    )
    actor = DevelopmentIdentityResolver().authenticate(None)

    first = endpoint(payload=payload, http_request=_request(), actor=actor)
    replay = endpoint(payload=payload, http_request=_request(), actor=actor)

    assert isinstance(first, RecallContextProjectionResponse)
    assert replay == first
    body = first.model_dump(mode="json")
    assert body["trace_id"] == trace_id
    assert body["source_revision_ids"] == [revision_id]
    assert body["segments"][0]["sources"][0]["revision_id"] == revision_id
    assert json.loads(body["content"])["memories"][0]["value"] == "decaf coffee"
    assert body["included_item_count"] == 1
    assert body["omitted_item_count"] == 0
    assert body["projected_character_count"] <= body["max_characters"]
    assert all(
        len(body[field]) == 64
        for field in (
            "configuration_sha256",
            "source_sha256",
            "projection_sha256",
        )
    )


def test_api_route_fails_closed_for_scope_and_schema_input(harness: Harness) -> None:
    _, trace_id = _trace(harness)
    endpoint = _projection_endpoint(harness)
    actor = DevelopmentIdentityResolver().authenticate(None)
    cross_scope = RecallContextProjectionRequest(
        tenant_id="tenant-a",
        subject_id="bob",
        trace_id=trace_id,
    )

    with pytest.raises(NotFoundError, match="trace not found"):
        endpoint(payload=cross_scope, http_request=_request(), actor=actor)
    with pytest.raises(ValidationError):
        RecallContextProjectionRequest(
            tenant_id="tenant-a",
            subject_id="alice",
            trace_id=trace_id,
            max_characters=63,
        )
    with pytest.raises(ValidationError):
        RecallContextProjectionRequest(
            tenant_id="tenant-a",
            subject_id="alice",
            trace_id=trace_id,
            algorithm="llm-summary-v1",
        )


def test_openapi_exposes_compression_algorithms_budget_and_authorization(harness: Harness) -> None:
    schema = create_app(harness.app, clock=harness.clock).openapi()
    operation = schema["paths"]["/v1/recall-contexts"]["post"]
    request_schema = schema["components"]["schemas"]["RecallContextProjectionRequest"]
    algorithm_schema = schema["components"]["schemas"]["ContextCompressionAlgorithm"]

    assert operation["security"] == [{"OAuth2AccessToken": []}]
    assert operation["summary"] == "压缩一次召回的可归因上下文"
    assert request_schema["properties"]["max_characters"]["minimum"] == 64
    assert request_schema["properties"]["max_characters"]["maximum"] == 100_000
    assert set(algorithm_schema["enum"]) == {
        "ranked-extractive-v1",
        "exact-deduplicated-v1",
    }
