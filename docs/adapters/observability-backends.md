# Observability backends

Each of the four concerns has an author-facing ABC in `servicewright.adapters.observability`, and
one or more concrete backends behind an extra. Selection happens in
[`ObsConfig`](../concepts/observability.md#selecting-backends).

## Prometheus (metrics)

```bash
pip install "servicewright[metrics]"
```

```python
ObsConfig(metrics="prometheus")
```

An instrument factory over a `CollectorRegistry` — by default the process-global one, so
out-of-process emitters (database pools, client libraries) land in the same scrape.

When `settings.metrics.enabled` is true it also starts a **standalone** WSGI exposition server on
`settings.metrics.host:port`, in a daemon thread. That exists for socket-less processes — a pure
cron worker or Kafka consumer has no HTTP port of its own to expose metrics on.

`shutdown()` stops that server deterministically and joins the thread with a 5-second timeout.

!!! info "Instruments are cached per registry, not per sink"

    Prometheus collectors are owned by the registry they register into, so constructing the same
    metric name twice raises a duplicate-timeseries error. The cache is keyed by registry, which
    means two entrypoints, two `AppSpec`s or a second `Host` in one process all share the same
    instrument instead of crashing.

## OpenTelemetry (tracing)

```bash
pip install "servicewright[observability]"
pip install "servicewright[fastapi-tracing]"    # + HTTP request spans
```

```python
ObsConfig(tracing="otel")
```

`setup()` installs the **global** tracer provider with:

- a resource carrying `service.name` (from `settings.tracing.service_name`, falling back to
  `AppSpec.service_name`) and `service.version` from `get_app_version()`;
- a `ParentBased(TraceIdRatioBased(sample_ratio))` sampler;
- an OTLP **gRPC** span exporter, when `collector_url` is set, behind a `BatchSpanProcessor`;
- optionally a console exporter, for local debugging.

Mint spans through the sink rather than importing OpenTelemetry yourself:

```python
tracer = ctx.observability.tracing.tracer("orders.use_cases")

with tracer.start_as_current_span("charge_card") as span:
    span.set_attribute("order.id", order_id)
```

With tracing disabled that is a null object, and the `with` block still works.

## Sentry (error tracking)

```bash
pip install "servicewright[sentry]"
```

```python
ObsConfig(error_tracking="sentry")
```

`setup()` calls `sentry_sdk.init` with the DSN, environment, release (your app version) and
sampling rates from `settings.error_tracking`. The [redactor](../concepts/observability.md#redaction)
is installed as `before_send`, so every outgoing event is filtered — including stack-frame locals
and breadcrumb payloads.

### Everything else `sentry_sdk.init` takes

The settings section models the deployment facts. Anything else — `ignore_errors`, a
`before_send` that drops, `before_send_transaction`, `traces_sampler`, `integrations`,
`send_default_pii` — goes through the sink's constructor, via
[instance injection](../concepts/observability.md#instance-injection):

```python
from servicewright.adapters.observability import SentryErrorTrackingSink

observability = ObservabilityManager(
    ObsConfig(error_tracking="sentry"),
    error_tracking=SentryErrorTrackingSink(ignore_errors=[DomainError]),
)
```

A `before_send` given this way is composed with the redactor, not replaced by it: it runs first,
with the full `(event, hint)` — `hint["exc_info"]` for a captured exception,
`hint["log_record"]` for one that arrived through the logging integration — and may return
`None` to drop the event; whatever survives is redacted.

```python
def keep(event: dict, hint: dict) -> dict | None:
    exc = hint.get("exc_info", (None, None, None))[1]
    return None if isinstance(exc, DomainError) else event


SentryErrorTrackingSink(before_send=keep)
```

Health probes are the other usual case. With Sentry performance on, kubelet's `livez` / `readyz`
polling becomes a transaction every few seconds per pod; drop the `/system` namespace with
`before_send_transaction` (or a `traces_sampler` returning `0` for it):

```python
def drop_probes(event: dict, hint: dict) -> dict | None:
    return None if event.get("transaction", "").startswith("/system/") else event


SentryErrorTrackingSink(before_send_transaction=drop_probes)
```

`dsn`, `environment`, `release`, the sample rates and `debug` are settings-driven and the sink
rejects them at construction, so a value cannot come from two places.

The reporting seam:

```python
reporter = ctx.observability.error_tracking.reporter()

reporter.capture_exception(exc)
reporter.add_breadcrumb("charging card", category="payment")
reporter.set_tags(tenant=tenant_id)
```

`shutdown()` flushes queued events with a short timeout.

## structlog (logging)

```bash
pip install "servicewright[observability]"
```

```python
ObsConfig(logging="structlog")
```

Configures structlog **and** routes the standard library root logger through the same processor
chain, so third-party library logs come out in the same format as yours. The chain adds
contextvars merging, logger name, level, an ISO-8601 UTC timestamp, stack info and exception
formatting — then renders JSON (`use_json=True`) or the pretty console renderer.

The redactor runs as a processor over every event dict.

Because contextvars merging is in the chain, anything bound into the
[context store](../concepts/context.md) shows up on every line automatically:

```json
{"event": "Request finished", "request_id": "9f2c...", "status_code": 200, "level": "info"}
```

## stdlib (logging)

```python
ObsConfig(logging="stdlib")
```

No extra required. One root handler, level from settings, and either plain text or one JSON object
per line. The JSON formatter includes the record's `extra` fields and runs them through the
redactor.

Use it when you do not want structlog in your dependency tree, or when a platform log collector
already expects a specific plain format.

## Writing your own backend

Subclass the matching ABC. The `backend` attribute is a label used in logs.

```python
from servicewright.adapters.observability import MetricsSink


class StatsdMetricsSink(MetricsSink):
    backend = "statsd"

    def setup(self, ctx) -> None:
        settings = ctx.settings.metrics
        self._client = StatsdClient(settings.host, settings.port)

    def shutdown(self) -> None:
        self._client.close()

    def counter(self, name, description, label_names=()):
        return StatsdCounter(self._client, name)

    def histogram(self, name, description, label_names=(), buckets=None):
        return StatsdHistogram(self._client, name)

    def gauge(self, name, description, label_names=()):
        return StatsdGauge(self._client, name)
```

Then either register it by name:

```python
from servicewright import register_sink

register_sink("metrics", "statsd", "myapp.observability:StatsdMetricsSink")
# ObsConfig(metrics="statsd")
```

…or inject the instance and skip the registry entirely:

```python
ObservabilityManager(metrics=StatsdMetricsSink())
```

Registering by name keeps `main.py` free of SDK imports — the module is loaded only if the backend
is actually selected. Injecting an instance is better when the backend needs constructor
arguments.

### The four ABCs

| ABC | Must implement |
| --- | --- |
| `MetricsSink` | `setup`, `shutdown`, `counter`, `histogram`, `gauge` |
| `TracingSink` | `setup`, `shutdown`, `tracer` (+ optional `instrument_fastapi`) |
| `ErrorTrackingSink` | `setup`, `shutdown`, `reporter` |
| `LoggingSink` | `setup`, `shutdown` |

The objects they mint satisfy small protocols:

```python
class CounterProtocol(Protocol):
    def inc(self, amount: float = 1.0, **labels: str) -> None: ...


class HistogramProtocol(Protocol):
    def observe(self, value: float, **labels: str) -> None: ...


class GaugeProtocol(Protocol):
    def set(self, value: float, **labels: str) -> None: ...
    def inc(self, amount: float = 1.0, **labels: str) -> None: ...
    def dec(self, amount: float = 1.0, **labels: str) -> None: ...
```

Two rules worth honouring:

1. **Repeated requests for the same metric name must return the same instrument.** Callers mint
   instruments at bind time and may do so more than once.
2. **`setup()` runs before the DI container exists.** Everything you need has to be reachable from
   `ctx.settings`.

## Metrics recorders

Transport adapters own their metric names and compose recorders from the generic instruments:

```python
from servicewright.adapters.grpc import GrpcServerMetricsRecorder

recorder = GrpcServerMetricsRecorder(ctx.observability.metrics, prefix="myapp")
recorder.record_request(service, method, status, grpc_code, duration)
```

The scheduler's [`SchedulerJobMetricsRecorder`](scheduler.md#metrics) is the same shape, plus a
`track(job_id)` context manager that accounts a whole run — in-progress up and down, duration,
outcome from the exception — around its body.

Doing your own is the same shape:

```python
class OutboxMetricsRecorder:
    def __init__(self, sink: MetricsSinkProtocol) -> None:
        self._published = sink.counter("outbox_published_total", "Published events", ("topic",))
        self._lag = sink.histogram("outbox_lag_seconds", "Publish lag", ("topic",))

    def record(self, topic: str, lag: float) -> None:
        self._published.inc(topic=topic)
        self._lag.observe(lag, topic=topic)
```

Mint it once at bind time, use it for the life of the entrypoint. It works over any backend,
including none at all.
