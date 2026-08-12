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
    Litestar. servicewright opens the per-unit scope itself via its own
    middleware/interceptor, so wiring dishka's framework setup here too would
    double-open the request scope. Native ``FromDishka`` handler-injection is a
    separate, later enhancement layered on top of this adapter.
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
        """
        request_context = dict(context) if context is not None else None
        async with self._container(context=request_context) as request_container:
            yield DishkaScope(request_container)


__all__ = [
    "DishkaContainer",
    "DishkaScope",
]
