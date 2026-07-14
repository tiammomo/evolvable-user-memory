"""Run the first complete memory workflow against the local development API."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

BASE_URL = "http://127.0.0.1:38089"
SCOPE = {"tenant_id": "demo", "subject_id": "alice"}


def request_json(
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> dict[str, Any] | list[dict[str, Any]]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers={"content-type": "application/json"} if data is not None else {},
    )
    try:
        with urlopen(request, timeout=5) as response:
            result: object = json.loads(response.read())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"API returned HTTP {exc.code}: {detail}") from exc
    if isinstance(result, dict | list):
        return result
    raise RuntimeError("API returned an unexpected JSON value")


def main() -> None:
    run_id = uuid4().hex[:10]
    print("1/5 Recording an evidence-grounded preference...")
    created = request_json(
        "POST",
        "/v1/preferences",
        {
            **SCOPE,
            "source": "example-client",
            "idempotency_key": f"example:{run_id}:preference",
            "key": "drink.preference",
            "value": "decaf coffee",
            "context": {"time_of_day": "evening"},
            "evidence_text": "晚上我只喝低因咖啡",
            "confidence": 0.92,
        },
    )
    assert isinstance(created, dict)
    print(f"    record={created['record_id']} revision={created['revision_id']}")

    print("2/5 Listing the current belief without mutating it...")
    query = urlencode(SCOPE)
    memories = request_json("GET", f"/v1/preferences?{query}")
    assert isinstance(memories, list)
    print(f"    active preferences in scope: {len(memories)}")

    print("3/5 Recalling in the evening context...")
    recalled = request_json(
        "POST",
        "/v1/recall",
        {
            **SCOPE,
            "query": "晚上应该准备什么饮料",
            "context": {"time_of_day": "evening"},
            "limit": 5,
        },
    )
    assert isinstance(recalled, dict)
    items = recalled["items"]
    if not isinstance(items, list) or not items:
        raise RuntimeError("the example preference was not recalled")
    first = items[0]
    print(f"    trace={recalled['trace_id']} top result={first['value']}")

    print("4/5 Attributing a helpful outcome to that exact trace...")
    outcome = request_json(
        "POST",
        "/v1/outcomes",
        {
            **SCOPE,
            "trace_id": recalled["trace_id"],
            "revision_id": first["revision_id"],
            "kind": "helpful",
            "idempotency_key": f"example:{run_id}:outcome",
            "note": "local getting-started example",
        },
    )
    assert isinstance(outcome, dict)
    utility = outcome["utility"]
    print(f"    contextual utility mean={utility['mean']:.3f}")

    print("5/5 Appending a correction and reading immutable history...")
    corrected = request_json(
        "POST",
        f"/v1/preferences/{created['record_id']}/corrections",
        {
            **SCOPE,
            "source": "example-client",
            "idempotency_key": f"example:{run_id}:correction",
            "value": "herbal tea",
            "evidence_text": "其实晚上我改喝花草茶",
            "reason": "getting-started example correction",
        },
    )
    assert isinstance(corrected, dict)
    history = request_json(
        "GET",
        f"/v1/preferences/{created['record_id']}/revisions?{query}",
    )
    assert isinstance(history, list)
    history_values = [item["value"] for item in history]
    print(f"    current revision=#{corrected['sequence']} history values={history_values}")
    print("Done. Open http://127.0.0.1:33009 to inspect the result.")


if __name__ == "__main__":
    main()
