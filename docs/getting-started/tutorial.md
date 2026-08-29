# Tutorial: an API and a cron job in one process

This is the walkthrough for a service that looks like most real services: an HTTP API, plus a
scheduled job that cleans up after it. They share one dependency container, one logging setup,
one metrics registry and one shutdown.

```bash
pip install "servicewright[fastapi,apscheduler4,dishka,metrics,observability]"
```

## 1. Settings

Sections are named by **concern**, never by vendor. Which backend reads a section is decided
later, in `ObsConfig`.

```python title="settings.py"
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "INFO"
    use_json: bool = True


@dataclass(frozen=True)
class MetricsSettings:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9090
    prefix: str | None = None


@dataclass(frozen=True)
class Settings:
    logging: LoggingSettings | None = field(default_factory=LoggingSettings)
    metrics: MetricsSettings | None = field(default_factory=MetricsSettings)
    tracing: object | None = None          # no OTLP collector in this tutorial
    error_tracking: object | None = None   # no Sentry DSN in this tutorial

    def get_app_version(self) -> str:
        return "1.0.0"
```

A section left as `None` disables its concern entirely. See [Settings](../concepts/settings.md)
for the full field list of each one. The dataclasses keep the tutorial self-contained; the
`settings` extra ships the same sections as [pydantic-settings models](../concepts/settings.md#shipped-models)
loaded from the environment.

## 2. Domain and container

Nothing here knows that servicewright exists. That is the point — your business code stays
plain.

```python title="domain.py"
class OrderRepository:
    """A process-lifetime singleton: holds the connection pool."""

    async def find(self, order_id: str) -> dict | None: ...

    async def delete_expired(self) -> int: ...


class GetOrder:
    """A use case: one per unit of work."""

    def __init__(self, orders: OrderRepository) -> None:
        self._orders = orders

    async def execute(self, order_id: str) -> dict: ...
```

Wire them with dishka. `Scope.APP` maps to the application scope, `Scope.REQUEST` maps to the
per-unit-of-work scope:

```python title="container.py"
from dishka import Provider, Scope, make_async_container, provide

from servicewright.adapters.dishka import DishkaContainer


class AppProvider(Provider):
    @provide(scope=Scope.APP)
    def orders(self) -> OrderRepository:
        return OrderRepository()

    @provide(scope=Scope.REQUEST)
    def get_order(self, orders: OrderRepository) -> GetOrder:
        return GetOrder(orders)


def build_container(settings: Settings) -> DishkaContainer:
    return DishkaContainer(make_async_container(AppProvider()))
```

`build_container` is called once, at bootstrap, with your settings. See
[dishka](../adapters/dishka.md).

## 3. The HTTP entrypoint

Handlers reach the per-request scope through the `UnitScopeDep` dependency:

```python title="api.py"
from fastapi import APIRouter

from servicewright import ErrorKind, ServiceError
from servicewright.adapters.fastapi import UnitScopeDep

router = APIRouter(prefix="/orders", tags=["orders"])


class OrderNotFoundError(ServiceError):
    kind = ErrorKind.NOT_FOUND     # code auto-derives to "order_not_found"


@router.get("/{order_id}")
async def get_order(order_id: str, scope: UnitScopeDep) -> dict:
    use_case = await scope.get(GetOrder)
    order = await use_case.execute(order_id)
    if order is None:
        raise OrderNotFoundError("no such order", params={"order_id": order_id})
    return order
```

Raising `OrderNotFoundError` produces a real 404 with an RFC 9457 body:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "code": "order_not_found",
  "detail": "no such order",
  "params": { "order_id": "42" }
}
```

The same exception raised behind a gRPC entrypoint would abort with `NOT_FOUND` instead. See
[Errors](../concepts/errors.md).

## 4. The scheduled job

A job takes the DI scope as its **first argument**. That is not a special convention invented for
schedulers — it is the same "one unit of work, one scope" rule the HTTP request follows.

```python title="jobs.py"
from apscheduler.triggers.interval import IntervalTrigger

from servicewright import UnitScopeProtocol
from servicewright.adapters.apscheduler4 import ScheduledJob


async def sweep_expired_orders(scope: UnitScopeProtocol) -> None:
    orders = await scope.get(OrderRepository)
    removed = await orders.delete_expired()
    print(f"swept {removed} expired orders")


sweep_job = ScheduledJob(
    id="sweep-expired-orders",
    func=sweep_expired_orders,
    trigger=IntervalTrigger(minutes=5),
    max_instances=1,
)
```

## 5. Put it together

```python title="main.py"
from servicewright import AppSpec, ObsConfig, ObservabilityManager, Service, run_sync
from servicewright.adapters.apscheduler4 import SchedulerEntrypoint
from servicewright.adapters.fastapi import FastApiEntrypoint, HttpConfig


def build_service() -> Service:
    spec = AppSpec(
        service_name="orders-service",
        create_container=build_container,
        observability=ObservabilityManager(
            ObsConfig(metrics="prometheus", logging="structlog", tracing=None, error_tracking=None),
        ),
        drain_grace_seconds=30.0,
    )

    http = FastApiEntrypoint(
        config=HttpConfig(port=8000),
        routers=(router,),
        metrics=True,
    )
    cron = SchedulerEntrypoint(jobs=[sweep_job])

    return Service(spec, entrypoints=[http, cron])


if __name__ == "__main__":
    run_sync(build_service(), Settings())
```

```bash
python main.py
```

## 6. What is running

```
GET  http://localhost:8000/orders/42              your API
GET  http://localhost:8000/system/health/livez    liveness probe
GET  http://localhost:8000/system/health/readyz   readiness probe
GET  http://localhost:8000/system/docs            Swagger UI
GET  http://localhost:8000/system/metrics         in-app request metrics
GET  http://localhost:9090/metrics                standalone metrics server
```

!!! question "Two metrics endpoints?"

    They serve different needs and you usually want only one.

    - `settings.metrics.enabled = True` starts a **standalone** exposition server on
      `settings.metrics.port`. It exists so that socket-less processes — a pure cron worker, a
      Kafka consumer — can still be scraped.
    - `FastApiEntrypoint(metrics=True)` exposes **in-app** HTTP request metrics on the API's own
      port via `prometheus-fastapi-instrumentator`.

    Both write into the same Prometheus registry, so scraping either one gets you every metric.
    For a pure API, set `enabled=False` and keep the in-app endpoint.

Your logs are JSON, and every request line carries the `request_id` that the response returned in
its `X-Request-ID` header. Job runs carry `job_id` and `run_id`.

## 7. Shutting down

Send a `SIGTERM` (or press ++ctrl+c++) and watch the order:

1. `/system/health/readyz` starts returning **503** — before anything stops accepting.
   Your load balancer removes the pod while it can still serve.
2. The scheduler pauses its schedules so no new job fires, and waits up to `drain_grace_seconds`
   for the job that is already running.
3. uvicorn closes the listener and finishes in-flight requests.
4. `pre_shutdown` hooks run while the application scope is still alive.
5. The dishka container closes, finalizing the connection pool.
6. Logs are flushed and the process exits `0`.

Nothing about that ordering had to be configured. It is what `Host` does. See
[Lifecycle](../concepts/lifecycle.md).

## 8. Splitting them apart later

Say the sweep grows heavy and you want to scale it independently. You do not restructure
anything — you build a second `Service` from the **same** `AppSpec` factory with a different
entrypoint list:

```python
api_service = Service(build_spec(), entrypoints=[http])
worker_service = Service(build_spec(), entrypoints=[cron])
```

Two deployments, one codebase, identical warmup, health, observability and lifecycle.

## Next

- [Project layout](../blueprints/project-layout.md) — where all this code goes in a real repository.
- [HTTP API blueprint](../blueprints/http-api.md) — the same service with Postgres, warmup and
  health wired in properly.
- [Architecture](../concepts/architecture.md) — the model behind what you just built.
- [Kubernetes](../operations/kubernetes.md) — probes, `terminationGracePeriodSeconds`, exit codes.
