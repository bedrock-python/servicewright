"""Settings protocols for the four observability concerns.

Sections are named by CONCERN (logging / metrics / tracing / error-tracking),
never by vendor — which backend consumes a section is decided by
:class:`~servicewright.core.observability.config.ObsConfig`. Each protocol
declares only the minimal shape the built-in sinks need; backends are free to
read extra fields off the same section via ``getattr``. Members are read-only
properties rather than mutable attributes because nothing in the library ever
writes to a section, so a settings model is free to NARROW what it declares — a
``Literal`` log level, a bounded sample ratio, a ``port`` newtype — and still
satisfy the protocol.
"""

from __future__ import annotations

from typing import Literal, Protocol

LogLevelStr = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LoggingSettingsProtocol(Protocol):
    """Logging concern: root level and rendering format."""

    @property
    def level(self) -> LogLevelStr | str: ...

    @property
    def use_json(self) -> bool: ...


class ErrorTrackingSettingsProtocol(Protocol):
    """Error-tracking concern: reporting endpoint and sampling."""

    @property
    def dsn(self) -> str | None: ...

    @property
    def environment(self) -> str: ...

    @property
    def traces_sample_rate(self) -> float: ...

    @property
    def profiles_sample_rate(self) -> float: ...

    @property
    def debug(self) -> bool: ...


class TracingSettingsProtocol(Protocol):
    """Tracing concern: exporter endpoint and sampling."""

    @property
    def service_name(self) -> str: ...

    @property
    def collector_url(self) -> str | None: ...

    @property
    def sample_ratio(self) -> float: ...

    @property
    def insecure(self) -> bool: ...

    @property
    def enable_console_exporter(self) -> bool: ...

    @property
    def excluded_urls(self) -> str | None: ...


class MetricsSettingsProtocol(Protocol):
    """Metrics concern: standalone exposition endpoint."""

    @property
    def enabled(self) -> bool: ...

    @property
    def port(self) -> int: ...

    @property
    def host(self) -> str: ...

    @property
    def prefix(self) -> str | None: ...


__all__ = [
    "ErrorTrackingSettingsProtocol",
    "LogLevelStr",
    "LoggingSettingsProtocol",
    "MetricsSettingsProtocol",
    "TracingSettingsProtocol",
]
