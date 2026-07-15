from __future__ import annotations

import json
import logging
import re
from time import perf_counter
from typing import Final
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_REQUEST_ID_HEADER: Final = "X-Request-ID"
_REQUEST_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_SECURITY_HEADERS: Final = {
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Permitted-Cross-Domain-Policies": "none",
}


class RequestBodyTooLargeError(HTTPException):
    """The request stream exceeded the configured byte limit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(status_code=413, detail=_too_large_detail(limit))


class ApiRuntimeMiddleware:
    """Bound request bodies, correlate requests, and emit metadata-only access logs."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_request_body_bytes: int,
    ) -> None:
        if max_request_body_bytes < 1:
            raise ValueError("max_request_body_bytes must be positive")
        self._app = app
        self._max_request_body_bytes = max_request_body_bytes
        self._access_logger = logging.getLogger("evolvable_memory.access")
        self._error_logger = logging.getLogger("evolvable_memory.error")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        started_at = perf_counter()
        request_id = _request_id(Headers(scope=scope).get(_REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id
        status_code = 500
        received_bytes = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_request_body_bytes:
                    raise RequestBodyTooLargeError(self._max_request_body_bytes)
            return message

        async def send_with_runtime_headers(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers[_REQUEST_ID_HEADER] = request_id
                for name, value in _SECURITY_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        try:
            content_length = _content_length(Headers(scope=scope))
            if content_length is not None and content_length > self._max_request_body_bytes:
                status_code = 413
                await self._too_large_response(request_id)(
                    scope,
                    receive,
                    send_with_runtime_headers,
                )
                return
            await self._app(scope, limited_receive, send_with_runtime_headers)
        except RequestBodyTooLargeError:
            if response_started:
                raise
            status_code = 413
            await self._too_large_response(request_id)(
                scope,
                receive,
                send_with_runtime_headers,
            )
        except Exception as exc:
            if response_started:
                raise
            status_code = 500
            self._error_logger.error(
                _structured_log(
                    event="unhandled_exception",
                    request_id=request_id,
                    exception_type=type(exc).__name__,
                )
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "error": "InternalServerError",
                    "detail": "An unexpected server error occurred.",
                    "request_id": request_id,
                },
            )
            await response(scope, receive, send_with_runtime_headers)
        finally:
            duration_ms = round((perf_counter() - started_at) * 1_000, 3)
            self._access_logger.info(
                _structured_log(
                    event="http_request",
                    request_id=request_id,
                    method=str(scope.get("method", "")),
                    route=_route_template(scope),
                    status_code=status_code,
                    duration_ms=duration_ms,
                    request_bytes=received_bytes,
                )
            )

    def _too_large_response(self, request_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={
                "error": "RequestBodyTooLargeError",
                "detail": _too_large_detail(self._max_request_body_bytes),
                "request_id": request_id,
            },
        )


def _request_id(candidate: str | None) -> str:
    if candidate is not None and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid4().hex


def _too_large_detail(limit: int) -> str:
    return f"Request body exceeds the configured limit of {limit} bytes."


def _content_length(headers: Headers) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    template = getattr(route, "path", None)
    return template if isinstance(template, str) else "<unmatched>"


def _structured_log(**values: object) -> str:
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
