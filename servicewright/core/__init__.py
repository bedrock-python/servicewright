"""Core framework-agnostic microservice runtime abstractions."""

from __future__ import annotations

from .aio import Host
from .constants import (
    DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_DRAIN_GRACE_SECONDS,
)
from .context import (
    STANDARD_PROPAGATION_HEADERS,
    ContextSetter,
    bind_context,
    bind_context_values,
    current_context,
    get_context_value,
    is_safe_context_id,
    propagation_metadata,
    set_context_value,
)
from .contracts import (
    AppScopeProtocol,
    AsyncWarmer,
    BaseServiceSettingsProtocol,
    DependencyContainerProtocol,
    HealthCheckerProtocol,
    LifecycleHookProtocol,
    UnitScopeProtocol,
)
from .errors import (
    ErrorInfo,
    ErrorKind,
    HttpErrorRendererProtocol,
    ProblemDetailsRenderer,
    RenderedError,
    ServiceError,
    mask_private_error,
)
from .exceptions import (
    CleanupTimeoutError,
    DrainTimeoutError,
    KafkaProducerWarmupError,
    PostgresWarmupError,
    RedisWarmupError,
    ServiceWrightError,
    WarmupError,
    WarmupTimeoutError,
)
from .health import HealthRegistry, HealthReport, ProbeStatus
from .lifecycle import Lifecycle
from .observability import (
    ErrorTrackingSettingsProtocol,
    KeyRedactor,
    LoggingSettingsProtocol,
    MetricsSettingsProtocol,
    ObsConfig,
    ObservabilityManager,
    ObsSetupContext,
    TracingSettingsProtocol,
    register_sink,
)
from .service import Service, run
from .signals import install_signal_handlers
from .spec import AppSpec, BootstrapContext, ServiceContext
from .warmup import collect_warmers, perform_warmup, warmup_async

__all__ = [
    "DEFAULT_CLEANUP_TIMEOUT_SECONDS",
    "DEFAULT_DRAIN_GRACE_SECONDS",
    "STANDARD_PROPAGATION_HEADERS",
    "AppScopeProtocol",
    "AppSpec",
    "AsyncWarmer",
    "BaseServiceSettingsProtocol",
    "BootstrapContext",
    "CleanupTimeoutError",
    "ContextSetter",
    "DependencyContainerProtocol",
    "DrainTimeoutError",
    "ErrorInfo",
    "ErrorKind",
    "ErrorTrackingSettingsProtocol",
    "HealthCheckerProtocol",
    "HealthRegistry",
    "HealthReport",
    "Host",
    "HttpErrorRendererProtocol",
    "KafkaProducerWarmupError",
    "KeyRedactor",
    "Lifecycle",
    "LifecycleHookProtocol",
    "LoggingSettingsProtocol",
    "MetricsSettingsProtocol",
    "ObsConfig",
    "ObsSetupContext",
    "ObservabilityManager",
    "PostgresWarmupError",
    "ProbeStatus",
    "ProblemDetailsRenderer",
    "RedisWarmupError",
    "RenderedError",
    "Service",
    "ServiceContext",
    "ServiceError",
    "ServiceWrightError",
    "TracingSettingsProtocol",
    "UnitScopeProtocol",
    "WarmupError",
    "WarmupTimeoutError",
    "bind_context",
    "bind_context_values",
    "collect_warmers",
    "current_context",
    "get_context_value",
    "install_signal_handlers",
    "is_safe_context_id",
    "mask_private_error",
    "perform_warmup",
    "propagation_metadata",
    "register_sink",
    "run",
    "set_context_value",
    "warmup_async",
]
