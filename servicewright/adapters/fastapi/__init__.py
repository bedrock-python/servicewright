"""FastAPI entrypoint adapter (``[fastapi]`` extra)."""

from __future__ import annotations

from .config import (
    CorrelationIdMiddlewareConfig,
    CORSMiddlewareConfig,
    GZipMiddlewareConfig,
    HealthConfig,
    HttpConfig,
    LoggingMiddlewareConfig,
    MetricsInstrumentatorConfig,
    MiddlewareConfig,
)
from .context import (
    OtelBaggageSetter,
    StructlogSetter,
    get_default_context_setters,
)
from .entrypoint import (
    ConfigureApp,
    FastApiEntrypoint,
    FastApiPlugin,
    RoutesRegisterer,
)
from .exceptions import setup_default_exception_handlers
from .headers import (
    AuthorizationHeader,
    IdempotencyKey,
    XFingerprintHeader,
    XUserId,
)
from .metrics import setup_metrics_instrumentator
from .schemas import LivenessResponse, ProblemDetails, ReadinessResponse
from .settings import (
    CorrelationIdMiddlewareSettings,
    CORSMiddlewareSettings,
    GZipMiddlewareSettings,
    HealthSettings,
    HttpServerSettings,
    LoggingMiddlewareSettings,
    MiddlewareSettings,
    UvicornSettings,
)
from .unit_scope import (
    UnitScopeDep,
    UnitScopeMiddleware,
    current_unit_scope,
    get_unit_scope,
)

__all__ = [
    "AuthorizationHeader",
    "CORSMiddlewareConfig",
    "CORSMiddlewareSettings",
    "ConfigureApp",
    "CorrelationIdMiddlewareConfig",
    "CorrelationIdMiddlewareSettings",
    "FastApiEntrypoint",
    "FastApiPlugin",
    "GZipMiddlewareConfig",
    "GZipMiddlewareSettings",
    "HealthConfig",
    "HealthSettings",
    "HttpConfig",
    "HttpServerSettings",
    "IdempotencyKey",
    "LivenessResponse",
    "LoggingMiddlewareConfig",
    "LoggingMiddlewareSettings",
    "MetricsInstrumentatorConfig",
    "MiddlewareConfig",
    "MiddlewareSettings",
    "OtelBaggageSetter",
    "ProblemDetails",
    "ReadinessResponse",
    "RoutesRegisterer",
    "StructlogSetter",
    "UnitScopeDep",
    "UnitScopeMiddleware",
    "UvicornSettings",
    "XFingerprintHeader",
    "XUserId",
    "current_unit_scope",
    "get_default_context_setters",
    "get_unit_scope",
    "setup_default_exception_handlers",
    "setup_metrics_instrumentator",
]
