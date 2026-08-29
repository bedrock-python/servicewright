"""Pluggable observability add-on adapters (extra-gated implementation layer).

Side-effect-free: importing this package pulls in no SDK. The author-facing sink
ABCs are re-exported here from :mod:`base`. The concrete backends live in
``_metrics`` / ``_tracing`` / ``_errors`` / ``_logging``, each behind its own
extra and import guard, and are reachable from here too — lazily, through a
module-level ``__getattr__`` (PEP 562), so the package stays importable with no
extra installed and asking for a backend whose extra is missing raises the same
``ImportError`` that names the extra::

    from servicewright.adapters.observability import SentryErrorTrackingSink

Selected by name (``ObsConfig(error_tracking="sentry")``) they are resolved
through :mod:`servicewright.core.observability.registry`; the runtime seams they
implement are defined in :mod:`servicewright.core.contracts.observability`.

Backends are transport-neutral: they expose generic instruments (counter /
histogram); transport adapters own their metric names and recorders.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .base import ErrorTrackingSink as ErrorTrackingSink
from .base import LoggingSink as LoggingSink
from .base import MetricsSink as MetricsSink
from .base import TracingSink as TracingSink

if TYPE_CHECKING:
    # Static re-exports for type checkers and IDEs; at runtime ``__getattr__``
    # imports each backend on first access, so its extra is only needed then.
    from ._errors.sentry import SentryErrorTrackingSink as SentryErrorTrackingSink
    from ._logging.stdlib import StdlibLoggingSink as StdlibLoggingSink
    from ._logging.structlog import StructlogLoggingSink as StructlogLoggingSink
    from ._metrics.prometheus import PrometheusMetricsSink as PrometheusMetricsSink
    from ._tracing.otel import OtelTracingSink as OtelTracingSink

# Concrete backends by public name -> the private module that defines them.
_BACKEND_MODULES: dict[str, str] = {
    "OtelTracingSink": "._tracing.otel",
    "PrometheusMetricsSink": "._metrics.prometheus",
    "SentryErrorTrackingSink": "._errors.sentry",
    "StdlibLoggingSink": "._logging.stdlib",
    "StructlogLoggingSink": "._logging.structlog",
}

__all__ = [
    "ErrorTrackingSink",
    "LoggingSink",
    "MetricsSink",
    "OtelTracingSink",
    "PrometheusMetricsSink",
    "SentryErrorTrackingSink",
    "StdlibLoggingSink",
    "StructlogLoggingSink",
    "TracingSink",
]


def __getattr__(name: str) -> Any:
    """Import a concrete backend on first access; its extra's ``ImportError`` propagates unchanged."""
    try:
        module_name = _BACKEND_MODULES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(importlib.import_module(module_name, __name__), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_BACKEND_MODULES))
