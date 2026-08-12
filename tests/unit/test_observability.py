"""Unit tests for the multi-sink ObservabilityManager, registry and redaction."""

from __future__ import annotations

from typing import Any

import pytest

from servicewright.core.observability import (
    KeyRedactor,
    NullCounter,
    NullErrorReporter,
    NullErrorTrackingSink,
    NullHistogram,
    NullLoggingSink,
    NullMetricsSink,
    NullSpan,
    NullTracer,
    NullTracingSink,
    ObsConfig,
    ObservabilityManager,
    register_sink,
    resolve_sink,
)
from servicewright.core.observability import registry as registry_mod
from servicewright.core.observability.redaction import MASK
from servicewright.testing import FakeSettings

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeSink:
    """Sink double recording setup/shutdown; class-level log shared per test."""

    events: list[tuple[str, str]] = []
    fail_on_shutdown = False

    backend = "fake"

    def __init__(self) -> None:
        self.setup_ctx: Any = None

    def setup(self, ctx: Any) -> None:
        self.setup_ctx = ctx
        type(self).events.append(("setup", type(self).__name__))

    def shutdown(self) -> None:
        type(self).events.append(("shutdown", type(self).__name__))
        if type(self).fail_on_shutdown:
            raise RuntimeError("shutdown boom")


class _FakeLoggingSink(_FakeSink):
    pass


class _FakeErrorSink(_FakeSink):
    def reporter(self) -> NullErrorReporter:
        return NullErrorReporter()


class _FakeTracingSink(_FakeSink):
    def tracer(self, name: str) -> NullTracer:
        return NullTracer()

    def instrument_fastapi(self, app: Any, *, excluded_urls: str | None = None) -> None:
        return None


class _FakeMetricsSink(_FakeSink):
    def mount(self, app: Any) -> None:
        return None

    def counter(self, name: str, description: str, label_names: tuple[str, ...] = ()) -> NullCounter:
        return NullCounter()

    def histogram(
        self,
        name: str,
        description: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> NullHistogram:
        return NullHistogram()


class _ConfiguredSettings(FakeSettings):
    """Settings double with every observability section configured."""

    class _Logging:
        level = "INFO"
        use_json = True

    class _ErrorTracking:
        dsn = "http://sentry.local/1"
        environment = "test-env"
        traces_sample_rate = 0.1
        profiles_sample_rate = 0.2
        debug = False

    class _Tracing:
        service_name = "otel-svc"
        collector_url = None
        sample_ratio = 1.0
        insecure = True
        enable_console_exporter = False
        excluded_urls = None

    class _Metrics:
        enabled = False
        host = "127.0.0.1"
        port = 9000
        prefix = None

    logging: Any = _Logging()
    error_tracking: Any = _ErrorTracking()
    tracing: Any = _Tracing()
    metrics: Any = _Metrics()


@pytest.fixture(autouse=True)
def _fake_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every registry entry at in-test fake sinks and reset their logs."""
    for sink_class in (_FakeLoggingSink, _FakeErrorSink, _FakeTracingSink, _FakeMetricsSink):
        sink_class.fail_on_shutdown = False
    _FakeSink.events = []
    for sink_class in (_FakeLoggingSink, _FakeErrorSink, _FakeTracingSink, _FakeMetricsSink):
        sink_class.events = _FakeSink.events

    fakes = {
        ("logging", "structlog"): _FakeLoggingSink,
        ("logging", "stdlib"): _FakeLoggingSink,
        ("error_tracking", "sentry"): _FakeErrorSink,
        ("tracing", "otel"): _FakeTracingSink,
        ("metrics", "prometheus"): _FakeMetricsSink,
    }

    def fake_resolve(concern: str, backend: str) -> type[Any]:
        key = (concern, backend)
        if key not in fakes:
            raise ValueError(f"Unknown {concern} backend {backend!r}; registered backends: []")
        return fakes[key]

    monkeypatch.setattr("servicewright.core.observability.manager.resolve_sink", fake_resolve)


# --------------------------------------------------------------------------- #
# Defaults / null behaviour
# --------------------------------------------------------------------------- #
def test__observability_manager__no_config__leaves_every_concern_null() -> None:
    manager = ObservabilityManager()
    assert isinstance(manager.metrics, NullMetricsSink)
    assert isinstance(manager.tracing, NullTracingSink)
    assert isinstance(manager.error_tracking, NullErrorTrackingSink)
    assert isinstance(manager.logging, NullLoggingSink)


def test__observability_manager_configure__no_config__leaves_every_concern_null() -> None:
    manager = ObservabilityManager()
    manager.configure(_ConfiguredSettings(), service_name="svc")
    assert isinstance(manager.metrics, NullMetricsSink)
    assert isinstance(manager.logging, NullLoggingSink)
    assert _FakeSink.events == []


def test__observability_manager_configure__concern_absent_from_settings__stays_null() -> None:
    manager = ObservabilityManager(ObsConfig())
    manager.configure(FakeSettings(), service_name="svc")  # every section is None
    assert isinstance(manager.metrics, NullMetricsSink)
    assert isinstance(manager.tracing, NullTracingSink)
    assert isinstance(manager.error_tracking, NullErrorTrackingSink)
    assert isinstance(manager.logging, NullLoggingSink)


def test__observability_manager_configure__concern_disabled_in_config__stays_null() -> None:
    manager = ObservabilityManager(ObsConfig(metrics=None, tracing=None, error_tracking=None, logging=None))
    manager.configure(_ConfiguredSettings(), service_name="svc")
    assert isinstance(manager.metrics, NullMetricsSink)
    assert isinstance(manager.tracing, NullTracingSink)


# --------------------------------------------------------------------------- #
# Selection + activation
# --------------------------------------------------------------------------- #
def test__observability_manager_configure__selected_and_configured__sets_the_sinks_up_in_order() -> None:
    manager = ObservabilityManager(ObsConfig())
    manager.configure(_ConfiguredSettings(), service_name="svc")

    assert isinstance(manager.logging, _FakeLoggingSink)
    assert isinstance(manager.error_tracking, _FakeErrorSink)
    assert isinstance(manager.tracing, _FakeTracingSink)
    assert isinstance(manager.metrics, _FakeMetricsSink)

    setup_order = [name for (event, name) in _FakeSink.events if event == "setup"]
    assert setup_order == ["_FakeLoggingSink", "_FakeErrorSink", "_FakeTracingSink", "_FakeMetricsSink"]


def test__observability_manager_configure__called__hands_each_sink_the_service_identity() -> None:
    redactor = KeyRedactor()
    manager = ObservabilityManager(ObsConfig(), redactor=redactor)
    manager.configure(_ConfiguredSettings(), service_name="orders")

    ctx = manager.logging.setup_ctx  # type: ignore[union-attr]
    assert ctx.service_name == "orders"
    assert ctx.app_version == "0.0.0-test"
    assert ctx.environment == "test-env"
    assert ctx.redactor is redactor


def test__observability_manager_configure__error_tracking_without_a_dsn__stays_null() -> None:
    settings = _ConfiguredSettings()

    class _NoDsn:
        dsn = None
        environment = "test"

    settings.error_tracking = _NoDsn()
    manager = ObservabilityManager(ObsConfig())
    manager.configure(settings, service_name="svc")
    assert isinstance(manager.error_tracking, NullErrorTrackingSink)


def test__observability_manager_configure__called_twice__sets_up_only_once() -> None:
    manager = ObservabilityManager(ObsConfig())
    manager.configure(_ConfiguredSettings(), service_name="svc")
    first_events = list(_FakeSink.events)
    manager.configure(_ConfiguredSettings(), service_name="svc")
    assert _FakeSink.events == first_events


def test__observability_manager_configure__unknown_backend_name__fails_fast() -> None:
    manager = ObservabilityManager(ObsConfig(logging="stdlib", metrics="datadog"))
    with pytest.raises(ValueError, match="Unknown metrics backend"):
        manager.configure(_ConfiguredSettings(), service_name="svc")


def test__observability_manager_configure__registered_third_party_backend__resolves_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A third-party backend registered by name is selectable like a built-in."""

    class _StatsdSink(_FakeMetricsSink):
        backend = "statsd"

    def resolve(concern: str, backend: str) -> type[Any]:
        assert (concern, backend) == ("metrics", "statsd")
        return _StatsdSink

    monkeypatch.setattr("servicewright.core.observability.manager.resolve_sink", resolve)

    manager = ObservabilityManager(ObsConfig(metrics="statsd", tracing=None, error_tracking=None, logging=None))
    manager.configure(_ConfiguredSettings(), service_name="svc")
    assert isinstance(manager.metrics, _StatsdSink)


# --------------------------------------------------------------------------- #
# Direct sink-instance injection (the registry-free DI path)
# --------------------------------------------------------------------------- #
def test__observability_manager__sink_instance_supplied__skips_the_registry_and_settings_gates() -> None:
    metrics_sink = _FakeMetricsSink()
    logging_sink = _FakeLoggingSink()
    manager = ObservabilityManager(metrics=metrics_sink, logging=logging_sink)

    # Every settings section is None: instances are still set up (their own
    # setup() decides what to do), by-name concerns stay Null.
    manager.configure(FakeSettings(), service_name="svc")

    assert manager.metrics is metrics_sink
    assert manager.logging is logging_sink
    assert isinstance(manager.tracing, NullTracingSink)
    assert metrics_sink.setup_ctx is not None

    manager.shutdown()
    shutdown_order = [name for (event, name) in _FakeSink.events if event == "shutdown"]
    assert shutdown_order == ["_FakeMetricsSink", "_FakeLoggingSink"]


def test__observability_manager__instance_and_config_name__prefers_the_instance() -> None:
    custom_metrics = _FakeMetricsSink()
    manager = ObservabilityManager(ObsConfig(), metrics=custom_metrics)
    manager.configure(_ConfiguredSettings(), service_name="svc")

    assert manager.metrics is custom_metrics
    # Other concerns still resolve by name through the registry.
    assert isinstance(manager.logging, _FakeLoggingSink)
    assert isinstance(manager.error_tracking, _FakeErrorSink)


def test__observability_manager_configure__environment_set_twice__prefers_the_top_level_one() -> None:
    settings = _ConfiguredSettings()
    settings.environment = "prod-eu"  # type: ignore[attr-defined]
    manager = ObservabilityManager(ObsConfig())
    manager.configure(settings, service_name="svc")
    assert manager.logging.setup_ctx.environment == "prod-eu"  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Shutdown
# --------------------------------------------------------------------------- #
def test__observability_manager_shutdown__sinks_active__tears_them_down_in_reverse_order() -> None:
    manager = ObservabilityManager(ObsConfig())
    manager.configure(_ConfiguredSettings(), service_name="svc")
    _FakeErrorSink.fail_on_shutdown = True

    manager.shutdown()  # must not raise despite the error sink failing

    shutdown_order = [name for (event, name) in _FakeSink.events if event == "shutdown"]
    assert shutdown_order == ["_FakeMetricsSink", "_FakeTracingSink", "_FakeErrorSink", "_FakeLoggingSink"]


def test__observability_manager_shutdown__never_configured__does_nothing() -> None:
    manager = ObservabilityManager(ObsConfig())
    manager.shutdown()
    assert _FakeSink.events == []


def test__observability_manager_shutdown__called_twice__tears_down_once() -> None:
    manager = ObservabilityManager(ObsConfig())
    manager.configure(_ConfiguredSettings(), service_name="svc")
    manager.shutdown()
    events_after_first = list(_FakeSink.events)
    manager.shutdown()
    assert _FakeSink.events == events_after_first


# --------------------------------------------------------------------------- #
# Registry (real resolve_sink, not the patched one)
# --------------------------------------------------------------------------- #
def test__sink_registry__unknown_backend__names_the_registered_ones() -> None:
    with pytest.raises(ValueError, match="registered backends"):
        resolve_sink("metrics", "no-such-backend")


def test__sink_registry__custom_sink_registered__resolves_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_mod, "_SINKS", dict(registry_mod._SINKS))
    register_sink("metrics", "custom", "servicewright.core.observability.null:NullMetricsSink")
    sink_class = resolve_sink("metrics", "custom")
    assert sink_class is NullMetricsSink


def test__sink_registry__malformed_import_target__raises() -> None:
    with pytest.raises(ValueError, match=r"module\.path:ClassName"):
        register_sink("metrics", "broken", "not-a-target")


def test__sink_registry__built_in_backend_names__resolve_to_real_classes() -> None:
    # The real registry entries must import (extras are installed in the dev env).
    for concern, backend in [
        ("metrics", "prometheus"),
        ("tracing", "otel"),
        ("error_tracking", "sentry"),
        ("logging", "structlog"),
        ("logging", "stdlib"),
    ]:
        sink_class = resolve_sink(concern, backend)
        assert sink_class.backend == backend  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Null seams
# --------------------------------------------------------------------------- #
def test__null_sinks__used_without_configuration__are_safe_no_ops() -> None:
    tracer = NullTracer()
    with tracer.start_as_current_span("op") as span:
        span.set_attribute("k", "v")
        span.record_exception(ValueError("x"))
    assert isinstance(span, NullSpan)

    NullCounter().inc(service="s", method="m")
    NullHistogram().observe(0.01, service="s", method="m")

    reporter = NullErrorReporter()
    reporter.capture_exception(ValueError("x"))
    reporter.add_breadcrumb("msg")
    reporter.set_tags(env="test")


# --------------------------------------------------------------------------- #
# KeyRedactor
# --------------------------------------------------------------------------- #
def test__key_redactor__nested_mapping__masks_every_sensitive_key() -> None:
    redactor = KeyRedactor()
    data = {
        "user": "alice",
        "password": "hunter2",
        "Authorization": "Bearer abc",
        "nested": {"api_key": "xyz", "ok": 1},
    }
    redacted = redactor(data)
    assert redacted["user"] == "alice"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["nested"]["api_key"] == "[REDACTED]"
    assert redacted["nested"]["ok"] == 1
    # The input is not mutated.
    assert data["password"] == "hunter2"


def test__key_redactor__custom_keys_and_mask__uses_them() -> None:
    redactor = KeyRedactor(sensitive_keys={"ssn"}, mask="***")
    assert redactor({"ssn": "1", "password": "x"}) == {"ssn": "***", "password": "x"}


# --------------------------------------------------------------------------- #
# Regression cover: the redactor must reach list-nested payloads
# --------------------------------------------------------------------------- #
@pytest.fixture
def redactor() -> KeyRedactor:
    return KeyRedactor(sensitive_keys={"password", "api_key", "ssn"})


@pytest.fixture
def sentry_shaped_event() -> dict[str, Any]:
    """A Sentry event: everything that matters hides inside lists."""
    return {
        "extra": {"password": "hunter2"},
        "exception": {
            "values": [
                {"stacktrace": {"frames": [{"vars": {"ssn": "123-45-6789", "safe": "keep"}}]}},
            ]
        },
        "breadcrumbs": {"values": [{"data": {"api_key": "sk-live-123"}}]},
    }


def test__key_redactor__flat_sensitive_key__is_masked(redactor: KeyRedactor) -> None:
    # Act
    result = redactor({"password": "hunter2", "safe": "keep"})

    # Assert
    assert result == {"password": MASK, "safe": "keep"}


def test__key_redactor__stack_frame_locals_inside_lists__are_masked(
    redactor: KeyRedactor,
    sentry_shaped_event: dict[str, Any],
) -> None:
    # Act
    result = redactor(sentry_shaped_event)

    # Assert
    frame_vars = result["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert frame_vars == {"ssn": MASK, "safe": "keep"}


def test__key_redactor__breadcrumb_data_inside_lists__is_masked(
    redactor: KeyRedactor,
    sentry_shaped_event: dict[str, Any],
) -> None:
    # Act
    result = redactor(sentry_shaped_event)

    # Assert
    assert result["breadcrumbs"]["values"][0]["data"] == {"api_key": MASK}


def test__key_redactor__tuple_of_mappings__is_masked(redactor: KeyRedactor) -> None:
    # Act
    result = redactor({"items": ({"password": "p"},)})

    # Assert
    assert result["items"] == ({"password": MASK},)


def test__key_redactor__called__does_not_mutate_the_original_payload(
    redactor: KeyRedactor,
    sentry_shaped_event: dict[str, Any],
) -> None:
    # Act
    redactor(sentry_shaped_event)

    # Assert
    assert sentry_shaped_event["extra"]["password"] == "hunter2"


def test__key_redactor__self_referencing_payload__does_not_recurse_forever(redactor: KeyRedactor) -> None:
    # Arrange
    payload: dict[str, Any] = {"password": "p"}
    payload["self"] = payload

    # Act
    result = redactor(payload)

    # Assert
    assert result["password"] == MASK


# --------------------------------------------------------------------------- #
# Regression cover: a second run of the same AppSpec must get live sinks
# --------------------------------------------------------------------------- #
def test__observability_manager__configure_after_shutdown__sets_the_sinks_up_again() -> None:
    # Arrange
    manager = ObservabilityManager(ObsConfig())
    manager.configure(_ConfiguredSettings(), service_name="svc")
    manager.shutdown()
    _FakeSink.events.clear()

    # Act
    manager.configure(_ConfiguredSettings(), service_name="svc")

    # Assert
    assert [event for event, _name in _FakeSink.events] == ["setup"] * 4


def test__observability_manager__shutdown__restores_the_null_sinks() -> None:
    # Arrange
    manager = ObservabilityManager(ObsConfig())
    manager.configure(_ConfiguredSettings(), service_name="svc")

    # Act
    manager.shutdown()

    # Assert
    assert isinstance(manager.logging, NullLoggingSink)
    assert isinstance(manager.metrics, NullMetricsSink)
    assert isinstance(manager.tracing, NullTracingSink)
    assert isinstance(manager.error_tracking, NullErrorTrackingSink)


def test__observability_manager__second_run_of_one_spec__reports_a_live_sink_again() -> None:
    # Arrange
    manager = ObservabilityManager(ObsConfig())

    # Act
    manager.configure(_ConfiguredSettings(), service_name="svc")
    manager.shutdown()
    manager.configure(_ConfiguredSettings(), service_name="svc")

    # Assert
    assert not isinstance(manager.logging, NullLoggingSink)
