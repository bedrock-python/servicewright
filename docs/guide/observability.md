# Observability

Four concerns — metrics, tracing, logging, error tracking — are protocols in the kernel with
selectable, extra-gated backends. `import servicewright` pays for none of them: a backend is
imported only when it is resolved.

## Selecting backends

```python
from servicewright import AppSpec, KeyRedactor, ObsConfig, ObservabilityManager

spec = AppSpec(
    service_name="orders",
    create_container=build_container,
    observability=ObservabilityManager(
        ObsConfig(metrics="prometheus", tracing="otel", error_tracking="sentry", logging="structlog"),
        redactor=KeyRedactor(),      # threaded into BOTH logging and error tracking
    ),
)
```

Selecting a backend says *which implementation to use when the concern is configured*; whether it
is active is decided by your settings (`settings.error_tracking.dsn` present, `settings.tracing`
present, ...). A concern that is unselected or unconfigured stays a NullObject — safe to call,
does nothing. A selected backend whose extra is missing fails fast at bootstrap.

| Concern | Backend | Extra |
| --- | --- | --- |
| metrics | `prometheus` | `metrics` |
| tracing | `otel` | `observability` (+ `fastapi-tracing` for HTTP spans) |
| error tracking | `sentry` | `sentry` |
| logging | `structlog`, `stdlib` | `observability`, — |

## Third-party backends

A backend is one module plus one registration — no kernel change:

```python
from servicewright import register_sink

register_sink("metrics", "statsd", "myapp.observability:StatsdMetricsSink")
# then: ObsConfig(metrics="statsd")
```

Or skip the registry entirely and inject a ready instance (it wins over the config name and decides
for itself what to do with settings):

```python
ObservabilityManager(metrics=StatsdMetricsSink(host="127.0.0.1"))
```

## Instruments, not metric names

Backends are instrument factories: they mint counters and histograms and know nothing about
transports. Metric **names** live with their owner — the transport adapter that mints them — so a
new backend automatically serves every transport, and a new transport serves every backend:

```python
recorder = GrpcServerMetricsRecorder(ctx.observability.metrics)   # owns grpc_requests_total
```

Prometheus instruments are cached per registry, so a second service in the same process reuses the
existing collectors instead of failing with a duplicate-timeseries error.

## Redaction

`KeyRedactor` masks values whose key contains a sensitive fragment, and walks the whole structure —
nested dicts *and* values inside lists. That matters where it counts: a Sentry event keeps
stack-frame locals under `exception.values[i].stacktrace.frames[j].vars` and breadcrumb payloads
under `breadcrumbs.values[i].data`.

```python
KeyRedactor(sensitive_keys={"password", "api_key", "ssn"}, mask="[REDACTED]")
```

Any callable `dict -> dict` works in its place.

## Logging

The logging sink owns the destination, level and format of **every** line, including the adapter's
own request logs — those go through the standard library logger, not around it, so
`settings.logging.level` filters them and `use_json` formats them like everything else. With the
logging concern switched off, the root logger is left untouched and the request lines follow
whatever your application configured.

Correlation is automatic: `ContextMiddleware` (HTTP) and `UnitScopeInterceptor` (gRPC) bind
`request_id` / `user_id` / `tenant_id` / `trace_id` into the transport-neutral context store and
push them into structlog contextvars and OTel Baggage.

## Lifecycle

The Host configures observability first (so bootstrap failures are observable) and shuts it down
last, off the event loop and under a budget. `shutdown()` returns the manager to its pre-configure
state, so a second run of the same `AppSpec` in one process gets a live stack again rather than a
silently dead one.
