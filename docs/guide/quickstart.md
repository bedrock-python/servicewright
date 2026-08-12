# Quick start

This walkthrough builds a service that serves an HTTP API **and** runs a cron job in one
process — one DI container, one observability setup, one graceful shutdown.

```bash
pip install "servicewright[fastapi,apscheduler4,dishka,metrics,observability]"
```

## 1. Settings

servicewright reads observability configuration from your settings object. Any object satisfying
`BaseServiceSettingsProtocol` works — sections are named by *concern*, and a section set to `None`
simply disables that concern:

```python
from dataclasses import dataclass


@dataclass
class LoggingSettings:
    level: str = "INFO"
    use_json: bool = True


@dataclass
class MetricsSettings:
    enabled: bool = True          # standalone /metrics server for socket-less processes
    host: str = "0.0.0.0"
    port: int = 9090
    prefix: str | None = None


@dataclass
class Settings:
    logging: LoggingSettings | None = LoggingSettings()
    metrics: MetricsSettings | None = MetricsSettings()
    tracing: object | None = None          # no otel collector in this example
    error_tracking: object | None = None   # no sentry DSN in this example

    def get_app_version(self) -> str:
        return "1.0.0"
```

## 2. DI container

The core is DI-agnostic; here we use the bundled dishka adapter:

```python
from dishka import Provider, Scope, make_async_container, provide

from servicewright.adapters.dishka import DishkaContainer


class AppProvider(Provider):
    @provide(scope=Scope.APP)
    def pool(self) -> Database: ...          # process-lifetime singleton

    @provide(scope=Scope.REQUEST)
    def handler(self, db: Database) -> SweepHandler: ...   # per unit of work


def build_container(settings: Settings) -> DishkaContainer:
    return DishkaContainer(make_async_container(AppProvider()))
```

## 3. Entrypoints

```python
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import APIRouter

from servicewright.adapters.apscheduler4 import ScheduledJob, SchedulerEntrypoint
from servicewright.adapters.fastapi import FastApiEntrypoint, HttpConfig

router = APIRouter()


@router.get("/orders/{order_id}")
async def get_order(order_id: str): ...


# A scheduled job is scope-first: it receives the per-job UnitScope as its
# first argument — structurally the same as a request handler resolving deps.
async def sweep_expired_orders(scope) -> None:
    handler = await scope.get(SweepHandler)
    await handler.run()


http = FastApiEntrypoint(config=HttpConfig(port=8000), routers=(router,))
cron = SchedulerEntrypoint(jobs=[
    ScheduledJob(id="sweep", func=sweep_expired_orders, trigger=IntervalTrigger(minutes=5)),
])
```

## 4. Assemble and run

```python
import asyncio

from servicewright import AppSpec, ObsConfig, ObservabilityManager, Service, run

spec = AppSpec(
    service_name="orders-service",
    create_container=build_container,
    observability=ObservabilityManager(
        ObsConfig(metrics="prometheus", logging="structlog", tracing=None, error_tracking=None),
    ),
)

service = Service(spec, entrypoints=[http, cron])

if __name__ == "__main__":
    asyncio.run(run(service, settings=Settings()))
```

## What you get

- `GET /system/health/livez` / `readyz` — Kubernetes probes driven by the shared
  `HealthRegistry`; readiness flips to `false` the moment a stop signal arrives.
- Structured JSON logs with per-request `request_id` correlation (the `ContextMiddleware` binds it,
  the configured logging sink formats it, and the response echoes it in `X-Request-ID`).
- Prometheus metrics on `:9090/metrics` (plus in-app instrumentation if you pass
  `FastApiEntrypoint(metrics=True)`).
- One `SIGTERM` drains **both** entrypoints: readiness flips to false first, then uvicorn stops
  accepting and finishes in-flight requests, the scheduler waits for running jobs, and the DI
  container closes its pools last. A second `SIGTERM` stops waiting and exits immediately.

Scaling the cron part separately later = the same `AppSpec` in a second process whose entrypoint
list contains only the scheduler.
