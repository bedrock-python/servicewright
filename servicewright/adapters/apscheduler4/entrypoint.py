"""``SchedulerEntrypoint``: APScheduler folded onto the Entrypoint contract.

This is a faithful fold of the apscheduler-runtime prototype's
``run_async_scheduled_worker`` + ``_execute_job`` onto the Host + Entrypoints
model:

- The :class:`AppSpec` stays transport-neutral; the entrypoint owns its own
  :class:`ScheduledJob` list.
- The HOST owns OS signals and the run-loop. ``bind`` enters the
  :class:`AsyncScheduler` and registers the schedules, ``serve`` starts it and
  waits on the host's ``stop`` event, ``drain``/``stop`` perform graceful /
  hard shutdown. The prototype's ``_register_shutdown_handlers`` (its own signal
  install) is intentionally NOT used.
- **Per-job ``UnitScope`` is the key upgrade.** Each fire opens
  ``container.unit_scope(context={"job_id": ..., "run_id": ...})`` via the
  sanctioned :meth:`ScopedEntrypoint.unit_scope`, so "a scheduled job == an HTTP
  request" is structurally true (a real DI scope per run, not just structlog
  contextvars). The schedule target is :meth:`_dispatch`, which takes only the
  job id (not a closure) so the schedule stays APScheduler-serializable.

Scheduler lifetime vs. the Host shutdown order
----------------------------------------------
The Host shuts an entrypoint down in three ordered phases on a *live* object:
``serve()`` returns when ``stop`` is set, THEN ``drain(grace)`` runs, THEN
``stop()``. The scheduler MUST therefore outlive ``serve()`` so ``drain`` has a
running scheduler to act on. To guarantee that:

- :meth:`bind` enters the :class:`AsyncScheduler` on a held
  :class:`contextlib.AsyncExitStack` and registers the schedules; the scheduler
  reference stays non-``None`` across ``serve()``.
- :meth:`serve` only starts the scheduler in the background and awaits ``stop``;
  it never closes or nulls the scheduler.
- :meth:`drain` pauses every schedule so NO new jobs fire, then waits for the
  in-flight job set to empty within ``grace`` (best-effort: it logs a warning on
  timeout instead of raising). It must NOT signal ``AsyncScheduler.stop()`` — in
  APScheduler 4.0.0a6 ``stop()`` cancels the scheduler's cancel scope, which
  hard-cancels in-flight jobs with zero grace (and ``wait_until_stopped()``
  returns immediately at the ``stopping`` state without awaiting them).
- :meth:`stop` performs the final teardown by closing the held exit stack
  (idempotent), which is the only place the scheduler is actually shut down.

The prototype's ``AsyncBackgroundTask`` is **out of scope** here: a long-running
background coroutine is already covered by the zero-dep
:class:`~servicewright.adapters.builtin.daemon.DaemonEntrypoint`. Compose a
``DaemonEntrypoint`` alongside a ``SchedulerEntrypoint`` in the same Service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ...core.contracts import ScopedEntrypoint
from ._imports import AsyncScheduler, RunState
from .config import ScheduledJob
from .exceptions import DuplicateScheduleError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ...core.spec import ServiceContext

logger = logging.getLogger(__name__)

# How often :meth:`SchedulerEntrypoint.drain` re-checks the in-flight job set
# while waiting for running jobs to finish within the grace window.
_DRAIN_POLL_INTERVAL_SECONDS = 0.05


class SchedulerEntrypoint(ScopedEntrypoint):
    """An APScheduler-driven scheduler entrypoint driven by the :class:`Host`.

    Args:
        jobs: The scheduled jobs to register. Each fires inside a fresh per-job
            :class:`~servicewright.core.contracts.UnitScopeProtocol`.
        kind: Telemetry label (default ``"scheduler"``).
        essential: Whether the entrypoint's exit/failure stops the process.
    """

    def __init__(
        self,
        *,
        jobs: Sequence[ScheduledJob],
        kind: str = "scheduler",
        essential: bool = True,
    ) -> None:
        super().__init__()
        self._jobs: list[ScheduledJob] = list(jobs)
        self.kind = kind
        self.essential = essential

        self._registry: dict[str, ScheduledJob] = {}
        self._scheduler: AsyncScheduler | None = None
        # Holds the entered ``AsyncScheduler`` so its lifetime survives serve();
        # closing it (only in stop()) is what actually tears the scheduler down.
        self._stack: contextlib.AsyncExitStack | None = None
        self._stopped = False

    @property
    def jobs(self) -> list[ScheduledJob]:
        """The configured scheduled jobs."""
        return self._jobs

    async def bind(self, ctx: ServiceContext[Any, Any]) -> None:
        """Capture the container, enter the scheduler, and register schedules.

        The :class:`AsyncScheduler` is entered here (not in :meth:`serve`) so it
        outlives ``serve()`` and is still alive when the Host calls
        :meth:`drain` / :meth:`stop`. The scheduler is held open on
        :attr:`_stack`; it is torn down only in :meth:`stop`.

        Raises:
            DuplicateScheduleError: If two jobs share the same ``id``.
        """
        await super().bind(ctx)
        self._registry = self._build_registry(self._jobs)

        stack = contextlib.AsyncExitStack()
        try:
            scheduler = await stack.enter_async_context(AsyncScheduler())
            for job in self._registry.values():
                await self._register_schedule(scheduler, job)
        except BaseException:
            await stack.aclose()
            raise
        self._scheduler = scheduler
        self._stack = stack

        logger.info(
            "Scheduler entrypoint bound",
            extra={"service": ctx.service_name, "jobs_count": len(self._registry)},
        )

    async def serve(self, *, stop: asyncio.Event) -> None:
        """Start the scheduler and run until the host's ``stop`` event is set.

        This only starts the (already-entered) scheduler in the background and
        waits on ``stop``. It deliberately does NOT close or null the scheduler:
        the Host calls :meth:`drain` / :meth:`stop` AFTER ``serve()`` returns and
        needs a live scheduler to drain.
        """
        scheduler = self._scheduler
        if scheduler is None:  # pragma: no cover - serve() always follows bind()
            raise RuntimeError("serve() called before bind(); scheduler is not initialized")
        await scheduler.start_in_background()
        logger.info("Scheduler started", extra={"jobs_count": len(self._registry)})
        await stop.wait()

    async def drain(self, grace: float) -> None:
        """Pause every schedule, then let in-flight jobs finish within ``grace``.

        No NEW jobs fire once the schedules are paused, but jobs already running
        keep going until they finish or ``grace`` elapses. This intentionally
        does NOT call ``AsyncScheduler.stop()``: in APScheduler 4.0.0a6 ``stop()``
        cancels the scheduler's cancel scope and hard-cancels in-flight jobs with
        zero grace. Teardown happens later, in :meth:`stop`.
        """
        scheduler = self._scheduler
        if scheduler is None or self._stopped or scheduler.state is not RunState.started:
            return

        await self._pause_all_schedules(scheduler)
        if await self._wait_for_running_jobs(scheduler, grace):
            return
        logger.warning(
            "Scheduler drain timed out with jobs still in flight",
            extra={"grace": grace, "running_jobs": len(scheduler._running_jobs)},
        )

    async def stop(self) -> None:
        """Tear the scheduler down (idempotent; safe before bind / after stop).

        This is the only place the scheduler is shut down: closing the held exit
        stack runs ``AsyncScheduler.__aexit__``, which cancels the scheduler's
        cancel scope and releases its services task group.
        """
        if self._stopped:
            return
        self._stopped = True
        stack = self._stack
        self._stack = None
        self._scheduler = None
        if stack is not None:
            await stack.aclose()

    @staticmethod
    async def _pause_all_schedules(scheduler: AsyncScheduler) -> None:
        for schedule in await scheduler.get_schedules():
            await scheduler.pause_schedule(schedule.id)

    @staticmethod
    async def _wait_for_running_jobs(scheduler: AsyncScheduler, grace: float) -> bool:
        """Poll the in-flight job set until empty or ``grace`` elapses.

        Returns ``True`` if every running job finished within ``grace``.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        while scheduler._running_jobs:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_DRAIN_POLL_INTERVAL_SECONDS)
        return True

    async def _register_schedule(self, scheduler: AsyncScheduler, job: ScheduledJob) -> None:
        # Register a DISTINCT task per job, keyed by the job id, all backed by
        # the shared :meth:`_dispatch` target. In APScheduler v4 every schedule
        # on the same callable would otherwise collapse onto a single task, so
        # per-job concurrency (``max_running_jobs``) could not differ per job.
        task_options: dict[str, Any] = {"func": self._dispatch}
        if job.max_instances is not None:
            task_options["max_running_jobs"] = job.max_instances
        await scheduler.configure_task(job.id, **task_options)

        options: dict[str, Any] = {"id": job.id, "args": (job.id,)}
        if job.misfire_grace_time is not None:
            options["misfire_grace_time"] = job.misfire_grace_time
        if job.coalesce is not None:
            options["coalesce"] = job.coalesce

        # Schedule by the job-id task (a string), not a closure, so the schedule
        # stays APScheduler-serializable.
        await scheduler.add_schedule(job.id, job.trigger, **options)

    async def _dispatch(self, job_id: str) -> None:
        """Run one scheduled job inside a fresh per-job unit scope.

        This is the schedule target. It looks the job up in the registry and
        opens ``unit_scope(context={"job_id", "run_id"})`` around the call,
        carrying the prototype's structured logging + duration tracking.
        """
        job = self._registry.get(job_id)
        if job is None:  # pragma: no cover - registry is authoritative for added schedules
            logger.error("Scheduled job id not found in registry", extra={"job_id": job_id})
            return

        run_id = str(uuid4())
        log_ctx = {"job_id": job_id, "run_id": run_id}
        start = time.perf_counter()
        logger.info("Job execution started", extra=log_ctx)
        try:
            async with self.unit_scope(context={"job_id": job_id, "run_id": run_id}) as scope:
                await job.func(scope, *job.args, **job.kwargs)
        except asyncio.CancelledError:
            logger.warning(
                "Job execution cancelled",
                extra={**log_ctx, "duration_seconds": round(time.perf_counter() - start, 4)},
            )
            raise
        except Exception:
            # A failed job is logged but MUST NOT crash the scheduler loop.
            logger.exception(
                "Job execution failed",
                extra={**log_ctx, "duration_seconds": round(time.perf_counter() - start, 4)},
            )
            return
        logger.info(
            "Job execution completed",
            extra={**log_ctx, "duration_seconds": round(time.perf_counter() - start, 4)},
        )

    @staticmethod
    def _build_registry(jobs: Sequence[ScheduledJob]) -> dict[str, ScheduledJob]:
        ids = [job.id for job in jobs]
        if len(ids) != len(set(ids)):
            duplicates = sorted({id_ for id_ in ids if ids.count(id_) > 1})
            raise DuplicateScheduleError(duplicates)
        return {job.id: job for job in jobs}


class SchedulerPlugin:
    """Declarative wiring: register a :class:`SchedulerEntrypoint` on the host.

    Pass the same arguments as :class:`SchedulerEntrypoint`; ``on_register``
    builds it and adds it to the host.
    """

    def __init__(
        self,
        *,
        jobs: Sequence[ScheduledJob],
        kind: str = "scheduler",
        essential: bool = True,
    ) -> None:
        self._entrypoint = SchedulerEntrypoint(jobs=jobs, kind=kind, essential=essential)

    @property
    def entrypoint(self) -> SchedulerEntrypoint:
        """The entrypoint that will be registered on the host."""
        return self._entrypoint

    def on_register(self, spec: Any, host: Any) -> None:
        """Append the scheduler entrypoint to the host."""
        host.add_entrypoint(self._entrypoint)


__all__ = [
    "SchedulerEntrypoint",
    "SchedulerPlugin",
]
