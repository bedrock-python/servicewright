"""Backend registry: (concern, backend name) -> lazily imported sink class.

``import servicewright`` pays nothing — the target module (and its SDK import
guard) loads only when the manager resolves a selected backend at Bootstrap.
Adding a backend = one adapter module + one :func:`register_sink` call (or a
built-in entry below); zero kernel changes.
"""

from __future__ import annotations

import importlib
from typing import Any

_SINKS: dict[tuple[str, str], str] = {
    ("metrics", "prometheus"): "servicewright.adapters.observability._metrics.prometheus:PrometheusMetricsSink",
    ("tracing", "otel"): "servicewright.adapters.observability._tracing.otel:OtelTracingSink",
    ("error_tracking", "sentry"): "servicewright.adapters.observability._errors.sentry:SentryErrorTrackingSink",
    ("logging", "structlog"): "servicewright.adapters.observability._logging.structlog:StructlogLoggingSink",
    ("logging", "stdlib"): "servicewright.adapters.observability._logging.stdlib:StdlibLoggingSink",
}


def register_sink(concern: str, backend: str, target: str) -> None:
    """Register (or override) a backend as ``"module.path:ClassName"``."""
    if ":" not in target:
        raise ValueError(f"Sink target must look like 'module.path:ClassName', got: {target!r}")
    _SINKS[(concern, backend)] = target


def resolve_sink(concern: str, backend: str) -> type[Any]:
    """Import and return the sink class registered for ``(concern, backend)``.

    Raises:
        ValueError: If no backend is registered under that name.
        ImportError: If the backend's extra is not installed (the adapter's
            import guard raises with an install hint).
    """
    target = _SINKS.get((concern, backend))
    if target is None:
        known = sorted(name for (c, name) in _SINKS if c == concern)
        raise ValueError(f"Unknown {concern} backend {backend!r}; registered backends: {known}")
    module_path, _, attribute = target.partition(":")
    module = importlib.import_module(module_path)
    sink_class: type[Any] = getattr(module, attribute)
    return sink_class


__all__ = ["register_sink", "resolve_sink"]
