"""User-facing Service facade, the ``run()`` coroutine and the ``run_sync()`` process entry point."""

from __future__ import annotations

import asyncio
import importlib
from typing import TYPE_CHECKING, Literal

from .aio.host import Host

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from .contracts import (
        BaseServiceSettingsProtocol,
        DependencyContainerProtocol,
        Entrypoint,
        Plugin,
    )
    from .spec import AppSpec

EventLoopName = Literal["auto", "asyncio", "uvloop"]
"""Which event loop implementation :func:`run_sync` creates."""

_LOOP_NAMES: tuple[str, ...] = ("auto", "asyncio", "uvloop")
# Optional loop implementations as "module:factory", imported only when
# selected — the same lazy scheme as the observability registry, so the kernel
# never imports an optional package at import time.
_UVLOOP_FACTORY = "uvloop:new_event_loop"


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

    def run_sync(self, settings: TSettings, *, loop: EventLoopName = "auto") -> None:
        """Run the service to completion on a fresh event loop; see :func:`run_sync`."""
        run_sync(self, settings, loop=loop)


async def run[TSettings: "BaseServiceSettingsProtocol", TContainer: "DependencyContainerProtocol"](
    service: Service[TSettings, TContainer],
    settings: TSettings,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Module-level convenience: ``await servicewright.run(service, settings)``."""
    await service.run(settings, stop=stop)


def run_sync[TSettings: "BaseServiceSettingsProtocol", TContainer: "DependencyContainerProtocol"](
    service: Service[TSettings, TContainer],
    settings: TSettings,
    *,
    loop: EventLoopName = "auto",
) -> None:
    """Run ``service`` to completion on a fresh event loop — the process entry point.

    The blocking twin of :func:`run`: the loop is created by :func:`asyncio.run`
    with the ``loop_factory`` :func:`event_loop_factory` picks for ``loop``, the
    Host installs the OS signal handlers (no ``stop`` event is supplied), and
    whatever the Host raises propagates, so the process exit code keeps its
    meaning.

    Args:
        service: The service to run.
        settings: Service settings.
        loop: ``"auto"`` (uvloop when installed, asyncio's default loop
            otherwise), ``"uvloop"`` (requires ``servicewright[uvloop]``) or
            ``"asyncio"``.
    """
    asyncio.run(service.run(settings), loop_factory=event_loop_factory(loop))


def event_loop_factory(loop: EventLoopName = "auto") -> Callable[[], asyncio.AbstractEventLoop] | None:
    """Return the ``loop_factory`` :func:`asyncio.run` needs for ``loop`` (``None`` = asyncio's default).

    Exposed for embedding: ``asyncio.run(service.run(settings), loop_factory=event_loop_factory("auto"))``
    is exactly what :func:`run_sync` does.

    Raises:
        ValueError: If ``loop`` is not ``"auto"``, ``"asyncio"`` or ``"uvloop"``.
        ImportError: If ``loop="uvloop"`` and uvloop is not installed.
    """
    if loop not in _LOOP_NAMES:
        raise ValueError(f"Unknown event loop {loop!r}; expected one of {list(_LOOP_NAMES)}")
    if loop == "asyncio":
        return None
    module_path, _, attribute = _UVLOOP_FACTORY.partition(":")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        if loop == "uvloop":
            raise ImportError("loop='uvloop' requires servicewright[uvloop]; install it.") from exc
        return None
    factory: Callable[[], asyncio.AbstractEventLoop] = getattr(module, attribute)
    return factory
