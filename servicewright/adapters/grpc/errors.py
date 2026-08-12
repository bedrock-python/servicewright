"""ServiceError -> gRPC status mapping: the transport-neutral errors over gRPC.

The same :class:`~servicewright.core.errors.ServiceError` a use case raises for
HTTP renders here as the matching ``grpc.StatusCode``: the interceptor catches
it, maps its kind, and aborts the RPC. Non-public errors are masked exactly
like over HTTP — the client sees a generic ``INTERNAL`` and the real code is
only logged. The machine-readable code travels in the ``x-error-code``
trailing metadata so clients can branch without parsing messages.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...core.errors import ErrorInfo, ErrorKind, ServiceError, mask_private_error
from ._imports import AsyncServerInterceptor, grpc

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ._imports import RpcCall

logger = logging.getLogger(__name__)

# Transport mapping: the neutral failure category -> the gRPC status.
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


class ServiceErrorInterceptor(AsyncServerInterceptor):
    """Abort RPCs failing with :class:`ServiceError` using the mapped status.

    Added automatically by :class:`GrpcEntrypoint` (inside the metrics
    interceptor, so aborts are recorded with their real status). Any other
    exception passes through untouched — compose grpc-server-kit's
    ``AsyncExceptionHandlerInterceptor`` for generic exception mapping.
    """

    async def around_call(self, call: RpcCall) -> AsyncIterator[None]:
        """Convert a raised ``ServiceError`` into a mapped gRPC abort."""
        try:
            yield
        except ServiceError as exc:
            await self._abort(call, exc)

    async def _abort(self, call: RpcCall, exc: ServiceError) -> None:
        if not exc.public:
            logger.warning(
                "Private service error occurred: %s",
                exc.code,
                extra={"error_code": exc.code, "error_kind": exc.kind, "params": exc.params},
            )
        info = mask_private_error(ErrorInfo.from_service_error(exc))
        status = GRPC_STATUS_BY_KIND[info.kind]

        # abort() raises grpc.aio.AbortError and never returns.
        await call.context.abort(
            status,
            info.detail or info.code,
            trailing_metadata=((ERROR_CODE_TRAILING_METADATA, info.code),),
        )


__all__ = [
    "ERROR_CODE_TRAILING_METADATA",
    "GRPC_STATUS_BY_KIND",
    "ServiceErrorInterceptor",
]
