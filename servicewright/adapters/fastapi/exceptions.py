"""Default exception handlers rendering through the pluggable error renderer.

Every failure normalizes into a :class:`~servicewright.core.errors.ErrorInfo`,
gets masked when non-public, and renders through the configured
:class:`~servicewright.core.errors.HttpErrorRendererProtocol` — RFC 9457
``application/problem+json`` by default. Swap the renderer to own the wire
format (custom envelope, localized messages) across ALL handlers at once:

- ``RequestValidationError`` -> 422 ``validation_error`` (sanitized errors)
- ``StarletteHTTPException`` -> 5xx masked / 4xx preserved with its detail
- ``ServiceError``           -> status from its kind; non-public masked to 500
- ``DeadlineExceededError``  -> 504 ``deadline_exceeded``
- unhandled ``Exception``    -> masked 500 ``internal_error``
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from deadline_budget import DeadlineExceededError as LibraryDeadlineExceededError
from fastapi.exceptions import RequestValidationError

from ...core.errors import (
    INTERNAL_ERROR_CODE,
    ErrorInfo,
    ErrorKind,
    ProblemDetailsRenderer,
    ServiceError,
    mask_private_error,
)
from ._imports import JSONResponse, StarletteHTTPException, status

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ...core.errors import HttpErrorRendererProtocol
    from ._imports import FastAPI, Request, Response

    # A caller-supplied exception handler: (request, exc) -> Response.
    ExceptionHandler = Callable[[Request, Any], "Response | Awaitable[Response]"]

logger = logging.getLogger(__name__)

# Starlette renamed HTTP_422_UNPROCESSABLE_ENTITY -> _CONTENT; prefer the new
# name when present, fall back on older Starlette.
_HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", None) or status.HTTP_422_UNPROCESSABLE_ENTITY

# Reverse map for wrapping HTTPExceptions into the closest neutral kind.
_KIND_BY_HTTP_STATUS = {
    status.HTTP_400_BAD_REQUEST: ErrorKind.INVALID,
    status.HTTP_401_UNAUTHORIZED: ErrorKind.UNAUTHENTICATED,
    status.HTTP_403_FORBIDDEN: ErrorKind.FORBIDDEN,
    status.HTTP_404_NOT_FOUND: ErrorKind.NOT_FOUND,
    status.HTTP_409_CONFLICT: ErrorKind.CONFLICT,
    status.HTTP_412_PRECONDITION_FAILED: ErrorKind.PRECONDITION_FAILED,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorKind.TOO_MANY_REQUESTS,
}

_MASKED_INTERNAL = ErrorInfo(kind=ErrorKind.INTERNAL, code=INTERNAL_ERROR_CODE)


def _sanitize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the JSON-safe, non-leaking fields of pydantic error dicts."""
    return [{key: error[key] for key in ("type", "loc", "msg") if key in error} for error in errors]


def _to_response(rendered_status: int, body: dict[str, Any], media_type: str, headers: Any = None) -> JSONResponse:
    return JSONResponse(status_code=rendered_status, content=body, media_type=media_type, headers=headers)


def setup_default_exception_handlers(app: FastAPI, *, renderer: HttpErrorRendererProtocol | None = None) -> None:
    """Register the default exception handlers on ``app``.

    Args:
        app: The FastAPI application.
        renderer: The wire-format renderer; defaults to RFC 9457
            :class:`~servicewright.core.errors.ProblemDetailsRenderer`.
    """
    active_renderer: HttpErrorRendererProtocol = renderer if renderer is not None else ProblemDetailsRenderer()

    def _render(info: ErrorInfo) -> JSONResponse:
        rendered = active_renderer.render(mask_private_error(info))
        return _to_response(rendered.status_code, rendered.body, rendered.media_type, rendered.headers)

    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Render Pydantic validation errors as a 422 ``validation_error``."""
        return _render(
            ErrorInfo(
                kind=ErrorKind.INVALID,
                code="validation_error",
                detail="Request validation failed.",
                params={"errors": _sanitize_validation_errors(exc.errors())},
                status_override=_HTTP_422,
            )
        )

    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Render HTTP exceptions: 5xx masked, 4xx preserved with their detail."""
        if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            info = ErrorInfo(
                kind=ErrorKind.INTERNAL,
                code=INTERNAL_ERROR_CODE,
                status_override=exc.status_code,
                headers=exc.headers,
            )
        else:
            info = ErrorInfo(
                kind=_KIND_BY_HTTP_STATUS.get(exc.status_code, ErrorKind.INVALID),
                code="http_error",
                detail=str(exc.detail),
                status_override=exc.status_code,
                headers=exc.headers,
            )
        return _render(info)

    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        """Map :class:`ServiceError` to its kind's status; mask non-public ones."""
        if not exc.public:
            logger.warning(
                "Private service error occurred: %s",
                exc.code,
                extra={"error_code": exc.code, "error_kind": exc.kind, "params": exc.params},
            )
        return _render(ErrorInfo.from_service_error(exc))

    async def deadline_exceeded_handler(request: Request, exc: LibraryDeadlineExceededError) -> JSONResponse:
        """Convert ``deadline_budget.DeadlineExceededError`` into a 504 response."""
        logger.warning(
            "Request deadline exceeded",
            extra={
                "budget_seconds": exc.budget_seconds,
                "elapsed_seconds": exc.elapsed_seconds,
                "path": str(request.url.path),
                "method": request.method,
            },
        )
        return _render(
            ErrorInfo(
                kind=ErrorKind.DEADLINE_EXCEEDED,
                code="deadline_exceeded",
                detail="Request took too long to process.",
                params={"budget_seconds": exc.budget_seconds, "elapsed_seconds": exc.elapsed_seconds},
            )
        )

    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Final fallback: log the traceback and return a masked 500."""
        logger.exception("Unhandled exception while processing request", exc_info=True)
        return _render(_MASKED_INTERNAL)

    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(ServiceError, service_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(LibraryDeadlineExceededError, deadline_exceeded_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = [
    "setup_default_exception_handlers",
]
