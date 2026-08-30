# servicewright — Reference Architecture

> **One `Host`, many `Entrypoint`s — assembled from top tools, configured by add-ons.**
> A backend service is described once as an `AppSpec` (who you are, how to build your DI
> container, your lifecycle/observability/warmup/health) and run by one `Host` that drives any
> number of pluggable `Entrypoint`s — HTTP, gRPC, scheduler, message consumer, background daemon,
> one-shot batch — through a single uniform contract. Each entrypoint can be wired with selectable
> **add-ons** (metrics / tracing / logging / error-tracking) whose backend you choose. The
> "API vs worker" distinction does not exist at the core; it collapses into *where the per-unit
> DI scope is opened*.

This is the model behind .NET Generic Host / `IHostedService`, Spring `SmartLifecycle`,
go-kratos `transport.Server`, and Litestar plugins — adapted to async Python. It was selected
over a forked "api-core / worker-core" design after a multi-framework study and an adversarial
review (the fork could not express "HTTP + scheduler in one process, one DI, one shutdown").

> **Status:** design source-of-truth. Consolidates three locked design pillars — (1) the
> two-layer `core/` + `adapters/` structure, (2) the dependency strategy, (3) the pluggable
> add-ons layer. Supersedes the prototype layout; the conceptual model (§1–§4, §7) carries
> forward, the structure (§5), extras (§6, §10), and add-ons (§9) are new.

---

## 1. The six nouns (public vocabulary)

| Concept | Meaning |
| --- | --- |
| **`Service`** | User-facing facade. `Service(spec, entrypoints=[...], plugins=[...])`; `service.run(settings)` blocks until a stop signal. Also a module-level `servicewright.run(service, settings)`. |
| **`AppSpec[TSettings, TContainer]`** | The **transport-neutral** declarative description: `service_name`, `create_container`, `lifecycle`, `observability`, `warmers`/`warmers_factory`, `health`. Never mentions HTTP/gRPC. |
| **`Host[TSettings, TContainer]`** | The kernel that runs an `AppSpec` + a list of `Entrypoint`s: owns lifecycle ordering, the app DI scope, warmup, readiness, OS signals, observability SDK bootstrap, the serve run-loop, and graceful shutdown. |
| **`Entrypoint`** | The pluggable driver = how work enters. Host-facing protocol: `kind`, `essential`, `bind`, `serve`, `drain`, `stop`. The Host treats every entrypoint identically. |
| **`AppScope` / `UnitScope`** | The two DI scope tiers. `AppScope` = process-lifetime singletons (pools, clients). `UnitScope` = one unit of work (one request / message / job). |
| **`Plugin`** | The **single** extension mechanism (Litestar `on_app_init` analogue): `on_register(spec, host)` mutates a neutral spec/host — appends entrypoints, warmers, health checks, lifecycle hooks, DI providers. Everything batteries-optional is a Plugin, never a subclass. |

Supporting types: `ServiceContext` (post-app-scope data bag), `BootstrapContext` (pre-scope),
`Lifecycle` (4 hook points), `HealthRegistry` + `HealthReport` (k8s probes), `AsyncWarmer`,
`ObservabilityManager` (the multi-sink add-on orchestrator, §9).

> **Naming rules.** Concrete user-facing classes get clean names (`Service`, `Host`, `AppSpec`,
> `Lifecycle`, `Entrypoint`, `Plugin`, `HealthRegistry`). Structural protocols keep the `...Protocol`
> suffix (`BaseServiceSettingsProtocol`, `DependencyContainerProtocol`, `AppScopeProtocol`,
> `UnitScopeProtocol`, `HealthCheckerProtocol`). `UnitScope` is deliberately **not** `UnitOfWork`
> — that term is the DB transaction (`AsyncUnitOfWork`) elsewhere in the platform.

---

## 2. Two layers — `core/` (abstractions) vs `adapters/` (implementations)

The single load-bearing structural rule. **`core/` is the pure kernel; `adapters/` is every
concrete framework binding.**

- **`core/`** imports **stdlib + `typing` only** — never `grpcio`, `fastapi`, `apscheduler`,
  `dishka`, `redis`, `sqlalchemy`, `pydantic`, or any observability SDK. It holds the entire
  contract surface, the `Host` loop-kernel, the lifecycle state machine, observability
  seam-specs/orchestration, warmup orchestration, the health registry.
- **`adapters/`** holds the implementation: one subpackage per `(framework, major)` for
  entrypoints, the DI binding, the concrete warmers, the concrete health-checkers, the
  add-on backend implementations, and the pydantic-settings models of the settings contract.

**Dependency rule (CI-enforced via import-linter):** `adapters → core → stdlib`; never
`core → adapters`; never `adapter → adapter`. **Litmus:** *deleting `adapters/` must leave
`core/` importable and green.*

**Why `adapters/` (not `infra`/`transports`/`drivers`):** it is the exact Ports-and-Adapters
term, reads correctly at the install site (`servicewright[fastapi]` → "the fastapi adapter"),
pairs 1:1 with `core/contracts/`, and shares no stem with `entrypoint` — making the prototype's
`entrypoint/` vs `entrypoints/` one-letter homograph structurally impossible. (`infra` collides
with the bedrock onion `infra/` layer; `transports` lies for scheduler/daemon/consumer.)

### The aio convention (servicewright's own rule)

`aio.py` (or `aio/`) segregates async code **only inside a package that genuinely mixes sync and
async members.** `core/lifecycle`, `core/observability`, `core/health`, `core/warmup` mix (their
specs/registries/managers are sync) → each gets a co-located `aio.py`. `core/aio/host.py` has no
sync twin → it earns the reserved top-level `core/aio/`. **An adapter is single-paradigm async —
it has no sync surface to disambiguate — so it has NO `aio/`.** This is a deliberate simplification
of grpc-server-kit's convention (which nests every async member under `aio/`).

---

## 3. DI-agnostic two-tier scope model

servicewright depends on **no DI library**. A container is anything implementing:

```python
class DependencyContainerProtocol(Protocol):
    def app_scope(self) -> AbstractAsyncContextManager[AppScopeProtocol]: ...
    def unit_scope(
        self, context: Mapping[str, Any] | None = None
    ) -> AbstractAsyncContextManager[UnitScopeProtocol]: ...

class AppScopeProtocol(Protocol):
    async def get(self, dependency_key: type[T] | str) -> T | Any: ...

class UnitScopeProtocol(Protocol):
    async def get(self, dependency_key: type[T] | str) -> T | Any: ...
```

- **`AppScope`** is opened **once** by the Host for the whole process and closed last.
- **`UnitScope`** is a child scope minted per unit of work, carrying the unit payload as `context`
  (the `Request`, the `ConsumerRecord`, `{job_id, run_id}`).

This makes **"a cron job == an HTTP request"** structurally true: both resolve their handler in a
fresh `UnitScope`. `adapters/dishka/` ships a one-class adapter mapping `AppScope → Scope.APP` and
`UnitScope → Scope.REQUEST`. Any other container works by implementing the protocol above.

### The double-scope rule — enforced by type, not docs

Two author-facing base classes (in `core/contracts/bases.py`); **which one you extend decides who
opens the `UnitScope`**, so the footgun is impossible:

- **`ServerEntrypoint`** — for socket-serving entrypoints whose framework/DI-integration opens the
  per-request scope itself (FastAPI, gRPC, Litestar, Flask). It has **no** `unit_scope` → cannot
  double-open. For the bundled HTTP adapters that framework-side owner is servicewright's own
  `UnitScopeMiddleware` by default; `unit_scope=False` (`MiddlewareConfig` / `LitestarConfig`)
  hands ownership to the DI library's native integration (dishka's `setup_dishka`), and the dishka
  adapter refuses to open a second scope on a request that integration has already scoped.
- **`ScopedEntrypoint`** — for loop/poll-driven entrypoints (scheduler, consumer, daemon, one-shot).
  It provides the **only** sanctioned per-unit API: `async with self.unit_scope(context) as scope:`.

The Host sees only the `Entrypoint` protocol and **never branches on `kind`** (`kind` is a telemetry
label only; it is non-unique — two gRPC entrypoints share `kind="grpc"`).

---

## 4. Unified lifecycle (the Host owns it)

Phase vocabulary: **Bootstrap → Warmup → Ready → Serve → Drain → Cleanup**. One sequence for all
archetypes.

**Startup** (fail-fast, ascending criticality):
1. `observability.configure(settings)` — **first**, so bootstrap failures are observable, and so
   the Host owns SDK init (§9). Order within: `logging → error-tracking → tracing → metrics`.
2. `bootstrap`: build the container (app scope not yet entered).
3. enter `AppScope`.
4. **warmup** (priority-grouped, fail-fast) — *not yet ready, not yet serving*.
5. `pre_start` hooks.
6. `entrypoint.bind(service_context)` for each (allocate server / subscribe topic / register jobs;
   mount the metrics endpoint / FastAPI instrumentation here — they need the live app).
7. **`health.ready = True`** + `post_start` hooks ("now serving").
8. install signal handlers; start every `entrypoint.serve(stop=stop_event)` in one `TaskGroup`;
   block until the stop event is set or an `essential` entrypoint exits.

Startup is **stop-aware**: a signal arriving during warmup or bind abandons the remaining phases at
the next boundary, so a pod that was already asked to terminate never binds ports or reports ready.
Only entrypoints that were actually bound are torn down afterwards.

**Shutdown** (best-effort, reverse order):
1. signal → **`health.ready = False` FIRST** (k8s/LB stops routing before we stop accepting;
   liveness stays OK).
2. `entrypoint.drain(grace)` in reverse — stop intake, finish in-flight within the grace window
   (`DrainTimeoutError` bounds it; the window comes from `AppSpec.drain_grace_seconds`).
3. `entrypoint.stop()` in reverse — hard stop, time-boxed by `AppSpec.cleanup_timeout_seconds`
   (`CleanupTimeoutError`); one entrypoint hanging here cannot block the others.
4. `pre_shutdown` hooks (app scope still alive — flush outbox, emit final events).
5. close `AppScope` (DI finalizers close pools/clients) — **always**, even on failure.
6. `observability.shutdown()` (flush traces, stop metrics server; each flush wrapped in
   `asyncio.to_thread` so it never blocks the loop) — best-effort, `suppress(Exception)`.
7. `post_shutdown` hooks.

Signals are centralized in `core/signals.py` (win32-safe): the first SIGINT/SIGTERM requests the
graceful sequence above, a second one exits immediately with `128 + signum` (cleanup can hang, and
an operator pressing Ctrl+C twice is asking for the process to die now), and the handlers are
removed once the run-loop is over.

`essential: bool` is a per-entrypoint flag controlling whether its exit/failure stops the whole
process. A failure also **propagates out of `Host.run`** once cleanup is done, so the exit code
distinguishes a crash from a graceful stop — a batch job that died mid-run must not look like a
success to whatever supervises it. A shutdown step that blew its budget likewise surfaces, unless a
serve failure is already propagating (it must not be masked).

---

## 5. Entrypoint contract

```python
class Entrypoint(Protocol):
    kind: str            # telemetry label only ("http"|"grpc"|"scheduler"|"kafka"|...)
    essential: bool      # if True, its failure/exit stops the whole process

    async def bind(self, ctx: ServiceContext) -> None: ...      # allocate/subscribe, no traffic yet
    async def serve(self, *, stop: asyncio.Event) -> None: ...  # run until stop; return still accepting
    async def drain(self, grace: float) -> None: ...            # stop intake, finish in-flight
    async def stop(self) -> None: ...                           # hard stop / release
```

Adding a new framework = **one `Entrypoint` adapter + (optionally) one `Plugin`, zero core edits**:

```python
class NatsPlugin:                                  # Litestar on_app_init analogue
    def on_register(self, spec: AppSpec, host: Host) -> None:
        spec.warmers.append(NatsWarmer(...))
        spec.health.add_check("nats", NatsHealthCheck())
        host.add_entrypoint(NatsConsumerEntrypoint(...))
```

Plugins **compose** (Prometheus + Sentry + Kafka + a custom plugin stack with no inheritance
diamond). This is the *only* extension surface.

---

## 6. Per-major-version adapters

Frameworks ship breaking major versions with incompatible APIs (APScheduler 3.x `AsyncIOScheduler`/
`add_job` vs 4.x `AsyncScheduler`/`configure_task` are end-to-end different). The structure lets
multiple majors of the same framework coexist as separate adapters.

**Rule:** an adapter folder is suffixed `<framework><major>` **iff its primary upstream contract
ships breaking majors** (or one module can't express two majors without version branches in hot
paths). API-stable upstreams get the bare name. **Folder name == extra name == `_imports.py` pin
token**, always.

| Adapter | Folder | Public import | Extra | Upstream pin |
|---|---|---|---|---|
| gRPC | `adapters/grpc/` | `from servicewright.adapters.grpc import GrpcEntrypoint` | `[grpc]` | `grpc-server-kit>=0.1,<0.2` |
| FastAPI | `adapters/fastapi/` | `…adapters.fastapi import FastApiEntrypoint` | `[fastapi]` | `fastapi>=0.115,<1` |
| Litestar | `adapters/litestar/` | `…adapters.litestar import LitestarEntrypoint` | `[litestar]` | `litestar>=2.12` |
| APScheduler 4.x | `adapters/apscheduler4/` | `…adapters.apscheduler4 import SchedulerEntrypoint, ScheduledJob` | `[apscheduler4]` | `apscheduler>=4.0.0a5,<5` |
| APScheduler 3.x | `adapters/apscheduler3/` | `…adapters.apscheduler3 import SchedulerEntrypoint, ScheduledJob` | `[apscheduler3]` | `apscheduler>=3.10,<4` |

Both apscheduler folders ship in every wheel (code always coexists); apscheduler is one distribution,
so an *environment* resolves one major and the other adapter's `_imports.py` hard-raises gracefully.
The two adapters share **nothing but the `ScopedEntrypoint` contract and identical public names** →
migrating 3→4 is one import line + one extra. A runnable conformance guard (AST symbol-set equality
in one env + a 2-env behavioral CI matrix) keeps the public surfaces identical. A CI guard fails if
a bare adapter's pin crosses a major (forces the suffix-promotion decision deliberately). Future
FastAPI 1.0 → `adapters/fastapi0/` + `adapters/fastapi1/`, `fastapi/` a one-cycle deprecation shim.

---

## 7. The contract surface (`core/contracts/`)

The entire contract surface lives in one folder, read once. Pure `Protocol`s and stateful
author-facing ABCs live in **separate files** so the distinction is visible:

```
core/contracts/
├── entrypoint.py      # Entrypoint Protocol (runtime_checkable)              — PURE
├── bases.py           # ServerEntrypoint, ScopedEntrypoint                   — STATEFUL ABCs
├── plugin.py          # Plugin protocol (on_register(spec, host))            — PURE
├── container.py       # DependencyContainerProtocol, App/UnitScopeProtocol   — PURE
├── settings.py        # BaseServiceSettingsProtocol                          — PURE
├── health.py          # HealthCheckerProtocol                               — PURE
├── lifecycle.py       # LifecycleHookProtocol                                — PURE
├── warmer.py          # AsyncWarmer (abc.ABC: raise_on_failure/priority)     — STATEFUL ABC
└── observability.py   # the add-on seams (§9): Metrics/Tracer/Span/Sentry/Redactor — PURE
```

---

## 8. Pluggable add-ons layer

servicewright assembles **top tools + strong entrypoints**, and each concern (metrics, tracing,
logging, error-tracking) is a **protocol** in the core with a **selectable, extra-gated backend**.
Adding a backend = drop one module + one extra; zero core changes. This is the "work with any
backend" property.

### 8.1 Concerns → core protocols (`core/contracts/observability.py`, pure)

Four orthogonal concerns + a cross-cutting `Redactor`. The runtime **seam shapes are reused verbatim
from grpc-server-kit**, so kit objects satisfy them structurally with zero coupling; the *names* are
vendor-neutral — the kernel never names an implementation (protocols match by shape, not by name):

```python
class SpanProtocol(Protocol):
    def set_attribute(self, key: str, value: SpanAttributeValue) -> None: ...
    def record_exception(self, exception: Exception) -> None: ...
    def __enter__(self) -> SpanProtocol: ...
    def __exit__(self, et, ev, tb) -> None: ...

class TracerProtocol(Protocol):
    def start_as_current_span(self, name: str) -> SpanProtocol: ...

class CounterProtocol(Protocol):                                 # generic metric instruments — the kernel
    def inc(self, amount: float = 1.0, **labels: str) -> None: ...   # knows no transport and no backend

class HistogramProtocol(Protocol):
    def observe(self, value: float, **labels: str) -> None: ...

class ErrorReporterProtocol(Protocol):                           # vendor-neutral; sentry objects fit structurally
    def capture_exception(self, error: Exception) -> None: ...
    def add_breadcrumb(self, message: str, category: str = "default", level: str = "info",
                       data: dict[str, object] | None = None) -> None: ...
    def set_tags(self, **tags: str) -> None: ...

class Redactor(Protocol):                                        # any dict->dict filter satisfies this; KeyRedactor is the built-in
    def __call__(self, data: dict[str, Any]) -> dict[str, Any]: ...
```

There is **no per-use-case recorder in the kernel**: metrics expose generic *instruments*
(counter / histogram / gauge, minted by `MetricsSinkProtocol.counter()/histogram()/gauge()`), and
each **transport adapter composes its own recorder from them and owns its frozen metric names**
(`adapters/grpc/metrics.py` owns `grpc_requests_total{…}` and the 5-arg
`GrpcServerMetricsRecorder`; `adapters/apscheduler{3,4}/metrics.py` own
`scheduler_job_runs_total{…}` and `SchedulerJobMetricsRecorder`). A new transport or a new backend
therefore never touches the kernel — backends implement three instrument types and automatically
serve every transport. The author-facing
`*Sink` ABCs carry SDK-touching methods (`mount(app)`, `tracer()`, `instrument_fastapi()`) and
therefore live in the **adapter** layer (`adapters/observability/base.py`), keeping
`core/contracts/` free of SDK escape hatches.

### 8.2 Implementation matrix

| Concern | Impls | Extra |
|---|---|---|
| **metrics** | `prometheus` (Victoria Metrics = use `prometheus` + scrape `/metrics`) | `metrics` |
| **tracing** | `otel` | `observability` (+ `fastapi-tracing` for HTTP request spans) |
| **error-tracking** | `sentry` | `sentry` |
| **logging** | `structlog` · `stdlib` | `observability` · (zero-dep) |

Impls live in `adapters/observability/_metrics|_tracing|_errors|_logging/`, each behind an
`_imports.py` hard-raise guard. A `core/observability/registry.py` (name → import-path) lazy-loads
the selected impl. `import servicewright` pays nothing — the guard fires only on resolution.

### 8.3 Configuration — per-app init, per-entrypoint emission

SDK init is process-global (one `sentry_sdk.init`, one global `TracerProvider`, one default
prometheus `REGISTRY`). What's per-entrypoint is **which initialized sink each entrypoint emits to**.
Backend selection is by **open `str` name** (built-ins + anything added via `register_sink`) —
an unknown name **fails fast at Bootstrap** listing the registered backends, so a typo cannot ship
silently — or by **direct sink instance** (the registry-free pure-DI path); an instance wins over
the config name and skips the settings-activation gate (its own `setup()` decides). `main.py`
imports no SDK either way:

```python
spec = AppSpec(service_name="orders", create_container=build_container,
    observability=ObservabilityManager(
        ObsConfig(metrics="prometheus", tracing="otel", error_tracking="sentry", logging="structlog"),
        redactor=KeyRedactor()))                    # one Redactor threaded into BOTH logging and error-tracking
# third-party backend, same mechanics:      register_sink("metrics", "statsd", "myapp.obs:StatsdSink")
# pure-DI alternative (no registry):        ObservabilityManager(metrics=StatsdSink(...))
http = FastApiEntrypoint(routers=[router])          # emits through the app-wide sinks
grpc = GrpcEntrypoint(config=GrpcConfig(port=50051), servicers=register, enable_metrics=True)
await Host(spec).run(settings, entrypoints=[http, grpc])
```

- **Metrics: one backend per process.** A per-entrypoint override is deliberately NOT offered —
  because out-of-process emitters (rest-client-kit, DB/Redis pools,
  omni-box) write the global prometheus registry regardless, so per-entrypoint metrics give one
  service split-brain telemetry.
- **Tracing / error-tracking are global singletons** → if two entrypoints name different backends,
  the Host **fails fast at Bootstrap**.
- **Logging is process-global** (root logger), not per-entrypoint.
- **Config must come from `settings`** (DSN, collector URL, tokens) — `configure(settings)` runs
  *before* the DI container exists, so container-resolved secrets are unavailable at sink setup.
- **Settings sections are named by CONCERN, never by vendor**: `settings.logging` /
  `settings.metrics` / `settings.tracing` / `settings.error_tracking`. Which backend consumes a
  section is `ObsConfig`'s decision; backends may read extra fields off their section via `getattr`.

### 8.4 Host owns the bootstrap (the deliberate inverse of grpc-server-kit)

grpc-server-kit stays **seam-only** (it is one library inside someone else's process; a global SDK
init would be hostile). **servicewright's `Host` IS the process**, so it owns what the kit pushes up:
`sentry_sdk.init`, the OTel `TracerProvider`/exporter, the prometheus exposition. Each impl exposes
`setup(ctx)` / `shutdown()`; the Host calls `setup` in Bootstrap and `shutdown` in Cleanup (reverse
order). Interceptors/middlewares stay **passive seams** touching already-initialized globals.

- **Metrics exposition** is owned by the transport: `FastApiEntrypoint(metrics=True)` mounts an
  in-app `/system/metrics`, and the Prometheus sink additionally starts an own-port WSGI server for
  socket-less `ScopedEntrypoint`-only processes when `settings.metrics.enabled` is set.
- **Idempotency** is per-sink-guarded (not one manager flag that silently skips on re-run).
- **Degradation (uniform):** missing extra = **hard-raise at Bootstrap**; disabled/unconfigured =
  **NullObject**. `shutdown()` is best-effort, never raises.
- **Frozen name contract:** metric names live with their OWNER — the transport adapter that mints
  the instruments (`adapters/grpc/metrics.py` for `grpc_requests_total{…}`,
  `adapters/apscheduler{3,4}/metrics.py` for `scheduler_job_runs_total{…}`); backends receive
  ready names and cannot rename them. Naming discipline (`make_metric_name`) is a pure kernel
  utility (`core/observability/naming.py`).

---

## 9. Package layout

```
servicewright/
├── pyproject.toml                  # extras matrix (§10); core deps = []
└── servicewright/
    ├── __init__.py                 # PUBLIC API: re-exports CORE only — zero adapter imports
    ├── py.typed · testing.py       # testing depends on CORE only
    │
    ├── core/                       # ══ LAYER 1: pure kernel (stdlib + typing ONLY) ══
    │   ├── spec.py                 # AppSpec, BootstrapContext, ServiceContext, Service
    │   ├── constants.py · exceptions.py · signals.py
    │   ├── contracts/              # the whole contract surface (§7) — protocols + author ABCs
    │   ├── lifecycle/              # phases.py · manager.py (sync) · aio.py (async runner)
    │   ├── observability/          # specs.py · config.py · manager.py · null.py · registry.py · aio.py
    │   ├── warmup/                 # aio.py (orchestration; warmers are adapters)
    │   ├── health/                 # registry.py · report.py · aio.py
    │   └── aio/                    # RESERVED for the single loop-owner
    │       └── host.py             # Host: runs N entrypoints; never branches on .kind
    │
    └── adapters/                   # ══ LAYER 2: concrete bindings (extra-gated) ══
        ├── builtin/                # daemon.py · oneshot.py — zero-dep, NO extra
        ├── _uvicorn.py             # shared ASGI server driver (fastapi + litestar)
        ├── grpc/                   # [grpc] — composes grpc-server-kit (external)
        ├── fastapi/                # [fastapi]
        ├── litestar/               # [litestar]
        ├── apscheduler4/ · apscheduler3/   # [apscheduler4] · [apscheduler3] — identical public names
        ├── dishka/                 # [dishka] — DI binding (dishka ⇄ core contracts)
        ├── warmers/                # redis · postgres · kafka  — SOFT-CAPABILITY degradation
        ├── health/                 # redis · postgres          — SOFT-CAPABILITY degradation
        └── observability/          # base.py (sink ABCs; instruments are the seam)
            ├── _metrics/  {prometheus}
            ├── _tracing/  {otel}
            ├── _errors/   {sentry}
            └── _logging/  {structlog, stdlib}

Planned but NOT shipped in 0.1: a `flask/` entrypoint, a `kafka/` consumer entrypoint, and the
datadog metrics/tracing backends. The `[kafka]` extra currently powers the Kafka **warmer** only.
```

`core/` + `adapters/builtin/` + the contract surface have zero third-party deps
(`dependencies = []`). You can write a custom `Entrypoint` with nothing installed.

**Degradation idioms (two, by design):** **HARD-RAISE** in every transport / DI / observability
adapter `_imports.py` (you cannot half-use a transport, DI container, or observability backend);
**SOFT-CAPABILITY** in `adapters/warmers/*` + `adapters/health/*` (`try/except → *_AVAILABLE=False`,
run with a reduced fallback). Coverage excludes guards by `# pragma: no cover`, not by regex.

---

## 10. Extras strategy

PEP 621 `[project.optional-dependencies]`; extra name == import subpath; every adapter lazily
guards its SDK and raises `install servicewright[<extra>]`. One infra extra powers both a warmer and
a health check for the same tech.

```toml
# entrypoint frameworks (per-major where the upstream breaks majors — §6)
fastapi      = ["fastapi>=0.115,<1", "uvicorn[standard]", "deadline-budget", "prometheus-fastapi-instrumentator"]
litestar     = ["litestar>=2.12", "uvicorn[standard]"]
grpc         = ["grpc-server-kit[reflection,channelz,health]>=0.1.0,<0.2"]   # external, on PyPI (§11)
apscheduler4 = ["apscheduler>=4.0.0a5,<5"]
apscheduler3 = ["apscheduler>=3.10,<4"]
kafka        = ["aiokafka>=0.10.0"]            # kafka warmer (the consumer entrypoint is not shipped yet)
# DI
dishka       = ["dishka>=1.4.0"]
# infra warmers + health
redis        = ["redis>=4.2.0"]
postgres     = ["sqlalchemy[asyncio]>=2.0.0,<3.0.0"]
# add-ons (§8) — sinks are implemented DIRECTLY on the SDKs (no service-observability)
observability   = ["opentelemetry-sdk>=1.39", "opentelemetry-exporter-otlp>=1.39", "structlog>=25.5"]  # otel tracing + structlog
metrics         = ["prometheus-client>=0.24.0,<1.0.0"]
sentry          = ["sentry-sdk>=2,<3"]
metrics-datadog = ["datadog>=0.49.0"]          # future
tracing-datadog = ["ddtrace>=2.0.0"]           # future
# convenience bundles
http = servicewright[fastapi]
all  = union of the PyPI-resolvable extras
```

---

## 11. Dependency strategy (consolidation end-state)

A port-study established that vendoring everything is wrong: shared contract libs, if duplicated
while their originals still ship, silently break (two distinct `ContextVar`s break trace
correlation; a second `DeadlineExceededError` class makes the 504 handler's `isinstance` miss; a
forked `Error` envelope splits the wire contract). Per-lib disposition:

| Lib | Disposition |
|---|---|
| `grpc-server-kit` | **External** PyPI (published 2026-08-10, `[grpc]` pins `>=0.1.0,<0.2`). Final direction (supersedes the 2026-07-12 "authority reversed" plan): the kit is fully standalone — it owns its SDK-free seam protocols and streaming-aware interceptor base, depends on grpcio only, and never imports servicewright; servicewright's gRPC adapter composes over it |
| `grpc-interceptor-kit` | **Dropped** (2026-08-11): the kit's own `AsyncServerInterceptor`/`AsyncMetricsInterceptor` replaced every use; no servicewright code imports it |
| `deadline-budget-kit`, `omni-box` | **External** PyPI (already published) |
| `asgi-middlewares-kit` | **Vendored** (2026-07-13) into `adapters/fastapi/middlewares/` (servicewright was its only consumer; dropped the dead-`CSRFMiddleware` `itsdangerous` dep) |
| `service-observability` | **Dropped** (decided 2026-07-12): sinks implement directly on the SDKs (sentry-sdk / opentelemetry / prometheus-client / structlog); the zero-dep protocol surface lives in `core/contracts/observability.py`, frozen metric names with their transport adapters (e.g. `adapters/grpc/metrics.py`) |
| `service-context` | **Dropped** (2026-08-11, supersedes the 2026-07-13 soft-capability plan): the root problem — correlation — is solved natively. Canonical store = `core/context.py` (transport-neutral ContextVars, written by the FastAPI middleware and the gRPC unit-scope interceptor); bridges = pluggable `ContextSetter`s (structlog always, `OtelBaggageSetter` when otel installed — W3C Baggage propagates cross-service). Platforms bridge their own contextvars via `MiddlewareConfig.context_setters`; long-term direction is OTel baggage |
| `service-contracts` | **Dropped** (2026-08-11): the root problem splits — the *mechanism* (exception→response mapping, masking) is servicewright's (`core/errors.py`: `ServiceError` taxonomy + `HttpErrorRendererProtocol`, default RFC 9457 problem+json; gRPC maps `ErrorKind`→`StatusCode`); the *contract* (codes, en/ru messages) is the platform's — its handlers register via native `add_exception_handler` / a custom renderer. The lib stays a private ai-ops contract |

**Net:** `core/` absorbs only the zero-dep protocol surface (the 4 kit seams + `Redactor` +
`Processor` + `LogLevelStr`). One vendored slice (`asgi-middlewares-kit`). Everything else is an
external dependency.

---

## 12. Archetype mapping

| Archetype | Entrypoint adapter | Base | UnitScope opened by | In-lib? |
| --- | --- | --- | --- | --- |
| Request/response API | `fastapi` / `litestar` / `grpc` | `ServerEntrypoint` | framework DI integration | yes (extras) |
| Scheduled / cron | `apscheduler4` / `apscheduler3` | `ScopedEntrypoint` | per-job, by entrypoint | yes |
| Message / event consumer | write your own on `ScopedEntrypoint` | `ScopedEntrypoint` | per-message, by entrypoint | not yet |
| Background daemon / stream | `builtin/daemon` | `ScopedEntrypoint` | one long scope (optional) | yes, zero-dep built-in |
| One-shot / batch / CLI | `builtin/oneshot` | `ScopedEntrypoint` | one scope, run once | yes, zero-dep built-in |

**Satellites (separate libs, consumed as `AppScope` singletons via Plugins — never `Entrypoint`s):**
DB/UoW (`sqlalchemy-*`), outbox/inbox (`omni-box`), `rest-client-kit`, `deadline-budget`.

---

## 13. End-user example (HTTP API + cron in ONE process)

```python
import asyncio

from apscheduler.triggers.interval import IntervalTrigger

from servicewright import AppSpec, ObsConfig, ObservabilityManager, Service, run
from servicewright.adapters.apscheduler4 import ScheduledJob, SchedulerEntrypoint
from servicewright.adapters.fastapi import FastApiEntrypoint


def build_service() -> Service[Settings, DishkaContainer]:
    spec = AppSpec(
        service_name="orders-service",
        create_container=build_container,
        observability=ObservabilityManager(ObsConfig(metrics="prometheus", tracing="otel")),
    )
    spec.warmers.append(PostgresWarmer())                 # primed before readiness flips true
    spec.health.add_check("postgres", PostgresHealthCheck())

    http = FastApiEntrypoint(routers=[router])            # ServerEntrypoint, kind="http"
    cron = SchedulerEntrypoint(jobs=[                     # ScopedEntrypoint, kind="scheduler"
        ScheduledJob(id="sweep", func=sweep, trigger=IntervalTrigger(minutes=5)),
    ])
    return Service(spec, entrypoints=[http, cron])


if __name__ == "__main__":
    # Blocks until SIGTERM, drains both entrypoints, and exits non-zero if either died.
    asyncio.run(run(build_service(), Settings()))
```

---

## 14. Migration from the prototype

The conceptual model (§1–§5) carries forward; the structure is the change. Key moves:

- `entrypoint/protocol.py` → `core/contracts/{entrypoint.py (pure protocol), bases.py (ABCs)}`;
  `entrypoint/plugin.py` → `core/contracts/plugin.py`; `core/protocols.py` →
  `core/contracts/{container,settings,health}.py`.
- `core/host.py` → `core/aio/host.py` (the one reserved loop-owner). Async halves of
  lifecycle/observability/warmup/health move to co-located `<domain>/aio.py`.
- `entrypoints/{daemon,oneshot}` → `adapters/builtin/`; the `_grpc`/`_http`/`_litestar`/`_scheduler`
  private-pkg + public-facade double-layer collapses to one flat `adapters/<framework[major]>/`;
  the misnamed-generic `_http` splits 1:1 into `adapters/fastapi/` + `adapters/litestar/`;
  `entrypoints/scheduler.py` → `adapters/apscheduler4/` + a **new** parallel `adapters/apscheduler3/`.
- `warmup/aio/warmers/*` + `health/checks/*` move **out of core** into `adapters/{warmers,health}/`.
- `contrib/dishka.py` → `adapters/dishka/`.
- Observability: protocols → `core/contracts/observability.py`; manager/specs/config/null/registry →
  `core/observability/`; impls → `adapters/observability/_*`. `host.py` lines that call
  `configure`/`shutdown` are unchanged; the multi-sink generalization lives in `ObservabilityManager`.
- Extras: `[scheduler]` → `[apscheduler4]` + new `[apscheduler3]`; add the add-on extras (§10);
  `[grpc]` repointed to the published PyPI `grpc-server-kit` (grpc-interceptor-kit dropped).

---

## 15. Open decisions & non-goals

**Still open (human call):**
- Datadog name→tag mapping strictness (canonical-in-servicewright vs adapter-defined).
- `shutdown()` flush via `asyncio.to_thread` vs an optional `async aclose()` on sinks.
- Whether to ship a concrete first add-on adapter set beyond `prometheus`/`otel`/`sentry`/`structlog`
  (datadog proves the "different client" path) at v1.

**Non-goals:**
- No infra clients (DB/cache/broker), outbox, or idempotency here — separate libs injected as
  `AppScope` singletons.
- The `Host` never branches on `kind`; no `if transport == ...` in the kernel.
- One extension mechanism only (`Plugin`); no inheritance-by-transport.
- `core/` never imports a third-party SDK; observability SDKs live behind add-on adapters only.
