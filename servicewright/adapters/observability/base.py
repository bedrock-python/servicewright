"""Author-facing add-on sink ABCs (the adapter-layer implementation contract).

Each concrete backend (prometheus / datadog / otel / sentry / structlog / stdlib)
subclasses one of these. Unlike the pure seams in
``servicewright.core.contracts.observability``, these ABCs reference SDK-touching
method shapes (``mount(app)`` / ``instrument_fastapi`` / minting a tracer), so they
live in the adapter layer.

Lifecycle: the Host calls :meth:`setup` in Bootstrap and :meth:`shutdown` in
Cleanup (best-effort, reverse order). Emitters use the seam objects minted on
demand. Missing extra => the concrete subclass's ``_imports`` guard hard-raises at
Bootstrap; ``enabled=False`` => a NullObject sink is used instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...core.contracts.observability import (
        CounterProtocol,
        ErrorReporterProtocol,
        HistogramProtocol,
        TracerProtocol,
    )
    from ...core.observability.config import ObsSetupContext


class MetricsSink(ABC):
    """A metrics backend (prometheus / datadog / otel).

    A transport-neutral instrument factory: transport adapters compose their
    recorders (and own their frozen metric names) from these instruments.
    Repeated requests for the same ``name`` must return the same instrument.
    """

    backend: str

    @abstractmethod
    def setup(self, ctx: ObsSetupContext) -> None:
        """Initialize the client/registry; start exposition if settings enable it."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Flush/close the client and stop exposition (best-effort)."""
        ...

    @abstractmethod
    def counter(self, name: str, description: str, label_names: tuple[str, ...] = ()) -> CounterProtocol:
        """Mint (or reuse) a counter instrument."""
        ...

    @abstractmethod
    def histogram(
        self,
        name: str,
        description: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> HistogramProtocol:
        """Mint (or reuse) a histogram instrument (``None`` buckets = backend default)."""
        ...


class TracingSink(ABC):
    """A tracing backend (otel / datadog)."""

    backend: str

    @abstractmethod
    def setup(self, ctx: ObsSetupContext) -> None:
        """Install the global tracer provider + exporter + sampler."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Flush spans (best-effort)."""
        ...

    @abstractmethod
    def tracer(self, name: str) -> TracerProtocol:
        """Mint a tracer seam."""
        ...

    def instrument_fastapi(self, app: Any, *, excluded_urls: str | None = None) -> None:  # noqa: B027 - optional hook
        """Instrument a live FastAPI app (needs the app instance, so done at bind)."""


class ErrorTrackingSink(ABC):
    """An error-tracking backend (sentry)."""

    backend: str

    @abstractmethod
    def setup(self, ctx: ObsSetupContext) -> None:
        """Initialize the global error-tracking client."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Flush queued events (best-effort)."""
        ...

    @abstractmethod
    def reporter(self) -> ErrorReporterProtocol:
        """Mint the error-reporter seam."""
        ...


class LoggingSink(ABC):
    """A logging backend (structlog / stdlib)."""

    backend: str

    @abstractmethod
    def setup(self, ctx: ObsSetupContext) -> None:
        """Configure the root logger / processor chain."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Tear down logging handlers (best-effort)."""
        ...


__all__ = [
    "ErrorTrackingSink",
    "LoggingSink",
    "MetricsSink",
    "TracingSink",
]
