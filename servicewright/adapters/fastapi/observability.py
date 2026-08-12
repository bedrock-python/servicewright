"""FastAPI-specific OpenTelemetry instrumentation.

The Host configures the observability sinks itself (BEFORE ``bind``), so this
entrypoint must NOT set up SDKs. But FastAPI instrumentation needs the *app
instance*, which only exists at ``bind`` time — so the entrypoint asks the
app's tracing sink to instrument the app DIRECTLY here. When tracing is
disabled/unconfigured the sink is a NullObject and this is a no-op.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...core.spec import ServiceContext
    from ._imports import FastAPI
    from .config import HealthConfig, MiddlewareConfig

logger = logging.getLogger(__name__)


def _build_excluded_urls(
    otel_settings: Any,
    middlewares: MiddlewareConfig,
    health: HealthConfig,
) -> str:
    """Assemble the comma-joined excluded-URL set (settings + ignored + health)."""
    excluded: set[str] = set()

    excluded_urls = getattr(otel_settings, "excluded_urls", None)
    if excluded_urls:
        excluded.update(path.strip() for path in str(excluded_urls).split(",") if path.strip())

    excluded.update(middlewares.logging.ignored_paths)

    if health.enabled:
        excluded.add(health.liveness_path)
        excluded.add(health.readiness_path)

    return ",".join(sorted(excluded))


def instrument_fastapi_app(
    app: FastAPI,
    ctx: ServiceContext[Any, Any],
    *,
    middlewares: MiddlewareConfig,
    health: HealthConfig,
) -> None:
    """Instrument ``app`` for tracing through the configured tracing sink.

    No-op when the service has no tracing settings (the sink is then a
    NullObject anyway); the sink itself degrades with a friendly warning when
    the FastAPI instrumentor is unavailable.
    """
    otel_settings = getattr(ctx.settings, "tracing", None)
    if not otel_settings:
        return

    excluded_urls = _build_excluded_urls(otel_settings, middlewares, health)
    ctx.observability.tracing.instrument_fastapi(app, excluded_urls=excluded_urls)


__all__ = ["instrument_fastapi_app"]
