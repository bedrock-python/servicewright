"""``SchedulerJobMetricsRecorder``: the scheduled-job run accounting, over a real registry.

Regression cover for issue #25: the scheduler entrypoint recorded nothing, and
every consumer who filled the gap hit the same two traps — a last-success
timestamp stamped on every run (so a job failing every time looks fresh) and an
in-progress gauge leaked on the error path. Both are pinned here against the
Prometheus backend, so the samples are the ones a scrape would see.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from prometheus_client import CollectorRegistry

from servicewright.adapters.apscheduler4 import SchedulerJobMetricsRecorder
from servicewright.adapters.apscheduler4.metrics import (
    DEFAULT_SCHEDULER_BUCKETS,
    ERROR_KIND_NONE,
    ERROR_KIND_UNEXPECTED,
    OUTCOME_CANCELLED,
    OUTCOME_ERROR,
    OUTCOME_OK,
    classify_outcome,
)
from servicewright.adapters.observability import PrometheusMetricsSink
from servicewright.core.errors import ErrorKind, ServiceError
from servicewright.core.observability import NullMetricsSink

pytestmark = pytest.mark.unit


class _Registry:
    """A fresh Prometheus registry with sample lookups in the recorder's vocabulary."""

    def __init__(self, prefix: str = "") -> None:
        self.registry = CollectorRegistry()
        self.sink = PrometheusMetricsSink(registry=self.registry)
        self._prefix = prefix

    def runs(self, job_id: str, outcome: str, error_kind: str) -> float | None:
        return self.registry.get_sample_value(
            f"{self._prefix}scheduler_job_runs_total",
            {"job_id": job_id, "outcome": outcome, "error_kind": error_kind},
        )

    def duration_count(self, job_id: str) -> float | None:
        return self.registry.get_sample_value(f"{self._prefix}scheduler_job_duration_seconds_count", {"job_id": job_id})

    def last_success(self, job_id: str) -> float | None:
        return self.registry.get_sample_value(
            f"{self._prefix}scheduler_job_last_success_timestamp_seconds", {"job_id": job_id}
        )

    def in_progress(self, job_id: str) -> float | None:
        return self.registry.get_sample_value(f"{self._prefix}scheduler_job_in_progress", {"job_id": job_id})


# --------------------------------------------------------------------------- #
# Outcome vocabulary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(None, (OUTCOME_OK, ERROR_KIND_NONE), id="success"),
        pytest.param(asyncio.CancelledError(), (OUTCOME_CANCELLED, ERROR_KIND_NONE), id="cancelled"),
        pytest.param(RuntimeError("db down"), (OUTCOME_ERROR, ERROR_KIND_UNEXPECTED), id="unexpected"),
        pytest.param(ServiceError(kind=ErrorKind.NOT_FOUND), (OUTCOME_ERROR, "not_found"), id="service-error-kind"),
        pytest.param(ServiceError(), (OUTCOME_ERROR, "internal"), id="service-error-default-kind"),
    ],
)
def test__classify_outcome__how_the_run_ended__maps_to_the_label_pair(
    error: BaseException | None, expected: tuple[str, str]
) -> None:
    assert classify_outcome(error) == expected


# --------------------------------------------------------------------------- #
# The two traps
# --------------------------------------------------------------------------- #
def test__recorder__job_fails_every_time__last_success_never_moves() -> None:
    reg = _Registry()
    recorder = SchedulerJobMetricsRecorder(reg.sink)
    recorder.record_run("mailout", duration=1.0)
    stamped = reg.last_success("mailout")
    assert stamped is not None

    for _ in range(3):
        recorder.record_run("mailout", duration=0.2, error=RuntimeError("smtp down"))

    # The staleness alert can fire: a failing job does not look fresh.
    assert reg.last_success("mailout") == stamped
    assert reg.runs("mailout", OUTCOME_ERROR, ERROR_KIND_UNEXPECTED) == 3.0
    assert reg.runs("mailout", OUTCOME_OK, ERROR_KIND_NONE) == 1.0


def test__recorder__job_raises_inside_track__in_progress_is_released_and_the_error_reraised() -> None:
    reg = _Registry()
    recorder = SchedulerJobMetricsRecorder(reg.sink)

    with pytest.raises(RuntimeError, match="boom"), recorder.track("sweep"):
        assert reg.in_progress("sweep") == 1.0
        raise RuntimeError("boom")

    assert reg.in_progress("sweep") == 0.0
    assert reg.runs("sweep", OUTCOME_ERROR, ERROR_KIND_UNEXPECTED) == 1.0
    assert reg.last_success("sweep") is None


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #
def test__recorder__successful_run__counts_observes_and_stamps_now() -> None:
    reg = _Registry()
    recorder = SchedulerJobMetricsRecorder(reg.sink)
    before = time.time()

    with recorder.track("report"):
        pass

    assert reg.runs("report", OUTCOME_OK, ERROR_KIND_NONE) == 1.0
    assert reg.duration_count("report") == 1.0
    assert reg.in_progress("report") == 0.0
    stamped = reg.last_success("report")
    assert stamped is not None and before <= stamped <= time.time()


def test__recorder__run_cancelled__counted_as_cancelled_not_timed_and_reraised() -> None:
    reg = _Registry()
    recorder = SchedulerJobMetricsRecorder(reg.sink)

    with pytest.raises(asyncio.CancelledError), recorder.track("slow"):
        raise asyncio.CancelledError

    assert reg.runs("slow", OUTCOME_CANCELLED, ERROR_KIND_NONE) == 1.0
    assert reg.duration_count("slow") is None  # it did not finish, so it has no duration
    assert reg.in_progress("slow") == 0.0
    assert reg.last_success("slow") is None


def test__recorder__service_error__error_kind_separates_domain_failures_from_infrastructure() -> None:
    reg = _Registry()
    recorder = SchedulerJobMetricsRecorder(reg.sink)

    recorder.record_run("reconcile", duration=0.3, error=ServiceError(kind=ErrorKind.PRECONDITION_FAILED))
    recorder.record_run("reconcile", duration=0.3, error=ConnectionError("pg gone"))

    assert reg.runs("reconcile", OUTCOME_ERROR, "precondition_failed") == 1.0
    assert reg.runs("reconcile", OUTCOME_ERROR, ERROR_KIND_UNEXPECTED) == 1.0
    assert reg.duration_count("reconcile") == 2.0  # failed runs still have a duration


def test__recorder__prefix_given__prefixes_every_metric() -> None:
    reg = _Registry(prefix="myapp_")
    recorder = SchedulerJobMetricsRecorder(reg.sink, prefix="myapp")

    with recorder.track("j"):
        pass

    assert reg.runs("j", OUTCOME_OK, ERROR_KIND_NONE) == 1.0
    assert reg.duration_count("j") == 1.0
    assert reg.last_success("j") is not None
    assert reg.in_progress("j") == 0.0


def test__recorder__buckets__reach_past_request_scale() -> None:
    assert DEFAULT_SCHEDULER_BUCKETS[-1] == 1800.0
    assert tuple(sorted(DEFAULT_SCHEDULER_BUCKETS)) == DEFAULT_SCHEDULER_BUCKETS


def test__recorder__over_the_null_sink__records_nothing_and_never_raises() -> None:
    recorder = SchedulerJobMetricsRecorder(NullMetricsSink())

    with recorder.track("j"):
        pass
    with pytest.raises(RuntimeError), recorder.track("j"):
        raise RuntimeError
    recorder.record_run("j", duration=0.1, error=asyncio.CancelledError())
