"""Settings protocols for the four observability concerns.

Sections are named by CONCERN (logging / metrics / tracing / error-tracking),
never by vendor — which backend consumes a section is decided by
:class:`~servicewright.core.observability.config.ObsConfig`. Each protocol
declares only the minimal shape the built-in sinks need; backends are free to
read extra fields off the same section via ``getattr``.
"""

from __future__ import annotations

from typing import Literal, Protocol

LogLevelStr = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LoggingSettingsProtocol(Protocol):
    """Logging concern: root level and rendering format."""

    level: LogLevelStr | str
    use_json: bool


class ErrorTrackingSettingsProtocol(Protocol):
    """Error-tracking concern: reporting endpoint and sampling."""

    dsn: str | None
    environment: str
    traces_sample_rate: float
    profiles_sample_rate: float
    debug: bool


class TracingSettingsProtocol(Protocol):
    """Tracing concern: exporter endpoint and sampling."""

    service_name: str
    collector_url: str | None
    sample_ratio: float
    insecure: bool
    enable_console_exporter: bool
    excluded_urls: str | None


class MetricsSettingsProtocol(Protocol):
    """Metrics concern: standalone exposition endpoint."""

    enabled: bool
    port: int
    host: str
    prefix: str | None


__all__ = [
    "ErrorTrackingSettingsProtocol",
    "LogLevelStr",
    "LoggingSettingsProtocol",
    "MetricsSettingsProtocol",
    "TracingSettingsProtocol",
]
