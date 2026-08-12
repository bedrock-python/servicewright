"""First-party zero-dependency entrypoint adapters (no extra required)."""

from __future__ import annotations

from .daemon import DaemonEntrypoint as DaemonEntrypoint
from .oneshot import OneShotEntrypoint as OneShotEntrypoint

__all__ = ["DaemonEntrypoint", "OneShotEntrypoint"]
