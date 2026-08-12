"""Lazy import guard for the optional APScheduler 3.x stack.

Importing this module fails with a friendly message when the ``apscheduler3``
extra is not installed, so ``import servicewright`` never pays the cost (or the
failure) of the APScheduler dependency. Every scheduler submodule imports its
third-party symbols from here.

APScheduler 3.x and 4.x are the same distribution and cannot coexist in one
environment; this guard hard-raises whenever 3.x's ``AsyncIOScheduler`` is not
importable (e.g. when v4 is installed instead).
"""

from __future__ import annotations

_INSTALL_HINT = "APScheduler 3.x support requires servicewright[apscheduler3]; install it."

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.base import BaseTrigger as Trigger
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(_INSTALL_HINT) from exc

__all__ = [
    "AsyncIOScheduler",
    "Trigger",
]
