from __future__ import annotations

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket
from socketserver import BaseServer
from typing import cast
from urllib.parse import urlsplit

from evolvable_memory.config import FrontendSettings

_STATIC_DIRECTORY = Path(__file__).parent / "api" / "static"


class FrontendRequestHandler(SimpleHTTPRequestHandler):
    def __init__(
        self,
        request: socket,
        client_address: tuple[str, int],
        server: BaseServer,
        *,
        directory: str | None = None,
        public_api_url: str = "http://127.0.0.1:38089",
    ) -> None:
        self.public_api_url = public_api_url.rstrip("/")
        parsed_api_url = urlsplit(self.public_api_url)
        self.public_api_origin = f"{parsed_api_url.scheme}://{parsed_api_url.netloc}"
        super().__init__(request, client_address, server, directory=directory)

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/runtime-config.js":
            self._serve_runtime_config()
            return
        super().do_GET()

    def _serve_runtime_config(self) -> None:
        configuration = json.dumps(
            {"apiBaseUrl": self.public_api_url},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        content = f"globalThis.EMF_RUNTIME_CONFIG = {configuration};\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; "
            f"connect-src 'self' {self.public_api_origin}; "
            "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
            "img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'",
        )
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Log response metadata without a caller-controlled path or query string."""
        self.log_message(
            "frontend_request method=%s status=%s size=%s",
            self.command,
            code,
            size,
        )


def run() -> None:
    settings = FrontendSettings.from_environment()
    handler = cast(
        type[FrontendRequestHandler],
        partial(
            FrontendRequestHandler,
            directory=str(_STATIC_DIRECTORY),
            public_api_url=settings.public_api_url,
        ),
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
