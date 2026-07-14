from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from evolvable_memory.config import FrontendSettings

_STATIC_DIRECTORY = Path(__file__).parent / "api" / "static"


class FrontendRequestHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def run() -> None:
    settings = FrontendSettings.from_environment()
    handler = cast(
        type[FrontendRequestHandler],
        partial(FrontendRequestHandler, directory=str(_STATIC_DIRECTORY)),
    )
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    print(f"Frontend running on http://{settings.host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
