"""Structural sink protocols the manager and entrypoints program against.

These are the *runtime handles* for the four add-on concerns. They mirror the
author-facing ABCs in ``servicewright.adapters.observability.base`` (which
concrete backends subclass), but stay pure so the manager — which lives in the
kernel — never imports the adapter layer. ``app`` parameters are deliberately
``Any``: the kernel cannot name framework types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ..contracts.observability import (
        CounterProtocol,
        ErrorReporterProtocol,
        HistogramProtocol,
        TracerProtocol,
    )
    from .config import ObsSetupContext


class MetricsSinkProtocol(Protocol):
    """A metrics backend handle (prometheus / datadog / otel).

    Exposes generic *instruments*; transport adapters compose their own
    recorders (and own their frozen metric names) from these, so neither a new
    transport nor a new backend ever touches the kernel. Backends must return
    the same instrument for repeated requests of the same ``name``.
    """

    backend: str

    def setup(self, ctx: ObsSetupContext) -> None:
        """Initialize the client/registry (+ exposition when settings enable it)."""
        ...

    def shutdown(self) -> None:
        """Flush/close the client (best-effort)."""
        ...

    def counter(self, name: str, description: str, label_names: tuple[str, ...] = ()) -> CounterProtocol:
        """Mint (or reuse) a counter instrument."""
        ...

    def histogram(
        self,
        name: str,
        description: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> HistogramProtocol:
        """Mint (or reuse) a histogram instrument (``None`` buckets = backend default)."""
        ...


class TracingSinkProtocol(Protocol):
    """A tracing backend handle (otel / datadog)."""

    backend: str

    def setup(self, ctx: ObsSetupContext) -> None:
        """Install the global tracer provider + exporter + sampler."""
        ...

    def shutdown(self) -> None:
        """Flush spans (best-effort)."""
        ...

    def tracer(self, name: str) -> TracerProtocol:
        """Mint a tracer seam."""
        ...

    def instrument_fastapi(self, app: Any, *, excluded_urls: str | None = None) -> None:
        """Instrument a live FastAPI app (needs the app instance, so done at bind)."""
        ...


class ErrorTrackingSinkProtocol(Protocol):
    """An error-tracking backend handle (sentry)."""

    backend: str

    def setup(self, ctx: ObsSetupContext) -> None:
        """Initialize the global error-tracking client."""
        ...

    def shutdown(self) -> None:
        """Flush queued events (best-effort)."""
        ...

    def reporter(self) -> ErrorReporterProtocol:
        """Mint the error-reporter seam."""
        ...


class LoggingSinkProtocol(Protocol):
    """A logging backend handle (structlog / stdlib)."""

    backend: str

    def setup(self, ctx: ObsSetupContext) -> None:
        """Configure the root logger / processor chain."""
        ...

    def shutdown(self) -> None:
        """Tear down the handlers this sink installed (best-effort)."""
        ...


__all__ = [
    "ErrorTrackingSinkProtocol",
    "LoggingSinkProtocol",
    "MetricsSinkProtocol",
    "TracingSinkProtocol",
]
