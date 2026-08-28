# Settings

servicewright reads a small, fixed set of things off a settings object that **you** own and
construct. It never loads a `.env`, never reads `os.environ`, and never defines a settings class
you have to inherit from.

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

### `logging`

| Field | Type | Meaning |
| --- | --- | --- |
| `level` | `str` | Root log level: `DEBUG`, `INFO`, ... |
| `use_json` | `bool` | JSON lines vs human-readable console output |

### `metrics`

| Field | Type | Meaning |
| --- | --- | --- |
| `enabled` | `bool` | Start a standalone exposition server |
| `host` | `str` | Bind host for that server |
| `port` | `int` | Bind port for that server |
| `prefix` | `str \| None` | Metric name prefix, for backends that use one |

!!! note

    The built-in Prometheus backend does not apply `prefix` itself. Metric names belong to the
    recorder that owns them, so prefixes are set there — for example
    `GrpcEntrypoint(metrics_prefix="myapp")`. The field exists for backends that prefix
    globally.

### `tracing`

| Field | Type | Meaning |
| --- | --- | --- |
| `service_name` | `str` | Resource service name (falls back to `AppSpec.service_name`) |
| `collector_url` | `str \| None` | OTLP endpoint; `None` means no exporter |
| `sample_ratio` | `float` | Ratio for the parent-based sampler |
| `insecure` | `bool` | Plaintext OTLP connection |
| `enable_console_exporter` | `bool` | Also print spans to stdout |
| `excluded_urls` | `str \| None` | Comma-separated paths to skip |

### `error_tracking`

| Field | Type | Meaning |
| --- | --- | --- |
| `dsn` | `str \| None` | Reporting endpoint; **empty means the concern is off** |
| `environment` | `str` | Environment tag |
| `traces_sample_rate` | `float` | Performance-trace sampling |
| `profiles_sample_rate` | `float` | Profiling sampling |
| `debug` | `bool` | SDK debug mode |

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

## With pydantic-settings

Anything with the right attribute names works. A typical real service:

```python
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class LoggingSettings(BaseModel):
    level: str = "INFO"
    use_json: bool = True


class MetricsSettings(BaseModel):
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 9090
    prefix: str | None = None


class TracingSettings(BaseModel):
    service_name: str = ""
    collector_url: str | None = None
    sample_ratio: float = 1.0
    insecure: bool = True
    enable_console_exporter: bool = False
    excluded_urls: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", env_file=".env")

    app_version: str = "0.0.0"
    environment: str = "local"

    logging: LoggingSettings = LoggingSettings()
    metrics: MetricsSettings = MetricsSettings()
    tracing: TracingSettings | None = None
    error_tracking: None = None

    def get_app_version(self) -> str:
        return self.app_version
```

```bash
LOGGING__LEVEL=DEBUG METRICS__PORT=9100 python main.py
```

!!! note

    servicewright never imports pydantic. The example above works because the attributes line up,
    not because of any integration.

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
