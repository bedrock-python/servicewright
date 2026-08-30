# Scheduler

```bash
pip install "servicewright[apscheduler4]"    # or [apscheduler3]
```

Cron and interval jobs as an entrypoint. Every run gets its own
[unit scope](../concepts/dependency-injection.md) — which is what makes "a scheduled job is an
HTTP request" structurally true, not just a slogan.

```python
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from servicewright import Service, UnitScopeProtocol
from servicewright.adapters.apscheduler4 import ScheduledJob, SchedulerEntrypoint


async def sweep_expired_orders(scope: UnitScopeProtocol) -> None:
    orders = await scope.get(OrderRepository)
    await orders.delete_expired()


async def send_daily_report(scope: UnitScopeProtocol, recipient: str) -> None:
    mailer = await scope.get(Mailer)
    await mailer.send_report(recipient)


cron = SchedulerEntrypoint(jobs=[
    ScheduledJob(
        id="sweep",
        func=sweep_expired_orders,
        trigger=IntervalTrigger(minutes=5),
        max_instances=1,
    ),
    ScheduledJob(
        id="daily-report",
        func=send_daily_report,
        trigger=CronTrigger(hour=6, minute=0),
        args=("ops@example.com",),
    ),
])

service = Service(spec, entrypoints=[cron])
```

## The job function

```python
async def my_job(scope: UnitScopeProtocol, *args, **kwargs) -> None: ...
```

The scope always comes **first**; your `args` and `kwargs` follow. Resolve everything you need
from the scope — a fresh database session, a use case, a client — exactly as a request handler
would.

The unit-scope context carries `{"job_id": ..., "run_id": ...}`, so a container can make them
injectable, and every log line emitted during the run carries them.

## `ScheduledJob`

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | `str` | Unique job id. Duplicates raise `DuplicateScheduleError` at bind. |
| `func` | async callable | `func(scope, *args, **kwargs)` |
| `trigger` | APScheduler trigger | `IntervalTrigger`, `CronTrigger`, `DateTrigger`, ... |
| `args` | sequence | Positional arguments after `scope` |
| `kwargs` | mapping | Keyword arguments |
| `max_instances` | `int \| None` | Concurrent runs allowed for this id |
| `misfire_grace_time` | `float \| None` | Seconds after which a missed run is skipped |
| `coalesce` | see below | Whether missed runs collapse into one |

## What happens when a job fails

It is logged with its `job_id`, `run_id` and duration — and the scheduler keeps running. One bad
run must never take the process down, because the next run may well succeed.

```
Job execution started      job_id=sweep run_id=6a1f...
Job execution failed       job_id=sweep run_id=6a1f... duration_seconds=0.42
```

Successful runs log `Job execution completed` with the same fields.

If you *want* a failing job to stop the service, raise out of a `pre_start` hook after checking
whatever invariant matters, or use a [one-shot entrypoint](daemon-and-oneshot.md) instead.

## Metrics

```python
SchedulerEntrypoint(jobs=[...], enable_metrics=True)
```

Records every run through whatever metrics backend the app configured — a null object when
metrics are off, so the flag is always safe:

| Metric | Type | Labels |
| --- | --- | --- |
| `scheduler_job_runs_total` | counter | `job_id`, `outcome`, `error_kind` |
| `scheduler_job_duration_seconds` | histogram | `job_id` |
| `scheduler_job_last_success_timestamp_seconds` | gauge | `job_id` |
| `scheduler_job_in_progress` | gauge | `job_id` |

`outcome` is `ok`, `error` or `cancelled` (the Host drained or stopped the scheduler mid-run).
`error_kind` refines `error` with the library's own taxonomy: the `ErrorKind` of a
[`ServiceError`](../concepts/errors.md) — `validation`, `not_found`, ... — or `unexpected` for
any other exception, and `""` when the run did not fail. A job refused by a business rule and a
lost database connection are therefore different series, so an error-rate alert can ignore the
former:

```promql
sum by (job_id) (rate(scheduler_job_runs_total{outcome="error", error_kind="unexpected"}[15m])) > 0
```

Two details the recorder gets right so that you do not have to:

- **The last-success timestamp moves only on `ok`.** A job that fails every time goes stale,
  which is exactly what a staleness alert exists to catch:

  ```promql
  time() - scheduler_job_last_success_timestamp_seconds{job_id="mailout"} > 2 * 3600
  ```

  The gauge is process-local: after a restart the series is absent until the first success, so
  pair the rule with `absent(...)` or an `or vector(0)` fallback.

- **`in_progress` is released on every exit path**, error and cancellation included, so a
  "job is stuck" rule (`scheduler_job_in_progress > 0` for longer than the job should take)
  cannot become permanently true.

Cancelled runs are counted but not sampled into the duration histogram — they did not finish.
Durations bucket up to 1800 s (`DEFAULT_SCHEDULER_BUCKETS`), since jobs span orders of magnitude.
Prefix everything with `metrics_prefix="myapp"`. Both APScheduler adapters record identically.

The recorder is public: compose `SchedulerJobMetricsRecorder(ctx.observability.metrics)` in an
entrypoint of your own, or reuse `classify_outcome` to keep the same vocabulary in a recorder of
yours — see [Metrics recorders](observability-backends.md#metrics-recorders).

## Shutdown

`drain(grace)` **pauses every schedule** so no new job fires, then waits for the runs already in
flight, polling until they finish or the grace window expires. A timeout is logged as a warning —
it does not raise.

`stop()` then tears the scheduler down.

That ordering matters. Calling APScheduler's own `stop()` during drain would hard-cancel in-flight
jobs with zero grace, which is exactly what you do not want half-way through a database
transaction.

!!! tip

    Give long jobs enough room: `AppSpec(drain_grace_seconds=60.0)`. The scheduler's drain gets
    the Host's grace window, and it uses all of it if it needs to.

## APScheduler 3 vs 4

Both adapters expose an **identical** public surface — `SchedulerEntrypoint`, `SchedulerPlugin`,
`ScheduledJob`, `SchedulerJobMetricsRecorder`, `SchedulerError`, `DuplicateScheduleError` —
enforced by a conformance test. A
migration is one import line:

```python
from servicewright.adapters.apscheduler3 import ScheduledJob, SchedulerEntrypoint
# ↓
from servicewright.adapters.apscheduler4 import ScheduledJob, SchedulerEntrypoint
```

One field differs, because APScheduler itself changed it:

| | APScheduler 3.x | APScheduler 4.x |
| --- | --- | --- |
| `coalesce` | `bool` | `CoalescePolicy` enum |

!!! warning "They cannot share an environment"

    APScheduler 3 and 4 are the same distribution with incompatible majors, so only one can be
    installed. That is also why the `all` extra bundles `apscheduler4` and excludes
    `apscheduler3`.

Behaviourally the two adapters differ only where the libraries force it. APScheduler 3.x exposes
no in-flight job set and its executor cancels pending futures on shutdown regardless of the `wait`
flag, so that adapter tracks its own running jobs to make the grace window real.

## Lifecycle notes

- **`bind()`** creates the scheduler and registers the schedules. It is not started yet, and
  duplicate ids fail here.
- **`serve()`** starts the scheduler and waits for the stop event.
- **`drain(grace)`** pauses schedules and waits for running jobs.
- **`stop()`** tears the scheduler down.

The scheduler deliberately outlives `serve()`, because `drain()` needs a live scheduler to act on.

## Plugin form

```python
from servicewright.adapters.apscheduler4 import SchedulerPlugin

service = Service(spec, plugins=[SchedulerPlugin(jobs=[sweep_job])])
```

## Running jobs next to an API

This is the normal case, not an edge case:

```python
service = Service(spec, entrypoints=[http, cron])
```

One container, one warmup, one shutdown. When the sweep grows heavy enough to deserve its own
deployment, build a second `Service` from the same spec with only the scheduler in it.
