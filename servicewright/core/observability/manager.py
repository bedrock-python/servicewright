"""The multi-sink observability orchestrator (the add-ons layer entry point).

The Host owns SDK bootstrap: it calls :meth:`ObservabilityManager.configure`
first thing in Bootstrap (so bootstrap failures are observable) and
:meth:`ObservabilityManager.shutdown` last in Cleanup.

Two ways to select a backend, mixable per concern:

- **by name** (declarative, settings-friendly): ``ObsConfig(metrics="prometheus")``
  resolves lazily through the registry — ``main.py`` imports no SDK;
- **by instance** (pure DI): pass a ready sink — ``ObservabilityManager(metrics=MySink())``
  — bypassing the registry entirely. An instance wins over the config name.

Degradation rule (uniform):
- selected backend + configured settings + missing extra => **hard-raise** at
  Bootstrap (the adapter's import guard names the extra to install);
- concern disabled (no selection at all) or unconfigured in settings =>
  **NullObject** sink, emitters never see ``None``.
"""

from __future__ import annotations

from dataclasses import replace
from logging import getLogger
from typing import TYPE_CHECKING, Any

from .config import ObsConfig, ObsSetupContext
from .null import (
    NullErrorTrackingSink,
    NullLoggingSink,
    NullMetricsSink,
    NullTracingSink,
)
from .registry import resolve_sink

if TYPE_CHECKING:
    from ..contracts import BaseServiceSettingsProtocol
    from ..contracts.observability import Redactor
    from .sinks import (
        ErrorTrackingSinkProtocol,
        LoggingSinkProtocol,
        MetricsSinkProtocol,
        TracingSinkProtocol,
    )

logger = getLogger(__name__)


class ObservabilityManager:
    """Resolves, sets up and tears down the four add-on sinks.

    Args:
        config: App-wide backend selection by name. ``None`` selects nothing
            (concerns without an instance stay NullObject) — the default for a
            bare ``AppSpec``.
        redactor: Cross-cutting sensitive-data filter threaded into the
            logging, error-tracking and tracing sinks (every payload surface;
            metrics carry no payloads and are exempt).
        log_redactor: Per-surface override for the logging sink; wins over
            ``redactor`` there. Surfaces differ in volume by orders of
            magnitude — this is how a cheap masker goes on every log line
            while an expensive one guards only the error path.
        error_redactor: Per-surface override for the error-tracking sink.
            The natural home for ML-grade maskers: events are rare, shipped
            off the request path, and leak the most (stack-frame locals,
            request bodies).
        trace_redactor: Per-surface override for span attributes.
        metrics: Ready metrics sink instance (wins over ``config.metrics``).
        tracing: Ready tracing sink instance (wins over ``config.tracing``).
        error_tracking: Ready error-tracking sink instance (wins over
            ``config.error_tracking``).
        logging: Ready logging sink instance (wins over ``config.logging``).
    """

    def __init__(
        self,
        config: ObsConfig | None = None,
        *,
        redactor: Redactor | None = None,
        log_redactor: Redactor | None = None,
        error_redactor: Redactor | None = None,
        trace_redactor: Redactor | None = None,
        metrics: MetricsSinkProtocol | None = None,
        tracing: TracingSinkProtocol | None = None,
        error_tracking: ErrorTrackingSinkProtocol | None = None,
        logging: LoggingSinkProtocol | None = None,
    ) -> None:
        self._config = config
        self._redactor = redactor
        self._surface_redactors: dict[str, Redactor | None] = {
            "logging": log_redactor,
            "error_tracking": error_redactor,
            "tracing": trace_redactor,
        }
        self._configured = False
        # (concern, sink) pairs in setup order; sinks are registry-resolved and
        # therefore dynamically typed at this boundary.
        self._active: list[tuple[str, Any]] = []

        self._provided: dict[str, Any] = {
            "logging": logging,
            "error_tracking": error_tracking,
            "tracing": tracing,
            "metrics": metrics,
        }

        self._metrics: MetricsSinkProtocol = NullMetricsSink()
        self._tracing: TracingSinkProtocol = NullTracingSink()
        self._error_tracking: ErrorTrackingSinkProtocol = NullErrorTrackingSink()
        self._logging: LoggingSinkProtocol = NullLoggingSink()

    @property
    def config(self) -> ObsConfig | None:
        """The app-wide backend selection."""
        return self._config

    @property
    def metrics(self) -> MetricsSinkProtocol:
        """The metrics sink (NullObject until configured)."""
        return self._metrics

    @property
    def tracing(self) -> TracingSinkProtocol:
        """The tracing sink (NullObject until configured)."""
        return self._tracing

    @property
    def error_tracking(self) -> ErrorTrackingSinkProtocol:
        """The error-tracking sink (NullObject until configured)."""
        return self._error_tracking

    @property
    def logging(self) -> LoggingSinkProtocol:
        """The logging sink (NullObject until configured)."""
        return self._logging

    def configure(self, settings: BaseServiceSettingsProtocol, *, service_name: str = "") -> None:
        """Resolve and set up every selected+configured sink (fail-fast).

        Setup order is ``logging -> error-tracking -> tracing -> metrics`` so the
        earliest failures are already logged and reported.
        """
        if self._configured:
            logger.warning("ObservabilityManager already configured, ignoring duplicate call")
            return

        ctx = self._build_setup_context(settings, service_name)

        gates = {
            "logging": self._is_logging_configured,
            "error_tracking": self._is_error_tracking_configured,
            "tracing": self._is_tracing_configured,
            "metrics": self._is_metrics_configured,
        }
        for concern in ("logging", "error_tracking", "tracing", "metrics"):
            # An explicit instance is unconditional (its setup() decides what to
            # do with settings); a by-name selection additionally requires the
            # concern to be configured in settings.
            sink = self._provided.get(concern)
            if sink is None:
                backend = getattr(self._config, concern, None) if self._config is not None else None
                if backend is None or not gates[concern](settings):
                    continue
                sink = resolve_sink(concern, backend)()
            # Each sink sees the redactor resolved for its own surface: the
            # per-surface override, else the cross-cutting one. Metrics carry
            # no payloads, so their ctx never advertises a redactor.
            sink.setup(replace(ctx, redactor=self._redactor_for(concern)))
            self._active.append((concern, sink))
            setattr(self, f"_{concern}", sink)
            logger.info(
                "Observability sink configured",
                extra={"concern": concern, "backend": getattr(sink, "backend", "?")},
            )

        self._configured = True

    def shutdown(self) -> None:
        """Tear down active sinks in reverse setup order (best-effort, never raises).

        Returns the manager to its pre-``configure`` state: the concerns fall
        back to their NullObject sinks and the manager can be configured again.
        A ``Service`` reuses one long-lived ``AppSpec`` across runs, so leaving
        the torn-down sinks in place would give run 2 a stack that reports the
        real sink type while logging, metrics and tracing all silently go
        nowhere. Per-run idempotency stays where it belongs — inside each sink's
        own ``setup``.
        """
        for concern, sink in reversed(self._active):
            try:
                sink.shutdown()
            except Exception:
                logger.exception("Observability sink shutdown failed", extra={"concern": concern})
        self._active.clear()

        self._metrics = NullMetricsSink()
        self._tracing = NullTracingSink()
        self._error_tracking = NullErrorTrackingSink()
        self._logging = NullLoggingSink()
        self._configured = False

    def _redactor_for(self, concern: str) -> Redactor | None:
        if concern == "metrics":
            return None
        override = self._surface_redactors.get(concern)
        return override if override is not None else self._redactor

    def _build_setup_context(self, settings: BaseServiceSettingsProtocol, service_name: str) -> ObsSetupContext:
        environment = getattr(settings, "environment", None)
        if not environment:
            error_settings = getattr(settings, "error_tracking", None)
            environment = getattr(error_settings, "environment", "") if error_settings else ""
        return ObsSetupContext(
            service_name=service_name,
            app_version=settings.get_app_version(),
            environment=environment or "",
            settings=settings,
            redactor=self._redactor,
        )

    @staticmethod
    def _is_logging_configured(settings: BaseServiceSettingsProtocol) -> bool:
        return getattr(settings, "logging", None) is not None

    @staticmethod
    def _is_error_tracking_configured(settings: BaseServiceSettingsProtocol) -> bool:
        error_settings = getattr(settings, "error_tracking", None)
        return error_settings is not None and bool(getattr(error_settings, "dsn", None))

    @staticmethod
    def _is_tracing_configured(settings: BaseServiceSettingsProtocol) -> bool:
        return getattr(settings, "tracing", None) is not None

    @staticmethod
    def _is_metrics_configured(settings: BaseServiceSettingsProtocol) -> bool:
        return getattr(settings, "metrics", None) is not None


__all__ = ["ObservabilityManager"]
