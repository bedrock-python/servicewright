"""Configurators that assemble the FastAPI app: health, middleware, handlers.

Folded from the prototype's ``configurators`` module and re-pointed onto the new
model:

- Health routes are driven by the transport-agnostic
  :class:`~servicewright.core.health.HealthRegistry` (NOT the prototype's
  orchestrator/cache/checkers, which now live in ``servicewright/health``).
- The middleware stack and exception handlers are folded faithfully.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ._imports import (
    CORSMiddleware,
    GZipMiddleware,
    Response,
    status,
)
from .context import get_default_context_setters
from .exceptions import setup_default_exception_handlers
from .middlewares import (
    ContextMiddleware,
    LoggingMiddleware,
    ProcessingTimeMiddleware,
    SentryMiddleware,
    UnhandledErrorMiddleware,
)
from .schemas import LivenessResponse, ReadinessResponse
from .unit_scope import UnitScopeMiddleware

if TYPE_CHECKING:
    from ...core.errors import HttpErrorRendererProtocol
    from ...core.health import HealthRegistry
    from .config import MiddlewareConfig
    from .exceptions import ExceptionHandler

logger = logging.getLogger(__name__)


def is_valid_uuid(uuid_string: str) -> bool:
    """Accept any valid UUID (not just v4) as a correlation-id validator."""
    try:
        UUID(uuid_string)
    except (ValueError, AttributeError):
        return False
    return True


def setup_health_routes(app: Any, health: HealthRegistry, *, liveness_path: str, readiness_path: str) -> None:
    """Register ``/system`` liveness + readiness routes on the registry.

    Liveness always reports ``ok`` (the process is up). Readiness reflects
    :meth:`HealthRegistry.readiness` (the ``ready`` flag AND every check),
    returning 200 when healthy and 503 otherwise.
    """

    @app.get(liveness_path, tags=["health"], response_model=LivenessResponse)
    async def liveness() -> LivenessResponse:
        await health.liveness()
        return LivenessResponse(status="ok")

    @app.get(readiness_path, tags=["health"])
    async def readiness() -> Response:
        report = await health.readiness()
        body = ReadinessResponse(status="ok" if report.healthy else "unhealthy")
        status_code = status.HTTP_200_OK if report.healthy else status.HTTP_503_SERVICE_UNAVAILABLE
        return Response(
            content=body.model_dump_json(),
            status_code=status_code,
            media_type="application/json",
        )


def setup_middleware_stack(
    app: Any,
    config: MiddlewareConfig,
    container: Any,
    *,
    error_renderer: HttpErrorRendererProtocol | None = None,
) -> None:
    """Install the platform middleware stack in the documented order.

    Starlette runs middlewares in reverse registration order (last added = first
    to run). We register from innermost to outermost, so the order below is:
    custom -> CORS -> GZip -> logging -> processing-time -> sentry -> context
    -> unit-scope, with the unit scope as the outermost wrapper (so every other
    middleware + the handler see a live unit scope).

    ``ContextMiddleware`` is the single owner of the request id: it extracts or
    mints it, binds it into the context store and echoes it back to the client,
    so the logged, propagated and returned ids are the same value.
    """
    # Custom middlewares first (run last/innermost in the stack).
    for middleware_class, kwargs in config.custom:
        app.add_middleware(middleware_class, **kwargs)

    if config.cors.enabled:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors.allow_origins,
            allow_credentials=config.cors.allow_credentials,
            allow_methods=config.cors.allow_methods,
            allow_headers=config.cors.allow_headers,
            expose_headers=config.cors.expose_headers,
            max_age=config.cors.max_age,
        )

    if config.gzip.enabled:
        app.add_middleware(GZipMiddleware, minimum_size=config.gzip.minimum_size)

    if config.logging.enabled:
        app.add_middleware(LoggingMiddleware, ignored_paths=config.logging.ignored_paths)

    if config.processing_time:
        app.add_middleware(ProcessingTimeMiddleware)

    if config.sentry:
        app.add_middleware(SentryMiddleware)

    # Inside the context layer (so the 500 carries the request id) but outside
    # Sentry (which reports the exception on its way out).
    app.add_middleware(UnhandledErrorMiddleware, renderer=error_renderer)

    if config.context:
        setters = config.context_setters if config.context_setters is not None else get_default_context_setters()
        app.add_middleware(
            ContextMiddleware,
            context_setters=setters,
            request_id_header=config.correlation_id.header_name,
            echo_request_id=config.correlation_id.enabled,
        )

    # Unit scope LAST so it is the OUTERMOST middleware: every other middleware
    # and the handler run inside a live per-request UnitScope.
    app.add_middleware(UnitScopeMiddleware, container=container)


def setup_exception_handlers(
    app: Any,
    *,
    setup_defaults: bool,
    error_renderer: HttpErrorRendererProtocol | None,
    custom_handlers: dict[type[Exception], ExceptionHandler],
) -> None:
    """Register default + caller-supplied exception handlers."""
    if setup_defaults:
        setup_default_exception_handlers(app, renderer=error_renderer)

    for exc_class, handler in custom_handlers.items():
        app.add_exception_handler(exc_class, handler)


__all__ = [
    "is_valid_uuid",
    "setup_exception_handlers",
    "setup_health_routes",
    "setup_middleware_stack",
]
