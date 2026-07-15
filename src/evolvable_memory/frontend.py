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
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; "
            "connect-src 'self' http://127.0.0.1:* http://localhost:*; "
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
