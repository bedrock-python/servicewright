"""Scheduler entrypoint exceptions (ported from the APScheduler prototype)."""

from __future__ import annotations


class SchedulerError(Exception):
    """Base exception for the scheduler entrypoint."""


class DuplicateScheduleError(SchedulerError):
    """Raised when two or more scheduled jobs share the same ``id``."""

    def __init__(self, duplicates: list[str]) -> None:
        self.duplicates = duplicates
        super().__init__(f"Duplicate schedule IDs detected: {duplicates}")


__all__ = [
    "DuplicateScheduleError",
    "SchedulerError",
]
