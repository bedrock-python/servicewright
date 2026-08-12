"""The single extension mechanism: Plugin.on_register."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..aio.host import Host
    from ..spec import AppSpec


@runtime_checkable
class Plugin(Protocol):
    """Litestar ``on_app_init`` analogue.

    A plugin mutates a neutral spec/host: append entrypoints, warmers, health
    checks, lifecycle hooks or DI providers. This is the only batteries-optional
    extension surface — never an entrypoint subclass.
    """

    def on_register(self, spec: AppSpec, host: Host) -> None:
        """Mutate the spec/host before the run-loop starts."""
        ...
