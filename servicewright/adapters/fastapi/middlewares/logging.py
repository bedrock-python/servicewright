"""Structured request/response logging middleware.

Emits through the standard library logger so the configured logging sink owns
the destination, level and format of these lines exactly as it owns every other
log line in the service.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from starlette.datastructures import Headers

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

# A stdlib logger on purpose: this is the channel every logging sink configures
# (level, format, handler/stream, redaction). A module-level structlog logger
# would bypass all of it — printing to stdout in console format, unfiltered by
# ``settings.logging.level``, and still emitting when the logging concern is
# switched off entirely.
logger = logging.getLogger(__name__)

DEFAULT_IP_HEADERS = ("X-Forwarded-For-Y", "X-Forwarded-For", "X-Real-IP")


class LoggingMiddleware:
    """Logs the start and completion of each request with status and duration."""

    def __init__(
        self,
        app: ASGIApp,
        ignored_paths: Sequence[str] | None = None,
        ip_headers: Sequence[str] | None = DEFAULT_IP_HEADERS,
    ) -> None:
        self.app = app
        self._ip_headers = ip_headers
        self._compiled_patterns: list[re.Pattern[str]] = []
        self._ignored_paths: list[str] = []

        if ignored_paths:
            for path in ignored_paths:
                try:
                    self._compiled_patterns.append(re.compile(path))
                except re.error:
                    self._ignored_paths.append(path)

    def _should_ignore_path(self, path: str) -> bool:
        return path in self._ignored_paths or any(pattern.search(path) for pattern in self._compiled_patterns)

    def _get_client_ip(self, scope: Scope, headers: Headers) -> str:
        """Attempt to get the real client IP from headers or the connection."""
        if self._ip_headers:
            for header in self._ip_headers:
                if ip := headers.get(header):
                    # X-Forwarded-For can contain multiple IPs; take the first one.
                    return str(ip).split(",")[0].strip()

        client = scope.get("client")
        return client[0] if client else "unknown"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if self._should_ignore_path(path):
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        query = scope["query_string"].decode("utf-8")
        headers = Headers(scope=scope)
        ip = self._get_client_ip(scope, headers)

        logger.info(
            "Request started",
            extra={"method": method, "path": path, "query": query, "ip": ip},
        )

        start_time = time.perf_counter_ns()
        status_code = 500  # Default to 500 in case of an uncaught exception.

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            process_time_ms = (time.perf_counter_ns() - start_time) / 1_000_000
            logger.info(
                "Request finished",
                extra={
                    "method": method,
                    "path": path,
                    "status_code": status_code,
                    "duration_ms": round(process_time_ms, 2),
                    "ip": ip,
                },
            )


__all__ = ["DEFAULT_IP_HEADERS", "LoggingMiddleware"]
