# servicewright for AI agents

> One page holding everything a coding assistant needs to wire and run a service on
> servicewright correctly, plus a map of where the rest of the documentation keeps the
> details it leaves out. Give an agent this page rather than the whole site.

| | |
|---|---|
| Package | `servicewright` on PyPI, import root `servicewright` |
| Requires | Python 3.12+, asyncio. No runtime dependencies at all |
| Install | `pip install servicewright` · extras: `fastapi`, `fastapi-tracing`, `litestar`, `grpc`, `apscheduler4`, `apscheduler3`, `dishka`, `settings`, `observability`, `metrics`, `sentry`, `redis`, `postgres`, `kafka`, `uvloop`, `all` |
| Async | The whole runtime. `await service.run(settings)` inside a loop you own |
| Sync | `run_sync(service, settings, loop="auto")` — the process entry point, creates the loop |
| Source | <https://github.com/bedrock-python/servicewright> |

## How to read this page

Every page of this site is also served as raw Markdown at its own URL with `.md` in place
of the trailing slash — this page is `/agents.md`, the lifecycle page is
`/concepts/lifecycle.md` — so anything the map below points at can be fetched as plain text
rather than scraped out of HTML. The **Copy page** control at the top of a page does the
same thing for a human with a chat window open. The three API reference pages are the
exception: their Markdown is a handful of instructions to a docstring renderer rather than
the API, so read them as HTML, or read the docstrings in the source.

Top to bottom before writing code. [Rules that hold or break the code](#rules-that-hold-or-break-the-code)
is the section correctness lives in — those are the things the runtime will not save you
from, and most of them are about ordering. Every name used below is in the public API; if
you need something not listed here, fetch the page the [documentation map](#documentation-map)
points at rather than guessing a method that sounds plausible.

## Scope

**It does** run one process through one lifecycle — Bootstrap, Warmup, Ready, Serve, Drain,
Cleanup — for any number of entrypoints at once: an HTTP server, a gRPC server, a cron
scheduler, a daemon loop, a one-shot job. It owns signal handling, the readiness flag, the
drain ordering, the per-step budgets and the process exit code. It defines transport-neutral
contracts for dependency scopes, health checks, warmers, settings, errors and the four
observability concerns, and ships extra-gated adapters that implement them.

**It does not** ship a DI container (it calls yours through two methods), load configuration
(no `.env`, no `os.environ`, no base class to inherit — it reads the shape of the settings
object you construct and pass in), create an event loop except in `run_sync`, or import a
single third-party SDK from its kernel. It writes no routes and no business logic. Nothing
in it is a framework replacement: the FastAPI app the entrypoint builds is a normal FastAPI
app, deliberately without a container-managing lifespan.

## Mental model

Six nouns, and the flow between them.

* **`AppSpec`** — the transport-neutral description of a service: its name, the container
  factory, the lifecycle hooks, the observability manager, the health registry, the warmers
  and the three shutdown timings. One `AppSpec` can be run by different processes with
  different entrypoint lists; that is how an API and its worker stay one codebase.
* **`Entrypoint`** — how work enters. Four methods: `bind` (allocate, subscribe, open the
  socket — no traffic yet), `serve(stop=...)` (run until the stop event, then return
  **still accepting**), `drain(grace)` (stop intake, let in-flight work finish), `stop()`
  (hard stop). `kind` is a telemetry label; `essential` decides whether this entrypoint's
  exit takes the process with it.
* **`Host`** — the kernel that drives them. It configures observability, builds the
  container, opens the application scope, warms up, binds every entrypoint, flips readiness,
  serves them all in one `TaskGroup`, and tears everything down in reverse. It never
  branches on `kind`.
* **`Service`** — the facade over `AppSpec` + entrypoints + plugins. `run` / `run_sync`.
* **`Plugin`** — the one extension mechanism: `on_register(spec, host)` mutates a neutral
  spec and host before the run-loop starts. Every adapter ships one next to its entrypoint.
* **Scopes** — `AppScope` lives for the process and holds singletons; a fresh `UnitScope`
  wraps every unit of work (a request, an RPC, a job run, a message). Your container
  supplies both; the runtime never resolves a dependency itself.

The phase order, which is the whole point:

```
configure observability
  → create_container(settings) → open app scope
  → warmup (priority groups, fail-fast, 60s)
  → pre_start hooks
  → bind() each entrypoint, in order
  → health.ready = True → post_start hooks
  → serve() all concurrently, until stop
  → health.ready = False
  → drain(grace) in reverse bind order
  → stop() in reverse bind order
  → pre_shutdown hooks (app scope still open)
  → app scope closes
  → observability flush → post_shutdown hooks
```

Everything the runtime owns is on that line. Everything else — the container, the settings
object, the routes, the jobs, the business errors — is yours.

## Wiring

The smallest complete service, with nothing installed but the kernel:

```python
import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

from servicewright import AppSpec, DaemonEntrypoint, Service, run_sync


@dataclass(frozen=True)
class Settings:                      # any object of this shape satisfies the protocol
    logging: object | None = None    # every observability concern off
    metrics: object | None = None
    tracing: object | None = None
    error_tracking: object | None = None

    def get_app_version(self) -> str:
        return "1.0.0"


class Scope:
    def __init__(self, provides: dict[Any, Any]) -> None:
        self._provides = provides

    async def get(self, dependency_key: Any) -> Any:
        return self._provides[dependency_key]


class Container:                     # the entire DI integration surface
    def __init__(self, provides: dict[Any, Any]) -> None:
        self._provides = provides

    @contextlib.asynccontextmanager
    async def app_scope(self):
        yield Scope(self._provides)  # closing it is where you close pools

    @contextlib.asynccontextmanager
    async def unit_scope(self, context=None):
        yield Scope(self._provides)


async def sweep(scope, stop: asyncio.Event) -> None:
    while not stop.is_set():         # a daemon loops until the stop event
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=5.0)


spec = AppSpec(
    service_name="ledger-sweeper",
    create_container=lambda settings: Container({}),
    drain_grace_seconds=30.0,
    cleanup_timeout_seconds=10.0,
)
service = Service(spec, entrypoints=[DaemonEntrypoint(sweep)])

if __name__ == "__main__":
    run_sync(service, Settings())    # installs SIGINT/SIGTERM, blocks until stopped
```

An HTTP API and a cron job in one process, with batteries:

```python
from servicewright import AppSpec, ObsConfig, ObservabilityManager, Service, run_sync
from servicewright.adapters.apscheduler4 import ScheduledJob, SchedulerEntrypoint
from servicewright.adapters.dishka import DishkaContainer
from servicewright.adapters.fastapi import FastApiEntrypoint, HttpConfig
from servicewright.adapters.settings import BaseServiceSettings

spec = AppSpec(
    service_name="orders",
    create_container=lambda settings: DishkaContainer(make_async_container(...)),
    observability=ObservabilityManager(ObsConfig(metrics="prometheus", logging="structlog")),
)
service = Service(
    spec,
    entrypoints=[
        FastApiEntrypoint(config=HttpConfig(port=8080), routers=(router,), metrics=True),
        SchedulerEntrypoint(jobs=[ScheduledJob(id="sweep", func=sweep, trigger=trigger)]),
    ],
)
run_sync(service, BaseServiceSettings())
```

Both entrypoints share one container, one observability setup and one shutdown. Swapping
`entrypoints=` is how the same `AppSpec` becomes a second process later.

## The API

Every name in this section is exported from the top level (`from servicewright import ...`)
unless a row says otherwise.

### Running a service

| Name | Signature | Returns |
|---|---|---|
| `Service` | `Service(spec, *, entrypoints=(), plugins=())` | facade; `.spec`, `.entrypoints`, `.plugins` |
| `Service.run` | `await service.run(settings, *, stop=None)` | `None`; blocks until stopped |
| `Service.run_sync` | `service.run_sync(settings, *, loop="auto")` | `None`; creates the loop |
| `run` | `await run(service, settings, *, stop=None)` | module-level twin of `Service.run` |
| `run_sync` | `run_sync(service, settings, *, loop="auto")` | `"auto"` / `"asyncio"` / `"uvloop"` |
| `event_loop_factory` | `event_loop_factory(loop="auto")` | the `loop_factory` for `asyncio.run`, or `None` |
| `Host` | `Host(spec)` | `.run(settings, entrypoints=(), *, plugins=(), stop=None)`, `.add_entrypoint(ep)`, `.bootstrap(settings)` |
| `install_signal_handlers` | `install_signal_handlers(stop_event)` | the installer the Host uses on the unowned path; returns an idempotent remover. Only useful when you pass `stop=` yourself — see rule 8 |

### Describing a service

| Name | Fields / arguments |
|---|---|
| `AppSpec` | `service_name`, `create_container: (TSettings) -> TContainer`, `lifecycle=Lifecycle()`, `observability=ObservabilityManager()`, `health=HealthRegistry()`, `warmers=[]`, `warmers_factory=None`, `drain_grace_seconds=30.0`, `cleanup_timeout_seconds=10.0`, `drain_delay_seconds=0.0` |
| `BootstrapContext` | `settings`, `service_name`, `container`, `lifecycle` — built by `Host.bootstrap`, before the app scope |
| `ServiceContext` | `bootstrap`, `app_scope`, `health`, `observability`; properties `.settings`, `.service_name`, `.container`, `.lifecycle`. This is what `bind(ctx)` receives |
| `DEFAULT_DRAIN_DELAY_SECONDS` | `0.0` |
| `DEFAULT_DRAIN_GRACE_SECONDS` | `30.0` |
| `DEFAULT_CLEANUP_TIMEOUT_SECONDS` | `10.0` |

`warmers_factory` is `(ServiceContext) -> Sequence[AsyncWarmer] | Awaitable[...]`, resolved
once per run inside the application scope and appended to `spec.warmers`.

### Contracts you implement

| Protocol / base | Members | Implement it when |
|---|---|---|
| `Entrypoint` (protocol) | `kind: str`, `essential: bool`, `bind(ctx)`, `serve(*, stop)`, `drain(grace)`, `stop()` | writing a driver from scratch |
| `ServerEntrypoint` (ABC) | the four methods; `kind="server"`, `essential=True`; **no** `unit_scope` | the framework opens the per-request scope (HTTP, gRPC) |
| `ScopedEntrypoint` (ABC) | the four methods plus `unit_scope(context=None)`; `kind="scoped"` | you open the per-unit scope yourself (loops, pollers, schedulers) |
| `Plugin` (protocol) | `on_register(spec, host)` | packaging wiring declaratively |
| `DependencyContainerProtocol` | `app_scope()`, `unit_scope(context=None)` — both async context managers | binding a DI container |
| `AppScopeProtocol` / `UnitScopeProtocol` | `await scope.get(key_or_type)` | the two scope tiers |
| `BaseServiceSettingsProtocol` | properties `logging`, `metrics`, `tracing`, `error_tracking` (each `... | None`) and `get_app_version()` | any settings object |
| `HealthCheckerProtocol` | `await check() -> bool` | a readiness check |
| `AsyncWarmer` (ABC) | `await warmup()`; `priority` (lower first, default `0`), `raise_on_failure` (constructor keyword, default `True`) | priming a pool before readiness |
| `LifecycleHookProtocol` | `await hook(app_scope=None)` | a start/shutdown hook |
| `ContextSetter` | `set(context_data) -> Callable[[], None]` | bridging context into structlog, OTel baggage, … |
| `HttpErrorRendererProtocol` | `render(info: ErrorInfo) -> RenderedError` | owning the error wire format |

`ScopedEntrypoint.unit_scope()` raises `RuntimeError` before `bind()` has run.

### Lifecycle, health, warmup

| Name | Signature | Notes |
|---|---|---|
| `Lifecycle` | `add_pre_start_hook(h)`, `add_post_start_hook(h)`, `add_pre_shutdown_hook(h)`, `add_post_shutdown_hook(h)` | a hook is an async callable taking the app scope or nothing; the signature is introspected |
| `HealthRegistry` | `HealthRegistry(*, readiness_cache_ttl=0.0)`; `.ready`, `.add_check(name, check)`, `.checks`, `await .liveness()`, `await .readiness()` | `add_check` raises `ValueError` on a duplicate name |
| `HealthReport` | `healthy: bool`, `checks: dict[str, bool]`, `.status` | frozen |
| `ProbeStatus` | `HEALTHY` / `UNHEALTHY`, `ProbeStatus.from_bool(flag)` | `StrEnum` |
| `collect_warmers` | `await collect_warmers(base_warmers, warmers_factory, app_ctx)` | what the Host calls |
| `perform_warmup` | `await perform_warmup(service_name, warmers, timeout=60.0)` | raises `WarmupTimeoutError` on overrun |
| `warmup_async` | `await warmup_async(warmers=None, raise_on_failure=True, timeout=None)` | the priority-group engine underneath |

### Errors on the wire

| Name | Signature | Notes |
|---|---|---|
| `ServiceError` | `ServiceError(detail=None, *, code=None, kind=None, params=None, public=None)` | subclass it and set `kind` / `code` / `public` as class attributes; `code` defaults to the snake_cased class name minus `Error` |
| `ErrorKind` | `INVALID`, `UNAUTHENTICATED`, `FORBIDDEN`, `NOT_FOUND`, `CONFLICT`, `PRECONDITION_FAILED`, `TOO_MANY_REQUESTS`, `DEADLINE_EXCEEDED`, `UNAVAILABLE`, `NOT_IMPLEMENTED`, `INTERNAL` | 400/401/403/404/409/412/429/504/503/501/500 over HTTP |
| `ErrorInfo` | `kind`, `code`, `detail=None`, `params={}`, `public=True`, `status_override=None`, `headers=None`; `.http_status`; `ErrorInfo.from_service_error(exc)` | the normalized view every renderer takes |
| `mask_private_error` | `mask_private_error(info) -> ErrorInfo` | `public=False` collapses to a generic `internal_error` 500 |
| `INTERNAL_ERROR_CODE` | `"internal_error"` | the code every masked error renders as, on both transports |
| `RenderedError` | `status_code`, `body`, `media_type="application/problem+json"`, `headers=None` | |
| `ProblemDetailsRenderer` | `ProblemDetailsRenderer(*, type_base=None)` | RFC 9457; the default renderer |

`HTTP_STATUS_BY_KIND`, `INTERNAL_ERROR_CODE`, `status_title` and `to_json_safe` live in
`servicewright.core.errors` and are not re-exported at the top level.

`ErrorKind.CONFLICT` maps to `ALREADY_EXISTS` and not to `ABORTED`: both mean 409, but `ABORTED`
tells the client to retry at a higher level, which a state conflict will not survive. Both status
tables are injective, so a status maps back to exactly one kind.

### Request context

| Name | Signature | Notes |
|---|---|---|
| `bind_context` | `with bind_context(request_id="…", user_id="…"):` | context manager |
| `bind_context_values` | `remove = bind_context_values(mapping)` | `None` values are skipped; the remover is idempotent |
| `set_context_value` / `get_context_value` | `set_context_value(key, value) -> Token`, `get_context_value(key, default=None)` | |
| `current_context` | `current_context() -> dict[str, Any]` | every non-`None` value |
| `propagation_metadata` | `propagation_metadata(keys=None) -> dict[str, str]` | `{header: value}` ready for outbound headers / gRPC metadata |
| `STANDARD_PROPAGATION_HEADERS` | `request_id`→`x-request-id`, `user_id`→`x-user-id`, `tenant_id`→`x-tenant-id`, `trace_id`→`x-trace-id` | the default mapping |
| `is_safe_context_id` | `is_safe_context_id(value) -> bool` | ≤ 256 chars, `A-Za-z0-9 . _ - + = / :` and space |

`get_context_var` and `reset_context_value` are in `servicewright.core.context` if you need
the raw `ContextVar`; `bind_context_values` returns the remover, so usually you do not.

### Observability

| Name | Signature | Notes |
|---|---|---|
| `ObservabilityManager` | `ObservabilityManager(config=None, *, redactor=None, log_redactor=None, error_redactor=None, trace_redactor=None, metrics=None, tracing=None, error_tracking=None, logging=None)` | a passed sink instance wins over the config name; `.metrics`, `.tracing`, `.error_tracking`, `.logging` are the live handles |
| `ObservabilityManager.configure` | `configure(settings, *, service_name="")` | the Host calls it first; a second call in one run warns and returns |
| `ObservabilityManager.shutdown` | `shutdown()` | reverse setup order, never raises, resets to NullObjects |
| `ObsConfig` | `ObsConfig(metrics="prometheus", tracing="otel", error_tracking="sentry", logging="structlog")` | **the defaults are not `None`** — see rule 10 |
| `ObsSetupContext` | `service_name`, `app_version`, `environment`, `settings`, `redactor` | handed to each sink's `setup()` |
| `register_sink` | `register_sink(concern, backend, target)` | `target` is `"module.path:ClassName"`, imported lazily |
| `KeyRedactor` | `KeyRedactor(sensitive_keys=DEFAULT_SENSITIVE_KEYS, mask="[REDACTED]", *, safe_keys=frozenset())` | substring match on the key, walks dicts, lists and tuples |
| `ValueRedactor` | `ValueRedactor(masker, mask="[REDACTED]")` | lifts a `(str) -> str` masker over every string value; fails closed |
| `ChainRedactor` | `ChainRedactor(*redactors)` | applied left to right; key-based first is the convention |
| settings protocols | `LoggingSettingsProtocol`, `MetricsSettingsProtocol`, `TracingSettingsProtocol`, `ErrorTrackingSettingsProtocol` | the shape of each settings section |

Concerns not selected (or not configured in settings) stay NullObject sinks: emitters never
see `None`, and nothing is exported.

### Built-in entrypoints (no extra)

| Name | Signature |
|---|---|
| `DaemonEntrypoint` | `DaemonEntrypoint(func, *, kind="daemon", essential=True)` — `func(scope, stop)` loops until `stop` is set, in **one** long-lived unit scope |
| `OneShotEntrypoint` | `OneShotEntrypoint(func, *, kind="oneshot", essential=True)` — `func(scope)` runs once in a fresh unit scope, then returns, which stops the service |

### Adapters

Each subpackage needs its extra; importing one without it raises `ImportError` naming what
to install. `servicewright.adapters.warmers` and `.health` are the exception: they are
duck-typed on the client you pass in and soft-import their SDK, so they import with no extra
installed and raise (or degrade) at construction instead.

| Import | Public names |
|---|---|
| `servicewright.adapters.fastapi` | `FastApiEntrypoint`, `FastApiPlugin`, `HttpConfig`, `MiddlewareConfig`, `HealthConfig`, `CORSMiddlewareConfig`, `LoggingMiddlewareConfig`, `GZipMiddlewareConfig`, `CorrelationIdMiddlewareConfig`, `MetricsInstrumentatorConfig`, `UnitScopeDep`, `UnitScopeMiddleware`, `get_unit_scope`, `current_unit_scope`, `setup_default_exception_handlers`, `setup_metrics_instrumentator`, `LivenessResponse`, `ReadinessResponse`, `ProblemDetails`, `XUserId`, `IdempotencyKey`, `AuthorizationHeader`, `XFingerprintHeader`, `OtelBaggageSetter`, `StructlogSetter`, `get_default_context_setters`, `RoutesRegisterer`, `ConfigureApp` |
| `servicewright.adapters.litestar` | `LitestarEntrypoint`, `LitestarPlugin`, `LitestarConfig`, `HealthConfig`, `build_health_routes`, `UnitScopeMiddleware`, `get_unit_scope`, `current_unit_scope`, `RouteRegisterer`, `ConfigureApp` |
| `servicewright.adapters.grpc` | `GrpcEntrypoint`, `GrpcPlugin`, `GrpcConfig`, `ServicerRegisterer`, `InterceptorFactory`, `ServiceErrorInterceptor`, `UnhandledErrorInterceptor`, `GRPC_STATUS_BY_KIND`, `ERROR_CODE_TRAILING_METADATA`, `GrpcHealthBridge`, `UnitScopeInterceptor`, `current_unit_scope`, `GrpcServerMetricsRecorder`, `IDEMPOTENCY_KEY_METADATA`, `get_idempotency_key`, `get_client_ip`, `get_user_agent`, `get_client_context` |
| `servicewright.adapters.apscheduler4` (and `.apscheduler3`) | `SchedulerEntrypoint`, `SchedulerPlugin`, `ScheduledJob`, `ScheduledJobFunc`, `SchedulerJobMetricsRecorder`, `SchedulerError`, `DuplicateScheduleError` |
| `servicewright.adapters.dishka` | `DishkaContainer`, `DishkaScope` |
| `servicewright.adapters.settings` | `BaseServiceSettings`, `LoggingSettings`, `MetricsSettings`, `TracingSettings`, `ErrorTrackingSettings` |
| `servicewright.adapters.observability` | ABCs `MetricsSink`, `TracingSink`, `ErrorTrackingSink`, `LoggingSink`; backends `PrometheusMetricsSink`, `OtelTracingSink`, `SentryErrorTrackingSink`, `StructlogLoggingSink`, `StdlibLoggingSink` (each imported lazily on first access) |
| `servicewright.adapters.warmers` | `RedisWarmer`, `PostgresWarmer`, `KafkaProducerWarmer` — all `(client, …, timeout=10.0, priority=0, raise_on_failure=True)`; the package and the submodules both export them, extra or no extra, and `PostgresWarmer` is the only one that needs its SDK (`PostgresWarmupError` at construction without it) |
| `servicewright.adapters.health.postgres` / `.redis` | `PostgresHealthCheck(session_maker, timeout=5.0)`, `RedisHealthCheck(client, timeout=5.0)` — import from the submodule, not the package |
| `servicewright.testing` | `FakeContainer`, `FakeScope`, `FakeSettings`, `FakeEntrypoint` |

Entrypoint constructors, all keyword-only:

* `FastApiEntrypoint(config=None, routers=(), routes_registerer=None, middlewares=None,
  exception_handlers=None, default_exception_handlers=True, error_renderer=None,
  metrics=False, configure_app=None, kind="http", essential=True)`. `HttpConfig` defaults:
  `host="0.0.0.0"`, `port=8000`, `graceful_timeout=10.0`, probes at
  `/system/health/livez` and `/system/health/readyz`, docs at `/system/docs`,
  `redirect_slashes=False`. `MiddlewareConfig` defaults have unit scope, context, sentry,
  processing time, logging, correlation id, gzip and CORS all on.
* `LitestarEntrypoint(config=None, route_handlers=(), route_registerer=None,
  configure_app=None, kind="http", essential=True)`. `LitestarConfig` probes are
  `/system/livez` and `/system/readyz`; `unit_scope=True`.
* `GrpcEntrypoint(config, servicers, interceptors=(), interceptors_factory=None,
  context_setters=None, map_service_errors=True, enable_metrics=False,
  metrics_prefix=None, kind="grpc", essential=True)`. `GrpcConfig` defaults:
  `port=50051`, `grace_period=30.0`, `enable_reflection=False`, `enable_channelz=False`,
  `health_service_names=()`, `health_refresh_interval=5.0`. Interceptor chain, outermost first:
  `UnitScopeInterceptor`, `UnhandledErrorInterceptor`, metrics, yours,
  `ServiceErrorInterceptor`.
* `SchedulerEntrypoint(jobs, enable_metrics=False, metrics_prefix=None,
  kind="scheduler", essential=True)`; `ScheduledJob(id, func, trigger, args=(), kwargs={},
  max_instances=None, misfire_grace_time=None, coalesce=None)`.

Each `*Plugin` takes exactly the same arguments as its entrypoint and exposes `.entrypoint`.

## Rules that hold or break the code

1. **The Host owns the lifecycle; you own the container.** `create_container(settings)` is
   called by the Host, the application scope is opened by the Host and closed by it last.
   Do not open the app scope yourself, and do not manage warmup, DI or registration from a
   framework lifespan — the FastAPI app the entrypoint builds deliberately has none.
2. **`serve()` returns while still accepting work.** When `stop` is set, return; do not
   close the listener there. The Host flips readiness to false *first*, then calls
   `drain(grace)`, which is what closes intake, then `stop()`. Shutting down inside `serve`
   makes the drain window inert and kills the readiness endpoint before the load balancer
   has stopped routing.
3. **Readiness flips true only after every `bind()` returned, and false before any drain.**
   Between the flip to false and the first `drain()` the Host waits `drain_delay_seconds`
   (default `0.0`) with every entrypoint still accepting: the flip reaches the load balancer
   asynchronously, and at `0.0` the listener closes in the same tick, so whatever is still
   routed to the pod is refused. Set it to the cluster's endpoint propagation lag; it is
   skipped when the service never reached Ready. Anything that raises between opening the
   app scope and the post-start hooks aborts startup, tears down whatever was already bound,
   and propagates out of `run()`.
4. **Teardown is reverse bind order, and only for entrypoints that were bound.** An
   entrypoint whose `bind` raised halfway is still drained and stopped — it is recorded
   before the await, because a half-bind has already allocated something. A failing
   shutdown step is logged and skipped so the others still get their turn.
5. **Warmup's budget is a fixed 60 seconds and is not an `AppSpec` field.** Only
   `drain_delay_seconds` (0.0), `drain_grace_seconds` (30.0) and `cleanup_timeout_seconds`
   (10.0) are configurable. The delay is spent once, before the first drain; the drain step
   is allowed `drain_grace_seconds + 5`; every post-drain step gets `cleanup_timeout_seconds`.
   `terminationGracePeriodSeconds` must exceed the sum of all three. An overrun raises
   `DrainTimeoutError` / `CleanupTimeoutError` out of `run()` — but only when nothing else is
   already propagating.
6. **A stop signal during startup abandons startup at the next phase boundary.** Warmup is
   cancelled, `bind` is skipped, readiness stays false, the process goes straight to
   cleanup. A `pre_start` hook cannot assume `serve` will follow.
7. **`essential=True` is the default on both entrypoint bases.** An essential entrypoint
   that raises stops everything and its exception propagates out of `run()` after cleanup,
   so the process exits non-zero; an essential entrypoint that merely *returns* stops the
   service gracefully (that is exactly how `OneShotEntrypoint` works). A non-essential
   failure is logged and the rest keep serving. Set `essential=False` for a sidecar you do
   not want taking the API down.
8. **Signals are installed only when you do not pass `stop`.** `await service.run(settings,
   stop=my_event)` installs none — that is the embedding and test path, and you own
   SIGINT/SIGTERM. Call `install_signal_handlers(my_event)` yourself to get the same handlers
   back, and its return value to remove them. A second signal on the owned path exits
   immediately with `128 + signum`, skipping every remaining cleanup step.
9. **Which base class you extend decides who opens the unit scope.** `ServerEntrypoint`
   exposes no `unit_scope` at all, because the transport adapter's middleware or interceptor
   opens it per request. `ScopedEntrypoint.unit_scope()` is the only sanctioned per-unit API
   and raises `RuntimeError` if called before `bind()`.
10. **Batteries are opt-in, and their absence is silent — but `ObsConfig()` is not empty.**
    A bare `AppSpec` gets an `ObservabilityManager()` whose four sinks are NullObjects:
    counters increment, spans open, nothing is exported and nothing complains. Selecting is
    `ObservabilityManager(ObsConfig(...))` — and `ObsConfig`'s field defaults are
    `"prometheus"`, `"otel"`, `"sentry"`, `"structlog"`, so `ObsConfig()` selects all four.
    Pass `None` for the concerns you do not want.
11. **A concern is active only when a backend is selected *and* the settings section is
    present** (error tracking additionally needs a non-empty `dsn`). When both hold and the
    extra is missing, bootstrap hard-raises `ImportError` naming the extra. With
    `BaseServiceSettings` defaults that means `ObsConfig()` requires
    `servicewright[metrics,observability]`; its `tracing` section is `None` on purpose,
    because a present section installs a tracer provider.
12. **Settings are read structurally, never inherited.** The kernel reads `settings.logging`,
    `.metrics`, `.tracing`, `.error_tracking` and `get_app_version()` off whatever object you
    pass; a misspelled field silently becomes a default. `servicewright[settings]` ships the
    contract as pydantic models so it cannot drift.
13. **Warmers run in priority groups — lower `priority` first, equal priorities in
    parallel** — and the Host always calls the engine with `raise_on_failure=True`. A warmer
    that must not abort startup sets `raise_on_failure=False` on **itself**; a failing group
    stops the groups after it.
14. **Liveness ignores your health checks entirely.** It is healthy while the loop runs.
    Readiness is the `ready` flag AND every registered check; a check that raises counts as
    a failure, and `add_check` refuses a duplicate name with `ValueError`. Use
    `readiness_cache_ttl` when probes are frequent enough to hammer a dependency.
15. **The probe paths differ per adapter.** FastAPI serves `/system/health/livez` and
    `/system/health/readyz`; Litestar serves `/system/livez` and `/system/readyz`; gRPC
    pushes readiness onto the standard health service every `health_refresh_interval`
    seconds (5.0, `0` disables polling). Configure the Kubernetes probe for the entrypoint
    you actually run.
16. **`ServiceError(public=False)` masks everything at the transport**: the client gets a
    generic `internal_error` 500 (or `INTERNAL` over gRPC) and the real code only reaches the
    log. A subclass's `code` is derived from its class name, so renaming the class is a wire
    change. **An exception that is not a `ServiceError` is masked identically**, by
    `UnhandledErrorMiddleware` over HTTP and `UnhandledErrorInterceptor` over gRPC — both
    installed unconditionally, neither removed by `default_exception_handlers=False` or
    `map_service_errors=False`, which only stop the mapping of the errors you declared. So a
    caller cannot tell an error you hid from one you never knew about, and no exception text
    reaches the wire. Do not catch-and-return an exception in a handler or servicer to "make the
    error nicer": that is the one way to get its message back onto the wire.
17. **One unit scope per request, not two.** If the DI framework's own integration owns the
    request scope (dishka's `setup_dishka`), switch servicewright's off —
    `MiddlewareConfig(unit_scope=False)` or `LitestarConfig(unit_scope=False)` — otherwise
    `DishkaContainer.unit_scope` raises `RuntimeError`. With the middleware off,
    `current_unit_scope()` and `UnitScopeDep` raise `LookupError`; resolve through the other
    integration instead.
18. **A scheduled job is scope-first.** `ScheduledJob.func` is called as
    `func(scope, *args, **kwargs)` where `scope` is that run's `UnitScopeProtocol`. Duplicate
    job ids raise `DuplicateScheduleError` at bind. A job that raises is logged and
    swallowed — it never crashes the scheduler loop and never stops the service, so anything
    that must page someone has to do it from inside the job. The `apscheduler3` and `apscheduler4`
    adapters have the same public names and can never be installed together — one
    distribution, incompatible majors; the only public difference is `coalesce` (`bool` in
    3.x, `CoalescePolicy` in 4.x).
19. **The FastAPI `metrics=` flag and `settings.metrics.enabled` are different things.**
    `FastApiEntrypoint(metrics=True)` adds the in-app `/system/metrics` route;
    `settings.metrics.enabled` starts the sink's own standalone exposition server on its own
    port. Neither implies the other.
20. **gRPC reflection and channelz are off by default and unauthenticated when on**, on the
    same port as production traffic. The effective gRPC drain is
    `min(host drain_grace_seconds, config.grace_period)`.

## Common mistakes

```python
# WRONG — shutting the server down inside serve()
async def serve(self, *, stop: asyncio.Event) -> None:
    await stop.wait()
    await self._server.shutdown()      # drain(grace) now has nothing left to do

# RIGHT — return still accepting; the Host calls drain() then stop()
async def serve(self, *, stop: asyncio.Event) -> None:
    await self._server_task_until(stop)

async def drain(self, grace: float) -> None:
    await self._server.close_listener_and_wait(grace)

async def stop(self) -> None:
    await self._server.kill()
```

```python
# WRONG — trusting the readiness flip alone to keep a rollout clean
spec = AppSpec(service_name="orders", create_container=build_container)
# readiness goes red and the listener closes in the same tick; whatever the load
# balancer still routes during endpoint propagation is refused

# RIGHT — hold the listeners open for the propagation lag, and budget for it
spec = AppSpec(service_name="orders", create_container=build_container, drain_delay_seconds=5.0)
# terminationGracePeriodSeconds > 5 + drain_grace_seconds + cleanup_timeout_seconds
```

```python
# WRONG — a lifespan that owns the container, next to a Host that also owns it
app = FastAPI(lifespan=my_container_lifespan)
entrypoint = FastApiEntrypoint(configure_app=lambda app, ctx: None)

# RIGHT — the Host owns bootstrap, warmup and the app scope; the entrypoint builds the app
spec = AppSpec(service_name="orders", create_container=build_container)
entrypoint = FastApiEntrypoint(routers=(router,))
```

```python
# WRONG — expecting metrics and JSON logs from a bare AppSpec
spec = AppSpec(service_name="orders", create_container=build_container)
# every sink is a NullObject: nothing is exported and nothing raises

# RIGHT — select the backends, install their extras
spec = AppSpec(
    service_name="orders",
    create_container=build_container,
    observability=ObservabilityManager(ObsConfig(metrics="prometheus", logging="structlog",
                                                 tracing=None, error_tracking=None)),
)
```

```python
# WRONG — a server entrypoint opening its own per-request scope
class MyServer(ServerEntrypoint):
    async def handle(self, request):
        async with self.unit_scope({"request": request}):   # AttributeError
            ...

# RIGHT — read the scope the adapter's middleware already opened
from servicewright.adapters.fastapi import UnitScopeDep

async def handler(scope: UnitScopeDep):
    use_case = await scope.get(CreateOrder)
```

```python
# WRONG — a scheduled job that forgets it is scope-first
async def sweep() -> None: ...
ScheduledJob(id="sweep", func=sweep, trigger=trigger)   # TypeError at fire time

# RIGHT
async def sweep(scope: UnitScopeProtocol) -> None:
    repo = await scope.get(OrderRepository)
    await repo.delete_expired()
```

```python
# WRONG — passing a stop event and still expecting SIGTERM to be handled
await service.run(settings, stop=asyncio.Event())   # no signal handlers are installed

# RIGHT — let the runtime own the signals
run_sync(service, settings)
```

## Errors

Every runtime error derives from `ServiceWrightError`:

| Exception | Raised when |
|---|---|
| `ServiceWrightError` | base class for everything the runtime raises |
| `WarmupError` | a warmer failed (or the engine's own timeout elapsed) with failure raising enabled |
| `WarmupTimeoutError` | warmup did not finish inside its 60-second budget (also a `TimeoutError`) |
| `DrainTimeoutError` | an entrypoint's `drain` outlived `drain_grace_seconds + 5` (also a `TimeoutError`) |
| `CleanupTimeoutError` | a `stop()`, hook or observability flush outlived `cleanup_timeout_seconds` (also a `TimeoutError`) |
| `RedisWarmupError`, `PostgresWarmupError`, `KafkaProducerWarmupError` | the matching built-in warmer failed; all subclass `WarmupError` |

`ServiceError` is **not** one of these: it is the base class for the business errors your
code raises, and the transports render it rather than letting it escape.

Adapter-local and stdlib exceptions you will meet:

| Exception | Raised when |
|---|---|
| `SchedulerError` / `DuplicateScheduleError` | in `servicewright.adapters.apscheduler4` / `.apscheduler3`; two jobs share an `id` |
| `ImportError` | an adapter, sink or settings model imported without its extra — the message names the extra |
| `RuntimeError` | `unit_scope()` before `bind()`; `serve()` before `bind()`; two request scopes opened for one request |
| `LookupError` | `current_unit_scope()` / `get_unit_scope()` outside a request whose unit-scope middleware is installed |
| `ValueError` | a duplicate health check name, an unknown observability backend name, an unknown `loop=`, a non-positive warmer timeout, `allow_credentials` with a wildcard CORS origin |

## Documentation map

Fetch a page when the task is the one named beside it.

| Page | Read it when |
|---|---|
| [Home](index.md) | the pitch, and which of the archetypes below you are building |
| [Installation](getting-started/installation.md) | choosing extras, Python versions, what each one pulls in |
| [Your first service](getting-started/first-service.md) | writing a whole service with the kernel alone |
| [Tutorial](getting-started/tutorial.md) | an HTTP API and a cron job in one process, end to end |
| [Architecture](concepts/architecture.md) | the six nouns, the two layers, the dependency rule the CI enforces |
| [Lifecycle](concepts/lifecycle.md) | phase order, budgets, signals, exit codes, hooks — the correctness page |
| [Entrypoints](concepts/entrypoints.md) | the four methods, `kind`, `essential`, which base to extend |
| [Dependency injection](concepts/dependency-injection.md) | the two scope tiers and what belongs in each |
| [Settings](concepts/settings.md) | the settings shape, the shipped models, environment variables |
| [Errors](concepts/errors.md) | `ServiceError`, masking, custom renderers, the per-transport mapping |
| [Request context](concepts/context.md) | correlation ids, propagation, context setters |
| [Health checks](concepts/health.md) | liveness vs readiness, writing a check, caching probes |
| [Warmup](concepts/warmup.md) | priority groups, fail-fast, what to prime before readiness |
| [Observability](concepts/observability.md) | selecting backends, the four concerns, redaction, instruments |
| [Plugins](concepts/plugins.md) | packaging wiring as `on_register` |
| [Adapters overview](adapters/overview.md) | which adapter family solves the problem in front of you |
| [FastAPI](adapters/fastapi.md) | the HTTP entrypoint, its middleware stack, probes, per-request scope |
| [Litestar](adapters/litestar.md) | the lean HTTP entrypoint and how it differs from the FastAPI one |
| [gRPC](adapters/grpc.md) | servicers, interceptors, the health bridge, error mapping, reflection |
| [Scheduler](adapters/scheduler.md) | cron and interval jobs, triggers, the 3.x/4.x split |
| [Daemon and one-shot](adapters/daemon-and-oneshot.md) | loops and batch jobs with nothing installed |
| [dishka](adapters/dishka.md) | binding dishka, and who owns the request scope |
| [Observability backends](adapters/observability-backends.md) | prometheus, OTel, Sentry, structlog, stdlib, or writing your own sink |
| [Infrastructure](adapters/infrastructure.md) | the ready-made Redis / Postgres / Kafka warmers and checks |
| [Project layout](blueprints/project-layout.md) | where servicewright is allowed to appear in your package tree |
| [HTTP API service](blueprints/http-api.md) | a production-shaped HTTP service to copy |
| [gRPC service](blueprints/grpc-service.md) | the same service with a gRPC front |
| [Background worker](blueprints/worker.md) | several jobs side by side in one process |
| [Batch job](blueprints/batch-job.md) | run-once work that still needs the whole lifecycle |
| [Writing an entrypoint](guides/custom-entrypoint.md) | implementing the four methods for a transport that has no adapter |
| [Testing](guides/testing.md) | the fakes, asserting lifecycle order, testing entrypoints |
| [Kubernetes](operations/kubernetes.md) | probes, `drain_delay_seconds` in place of a `preStop` sleep, `terminationGracePeriodSeconds`, exit codes |
| [Production checklist](operations/checklist.md) | the once-per-service pass before shipping |
| [Runbooks](operations/runbooks.md) | a symptom in production: never ready, hung drain, silent metrics |
| [API reference: servicewright](reference/servicewright.md) | an exact signature or docstring from the top-level package — HTML only, see above |
| [API reference: adapters](reference/adapters.md) | the same for every adapter subpackage — HTML only |
| [API reference: testing](reference/testing.md) | the same for the test doubles — HTML only |
| [Changelog](changelog.md) | what changed between versions |
