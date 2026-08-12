"""Request-context middleware: headers/cookies -> the core context store + setters."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from starlette.datastructures import Headers

from ....core.context import (
    bind_context_values,
    current_context,
    get_context_value,
    get_context_var,
    is_safe_context_id,
    set_context_value,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

    from .protocols import ContextExtractor, ContextSetter

logger = logging.getLogger(__name__)


def get_context() -> dict[str, Any]:
    """Get all non-``None`` values from the request context as a dictionary."""
    return current_context()


class ContextMiddleware:
    """Middleware for managing request context using contextvars.

    Extracts values from headers, cookies and custom extractors, stores them in
    the transport-neutral :mod:`servicewright.core.context` store (available
    throughout the async task), and pushes them into pluggable
    :class:`ContextSetter`s (logging contextvars, tracing baggage, ...).

    It also **echoes the request id back to the client**, so exactly one
    identifier exists per request: the one in the logs, the one propagated
    downstream, and the one the caller can quote in a bug report.
    """

    def __init__(
        self,
        app: ASGIApp,
        header_extractors: dict[str, str] | None = None,
        cookie_extractors: dict[str, str] | None = None,
        custom_extractors: list[ContextExtractor] | None = None,
        auto_generate_request_id: bool = True,
        request_id_header: str = "x-request-id",
        request_id_context_key: str = "request_id",
        context_setters: list[ContextSetter] | None = None,
        validate_ids: bool = True,
        echo_request_id: bool = True,
    ) -> None:
        self.app = app
        self.echo_request_id = echo_request_id
        self.header_extractors = header_extractors or {
            "x-request-id": "request_id",
            "x-user-id": "user_id",
            "x-tenant-id": "tenant_id",
            "x-trace-id": "trace_id",
        }
        self.cookie_extractors = cookie_extractors or {}
        self.custom_extractors = custom_extractors or []
        self.auto_generate_request_id = auto_generate_request_id
        self.request_id_header = request_id_header.lower()
        self.request_id_context_key = request_id_context_key
        self.context_setters = context_setters or []
        # Header/cookie values are client input bound into logs and outbound
        # propagation — drop log-unsafe or overlong ones (a dropped request id
        # is regenerated). Custom extractors are developer-owned: not filtered.
        self.validate_ids = validate_ids

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        context_data: dict[str, Any] = {}

        # 1. Extract from headers.
        for header, ctx_key in self.header_extractors.items():
            if (value := headers.get(header.lower())) and self._accepts(value):
                context_data[ctx_key] = value

        # 2. Extract from cookies (if any).
        if self.cookie_extractors:
            from starlette.requests import Request

            request = Request(scope)
            for cookie, ctx_key in self.cookie_extractors.items():
                if (value := request.cookies.get(cookie)) and self._accepts(value):
                    context_data[ctx_key] = value

        # 3. Custom extractors.
        for extractor in self.custom_extractors:
            try:
                extracted = extractor(scope, headers)
                if extracted:
                    context_data.update(extracted)
            except Exception:  # noqa: S110 - middleware must not crash the request
                pass

        # 4. Auto-generate the request ID if missing (or dropped as unsafe).
        if self.auto_generate_request_id and self.request_id_context_key not in context_data:
            context_data[self.request_id_context_key] = str(uuid.uuid4())

        await self._run(scope, receive, send, context_data)

    def _accepts(self, value: str) -> bool:
        return not self.validate_ids or is_safe_context_id(value)

    def _echoing_send(self, send: Send, request_id: str | None) -> Send:
        """Wrap ``send`` so the response carries the request id back."""
        if not self.echo_request_id or not request_id:
            return send

        header = (self.request_id_header.encode("latin-1"), request_id.encode("latin-1"))

        async def send_with_request_id(message: Any) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], header]
            await send(message)

        return send_with_request_id

    async def _run(self, scope: Scope, receive: Receive, send: Send, context_data: dict[str, Any]) -> None:
        """Serve the request with ``context_data`` bound into the store and setters."""
        send = self._echoing_send(send, context_data.get(self.request_id_context_key))

        # Bind into the core context store (one remover resets everything).
        store_remover = bind_context_values(context_data)

        # Call external setters and store cleanup callables.
        removers: list[Callable[[], None]] = []
        for ctx_setter in self.context_setters:
            try:
                remover = ctx_setter.set(context_data)
                if remover is not None:
                    removers.append(remover)
            except Exception:
                logger.exception("Failed to call context setter")

        try:
            await self.app(scope, receive, send)
        finally:
            # Call external cleanups in reverse order.
            for remover in reversed(removers):
                try:
                    remover()
                except Exception:
                    logger.exception("Failed to call context cleanup remover")

            store_remover()


__all__ = [
    "ContextMiddleware",
    "get_context",
    "get_context_value",
    "get_context_var",
    "set_context_value",
]
