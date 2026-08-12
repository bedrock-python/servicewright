"""Transport-neutral request context: the canonical correlation store.

One unit of work (an HTTP request, an RPC, a job run, a consumed message) binds
its identifiers — ``request_id`` / ``user_id`` / ``trace_id`` / anything else —
into this store; business code reads them back anywhere in the async call tree
without threading parameters through every signature.

The store is a registry of :class:`contextvars.ContextVar` objects keyed by
string, so values are isolated per asyncio task tree exactly like any
contextvar. Transport adapters are the writers (the FastAPI
``ContextMiddleware`` and the gRPC ``UnitScopeInterceptor`` both bind here);
:func:`get_context_value` / :func:`current_context` are the read API.

Bridging values into *external* systems (structlog, OpenTelemetry baggage, a
platform's own contextvars) is the job of :class:`ContextSetter` plugins owned
by the transport adapters — the store itself depends on nothing.
"""

from __future__ import annotations

import contextlib
import re
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

# Registry of context variables, keyed by context key. Created lazily and never
# removed: the set of keys a service uses is small and stable.
_CONTEXT_VARS: dict[str, ContextVar[Any]] = {}

# Correlation identifiers are semi-trusted client input that ends up in logs and
# outbound headers — bound the length and restrict to a log-safe alphabet.
# The pattern is anchored and backtracking-free (ReDoS-safe).
MAX_CONTEXT_ID_LENGTH = 256
_SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9._\-+=/ :]+")

# Default context-key -> outbound-header mapping used by propagation_metadata().
# The same header names the transports extract on the incoming side.
STANDARD_PROPAGATION_HEADERS: dict[str, str] = {
    "request_id": "x-request-id",
    "user_id": "x-user-id",
    "tenant_id": "x-tenant-id",
    "trace_id": "x-trace-id",
}


def is_safe_context_id(value: str) -> bool:
    """True when ``value`` is a well-formed correlation identifier.

    Rejects empty, overlong (> 256 chars) and log-unsafe values (anything
    outside ``A-Za-z0-9 . _ - + = / space :``). Transports use this to filter
    identifiers extracted from headers/metadata before binding them.
    """
    return 0 < len(value) <= MAX_CONTEXT_ID_LENGTH and _SAFE_ID_PATTERN.fullmatch(value) is not None


def propagation_metadata(keys: Mapping[str, str] | None = None) -> dict[str, str]:
    """Collect the current context as outbound headers / gRPC metadata.

    Returns ``{header: value}`` for every mapped context key currently set —
    ready to merge into HTTP request headers or gRPC invocation metadata, e.g.
    as the callable feeding a client context interceptor.

    Args:
        keys: ``{context_key: header_name}`` mapping; ``None`` uses
            :data:`STANDARD_PROPAGATION_HEADERS` (request/user/tenant/trace).
    """
    mapping = STANDARD_PROPAGATION_HEADERS if keys is None else keys
    metadata: dict[str, str] = {}
    for key, header in mapping.items():
        value = get_context_value(key)
        if value is not None:
            metadata[header] = str(value)
    return metadata


class ContextSetter(Protocol):
    """Pushes context values into an external system (logging, tracing, ...).

    Implementations receive the full per-unit context dictionary and return a
    cleanup callable that undoes the binding when the unit of work finishes.
    """

    def set(self, context_data: dict[str, Any]) -> Callable[[], None]:
        """Set the context values and return a cleanup callable that resets them."""
        ...


def get_context_var(key: str) -> ContextVar[Any]:
    """Get or create the :class:`~contextvars.ContextVar` for ``key``."""
    if key not in _CONTEXT_VARS:
        _CONTEXT_VARS[key] = ContextVar(key, default=None)
    return _CONTEXT_VARS[key]


def set_context_value(key: str, value: Any) -> Token[Any]:
    """Set ``key`` in the current context; returns the token for reset."""
    return get_context_var(key).set(value)


def reset_context_value(key: str, token: Token[Any]) -> None:
    """Reset ``key`` to its state before the matching :func:`set_context_value`."""
    get_context_var(key).reset(token)


def get_context_value(key: str, default: Any = None) -> Any:
    """Get ``key`` from the current context (``default`` when unset)."""
    var = _CONTEXT_VARS.get(key)
    return default if var is None else var.get(default)


def current_context() -> dict[str, Any]:
    """Return every non-``None`` value in the current context as a dict."""
    return {key: value for key, var in _CONTEXT_VARS.items() if (value := var.get()) is not None}


def bind_context_values(values: Mapping[str, Any]) -> Callable[[], None]:
    """Bind ``values`` into the store; returns a remover that resets them all.

    ``None`` values are skipped (absent, not bound). The remover resets in
    reverse binding order and is idempotent.
    """
    tokens: list[tuple[str, Token[Any]]] = [
        (key, set_context_value(key, value)) for key, value in values.items() if value is not None
    ]

    def remove() -> None:
        while tokens:
            key, token = tokens.pop()
            reset_context_value(key, token)

    return remove


@contextlib.contextmanager
def bind_context(**values: Any) -> Generator[None, None, None]:
    """Context manager binding ``values`` for the duration of the block."""
    remove = bind_context_values(values)
    try:
        yield
    finally:
        remove()


__all__ = [
    "MAX_CONTEXT_ID_LENGTH",
    "STANDARD_PROPAGATION_HEADERS",
    "ContextSetter",
    "bind_context",
    "bind_context_values",
    "current_context",
    "get_context_value",
    "get_context_var",
    "is_safe_context_id",
    "propagation_metadata",
    "reset_context_value",
    "set_context_value",
]
