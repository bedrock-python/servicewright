"""Sentry scope enrichment middleware (soft-capability: no-op without sentry-sdk)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

try:
    import sentry_sdk

    HAS_SENTRY = True
except ImportError:  # pragma: no cover - exercised only without sentry-sdk
    HAS_SENTRY = False


class SentryMiddleware:
    """Enriches the Sentry scope with data from the request context.

    Works best when placed after :class:`ContextMiddleware`: it takes values
    from contextvars and applies them as Sentry tags or user info.
    """

    def __init__(
        self,
        app: ASGIApp,
        context_keys: list[str] | None = None,
        tag_mapping: dict[str, str] | None = None,
        add_request_data: bool = True,
        add_user_data: bool = True,
        user_id_key: str = "user_id",
        custom_processor: Callable[[Any, Scope], None] | None = None,
    ) -> None:
        self.app = app
        self.context_keys = context_keys or ["request_id", "tenant_id", "user_id"]
        self.tag_mapping = tag_mapping or {}
        self.add_request_data = add_request_data
        self.add_user_data = add_user_data
        self.user_id_key = user_id_key
        self.custom_processor = custom_processor

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not HAS_SENTRY or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        from .context import get_context

        context = get_context()
        sentry_scope = sentry_sdk.get_isolation_scope()

        # 1. Add request tags from the context.
        for key in self.context_keys:
            if value := context.get(key):
                tag_name = self.tag_mapping.get(key, key)
                sentry_scope.set_tag(tag_name, value)

        # 2. Add user data.
        if self.add_user_data and (user_id := context.get(self.user_id_key)):
            user_info = {"id": str(user_id)}
            if email := context.get("user_email"):
                user_info["email"] = str(email)
            if username := context.get("username"):
                user_info["username"] = str(username)
            sentry_scope.set_user(user_info)

        # 3. Add request data (extra context).
        if self.add_request_data:
            sentry_scope.set_context(
                "request_info",
                {
                    "path": scope.get("path"),
                    "method": scope.get("method"),
                    "query_string": scope.get("query_string", b"").decode("utf-8"),
                },
            )

        # 4. Custom processing.
        if self.custom_processor:
            try:
                self.custom_processor(sentry_scope, scope)
            except Exception:
                logger.exception("Error in SentryMiddleware custom_processor")

        await self.app(scope, receive, send)


__all__ = ["SentryMiddleware"]
