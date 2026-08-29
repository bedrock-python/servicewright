# Your first service

We will build a background worker that sweeps expired rows every five seconds, and stops cleanly
when Kubernetes sends it a `SIGTERM`.

No extras, no DI library, no framework. Just `pip install servicewright`.

## 1. Settings

servicewright reads observability configuration off a settings object you own. It never invents
one, and it never reaches for a global.

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    logging: object | None = None
    metrics: object | None = None
    tracing: object | None = None
    error_tracking: object | None = None

    def get_app_version(self) -> str:
        return "1.0.0"
```

Four sections, all `None` for now, plus a version. A section set to `None` means "this concern is
not configured" and the matching sink stays a no-op. Later you fill them in with real values —
see [Settings](../concepts/settings.md).

!!! tip

    Any object with these attributes works: a `pydantic-settings` model, a dataclass, a plain
    class. servicewright checks shape, not inheritance. The `settings` extra ships the shape as
    ready-made models — see [Settings](../concepts/settings.md#shipped-models).

## 2. A dependency container

The kernel does not depend on a DI library. It asks for exactly two things: a scope that lives as
long as the process, and a scope that lives as long as one unit of work.

```python
import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import Any


class Scope:
    """Resolves dependencies. One method is the entire scope contract."""

    def __init__(self, provides: Mapping[Any, Any]) -> None:
        self._provides = dict(provides)

    async def get(self, dependency_key: type[Any] | str) -> Any:
        return self._provides[dependency_key]


class Container:
    """A whole DI container in two methods."""

    def __init__(self, provides: Mapping[Any, Any]) -> None:
        self._provides = dict(provides)

    @contextlib.asynccontextmanager
    async def app_scope(self) -> AsyncIterator[Scope]:
        # Opened once at startup, closed last at shutdown.
        yield Scope(self._provides)

    @contextlib.asynccontextmanager
    async def unit_scope(self, context: Mapping[Any, Any] | None = None) -> AsyncIterator[Scope]:
        # Opened once per request / job / message.
        yield Scope(self._provides)
```

In a real service you would use [dishka](../adapters/dishka.md) instead:

```python
from servicewright.adapters.dishka import DishkaContainer

def build_container(settings: Settings) -> DishkaContainer:
    return DishkaContainer(make_async_container(AppProvider()))
```

The kernel cannot tell the difference. See [Dependency injection](../concepts/dependency-injection.md).

## 3. The work

```python
class Sweeper:
    def __init__(self) -> None:
        self.runs = 0

    async def sweep(self) -> int:
        self.runs += 1
        return 0  # rows removed
```

And the loop that drives it. It receives the DI scope and a `stop` event:

```python
import asyncio
import contextlib

from servicewright import UnitScopeProtocol

SWEEP_INTERVAL_SECONDS = 5.0


async def sweep_loop(scope: UnitScopeProtocol, stop: asyncio.Event) -> None:
    sweeper = await scope.get(Sweeper)
    while not stop.is_set():
        removed = await sweeper.sweep()
        print(f"swept {removed} rows")
        # Sleeps, but wakes up immediately when a shutdown signal arrives.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_INTERVAL_SECONDS)
```

!!! note "Why `stop` and not `while True`"

    `stop` is set the moment a `SIGTERM` arrives. Waiting on it instead of sleeping blindly is
    what turns a 5-second poll interval into a shutdown that takes milliseconds, not 5 seconds.

## 4. Assemble and run

```python
from servicewright import AppSpec, DaemonEntrypoint, Service, run_sync


def build_container(settings: Settings) -> Container:
    return Container({Sweeper: Sweeper()})


spec = AppSpec(
    service_name="ledger-sweeper",
    create_container=build_container,
)

service = Service(spec, entrypoints=[DaemonEntrypoint(sweep_loop)])

if __name__ == "__main__":
    run_sync(service, Settings())
```

Run it, let it tick a few times, then press ++ctrl+c++.

## What just happened

In order:

1. **Bootstrap.** Observability was configured (nothing selected here, so nothing happened), then
   `build_container(settings)` was called and the **application scope** was opened.
2. **Warmup.** No warmers registered, so this was instant. In a real service this is where the
   connection pools get primed — *before* anything reports ready.
3. **Bind.** `DaemonEntrypoint.bind()` captured the container. Nothing is accepted yet.
4. **Ready.** `spec.health.ready` flipped to `True`.
5. **Serve.** Your `sweep_loop` ran inside one long-lived unit scope.
6. ++ctrl+c++ → **readiness flipped back to `False` first**, then `drain()`, then `stop()`.
7. **Cleanup.** The application scope closed, so pools and clients got finalized last.

That ordering is the whole point of the Host, and it is identical for an HTTP server, a gRPC
server and a cron job. See [Lifecycle](../concepts/lifecycle.md).

## What you got for free

- **Signal handling.** The first `SIGINT`/`SIGTERM` starts the graceful sequence. A second one
  exits immediately with `128 + signum`, because cleanup can hang and an operator hitting
  ++ctrl+c++ twice means *now*.
- **Meaningful exit codes.** A crash in `sweep_loop` propagates out of `run()` after cleanup, so
  the process exits non-zero and a supervisor sees a failure. A clean stop exits `0`.
- **A readiness signal.** `spec.health` is already tracking readiness. Add an HTTP entrypoint and
  `/system/health/readyz` starts reporting it, with no wiring on your side.

## Next

- [Tutorial](tutorial.md) — an HTTP API and a cron job in one process, with real DI and metrics.
- [Architecture](../concepts/architecture.md) — the six nouns and how they fit together.
