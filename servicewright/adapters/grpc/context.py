"""Context integration setters for :class:`UnitScopeInterceptor`.

Per RPC, the interceptor binds ``request_id`` / ``user_id`` / ``trace_id`` into
the transport-neutral :mod:`servicewright.core.context` store and pushes them
into pluggable :class:`~servicewright.core.context.ContextSetter`s so downstream
systems see them too — the gRPC counterpart of what the FastAPI
``ContextMiddleware`` does, so log lines correlate identically on both
transports.

Both built-in setters are SOFT capabilities: ``servicewright[grpc]`` pulls
neither ``structlog`` nor ``opentelemetry-api``, so each activates only when its
package is importable.

- :class:`StructlogSetter` — log-line correlation. Activates when ``structlog``
  is installed (it ships with ``servicewright[observability]``).
- :class:`OtelBaggageSetter` — puts the values into W3C Baggage so
  OpenTelemetry-instrumented clients propagate them to downstream services
  automatically. Activates when ``opentelemetry-api`` is installed.

Custom setters plug in via ``GrpcEntrypoint(context_setters=...)`` — that is the
seam for bridging into a platform's own contextvars package without
servicewright depending on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.context import ContextSetter

if TYPE_CHECKING:
    from collections.abc import Callable

try:  # pragma: no cover - exercised only without structlog installed
    import structlog

    STRUCTLOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    STRUCTLOG_AVAILABLE = False

try:  # pragma: no cover - exercised only without opentelemetry installed
    from opentelemetry import baggage as _otel_baggage
    from opentelemetry import context as _otel_context

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    OTEL_AVAILABLE = False

# The context keys propagated into the setters, in order.
_CONTEXT_KEYS = ("request_id", "user_id", "trace_id")

_STRUCTLOG_HINT = "StructlogSetter requires the 'structlog' package; install servicewright[observability]."
_OTEL_HINT = "OtelBaggageSetter requires the 'opentelemetry-api' package; install servicewright[observability]."


class OtelBaggageRemover:
    """Cleanup callable that detaches the OTel context token it captured."""

    def __init__(self, token: object | None) -> None:
        self._token = token

    def __call__(self) -> None:
        """Detach the attached OTel context (idempotent)."""
        if self._token is not None:
            _otel_context.detach(self._token)
            self._token = None


class OtelBaggageSetter(ContextSetter):
    """Put request/user/trace identifiers into OpenTelemetry Baggage.

    Baggage rides the W3C ``baggage`` header, so OTel-instrumented clients
    (httpx, grpc, kafka) propagate the values to downstream services with no
    custom code. Filter propagators on clients calling third parties if these
    identifiers must not leave your system.

    Raises:
        ImportError: If ``opentelemetry-api`` is not installed.
    """

    def __init__(self) -> None:
        if not OTEL_AVAILABLE:
            raise ImportError(_OTEL_HINT)

    def set(self, context_data: dict[str, Any]) -> Callable[[], None]:
        """Attach one OTel context carrying the values; return the detacher."""
        ctx = _otel_context.get_current()
        dirty = False
        for key in _CONTEXT_KEYS:
            if value := context_data.get(key):
                ctx = _otel_baggage.set_baggage(key, str(value), context=ctx)
                dirty = True

        token = _otel_context.attach(ctx) if dirty else None
        return OtelBaggageRemover(token)


class StructlogRemover:
    """Cleanup callable that unbinds the structlog contextvars it bound."""

    def __init__(self, bound_keys: list[str]) -> None:
        self._bound_keys = bound_keys

    def __call__(self) -> None:
        """Unbind the structlog contextvars this remover bound (idempotent)."""
        if self._bound_keys:
            structlog.contextvars.unbind_contextvars(*self._bound_keys)
            self._bound_keys = []


class StructlogSetter(ContextSetter):
    """Bind request/user/trace identifiers into ``structlog`` contextvars.

    This is what puts ``request_id`` on every log line emitted while the RPC is
    in flight: the structlog sink's processor chain starts with
    ``structlog.contextvars.merge_contextvars``, which reads exactly this store.

    Raises:
        ImportError: If ``structlog`` is not installed.
    """

    def __init__(self) -> None:
        if not STRUCTLOG_AVAILABLE:
            raise ImportError(_STRUCTLOG_HINT)

    def set(self, context_data: dict[str, Any]) -> Callable[[], None]:
        """Bind the contextvars and return a remover."""
        bind_data: dict[str, Any] = {}
        bound_keys: list[str] = []
        for key in _CONTEXT_KEYS:
            if value := context_data.get(key):
                bind_data[key] = value
                bound_keys.append(key)

        if bind_data:
            structlog.contextvars.bind_contextvars(**bind_data)

        return StructlogRemover(bound_keys)


def get_default_context_setters() -> list[ContextSetter]:
    """Return the default context setters for the gRPC transport.

    Adds the structlog setter when ``structlog`` is installed (log correlation
    out of the box) and the OTel Baggage setter when ``opentelemetry-api`` is
    installed (the identifiers propagate to downstream services via
    instrumented clients). Both ship with ``servicewright[observability]``;
    without them the list is empty and the RPC context still lands in the
    transport-neutral store.
    """
    setters: list[ContextSetter] = []
    if OTEL_AVAILABLE:
        setters.append(OtelBaggageSetter())
    if STRUCTLOG_AVAILABLE:
        setters.append(StructlogSetter())
    return setters


__all__ = [
    "OTEL_AVAILABLE",
    "STRUCTLOG_AVAILABLE",
    "OtelBaggageRemover",
    "OtelBaggageSetter",
    "StructlogRemover",
    "StructlogSetter",
    "get_default_context_setters",
]
