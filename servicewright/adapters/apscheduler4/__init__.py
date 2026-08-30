"""APScheduler 4.x scheduler entrypoint adapter (``[apscheduler4]`` extra)."""

from __future__ import annotations

from .config import ScheduledJob, ScheduledJobFunc
from .entrypoint import SchedulerEntrypoint, SchedulerPlugin
from .exceptions import DuplicateScheduleError, SchedulerError
from .metrics import SchedulerJobMetricsRecorder

__all__ = [
    "DuplicateScheduleError",
    "ScheduledJob",
    "ScheduledJobFunc",
    "SchedulerEntrypoint",
    "SchedulerError",
    "SchedulerJobMetricsRecorder",
    "SchedulerPlugin",
]
