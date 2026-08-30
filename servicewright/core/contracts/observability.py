"""Observability add-on seam protocols (the pluggable add-ons contract surface).

Pure, SDK-free protocols that an emitter (a gRPC interceptor / an ASGI
middleware) calls without knowing the concrete backend. The names are
vendor-neutral AND transport-neutral — the kernel never names an implementation
or a transport. These protocols are servicewright's source of truth; satellite
libraries (grpc-server-kit and friends) are adapted to fit them.

The author-facing ``*Sink`` ABCs (which carry SDK-touching methods such as
``mount(app)`` / ``tracer()`` / ``setup()``) live in the adapter layer
(``servicewright.adapters.observability``), NOT here, so this module stays free of
``Any``-typed SDK escape hatches and importable with zero extras installed.

Metrics deliberately expose generic *instruments* (counter / histogram / gauge)
rather than per-use-case recorders: each transport adapter composes its own recorder
(and owns its frozen metric names) from these instruments, so a new transport
or a new backend never touches the kernel.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, runtime_checkable

type SpanAttributeValue = str | int | float | bool | list[str] | list[int] | list[float] | list[bool]


@runtime_checkable
class SpanProtocol(Protocol):
    """A tracing span context manager (grpc-server-kit's exact seam)."""

    def set_attribute(self, key: str, value: SpanAttributeValue) -> None:
        """Attach a typed attribute to the span."""
        ...

    def record_exception(self, exception: Exception) -> None:
        """Record an exception against the span."""
        ...

    def __enter__(self) -> SpanProtocol: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class TracerProtocol(Protocol):
    """Mints spans for the active trace (grpc-server-kit's exact seam)."""

    def start_as_current_span(self, name: str) -> SpanProtocol:
        """Start a span and make it current for its ``with`` block."""
        ...


class CounterProtocol(Protocol):
    """A monotonically increasing metric instrument."""

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """Increment by ``amount`` for the given label values."""
        ...


class HistogramProtocol(Protocol):
    """A distribution-sampling metric instrument."""

    def observe(self, value: float, **labels: str) -> None:
        """Record one observation for the given label values."""
        ...


class GaugeProtocol(Protocol):
    """A metric instrument holding a value that goes up and down.

    The instrument for anything that is a *level* rather than a rate — runs in
    progress, queue depth, the timestamp of the last success.
    """

    def set(self, value: float, **labels: str) -> None:
        """Set the value for the given label values."""
        ...

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """Increment by ``amount`` for the given label values."""
        ...

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        """Decrement by ``amount`` for the given label values."""
        ...


class ErrorReporterProtocol(Protocol):
    """Error-reporting seam (vendor-neutral; any tracker with this shape fits).

    grpc-server-kit's ``SentryProtocol`` objects satisfy this structurally —
    protocols match by shape, not by name.
    """

    def capture_exception(self, error: Exception) -> None:
        """Report an exception to the error tracker."""
        ...

    def add_breadcrumb(
        self,
        message: str,
        category: str = "default",
        level: str = "info",
        data: dict[str, object] | None = None,
    ) -> None:
        """Attach a breadcrumb to the current scope."""
        ...

    def set_tags(self, **tags: str) -> None:
        """Set tags on the current scope."""
        ...


class Redactor(Protocol):
    """Cross-cutting sensitive-data filter threaded into logging, error tracking and tracing."""

    def __call__(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return a redacted copy of ``data``."""
        ...


class Masker(Protocol):
    """Value-level masker for a single string (the PII scrubbing seam).

    Where a :class:`Redactor` decides by *key* ("this field is named password"),
    a masker looks at the *value* itself - the email inside a free-form message,
    the card number in a path. Any ``(str) -> str`` callable qualifies: a regex
    substitution, a format-preserving masker, a locally hosted NER model. Lift
    one over whole payloads with
    :class:`~servicewright.core.observability.redaction.ValueRedactor`.

    Contract: synchronous; runs once per string value on the surface it is
    attached to. Pick the surface to match the cost - a regex masker is fine on
    every log line, an ML model belongs on the error path, where volume is
    lowest and payloads leak the most.
    """

    def __call__(self, value: str) -> str:
        """Return the masked form of ``value``."""
        ...


__all__ = [
    "CounterProtocol",
    "ErrorReporterProtocol",
    "GaugeProtocol",
    "HistogramProtocol",
    "Masker",
    "Redactor",
    "SpanAttributeValue",
    "SpanProtocol",
    "TracerProtocol",
]
