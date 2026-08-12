"""Behavioural tests for the APScheduler 3.x scheduler entrypoint.

APScheduler 3.x and 4.x are the same distribution with incompatible majors, so
they can never be installed together. These tests therefore run only in the
``[apscheduler3]`` environment (a dedicated CI job) and skip everywhere else;
``tests/unit/test_scheduler.py`` is their v4 mirror and skips here. The
APScheduler SDK itself is mocked, so nothing schedules or sleeps for real.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import TYPE_CHECKING, Any

import pytest

from servicewright.core.spec import BootstrapContext, ServiceContext
from servicewright.testing import FakeContainer, FakeScope, FakeSettings

if TYPE_CHECKING:
    from collections.abc import Callable


def _apscheduler3_installed() -> bool:
    """APScheduler 3.x is the major that ships ``schedulers.asyncio``.

    The distribution metadata is unreliable here (the v4 pre-releases report no
    version at all), so the majors are told apart the same way the adapters'
    import guards do: by what is importable.
    """
    try:
        return importlib.util.find_spec("apscheduler.schedulers.asyncio") is not None
    except ModuleNotFoundError:
        # v4 has no ``apscheduler.schedulers`` package at all, so resolving the
        # child spec raises rather than returning None.
        return False


APSCHEDULER3_INSTALLED = _apscheduler3_installed()

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not APSCHEDULER3_INSTALLED, reason="requires the [apscheduler3] environment"),
]

if APSCHEDULER3_INSTALLED:  # pragma: no branch - import guard for the v4 environment
    from servicewright.adapters.apscheduler3 import (
        DuplicateScheduleError,
        ScheduledJob,
        SchedulerEntrypoint,
        SchedulerPlugin,
    )
    from servicewright.adapters.apscheduler3 import entrypoint as entrypoint_mod


class _FakeAsyncIOScheduler:
    """Stand-in for APScheduler 3's ``AsyncIOScheduler``."""

    instances: list[_FakeAsyncIOScheduler] = []

    def __init__(self) -> None:
        self.running = False
        self.paused = False
        self.jobs: list[dict[str, Any]] = []
        self.shutdown_calls: list[bool] = []
        _FakeAsyncIOScheduler.instances.append(self)

    def add_job(self, func: Callable[..., Any], trigger: Any, **options: Any) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **options})

    def start(self) -> None:
        self.running = True

    def pause(self) -> None:
        self.paused = True

    def shutdown(self, wait: bool = True) -> None:
        self.shutdown_calls.append(wait)
        self.running = False


@pytest.fixture
def patched_scheduler(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAsyncIOScheduler]:
    """Replace the SDK scheduler with the recording fake."""
    _FakeAsyncIOScheduler.instances.clear()
    monkeypatch.setattr(entrypoint_mod, "AsyncIOScheduler", _FakeAsyncIOScheduler)
    return _FakeAsyncIOScheduler


@pytest.fixture
def service_ctx() -> ServiceContext:
    return ServiceContext(
        bootstrap=BootstrapContext(
            settings=FakeSettings(),
            service_name="svc",
            container=FakeContainer(),
            lifecycle=object(),  # type: ignore[arg-type]
        ),
        app_scope=FakeScope(),
        health=None,  # type: ignore[arg-type]
    )


def _job(job_id: str = "reconcile", func: Any = None) -> ScheduledJob:
    async def noop(scope: Any) -> None:
        return None

    return ScheduledJob(id=job_id, func=func or noop, trigger=object())


async def test__scheduler_entrypoint_bind__jobs_configured__registers_each_of_them(
    patched_scheduler: type[_FakeAsyncIOScheduler],
    service_ctx: ServiceContext,
) -> None:
    # Arrange
    ep = SchedulerEntrypoint(jobs=[_job("a"), _job("b")])

    # Act
    await ep.bind(service_ctx)

    # Assert
    assert [job["id"] for job in patched_scheduler.instances[-1].jobs] == ["a", "b"]


async def test__scheduler_entrypoint_bind__duplicate_job_ids__raises(
    patched_scheduler: type[_FakeAsyncIOScheduler],
    service_ctx: ServiceContext,
) -> None:
    # Arrange
    ep = SchedulerEntrypoint(jobs=[_job("same"), _job("same")])

    # Act & Assert
    with pytest.raises(DuplicateScheduleError):
        await ep.bind(service_ctx)


async def test__scheduler_entrypoint_serve__stop_set__starts_then_returns(
    patched_scheduler: type[_FakeAsyncIOScheduler],
    service_ctx: ServiceContext,
) -> None:
    # Arrange
    ep = SchedulerEntrypoint(jobs=[_job()])
    await ep.bind(service_ctx)
    stop = asyncio.Event()
    stop.set()

    # Act
    await ep.serve(stop=stop)

    # Assert
    assert patched_scheduler.instances[-1].running is True


async def test__scheduler_entrypoint_drain__called__pauses_new_job_runs(
    patched_scheduler: type[_FakeAsyncIOScheduler],
    service_ctx: ServiceContext,
) -> None:
    # Arrange
    ep = SchedulerEntrypoint(jobs=[_job()])
    await ep.bind(service_ctx)
    stop = asyncio.Event()
    stop.set()
    await ep.serve(stop=stop)

    # Act
    await ep.drain(1.0)

    # Assert
    assert patched_scheduler.instances[-1].paused is True


async def test__scheduler_entrypoint_drain__job_in_flight__waits_for_it_to_finish(
    patched_scheduler: type[_FakeAsyncIOScheduler],
    service_ctx: ServiceContext,
) -> None:
    # Arrange
    finished = False

    async def slow_job(scope: Any) -> None:
        nonlocal finished
        await asyncio.sleep(0.2)
        finished = True

    ep = SchedulerEntrypoint(jobs=[_job("slow", slow_job)])
    await ep.bind(service_ctx)
    stop = asyncio.Event()
    stop.set()
    await ep.serve(stop=stop)
    running = asyncio.ensure_future(ep._dispatch("slow"))
    await asyncio.sleep(0)

    # Act
    await ep.drain(5.0)

    # Assert
    assert finished is True
    await running


async def test__scheduler_entrypoint_drain__grace_expires__returns_without_hanging(
    patched_scheduler: type[_FakeAsyncIOScheduler],
    service_ctx: ServiceContext,
) -> None:
    # Arrange
    async def endless_job(scope: Any) -> None:
        await asyncio.sleep(30)

    ep = SchedulerEntrypoint(jobs=[_job("endless", endless_job)])
    await ep.bind(service_ctx)
    stop = asyncio.Event()
    stop.set()
    await ep.serve(stop=stop)
    running = asyncio.ensure_future(ep._dispatch("endless"))
    await asyncio.sleep(0)

    # Act
    await asyncio.wait_for(ep.drain(0.1), timeout=5)

    # Assert
    running.cancel()
    assert patched_scheduler.instances[-1].paused is True


async def test__scheduler_entrypoint_stop__called_twice__is_idempotent(
    patched_scheduler: type[_FakeAsyncIOScheduler],
    service_ctx: ServiceContext,
) -> None:
    # Arrange
    ep = SchedulerEntrypoint(jobs=[_job()])
    await ep.bind(service_ctx)
    stop = asyncio.Event()
    stop.set()
    await ep.serve(stop=stop)

    # Act
    await ep.stop()
    await ep.stop()

    # Assert
    assert patched_scheduler.instances[-1].shutdown_calls == [False]


async def test__scheduler_entrypoint_dispatch__job_runs__opens_one_unit_scope(
    patched_scheduler: type[_FakeAsyncIOScheduler],
    service_ctx: ServiceContext,
) -> None:
    # Arrange
    container = service_ctx.container
    ep = SchedulerEntrypoint(jobs=[_job("scoped")])
    await ep.bind(service_ctx)

    # Act
    await ep._dispatch("scoped")

    # Assert
    assert container.unit_scopes_opened == 1


async def test__scheduler_entrypoint_dispatch__job_raises__does_not_propagate(
    patched_scheduler: type[_FakeAsyncIOScheduler],
    service_ctx: ServiceContext,
) -> None:
    # Arrange
    async def failing_job(scope: Any) -> None:
        raise RuntimeError("job boom")

    ep = SchedulerEntrypoint(jobs=[_job("failing", failing_job)])
    await ep.bind(service_ctx)

    # Act
    await ep._dispatch("failing")

    # Assert
    assert ep._running_jobs == set()


async def test__scheduler_plugin__registered__adds_its_entrypoint_to_the_host() -> None:
    # Arrange
    added: list[Any] = []

    class _Host:
        def add_entrypoint(self, entrypoint: Any) -> None:
            added.append(entrypoint)

    plugin = SchedulerPlugin(jobs=[_job()])

    # Act
    plugin.on_register(object(), _Host())

    # Assert
    assert added == [plugin.entrypoint]
