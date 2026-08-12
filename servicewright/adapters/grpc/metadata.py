"""Helpers for extracting client context from gRPC invocation metadata.

Ported verbatim (behaviour-preserving) from the gRPC service-runtime prototype.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.context import is_safe_context_id

if TYPE_CHECKING:
    import grpc.aio

IDEMPOTENCY_KEY_METADATA = "x-idempotency-key"
USER_AGENT_METADATA = "user-agent"
X_USER_AGENT_METADATA = "x-user-agent"

REQUEST_ID_METADATA = "x-request-id"
USER_ID_METADATA = "x-user-id"
TENANT_ID_METADATA = "x-tenant-id"
TRACE_ID_METADATA = "x-trace-id"

_X_FORWARDED_FOR = "x-forwarded-for"
_X_REAL_IP = "x-real-ip"

# Correlation metadata -> context-key mapping (mirrors the HTTP middleware).
_CORRELATION_METADATA_KEYS: tuple[tuple[str, str], ...] = (
    (REQUEST_ID_METADATA, "request_id"),
    (USER_ID_METADATA, "user_id"),
    (TENANT_ID_METADATA, "tenant_id"),
    (TRACE_ID_METADATA, "trace_id"),
)


def _decode(value: str | bytes) -> str:
    """Return a ``str`` for a metadata value that may be ``bytes``."""
    return value if isinstance(value, str) else value.decode("utf-8")


def get_idempotency_key(context: grpc.aio.ServicerContext) -> str | None:
    """Extract the idempotency key from gRPC metadata (case-insensitive)."""
    metadata = context.invocation_metadata()
    if not metadata:
        return None

    for key, value in metadata:
        if key.lower() == IDEMPOTENCY_KEY_METADATA:
            return _decode(value)

    return None


def get_client_ip(context: grpc.aio.ServicerContext) -> str | None:
    """Extract the client IP from gRPC metadata.

    Looks for ``x-forwarded-for`` first (proxy / load balancer), then
    ``x-real-ip`` (nginx / other reverse proxies).
    """
    metadata = context.invocation_metadata()
    if not metadata:
        return None

    metadata_dict = {key.lower(): value for key, value in metadata}
    forwarded = metadata_dict.get(_X_FORWARDED_FOR)
    real_ip = metadata_dict.get(_X_REAL_IP)
    chosen = forwarded or real_ip
    return _decode(chosen) if chosen is not None else None


def get_user_agent(context: grpc.aio.ServicerContext) -> str | None:
    """Extract the user agent from gRPC metadata.

    Prefers the custom ``x-user-agent`` header (to avoid the gRPC-reserved
    ``user-agent`` header), falling back to ``user-agent``.
    """
    metadata = context.invocation_metadata()
    if not metadata:
        return None

    user_agent: str | None = None
    for key, value in metadata:
        key_lower = key.lower()
        if key_lower == X_USER_AGENT_METADATA:
            return _decode(value)
        if key_lower == USER_AGENT_METADATA and user_agent is None:
            user_agent = _decode(value)

    return user_agent


def get_client_context(context: grpc.aio.ServicerContext) -> tuple[str | None, str | None]:
    """Return ``(client_ip, user_agent)`` extracted from gRPC metadata."""
    return get_client_ip(context), get_user_agent(context)


def get_correlation_ids(context: grpc.aio.ServicerContext) -> dict[str, str]:
    """Extract correlation identifiers from gRPC metadata (mirrors the HTTP middleware).

    Reads ``x-request-id`` / ``x-user-id`` / ``x-tenant-id`` / ``x-trace-id``
    (case-insensitive); malformed values (log-unsafe or overlong) are dropped.
    """
    metadata = context.invocation_metadata()
    if not metadata:
        return {}

    metadata_dict = {key.lower(): value for key, value in metadata}
    correlation: dict[str, str] = {}
    for header, ctx_key in _CORRELATION_METADATA_KEYS:
        raw = metadata_dict.get(header)
        if raw is None:
            continue
        value = _decode(raw)
        if is_safe_context_id(value):
            correlation[ctx_key] = value
    return correlation


__all__ = [
    "IDEMPOTENCY_KEY_METADATA",
    "REQUEST_ID_METADATA",
    "TENANT_ID_METADATA",
    "TRACE_ID_METADATA",
    "USER_AGENT_METADATA",
    "USER_ID_METADATA",
    "X_USER_AGENT_METADATA",
    "get_client_context",
    "get_client_ip",
    "get_correlation_ids",
    "get_idempotency_key",
    "get_user_agent",
]
