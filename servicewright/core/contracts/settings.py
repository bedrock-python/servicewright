"""Service settings protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..observability.protocols import (
        ErrorTrackingSettingsProtocol,
        LoggingSettingsProtocol,
        MetricsSettingsProtocol,
        TracingSettingsProtocol,
    )


class BaseServiceSettingsProtocol(Protocol):
    """Protocol for complete microservice settings.

    Observability sections are named by concern; ``None`` means the concern is
    unconfigured (its sink stays a NullObject).
    """

    @property
    def logging(self) -> LoggingSettingsProtocol | None:
        """Get logging configuration."""
        ...

    @property
    def metrics(self) -> MetricsSettingsProtocol | None:
        """Get metrics configuration."""
        ...

    @property
    def error_tracking(self) -> ErrorTrackingSettingsProtocol | None:
        """Get error-tracking configuration."""
        ...

    @property
    def tracing(self) -> TracingSettingsProtocol | None:
        """Get tracing configuration."""
        ...

    def get_app_version(self) -> str:
        """Return application version string."""
        ...


__all__ = ["BaseServiceSettingsProtocol"]
