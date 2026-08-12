# Configuration

## Extras

Every framework binding lives behind an extra; the bare install has zero dependencies.

| Extra | Brings | Enables |
| --- | --- | --- |
| `fastapi` | fastapi, uvicorn, deadline-budget, prometheus-fastapi-instrumentator | `adapters.fastapi` |
| `litestar` | litestar, uvicorn | `adapters.litestar` |
| `grpc` | `grpc-server-kit[reflection,channelz,health]` | `adapters.grpc` |
| `apscheduler4` / `apscheduler3` | apscheduler 4.x / 3.x (mutually exclusive) | `adapters.apscheduler4` / `adapters.apscheduler3` |
| `dishka` | dishka | `adapters.dishka` |
| `metrics` | prometheus-client | the `prometheus` metrics backend |
| `observability` | opentelemetry-sdk + OTLP gRPC exporter, structlog | the `otel` tracing and `structlog` logging backends |
| `fastapi-tracing` | the above + opentelemetry-instrumentation-fastapi | HTTP request spans for the FastAPI entrypoint |
| `sentry` | sentry-sdk | the `sentry` error-tracking backend |
| `redis` / `postgres` / `kafka` | client library | the matching warmers and health checks |
| `all` | everything except the conflicting `apscheduler3` | the full runtime |

## Settings: sections named by concern

The Host reads observability configuration from your settings object
(`BaseServiceSettingsProtocol`). Sections are named by **concern**, never by vendor — which
backend consumes a section is decided by `ObsConfig`:

| Section | Minimal shape | Consumed by |
| --- | --- | --- |
| `logging` | `level`, `use_json` | logging backends (structlog / stdlib) |
| `metrics` | `enabled`, `host`, `port`, `prefix` | metrics backends (standalone exposition) |
| `tracing` | `service_name`, `collector_url`, `sample_ratio`, `insecure`, `enable_console_exporter`, `excluded_urls` | tracing backends |
| `error_tracking` | `dsn`, `environment`, `traces_sample_rate`, `profiles_sample_rate`, `debug` | error-tracking backends |

A section set to `None` disables the concern (its sink stays a NullObject). A backend may read
extra fields off its section via `getattr` — the protocols declare only the minimum.

## Observability: `ObsConfig` and the manager

```python
from servicewright import AppSpec, KeyRedactor, ObsConfig, ObservabilityManager

spec = AppSpec(
    service_name="orders",
    create_container=build_container,
    observability=ObservabilityManager(
        ObsConfig(
            metrics="prometheus",       # open string: built-ins or anything you register
            tracing="otel",
            error_tracking="sentry",
            logging="structlog",        # or "stdlib" for the zero-dependency fallback
        ),
        redactor=KeyRedactor(),         # masks password/token/... in BOTH logs and sentry events
    ),
)
```

Rules:

- **Selection vs activation.** `ObsConfig` picks *which implementation to use when the concern is
  configured*; the settings decide whether it is active. Selected + configured + missing extra →
  a hard, friendly `ImportError` at Bootstrap. Unknown backend name → `ValueError` listing the
  registered backends. Disabled/unconfigured → NullObject, emitters never see `None`.
- **Instance injection** bypasses the registry (and the settings gate — your sink's `setup()`
  decides): `ObservabilityManager(metrics=MyStatsdSink())`.
- The default `ObservabilityManager()` (no config) disables everything — a bare `AppSpec` runs
  with zero observability side effects.

## HTTP entrypoint (`HttpConfig` / `MiddlewareConfig`)

```python
from servicewright.adapters.fastapi import FastApiEntrypoint, HttpConfig, MiddlewareConfig

http = FastApiEntrypoint(
    config=HttpConfig(host="0.0.0.0", port=8000, graceful_timeout=10.0),
    routers=(router,),
    middlewares=MiddlewareConfig(
        context=True,              # contextvars propagation (request_id/user_id/trace_id)
        context_setters=None,      # None = defaults (structlog + OTel baggage if installed)
        sentry=True,               # Sentry scope enrichment from the request context
        processing_time=True,      # X-Process-Time response header
    ),
    metrics=True,                  # in-app /system/metrics via the instrumentator
)
```

The middleware stack order (outermost first): UnitScope → context → unhandled-error → sentry →
processing-time → logging → gzip → CORS → your `custom` middlewares — so every layer and the
handler run inside a live per-request `UnitScope`.

`ContextMiddleware` is the single owner of the request id: it takes the client's `X-Request-ID`
(when it is log-safe), mints one otherwise, binds it into the context store and echoes it back on
the response — so the id in your logs, the id propagated downstream and the id the caller sees are
the same value. The unhandled-error layer sits inside it on purpose: a masked 500 still carries
that id, both on the wire and on the logged traceback.

## gRPC entrypoint (`GrpcConfig`)

```python
from servicewright.adapters.grpc import GrpcConfig, GrpcEntrypoint

grpc_ep = GrpcEntrypoint(
    config=GrpcConfig(port=50051, enable_reflection=True, enable_channelz=True),
    servicers=register_servicers,        # (server, ctx) -> None
    enable_metrics=True,                 # RPC metrics through the app's metrics sink
    metrics_prefix=None,
)
```

`enable_metrics=True` records `grpc_requests_total{service,method,status,grpc_code}` and
`grpc_request_duration_seconds{service,method}` through whatever metrics backend the app
configured (a NullObject when metrics are off — the flag is always safe).

## Scheduler entrypoint

```python
from servicewright.adapters.apscheduler4 import ScheduledJob, SchedulerEntrypoint

cron = SchedulerEntrypoint(jobs=[
    ScheduledJob(
        id="sweep",
        func=sweep,                      # async def sweep(scope, *args, **kwargs)
        trigger=IntervalTrigger(minutes=5),
        max_instances=1,
        misfire_grace_time=30.0,
    ),
])
```

`adapters.apscheduler3` ships the same public names for APScheduler 3.x environments — migrating
between majors is one import line + one extra.
