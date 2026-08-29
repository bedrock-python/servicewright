# Settings

servicewright reads a small, fixed set of things off a settings object that **you** own and
construct. The kernel never loads a `.env`, never reads `os.environ`, and never defines a class
you have to inherit from: it checks shape, not ancestry. The `settings` extra ships that shape as
ready-made [pydantic-settings models](#shipped-models), so you do not have to transcribe it.

## The protocol

```python
class BaseServiceSettingsProtocol(Protocol):
    logging: LoggingSettingsProtocol | None
    metrics: MetricsSettingsProtocol | None
    tracing: TracingSettingsProtocol | None
    error_tracking: ErrorTrackingSettingsProtocol | None

    def get_app_version(self) -> str: ...
```

Four optional sections and a version. That is all the kernel asks for.

Sections are named by **concern**, never by vendor. There is no `sentry` section — there is an
`error_tracking` section, and `ObsConfig` decides that Sentry is the backend reading it. Swapping
Sentry for something else does not touch your settings class.

## The sections

Each table lists the **default** the built-in backend falls back to when the field is missing
from your section. The [shipped models](#shipped-models) carry the same values, so a model of
your own that uses them behaves exactly like the shipped one.

### `logging`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `level` | `str` | `"INFO"` | Root log level: `DEBUG`, `INFO`, ... |
| `use_json` | `bool` | `True` | JSON lines vs human-readable console output |

### `metrics`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `enabled` | `bool` | `False` | Start a standalone exposition server |
| `host` | `str` | `"0.0.0.0"` | Bind host for that server |
| `port` | `int` | `9090` | Bind port for that server |
| `prefix` | `str \| None` | `None` | Metric name prefix, for backends that use one |

`host` and `port` are read only when `enabled` is true and have no backend fallback; their
defaults are the shipped model's.

!!! note

    The built-in Prometheus backend does not apply `prefix` itself. Metric names belong to the
    recorder that owns them, so prefixes are set there — for example
    `GrpcEntrypoint(metrics_prefix="myapp")`. The field exists for backends that prefix
    globally.

### `tracing`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `service_name` | `str` | `""` | Resource service name; empty falls back to `AppSpec.service_name` |
| `collector_url` | `str \| None` | `None` | OTLP endpoint; `None` means no exporter |
| `sample_ratio` | `float` | `1.0` | Ratio for the parent-based sampler |
| `insecure` | `bool` | `True` | Plaintext OTLP connection |
| `enable_console_exporter` | `bool` | `False` | Also print spans to stdout |
| `excluded_urls` | `str \| None` | `None` | Comma-separated paths to skip |

### `error_tracking`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `dsn` | `str \| None` | `None` | Reporting endpoint; **empty means the concern is off** |
| `environment` | `str` | `""` | Environment tag; empty falls back to `settings.environment` |
| `traces_sample_rate` | `float` | `0.0` | Performance-trace sampling |
| `profiles_sample_rate` | `float` | `0.0` | Profiling sampling |
| `debug` | `bool` | `False` | SDK debug mode |

### Narrowing a section

Sections may declare types **narrower** than the tables above: a `Literal` level, a bounded
`sample_ratio`, a `port` newtype. Section members are read-only properties and nothing in
servicewright writes to them, so a narrowed model still satisfies the protocol. `LogLevelStr` is
exported for the log level:

```python
from servicewright.core.observability import LogLevelStr


class LoggingSettings(BaseModel):
    level: LogLevelStr = "INFO"
    use_json: bool = True
```

### Optional extras

- `settings.environment` — if present, it is used as the environment for observability setup.
  Otherwise `error_tracking.environment` is used.
- Backends may read **additional** fields off their own section via `getattr`. The protocols
  declare the minimum, not the maximum, so a custom backend can carry its own configuration in
  the same section.

## `None` disables a concern

```python
@dataclass(frozen=True)
class Settings:
    logging: LoggingSettings | None = LoggingSettings()
    metrics: None = None          # off
    tracing: None = None          # off
    error_tracking: None = None   # off

    def get_app_version(self) -> str:
        return "1.0.0"
```

A concern that is `None` gets a null-object sink: emitters can call it freely and nothing happens.
Your code never has to check whether metrics are configured.

Error tracking is special: the section may exist while `dsn` is empty, which also counts as off.

## Shipped models

Install the `settings` extra and the four sections plus the composite come as pydantic models,
carrying the defaults above:

```bash
pip install "servicewright[settings]"
```

```python
from pydantic_settings import SettingsConfigDict

from servicewright.adapters.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    model_config = SettingsConfigDict(env_file=".env")

    app_version: str = "1.4.0"
    database_dsn: str
```

```bash
LOGGING__LEVEL=DEBUG TRACING__COLLECTOR_URL=otel:4317 python main.py
```

`BaseServiceSettings` is a `pydantic_settings.BaseSettings` with `env_nested_delimiter="__"`, the
four sections, `environment` (default `"local"`), `app_version` (default `"0.0.0"`) and a
`get_app_version()` returning it. Your `model_config` is merged with the base one, so an
`env_file` or an `env_prefix` of yours keeps the `__` nesting.

### What is on by default

| Section | Default | Why |
| --- | --- | --- |
| `logging` | `LoggingSettings()` — `INFO`, JSON | you want logs |
| `metrics` | `MetricsSettings()` — sink on, exposition server off | inert until something records |
| `error_tracking` | `ErrorTrackingSettings()` — no `dsn`, so off | switches on from `ERROR_TRACKING__DSN` alone |
| `tracing` | `None` | a present section installs a tracer provider; `TRACING__COLLECTOR_URL=...` creates it |

A section typed `TracingSettings | None` is built from its nested variables the moment one is
set, so `TRACING__COLLECTOR_URL=otel:4317` is all it takes to switch tracing on.

A present section still activates its backend at bootstrap, which needs that backend's extra
installed — `observability` for `structlog` and `otel`, `metrics` for `prometheus`. Otherwise set
the section to `None` or select another backend in [`ObsConfig`](observability.md). The `settings`
extra itself pulls in nothing but pydantic-settings.

### Validation instead of silent fallbacks

The models validate what the backends assume: the log level must be one of `LogLevelStr`
(case-insensitively, so `LOGGING__LEVEL=debug` is fine while `warn` is a load-time error rather
than a silent `INFO`), the sample ratios are bounded to `[0, 1]`, the metrics port to a port.

### Disabling and narrowing

Exactly as with a hand-written class: a section annotated `None` is off, a section model can be
subclassed to narrow or extend it, and a default can be replaced:

```python
from typing import Literal

from servicewright.adapters.settings import BaseServiceSettings, LoggingSettings, TracingSettings


class StrictLogging(LoggingSettings):
    level: Literal["INFO", "WARNING", "ERROR"] = "INFO"


class Settings(BaseServiceSettings):
    logging: StrictLogging = StrictLogging()
    tracing: TracingSettings = TracingSettings(sample_ratio=0.1)  # on, sampling 10 %
    metrics: None = None                                          # off
```

### Writing your own

Anything with the right attribute names still works — a frozen dataclass, a plain class, a
pydantic-settings class of your own; [Your first service](../getting-started/first-service.md)
shows the smallest one. The kernel never imports pydantic: the models live in
`servicewright.adapters.settings`, behind their extra, like every other binding.

## What is *not* in settings

Server configuration is not. `HttpConfig`, `GrpcConfig` and `LitestarConfig` are passed to their
entrypoint **at construction**:

```python
FastApiEntrypoint(config=HttpConfig(port=8000))
```

This is deliberate. `AppSpec` stays transport-neutral, which is what lets the same spec run an
API in one process and a worker in another. It also means two HTTP entrypoints in one process can
have different ports without inventing a settings namespace for each.

Of course, nothing stops you feeding your own settings into that config:

```python
FastApiEntrypoint(config=HttpConfig(host=settings.http.host, port=settings.http.port))
```

You just do it explicitly, where you can see it.

## Next

- [Observability](observability.md) — how `ObsConfig` maps backends onto these sections.
