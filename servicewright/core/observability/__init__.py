"""Observability add-ons layer: config, sink protocols, registry, manager."""

from __future__ import annotations

from .config import (
    ObsConfig,
    ObsSetupContext,
)
from .manager import ObservabilityManager
from .naming import make_metric_name
from .null import (
    NullCounter,
    NullErrorReporter,
    NullErrorTrackingSink,
    NullHistogram,
    NullLoggingSink,
    NullMetricsSink,
    NullSpan,
    NullTracer,
    NullTracingSink,
)
from .protocols import (
    ErrorTrackingSettingsProtocol,
    LoggingSettingsProtocol,
    LogLevelStr,
    MetricsSettingsProtocol,
    TracingSettingsProtocol,
)
from .redaction import DEFAULT_SENSITIVE_KEYS, KeyRedactor
from .registry import register_sink, resolve_sink
from .sinks import (
    ErrorTrackingSinkProtocol,
    LoggingSinkProtocol,
    MetricsSinkProtocol,
    TracingSinkProtocol,
)

__all__ = [
    "DEFAULT_SENSITIVE_KEYS",
    "ErrorTrackingSettingsProtocol",
    "ErrorTrackingSinkProtocol",
    "KeyRedactor",
    "LogLevelStr",
    "LoggingSettingsProtocol",
    "LoggingSinkProtocol",
    "MetricsSettingsProtocol",
    "MetricsSinkProtocol",
    "NullCounter",
    "NullErrorReporter",
    "NullErrorTrackingSink",
    "NullHistogram",
    "NullLoggingSink",
    "NullMetricsSink",
    "NullSpan",
    "NullTracer",
    "NullTracingSink",
    "ObsConfig",
    "ObsSetupContext",
    "ObservabilityManager",
    "TracingSettingsProtocol",
    "TracingSinkProtocol",
    "make_metric_name",
    "register_sink",
    "resolve_sink",
]
