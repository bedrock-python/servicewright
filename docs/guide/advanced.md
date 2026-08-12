# Advanced

## Writing your own entrypoint

An entrypoint is four methods; the Host drives them and never branches on what you are. Pick the
base class by **who opens the per-unit DI scope** — that choice makes the double-scope footgun
impossible:

- **`ServerEntrypoint`** — socket-serving frameworks whose integration opens the per-request scope
  itself (FastAPI, gRPC, Litestar). No `unit_scope` access at all.
- **`ScopedEntrypoint`** — loop/poll-driven work (consumer, daemon, scheduler). Provides the only
  sanctioned per-unit API: `async with self.unit_scope(context) as scope:`.

```python
from servicewright import ScopedEntrypoint


class NatsConsumerEntrypoint(ScopedEntrypoint):
    kind = "nats"           # telemetry label only
    essential = True        # its failure/exit stops the whole process

    async def bind(self, ctx) -> None:
        await super().bind(ctx)                       # captures the container
        self._sub = await connect_and_subscribe(...)  # allocate, no traffic yet

    async def serve(self, *, stop) -> None:
        while not stop.is_set():
            msg = await self._sub.next(timeout=1.0)
            if msg is None:
                continue
            async with self.unit_scope({"subject": msg.subject}) as scope:
                handler = await scope.get(MessageHandler)
                await handler.handle(msg)

    async def drain(self, grace: float) -> None:
        await self._sub.unsubscribe()                 # stop intake, let in-flight finish

    async def stop(self) -> None:
        await self._sub.close()
```

No dependencies are required to write this — the contract surface is pure Python.

## Plugins

A plugin is the single extension mechanism — it composes, never subclasses:

```python
class NatsPlugin:
    def on_register(self, spec, host) -> None:
        spec.warmers.append(NatsWarmer(...))
        spec.health.add_check("nats", NatsHealthCheck(...))
        host.add_entrypoint(NatsConsumerEntrypoint(...))


service = Service(spec, plugins=[NatsPlugin()])
```

## Custom observability backends

Backends are instrument factories; adding one touches no kernel code:

```python
from servicewright import register_sink
from servicewright.adapters.observability import MetricsSink


class StatsdMetricsSink(MetricsSink):
    backend = "statsd"

    def setup(self, ctx) -> None: ...        # read host/port off ctx.settings.metrics
    def shutdown(self) -> None: ...
    def counter(self, name, description, label_names=()): ...
    def histogram(self, name, description, label_names=(), buckets=None): ...


register_sink("metrics", "statsd", "myapp.observability:StatsdMetricsSink")
# then: ObsConfig(metrics="statsd")
# or pure DI, no registry: ObservabilityManager(metrics=StatsdMetricsSink())
```

Transport adapters compose their recorders from these generic instruments and own their metric
names (e.g. `adapters/grpc/metrics.py` owns `grpc_requests_total`), so a new backend
automatically serves every transport — and vice versa.

## Request context and correlation

Every unit of work binds its identifiers into the transport-neutral core store —
the FastAPI `ContextMiddleware` and the gRPC `UnitScopeInterceptor` both extract
`x-request-id` / `x-user-id` / `x-tenant-id` / `x-trace-id` (generating a request
id when the client sent none, dropping log-unsafe or overlong values). Business
code reads them anywhere in the async call tree:

```python
from servicewright import get_context_value

user_id = get_context_value("user_id")
```

For **outbound** calls, `propagation_metadata()` collects the current identifiers
as ready-to-send headers / gRPC metadata — hand it to your HTTP client or a
client-side context interceptor and the correlation chain continues downstream:

```python
from servicewright import propagation_metadata

response = await http_client.get(url, headers=propagation_metadata())
# or: extra_interceptors=[AsyncClientContextInterceptor(propagation_metadata)]
```

On top of the store, pluggable `ContextSetter`s bridge the values into external
systems. The defaults: structlog contextvars (log-line correlation out of the box)
plus, when `opentelemetry-api` is installed (`servicewright[observability]`), the
`OtelBaggageSetter` — the identifiers ride W3C Baggage, so OTel-instrumented
clients propagate them to downstream services automatically. Replace or extend
them entirely (this is also the seam for bridging into your platform's own
contextvars package):

```python
class MyTracingSetter:
    def set(self, context_data: dict) -> callable:
        token = my_ctx.set(context_data.get("trace_id"))
        return lambda: my_ctx.reset(token)


middlewares = MiddlewareConfig(context_setters=[MyTracingSetter()])
```

## Error handling: one taxonomy, every transport

Business code raises typed errors from the transport-neutral taxonomy — or its
own subclasses:

```python
from servicewright import ErrorKind, ServiceError


class UserMissingError(ServiceError):
    kind = ErrorKind.NOT_FOUND  # code auto-derives: "user_missing"


raise UserMissingError("no such user", params={"user_id": user_id})
```

The transports map the same error consistently: the FastAPI adapter renders a 404
RFC 9457 `application/problem+json` document; the gRPC adapter aborts with
`NOT_FOUND` (the machine code travels in the `x-error-code` trailing metadata).
`public=False` masks an error everywhere — the client sees a generic internal
error, the real code goes to the log only.

The default FastAPI handlers (validation → 422, `HTTPException`, `ServiceError`,
deadline → 504, unhandled → masked 500) all render through one pluggable
renderer. Own the wire format — a custom envelope, localized messages resolved
from `code` + `params` — by implementing it once:

```python
from servicewright import ErrorInfo, RenderedError


class MyEnvelopeRenderer:
    def render(self, info: ErrorInfo) -> RenderedError:
        return RenderedError(
            status_code=info.http_status,
            body={"error": {"code": info.code, "message": self._localize(info)}},
            media_type="application/json",
        )


http = FastApiEntrypoint(config=..., error_renderer=MyEnvelopeRenderer())
```

Handlers for your own exception types plug in via the native
`exception_handlers={MyError: my_handler}` argument; `default_exception_handlers=False`
switches the built-ins off entirely.

## Warmers and health checks

```python
spec.warmers.append(PostgresWarmer(...))              # primed before readiness flips true
spec.health.add_check("postgres", PostgresHealthCheck(...))
```

Warmup runs in priority groups, fail-fast, *before* the service reports ready. Health checks
drive both the HTTP `/system/health/*` routes and the gRPC health service from one registry.

## Testing

`servicewright.testing` ships zero-dependency doubles:

```python
from servicewright.testing import FakeContainer, FakeEntrypoint, FakeScope, FakeSettings

container = FakeContainer(provides={MyHandler: handler})
service = Service(AppSpec(service_name="t", create_container=lambda s: container),
                  entrypoints=[FakeEntrypoint()])
stop = asyncio.Event()
...
await service.run(FakeSettings(), stop=stop)   # externally supplied stop = no signal handlers
```

`FakeEntrypoint.events` records the `bind → serve → drain → stop` order, `FakeContainer` counts
opened scopes and captures every unit-scope context.

## One spec, many processes

`Host` runs N entrypoints in one process, but nothing forces you to colocate: for independent
scaling, build **two** `Service`s from the **same** `AppSpec` factory with different entrypoint
lists (API pods run the HTTP entrypoint, worker pods run the consumer). Warmers, health,
observability and lifecycle stay identical in both.
