"""Correlated last-resort handling for unhandled exceptions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ....core.errors import (
    INTERNAL_ERROR_CODE,
    ErrorInfo,
    ErrorKind,
    ProblemDetailsRenderer,
    mask_private_error,
)
from .._imports import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from ....core.errors import HttpErrorRendererProtocol

logger = logging.getLogger(__name__)

_MASKED_INTERNAL = ErrorInfo(kind=ErrorKind.INTERNAL, code=INTERNAL_ERROR_CODE)


class UnhandledErrorMiddleware:
    """Render unhandled exceptions from inside the request-context layer.

    Starlette's own ``ServerErrorMiddleware`` sits *outside* every user
    middleware, so a 500 produced there has already left the context layer: the
    response carries none of its headers and the traceback is logged without the
    request id — leaving the one status class that must be correlatable as the
    only one that is not. Handling the exception here, inside the context
    middleware, keeps the request id on both the log record and the response.

    Args:
        app: The wrapped ASGI application.
        renderer: Wire-format renderer; defaults to RFC 9457 problem details.
    """

    def __init__(self, app: ASGIApp, *, renderer: HttpErrorRendererProtocol | None = None) -> None:
        self.app = app
        self._renderer: HttpErrorRendererProtocol = renderer if renderer is not None else ProblemDetailsRenderer()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("Unhandled exception while processing request")
            if response_started:
                # The status line is already on the wire; the connection has to
                # break rather than mix two responses.
                raise
            await self._masked_response()(scope, receive, send)

    def _masked_response(self) -> Any:
        rendered = self._renderer.render(mask_private_error(_MASKED_INTERNAL))
        return JSONResponse(
            status_code=rendered.status_code,
            content=rendered.body,
            media_type=rendered.media_type,
            headers=dict(rendered.headers) if rendered.headers else None,
        )


__all__ = ["UnhandledErrorMiddleware"]
