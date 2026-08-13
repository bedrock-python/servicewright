# Daemon and one-shot

Two entrypoints that need no extra at all — they are pure Python and ship with the kernel. Between
them they cover most work that is not a server.

## `DaemonEntrypoint`

A long-running loop, wrapped in **one** unit scope for its whole life.

```python
import asyncio
import contextlib

from servicewright import DaemonEntrypoint, Service, UnitScopeProtocol

POLL_INTERVAL_SECONDS = 5.0


async def outbox_publisher(scope: UnitScopeProtocol, stop: asyncio.Event) -> None:
    publisher = await scope.get(OutboxPublisher)
    while not stop.is_set():
        await publisher.publish_batch()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)


service = Service(spec, entrypoints=[DaemonEntrypoint(outbox_publisher, kind="outbox")])
```

Your function receives the scope and the `stop` event, and is expected to return when `stop` is
set.

!!! tip "Wait on `stop`, do not sleep on it"

    `await asyncio.sleep(5)` makes shutdown take up to five seconds.
    `await asyncio.wait_for(stop.wait(), timeout=5)` makes it instant, and still polls every five
    seconds otherwise.

### One scope for the whole loop

That is the right default for a publisher holding one connection, and the wrong one for a consumer
where each message deserves its own session and transaction. If you need a scope per iteration,
write a [`ScopedEntrypoint`](../guides/custom-entrypoint.md) instead — it is about fifteen lines,
and `unit_scope()` is right there.

## `OneShotEntrypoint`

Runs a function exactly once inside a fresh unit scope, then returns.

```python
from servicewright import OneShotEntrypoint, Service, UnitScopeProtocol


async def migrate(scope: UnitScopeProtocol) -> None:
    migrator = await scope.get(Migrator)
    await migrator.upgrade()


service = Service(spec, entrypoints=[OneShotEntrypoint(migrate, kind="migration")])
```

Because it is `essential=True` by default, its return **stops the whole service**. That is what
makes it a batch job:

- the function returns → the Host shuts everything down gracefully → exit code `0`;
- the function raises → the exception propagates out of `run()` after cleanup → **non-zero exit**,
  and your Kubernetes `Job` retries.

You still get warmup, health, observability and the full shutdown ordering. A migration job and an
API server are configured the same way.

## Arguments

Both take the same three:

| Argument | Default | Meaning |
| --- | --- | --- |
| `func` | required | The callable, positional |
| `kind` | `"daemon"` / `"oneshot"` | Telemetry label |
| `essential` | `True` | Whether its failure or exit stops the process |

Set `essential=False` for background work that may fail without taking the service with it:

```python
DaemonEntrypoint(cache_refresher, kind="cache-refresh", essential=False)
```

## Combining them

```python
service = Service(spec, entrypoints=[
    FastApiEntrypoint(routers=(router,)),
    DaemonEntrypoint(outbox_publisher, kind="outbox"),
])
```

The API serves while the outbox publisher drains the queue, both on the same container, both
stopping together.
