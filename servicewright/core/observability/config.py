"""Pluggable add-ons configuration: per-app defaults + per-entrypoint overrides.

SDK init is process-global (one error-tracking client, one global tracer
provider, one default metrics registry); what is per-entrypoint is *which*
initialized sink each entrypoint emits to. ``ObsConfig`` carries the app-wide
backend choice by name.

Backend names are OPEN strings: the built-ins ship in the registry
(``prometheus`` / ``otel`` / ``sentry`` / ``structlog`` / ``stdlib``) and any
third-party backend registered via
:func:`~servicewright.core.observability.registry.register_sink` is selectable
the same way. An unknown name fails fast at Bootstrap with the list of
registered backends — a typo cannot ship silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..contracts import BaseServiceSettingsProtocol
    from ..contracts.observability import Redactor


@dataclass(frozen=True, slots=True)
class ObsSetupContext:
    """Built by the manager from settings + spec and handed to each sink's ``setup()``.

    Observability config must be reachable from ``settings`` (DSN, collector URL,
    tokens): ``setup()`` runs in Bootstrap, *before* the DI container exists, so
    container-resolved secrets are unavailable at sink setup time.

    ``redactor`` arrives already resolved for the receiving sink's surface: the
    manager applies its per-surface overrides (``log_redactor`` /
    ``error_redactor`` / ``trace_redactor``) before handing the context over,
    so a sink never has to know which override chain produced it.
    """

    service_name: str
    app_version: str
    environment: str
    settings: BaseServiceSettingsProtocol
    redactor: Redactor | None = None


@dataclass(frozen=True, slots=True)
class ObsConfig:
    """App-wide add-on backend defaults (the process-global selection).

    Selecting a backend here says *which implementation to use when the concern
    is configured*; whether the concern is active is decided by the settings
    (``settings.error_tracking.dsn`` present, ``settings.tracing`` present, ...).
    Missing extra for a selected+configured backend hard-raises at Bootstrap.
    """

    metrics: str | None = "prometheus"
    tracing: str | None = "otel"
    error_tracking: str | None = "sentry"
    logging: str | None = "structlog"


__all__ = [
    "ObsConfig",
    "ObsSetupContext",
]
