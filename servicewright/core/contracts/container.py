"""DI container and the two-tier dependency-scope protocols."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, overload

if TYPE_CHECKING:
    from collections.abc import Mapping

T = TypeVar("T")


class AppScopeProtocol(Protocol):
    """Protocol for application-level dependency scope.

    The application scope is opened once for the whole process lifetime and
    hosts long-lived singletons (connection pools, clients, ...).
    """

    @overload
    async def get(self, dependency_key: type[T]) -> T: ...

    @overload
    async def get(self, dependency_key: str) -> Any: ...

    async def get(self, dependency_key: type[T] | str) -> T | Any:
        """Retrieve dependency instance by key or type."""
        ...


class UnitScopeProtocol(Protocol):
    """Protocol for unit-of-work-level dependency scope.

    A unit scope is minted per unit of work (one request, message, job, task
    or activity) and carries that unit's payload as its ``context``.
    """

    @overload
    async def get(self, dependency_key: type[T]) -> T: ...

    @overload
    async def get(self, dependency_key: str) -> Any: ...

    async def get(self, dependency_key: type[T] | str) -> T | Any:
        """Retrieve dependency instance by key or type."""
        ...


class DependencyContainerProtocol(Protocol):
    """Protocol for a DI container exposing the two scope tiers."""

    def app_scope(self) -> contextlib.AbstractAsyncContextManager[AppScopeProtocol]:
        """Return async context manager for the process-lifetime application scope."""
        ...

    def unit_scope(
        self, context: Mapping[Any, Any] | None = None
    ) -> contextlib.AbstractAsyncContextManager[UnitScopeProtocol]:
        """Return async context manager for a per-unit-of-work scope.

        ``context`` keys are container-defined: string-keyed payloads and
        type-keyed contexts (e.g. dishka's ``{Request: request}``) both fit.
        """
        ...
