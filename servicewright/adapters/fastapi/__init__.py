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
from .unit_scope import (
    UnitScopeDep,
    UnitScopeMiddleware,
    current_unit_scope,
    get_unit_scope,
)

__all__ = [
    "AuthorizationHeader",
    "CORSMiddlewareConfig",
    "ConfigureApp",
    "CorrelationIdMiddlewareConfig",
    "FastApiEntrypoint",
    "FastApiPlugin",
    "GZipMiddlewareConfig",
    "HealthConfig",
    "HttpConfig",
    "IdempotencyKey",
    "LivenessResponse",
    "LoggingMiddlewareConfig",
    "MetricsInstrumentatorConfig",
    "MiddlewareConfig",
    "OtelBaggageSetter",
    "ProblemDetails",
    "ReadinessResponse",
    "RoutesRegisterer",
    "StructlogSetter",
    "UnitScopeDep",
    "UnitScopeMiddleware",
    "XFingerprintHeader",
    "XUserId",
    "current_unit_scope",
    "get_default_context_setters",
    "get_unit_scope",
    "setup_default_exception_handlers",
    "setup_metrics_instrumentator",
]
