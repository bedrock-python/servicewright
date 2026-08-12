"""Transport-neutral error taxonomy and the pluggable error-rendering seam.

The problem this solves: in a multi-entrypoint runtime one business error must
render correctly over ANY transport — 404 over HTTP, ``NOT_FOUND`` over gRPC —
with a single masking rule for non-public details. The design has three
pieces, each replaceable:

- :class:`ServiceError` — the exception business code raises. Subclass it per
  error (``class UserMissingError(ServiceError)`` with ``kind``/``code`` class
  attributes) or raise it directly with arguments.
- :class:`ErrorInfo` — the normalized view of *any* failure (a ``ServiceError``,
  a validation error, an unhandled exception). Transport handlers build it,
  apply :func:`mask_private_error`, and hand it to a renderer.
- :class:`HttpErrorRendererProtocol` — turns an :class:`ErrorInfo` into a wire
  response. The default is :class:`ProblemDetailsRenderer` (RFC 9457
  ``application/problem+json``); products with their own wire contract (a
  custom envelope, localized message catalogs) plug in their own renderer and
  every default handler speaks their format automatically.

This module is pure stdlib: transports map :class:`ErrorKind` to their own
status vocabulary (the FastAPI adapter uses :data:`HTTP_STATUS_BY_KIND`; the
gRPC adapter maps kinds to ``grpc.StatusCode``).
"""

from __future__ import annotations

import enum
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from types import MappingProxyType
from typing import Any, Protocol


class ErrorKind(enum.StrEnum):
    """Transport-neutral failure category (maps to HTTP and gRPC statuses)."""

    INVALID = "invalid"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PRECONDITION_FAILED = "precondition_failed"
    TOO_MANY_REQUESTS = "too_many_requests"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    UNAVAILABLE = "unavailable"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL = "internal"


HTTP_STATUS_BY_KIND: dict[ErrorKind, int] = {
    ErrorKind.INVALID: 400,
    ErrorKind.UNAUTHENTICATED: 401,
    ErrorKind.FORBIDDEN: 403,
    ErrorKind.NOT_FOUND: 404,
    ErrorKind.CONFLICT: 409,
    ErrorKind.PRECONDITION_FAILED: 412,
    ErrorKind.TOO_MANY_REQUESTS: 429,
    ErrorKind.DEADLINE_EXCEEDED: 504,
    ErrorKind.UNAVAILABLE: 503,
    ErrorKind.NOT_IMPLEMENTED: 501,
    ErrorKind.INTERNAL: 500,
}

# The code every masked (non-public) error renders as.
INTERNAL_ERROR_CODE = "internal_error"

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _derive_code(class_name: str) -> str:
    """Derive a machine code from a class name: ``UserMissingError`` -> ``user_missing``."""
    stripped = class_name.lstrip("_")
    name = stripped.removesuffix("Error") or stripped or class_name
    return _CAMEL_BOUNDARY.sub("_", name).lower()


class ServiceError(Exception):
    """Base class for typed business errors raised by application code.

    Subclasses set ``kind`` (and optionally ``code``) as class attributes; the
    code defaults to the snake_cased class name without the ``Error`` suffix.
    Every attribute can also be overridden per-instance via keyword arguments,
    and the resolved value is what the instance reports — ``exc.code``,
    ``exc.kind`` and ``exc.public`` are always the values the transports act on,
    so ``except ServiceError as exc: if exc.code == "user_missing"`` works.

    Args:
        detail: Human-readable message shown to the client when the error is
            public (falls back to a generic phrase for the kind).
        code: Machine-readable error code (stable API for clients).
        kind: The failure category driving the transport status.
        params: Structured details for the client. Values that are not JSON
            primitives are coerced by the renderer, never dropped.
        public: When ``False`` the transports mask everything: the client sees
            a generic internal error and the real code is only logged.
    """

    # Declared as plain class attributes (not ClassVar): __init__ shadows them
    # with the resolved per-instance values, so both views always agree.
    kind: ErrorKind = ErrorKind.INTERNAL
    code: str | None = None
    public: bool = True
    detail: str | None = None
    params: Mapping[str, Any] = MappingProxyType({})

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        kind: ErrorKind | None = None,
        params: Mapping[str, Any] | None = None,
        public: bool | None = None,
    ) -> None:
        cls = type(self)
        resolved_code = code if code is not None else (cls.code or _derive_code(cls.__name__))
        super().__init__(detail if detail is not None else resolved_code)
        self.detail = detail
        self.code = resolved_code
        self.kind = kind if kind is not None else cls.kind
        self.params = dict(params) if params else {}
        self.public = public if public is not None else cls.public


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Normalized view of one failure, ready for rendering.

    Attributes:
        kind: The failure category.
        code: Machine-readable error code.
        detail: Human-readable message (``None`` -> the renderer supplies a
            generic phrase).
        params: Structured, JSON-safe details.
        public: Whether the details may be shown to the client.
        status_override: Explicit HTTP status taking precedence over the kind's
            default (e.g. 422 for request validation).
        headers: Extra response headers (e.g. from an ``HTTPException``).
    """

    kind: ErrorKind
    code: str
    detail: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    public: bool = True
    status_override: int | None = None
    headers: Mapping[str, str] | None = None

    @classmethod
    def from_service_error(cls, exc: ServiceError) -> ErrorInfo:
        """Build the normalized view of a :class:`ServiceError`."""
        return cls(
            kind=exc.kind,
            code=exc.code or INTERNAL_ERROR_CODE,
            detail=exc.detail,
            params=exc.params,
            public=exc.public,
        )

    @property
    def http_status(self) -> int:
        """The HTTP status: the override if set, else the kind's default."""
        return self.status_override if self.status_override is not None else HTTP_STATUS_BY_KIND[self.kind]


def mask_private_error(info: ErrorInfo) -> ErrorInfo:
    """Return the client-safe view: non-public errors collapse to a generic 500.

    Public errors pass through unchanged. The caller is responsible for logging
    the original (that is where the real code/params must go).
    """
    if info.public:
        return info
    return ErrorInfo(kind=ErrorKind.INTERNAL, code=INTERNAL_ERROR_CODE, public=True)


@dataclass(frozen=True, slots=True)
class RenderedError:
    """A rendered wire response for one failure."""

    status_code: int
    body: dict[str, Any]
    media_type: str = "application/problem+json"
    headers: Mapping[str, str] | None = None


class HttpErrorRendererProtocol(Protocol):
    """Turns a normalized :class:`ErrorInfo` into a wire response.

    Implement this to own the error wire format end to end — a custom envelope,
    localized messages resolved from ``info.code`` + ``info.params``, extra
    fields. Every default exception handler renders through the configured
    renderer, so one implementation switches the whole surface.
    """

    def render(self, info: ErrorInfo) -> RenderedError:
        """Render one failure (already masked by the caller when non-public)."""
        ...


def status_title(status: int) -> str:
    """Return a human-readable title for any HTTP status, registered or not.

    ``http.HTTPStatus`` only knows IANA-registered codes, but services really do
    return 499 (client closed request) or the 52x Cloudflare range. Rendering an
    error must never fail because of the status it is reporting, so unknown
    codes fall back to their class.
    """
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        if 400 <= status < 500:
            return "Client Error"
        if 500 <= status < 600:
            return "Server Error"
        return "Error"


def to_json_safe(value: Any, _seen: frozenset[int] = frozenset()) -> Any:
    """Coerce any value into something :mod:`json` can serialize.

    Total by construction: containers are walked, non-finite floats and unknown
    objects become strings, and a self-referencing structure is cut with a
    marker instead of recursing forever. Rendering an error is the last chance
    the client has to learn what went wrong — it may never be the thing that
    fails, which is what happens when a ``UUID`` reaches ``json.dumps``.
    """
    if isinstance(value, enum.Enum):
        # StrEnum/IntEnum members are str/int instances, so this must come first.
        return to_json_safe(value.value, _seen)
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        # json.dumps(allow_nan=False) — Starlette's default — rejects these.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        if id(value) in _seen:
            return "<recursive>"
        seen = _seen | {id(value)}
        return {str(key): to_json_safe(item, seen) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        if id(value) in _seen:
            return "<recursive>"
        seen = _seen | {id(value)}
        return [to_json_safe(item, seen) for item in value]
    return str(value)


class ProblemDetailsRenderer:
    """The default renderer: RFC 9457 Problem Details (``application/problem+json``).

    Body: ``type`` (a URI built from ``type_base`` and the code, or
    ``about:blank``), ``title`` (the HTTP reason phrase), ``status``,
    ``detail`` (when present) plus the extension members ``code`` and
    ``params`` (when non-empty).

    Rendering is total: ``params`` are coerced with :func:`to_json_safe` (a
    ``UUID`` or ``datetime`` renders as its string form rather than turning the
    intended 404 into a masked 500) and the title tolerates non-IANA statuses.

    Args:
        type_base: Base URI for the ``type`` member; ``None`` renders
            ``about:blank`` per the RFC default.
    """

    def __init__(self, *, type_base: str | None = None) -> None:
        self._type_base = type_base.rstrip("/") if type_base else None

    def render(self, info: ErrorInfo) -> RenderedError:
        """Render the failure as an RFC 9457 problem document."""
        status = info.http_status
        body: dict[str, Any] = {
            "type": f"{self._type_base}/{info.code}" if self._type_base else "about:blank",
            "title": status_title(status),
            "status": status,
            "code": info.code,
        }
        if info.detail:
            body["detail"] = info.detail
        if info.params:
            body["params"] = to_json_safe(dict(info.params))
        return RenderedError(status_code=status, body=body, headers=info.headers)


__all__ = [
    "HTTP_STATUS_BY_KIND",
    "INTERNAL_ERROR_CODE",
    "ErrorInfo",
    "ErrorKind",
    "HttpErrorRendererProtocol",
    "ProblemDetailsRenderer",
    "RenderedError",
    "ServiceError",
    "mask_private_error",
    "status_title",
    "to_json_safe",
]
