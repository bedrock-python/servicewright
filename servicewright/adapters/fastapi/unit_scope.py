"""Per-request ``UnitScope`` for the FastAPI entrypoint (DI-agnostic).

Mirrors the gRPC ``UnitScopeInterceptor``: per request it opens
``container.unit_scope(context={"request": request})`` and exposes the scope
three ways so handlers can reach it however they prefer:

- on ``request.state.unit_scope`` (Starlette state),
- via a :class:`contextvars.ContextVar` read by :func:`current_unit_scope`,
- via the FastAPI dependency :func:`get_unit_scope` (alias :data:`UnitScopeDep`).

This is the ONLY per-request scope mechanism the adapter installs; nothing else
in it opens a unit scope. When the framework's own DI integration owns the
request scope instead (dishka's ``setup_dishka``), switch it off with
``MiddlewareConfig(unit_scope=False)`` so the two never open two scopes per
request.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from ...core.contracts import DependencyContainerProtocol, UnitScopeProtocol

# The per-request unit scope, set by UnitScopeMiddleware and read by handlers.
_current_unit_scope: contextvars.ContextVar[UnitScopeProtocol] = contextvars.ContextVar("servicewright_http_unit_scope")


def current_unit_scope() -> UnitScopeProtocol:
    """Return the :class:`UnitScopeProtocol` for the in-flight HTTP request.

    Raises:
        LookupError: If called outside a request handled by
            :class:`UnitScopeMiddleware`.
    """
    try:
        return _current_unit_scope.get()
    except LookupError as exc:
        raise LookupError(
            "No active HTTP unit scope; current_unit_scope() must be called inside a request "
            "served by a FastApiEntrypoint with UnitScopeMiddleware installed "
            "(MiddlewareConfig.unit_scope=True, the default)."
        ) from exc


def get_unit_scope(request: Request) -> UnitScopeProtocol:
    """FastAPI dependency returning the per-request unit scope.

    Resolves from ``request.state`` (set by :class:`UnitScopeMiddleware`).

    Example:
        >>> from typing import Annotated
        >>> from fastapi import Depends
        >>> async def handler(scope: Annotated[UnitScopeProtocol, Depends(get_unit_scope)]):
        ...     use_case = await scope.get(MyUseCase)
    """
    scope: UnitScopeProtocol | None = getattr(request.state, "unit_scope", None)
    if scope is None:
        raise LookupError(
            "No active HTTP unit scope on request.state; the FastApiEntrypoint UnitScopeMiddleware "
            "must be installed (MiddlewareConfig.unit_scope=True, the default) for get_unit_scope() to resolve."
        )
    return scope


# Annotated dependency alias for ergonomic handler signatures.
UnitScopeDep = Annotated["UnitScopeProtocol", Depends(get_unit_scope)]


class UnitScopeMiddleware:
    """Open one ``UnitScope`` per request and expose it three ways.

    Installed by the :class:`FastApiEntrypoint` as the outermost wrapper around
    the handler unless ``MiddlewareConfig.unit_scope`` is off. The scope
    carries the ``Request`` as its ``context`` and stays open until the
    response is fully delivered.

    This is deliberately a raw ASGI middleware rather than a
    ``BaseHTTPMiddleware``: the latter hands control back as soon as the
    response *starts*, so the scope — and with it every REQUEST-scoped
    dependency, e.g. a database session — would be finalized while a streaming
    body is still being produced and before ``BackgroundTask``s run, truncating
    responses that the client already received a 200 for. Awaiting the inner app
    to completion keeps the scope alive for the whole exchange, which is also
    what the Litestar adapter does.
    """

    def __init__(self, app: ASGIApp, container: DependencyContainerProtocol) -> None:
        self._app = app
        self._container = container

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Wrap the request in a fresh unit scope bound to request.state + a contextvar."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope)
        async with self._container.unit_scope({"request": request}) as unit_scope:
            request.state.unit_scope = unit_scope
            token = _current_unit_scope.set(unit_scope)
            try:
                await self._app(scope, receive, send)
            finally:
                _current_unit_scope.reset(token)


__all__ = [
    "UnitScopeDep",
    "UnitScopeMiddleware",
    "current_unit_scope",
    "get_unit_scope",
]
