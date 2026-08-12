"""Lazy import guard for the optional APScheduler stack.

Importing this module fails with a friendly message when the ``scheduler`` extra
is not installed, so ``import servicewright`` never pays the cost (or the failure)
of the APScheduler dependency. Every scheduler submodule imports its third-party
symbols from here.
"""

from __future__ import annotations

_INSTALL_HINT = "APScheduler 4.x support requires servicewright[apscheduler4]; install it."

try:
    from apscheduler import AsyncScheduler, CoalescePolicy, RunState, Schedule
    from apscheduler.abc import Trigger
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(_INSTALL_HINT) from exc

__all__ = [
    "AsyncScheduler",
    "CoalescePolicy",
    "RunState",
    "Schedule",
    "Trigger",
]
