"""Unit tests for the concrete observability backends (adapter sinks)."""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from prometheus_client import CollectorRegistry

from servicewright.adapters.observability._errors import sentry as sentry_mod
from servicewright.adapters.observability._errors.sentry import SentryErrorTrackingSink, SentryReporter
from servicewright.adapters.observability._logging.stdlib import StdlibLoggingSink
from servicewright.adapters.observability._logging.structlog import StructlogLoggingSink
from servicewright.adapters.observability._metrics import prometheus as prometheus_mod
from servicewright.adapters.observability._metrics.prometheus import (
    PrometheusCounter,
    PrometheusHistogram,
    PrometheusMetricsSink,
)
from servicewright.adapters.observability._tracing import otel as otel_mod
from servicewright.adapters.observability._tracing.otel import OtelTracingSink
from servicewright.core.observability import KeyRedactor, ObsSetupContext, make_metric_name
from servicewright.testing import FakeSettings

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Settings doubles
# --------------------------------------------------------------------------- #
class _LoggingSettings:
    def __init__(self, level: str = "INFO", use_json: bool = True) -> None:
        self.level = level
        self.use_json = use_json


class _SentrySettings:
    dsn = "http://key@sentry.local/1"
    environment = "test-env"
    traces_sample_rate = 0.25
    profiles_sample_rate = 0.5
    debug = False


class _OtelSettings:
    service_name = "otel-svc"
    collector_url: str | None = None
    sample_ratio = 0.5
    insecure = True
    enable_console_exporter = False
    excluded_urls = None


class _MetricsSettings:
    def __init__(self, enabled: bool = True, host: str = "127.0.0.1", port: int = 0) -> None:
        self.enabled = enabled
        self.host = host
        self.port = port
        self.prefix = None


def _ctx(settings: Any, redactor: Any = None) -> ObsSetupContext:
    return ObsSetupContext(
        service_name="svc",
        app_version="1.2.3",
        environment="test-env",
        settings=settings,
        redactor=redactor,
    )


def _settings_with(**sections: Any) -> FakeSettings:
    settings = FakeSettings()
    for name, value in sections.items():
        setattr(settings, name, value)
    return settings


def _json_line(captured: str, *, event: str) -> dict[str, Any]:
    lines = [json.loads(line) for line in captured.strip().splitlines() if line.startswith("{")]
    return next(line for line in lines if line["event"] == event)


# --------------------------------------------------------------------------- #
# metric names
# --------------------------------------------------------------------------- #
def test__make_metric_name__prefix_given__prepends_it() -> None:
    assert make_metric_name("requests_total") == "requests_total"
    assert make_metric_name("requests_total", "svc") == "svc_requests_total"


def test__make_metric_name__invalid_prefix__raises() -> None:
    with pytest.raises(ValueError, match="Invalid metric prefix"):
        make_metric_name("x", "9bad-prefix")


# --------------------------------------------------------------------------- #
# Prometheus
# --------------------------------------------------------------------------- #
def test__prometheus_instruments__values_recorded__land_in_the_registry() -> None:
    registry = CollectorRegistry()
    sink = PrometheusMetricsSink(registry=registry)

    counter = sink.counter("jobs_total", "Jobs processed", ("queue", "status"))
    counter.inc(queue="default", status="ok")
    counter.inc(2.0, queue="default", status="ok")

    histogram = sink.histogram("job_duration_seconds", "Job duration", ("queue",), buckets=(0.1, 1.0))
    histogram.observe(0.5, queue="default")

    # prometheus_client strips the "_total" suffix from the family name and
    # re-adds it on samples, so the sample keeps the requested name.
    assert registry.get_sample_value("jobs_total", {"queue": "default", "status": "ok"}) == 3.0
    assert registry.get_sample_value("job_duration_seconds_count", {"queue": "default"}) == 1.0


def test__prometheus_instruments__no_labels__still_record() -> None:
    registry = CollectorRegistry()
    sink = PrometheusMetricsSink(registry=registry)
    sink.counter("plain_total", "No labels").inc()
    sink.histogram("plain_seconds", "No labels").observe(0.2)
    assert registry.get_sample_value("plain_total", {}) == 1.0
    assert registry.get_sample_value("plain_seconds_count", {}) == 1.0


def test__prometheus_sink__same_name_requested_twice__returns_the_same_instrument() -> None:
    sink = PrometheusMetricsSink(registry=CollectorRegistry())
    first = sink.counter("hits_total", "Hits", ("a",))
    second = sink.counter("hits_total", "Hits", ("a",))
    assert first is second
    assert isinstance(first, PrometheusCounter)
    assert isinstance(sink.histogram("lat_seconds", "Latency"), PrometheusHistogram)


def test__prometheus_sink__name_reused_for_another_type__raises() -> None:
    sink = PrometheusMetricsSink(registry=CollectorRegistry())
    sink.counter("dual_total", "Counter first")
    with pytest.raises(TypeError, match="different instrument type"):
        sink.histogram("dual_total", "Histogram second")


def test__prometheus_sink__exposition_enabled__starts_and_stops_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_server = MagicMock()
    make_server_calls: list[dict[str, Any]] = []

    def fake_make_server(**kwargs: Any) -> MagicMock:
        make_server_calls.append(kwargs)
        return fake_server

    fake_thread = MagicMock()
    fake_thread.is_alive.return_value = False
    monkeypatch.setattr(prometheus_mod, "make_server", fake_make_server)
    monkeypatch.setattr(prometheus_mod.threading, "Thread", MagicMock(return_value=fake_thread))

    sink = PrometheusMetricsSink(registry=CollectorRegistry())
    sink.setup(_ctx(_settings_with(metrics=_MetricsSettings(enabled=True, port=9309))))

    assert make_server_calls[0]["host"] == "127.0.0.1"
    assert make_server_calls[0]["port"] == 9309
    fake_thread.start.assert_called_once()

    # Second setup is idempotent.
    sink.setup(_ctx(_settings_with(metrics=_MetricsSettings(enabled=True, port=9309))))
    assert len(make_server_calls) == 1

    sink.shutdown()
    fake_server.shutdown.assert_called_once()
    fake_server.server_close.assert_called_once()
    fake_thread.join.assert_called_once()

    # Shutdown twice is a no-op.
    sink.shutdown()
    fake_server.shutdown.assert_called_once()


def test__prometheus_sink__exposition_disabled__starts_no_server(monkeypatch: pytest.MonkeyPatch) -> None:
    boom = MagicMock(side_effect=AssertionError("server must not start"))
    monkeypatch.setattr(prometheus_mod, "make_server", boom)

    sink = PrometheusMetricsSink(registry=CollectorRegistry())
    sink.setup(_ctx(_settings_with(metrics=_MetricsSettings(enabled=False))))
    sink.setup(_ctx(FakeSettings()))  # no metrics section at all
    sink.shutdown()  # nothing started, nothing to stop
    boom.assert_not_called()


# --------------------------------------------------------------------------- #
# OpenTelemetry
# --------------------------------------------------------------------------- #
def test__otel_sink_setup__collector_configured__installs_the_global_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(otel_mod.trace, "set_tracer_provider", installed.append)

    sink = OtelTracingSink()
    sink.setup(_ctx(_settings_with(tracing=_OtelSettings())))

    assert len(installed) == 1
    provider = installed[0]
    assert provider.resource.attributes["service.name"] == "otel-svc"
    assert provider.resource.attributes["service.version"] == "1.2.3"

    # Idempotent per sink instance.
    sink.setup(_ctx(_settings_with(tracing=_OtelSettings())))
    assert len(installed) == 1

    sink.shutdown()
    sink.shutdown()  # second is a no-op


def test__otel_sink_setup__no_tracing_settings__does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(otel_mod.trace, "set_tracer_provider", installed.append)
    sink = OtelTracingSink()
    sink.setup(_ctx(FakeSettings()))
    assert installed == []


def test__otel_sink_tracer__used__mints_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otel_mod.trace, "set_tracer_provider", lambda provider: None)
    sink = OtelTracingSink()
    tracer = sink.tracer("test")
    with tracer.start_as_current_span("op") as span:  # type: ignore[attr-defined]
        span.set_attribute("k", "v")


def test__otel_sink__instrument_fastapi_called__instruments_the_app() -> None:
    from fastapi import FastAPI

    app = FastAPI()
    sink = OtelTracingSink()
    sink.instrument_fastapi(app, excluded_urls="/system/liveness")
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True


def test__otel_sink__instrumentor_not_installed__degrades_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("opentelemetry.instrumentation.fastapi"):
            raise ImportError("missing instrumentor")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with caplog.at_level(logging.WARNING):
        OtelTracingSink().instrument_fastapi(object(), excluded_urls=None)
    assert any("instrumentor not installed" in record.message for record in caplog.records)


# --------------------------------------------------------------------------- #
# Sentry
# --------------------------------------------------------------------------- #
def test__sentry_sink_setup__dsn_configured__initializes_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    init_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_mod.sentry_sdk, "init", lambda **kwargs: init_calls.append(kwargs))
    flush = MagicMock()
    monkeypatch.setattr(sentry_mod.sentry_sdk, "flush", flush)

    redactor = KeyRedactor()
    sink = SentryErrorTrackingSink()
    sink.setup(_ctx(_settings_with(error_tracking=_SentrySettings()), redactor=redactor))

    assert len(init_calls) == 1
    kwargs = init_calls[0]
    assert kwargs["dsn"] == "http://key@sentry.local/1"
    assert kwargs["environment"] == "test-env"
    assert kwargs["release"] == "1.2.3"
    assert kwargs["traces_sample_rate"] == 0.25

    # before_send applies the cross-cutting redactor to outgoing events.
    before_send = kwargs["before_send"]
    event = before_send({"extra": {"password": "hunter2"}, "message": "x"}, {})
    assert event["extra"]["password"] == "[REDACTED]"

    # Idempotent per sink instance; shutdown flushes.
    sink.setup(_ctx(_settings_with(error_tracking=_SentrySettings())))
    assert len(init_calls) == 1
    sink.shutdown()
    flush.assert_called_once()


def test__sentry_sink_setup__no_dsn__does_not_initialize_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    init = MagicMock()
    flush = MagicMock()
    monkeypatch.setattr(sentry_mod.sentry_sdk, "init", init)
    monkeypatch.setattr(sentry_mod.sentry_sdk, "flush", flush)

    sink = SentryErrorTrackingSink()
    sink.setup(_ctx(FakeSettings()))
    sink.shutdown()

    init.assert_not_called()
    flush.assert_not_called()


def test__sentry_sink_setup__no_redactor__installs_no_before_send_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    init_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_mod.sentry_sdk, "init", lambda **kwargs: init_calls.append(kwargs))
    SentryErrorTrackingSink().setup(_ctx(_settings_with(error_tracking=_SentrySettings())))
    assert init_calls[0]["before_send"] is None


@pytest.fixture
def sentry_init_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the kwargs of every ``sentry_sdk.init`` call instead of initializing the SDK."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_mod.sentry_sdk, "init", lambda **kwargs: calls.append(kwargs))
    return calls


class _DomainError(Exception):
    """An expected business error a service does not want reported."""


def _drop_domain_errors(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    exc_info = hint.get("exc_info")
    if exc_info is not None and isinstance(exc_info[1], _DomainError):
        return None
    return event


def test__sentry_sink__init_kwargs_given__are_forwarded_next_to_the_settings_driven_ones(
    sentry_init_calls: list[dict[str, Any]],
) -> None:
    # Arrange
    sink = SentryErrorTrackingSink(ignore_errors=[_DomainError], send_default_pii=False)

    # Act
    sink.setup(_ctx(_settings_with(error_tracking=_SentrySettings())))

    # Assert
    kwargs = sentry_init_calls[0]
    assert kwargs["ignore_errors"] == [_DomainError]
    assert kwargs["send_default_pii"] is False
    assert kwargs["dsn"] == "http://key@sentry.local/1"
    assert kwargs["release"] == "1.2.3"


@pytest.mark.parametrize(
    "argument",
    ["dsn", "environment", "release", "traces_sample_rate", "profiles_sample_rate", "debug"],
)
def test__sentry_sink__settings_driven_argument_given__raises_at_construction(argument: str) -> None:
    with pytest.raises(ValueError, match=f"{argument}: driven by settings.error_tracking"):
        SentryErrorTrackingSink(**{argument: "x"})


def test__sentry_sink__before_send_given__runs_first_with_the_hint_and_may_drop(
    sentry_init_calls: list[dict[str, Any]],
) -> None:
    # Arrange
    sink = SentryErrorTrackingSink(before_send=_drop_domain_errors)
    sink.setup(_ctx(_settings_with(error_tracking=_SentrySettings()), redactor=KeyRedactor()))
    before_send = sentry_init_calls[0]["before_send"]
    event = {"extra": {"password": "hunter2"}, "message": "x"}

    # Act
    dropped = before_send(event, {"exc_info": (_DomainError, _DomainError("expected"), None)})
    kept = before_send(event, {"exc_info": (ValueError, ValueError("unexpected"), None)})

    # Assert
    assert dropped is None
    assert kept is not None
    assert kept["extra"]["password"] == "[REDACTED]"


def test__sentry_sink__before_send_given_without_a_redactor__is_installed_on_its_own(
    sentry_init_calls: list[dict[str, Any]],
) -> None:
    # Arrange
    sink = SentryErrorTrackingSink(before_send=_drop_domain_errors)
    sink.setup(_ctx(_settings_with(error_tracking=_SentrySettings())))
    before_send = sentry_init_calls[0]["before_send"]
    event = {"extra": {"password": "hunter2"}}

    # Act
    kept = before_send(event, {"exc_info": (ValueError, ValueError("unexpected"), None)})

    # Assert
    assert kept == {"extra": {"password": "hunter2"}}


def test__sentry_sink__before_send_transaction_given__is_forwarded_untouched(
    sentry_init_calls: list[dict[str, Any]],
) -> None:
    # Arrange
    def drop_probes(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
        return None if str(event.get("transaction", "")).startswith("/system/") else event

    sink = SentryErrorTrackingSink(before_send_transaction=drop_probes)

    # Act
    sink.setup(_ctx(_settings_with(error_tracking=_SentrySettings()), redactor=KeyRedactor()))

    # Assert
    assert sentry_init_calls[0]["before_send_transaction"] is drop_probes


def test__sentry_reporter__error_reported__delegates_to_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = MagicMock()
    breadcrumb = MagicMock()
    set_tag = MagicMock()
    monkeypatch.setattr(sentry_mod.sentry_sdk, "capture_exception", capture)
    monkeypatch.setattr(sentry_mod.sentry_sdk, "add_breadcrumb", breadcrumb)
    monkeypatch.setattr(sentry_mod.sentry_sdk, "set_tag", set_tag)

    reporter = SentryReporter()
    error = ValueError("boom")
    reporter.capture_exception(error)
    reporter.add_breadcrumb("msg", category="db", level="warning", data={"q": 1})
    reporter.set_tags(env="test", zone="a")

    capture.assert_called_once_with(error)
    breadcrumb.assert_called_once_with(message="msg", category="db", level="warning", data={"q": 1})
    assert set_tag.call_count == 2


# --------------------------------------------------------------------------- #
# Logging sinks
# --------------------------------------------------------------------------- #
def test__stdlib_logging_sink__json_enabled__emits_redacted_json_lines(capsys: pytest.CaptureFixture[str]) -> None:
    sink = StdlibLoggingSink()
    sink.setup(_ctx(_settings_with(logging=_LoggingSettings("INFO", use_json=True)), redactor=KeyRedactor()))
    try:
        logging.getLogger("test.stdlib").info("hello %s", "world", extra={"password": "x", "user": "alice"})
    finally:
        sink.shutdown()

    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.stdlib"
    assert payload["password"] == "[REDACTED]"
    assert payload["user"] == "alice"


def test__stdlib_logging_sink__json_disabled__emits_plain_lines(capsys: pytest.CaptureFixture[str]) -> None:
    sink = StdlibLoggingSink()
    sink.setup(_ctx(_settings_with(logging=_LoggingSettings("WARNING", use_json=False))))
    try:
        root = logging.getLogger()
        assert root.level == logging.WARNING
        logging.getLogger("test.plain").warning("plain message")
    finally:
        sink.shutdown()

    assert "plain message" in capsys.readouterr().err


def test__stdlib_logging_sink_shutdown__called__removes_its_handler() -> None:
    sink = StdlibLoggingSink()
    before = list(logging.getLogger().handlers)
    sink.setup(_ctx(_settings_with(logging=_LoggingSettings())))
    assert len(logging.getLogger().handlers) == len(before) + 1
    sink.shutdown()
    assert logging.getLogger().handlers == before
    sink.shutdown()  # idempotent


def test__stdlib_logging_sink_setup__no_logging_settings__does_nothing() -> None:
    sink = StdlibLoggingSink()
    before = list(logging.getLogger().handlers)
    sink.setup(_ctx(FakeSettings()))
    assert logging.getLogger().handlers == before


def test__structlog_logging_sink__json_enabled__emits_redacted_json_lines(capsys: pytest.CaptureFixture[str]) -> None:
    import structlog

    sink = StructlogLoggingSink()
    sink.setup(_ctx(_settings_with(logging=_LoggingSettings("INFO", use_json=True)), redactor=KeyRedactor()))
    try:
        structlog.get_logger("test.structlog").info("structured event", password="x", user="alice")
        logging.getLogger("test.foreign").info("foreign record")
    finally:
        sink.shutdown()

    err = capsys.readouterr().err
    lines = [json.loads(line) for line in err.strip().splitlines() if line.startswith("{")]
    structured = next(line for line in lines if line["event"] == "structured event")
    assert structured["password"] == "[REDACTED]"
    assert structured["user"] == "alice"
    assert structured["level"] == "info"
    # Foreign stdlib records render through the same chain.
    foreign = next(line for line in lines if line["event"] == "foreign record")
    assert foreign["logger"] == "test.foreign"


def test__structlog_logging_sink__foreign_record_with_extra__keeps_the_extra_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """servicewright's own lines carry their payload in ``extra``; dropping it blanks them all."""
    sink = StructlogLoggingSink()
    sink.setup(_ctx(_settings_with(logging=_LoggingSettings("INFO", use_json=True))))
    try:
        logging.getLogger("test.access").info(
            "Request finished", extra={"method": "GET", "path": "/v1/users", "status_code": 200}
        )
    finally:
        sink.shutdown()

    payload = _json_line(capsys.readouterr().err, event="Request finished")
    assert (payload["method"], payload["path"], payload["status_code"]) == ("GET", "/v1/users", 200)


def test__structlog_logging_sink__foreign_record_with_extra__redacts_the_recovered_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A field recovered from ``extra`` is payload like any other, so the redactor must see it."""
    sink = StructlogLoggingSink()
    sink.setup(_ctx(_settings_with(logging=_LoggingSettings("INFO", use_json=True)), redactor=KeyRedactor()))
    try:
        logging.getLogger("test.access").info("Token used", extra={"token": "s3cret", "user": "alice"})
    finally:
        sink.shutdown()

    payload = _json_line(capsys.readouterr().err, event="Token used")
    assert (payload["token"], payload["user"]) == ("[REDACTED]", "alice")


def test__structlog_logging_sink_shutdown__called__restores_the_root_logger() -> None:
    sink = StructlogLoggingSink()
    before = list(logging.getLogger().handlers)
    sink.setup(_ctx(_settings_with(logging=_LoggingSettings())))
    assert len(logging.getLogger().handlers) == len(before) + 1
    sink.shutdown()
    assert logging.getLogger().handlers == before


# --------------------------------------------------------------------------- #
# Regression cover: instruments belong to a registry, not to a sink instance
# --------------------------------------------------------------------------- #
@pytest.fixture
def shared_registry() -> CollectorRegistry:
    return CollectorRegistry()


def test__prometheus_sink__two_sinks_on_one_registry__reuse_the_same_counter(
    shared_registry: CollectorRegistry,
) -> None:
    # Arrange
    first = PrometheusMetricsSink(registry=shared_registry)
    second = PrometheusMetricsSink(registry=shared_registry)

    # Act
    counter = first.counter("shared_jobs_total", "Jobs", ("queue",))
    same_counter = second.counter("shared_jobs_total", "Jobs", ("queue",))

    # Assert
    assert counter is same_counter


def test__prometheus_sink__two_sinks_on_one_registry__reuse_the_same_histogram(
    shared_registry: CollectorRegistry,
) -> None:
    # Arrange
    first = PrometheusMetricsSink(registry=shared_registry)
    second = PrometheusMetricsSink(registry=shared_registry)

    # Act
    histogram = first.histogram("shared_job_seconds", "Duration", ("queue",))
    same_histogram = second.histogram("shared_job_seconds", "Duration", ("queue",))

    # Assert
    assert histogram is same_histogram


def test__prometheus_sink__second_service_in_the_process__does_not_raise_duplicate_timeseries() -> None:
    # Arrange
    # Both sinks default to the process-global REGISTRY, which is exactly what
    # the registry-resolver builds for a second AppSpec in one process.
    first = PrometheusMetricsSink()
    second = PrometheusMetricsSink()
    first.counter("global_probe_total", "Probe", ("code",))

    # Act
    reused = second.counter("global_probe_total", "Probe", ("code",))

    # Assert
    assert reused is not None


def test__prometheus_sink__separate_registries__keep_separate_instruments(
    shared_registry: CollectorRegistry,
) -> None:
    # Arrange
    other_registry = CollectorRegistry()

    # Act
    counter = PrometheusMetricsSink(registry=shared_registry).counter("isolated_total", "Isolated")
    other_counter = PrometheusMetricsSink(registry=other_registry).counter("isolated_total", "Isolated")

    # Assert
    assert counter is not other_counter


def test__prometheus_sink__name_reused_for_another_instrument_type__raises(
    shared_registry: CollectorRegistry,
) -> None:
    # Arrange
    sink = PrometheusMetricsSink(registry=shared_registry)
    sink.counter("clashing_name_total", "A counter")

    # Act & Assert
    with pytest.raises(TypeError, match="already registered"):
        sink.histogram("clashing_name_total", "A histogram")


# --------------------------------------------------------------------------- #
# OpenTelemetry span-attribute redaction
# --------------------------------------------------------------------------- #
def _provider_with_memory_exporter(monkeypatch: pytest.MonkeyPatch, redactor: Any) -> tuple[Any, Any]:
    """Set the otel sink up with a redactor and attach an in-memory exporter AFTER it."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    installed: list[Any] = []
    monkeypatch.setattr(otel_mod.trace, "set_tracer_provider", installed.append)
    sink = OtelTracingSink()
    otel_settings = _OtelSettings()
    otel_settings.sample_ratio = 1.0  # the shared double samples 50% - deterministic here
    sink.setup(_ctx(_settings_with(tracing=otel_settings), redactor=redactor))
    provider = installed[0]
    exporter = InMemorySpanExporter()
    # Registered after the redacting processor: on_end runs in registration
    # order, so this exporter sees exactly what a real OTLP exporter would.
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test__otel_sink__redactor_in_ctx__span_attributes_are_redacted_before_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, exporter = _provider_with_memory_exporter(monkeypatch, KeyRedactor())

    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("op") as span:
        span.set_attribute("password", "hunter2")
        span.set_attribute("http.route", "/orders")

    (exported,) = exporter.get_finished_spans()
    assert exported.attributes["password"] == "[REDACTED]"
    assert exported.attributes["http.route"] == "/orders"


def test__otel_sink__redactor_raises__span_keeps_timing_but_loses_all_attributes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(data: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("scrubber down")

    provider, exporter = _provider_with_memory_exporter(monkeypatch, broken)

    tracer = provider.get_tracer("test")
    with caplog.at_level(logging.WARNING):
        with tracer.start_as_current_span("op") as span:
            span.set_attribute("password", "hunter2")
        with tracer.start_as_current_span("op2") as span:
            span.set_attribute("password", "hunter2")

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    # Fail closed: a failing scrubber must never publish what it was meant to scrub.
    assert all(dict(exported.attributes or {}) == {} for exported in spans)
    assert all(exported.end_time is not None for exported in spans)
    warnings = [record for record in caplog.records if "trace redactor raised" in record.message]
    assert len(warnings) == 1  # once per processor, not per span


def test__otel_sink__no_redactor__spans_pass_through_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, exporter = _provider_with_memory_exporter(monkeypatch, None)

    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("op") as span:
        span.set_attribute("password", "hunter2")

    (exported,) = exporter.get_finished_spans()
    assert exported.attributes["password"] == "hunter2"
