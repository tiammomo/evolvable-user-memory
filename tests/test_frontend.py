from __future__ import annotations

from functools import partial
from threading import Thread

import httpx
import pytest

from evolvable_memory import frontend


def test_frontend_server_serves_console_without_cache(
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = partial(
        frontend.FrontendRequestHandler,
        directory=str(frontend._STATIC_DIRECTORY),
    )
    server = frontend.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.handle_request)
    thread.start()
    try:
        host, port = server.server_address
        response = httpx.get(
            f"http://{host}:{port}/?raw-evidence=must-not-appear",
            timeout=2,
        )
    finally:
        thread.join(timeout=2)
        server.server_close()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    frontend_log = capsys.readouterr().err
    assert "frontend_request method=GET status=200" in frontend_log
    assert "raw-evidence" not in frontend_log
    assert "Evolvable Memory · 记忆工作台" in response.text
    assert 'id="memory-form"' in response.text
    assert 'id="view-memories"' in response.text
    assert 'id="recall-form"' in response.text
    assert 'id="start-example"' in response.text
    assert 'id="open-onboarding"' in response.text
    assert 'id="onboarding-dialog"' in response.text
    assert 'id="retry-health"' in response.text
    assert 'id="history-modal" aria-labelledby="history-title"' in response.text
    assert 'id="correction-modal" aria-labelledby="correction-title"' in response.text
    assert 'id="journey-view"' in response.text
    assert response.text.count('class="journey-number"') == 5
    assert 'id="recall-guidance"' in response.text
    assert 'class="panel storage-card"' in response.text
    assert 'id="storage-title"' in response.text
    assert 'name="expected_revision_id"' in response.text
    assert 'href="#main-content"' in response.text
    assert 'id="main-content" tabindex="-1"' in response.text
    assert 'data-view="overview" aria-current="page"' in response.text

    styles = (frontend._STATIC_DIRECTORY / "styles.css").read_text(encoding="utf-8")
    assert ".journey-number {" in styles
    assert ".journey-step > span {" not in styles

    script = (frontend._STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")
    assert "updateStorageDisplay(health.storage)" in script
    assert "form.elements.expected_revision_id.value = item.revision_id" in script
    assert "--muted: #5f6e67;" in styles
    assert ".status-retry {" in styles


def test_frontend_guards_scope_changes_and_idempotent_retries() -> None:
    script = (frontend._STATIC_DIRECTORY / "app.js").read_text(encoding="utf-8")

    assert "scopeGeneration" in script
    assert "cancelScopedRequests()" in script
    assert "new AbortController()" in script
    assert "generation !== state.scopeGeneration" in script
    assert "idempotencyKeyFor" in script
    assert 'idempotencyKeyFor("preference"' in script
    assert 'idempotencyKeyFor(operation, "web:outcome"' in script
    assert 'idempotencyKeyFor(operation, "web:correction"' in script
    assert "HEALTH_TIMEOUT_MS = 3500" in script
    assert '$("#retry-health").addEventListener("click", checkHealth)' in script


def test_frontend_entrypoint_uses_configured_address(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []

    class FakeServer:
        def __init__(self, address: tuple[str, int], _handler: object) -> None:
            events.append(address)

        def serve_forever(self) -> None:
            events.append("served")
            raise KeyboardInterrupt

        def server_close(self) -> None:
            events.append("closed")

    monkeypatch.setenv("EMF_FRONTEND_HOST", "127.0.0.2")
    monkeypatch.setenv("EMF_FRONTEND_PORT", "33010")
    monkeypatch.setattr(frontend, "ThreadingHTTPServer", FakeServer)

    frontend.run()

    assert events == [("127.0.0.2", 33010), "served", "closed"]
