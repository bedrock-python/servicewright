"""``SchedulerEntrypoint``: APScheduler 3.x folded onto the Entrypoint contract.

A parallel of the ``apscheduler4`` adapter with an IDENTICAL public surface
(:class:`SchedulerEntrypoint` / :class:`SchedulerPlugin` / ``ScheduledJob``) but
implemented against APScheduler 3.x (``AsyncIOScheduler`` / ``add_job`` /
``shutdown``), which is API-incompatible with v4. A service migrates 3->4 by
changing one import line and one extra.

Lifecycle (driven by the Host, which owns OS signals and the run-loop):

- :meth:`bind` builds the :class:`AsyncIOScheduler` and registers the jobs (it is
  not started yet), and captures the container for per-job DI scopes.
- :meth:`serve` starts the scheduler onto the running event loop and waits on the
  host's ``stop`` event.
- :meth:`drain` pauses job processing so NO new jobs fire; jobs already running
  keep going.
- :meth:`stop` shuts the scheduler down (idempotent).

Each fire opens ``container.unit_scope(context={"job_id", "run_id"})`` so "a
scheduled job == an HTTP request" is structurally true.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ...core.contracts import ScopedEntrypoint
from ._imports import AsyncIOScheduler
from .config import ScheduledJob
from .exceptions import DuplicateScheduleError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ...core.spec import ServiceContext

logger = logging.getLogger(__name__)

# How often drain() re-checks the in-flight run set (mirrors the v4 adapter).
_DRAIN_POLL_INTERVAL_SECONDS = 0.05


class SchedulerEntrypoint(ScopedEntrypoint):
    """An APScheduler 3.x-driven scheduler entrypoint driven by the :class:`Host`.

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
        self._scheduler: AsyncIOScheduler | None = None
        self._running_jobs: set[str] = set()
        self._stopped = False

    @property
    def jobs(self) -> list[ScheduledJob]:
        """The configured scheduled jobs."""
        return self._jobs

    async def bind(self, ctx: ServiceContext[Any, Any]) -> None:
        """Capture the container, build the scheduler, and register jobs.

        The scheduler is created and the jobs are added here, but it is not
        started until :meth:`serve`.

        Raises:
            DuplicateScheduleError: If two jobs share the same ``id``.
        """
        await super().bind(ctx)
        self._registry = self._build_registry(self._jobs)

        scheduler = AsyncIOScheduler()
        for job in self._registry.values():
            self._register_job(scheduler, job)
        self._scheduler = scheduler

        logger.info(
            "Scheduler entrypoint bound",
            extra={"service": ctx.service_name, "jobs_count": len(self._registry)},
        )

    async def serve(self, *, stop: asyncio.Event) -> None:
        """Start the scheduler and run until the host's ``stop`` event is set."""
        scheduler = self._scheduler
        if scheduler is None:  # pragma: no cover - serve() always follows bind()
            raise RuntimeError("serve() called before bind(); scheduler is not initialized")
        scheduler.start()
        logger.info("Scheduler started", extra={"jobs_count": len(self._registry)})
        await stop.wait()

    async def drain(self, grace: float) -> None:
        """Pause new job runs and let in-flight ones finish within ``grace``.

        APScheduler 3.x exposes no in-flight-job set of its own — and its
        ``AsyncIOExecutor.shutdown`` cancels every pending future regardless of
        ``wait`` — so this adapter tracks its own runs in :meth:`_dispatch`.
        Without that, ``stop()`` would cancel a running job mid-transaction and
        the whole grace window would be silently discarded, which is exactly the
        behaviour the v4 sibling avoids.
        """
        scheduler = self._scheduler
        if scheduler is None or self._stopped or not scheduler.running:
            return
        scheduler.pause()
        if not await self._wait_for_running_jobs(grace):
            logger.warning(
                "Scheduler drain timed out with jobs still running",
                extra={"grace": grace, "running_jobs": len(self._running_jobs)},
            )

    async def _wait_for_running_jobs(self, grace: float) -> bool:
        """Poll the in-flight run set until empty or ``grace`` elapses."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        while self._running_jobs:
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_DRAIN_POLL_INTERVAL_SECONDS)
        return True

    async def stop(self) -> None:
        """Shut the scheduler down (idempotent; safe before bind / after stop)."""
        if self._stopped:
            return
        self._stopped = True
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)

    def _register_job(self, scheduler: AsyncIOScheduler, job: ScheduledJob) -> None:
        options: dict[str, Any] = {"id": job.id, "args": (job.id,)}
        if job.max_instances is not None:
            options["max_instances"] = job.max_instances
        if job.misfire_grace_time is not None:
            options["misfire_grace_time"] = job.misfire_grace_time
        if job.coalesce is not None:
            options["coalesce"] = job.coalesce
        scheduler.add_job(self._dispatch, job.trigger, **options)

    async def _dispatch(self, job_id: str) -> None:
        """Run one scheduled job inside a fresh per-job unit scope."""
        job = self._registry.get(job_id)
        if job is None:  # pragma: no cover - registry is authoritative for added jobs
            logger.error("Scheduled job id not found in registry", extra={"job_id": job_id})
            return

        run_id = str(uuid4())
        log_ctx = {"job_id": job_id, "run_id": run_id}
        start = time.perf_counter()
        logger.info("Job execution started", extra=log_ctx)
        self._running_jobs.add(run_id)
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
            logger.exception(
                "Job execution failed",
                extra={**log_ctx, "duration_seconds": round(time.perf_counter() - start, 4)},
            )
            return
        finally:
            self._running_jobs.discard(run_id)
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
    """Declarative wiring: register a :class:`SchedulerEntrypoint` on the host."""

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
