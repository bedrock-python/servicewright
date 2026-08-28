"""Dishka adapter implementing :class:`DependencyContainerProtocol`.

Maps servicewright's two-tier DI scope model onto dishka:

- ``AppScope``  <-> dishka ``Scope.APP``
- ``UnitScope`` <-> dishka ``Scope.REQUEST``

A :class:`DishkaContainer` wraps the ``AsyncContainer`` returned by dishka's
``make_async_container(...)`` (which is already at ``Scope.APP``). Its two scope
context managers delegate to the dishka container:

- :meth:`DishkaContainer.app_scope` yields a thin wrapper over the APP-scoped
  container and, on exit, awaits ``container.close()`` to finalize APP-scoped
  dependencies (pools, clients, ...).
- :meth:`DishkaContainer.unit_scope` enters the next (REQUEST) scope via
  ``async with self._container(context=context)`` and yields a wrapper over the
  REQUEST-scoped child container. Exiting the ``async with`` closes the REQUEST
  scope, so dishka finalizes REQUEST-scoped dependencies.

Importing this module requires the ``dishka`` extra::

    pip install servicewright[dishka]

Note:
    This adapter does NOT install dishka's native framework integration
    (``setup_dishka`` / ``FromDishka`` handler injection) for FastAPI or
    Litestar: servicewright's HTTP adapters open the per-request scope
    themselves. To let dishka's integration own it instead, switch the
    adapter's middleware off (``MiddlewareConfig(unit_scope=False)`` /
    ``LitestarConfig(unit_scope=False)``) and call ``setup_dishka`` from
    ``configure_app``. Installing both would open two REQUEST scopes per
    request, which :meth:`DishkaContainer.unit_scope` refuses to do.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, TypeVar, overload

try:
    from dishka import AsyncContainer
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("Dishka support requires servicewright[dishka]; install it.") from exc

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

T = TypeVar("T")

# Where dishka's own ASGI integrations publish the request container: their
# middleware sets it on the request before anything else runs, so finding it on
# the request servicewright is about to scope means both integrations are installed.
_DISHKA_REQUEST_CONTAINER_ATTR = "dishka_container"


class DishkaScope:
    """Thin wrapper over a dishka ``AsyncContainer`` exposing ``get``.

    Satisfies both :class:`~servicewright.core.contracts.AppScopeProtocol` and
    :class:`~servicewright.core.contracts.UnitScopeProtocol`: ``get`` resolves a
    dependency by type or string key, delegating to the wrapped container.
    """

    def __init__(self, container: AsyncContainer) -> None:
        self._container = container

    @property
    def container(self) -> AsyncContainer:
        """The wrapped dishka container at this scope."""
        return self._container

    @overload
    async def get(self, dependency_key: type[T]) -> T: ...

    @overload
    async def get(self, dependency_key: str) -> Any: ...

    async def get(self, dependency_key: type[T] | str) -> T | Any:
        """Resolve a dependency by type or string key from this dishka scope."""
        return await self._container.get(dependency_key)


class DishkaContainer:
    """Adapt a dishka ``AsyncContainer`` to :class:`DependencyContainerProtocol`.

    Args:
        container: The APP-scoped ``AsyncContainer`` returned by dishka's
            ``make_async_container(...)``.
    """

    def __init__(self, container: AsyncContainer) -> None:
        self._container = container

    @property
    def container(self) -> AsyncContainer:
        """The underlying APP-scoped dishka container."""
        return self._container

    @contextlib.asynccontextmanager
    async def app_scope(self) -> AsyncIterator[DishkaScope]:
        """Yield the APP scope; closing it finalizes APP-scoped dependencies.

        The dishka container is already at ``Scope.APP`` after
        ``make_async_container``; this context manager simply guarantees that
        ``container.close()`` runs on exit (the Host closes the app scope last).
        """
        try:
            yield DishkaScope(self._container)
        finally:
            await self._container.close()

    @contextlib.asynccontextmanager
    async def unit_scope(self, context: Mapping[Any, Any] | None = None) -> AsyncIterator[DishkaScope]:
        """Enter dishka's ``Scope.REQUEST`` carrying ``context`` as request data.

        Exiting the ``async with`` closes the REQUEST scope, letting dishka
        finalize every REQUEST-scoped dependency.

        Raises:
            RuntimeError: If the request in ``context`` already carries a dishka
                request container — dishka's own framework integration
                (``setup_dishka``) is installed next to servicewright's
                per-request middleware, which would open two REQUEST scopes
                per request.
        """
        _reject_double_open(context)
        request_context = dict(context) if context is not None else None
        async with self._container(context=request_context) as request_container:
            yield DishkaScope(request_container)


def _reject_double_open(context: Mapping[Any, Any] | None) -> None:
    """Raise if the HTTP request in ``context`` is already scoped by dishka's own integration."""
    request = context.get("request") if context else None
    state = getattr(request, "state", None)
    if getattr(state, _DISHKA_REQUEST_CONTAINER_ATTR, None) is None:
        return
    raise RuntimeError(
        "This request already carries a dishka request container: dishka's framework integration "
        "(setup_dishka) and servicewright's per-request unit scope are both installed, which would open "
        "two REQUEST scopes per request. Let dishka own the scope with MiddlewareConfig(unit_scope=False) "
        "(FastAPI) / LitestarConfig(unit_scope=False) (Litestar), or drop setup_dishka and resolve through "
        "UnitScopeDep / current_unit_scope()."
    )


__all__ = [
    "DishkaContainer",
    "DishkaScope",
]
