"""Built-in gRPC interceptors.

``UnitScopeInterceptor`` makes "one RPC == one UnitScope" structurally true in a
DI-agnostic way: per RPC it opens ``container.unit_scope(context={...})`` and
exposes the scope to the servicer through a :class:`contextvars.ContextVar`,
readable via :func:`current_unit_scope`.

It builds on grpc-server-kit's streaming-aware
:class:`~grpc_server_kit.aio.interceptors.AsyncServerInterceptor` base, so the
scope stays open for the *whole* RPC — including the full response stream of
streaming calls. RPC metrics are recorded by the kit's
``AsyncMetricsInterceptor`` (wired by :class:`GrpcEntrypoint` over the
adapter's :class:`~servicewright.adapters.grpc.metrics.GrpcServerMetricsRecorder`).
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ...core.context import bind_context_values, is_safe_context_id
from ._imports import AsyncServerInterceptor, RpcCall
from .metadata import get_client_context, get_correlation_ids, get_idempotency_key

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    import grpc.aio

    from ...core.context import ContextSetter
    from ...core.contracts import DependencyContainerProtocol, UnitScopeProtocol

logger = logging.getLogger(__name__)

# The per-RPC unit scope, set by UnitScopeInterceptor and read by servicers.
_current_unit_scope: contextvars.ContextVar[UnitScopeProtocol] = contextvars.ContextVar("servicewright_grpc_unit_scope")


def current_unit_scope() -> UnitScopeProtocol:
    """Return the :class:`UnitScopeProtocol` for the in-flight RPC.

    Raises:
        LookupError: If called outside an RPC handled by
            :class:`UnitScopeInterceptor`.
    """
    try:
        return _current_unit_scope.get()
    except LookupError as exc:
        raise LookupError(
            "No active gRPC unit scope; current_unit_scope() must be called inside an RPC "
            "served by a GrpcEntrypoint (UnitScopeInterceptor is added automatically)."
        ) from exc


class UnitScopeInterceptor(AsyncServerInterceptor):
    """Open one ``UnitScope`` per RPC and expose it via a context variable.

    This is added first to the interceptor chain by :class:`GrpcEntrypoint`, so
    every downstream interceptor and the servicer see a live unit scope. The
    scope carries the RPC payload (method name, correlation ids, idempotency
    key, client ip / user agent) as its ``context``, mirrored into the core
    context store. Correlation ids come from ``x-request-id`` / ``x-user-id`` /
    ``x-tenant-id`` / ``x-trace-id`` metadata (like the HTTP middleware), with
    the request id auto-generated when the client sent none.

    The same values are pushed into the supplied
    :class:`~servicewright.core.context.ContextSetter`s, which is what bridges
    them into systems that keep their OWN store — structlog contextvars (so
    every log line emitted during the RPC carries ``request_id``) and OTel
    Baggage. :class:`GrpcEntrypoint` installs
    :func:`~servicewright.adapters.grpc.context.get_default_context_setters` by
    default; a setter raising is logged and never fails the RPC.

    Args:
        container: The DI container the per-RPC unit scope is opened on.
        context_setters: Bridges pushing the RPC context into external systems.
    """

    def __init__(
        self,
        container: DependencyContainerProtocol,
        *,
        context_setters: Sequence[ContextSetter] = (),
    ) -> None:
        super().__init__()
        self._container = container
        self._context_setters = tuple(context_setters)

    async def around_call(self, call: RpcCall) -> AsyncIterator[None]:
        """Wrap the RPC in a fresh unit scope bound to a context variable."""
        unit_context = self._build_context(call.context, call.method_name)
        # Mirror the RPC payload into the transport-neutral core context store
        # so business code reads it the same way as under any other transport.
        remove_context = bind_context_values(unit_context)
        removers = self._apply_setters(unit_context)
        try:
            async with self._container.unit_scope(unit_context) as scope:
                token = _current_unit_scope.set(scope)
                try:
                    yield
                finally:
                    _current_unit_scope.reset(token)
        finally:
            self._undo_setters(removers)
            remove_context()

    def _apply_setters(self, unit_context: dict[str, Any]) -> list[Callable[[], None]]:
        """Push the RPC context into every setter; return their cleanups."""
        removers: list[Callable[[], None]] = []
        for setter in self._context_setters:
            try:
                remover = setter.set(unit_context)
            except Exception:
                logger.exception("Failed to call gRPC context setter")
                continue
            if remover is not None:
                removers.append(remover)
        return removers

    @staticmethod
    def _undo_setters(removers: list[Callable[[], None]]) -> None:
        """Run the setters' cleanups in reverse order, never raising."""
        for remover in reversed(removers):
            try:
                remover()
            except Exception:
                logger.exception("Failed to call gRPC context cleanup remover")

    @staticmethod
    def _build_context(context: grpc.aio.ServicerContext, method_name: str) -> dict[str, Any]:
        client_ip, user_agent = get_client_context(context)
        idempotency_key = get_idempotency_key(context)
        if idempotency_key is not None and not is_safe_context_id(idempotency_key):
            idempotency_key = None

        unit_context: dict[str, Any] = {
            "grpc_method": method_name,
            "idempotency_key": idempotency_key,
            "client_ip": client_ip,
            "user_agent": user_agent,
        }
        unit_context.update(get_correlation_ids(context))
        if "request_id" not in unit_context:
            unit_context["request_id"] = str(uuid.uuid4())
        return unit_context


__all__ = [
    "UnitScopeInterceptor",
    "current_unit_scope",
]
