"""The smallest real servicewright service.

This example shows how to:

1. Describe a service once as an `AppSpec` -- a service name plus a DI
   container factory is everything the kernel demands.
2. Satisfy `DependencyContainerProtocol` with two methods and no DI library at
   all: `app_scope()` holds process-lifetime singletons, `unit_scope()` mints a
   fresh scope per unit of work. A real service swaps in `DishkaContainer`
   here and the kernel never notices.
3. Drive the service with the built-in `DaemonEntrypoint`, whose loop runs
   inside one long-lived unit scope until the host's `stop` event is set.
4. Watch the unified lifecycle in order: warmup -> pre_start -> bind -> READY
   -> post_start -> serve -> drain -> stop -> pre_shutdown -> post_shutdown.
   Readiness flips OFF *before* draining begins -- that ordering is what makes
   a Kubernetes rollout lossless.

Run with: `python examples/minimal_service.py`. It serves for about a second,
stops itself through an externally supplied `stop` event, and exits 0.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from servicewright import AppSpec, DaemonEntrypoint, Lifecycle, Service

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from servicewright import AppScopeProtocol, ServiceContext, UnitScopeProtocol

SERVICE_NAME = "ledger-sweeper"
TICK_SECONDS = 0.25
SERVE_FOR_SECONDS = 1.0


@dataclass(frozen=True)
class Settings:
    """All `BaseServiceSettingsProtocol` asks for; every observability section is off."""

    logging: object | None = None
    metrics: object | None = None
    tracing: object | None = None
    error_tracking: object | None = None

    def get_app_version(self) -> str:
        return "1.0.0"


class Ledger:
    """The service's one singleton; the daemon resolves it from its unit scope."""

    def __init__(self) -> None:
        self.ticks = 0

    def record_tick(self) -> None:
        self.ticks += 1


class InMemoryScope:
    """A dependency scope: a single `get` satisfies both scope protocols."""

    def __init__(self, provides: Mapping[Any, Any]) -> None:
        self._provides = dict(provides)

    async def get(self, dependency_key: type[Any] | str) -> Any:
        return self._provides[dependency_key]


class InMemoryContainer:
    """A whole DI container in two methods -- the entire integration surface."""

    def __init__(self, provides: Mapping[Any, Any]) -> None:
        self._provides = dict(provides)
        self.unit_scopes_opened = 0

    @contextlib.asynccontextmanager
    async def app_scope(self) -> AsyncIterator[InMemoryScope]:
        print("[container]  app scope opened (process-lifetime singletons live here)")
        try:
            yield InMemoryScope(self._provides)
        finally:
            print("[container]  app scope closed (always the very last step)")

    @contextlib.asynccontextmanager
    async def unit_scope(self, context: Mapping[Any, Any] | None = None) -> AsyncIterator[InMemoryScope]:
        self.unit_scopes_opened += 1
        yield InMemoryScope(self._provides)


class NarratedDaemon(DaemonEntrypoint):
    """A `DaemonEntrypoint` that narrates the four Entrypoint contract methods.

    Nothing but the printing is added -- every method delegates to the built-in.
    """

    async def bind(self, ctx: ServiceContext[Any, Any]) -> None:
        await super().bind(ctx)
        print(f"[entrypoint] bind      (kind={self.kind}, service={ctx.service_name}, no traffic yet)")

    async def serve(self, *, stop: asyncio.Event) -> None:
        print("[entrypoint] serve     (one unit scope wraps the whole loop)")
        await super().serve(stop=stop)

    async def drain(self, grace: float) -> None:
        print(f"[entrypoint] drain     (grace={grace:.0f}s for in-flight work)")
        await super().drain(grace)

    async def stop(self) -> None:
        print("[entrypoint] stop      (hard stop, resources released)")
        await super().stop()


async def sweep_loop(scope: UnitScopeProtocol, stop: asyncio.Event) -> None:
    """The daemon body: resolve dependencies once, then loop until `stop` is set."""
    ledger = await scope.get(Ledger)
    while not stop.is_set():
        ledger.record_tick()
        print(f"[daemon]     tick #{ledger.ticks}")
        # A sleep that wakes up the moment a stop signal arrives.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)


def register_lifecycle_hooks(lifecycle: Lifecycle) -> None:
    """Attach a print to each of the four hook points the Host drives."""

    async def pre_start(app_scope: AppScopeProtocol | None) -> None:
        print("[lifecycle]  pre_start  (warmup finished, nothing bound yet)")

    async def post_start(app_scope: AppScopeProtocol | None) -> None:
        print("[lifecycle]  post_start (readiness is already true)")

    async def pre_shutdown(app_scope: AppScopeProtocol | None) -> None:
        print("[lifecycle]  pre_shutdown (every entrypoint drained and stopped)")

    async def post_shutdown(app_scope: AppScopeProtocol | None) -> None:
        print("[lifecycle]  post_shutdown (app scope already closed)")

    lifecycle.add_pre_start_hook(pre_start)
    lifecycle.add_post_start_hook(post_start)
    lifecycle.add_pre_shutdown_hook(pre_shutdown)
    lifecycle.add_post_shutdown_hook(post_shutdown)


async def main() -> None:
    ledger = Ledger()
    container = InMemoryContainer({Ledger: ledger})

    spec: AppSpec[Settings, InMemoryContainer] = AppSpec(
        service_name=SERVICE_NAME,
        create_container=lambda _settings: container,
    )
    register_lifecycle_hooks(spec.lifecycle)

    service = Service(spec, entrypoints=[NarratedDaemon(sweep_loop, kind="sweeper")])

    # Supplying `stop` is the embedding/test path: the Host then does NOT
    # install SIGINT/SIGTERM handlers. Production code omits it and lets the
    # Host translate the signal into this very event.
    stop = asyncio.Event()

    async def serve_then_stop() -> None:
        while not spec.health.ready:
            await asyncio.sleep(0.01)
        print(f"\n--- serving (readiness={spec.health.ready}) ---")
        await asyncio.sleep(SERVE_FOR_SECONDS)
        print("\n--- shutdown (readiness flips off BEFORE the drain) ---")
        stop.set()

    print("--- startup ---")
    await asyncio.gather(service.run(Settings(), stop=stop), serve_then_stop())

    print(f"\n[summary] {ledger.ticks} tick(s) recorded across {container.unit_scopes_opened} unit scope(s)")
    print(f"[summary] readiness after shutdown: {spec.health.ready}")


if __name__ == "__main__":
    asyncio.run(main())
