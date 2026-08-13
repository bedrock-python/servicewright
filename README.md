# servicewright

**One `Host`, many `Entrypoint`s** — a batteries-optional microservice runtime for async Python.

[![PyPI](https://img.shields.io/pypi/v/servicewright?color=blue)](https://pypi.org/project/servicewright/)
[![Python](https://img.shields.io/pypi/pyversions/servicewright)](https://pypi.org/project/servicewright/)
[![License](https://img.shields.io/github/license/bedrock-python/servicewright)](LICENSE)
[![CI](https://github.com/bedrock-python/servicewright/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/bedrock-python/servicewright/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/bedrock-python/servicewright/graph/badge.svg)](https://codecov.io/gh/bedrock-python/servicewright)
[![Docs](https://img.shields.io/badge/docs-online-blue)](https://bedrock-python.github.io/servicewright/)

Describe a service once as an `AppSpec` (DI container, lifecycle, observability, warmup, health) and
run it through any number of pluggable entrypoints — HTTP, gRPC, scheduler, background daemon,
one-shot batch — under one unified lifecycle. The "API vs worker" distinction does not exist at the
core: a cron job and an HTTP request are the same thing — one unit of work in a fresh DI scope.

This is the model behind .NET Generic Host, Spring `SmartLifecycle` and go-kratos
`transport.Server`, adapted to async Python.

## Why

- **One lifecycle for every archetype**: Bootstrap → Warmup → Ready → Serve → Drain → Cleanup.
  Kubernetes-correct shutdown out of the box: readiness flips to `false` *before* draining, every
  entrypoint finishes in-flight work within a grace window, the DI scope closes last, and a service
  that dies mid-serve exits non-zero instead of looking like a graceful stop.
- **DI-agnostic two-tier scopes**: the core depends on no DI library. `AppScope` holds
  process-lifetime singletons; a fresh `UnitScope` wraps every request / RPC / job / message.
  A dishka adapter ships in the box; any container fits by implementing two methods.
- **Pluggable observability add-ons**: metrics / tracing / logging / error-tracking are protocols
  in the kernel with selectable, extra-gated backends (prometheus, OpenTelemetry, Sentry,
  structlog). Adding a backend = one module + one `register_sink` call, zero core changes.
- **One error taxonomy, every transport**: a `ServiceError` raised in business code renders as an
  RFC 9457 problem document over HTTP and as the mapped `grpc.StatusCode` over gRPC, with one
  masking rule for non-public details and a pluggable renderer when you own the wire format.
- **Zero hard dependencies**: `pip install servicewright` brings pure Python. Every framework
  binding lives behind an extra; the kernel never imports an SDK, a vendor, or a transport.

## Installation

```bash
pip install servicewright                    # pure kernel, zero dependencies
pip install "servicewright[fastapi]"         # + FastAPI/uvicorn entrypoint
pip install "servicewright[grpc]"            # + gRPC entrypoint
pip install "servicewright[apscheduler4]"    # + cron/scheduler entrypoint
pip install "servicewright[metrics,observability,sentry]"  # + prometheus, otel+structlog, sentry
pip install "servicewright[all]"             # everything except the conflicting [apscheduler3]
```

**Requirements:** Python 3.12+

## Quick start — HTTP API + cron in ONE process

```python
import asyncio

from servicewright import AppSpec, ObsConfig, ObservabilityManager, Service, run
from servicewright.adapters.apscheduler4 import ScheduledJob, SchedulerEntrypoint
from servicewright.adapters.fastapi import FastApiEntrypoint


def build_service() -> Service:
    spec = AppSpec(
        service_name="orders-service",
        create_container=build_container,   # your DI container factory
        observability=ObservabilityManager(
            ObsConfig(metrics="prometheus", tracing="otel", logging="structlog"),
        ),
    )

    http = FastApiEntrypoint(routers=(router,))          # kind="http"
    cron = SchedulerEntrypoint(jobs=[                    # kind="scheduler"
        ScheduledJob(id="sweep", func=sweep_expired_orders, trigger=interval_trigger),
    ])
    return Service(spec, entrypoints=[http, cron])


if __name__ == "__main__":
    asyncio.run(run(build_service(), Settings()))
```

Both entrypoints share one DI container, one observability setup and one graceful shutdown.
Scaling the worker separately later = the same `AppSpec` in a second process with a different
entrypoint list.

## Entrypoints

| Archetype | Adapter | Extra |
| --- | --- | --- |
| HTTP API | `adapters.fastapi` / `adapters.litestar` | `fastapi` / `litestar` |
| gRPC API | `adapters.grpc` | `grpc` |
| Scheduled / cron | `adapters.apscheduler4` / `adapters.apscheduler3` | `apscheduler4` / `apscheduler3` |
| Background daemon | `DaemonEntrypoint` (built-in) | — |
| One-shot / batch | `OneShotEntrypoint` (built-in) | — |

Writing your own entrypoint = implementing four methods (`bind`, `serve`, `drain`, `stop`)
with nothing installed.

## What's inside

The kernel is `core/`; everything that touches a third-party SDK is an extra-gated adapter. An
import-linter contract enforces the direction in CI: deleting `adapters/` leaves `core/` importable.

| Module | Responsibility | Extra |
| --- | --- | --- |
| `servicewright` | `AppSpec`, `Service`, `Host`, `run` — the public vocabulary | — |
| `core.contracts` | `Entrypoint`, `Plugin`, container/settings/health protocols | — |
| `core.aio.host` | The lifecycle kernel: warmup → ready → serve → drain → cleanup | — |
| `core.errors` | `ServiceError`, `ErrorKind`, RFC 9457 renderer + renderer seam | — |
| `core.context` | Transport-neutral correlation store + outbound propagation | — |
| `core.health` | `HealthRegistry` driving both HTTP routes and the gRPC health service | — |
| `core.warmup` | Priority-grouped, fail-fast warmup before readiness flips | — |
| `core.observability` | Sink protocols, NullObjects, backend registry, redaction | — |
| `adapters.builtin` | `DaemonEntrypoint`, `OneShotEntrypoint` — zero-dependency | — |
| `adapters.fastapi` | FastAPI entrypoint, middleware stack, problem-details handlers | `fastapi` |
| `adapters.litestar` | Litestar entrypoint | `litestar` |
| `adapters.grpc` | gRPC entrypoint over grpc-server-kit, error mapping, health bridge | `grpc` |
| `adapters.apscheduler4` / `apscheduler3` | Scheduler entrypoints with identical public surfaces | `apscheduler4` / `apscheduler3` |
| `adapters.dishka` | dishka ⇄ core scope binding | `dishka` |
| `adapters.observability` | prometheus / OpenTelemetry / Sentry / structlog / stdlib sinks | see below |
| `adapters.warmers`, `adapters.health` | Redis / Postgres / Kafka warmers and checks | `redis`, `postgres`, `kafka` |
| `servicewright.testing` | `FakeContainer`, `FakeEntrypoint`, `FakeScope`, `FakeSettings` | — |

## Optional dependencies

| Extra | Pulls in | Enables |
| --- | --- | --- |
| `fastapi` | fastapi, uvicorn, deadline-budget, prometheus-fastapi-instrumentator | `FastApiEntrypoint` + its middleware stack |
| `litestar` | litestar, uvicorn | `LitestarEntrypoint` |
| `grpc` | grpc-server-kit[reflection,channelz,health] | `GrpcEntrypoint`, error mapping, health bridge |
| `apscheduler4` / `apscheduler3` | apscheduler 4.x / 3.x | `SchedulerEntrypoint` (one major per environment) |
| `dishka` | dishka | `DishkaContainer` |
| `observability` | opentelemetry-sdk, OTLP gRPC exporter, structlog | `otel` tracing + `structlog` logging sinks |
| `fastapi-tracing` | the above + opentelemetry-instrumentation-fastapi | HTTP request spans |
| `metrics` | prometheus-client | `prometheus` metrics sink + `/system/metrics` |
| `sentry` | sentry-sdk | `sentry` error-tracking sink |
| `redis` / `postgres` / `kafka` | redis / sqlalchemy / aiokafka | matching warmers and health checks |
| `all` | everything except `apscheduler3` | the full runtime |

## Examples

Runnable, self-contained scripts (each exits 0):

- [`examples/minimal_service.py`](examples/minimal_service.py) — the smallest real service, narrating
  every lifecycle phase in order.
- [`examples/http_service.py`](examples/http_service.py) — a real uvicorn server with dishka DI,
  per-request scopes, health probes, an RFC 9457 error on the wire and a graceful drain.
- [`examples/errors_and_context.py`](examples/errors_and_context.py) — the error taxonomy, a custom
  renderer, masking, and the correlation store with outbound propagation.
- [`examples/warmup_and_health.py`](examples/warmup_and_health.py) — warmup priority groups,
  fail-fast, and health checks driving readiness.

## Documentation

Full documentation: **[bedrock-python.github.io/servicewright](https://bedrock-python.github.io/servicewright/)**

| | |
| --- | --- |
| [Your first service](https://bedrock-python.github.io/servicewright/getting-started/first-service/) | a complete service with nothing installed but the kernel |
| [Tutorial](https://bedrock-python.github.io/servicewright/getting-started/tutorial/) | an HTTP API and a cron job in one process |
| [Architecture](https://bedrock-python.github.io/servicewright/concepts/architecture/) | the six nouns, the two layers, the dependency rule |
| [Lifecycle](https://bedrock-python.github.io/servicewright/concepts/lifecycle/) | phase order, budgets, signals, exit codes |
| [Adapters](https://bedrock-python.github.io/servicewright/adapters/overview/) | FastAPI, Litestar, gRPC, scheduler, dishka, observability backends |
| [Blueprints](https://bedrock-python.github.io/servicewright/blueprints/project-layout/) | copy-paste skeletons: project layout, HTTP API, gRPC, worker, batch job |
| [Writing an entrypoint](https://bedrock-python.github.io/servicewright/guides/custom-entrypoint/) | four methods, worked end to end |
| [Kubernetes](https://bedrock-python.github.io/servicewright/operations/kubernetes/) | probes, grace periods, exit codes |
| [Runbooks](https://bedrock-python.github.io/servicewright/operations/runbooks/) | symptom → cause → fix |
| [API reference](https://bedrock-python.github.io/servicewright/reference/servicewright/) | generated from the source |

The design source-of-truth lives in [ARCHITECTURE.md](ARCHITECTURE.md).

## License

Apache 2.0 — see [LICENSE](LICENSE).
