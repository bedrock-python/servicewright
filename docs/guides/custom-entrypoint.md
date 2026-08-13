# Writing an entrypoint

Anything that brings work into your service can be an entrypoint: a NATS subscription, a Kafka
consumer, a WebSocket server, a file watcher, an SQS poller. It takes four methods and no
dependencies — the contract is pure Python.

We will write a NATS consumer.

## 1. Pick the base class

The question is: **who opens the per-unit DI scope?**

A consumer decides for itself what one unit of work is, so it is a
[`ScopedEntrypoint`](../concepts/entrypoints.md#scopedentrypoint).

(If you were wrapping a framework whose integration already opens a scope per request — a web
server — you would extend `ServerEntrypoint`, which has no `unit_scope` at all.)

## 2. The skeleton

```python
import asyncio
import logging

from servicewright import ScopedEntrypoint, ServiceContext

logger = logging.getLogger(__name__)


class NatsConsumerEntrypoint(ScopedEntrypoint):
    kind = "nats"          # telemetry label only
    essential = True       # its failure stops the process

    def __init__(self, url: str, subject: str) -> None:
        super().__init__()
        self._url = url
        self._subject = subject
        self._connection = None
        self._subscription = None
```

## 3. `bind` — allocate, accept nothing

```python
    async def bind(self, ctx: ServiceContext) -> None:
        await super().bind(ctx)          # captures the container for unit_scope()
        self._connection = await nats.connect(self._url)
        self._subscription = await self._connection.subscribe(self._subject)
        logger.info("NATS consumer bound", extra={"subject": self._subject})
```

Raising here is the right move if the connection cannot be made: the Host aborts startup, and the
pod never reports ready.

!!! warning

    If you override `bind`, call `super().bind(ctx)`. That is what captures the container —
    without it, `unit_scope()` raises `RuntimeError`.

## 4. `serve` — one scope per message

```python
    async def serve(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            message = await self._next_message(timeout=1.0)
            if message is None:
                continue

            async with self.unit_scope({"subject": message.subject}) as scope:
                handler = await scope.get(MessageHandler)
                try:
                    await handler.handle(message)
                except Exception:
                    logger.exception("Message handling failed", extra={"subject": message.subject})
```

Two things to notice:

- **The poll has a timeout.** Blocking forever on `next_message()` would mean the loop only
  notices `stop` when a message happens to arrive. A short timeout keeps shutdown responsive.
- **Handler errors are caught.** One poisoned message must not kill the consumer — the same rule
  the [scheduler](../adapters/scheduler.md#what-happens-when-a-job-fails) follows. Let the
  exception escape only if you genuinely want the process to die.

## 5. `drain` and `stop`

```python
    async def drain(self, grace: float) -> None:
        # Stop intake. The message currently being handled finishes on its own.
        if self._subscription is not None:
            await self._subscription.unsubscribe()

    async def stop(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
```

`drain` should return once intake has stopped and in-flight work has either finished or used up
`grace`. If you track in-flight units yourself, poll them here:

```python
    async def drain(self, grace: float) -> None:
        await self._subscription.unsubscribe()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        while self._in_flight and loop.time() < deadline:
            await asyncio.sleep(0.05)

        if self._in_flight:
            logger.warning("Drain timed out", extra={"in_flight": len(self._in_flight)})
```

`stop()` must be **idempotent** and safe to call after `drain`, and it may be called even if
`bind` failed halfway through.

## 6. Use it

```python
service = Service(spec, entrypoints=[
    FastApiEntrypoint(routers=(router,)),
    NatsConsumerEntrypoint("nats://localhost:4222", "orders.*"),
])
```

It now takes part in warmup, readiness, drain and cleanup exactly like the HTTP entrypoint.

## 7. Bundle it as a plugin

If the consumer comes with a warmer and a health check, wrap the whole thing so a service adopts
it in one line:

```python
class NatsPlugin:
    def __init__(self, url: str, subject: str) -> None:
        self._url = url
        self._subject = subject

    def on_register(self, spec, host) -> None:
        entrypoint = NatsConsumerEntrypoint(self._url, self._subject)
        host.add_entrypoint(entrypoint)
        spec.health.add_check("nats", NatsHealthCheck(self._url))
```

```python
service = Service(spec, plugins=[NatsPlugin("nats://localhost:4222", "orders.*")])
```

See [Plugins](../concepts/plugins.md).

## Testing it

Drive the four methods directly — no Host required:

```python
from servicewright.testing import FakeContainer


async def test__consumer__message_arrives__handled_in_its_own_scope() -> None:
    container = FakeContainer(provides={MessageHandler: handler})
    ctx = make_service_context(container)
    entrypoint = NatsConsumerEntrypoint("nats://test", "orders.*")

    await entrypoint.bind(ctx)
    stop = asyncio.Event()
    task = asyncio.create_task(entrypoint.serve(stop=stop))
    ...
    stop.set()
    await task
    await entrypoint.drain(1.0)
    await entrypoint.stop()

    assert container.unit_scopes_opened == 1
    assert container.unit_contexts == [{"subject": "orders.created"}]
```

`FakeContainer` counts the scopes it opened and records every context. See
[Testing](testing.md).

## Checklist

- [ ] Extends `ScopedEntrypoint` (you open the scope) or `ServerEntrypoint` (the framework does).
- [ ] `bind` calls `super().bind(ctx)` and raises on unavailable resources.
- [ ] `bind` allocates but accepts nothing.
- [ ] `serve` returns promptly when `stop` is set, and returns **while still accepting**.
- [ ] `serve` raises only for failures that should stop the process.
- [ ] `drain` stops intake and honours `grace`.
- [ ] `stop` is idempotent and safe after a partial `bind`.
- [ ] `kind` is set to something useful in logs.
- [ ] `essential` matches the semantics you want.
- [ ] No signal handlers, no `sys.exit`, no event-loop creation. The Host owns all three.
