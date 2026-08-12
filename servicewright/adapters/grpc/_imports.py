"""Lazy import guard for the optional gRPC stack.

Importing this module fails with a friendly message when the ``grpc`` extra is
not installed, so ``import servicewright`` never pays the cost (or the failure)
of the gRPC dependencies. Every gRPC submodule imports its third-party symbols
from here.
"""

from __future__ import annotations

_INSTALL_HINT = "gRPC support requires servicewright[grpc]; install it."

try:
    import grpc
    import grpc.aio
    from grpc_health.v1 import health_pb2, health_pb2_grpc
    from grpc_health.v1.health import aio as health_aio
    from grpc_server_kit import bind_server_port
    from grpc_server_kit.aio import AsyncServer, create_async_grpc_server
    from grpc_server_kit.aio.interceptors import AsyncMetricsInterceptor, AsyncServerInterceptor, RpcCall
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(_INSTALL_HINT) from exc

__all__ = [
    "AsyncMetricsInterceptor",
    "AsyncServer",
    "AsyncServerInterceptor",
    "RpcCall",
    "bind_server_port",
    "create_async_grpc_server",
    "grpc",
    "health_aio",
    "health_pb2",
    "health_pb2_grpc",
]
