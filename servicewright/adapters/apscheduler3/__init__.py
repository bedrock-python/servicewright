"""APScheduler 3.x scheduler entrypoint adapter (``[apscheduler3]`` extra).

Public surface is identical to the ``apscheduler4`` adapter (enforced by an AST
conformance test); only the implementation differs, against APScheduler 3.x.
"""

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
