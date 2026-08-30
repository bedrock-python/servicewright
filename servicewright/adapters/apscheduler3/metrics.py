"""Scheduled-job metrics: the recorder and its frozen wire names.

The scheduler adapter OWNS its metric vocabulary (the kernel is transport-neutral
and exposes only generic instruments). These names and label sets are a wire
contract with dashboards and alerts — do not rename.

A run ends in one of three *outcomes* — ``ok``, ``error`` or ``cancelled`` (the
Host drained or stopped the scheduler while the job was in flight). ``error_kind``
refines ``error`` with the library's own error taxonomy: the
:class:`~servicewright.core.errors.ErrorKind` of a
:class:`~servicewright.core.errors.ServiceError` (an expected, domain-level
failure — ``validation``, ``not_found``, ...) or ``unexpected`` for any other
exception. An error-rate alert can therefore tell a job refused by a business
rule from a lost database connection without the library knowing either.

Two details every hand-rolled version gets wrong, fixed here once:

- The last-success timestamp moves ONLY on ``ok`` — a job that fails every time
  goes stale, which is what a staleness alert exists to catch.
- The in-progress gauge is released on every exit path, error and cancellation
  included, so "job is stuck" cannot become permanently true.

The recorder composes backend-agnostic instruments minted from the app's
metrics sink, so it works identically over prometheus or any custom backend
(and records into no-ops when metrics are disabled).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Final, Literal

from ...core.errors import ServiceError
from ...core.observability.naming import make_metric_name

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ...core.observability.sinks import MetricsSinkProtocol

SCHEDULER_JOB_RUNS_TOTAL = "scheduler_job_runs_total"
SCHEDULER_JOB_RUNS_TOTAL_LABELS = ("job_id", "outcome", "error_kind")

SCHEDULER_JOB_DURATION_SECONDS = "scheduler_job_duration_seconds"
SCHEDULER_JOB_DURATION_LABELS = ("job_id",)

SCHEDULER_JOB_LAST_SUCCESS_TIMESTAMP_SECONDS = "scheduler_job_last_success_timestamp_seconds"
SCHEDULER_JOB_IN_PROGRESS = "scheduler_job_in_progress"
SCHEDULER_JOB_GAUGE_LABELS = ("job_id",)

OUTCOME_OK: Final = "ok"
OUTCOME_ERROR: Final = "error"
OUTCOME_CANCELLED: Final = "cancelled"
JobOutcome = Literal["ok", "error", "cancelled"]

# ``error_kind`` for runs that did not fail, and for failures outside the
# library's error taxonomy (anything that is not a ServiceError).
ERROR_KIND_NONE: Final = ""
ERROR_KIND_UNEXPECTED: Final = "unexpected"

# Jobs span orders of magnitude — a reconciliation sweep finishes well under a
# second, a nightly pass runs for minutes — so the ladder reaches far past the
# request-scale buckets of the RPC recorders.
DEFAULT_SCHEDULER_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)


def classify_outcome(error: BaseException | None) -> tuple[JobOutcome, str]:
    """Map how a run ended onto its ``(outcome, error_kind)`` label values.

    ``None`` is a successful run; a cancellation is the Host draining or
    stopping the scheduler mid-run; a :class:`ServiceError` carries its
    ``ErrorKind``; every other exception is ``unexpected``.
    """
    if error is None:
        return OUTCOME_OK, ERROR_KIND_NONE
    if isinstance(error, asyncio.CancelledError):
        return OUTCOME_CANCELLED, ERROR_KIND_NONE
    if isinstance(error, ServiceError):
        return OUTCOME_ERROR, str(error.kind)
    return OUTCOME_ERROR, ERROR_KIND_UNEXPECTED


class SchedulerJobMetricsRecorder:
    """The scheduled-job run recorder over generic instruments.

    Args:
        sink: The app's metrics sink (``ctx.observability.metrics``).
        prefix: Optional metric name prefix (``myapp`` → ``myapp_scheduler_job_runs_total``).
        buckets: Duration histogram buckets, in seconds.
    """

    def __init__(
        self,
        sink: MetricsSinkProtocol,
        *,
        prefix: str | None = None,
        buckets: tuple[float, ...] = DEFAULT_SCHEDULER_BUCKETS,
    ) -> None:
        self._runs_total = sink.counter(
            make_metric_name(SCHEDULER_JOB_RUNS_TOTAL, prefix),
            "Total number of scheduled job runs",
            SCHEDULER_JOB_RUNS_TOTAL_LABELS,
        )
        self._duration = sink.histogram(
            make_metric_name(SCHEDULER_JOB_DURATION_SECONDS, prefix),
            "Scheduled job run duration in seconds",
            SCHEDULER_JOB_DURATION_LABELS,
            buckets=buckets,
        )
        self._last_success = sink.gauge(
            make_metric_name(SCHEDULER_JOB_LAST_SUCCESS_TIMESTAMP_SECONDS, prefix),
            "Unix timestamp of the last successful run of the job",
            SCHEDULER_JOB_GAUGE_LABELS,
        )
        self._in_progress = sink.gauge(
            make_metric_name(SCHEDULER_JOB_IN_PROGRESS, prefix),
            "Number of runs of the job currently in progress",
            SCHEDULER_JOB_GAUGE_LABELS,
        )

    def mark_started(self, job_id: str) -> None:
        """Count one more run of ``job_id`` as in progress."""
        self._in_progress.inc(job_id=job_id)

    def mark_finished(self, job_id: str) -> None:
        """Count one run of ``job_id`` as no longer in progress (every exit path)."""
        self._in_progress.dec(job_id=job_id)

    def record_run(self, job_id: str, *, duration: float, error: BaseException | None = None) -> None:
        """Record one finished run: its outcome, its duration and — on success only — the time.

        A cancelled run has no duration worth a histogram sample (it did not
        finish), so only ``ok`` and ``error`` runs are observed.
        """
        outcome, error_kind = classify_outcome(error)
        self._runs_total.inc(job_id=job_id, outcome=outcome, error_kind=error_kind)
        if outcome != OUTCOME_CANCELLED:
            self._duration.observe(duration, job_id=job_id)
        if outcome == OUTCOME_OK:
            self._last_success.set(time.time(), job_id=job_id)

    @contextlib.contextmanager
    def track(self, job_id: str) -> Iterator[None]:
        """Account one run of ``job_id`` around its body.

        In-progress goes up on entry; on exit — normal, error or cancellation —
        it goes down and the run is recorded from the exception, which is then
        re-raised untouched.
        """
        self.mark_started(job_id)
        start = time.perf_counter()
        error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            error = exc
            raise
        finally:
            self.mark_finished(job_id)
            self.record_run(job_id, duration=time.perf_counter() - start, error=error)


__all__ = [
    "DEFAULT_SCHEDULER_BUCKETS",
    "ERROR_KIND_NONE",
    "ERROR_KIND_UNEXPECTED",
    "OUTCOME_CANCELLED",
    "OUTCOME_ERROR",
    "OUTCOME_OK",
    "SCHEDULER_JOB_DURATION_LABELS",
    "SCHEDULER_JOB_DURATION_SECONDS",
    "SCHEDULER_JOB_GAUGE_LABELS",
    "SCHEDULER_JOB_IN_PROGRESS",
    "SCHEDULER_JOB_LAST_SUCCESS_TIMESTAMP_SECONDS",
    "SCHEDULER_JOB_RUNS_TOTAL",
    "SCHEDULER_JOB_RUNS_TOTAL_LABELS",
    "JobOutcome",
    "SchedulerJobMetricsRecorder",
    "classify_outcome",
]
