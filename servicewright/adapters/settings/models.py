"""Pydantic models for the settings contract, one per observability section.

The kernel reads settings structurally
(:class:`~servicewright.core.contracts.BaseServiceSettingsProtocol` and the four
section protocols) and the built-in sinks fall back to a default with ``getattr``
whenever a field is missing. A hand-written model can therefore drift silently:
a field that should be optional becomes required, a misspelled one falls back to a
value nobody can see. These models are the contract written down once — fields
exactly the protocols', defaults exactly the sinks' fallbacks — plus
:class:`BaseServiceSettings` composing them and loading them from the environment
through pydantic-settings.

Subclass :class:`BaseServiceSettings` and keep the rest::

    from pydantic_settings import SettingsConfigDict

    from servicewright.adapters.settings import BaseServiceSettings


    class Settings(BaseServiceSettings):
        model_config = SettingsConfigDict(env_file=".env")

        app_version: str = "1.4.0"


    # LOGGING__LEVEL=DEBUG TRACING__COLLECTOR_URL=otel:4317 python main.py

Importing this module requires the ``settings`` extra::

    pip install servicewright[settings]
"""

from __future__ import annotations

from ...core.observability import LogLevelStr

try:
    from pydantic import BaseModel, Field, field_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("Settings models require servicewright[settings]; install it.") from exc


class LoggingSettings(BaseModel):
    """``settings.logging``: root level and rendering format.

    Satisfies :class:`~servicewright.core.observability.LoggingSettingsProtocol`.
    The level is validated against :data:`~servicewright.core.observability.LogLevelStr`
    (case-insensitively, so ``LOGGING__LEVEL=debug`` works) instead of silently
    falling back to ``INFO`` on a typo, which is what the sinks would do.
    """

    level: LogLevelStr = Field(default="INFO", description="Root log level; case-insensitive on input")
    use_json: bool = Field(default=True, description="JSON lines (True) or human-readable console output")

    @field_validator("level", mode="before")
    @classmethod
    def _uppercase_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class MetricsSettings(BaseModel):
    """``settings.metrics``: the standalone exposition endpoint.

    Satisfies :class:`~servicewright.core.observability.MetricsSettingsProtocol`.
    A present section activates the metrics sink; ``enabled`` only decides whether
    the sink also serves ``/metrics`` on its own port (the FastAPI entrypoint
    serves ``/system/metrics`` regardless).
    """

    enabled: bool = Field(default=False, description="Start a standalone exposition server")
    host: str = Field(default="0.0.0.0", description="Bind host of that server")
    port: int = Field(default=9090, ge=0, le=65535, description="Bind port of that server")
    prefix: str | None = Field(default=None, description="Metric name prefix, for backends that apply one globally")


class TracingSettings(BaseModel):
    """``settings.tracing``: exporter endpoint and sampling.

    Satisfies :class:`~servicewright.core.observability.TracingSettingsProtocol`.
    A present section installs the tracer provider even without a
    ``collector_url`` (spans are sampled but exported nowhere), which is why
    :class:`BaseServiceSettings` leaves it ``None`` until configured.
    """

    service_name: str = Field(default="", description="Resource service name; empty falls back to AppSpec.service_name")
    collector_url: str | None = Field(default=None, description="OTLP gRPC endpoint; None installs no exporter")
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0, description="Ratio for the parent-based sampler")
    insecure: bool = Field(default=True, description="Plaintext OTLP connection")
    enable_console_exporter: bool = Field(default=False, description="Also print spans to stdout")
    excluded_urls: str | None = Field(default=None, description="Comma-separated request paths to skip (FastAPI)")


class ErrorTrackingSettings(BaseModel):
    """``settings.error_tracking``: reporting endpoint and sampling.

    Satisfies :class:`~servicewright.core.observability.ErrorTrackingSettingsProtocol`.
    The concern stays off while ``dsn`` is empty, so the section can be present by
    default and switch on from ``ERROR_TRACKING__DSN`` alone.
    """

    dsn: str | None = Field(default=None, description="Reporting endpoint; empty keeps the concern off")
    environment: str = Field(default="", description="Environment tag; empty falls back to settings.environment")
    traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0, description="Performance-trace sampling")
    profiles_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0, description="Profiling sampling")
    debug: bool = Field(default=False, description="SDK debug mode")


class BaseServiceSettings(BaseSettings):
    """Environment-loaded service settings satisfying ``BaseServiceSettingsProtocol``.

    Subclass it: add your own fields, override ``model_config`` (pydantic merges it
    with this one, so ``env_nested_delimiter="__"`` survives an ``env_file`` or
    ``env_prefix`` of yours), narrow a section to a subclass of its model, or
    disable a concern with ``None``::

        class Settings(BaseServiceSettings):
            model_config = SettingsConfigDict(env_file=".env")

            app_version: str = "1.4.0"
            database_dsn: str

            metrics: None = None  # this concern is off
            logging: LoggingSettings = LoggingSettings(level="DEBUG")

    The sections present by default are the inert ones: ``logging`` (INFO, JSON),
    ``metrics`` (sink on, exposition server off) and ``error_tracking`` (no DSN,
    so off). ``tracing`` is ``None`` because a present section installs a tracer
    provider; ``TRACING__COLLECTOR_URL=...`` (any ``TRACING__*`` variable) creates
    it from the environment. A present section activates its backend at
    bootstrap, which needs that backend's extra installed.
    """

    model_config = SettingsConfigDict(env_nested_delimiter="__")

    app_version: str = Field(default="0.0.0", description="Returned by get_app_version()")
    environment: str = Field(default="local", description="Deployment environment, used for observability setup")

    logging: LoggingSettings | None = Field(default_factory=LoggingSettings)
    metrics: MetricsSettings | None = Field(default_factory=MetricsSettings)
    tracing: TracingSettings | None = None
    error_tracking: ErrorTrackingSettings | None = Field(default_factory=ErrorTrackingSettings)

    def get_app_version(self) -> str:
        """Return :attr:`app_version`; override to source the version elsewhere."""
        return self.app_version


__all__ = [
    "BaseServiceSettings",
    "ErrorTrackingSettings",
    "LoggingSettings",
    "MetricsSettings",
    "TracingSettings",
]
