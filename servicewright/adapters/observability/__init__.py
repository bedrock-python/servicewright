"""Pluggable observability add-on adapters (extra-gated implementation layer).

Side-effect-free: importing this package pulls in no SDK. The author-facing sink
ABCs are re-exported here from :mod:`base`; concrete backends live in ``_metrics``
/ ``_tracing`` / ``_errors`` / ``_logging`` (each behind its own extra and import
guard) and are resolved lazily through
:mod:`servicewright.core.observability.registry`. The runtime seams they
implement are defined in :mod:`servicewright.core.contracts.observability`.

Backends are transport-neutral: they expose generic instruments (counter /
histogram); transport adapters own their metric names and recorders.
"""

from __future__ import annotations

from .base import ErrorTrackingSink as ErrorTrackingSink
from .base import LoggingSink as LoggingSink
from .base import MetricsSink as MetricsSink
from .base import TracingSink as TracingSink

__all__ = [
    "ErrorTrackingSink",
    "LoggingSink",
    "MetricsSink",
    "TracingSink",
]
