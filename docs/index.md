# servicewright

**One `Host`, many `Entrypoint`s.** A batteries-optional microservice runtime for async Python:
describe a service once as an `AppSpec` (DI container, lifecycle, observability, warmup, health)
and run it through any number of pluggable entrypoints — HTTP, gRPC, scheduler, background daemon,
one-shot batch — under one unified lifecycle.

The "API vs worker" distinction does not exist at the core: it collapses into *where the per-unit
DI scope is opened*. A cron job and an HTTP request are the same thing — one unit of work in a
fresh `UnitScope`. This is the model behind .NET Generic Host, Spring `SmartLifecycle` and
go-kratos `transport.Server`, adapted to async Python.

## The six nouns

| Concept | Meaning |
| --- | --- |
| **`Service`** | User-facing facade: `Service(spec, entrypoints=[...], plugins=[...])`; `await service.run(settings)` blocks until a stop signal. |
| **`AppSpec`** | The transport-neutral declarative description: `service_name`, `create_container`, lifecycle, observability, warmers, health. Never mentions HTTP/gRPC. |
| **`Host`** | The kernel that runs an `AppSpec` + a list of entrypoints: lifecycle ordering, the app DI scope, warmup, readiness, OS signals, the serve run-loop, graceful shutdown. |
| **`Entrypoint`** | The pluggable driver — how work enters. Four methods: `bind`, `serve`, `drain`, `stop`. The Host treats every entrypoint identically and never branches on its kind. |
| **`AppScope` / `UnitScope`** | The two DI tiers: process-lifetime singletons vs one unit of work (request / RPC / job / message). |
| **`Plugin`** | The single extension mechanism: `on_register(spec, host)` mutates a neutral spec — appends entrypoints, warmers, health checks, hooks. |

## The unified lifecycle

```
Bootstrap → Warmup → Ready → Serve → Drain → Cleanup
```

Startup is fail-fast: observability first (so bootstrap failures are observable), then the
container, the app scope, warmup, `pre_start` hooks, `bind` for every entrypoint — and only then
`health.ready = True`. Shutdown is Kubernetes-correct: readiness flips to `false` **first** (the
balancer stops routing before you stop accepting), every entrypoint drains in-flight work within a
grace window, `pre_shutdown` hooks run while the app scope is still alive, the DI scope closes
last, observability flushes at the very end.

## Installation

```bash
pip install servicewright                    # pure kernel, zero dependencies
pip install "servicewright[fastapi]"         # + FastAPI/uvicorn entrypoint
pip install "servicewright[grpc]"            # + gRPC entrypoint
pip install "servicewright[apscheduler4]"    # + cron/scheduler entrypoint
pip install "servicewright[dishka]"          # + dishka DI adapter
pip install "servicewright[metrics,observability,sentry]"  # prometheus, otel+structlog, sentry
```

**Requirements:** Python 3.12+

Continue with the [quick start](guide/quickstart.md), the
[configuration guide](guide/configuration.md) or [advanced usage](guide/advanced.md).
