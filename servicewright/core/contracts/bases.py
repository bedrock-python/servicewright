"""Author-facing entrypoint base classes that encode the double-scope rule.

Which base you extend decides who opens the per-unit DI scope, so the footgun
is impossible:

- :class:`ServerEntrypoint` — socket-serving entrypoints whose framework opens
  the per-request scope itself. It has **no** access to ``unit_scope``.
- :class:`ScopedEntrypoint` — loop/poll-driven entrypoints that open the
  per-unit scope themselves via the sanctioned :meth:`ScopedEntrypoint.unit_scope`.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio
    import contextlib
    from collections.abc import Mapping

    from ..spec import ServiceContext
    from .container import DependencyContainerProtocol, UnitScopeProtocol


class ServerEntrypoint(abc.ABC):
    """Base for socket-serving entrypoints (FastAPI, gRPC, Litestar, Flask).

    The framework's DI integration owns the per-request scope, so this base
    deliberately exposes no ``unit_scope`` and cannot double-open one.
    """

    kind: str = "server"
    essential: bool = True

    async def bind(self, ctx: ServiceContext) -> None:
        """Bind to the service context. Override to allocate the server."""
        return None

    @abc.abstractmethod
    async def serve(self, *, stop: asyncio.Event) -> None:
        """Run the server until ``stop`` is set."""
        raise NotImplementedError

    async def drain(self, grace: float) -> None:
        """Stop accepting new connections; let in-flight requests finish."""
        return None

    async def stop(self) -> None:
        """Hard stop the server."""
        return None


class ScopedEntrypoint(abc.ABC):
    """Base for loop/poll-driven entrypoints (scheduler, consumer, daemon, one-shot).

    Provides the *only* sanctioned per-unit DI API: ``async with
    self.unit_scope(context) as scope:`` which delegates to the container.
    """

    kind: str = "scoped"
    essential: bool = True

    def __init__(self) -> None:
        self._container: DependencyContainerProtocol | None = None

    async def bind(self, ctx: ServiceContext) -> None:
        """Capture the container so per-unit scopes can be opened."""
        self._container = ctx.container

    def unit_scope(
        self, context: Mapping[Any, Any] | None = None
    ) -> contextlib.AbstractAsyncContextManager[UnitScopeProtocol]:
        """Open a per-unit-of-work DI scope.

        Raises:
            RuntimeError: If called before :meth:`bind`.
        """
        if self._container is None:
            raise RuntimeError("unit_scope() called before bind(); entrypoint is not bound to a container")
        return self._container.unit_scope(context)

    @abc.abstractmethod
    async def serve(self, *, stop: asyncio.Event) -> None:
        """Run the loop until ``stop`` is set."""
        raise NotImplementedError

    async def drain(self, grace: float) -> None:
        """Stop intake; let in-flight units finish."""
        return None

    async def stop(self) -> None:
        """Hard stop / release resources."""
        return None
