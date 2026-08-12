"""In-memory test doubles for the servicewright kernel."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, TypeVar

from .core.contracts import ScopedEntrypoint

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator, Mapping

    from .core.contracts import AppScopeProtocol, UnitScopeProtocol
    from .core.spec import ServiceContext

T = TypeVar("T")


class FakeSettings:
    """Minimal settings double whose observability sections are all disabled."""

    logging: Any = None
    error_tracking: Any = None
    tracing: Any = None
    metrics: Any = None

    def get_app_version(self) -> str:
        """Return a placeholder version."""
        return "0.0.0-test"


class FakeScope:
    """In-memory scope whose ``get`` resolves from a provided mapping."""

    def __init__(self, provides: Mapping[Any, Any] | None = None) -> None:
        self._provides: dict[Any, Any] = dict(provides) if provides else {}
        self.context: Mapping[Any, Any] | None = None

    async def get(self, dependency_key: type[T] | str) -> Any:
        """Resolve a dependency or raise ``KeyError`` when absent."""
        return self._provides[dependency_key]


class FakeContainer:
    """In-memory :class:`DependencyContainerProtocol` for tests and users."""

    def __init__(self, provides: Mapping[Any, Any] | None = None) -> None:
        self._provides: dict[Any, Any] = dict(provides) if provides else {}
        self.app_scopes_opened = 0
        self.unit_scopes_opened = 0
        self.unit_contexts: list[Mapping[Any, Any] | None] = []

    @contextlib.asynccontextmanager
    async def app_scope(self) -> AsyncIterator[AppScopeProtocol]:
        """Yield a fresh application scope."""
        self.app_scopes_opened += 1
        yield FakeScope(self._provides)

    @contextlib.asynccontextmanager
    async def unit_scope(self, context: Mapping[Any, Any] | None = None) -> AsyncIterator[UnitScopeProtocol]:
        """Yield a fresh unit scope carrying ``context``."""
        self.unit_scopes_opened += 1
        self.unit_contexts.append(context)
        scope = FakeScope(self._provides)
        scope.context = context
        yield scope


class FakeEntrypoint(ScopedEntrypoint):
    """Recording entrypoint that serves until ``stop`` is set.

    Useful in tests to assert lifecycle ordering. Set ``run_once=True`` to
    return immediately from ``serve`` (e.g. to model an essential exit).
    """

    def __init__(
        self,
        *,
        kind: str = "fake",
        essential: bool = True,
        run_once: bool = False,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.essential = essential
        self._run_once = run_once
        self.events: list[str] = []

    async def bind(self, ctx: ServiceContext) -> None:
        await super().bind(ctx)
        self.events.append("bind")

    async def serve(self, *, stop: asyncio.Event) -> None:
        self.events.append("serve")
        if self._run_once:
            return
        await stop.wait()

    async def drain(self, grace: float) -> None:
        self.events.append("drain")

    async def stop(self) -> None:
        self.events.append("stop")
