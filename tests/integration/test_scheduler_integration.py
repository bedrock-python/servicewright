"""Integration test driving a real in-process APScheduler ``AsyncScheduler``.

Marked ``integration`` so it does NOT run under ``-m unit``. It exercises the
actual APScheduler v4 primitives (no mocking): a real ``AsyncScheduler`` with a
short ``IntervalTrigger`` fires a job inside a real per-job ``UnitScope``, then
the Service is gracefully stopped.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from servicewright import AppSpec, Service
from servicewright.adapters.apscheduler4 import ScheduledJob, SchedulerEntrypoint
from servicewright.testing import FakeContainer, FakeSettings

pytestmark = pytest.mark.integration


async def test__scheduler_entrypoint__real_scheduler__fires_the_job_inside_a_unit_scope() -> None:
    container = FakeContainer(provides={str: "injected-dep"})
    spec: AppSpec[Any, Any] = AppSpec(service_name="scheduler-it-service", create_container=lambda _s: container)

    runs: list[dict[str, Any]] = []

    async def record_run(scope: Any) -> None:
        # Resolve a dependency from the REAL per-job unit scope and capture the
        # context carried by that scope (job_id + run_id).
        dep = await scope.get(str)
        runs.append({"dep": dep, "context": dict(scope.context or {})})

    job = ScheduledJob(id="tick", func=record_run, trigger=IntervalTrigger(seconds=0.1))
    ep = SchedulerEntrypoint(jobs=[job])
    service = Service(spec, entrypoints=[ep])

    stop = asyncio.Event()

    async def wait_for_first_run_then_stop() -> None:
        while not runs:
            await asyncio.sleep(0.02)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(service.run(FakeSettings(), stop=stop), wait_for_first_run_then_stop()),
        timeout=30.0,
    )

    # The job ran at least once inside a real unit scope.
    assert runs
    assert runs[0]["dep"] == "injected-dep"
    assert runs[0]["context"]["job_id"] == "tick"
    assert runs[0]["context"]["run_id"]
    # One unit scope per run; readiness flipped off on graceful stop.
    assert container.unit_scopes_opened >= 1
    assert spec.health.ready is False


async def test__scheduler_entrypoint_drain__real_scheduler__lets_the_in_flight_job_finish() -> None:
    """REGRESSION (real APScheduler): graceful shutdown must not hard-kill an in-flight job.

    Drives a real ``Service``/``Host`` (so ``bind`` -> ``serve`` -> ``drain`` ->
    ``stop`` run in the Host's own task, exactly as in production) backed by a real
    ``AsyncScheduler``. A job goes in-flight (sets ``started`` then sleeps); the
    Service is asked to shut down while it runs. The job MUST finish on its own
    within the grace window — never cancelled at zero grace.

    On the OLD code this fails: ``serve()`` tore the scheduler down the instant
    ``stop`` was set (hard-cancelling the running job), so ``drain`` was a no-op.

    NOTE: this MUST go through ``Service.run`` rather than hand-calling
    bind/serve/drain/stop across tasks — ``AsyncScheduler`` holds an anyio task
    group bound to the task that entered it, so entering it in one task and
    closing it in another raises a spurious ``CancelledError``. The real Host
    enters (bind) and closes (stop) the scheduler in the same ``run()`` task.
    """
    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="scheduler-drain-it", create_container=lambda _s: container)

    started = asyncio.Event()
    finished = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_job(_scope: Any) -> None:
        started.set()
        try:
            await asyncio.sleep(0.4)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finished.set()

    job = ScheduledJob(id="slow", func=slow_job, trigger=IntervalTrigger(seconds=0.05))
    service = Service(spec, entrypoints=[SchedulerEntrypoint(jobs=[job])])

    stop = asyncio.Event()

    async def trigger_shutdown_mid_flight() -> None:
        # Wait until a job is actually in flight, then ask the Host to shut down.
        await asyncio.wait_for(started.wait(), timeout=10.0)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(service.run(FakeSettings(), stop=stop), trigger_shutdown_mid_flight()),
        timeout=30.0,
    )

    # The in-flight job completed gracefully; it was NOT hard-cancelled.
    assert finished.is_set()
    assert not cancelled.is_set()
    assert spec.health.ready is False
