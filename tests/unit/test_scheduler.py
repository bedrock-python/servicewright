"""Unit tests for the scheduler entrypoint (servicewright.adapters.apscheduler4).

The APScheduler ``AsyncScheduler`` is mocked: it is patched in the entrypoint
module with a :class:`_FakeAsyncScheduler` so these tests never start a real
scheduler, data store, or event broker. One ``integration``-marked test (in
tests/integration) drives a real ``AsyncScheduler``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from servicewright import AppSpec, Entrypoint, Plugin, Service
from servicewright.core.health import HealthRegistry
from servicewright.core.spec import BootstrapContext, ServiceContext
from servicewright.testing import FakeContainer, FakeScope, FakeSettings


def _apscheduler4_installed() -> bool:
    """APScheduler 4 dropped ``schedulers.asyncio``; that is how the majors differ.

    The distribution metadata is unreliable here (the v4 pre-releases report no
    version at all), so the majors are told apart the way the adapters' own
    import guards do: by what is importable.
    """
    try:
        return importlib.util.find_spec("apscheduler.schedulers.asyncio") is None
    except ModuleNotFoundError:
        return True


APSCHEDULER4_INSTALLED = _apscheduler4_installed()

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not APSCHEDULER4_INSTALLED, reason="requires the [apscheduler4] environment"),
]

if APSCHEDULER4_INSTALLED:  # pragma: no branch - import guard for the aps3 environment
    from servicewright.adapters.apscheduler4 import (
        DuplicateScheduleError,
        ScheduledJob,
        ScheduledJobFunc,
        SchedulerEntrypoint,
        SchedulerError,
        SchedulerPlugin,
    )
    from servicewright.adapters.apscheduler4 import entrypoint as entrypoint_mod
    from servicewright.adapters.apscheduler4._imports import RunState


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeTrigger:
    """Stand-in for an ``apscheduler.abc.Trigger`` (never actually fired)."""

    def __init__(self, name: str = "trigger") -> None:
        self.name = name


class _FakeSchedule:
    """Stand-in for an ``apscheduler.Schedule`` row (only ``id`` + ``paused``)."""

    def __init__(self, schedule_id: str) -> None:
        self.id = schedule_id
        self.paused = False


class _FakeJob:
    """An in-flight job token tracked in the fake's ``_running_jobs`` set."""


class _FakeAsyncScheduler:
    """Stand-in for ``apscheduler.AsyncScheduler``.

    It faithfully models the parts of the real 4.0.0a6 contract the entrypoint
    relies on, without any real data store or event broker:

    - ``_running_jobs`` is the live in-flight job set (the entrypoint polls it in
      ``drain``); ``run_one_job`` adds/removes from it to simulate a job.
    - ``pause_schedule`` only *signals* (flips ``Schedule.paused``); it never
      cancels in-flight jobs. New fires after a pause are suppressed.
    - ``stop`` is the hard teardown: it marks running jobs as cancelled (the real
      ``stop()`` cancels the scheduler cancel scope) and stops the scheduler.
    - ``__aexit__`` performs the same teardown as ``stop`` (the real
      ``AsyncScheduler`` registers ``stop`` as its exit callback).
    """

    instances: list[_FakeAsyncScheduler] = []

    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.started = False
        self.stop_calls = 0
        self._state = RunState.stopped
        self.add_schedule_calls: list[dict[str, Any]] = []
        self.configure_task_calls: list[dict[str, Any]] = []
        self.target: Any = None
        self._schedules: list[_FakeSchedule] = []
        self._running_jobs: set[_FakeJob] = set()
        self.cancelled_jobs = 0
        _FakeAsyncScheduler.instances.append(self)

    async def __aenter__(self) -> _FakeAsyncScheduler:
        self.entered = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.exited = True
        await self._teardown()

    @property
    def state(self) -> RunState:
        return self._state

    async def configure_task(self, func_or_task_id: Any, **kwargs: Any) -> None:
        self.configure_task_calls.append({"task": func_or_task_id, **kwargs})

    async def add_schedule(self, func_or_task_id: Any, trigger: Any, **kwargs: Any) -> str:
        self.target = func_or_task_id
        schedule_id: str = kwargs.get("id", "schedule")
        self.add_schedule_calls.append({"func": func_or_task_id, "trigger": trigger, **kwargs})
        self._schedules.append(_FakeSchedule(schedule_id))
        return schedule_id

    async def get_schedules(self) -> list[_FakeSchedule]:
        return list(self._schedules)

    async def pause_schedule(self, schedule_id: str) -> None:
        for schedule in self._schedules:
            if schedule.id == schedule_id:
                schedule.paused = True

    async def start_in_background(self) -> None:
        self.started = True
        self._state = RunState.started

    async def stop(self) -> None:
        self.stop_calls += 1
        await self._teardown()

    async def _teardown(self) -> None:
        # Mirror the real ``stop()``: hard-cancel any in-flight jobs.
        self.cancelled_jobs += len(self._running_jobs)
        self._running_jobs.clear()
        self._state = RunState.stopped

    # --- helpers used by tests to simulate a real in-flight job lifecycle --- #
    def all_paused(self) -> bool:
        return bool(self._schedules) and all(s.paused for s in self._schedules)


def _make_service_ctx(
    container: FakeContainer,
    *,
    service_name: str = "svc",
    health: HealthRegistry | None = None,
) -> ServiceContext:
    bootstrap = BootstrapContext(
        settings=FakeSettings(),
        service_name=service_name,
        container=container,
        lifecycle=object(),  # type: ignore[arg-type]
    )
    return ServiceContext(
        bootstrap=bootstrap,
        app_scope=FakeScope(),
        health=health or HealthRegistry(),
    )


@pytest.fixture(autouse=True)
def patched_scheduler(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncScheduler]:
    """Patch ``AsyncScheduler`` in the entrypoint module with the fake."""
    _FakeAsyncScheduler.instances = []
    monkeypatch.setattr(entrypoint_mod, "AsyncScheduler", _FakeAsyncScheduler)
    return _FakeAsyncScheduler


def _job(job_id: str, func: ScheduledJobFunc, **kwargs: Any) -> ScheduledJob:
    return ScheduledJob(id=job_id, func=func, trigger=_FakeTrigger(job_id), **kwargs)


async def _noop(_scope: Any, *args: Any, **kwargs: Any) -> None:
    return None


def _set_event() -> asyncio.Event:
    """A stop event that is already set (so ``serve`` returns immediately)."""
    event = asyncio.Event()
    event.set()
    return event


# --------------------------------------------------------------------------- #
# Entrypoint protocol conformance & attributes
# --------------------------------------------------------------------------- #
def test__scheduler_entrypoint__constructed__satisfies_the_protocol_and_owns_a_unit_scope() -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    assert isinstance(ep, Entrypoint)
    assert ep.kind == "scheduler"
    assert ep.essential is True
    # ScopedEntrypoint exposes the sanctioned per-unit scope API.
    assert hasattr(ep, "unit_scope")


def test__scheduler_entrypoint__kind_and_essential_overridden__reports_them() -> None:
    ep = SchedulerEntrypoint(jobs=[], kind="cron", essential=False)
    assert ep.kind == "cron"
    assert ep.essential is False


def test__scheduler_entrypoint__constructed_with_jobs__exposes_them() -> None:
    jobs = [_job("a", _noop), _job("b", _noop)]
    ep = SchedulerEntrypoint(jobs=jobs)
    assert ep.jobs == jobs


# --------------------------------------------------------------------------- #
# bind() — validation + registry
# --------------------------------------------------------------------------- #
async def test__scheduler_bind__unique_job_ids__registers_each_of_them() -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop), _job("b", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    assert set(ep._registry) == {"a", "b"}


async def test__scheduler_bind__duplicate_job_ids__raises() -> None:
    ep = SchedulerEntrypoint(jobs=[_job("dup", _noop), _job("dup", _noop), _job("ok", _noop)])
    with pytest.raises(DuplicateScheduleError, match="dup") as exc:
        await ep.bind(_make_service_ctx(FakeContainer()))
    assert exc.value.duplicates == ["dup"]
    assert isinstance(exc.value, SchedulerError)


async def test__scheduler_bind__called__captures_the_container_for_per_job_scopes() -> None:
    container = FakeContainer()
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(container))
    # unit_scope() must not raise once bound.
    async with ep.unit_scope({"k": "v"}):
        pass
    assert container.unit_scopes_opened == 1


async def test__scheduler_bind__called__enters_the_scheduler_and_registers_schedules(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    # bind() (not serve()) enters the scheduler and registers the schedules, so
    # the scheduler outlives serve() for drain()/stop() to act on.
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    scheduler = patched_scheduler.instances[0]
    assert scheduler.entered is True
    assert scheduler.started is False  # not started until serve()
    assert ep._scheduler is scheduler
    assert len(scheduler.add_schedule_calls) == 1


async def test__scheduler_bind__registration_fails__closes_the_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If schedule registration raises mid-bind, the entered scheduler must be
    # torn down (exit stack closed) and the entrypoint left unbound.
    class _ExplodingScheduler(_FakeAsyncScheduler):
        async def configure_task(self, func_or_task_id: Any, **kwargs: Any) -> None:
            raise RuntimeError("configure boom")

    monkeypatch.setattr(entrypoint_mod, "AsyncScheduler", _ExplodingScheduler)

    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    with pytest.raises(RuntimeError, match="configure boom"):
        await ep.bind(_make_service_ctx(FakeContainer()))

    scheduler = _ExplodingScheduler.instances[-1]
    assert scheduler.exited is True  # the entered scheduler was torn down
    assert ep._scheduler is None
    assert ep._stack is None


# --------------------------------------------------------------------------- #
# serve() — registers schedules, starts, waits, stops
# --------------------------------------------------------------------------- #
async def test__scheduler_serve__jobs_configured__registers_them_with_trigger_and_id(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    trigger = _FakeTrigger("every-5m")
    job = ScheduledJob(id="sweep", func=_noop, trigger=trigger)
    ep = SchedulerEntrypoint(jobs=[job])
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()
    stop.set()
    await ep.serve(stop=stop)

    scheduler = patched_scheduler.instances[0]
    # bind() enters the scheduler; serve() only starts it and must NOT tear it
    # down (the Host drains/stops it AFTER serve returns).
    assert scheduler.entered is True
    assert scheduler.started is True
    assert scheduler.exited is False
    assert ep._scheduler is scheduler
    assert len(scheduler.add_schedule_calls) == 1
    call = scheduler.add_schedule_calls[0]
    assert call["trigger"] is trigger
    assert call["id"] == "sweep"
    # The schedule references the per-job task id (a serializable string), and
    # the dispatch target receives only the job id (not a closure).
    assert call["func"] == "sweep"
    assert call["args"] == ("sweep",)
    # A distinct per-job task is registered, backed by the shared _dispatch.
    assert scheduler.configure_task_calls == [{"task": "sweep", "func": ep._dispatch}]
    # No optional passthrough by default.
    assert "misfire_grace_time" not in call
    assert "coalesce" not in call


async def test__scheduler_serve__optional_job_options_set__passes_them_through(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    coalesce = object()
    job = ScheduledJob(
        id="opt",
        func=_noop,
        trigger=_FakeTrigger(),
        max_instances=3,
        misfire_grace_time=12.5,
        coalesce=coalesce,  # type: ignore[arg-type]
    )
    ep = SchedulerEntrypoint(jobs=[job])
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()
    stop.set()
    await ep.serve(stop=stop)

    scheduler = patched_scheduler.instances[0]
    # max_instances -> per-task max_running_jobs via configure_task on the per-job task.
    assert scheduler.configure_task_calls == [{"task": "opt", "func": ep._dispatch, "max_running_jobs": 3}]
    call = scheduler.add_schedule_calls[0]
    assert call["func"] == "opt"
    assert call["misfire_grace_time"] == 12.5
    assert call["coalesce"] is coalesce


async def test__scheduler_serve__max_instances_unset__omits_it(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    stop = asyncio.Event()
    stop.set()
    await ep.serve(stop=stop)
    # A per-job task is still registered, but without forcing max_running_jobs.
    assert patched_scheduler.instances[0].configure_task_calls == [{"task": "a", "func": ep._dispatch}]


async def test__scheduler_serve__stop_set__returns(patched_scheduler: type[_FakeAsyncScheduler]) -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()

    async def release() -> None:
        while not (patched_scheduler.instances and patched_scheduler.instances[0].started):
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(ep.serve(stop=stop), release())
    scheduler = patched_scheduler.instances[0]
    assert scheduler.started is True
    # serve() returns when stop is set but leaves the scheduler ALIVE so the Host
    # can still drain/stop it; it does NOT null the reference or tear down.
    assert scheduler.exited is False
    assert ep._scheduler is scheduler
    assert ep._stopped is False


async def test__scheduler_serve__no_jobs__still_starts_the_scheduler(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    ep = SchedulerEntrypoint(jobs=[])
    await ep.bind(_make_service_ctx(FakeContainer()))
    stop = asyncio.Event()
    stop.set()
    await ep.serve(stop=stop)
    scheduler = patched_scheduler.instances[0]
    assert scheduler.started is True
    assert scheduler.add_schedule_calls == []


# --------------------------------------------------------------------------- #
# _dispatch — per-job unit scope, logging, failure isolation
# --------------------------------------------------------------------------- #
async def test__scheduler_dispatch__job_fires__runs_it_inside_a_unit_scope() -> None:
    container = FakeContainer(provides={str: "dep"})
    seen: list[Any] = []

    async def job_func(scope: Any, *args: Any, **kwargs: Any) -> None:
        seen.append((scope, args, kwargs))
        assert await scope.get(str) == "dep"

    ep = SchedulerEntrypoint(
        jobs=[ScheduledJob(id="j", func=job_func, trigger=_FakeTrigger(), args=(1, 2), kwargs={"k": "v"})]
    )
    await ep.bind(_make_service_ctx(container))

    await ep._dispatch("j")

    assert container.unit_scopes_opened == 1
    unit_ctx = container.unit_contexts[0]
    assert unit_ctx is not None
    assert unit_ctx["job_id"] == "j"
    assert isinstance(unit_ctx["run_id"], str) and unit_ctx["run_id"]
    # func(scope, *args, **kwargs)
    scope, args, kwargs = seen[0]
    assert scope.context == unit_ctx
    assert args == (1, 2)
    assert kwargs == {"k": "v"}


async def test__scheduler_dispatch__several_runs__each_gets_its_own_run_id() -> None:
    container = FakeContainer()
    ep = SchedulerEntrypoint(jobs=[_job("j", _noop)])
    await ep.bind(_make_service_ctx(container))

    await ep._dispatch("j")
    await ep._dispatch("j")

    run_ids = [ctx["run_id"] for ctx in container.unit_contexts if ctx is not None]
    assert len(run_ids) == 2
    assert run_ids[0] != run_ids[1]


async def test__scheduler_dispatch__job_finished__closes_the_unit_scope() -> None:
    events: list[str] = []

    class _TrackingContainer(FakeContainer):
        @contextlib.asynccontextmanager
        async def unit_scope(self, context: Any = None) -> AsyncIterator[Any]:
            async with super().unit_scope(context) as scope:
                events.append("open")
                try:
                    yield scope
                finally:
                    events.append("close")

    container = _TrackingContainer()
    ep = SchedulerEntrypoint(jobs=[_job("j", _noop)])
    await ep.bind(_make_service_ctx(container))

    await ep._dispatch("j")
    # The per-job scope was opened and then closed before _dispatch returned.
    assert events == ["open", "close"]


async def test__scheduler_dispatch__job_succeeds__logs_start_and_completion(caplog: pytest.LogCaptureFixture) -> None:
    container = FakeContainer()
    ep = SchedulerEntrypoint(jobs=[_job("j", _noop)])
    await ep.bind(_make_service_ctx(container))

    with caplog.at_level("INFO", logger="servicewright.adapters.apscheduler4.entrypoint"):
        await ep._dispatch("j")

    messages = [r.message for r in caplog.records]
    assert "Job execution started" in messages
    assert "Job execution completed" in messages
    completed = next(r for r in caplog.records if r.message == "Job execution completed")
    assert getattr(completed, "job_id", None) == "j"
    assert isinstance(getattr(completed, "duration_seconds", None), float)


async def test__scheduler_dispatch__job_raises__is_logged_without_propagating(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = FakeContainer()

    async def boom(_scope: Any) -> None:
        raise ValueError("kaboom")

    ep = SchedulerEntrypoint(jobs=[ScheduledJob(id="bad", func=boom, trigger=_FakeTrigger())])
    await ep.bind(_make_service_ctx(container))

    with caplog.at_level("ERROR", logger="servicewright.adapters.apscheduler4.entrypoint"):
        # MUST NOT raise — a failed job cannot crash the scheduler loop.
        await ep._dispatch("bad")

    failed = [r for r in caplog.records if r.message == "Job execution failed"]
    assert len(failed) == 1
    assert getattr(failed[0], "job_id", None) == "bad"


async def test__scheduler_dispatch__job_cancelled__propagates_the_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container = FakeContainer()

    async def cancel_me(_scope: Any) -> None:
        raise asyncio.CancelledError

    ep = SchedulerEntrypoint(jobs=[ScheduledJob(id="c", func=cancel_me, trigger=_FakeTrigger())])
    await ep.bind(_make_service_ctx(container))

    with (
        caplog.at_level("WARNING", logger="servicewright.adapters.apscheduler4.entrypoint"),
        pytest.raises(asyncio.CancelledError),
    ):
        await ep._dispatch("c")

    assert any(r.message == "Job execution cancelled" for r in caplog.records)


async def test__scheduler_dispatch__unknown_job_id__does_nothing(caplog: pytest.LogCaptureFixture) -> None:
    container = FakeContainer()
    ep = SchedulerEntrypoint(jobs=[_job("known", _noop)])
    await ep.bind(_make_service_ctx(container))

    with caplog.at_level("ERROR", logger="servicewright.adapters.apscheduler4.entrypoint"):
        await ep._dispatch("unknown")

    assert container.unit_scopes_opened == 0
    assert any(r.message == "Scheduled job id not found in registry" for r in caplog.records)


# --------------------------------------------------------------------------- #
# drain() / stop()
# --------------------------------------------------------------------------- #
async def test__scheduler_drain__called__pauses_schedules_without_tearing_down(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    scheduler = patched_scheduler.instances[0]
    await ep.serve(stop=_set_event())

    await ep.drain(2.0)

    # Every schedule is paused so no NEW job fires, but drain must NOT signal the
    # hard ``stop()`` (which would cancel in-flight jobs) or tear the scheduler
    # down — that is reserved for stop().
    assert scheduler.all_paused() is True
    assert scheduler.stop_calls == 0
    assert scheduler.exited is False
    assert ep._scheduler is scheduler


async def test__scheduler_drain__job_in_flight__waits_for_it_within_the_grace(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    scheduler = patched_scheduler.instances[0]
    await ep.serve(stop=_set_event())

    # Put a job "in flight" and clear it shortly after drain starts polling.
    job = _FakeJob()
    scheduler._running_jobs.add(job)

    async def finish_job_soon() -> None:
        await asyncio.sleep(0.05)
        scheduler._running_jobs.discard(job)

    await asyncio.gather(ep.drain(2.0), finish_job_soon())

    # The job drained naturally — it was NOT hard-cancelled by drain.
    assert scheduler.cancelled_jobs == 0
    assert scheduler._running_jobs == set()


async def test__scheduler_drain__never_bound__does_nothing() -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    await ep.stop()  # tear the bound scheduler down
    await ep.drain(1.0)  # no live scheduler -> must not raise


async def test__scheduler_drain__already_stopped__does_nothing(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    scheduler = patched_scheduler.instances[0]
    await ep.serve(stop=_set_event())
    await ep.stop()
    await ep.drain(1.0)  # already stopped -> no schedules get paused / no raise.
    assert scheduler.all_paused() is False


async def test__scheduler_drain__never_started__does_nothing(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    scheduler = patched_scheduler.instances[0]
    # serve() was never called -> scheduler is bound but not started.
    await ep.drain(1.0)
    assert scheduler.all_paused() is False


async def test__scheduler_drain__grace_expires__logs_the_timeout(
    patched_scheduler: type[_FakeAsyncScheduler], caplog: pytest.LogCaptureFixture
) -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    scheduler = patched_scheduler.instances[0]
    await ep.serve(stop=_set_event())

    # A job stays in flight forever -> drain cannot finish within grace.
    scheduler._running_jobs.add(_FakeJob())

    with caplog.at_level("WARNING", logger="servicewright.adapters.apscheduler4.entrypoint"):
        await ep.drain(0.05)

    assert any(r.message == "Scheduler drain timed out with jobs still in flight" for r in caplog.records)


async def test__scheduler_stop__called__tears_the_scheduler_down(patched_scheduler: type[_FakeAsyncScheduler]) -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    scheduler = patched_scheduler.instances[0]
    await ep.serve(stop=_set_event())

    await ep.stop()

    assert scheduler.exited is True
    assert ep._scheduler is None
    assert ep._stopped is True


async def test__scheduler_stop__called_twice__tears_down_once(patched_scheduler: type[_FakeAsyncScheduler]) -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    scheduler = patched_scheduler.instances[0]
    await ep.serve(stop=_set_event())

    await ep.stop()
    await ep.stop()  # second call must be a no-op, not raise / re-tear-down.

    assert scheduler.exited is True


async def test__scheduler_stop__called_before_bind__does_nothing() -> None:
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.stop()  # no scheduler bound yet -> must not raise.
    assert ep._scheduler is None


# --------------------------------------------------------------------------- #
# REGRESSION: the real Host shutdown order must let in-flight jobs drain
# --------------------------------------------------------------------------- #
async def test__scheduler_entrypoint__host_shutdown__lets_the_in_flight_job_finish(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    """Drive the EXACT Host shutdown order and assert no zero-grace kill.

    Host order (see ``Host._serve`` then ``Host._shutdown_in_scope``):
    ``serve()`` runs to completion when ``stop`` is set, THEN ``drain(grace)``,
    THEN ``stop()``. The scheduler must still be alive for drain to act on, and
    drain must let an in-flight job finish within the grace window instead of
    hard-cancelling it.

    On the OLD code this FAILS: ``serve()``'s ``async with AsyncScheduler()``
    exited the moment ``stop`` was set (tearing the scheduler down and nulling
    ``_scheduler``), so ``drain`` was a no-op and the in-flight job was killed at
    zero grace. Here the fake's ``stop``/``__aexit__`` model that hard-cancel, so
    a regression would surface as ``cancelled_jobs == 1``.
    """
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    scheduler = patched_scheduler.instances[0]

    stop = asyncio.Event()
    job = _FakeJob()

    async def host_lifecycle() -> None:
        # Phase 1: serve() until stop is set (mirrors Host._serve).
        await ep.serve(stop=stop)
        # The scheduler MUST still be alive when drain begins.
        assert ep._scheduler is scheduler
        assert scheduler.exited is False
        # Phase 2: drain(grace), then Phase 3: stop() (mirrors _shutdown_in_scope).
        await ep.drain(grace=2.0)
        await ep.stop()

    async def trigger_shutdown_mid_flight() -> None:
        # Wait until the scheduler is serving, then put a job "in flight" and ask
        # the Host to shut down while that job is still running.
        while not scheduler.started:
            await asyncio.sleep(0)
        scheduler._running_jobs.add(job)
        stop.set()
        # The job keeps running for a moment, then finishes on its own.
        await asyncio.sleep(0.05)
        scheduler._running_jobs.discard(job)

    await asyncio.gather(host_lifecycle(), trigger_shutdown_mid_flight())

    # The in-flight job drained gracefully; it was NEVER hard-cancelled.
    assert scheduler.cancelled_jobs == 0
    # Schedules were paused (no new fires) and the scheduler was torn down last.
    assert scheduler.all_paused() is True
    assert scheduler.exited is True
    assert ep._stopped is True


async def test__scheduler_entrypoint__stop_without_drain__cancels_the_in_flight_job(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    """Pin the fake's semantics: ``stop`` (no drain) DOES cancel in-flight jobs.

    This guards the regression test above: if ``stop()`` is called while a job is
    in flight WITHOUT a preceding drain, the fake hard-cancels it (mirroring the
    real ``AsyncScheduler.stop()`` cancel-scope behaviour). This is exactly the
    failure mode the fix prevents.
    """
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    await ep.bind(_make_service_ctx(FakeContainer()))
    scheduler = patched_scheduler.instances[0]
    await ep.serve(stop=_set_event())

    scheduler._running_jobs.add(_FakeJob())
    await ep.stop()  # no drain first -> in-flight job is hard-cancelled.

    assert scheduler.cancelled_jobs == 1


# --------------------------------------------------------------------------- #
# SchedulerPlugin
# --------------------------------------------------------------------------- #
def test__scheduler_plugin__constructed__satisfies_the_plugin_protocol() -> None:
    plugin = SchedulerPlugin(jobs=[_job("a", _noop)])
    assert isinstance(plugin, Plugin)


def test__scheduler_plugin__constructed__exposes_its_entrypoint() -> None:
    plugin = SchedulerPlugin(jobs=[_job("a", _noop)], kind="cron", essential=False)
    ep = plugin.entrypoint
    assert isinstance(ep, SchedulerEntrypoint)
    assert ep.kind == "cron"
    assert ep.essential is False


def test__scheduler_plugin_on_register__called__adds_its_entrypoint_to_the_host() -> None:
    from unittest.mock import MagicMock

    plugin = SchedulerPlugin(jobs=[_job("a", _noop)])
    host = MagicMock()
    plugin.on_register(spec=MagicMock(), host=host)
    host.add_entrypoint.assert_called_once_with(plugin.entrypoint)


# --------------------------------------------------------------------------- #
# End-to-end-ish: drive a SchedulerEntrypoint through a real Host/Service
# --------------------------------------------------------------------------- #
async def test__scheduler_entrypoint__driven_by_a_service__completes_the_lifecycle(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    """Run a Service end-to-end with a real Host and a mocked scheduler."""
    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="sched-service", create_container=lambda _s: container)
    ep = SchedulerEntrypoint(jobs=[_job("a", _noop)])
    service = Service(spec, entrypoints=[ep])

    stop = asyncio.Event()

    async def run_then_stop() -> None:
        while not spec.health.ready:
            await asyncio.sleep(0)
        while not (patched_scheduler.instances and patched_scheduler.instances[0].started):
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(service.run(FakeSettings(), stop=stop), run_then_stop())

    scheduler = patched_scheduler.instances[0]
    assert scheduler.started is True
    assert scheduler.exited is True
    assert spec.health.ready is False
    assert container.app_scopes_opened == 1


async def test__scheduler_plugin__driven_by_a_service__completes_the_lifecycle(
    patched_scheduler: type[_FakeAsyncScheduler],
) -> None:
    """A SchedulerPlugin registers its entrypoint and the Host drives it."""
    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="sched-plugin-service", create_container=lambda _s: container)
    plugin = SchedulerPlugin(jobs=[_job("a", _noop)])
    service = Service(spec, plugins=[plugin])

    stop = asyncio.Event()

    async def run_then_stop() -> None:
        while not spec.health.ready:
            await asyncio.sleep(0)
        while not (patched_scheduler.instances and patched_scheduler.instances[0].started):
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(service.run(FakeSettings(), stop=stop), run_then_stop())

    assert patched_scheduler.instances[0].exited is True
    assert spec.health.ready is False
