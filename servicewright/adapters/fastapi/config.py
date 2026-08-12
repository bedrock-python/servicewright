"""Self-contained configuration for :class:`FastApiEntrypoint`.

The :class:`AppSpec` stays transport-neutral, so the HTTP entrypoint owns its
own configuration. Config is taken AT CONSTRUCTION (never read from global
settings). The middleware / metrics / health config dataclasses are faithfully
folded from the FastAPI service-runtime prototype's ``AsyncHttpAppSpec``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- /system namespace defaults (mirror the prototype constants) ------------- #
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8000
DEFAULT_GRACEFUL_TIMEOUT_SECONDS = 10.0

DEFAULT_OPENAPI_URL = "/system/openapi.json"
DEFAULT_DOCS_URL = "/system/docs"
DEFAULT_REDOC_URL = "/system/redoc"

DEFAULT_LIVENESS_PATH = "/system/health/livez"
DEFAULT_READINESS_PATH = "/system/health/readyz"
DEFAULT_METRICS_PATH = "/system/metrics"

# Paths excluded from request logging / metrics (the /system/* namespace).
DEFAULT_IGNORED_PATHS: tuple[str, ...] = (
    DEFAULT_LIVENESS_PATH,
    DEFAULT_READINESS_PATH,
    DEFAULT_METRICS_PATH,
    DEFAULT_DOCS_URL,
    DEFAULT_OPENAPI_URL,
)

DEFAULT_CORS_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "DELETE", "PATCH")
DEFAULT_CORRELATION_ID_HEADER = "X-Request-ID"


@dataclass(slots=True)
class CORSMiddlewareConfig:
    """Configuration for Starlette's ``CORSMiddleware``."""

    enabled: bool = True
    allow_origins: list[str] = field(default_factory=list)
    allow_credentials: bool = False
    allow_methods: list[str] = field(default_factory=lambda: list(DEFAULT_CORS_METHODS))
    allow_headers: list[str] = field(default_factory=lambda: ["*"])
    expose_headers: list[str] = field(default_factory=list)
    max_age: int = 600

    def __post_init__(self) -> None:
        """Reject the insecure ``allow_credentials`` + wildcard-origin combo."""
        if self.allow_credentials and "*" in self.allow_origins:
            raise ValueError(
                "allow_credentials=True cannot be used with allow_origins=['*']. Specify explicit origins for security."
            )


@dataclass(slots=True)
class LoggingMiddlewareConfig:
    """Configuration for the request-logging middleware."""

    enabled: bool = True
    ignored_paths: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORED_PATHS))


@dataclass(slots=True)
class GZipMiddlewareConfig:
    """Configuration for Starlette's ``GZipMiddleware``."""

    enabled: bool = True
    minimum_size: int = 1000


@dataclass(slots=True)
class CorrelationIdMiddlewareConfig:
    """How the request id is returned to the client.

    The id itself is owned by :class:`ContextMiddleware` — the same value that
    lands in the context store, the logs and outbound propagation. Echoing it
    is what lets a caller quote an id that actually appears in your logs.
    """

    enabled: bool = True
    header_name: str = DEFAULT_CORRELATION_ID_HEADER


@dataclass(slots=True)
class MiddlewareConfig:
    """Configuration for the standard platform middleware stack.

    The simple booleans toggle the parameter-less platform middlewares; the
    complex configs carry their own options. ``custom`` lets callers append
    extra ASGI middleware classes (added first so they run last in the stack).
    """

    # Simple toggle-only middlewares.
    context: bool = True
    sentry: bool = True
    processing_time: bool = True

    # Pluggable context propagation: ``None`` = the defaults (structlog +
    # OTel baggage when opentelemetry is installed); pass your own
    # ContextSetter list to replace them.
    context_setters: list[Any] | None = None

    # Complex middlewares.
    logging: LoggingMiddlewareConfig = field(default_factory=LoggingMiddlewareConfig)
    correlation_id: CorrelationIdMiddlewareConfig = field(default_factory=CorrelationIdMiddlewareConfig)
    gzip: GZipMiddlewareConfig = field(default_factory=GZipMiddlewareConfig)
    cors: CORSMiddlewareConfig = field(default_factory=CORSMiddlewareConfig)

    # Custom middlewares: (middleware_class, kwargs).
    custom: list[tuple[type, dict[str, Any]]] = field(default_factory=list)


@dataclass(slots=True)
class MetricsInstrumentatorConfig:
    """Configuration for ``prometheus-fastapi-instrumentator``."""

    init_kwargs: dict[str, Any] = field(default_factory=dict)
    instrument_kwargs: dict[str, Any] = field(default_factory=dict)
    expose_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HealthConfig:
    """Configuration for the ``/system`` health probe routes."""

    enabled: bool = True
    liveness_path: str = DEFAULT_LIVENESS_PATH
    readiness_path: str = DEFAULT_READINESS_PATH


@dataclass(slots=True)
class HttpConfig:
    """Self-contained configuration for an HTTP server entrypoint.

    Taken at construction, never read from global settings, so the AppSpec
    stays transport-neutral.
    """

    host: str = DEFAULT_HTTP_HOST
    port: int = DEFAULT_HTTP_PORT
    graceful_timeout: float = DEFAULT_GRACEFUL_TIMEOUT_SECONDS

    # FastAPI app construction.
    title: str | None = None
    version: str = "0.0.0"
    openapi_url: str = DEFAULT_OPENAPI_URL
    docs_url: str = DEFAULT_DOCS_URL
    redoc_url: str = DEFAULT_REDOC_URL
    redirect_slashes: bool = False
    fastapi_kwargs: dict[str, Any] = field(default_factory=dict)

    # uvicorn server construction.
    uvicorn_kwargs: dict[str, Any] = field(default_factory=dict)

    # Components.
    health: HealthConfig = field(default_factory=HealthConfig)

    @property
    def address(self) -> str:
        """Return the ``host:port`` bind address."""
        return f"{self.host}:{self.port}"
