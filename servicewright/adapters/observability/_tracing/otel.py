"""OpenTelemetry tracing backend: global TracerProvider + OTLP exporter.

The Host owns SDK bootstrap (servicewright's deliberate inverse of seam-only
libraries): ``setup()`` installs the global tracer provider, ``shutdown()``
flushes it. Interceptors/middlewares stay passive seams minted via
:meth:`OtelTracingSink.tracer`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from ..base import TracingSink

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("OpenTelemetry tracing requires servicewright[observability]; install it.") from exc

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan, Span

    from ....core.contracts.observability import Redactor, TracerProtocol
    from ....core.observability.config import ObsSetupContext

logger = logging.getLogger(__name__)


class _RedactingSpanProcessor(SpanProcessor):
    """Rewrites span attributes through the redactor before any exporter sees them.

    Registered FIRST, ahead of the exporting processors: ``on_end`` runs in
    registration order, and the batch exporter enqueues the same span object,
    so mutating attributes here is what the exporter later serializes. The SDK
    offers no public post-end attribute hook — rewriting ``_attributes`` is the
    established scrubbing pattern for OTel Python.

    Fail closed: if the redactor raises, the span keeps its name and timing but
    loses ALL attributes — a failing scrubber must never publish what it was
    supposed to scrub. One warning per processor, not per span.
    """

    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self._warned = False

    def on_end(self, span: ReadableSpan) -> None:
        attributes = span._attributes
        if not attributes:
            return
        try:
            span._attributes = self._redactor(dict(attributes))
        except Exception:
            span._attributes = {}
            if not self._warned:
                self._warned = True
                logger.warning("trace redactor raised; dropping span attributes", exc_info=True)

    def on_start(self, span: Span, parent_context: object = None) -> None:
        """Nothing to do at span start; redaction happens once, at the end."""

    def shutdown(self) -> None:
        """Stateless processor; nothing to flush."""

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Stateless processor; always already flushed."""
        return True


class OtelTracingSink(TracingSink):
    """OpenTelemetry backend driven by ``settings.tracing``."""

    backend = "otel"

    def __init__(self) -> None:
        self._provider: TracerProvider | None = None

    def setup(self, ctx: ObsSetupContext) -> None:
        """Install the global tracer provider (idempotent per sink instance)."""
        if self._provider is not None:
            return
        otel_settings = getattr(ctx.settings, "tracing", None)
        if otel_settings is None:
            return

        resource = Resource.create(
            {
                "service.name": getattr(otel_settings, "service_name", "") or ctx.service_name,
                "service.version": ctx.app_version,
            }
        )
        sampler = ParentBased(TraceIdRatioBased(getattr(otel_settings, "sample_ratio", 1.0)))
        provider = TracerProvider(resource=resource, sampler=sampler)

        # Must be registered before any exporting processor: on_end runs in
        # registration order, and exporters must only ever see redacted spans.
        if ctx.redactor is not None:
            provider.add_span_processor(_RedactingSpanProcessor(ctx.redactor))

        collector_url = getattr(otel_settings, "collector_url", None)
        if collector_url:
            exporter = OTLPSpanExporter(endpoint=collector_url, insecure=getattr(otel_settings, "insecure", True))
            provider.add_span_processor(BatchSpanProcessor(exporter))
        if getattr(otel_settings, "enable_console_exporter", False):
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)
        self._provider = provider
        logger.info("OpenTelemetry tracing configured", extra={"collector_url": collector_url})

    def shutdown(self) -> None:
        """Flush and shut the provider down (best-effort)."""
        if self._provider is None:
            return
        try:
            self._provider.shutdown()
        finally:
            self._provider = None

    def tracer(self, name: str) -> TracerProtocol:
        """Mint a tracer from the global provider."""
        return cast("TracerProtocol", trace.get_tracer(name))

    def instrument_fastapi(self, app: Any, *, excluded_urls: str | None = None) -> None:
        """Instrument a live FastAPI app; degrades with a warning when the instrumentor is absent."""
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        except ImportError:
            logger.warning(
                "OpenTelemetry FastAPI instrumentor not installed; skipping FastAPI instrumentation. "
                "Install opentelemetry-instrumentation-fastapi to enable request tracing."
            )
            return
        FastAPIInstrumentor.instrument_app(app, excluded_urls=excluded_urls)


__all__ = ["OtelTracingSink"]
