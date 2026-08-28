"""Sentry error-tracking backend: global SDK init + reporter seam.

``setup()`` calls ``sentry_sdk.init`` (process-global, Host-owned); the
cross-cutting redactor from :class:`ObsSetupContext` is applied to every
outgoing event via ``before_send``. The settings section models only the
deployment facts (DSN, environment, sampling); everything else
``sentry_sdk.init`` accepts — ``ignore_errors``, a ``before_send`` that drops,
``before_send_transaction``, ``traces_sampler``, ``integrations`` — is passed
through the sink's constructor.
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
    from collections.abc import Callable

    from ....core.contracts.observability import Redactor
    from ....core.observability.config import ObsSetupContext

logger = logging.getLogger(__name__)

_FLUSH_TIMEOUT_SECONDS = 2.0

# ``sentry_sdk.init`` arguments the sink derives from ``settings.error_tracking``
# and the setup context; passing them to the constructor too would make two
# sources of truth for one value, so they are rejected up front instead.
_SETTINGS_DRIVEN_INIT_ARGS = frozenset(
    {"dsn", "environment", "release", "traces_sample_rate", "profiles_sample_rate", "debug"}
)


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
    """Sentry backend driven by ``settings.error_tracking``.

    Args:
        **init_kwargs: Any further ``sentry_sdk.init`` argument the settings
            section does not model — ``ignore_errors``, ``before_send``,
            ``before_send_transaction``, ``traces_sampler``, ``integrations``,
            ``send_default_pii``, ... — used through instance injection::

                ObservabilityManager(
                    ObsConfig(error_tracking="sentry"),
                    error_tracking=SentryErrorTrackingSink(ignore_errors=[DomainError]),
                )

            The settings-driven arguments (``dsn``, ``environment``,
            ``release``, the sample rates, ``debug``) stay the sink's business
            and are rejected here. A ``before_send`` given this way is composed
            with the redactor rather than replaced by it: it runs first, with
            the full ``(event, hint)``, and may return ``None`` to drop the
            event; whatever survives is redacted.

    Raises:
        ValueError: If ``init_kwargs`` names a settings-driven argument.
    """

    backend = "sentry"

    def __init__(self, **init_kwargs: Any) -> None:
        clashing = sorted(_SETTINGS_DRIVEN_INIT_ARGS.intersection(init_kwargs))
        if clashing:
            raise ValueError(
                f"{', '.join(clashing)}: driven by settings.error_tracking and the setup context, "
                "not by SentryErrorTrackingSink arguments"
            )
        self._init_kwargs = init_kwargs
        self._initialized = False

    def setup(self, ctx: ObsSetupContext) -> None:
        """Initialize the global Sentry client (idempotent per sink instance)."""
        if self._initialized:
            return
        sentry_settings = getattr(ctx.settings, "error_tracking", None)
        if sentry_settings is None or not getattr(sentry_settings, "dsn", None):
            return

        init_kwargs = dict(self._init_kwargs)
        before_send = init_kwargs.pop("before_send", None)
        sentry_sdk.init(
            **init_kwargs,
            dsn=sentry_settings.dsn,
            environment=getattr(sentry_settings, "environment", "") or ctx.environment,
            release=ctx.app_version,
            traces_sample_rate=getattr(sentry_settings, "traces_sample_rate", 0.0),
            profiles_sample_rate=getattr(sentry_settings, "profiles_sample_rate", 0.0),
            debug=getattr(sentry_settings, "debug", False),
            before_send=_build_before_send(ctx.redactor, before_send),
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


def _build_before_send(redactor: Redactor | None, hook: Callable[..., Any] | None) -> Any:
    """Compose the caller's ``before_send`` with the redactor.

    The caller's hook decides *whether* an event is reported — it sees the
    ``hint`` (``exc_info``, ``log_record``) and returns ``None`` to drop — and
    the redactor decides *what it may contain*, so it runs on whatever the hook
    lets through. Either side may be absent.
    """
    if redactor is None and hook is None:
        return None

    def before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
        if hook is not None:
            filtered = hook(event, hint)
            if filtered is None:
                return None
            event = filtered
        return redactor(event) if redactor is not None else event

    return before_send


__all__ = ["SentryErrorTrackingSink", "SentryReporter"]
