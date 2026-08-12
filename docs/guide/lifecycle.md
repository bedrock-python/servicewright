# Lifecycle

One sequence runs every archetype — an HTTP API, a cron job and a batch script go through exactly
the same phases:

```
Bootstrap → Warmup → Ready → Serve → Drain → Cleanup
```

## Startup

1. **Observability is configured first**, so bootstrap failures are already observable.
2. The container is built and the **application scope** is entered.
3. **Warmup** runs in priority groups, fail-fast — connection pools are primed *before* anything
   reports ready.
4. `pre_start` hooks, then `bind()` on every entrypoint: allocate the socket, subscribe the topic,
   register the jobs. Nothing is accepted yet.
5. **`health.ready = True`** and `post_start` hooks.
6. Every `serve(stop=...)` runs in one `TaskGroup`.

Startup is **stop-aware**. A signal that arrives during warmup or bind abandons the remaining
phases at the next boundary, so a pod that was already asked to terminate never binds ports and
never reports itself ready:

```python
# SIGTERM during a 45s Kafka warmup:
#   the warmup is abandoned, bind() is skipped, readiness stays false,
#   and the process goes straight to cleanup.
```

A failure in `bind()` — a port already in use, a missing topic — aborts startup with that exception.
The HTTP adapters open their listening socket in `bind()` precisely so a port clash fails here,
loudly, instead of leaving a process that reports ready and serves nothing.

## Shutdown

1. **`health.ready = False` first** — load balancers stop routing before anything stops accepting.
2. `drain(grace)` in reverse order: stop intake, let in-flight work finish inside the window.
3. `stop()` in reverse order: hard stop, time-boxed.
4. `pre_shutdown` hooks (the app scope is still alive — flush the outbox, emit final events).
5. The application scope closes — DI finalizers close pools and clients.
6. Observability flushes, `post_shutdown` hooks run.

`serve()` returning is the signal that the entrypoint is *still accepting* and ready to be torn down
in order. That is why the HTTP entrypoints return from `serve()` without shutting uvicorn down —
closing the listener there would take the readiness endpoint down before the router knew, and make
the drain window meaningless.

## Budgets

```python
spec = AppSpec(
    service_name="orders",
    create_container=build_container,
    drain_grace_seconds=30.0,      # in-flight work window per entrypoint
    cleanup_timeout_seconds=10.0,  # per post-drain step: stop(), hooks, telemetry flush
)
```

A step that blows its budget raises `DrainTimeoutError` / `CleanupTimeoutError` — logged, and
surfaced from `run()` when nothing else is already propagating. One entrypoint hanging in `stop()`
can never block the others.

## Exit codes

A crash must not look like a graceful stop:

- an **essential** entrypoint whose `serve()` raises propagates that exception out of `run()` after
  cleanup — so a batch job that died mid-run exits non-zero and Kubernetes retries it;
- a non-essential entrypoint's failure is logged and the rest keep serving;
- a clean stop returns `None` and exits 0.

## Signals

The first `SIGINT`/`SIGTERM` requests the graceful sequence above. A **second** signal exits
immediately with `128 + signum` — cleanup can hang on a dead connection pool, and an operator
pressing Ctrl+C twice is asking for the process to die now. Handlers are removed when the run-loop
ends, so an embedded service never leaves them pointing at a closed event loop.

When you supply your own stop event, servicewright installs no handlers at all:

```python
stop = asyncio.Event()
await service.run(settings, stop=stop)   # you own the signals
```

## Hooks

```python
spec.lifecycle.add_pre_start_hook(migrate_database)
spec.lifecycle.add_post_start_hook(announce_ready)
spec.lifecycle.add_pre_shutdown_hook(flush_outbox)   # app scope still open
spec.lifecycle.add_post_shutdown_hook(final_report)  # after the scope closed
```

Start hooks are fail-fast (a raising hook aborts startup); shutdown hooks are best-effort — one
failing hook is logged and the rest still run.
