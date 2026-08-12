"""gRPC entrypoint adapter (``[grpc]`` extra)."""

from __future__ import annotations

from .config import GrpcConfig
from .entrypoint import GrpcEntrypoint, GrpcPlugin, InterceptorFactory, ServicerRegisterer
from .errors import ERROR_CODE_TRAILING_METADATA, GRPC_STATUS_BY_KIND, ServiceErrorInterceptor
from .health import GrpcHealthBridge
from .interceptors import UnitScopeInterceptor, current_unit_scope
from .metadata import (
    IDEMPOTENCY_KEY_METADATA,
    get_client_context,
    get_client_ip,
    get_idempotency_key,
    get_user_agent,
)
from .metrics import GrpcServerMetricsRecorder

__all__ = [
    "ERROR_CODE_TRAILING_METADATA",
    "GRPC_STATUS_BY_KIND",
    "IDEMPOTENCY_KEY_METADATA",
    "GrpcConfig",
    "GrpcEntrypoint",
    "GrpcHealthBridge",
    "GrpcPlugin",
    "GrpcServerMetricsRecorder",
    "InterceptorFactory",
    "ServiceErrorInterceptor",
    "ServicerRegisterer",
    "UnitScopeInterceptor",
    "current_unit_scope",
    "get_client_context",
    "get_client_ip",
    "get_idempotency_key",
    "get_user_agent",
]
