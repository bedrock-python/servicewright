# Installation

```bash
pip install servicewright
```

**Requirements:** Python 3.12 or newer.

That is the whole kernel: `AppSpec`, `Service`, `Host`, the lifecycle, the error taxonomy, the
health registry, the context store, the observability protocols. It has **no third-party
dependencies** — nothing is installed alongside it.

You then add exactly the framework bindings you use.

## Extras

```bash
pip install "servicewright[fastapi]"
pip install "servicewright[fastapi,grpc,metrics,observability]"
```

### Entrypoints

| Extra | Pulls in | Gives you |
| --- | --- | --- |
| `fastapi` | fastapi, uvicorn, deadline-budget, prometheus-fastapi-instrumentator | [`FastApiEntrypoint`](../adapters/fastapi.md) and its middleware stack |
| `litestar` | litestar, uvicorn | [`LitestarEntrypoint`](../adapters/litestar.md) |
| `grpc` | `grpc-server-kit[reflection,channelz,health]` | [`GrpcEntrypoint`](../adapters/grpc.md), error mapping, health bridge |
| `apscheduler4` | apscheduler 4.x | [`SchedulerEntrypoint`](../adapters/scheduler.md) |
| `apscheduler3` | apscheduler 3.x | the same entrypoint against APScheduler 3.x |

`DaemonEntrypoint` and `OneShotEntrypoint` need no extra — they are pure Python.

### Observability

| Extra | Pulls in | Gives you |
| --- | --- | --- |
| `metrics` | prometheus-client | the `prometheus` metrics backend |
| `observability` | opentelemetry-sdk, OTLP gRPC exporter, structlog | the `otel` tracing and `structlog` logging backends |
| `fastapi-tracing` | the above + opentelemetry-instrumentation-fastapi | HTTP request spans on the FastAPI entrypoint |
| `sentry` | sentry-sdk | the `sentry` error-tracking backend |

The `stdlib` logging backend needs no extra.

### Infrastructure and DI

| Extra | Pulls in | Gives you |
| --- | --- | --- |
| `dishka` | dishka | [`DishkaContainer`](../adapters/dishka.md) |
| `redis` | redis | `RedisWarmer`, `RedisHealthCheck` |
| `postgres` | `sqlalchemy[asyncio]` | `PostgresWarmer`, `PostgresHealthCheck` |
| `kafka` | aiokafka | `KafkaProducerWarmer` |

### Everything at once

```bash
pip install "servicewright[all]"
```

`all` bundles every extra **except `apscheduler3`**, which cannot coexist with `apscheduler4`
(see below).

!!! warning "APScheduler 3 and 4 are mutually exclusive"

    They are the same distribution with incompatible majors, so only one can be installed in a
    given environment. Pick the one that matches your project:

    ```bash
    pip install "servicewright[apscheduler4]"   # or
    pip install "servicewright[apscheduler3]"
    ```

    Both adapters expose an identical public surface, so migrating between them is one import
    line and one extra. See [Scheduler](../adapters/scheduler.md#apscheduler-3-vs-4).

## What "extra-gated" actually means

An extra never changes how the kernel behaves. It only decides whether a module is importable:

```python
from servicewright.adapters.fastapi import FastApiEntrypoint
# ImportError: ... requires servicewright[fastapi]; install it.
```

Missing extras fail **loudly and early** — at import time, or at bootstrap for a selected
observability backend — with a message naming the extra to install. They never degrade into
silence.

## Checking your install

```bash
python -c "import servicewright; print(servicewright.__version__)"
```

To confirm the kernel really is dependency-free, install it on its own and look at what it
requires:

```console
$ pip show servicewright
Name: servicewright
Version: 0.1.0
Requires:
Required-by:
```

An empty `Requires:` is the whole point.

## Next

- [Your first service](first-service.md) — a complete service with nothing installed but the kernel.
- [Tutorial](tutorial.md) — HTTP API + cron job in one process.
