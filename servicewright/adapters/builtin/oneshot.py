"""Zero-dependency one-shot/batch entrypoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.contracts import ScopedEntrypoint

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    from ...core.contracts import UnitScopeProtocol


class OneShotEntrypoint(ScopedEntrypoint):
    """Runs ``func`` exactly once inside a fresh unit scope, then returns.

    Being ``essential`` by default, its return stops the whole service.
    """

    def __init__(
        self,
        func: Callable[[UnitScopeProtocol], Awaitable[None]],
        *,
        kind: str = "oneshot",
        essential: bool = True,
    ) -> None:
        super().__init__()
        self._func = func
        self.kind = kind
        self.essential = essential

    async def serve(self, *, stop: asyncio.Event) -> None:
        """Open a unit scope, run the function once, then return."""
        async with self.unit_scope() as scope:
            await self._func(scope)
