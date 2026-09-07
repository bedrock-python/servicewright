"""ServiceError -> gRPC status mapping: the transport-neutral errors over gRPC.

The same :class:`~servicewright.core.errors.ServiceError` a use case raises for
HTTP renders here as the matching ``grpc.StatusCode``: the interceptor catches
it, maps its kind, and aborts the RPC. Non-public errors are masked exactly
like over HTTP — the client sees a generic ``INTERNAL`` and the real code is
only logged. The machine-readable code travels in the ``x-error-code``
trailing metadata so clients can branch without parsing messages.

An exception that is *not* a ``ServiceError`` is masked the same way by
:class:`UnhandledErrorInterceptor`, the counterpart of the HTTP stack's
``UnhandledErrorMiddleware``. Without it ``grpc.aio`` answers such an exception
with ``UNKNOWN`` and ``repr`` of it, so the one error class nobody vetted the
wording of is the one that reaches the caller verbatim.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...core.errors import INTERNAL_ERROR_CODE, ErrorInfo, ErrorKind, ServiceError, mask_private_error
from ._imports import AsyncServerInterceptor, grpc

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ._imports import RpcCall

logger = logging.getLogger(__name__)

# Transport mapping: the neutral failure category -> the gRPC status.
#
# Injective on purpose, so a status maps back to exactly one kind. CONFLICT is
# the only entry with a real alternative: ABORTED also renders as 409, but the
# gRPC contract tells clients to retry it at a higher level, which is wrong for
# a state conflict that will never succeed on a retry. FAILED_PRECONDITION is
# spoken for by PRECONDITION_FAILED. Clients needing to tell a duplicate key
# from a state conflict branch on the x-error-code metadata, not on the status.
GRPC_STATUS_BY_KIND: dict[ErrorKind, grpc.StatusCode] = {
    ErrorKind.INVALID: grpc.StatusCode.INVALID_ARGUMENT,
    ErrorKind.UNAUTHENTICATED: grpc.StatusCode.UNAUTHENTICATED,
    ErrorKind.FORBIDDEN: grpc.StatusCode.PERMISSION_DENIED,
    ErrorKind.NOT_FOUND: grpc.StatusCode.NOT_FOUND,
    ErrorKind.CONFLICT: grpc.StatusCode.ALREADY_EXISTS,
    ErrorKind.PRECONDITION_FAILED: grpc.StatusCode.FAILED_PRECONDITION,
    ErrorKind.TOO_MANY_REQUESTS: grpc.StatusCode.RESOURCE_EXHAUSTED,
    ErrorKind.DEADLINE_EXCEEDED: grpc.StatusCode.DEADLINE_EXCEEDED,
    ErrorKind.UNAVAILABLE: grpc.StatusCode.UNAVAILABLE,
    ErrorKind.NOT_IMPLEMENTED: grpc.StatusCode.UNIMPLEMENTED,
    ErrorKind.INTERNAL: grpc.StatusCode.INTERNAL,
}

ERROR_CODE_TRAILING_METADATA = "x-error-code"

_MASKED_INTERNAL = ErrorInfo(kind=ErrorKind.INTERNAL, code=INTERNAL_ERROR_CODE)


async def _abort(call: RpcCall, info: ErrorInfo) -> None:
    """Abort the RPC with the kind's status and the code in trailing metadata."""
    # abort() raises grpc.aio.AbortError and never returns.
    await call.context.abort(
        GRPC_STATUS_BY_KIND[info.kind],
        info.detail or info.code,
        trailing_metadata=((ERROR_CODE_TRAILING_METADATA, info.code),),
    )


class ServiceErrorInterceptor(AsyncServerInterceptor):
    """Abort RPCs failing with :class:`ServiceError` using the mapped status.

    Added automatically by :class:`GrpcEntrypoint` as the innermost
    interceptor, so a domain error becomes a mapped abort before any
    interceptor of yours sees it. Any other exception passes through untouched,
    to your own interceptors first and to :class:`UnhandledErrorInterceptor`
    last.
    """

    async def around_call(self, call: RpcCall) -> AsyncIterator[None]:
        """Convert a raised ``ServiceError`` into a mapped gRPC abort."""
        try:
            yield
        except ServiceError as exc:
            if not exc.public:
                logger.warning(
                    "Private service error occurred: %s",
                    exc.code,
                    extra={"error_code": exc.code, "error_kind": exc.kind, "params": exc.params},
                )
            await _abort(call, mask_private_error(ErrorInfo.from_service_error(exc)))


class UnhandledErrorInterceptor(AsyncServerInterceptor):
    """Abort with a masked ``INTERNAL`` when an RPC fails with anything else.

    The last resort, and the gRPC counterpart of the HTTP stack's
    ``UnhandledErrorMiddleware``: ``grpc.aio`` answers an exception it was never
    told about with ``UNKNOWN`` and ``repr`` of the exception, which puts a
    message nobody wrote for a client — a DSN, a row of a query — on the wire.
    Here the client gets exactly what a ``public=False``
    :class:`~servicewright.core.errors.ServiceError` produces (``INTERNAL``,
    detail ``internal_error``, ``x-error-code: internal_error``) and the real
    exception goes to the log with its traceback.

    :class:`GrpcEntrypoint` adds it inside :class:`UnitScopeInterceptor` — so
    the log record still carries the correlation ids — and outside your own
    interceptors, so an exception type you map yourself reaches your
    interceptor first and never gets here. A deliberate status passes through:
    ``grpc.aio.AbortError`` and ``grpc.RpcError`` are re-raised, and
    ``asyncio.CancelledError`` is a ``BaseException``, so a caller that walks
    away is not an error to report.
    """

    async def around_call(self, call: RpcCall) -> AsyncIterator[None]:
        """Run the RPC; mask anything that comes out of it without a status."""
        try:
            yield
        except (grpc.aio.AbortError, grpc.RpcError):
            raise
        except Exception:
            logger.exception("Unhandled exception while serving RPC", extra={"grpc_method": call.method_name})
            await _abort(call, mask_private_error(_MASKED_INTERNAL))


__all__ = [
    "ERROR_CODE_TRAILING_METADATA",
    "GRPC_STATUS_BY_KIND",
    "ServiceErrorInterceptor",
    "UnhandledErrorInterceptor",
]
