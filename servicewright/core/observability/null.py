"""NullObject sinks: the *disabled/unconfigured* half of the degradation rule.

Missing extra for a selected+configured backend hard-raises at Bootstrap;
a concern that is disabled or unconfigured resolves to these no-ops, so
emitters (interceptors, middlewares, entrypoints) never branch on ``None``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import TracebackType

    from .config import ObsSetupContext


class NullSpan:
    """No-op span; usable as a context manager."""

    def set_attribute(self, key: str, value: Any) -> None:
        """Discard the attribute."""

    def record_exception(self, exception: Exception) -> None:
        """Discard the exception."""

    def __enter__(self) -> NullSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class NullTracer:
    """No-op tracer minting :class:`NullSpan`."""

    def start_as_current_span(self, name: str) -> NullSpan:
        """Return a no-op span."""
        return NullSpan()


class NullCounter:
    """No-op counter instrument."""

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """Discard the increment."""


class NullHistogram:
    """No-op histogram instrument."""

    def observe(self, value: float, **labels: str) -> None:
        """Discard the observation."""


class NullGauge:
    """No-op gauge instrument."""

    def set(self, value: float, **labels: str) -> None:
        """Discard the value."""

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """Discard the increment."""

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        """Discard the decrement."""


class NullErrorReporter:
    """No-op error reporter."""

    def capture_exception(self, error: Exception) -> None:
        """Discard the exception."""

    def add_breadcrumb(
        self,
        message: str,
        category: str = "default",
        level: str = "info",
        data: dict[str, object] | None = None,
    ) -> None:
        """Discard the breadcrumb."""

    def set_tags(self, **tags: str) -> None:
        """Discard the tags."""


class NullMetricsSink:
    """Metrics concern disabled: every handle is a no-op."""

    backend: str = "null"

    def setup(self, ctx: ObsSetupContext) -> None:
        """No-op."""

    def shutdown(self) -> None:
        """No-op."""

    def counter(self, name: str, description: str, label_names: tuple[str, ...] = ()) -> NullCounter:
        """Return a no-op counter."""
        return NullCounter()

    def histogram(
        self,
        name: str,
        description: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> NullHistogram:
        """Return a no-op histogram."""
        return NullHistogram()

    def gauge(self, name: str, description: str, label_names: tuple[str, ...] = ()) -> NullGauge:
        """Return a no-op gauge."""
        return NullGauge()


class NullTracingSink:
    """Tracing concern disabled: every handle is a no-op."""

    backend: str = "null"

    def setup(self, ctx: ObsSetupContext) -> None:
        """No-op."""

    def shutdown(self) -> None:
        """No-op."""

    def tracer(self, name: str) -> NullTracer:
        """Return a no-op tracer."""
        return NullTracer()

    def instrument_fastapi(self, app: Any, *, excluded_urls: str | None = None) -> None:
        """No-op."""


class NullErrorTrackingSink:
    """Error-tracking concern disabled: every handle is a no-op."""

    backend: str = "null"

    def setup(self, ctx: ObsSetupContext) -> None:
        """No-op."""

    def shutdown(self) -> None:
        """No-op."""

    def reporter(self) -> NullErrorReporter:
        """Return a no-op reporter."""
        return NullErrorReporter()


class NullLoggingSink:
    """Logging concern disabled: leave the root logger untouched."""

    backend: str = "null"

    def setup(self, ctx: ObsSetupContext) -> None:
        """No-op."""

    def shutdown(self) -> None:
        """No-op."""


__all__ = [
    "NullCounter",
    "NullErrorReporter",
    "NullErrorTrackingSink",
    "NullGauge",
    "NullHistogram",
    "NullLoggingSink",
    "NullMetricsSink",
    "NullSpan",
    "NullTracer",
    "NullTracingSink",
]
