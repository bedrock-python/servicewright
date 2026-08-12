"""Pluggable add-ons contract layer: seam protocols + config + sink ABCs.

Covers the contract foundation of the add-ons design (concrete backends —
prometheus/datadog/otel/sentry/structlog — and the multi-sink ObservabilityManager
wiring are a separate impl layer).
"""

from __future__ import annotations

from typing import Any

import pytest

from servicewright.adapters.observability import ErrorTrackingSink, LoggingSink, MetricsSink, TracingSink
from servicewright.core.contracts import (
    CounterProtocol,
    ErrorReporterProtocol,
    HistogramProtocol,
    Redactor,
    SpanProtocol,
    TracerProtocol,
)
from servicewright.core.observability.config import ObsConfig, ObsSetupContext
from servicewright.testing import FakeSettings

pytestmark = pytest.mark.unit


def _setup_ctx(service_name: str = "svc", app_version: str = "1.0.0", environment: str = "prod") -> ObsSetupContext:
    return ObsSetupContext(
        service_name=service_name,
        app_version=app_version,
        environment=environment,
        settings=FakeSettings(),
    )


def test__obs_config__default__selects_the_built_in_backends() -> None:
    cfg = ObsConfig()
    assert cfg.metrics == "prometheus"
    assert cfg.tracing == "otel"
    assert cfg.error_tracking == "sentry"
    assert cfg.logging == "structlog"
    assert ObsConfig(metrics=None).metrics is None


def test__obs_setup_context__constructed__carries_the_setup_inputs() -> None:
    ctx = _setup_ctx()
    assert (ctx.service_name, ctx.app_version, ctx.environment) == ("svc", "1.0.0", "prod")
    assert isinstance(ctx.settings, FakeSettings)
    assert ctx.redactor is None


def test__observability_seams__imported__are_all_exported() -> None:
    protocols = (CounterProtocol, ErrorReporterProtocol, HistogramProtocol, Redactor, SpanProtocol, TracerProtocol)
    names = {p.__name__ for p in protocols}
    assert names == {
        "CounterProtocol",
        "ErrorReporterProtocol",
        "HistogramProtocol",
        "Redactor",
        "SpanProtocol",
        "TracerProtocol",
    }


class _Span:
    def set_attribute(self, key: str, value: Any) -> None: ...
    def record_exception(self, exception: Exception) -> None: ...
    def __enter__(self) -> _Span:
        return self

    def __exit__(self, *args: object) -> None: ...


class _Tracer:
    def start_as_current_span(self, name: str) -> _Span:
        return _Span()


class _Counter:
    def inc(self, amount: float = 1.0, **labels: str) -> None: ...


class _Histogram:
    def observe(self, value: float, **labels: str) -> None: ...


class _Reporter:
    def capture_exception(self, error: Exception) -> None: ...
    def add_breadcrumb(self, message: str, **kwargs: object) -> None: ...
    def set_tags(self, **tags: str) -> None: ...


class _MetricsSink(MetricsSink):
    backend = "test"

    def setup(self, ctx: ObsSetupContext) -> None: ...
    def shutdown(self) -> None: ...
    def counter(self, name: str, description: str, label_names: tuple[str, ...] = ()) -> _Counter:
        return _Counter()

    def histogram(
        self,
        name: str,
        description: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> _Histogram:
        return _Histogram()


class _TracingSink(TracingSink):
    backend = "test"

    def setup(self, ctx: ObsSetupContext) -> None: ...
    def shutdown(self) -> None: ...
    def tracer(self, name: str) -> _Tracer:
        return _Tracer()


class _ErrorSink(ErrorTrackingSink):
    backend = "test"

    def setup(self, ctx: ObsSetupContext) -> None: ...
    def shutdown(self) -> None: ...
    def reporter(self) -> _Reporter:
        return _Reporter()


class _LoggingSink(LoggingSink):
    backend = "test"

    def setup(self, ctx: ObsSetupContext) -> None: ...
    def shutdown(self) -> None: ...


def test__sink_abc__instantiated_directly__raises() -> None:
    for sink in (MetricsSink, TracingSink, ErrorTrackingSink, LoggingSink):
        with pytest.raises(TypeError):
            sink()  # type: ignore[abstract]


def test__metrics_sink__defaults_not_overridden__are_no_ops() -> None:
    sink = _MetricsSink()
    sink.counter("x_total", "desc", ("a",)).inc(a="1")
    sink.histogram("x_seconds", "desc", ("a",)).observe(0.1, a="1")


def test__tracing_sink__instrument_fastapi_not_overridden__is_a_no_op() -> None:
    assert _TracingSink().instrument_fastapi(object(), excluded_urls="/health") is None


def test__concrete_sinks__checked_against_the_seams__satisfy_them() -> None:
    # runtime_checkable seams accept structurally-compatible fakes
    assert isinstance(_Tracer(), TracerProtocol)
    assert isinstance(_Span(), SpanProtocol)
    # the non-runtime-checkable seams are satisfied at type-check time; smoke their impls
    _Counter().inc(service="s")
    _Histogram().observe(0.1, service="s")
    _ErrorSink().reporter().capture_exception(ValueError("x"))
    _LoggingSink().setup(_setup_ctx())
