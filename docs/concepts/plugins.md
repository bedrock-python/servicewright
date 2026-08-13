# Plugins

A plugin is the one extension mechanism. It takes a neutral `AppSpec` and a `Host`, and adds
things to them.

```python
class Plugin(Protocol):
    def on_register(self, spec: AppSpec, host: Host) -> None: ...
```

One method, called once, before the run loop starts. There is no subclassing of entrypoints, no
registry to import into, no hook ordering to reason about.

## What a plugin can do

Everything a caller could do by hand — which is the point. A plugin bundles the wiring for one
capability so a service can adopt it in one line:

```python
class NatsPlugin:
    def __init__(self, url: str) -> None:
        self._url = url

    def on_register(self, spec, host) -> None:
        client = NatsClient(self._url)

        host.add_entrypoint(NatsConsumerEntrypoint(client))          # a new driver
        spec.warmers.append(NatsWarmer(client))                      # primed before ready
        spec.health.add_check("nats", NatsHealthCheck(client))       # drives /readyz
        spec.lifecycle.add_pre_shutdown_hook(client.flush)           # graceful shutdown
```

```python
service = Service(spec, plugins=[NatsPlugin("nats://localhost:4222")])
```

The consumer now participates in warmup, readiness, drain and cleanup like everything else.

## The built-in plugins

Each entrypoint adapter ships a `*Plugin` twin that takes the same arguments and registers the
entrypoint for you:

| Plugin | Registers |
| --- | --- |
| `FastApiPlugin` | `FastApiEntrypoint` |
| `LitestarPlugin` | `LitestarEntrypoint` |
| `GrpcPlugin` | `GrpcEntrypoint` |
| `SchedulerPlugin` | `SchedulerEntrypoint` |

```python
from servicewright.adapters.fastapi import FastApiPlugin

service = Service(spec, plugins=[FastApiPlugin(config=HttpConfig(port=8000), routers=(router,))])
```

Use whichever reads better. Passing the entrypoint directly is more explicit; a plugin composes
better when the wiring is bundled with other things.

Both expose the built entrypoint, which is handy in tests:

```python
plugin = FastApiPlugin(routers=(router,))
app = await plugin.entrypoint.build_app(ctx)
```

## When plugins run

`on_register` is called **before** anything else — before observability is configured, before the
container is built. A plugin can therefore still replace `spec.observability`, add warmers that
will actually run, and register health checks that the first readiness probe will see.

Plugins run in the order you list them, and each sees the mutations of the ones before it.

## Next

- [Write your own entrypoint](../guides/custom-entrypoint.md) — usually the thing a plugin wraps.
- [Architecture](architecture.md) — where plugins sit in the model.
