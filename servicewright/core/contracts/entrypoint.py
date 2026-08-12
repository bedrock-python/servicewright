"""The Host-facing Entrypoint protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import asyncio

    from ..spec import ServiceContext


@runtime_checkable
class Entrypoint(Protocol):
    """Host-facing protocol implemented by every driver."""

    kind: str
    """Telemetry label only ("http"|"grpc"|"scheduler"|"kafka"|...)."""

    essential: bool
    """If True, this entrypoint's failure/exit stops the whole process.

    A failure also propagates out of ``Host.run`` once cleanup is done, so the
    process exit code distinguishes a crash from a graceful stop.
    """

    async def bind(self, ctx: ServiceContext) -> None:
        """Allocate/subscribe/register. No traffic is accepted yet.

        Raise here if the resource cannot be acquired (a port already in use, a
        missing topic): the Host aborts startup instead of reporting ready.
        """
        ...

    async def serve(self, *, stop: asyncio.Event) -> None:
        """Run until ``stop`` is set, then return **without** shutting down.

        Returning is the signal that the entrypoint is still accepting work and
        is ready to be torn down in order: the Host flips readiness to false
        first (so load balancers stop routing), then calls :meth:`drain`, then
        :meth:`stop`. Closing listeners here instead would make ``drain(grace)``
        inert and would take the readiness endpoint down before the router knows.

        Raise to report a fatal serve-time failure.
        """
        ...

    async def drain(self, grace: float) -> None:
        """Stop intake and let in-flight units finish within ``grace`` seconds."""
        ...

    async def stop(self) -> None:
        """Hard stop / release resources."""
        ...
