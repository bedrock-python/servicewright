"""Zero-dependency long-running daemon entrypoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.contracts import ScopedEntrypoint

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    from ...core.contracts import UnitScopeProtocol


class DaemonEntrypoint(ScopedEntrypoint):
    """Runs ``func(scope, stop)`` in one long-lived unit scope.

    The function is expected to loop until ``stop`` is set.
    """

    def __init__(
        self,
        func: Callable[[UnitScopeProtocol, asyncio.Event], Awaitable[None]],
        *,
        kind: str = "daemon",
        essential: bool = True,
    ) -> None:
        super().__init__()
        self._func = func
        self.kind = kind
        self.essential = essential

    async def serve(self, *, stop: asyncio.Event) -> None:
        """Open one unit scope and hand control to the user loop."""
        async with self.unit_scope() as scope:
            await self._func(scope, stop)
