"""User-facing Service facade and module-level run() convenience."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .aio.host import Host

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Iterable

    from .contracts import (
        BaseServiceSettingsProtocol,
        DependencyContainerProtocol,
        Entrypoint,
        Plugin,
    )
    from .spec import AppSpec


class Service[TSettings: "BaseServiceSettingsProtocol", TContainer: "DependencyContainerProtocol"]:
    """Declarative facade: an :class:`AppSpec` plus entrypoints and plugins.

    ``service.run(settings)`` builds a :class:`Host` and blocks until a stop
    signal is received.
    """

    def __init__(
        self,
        spec: AppSpec[TSettings, TContainer],
        *,
        entrypoints: Iterable[Entrypoint] = (),
        plugins: Iterable[Plugin] = (),
    ) -> None:
        self.spec = spec
        self._entrypoints: list[Entrypoint] = list(entrypoints)
        self._plugins: list[Plugin] = list(plugins)

    @property
    def entrypoints(self) -> list[Entrypoint]:
        """Configured entrypoints."""
        return self._entrypoints

    @property
    def plugins(self) -> list[Plugin]:
        """Configured plugins."""
        return self._plugins

    async def run(self, settings: TSettings, *, stop: asyncio.Event | None = None) -> None:
        """Run the service, blocking until ``stop`` is set or a signal arrives."""
        host: Host[TSettings, TContainer] = Host(self.spec)
        await host.run(settings, self._entrypoints, plugins=self._plugins, stop=stop)


async def run[TSettings: "BaseServiceSettingsProtocol", TContainer: "DependencyContainerProtocol"](
    service: Service[TSettings, TContainer],
    settings: TSettings,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Module-level convenience: ``await servicewright.run(service, settings)``."""
    await service.run(settings, stop=stop)
