from __future__ import annotations

from functools import partial
from threading import Thread

import httpx
import pytest

from evolvable_memory import frontend


def test_frontend_server_serves_console_without_cache() -> None:
    handler = partial(
        frontend.FrontendRequestHandler,
        directory=str(frontend._STATIC_DIRECTORY),
    )
    server = frontend.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.handle_request)
    thread.start()
    try:
        host, port = server.server_address
        response = httpx.get(f"http://{host}:{port}/", timeout=2)
    finally:
        thread.join(timeout=2)
        server.server_close()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "Evolvable Memory · 记忆工作台" in response.text
    assert 'id="memory-form"' in response.text
    assert 'id="view-memories"' in response.text
    assert 'id="recall-form"' in response.text
    assert 'id="start-example"' in response.text


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
