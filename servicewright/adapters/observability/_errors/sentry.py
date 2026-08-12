"""Sentry error-tracking backend: global SDK init + reporter seam.

``setup()`` calls ``sentry_sdk.init`` (process-global, Host-owned); the
cross-cutting redactor from :class:`ObsSetupContext` is applied to every
outgoing event via ``before_send``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..base import ErrorTrackingSink

try:
    import sentry_sdk
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("Sentry error tracking requires servicewright[sentry]; install it.") from exc

if TYPE_CHECKING:
    from ....core.contracts.observability import Redactor
    from ....core.observability.config import ObsSetupContext

logger = logging.getLogger(__name__)

_FLUSH_TIMEOUT_SECONDS = 2.0


class SentryReporter:
    """Thin :class:`ErrorReporterProtocol` seam over the global ``sentry_sdk``."""

    def capture_exception(self, error: Exception) -> None:
        """Report an exception to Sentry."""
        sentry_sdk.capture_exception(error)

    def add_breadcrumb(
        self,
        message: str,
        category: str = "default",
        level: str = "info",
        data: dict[str, object] | None = None,
    ) -> None:
        """Attach a breadcrumb to the current scope."""
        sentry_sdk.add_breadcrumb(message=message, category=category, level=level, data=data)

    def set_tags(self, **tags: str) -> None:
        """Set tags on the current scope."""
        for key, value in tags.items():
            sentry_sdk.set_tag(key, value)


class SentryErrorTrackingSink(ErrorTrackingSink):
    """Sentry backend driven by ``settings.error_tracking``."""

    backend = "sentry"

    def __init__(self) -> None:
        self._initialized = False

    def setup(self, ctx: ObsSetupContext) -> None:
        """Initialize the global Sentry client (idempotent per sink instance)."""
        if self._initialized:
            return
        sentry_settings = getattr(ctx.settings, "error_tracking", None)
        if sentry_settings is None or not getattr(sentry_settings, "dsn", None):
            return

        sentry_sdk.init(
            dsn=sentry_settings.dsn,
            environment=getattr(sentry_settings, "environment", "") or ctx.environment,
            release=ctx.app_version,
            traces_sample_rate=getattr(sentry_settings, "traces_sample_rate", 0.0),
            profiles_sample_rate=getattr(sentry_settings, "profiles_sample_rate", 0.0),
            debug=getattr(sentry_settings, "debug", False),
            before_send=_build_before_send(ctx.redactor),
        )
        self._initialized = True
        logger.info("Sentry error tracking configured", extra={"environment": ctx.environment})

    def shutdown(self) -> None:
        """Flush queued events (best-effort)."""
        if not self._initialized:
            return
        sentry_sdk.flush(timeout=_FLUSH_TIMEOUT_SECONDS)

    def reporter(self) -> SentryReporter:
        """Mint the error-reporter seam."""
        return SentryReporter()


def _build_before_send(redactor: Redactor | None) -> Any:
    if redactor is None:
        return None

    def before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
        return redactor(event)

    return before_send


__all__ = ["SentryErrorTrackingSink", "SentryReporter"]
