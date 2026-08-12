"""Per-request ``UnitScope`` for the Litestar entrypoint (DI-agnostic).

Mirrors the FastAPI ``UnitScopeMiddleware`` / gRPC ``UnitScopeInterceptor``: per
request it opens ``container.unit_scope(context={"request": request})`` and
exposes the scope two ways so handlers can reach it however they prefer:

- via a :class:`contextvars.ContextVar` read by :func:`current_unit_scope`,
- via the Litestar dependency :func:`get_unit_scope` (provide it through
  ``dependencies={"unit_scope": Provide(get_unit_scope)}`` on a handler/router/app).

This is the ONLY per-request scope mechanism; nothing else opens a unit scope.
A dishka-native ``FromDishka`` integration is a separate, later enhancement.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

from ...core.contracts import DependencyContainerProtocol, UnitScopeProtocol
from ._imports import ASGIMiddleware, Request, ScopeType

if TYPE_CHECKING:
    from ._imports import ASGIApp, Receive, Scope, Send

# Key under which the unit scope is stashed in the ASGI connection scope state.
_STATE_KEY = "servicewright_unit_scope"

# The per-request unit scope, set by UnitScopeMiddleware and read by handlers.
_current_unit_scope: contextvars.ContextVar[UnitScopeProtocol] = contextvars.ContextVar(
    "servicewright_litestar_unit_scope"
)


def current_unit_scope() -> UnitScopeProtocol:
    """Return the :class:`UnitScopeProtocol` for the in-flight Litestar request.

    Raises:
        LookupError: If called outside a request handled by
            :class:`UnitScopeMiddleware`.
    """
    try:
        return _current_unit_scope.get()
    except LookupError as exc:
        raise LookupError(
            "No active Litestar unit scope; current_unit_scope() must be called inside a request "
            "served by a LitestarEntrypoint (UnitScopeMiddleware is added automatically)."
        ) from exc


def get_unit_scope(request: Request) -> UnitScopeProtocol:
    """Litestar dependency returning the per-request unit scope.

    Resolves from the ASGI connection scope state (set by
    :class:`UnitScopeMiddleware`).

    Example:
        >>> from litestar import get
        >>> from litestar.di import Provide
        >>> from servicewright.adapters.litestar import get_unit_scope
        >>> @get("/users/{user_id:str}", dependencies={"unit_scope": Provide(get_unit_scope)})
        ... async def handler(user_id: str, unit_scope: object) -> dict:
        ...     use_case = await unit_scope.get(MyUseCase)
        ...     return await use_case.execute(user_id)
    """
    state = request.scope.get("state") or {}
    scope: UnitScopeProtocol | None = state.get(_STATE_KEY)
    if scope is None:
        raise LookupError(
            "No active Litestar unit scope on the connection state; the LitestarEntrypoint "
            "UnitScopeMiddleware must be installed for get_unit_scope() to resolve."
        )
    return scope


class UnitScopeMiddleware(ASGIMiddleware):
    """Open one ``UnitScope`` per request and expose it two ways.

    Added to the Litestar app by the :class:`LitestarEntrypoint`. The scope
    carries the ``Request`` as its ``context`` and is closed/reset after the
    response is produced. Restricted to HTTP scopes (websockets/lifespan pass
    through untouched).
    """

    scopes = (ScopeType.HTTP,)

    def __init__(self, container: DependencyContainerProtocol) -> None:
        self._container = container

    async def handle(self, scope: Scope, receive: Receive, send: Send, next_app: ASGIApp) -> None:
        """Wrap the request in a fresh unit scope bound to scope state + a contextvar."""
        request: Request = Request(scope)
        async with self._container.unit_scope({"request": request}) as unit_scope:
            connection_state = scope.setdefault("state", {})
            connection_state[_STATE_KEY] = unit_scope
            token = _current_unit_scope.set(unit_scope)
            try:
                await next_app(scope, receive, send)
            finally:
                _current_unit_scope.reset(token)


__all__ = [
    "UnitScopeMiddleware",
    "current_unit_scope",
    "get_unit_scope",
]
