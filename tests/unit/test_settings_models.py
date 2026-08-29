"""The shipped settings models are the settings contract, written down once.

Regression cover for issue #22: the models the docs told users to transcribe
were never shipped, so every copy drifted in its own way — a required
``service_name``, guessed defaults, a hand-rolled ``Literal`` level. These pin
that the shipped models carry the sinks' own fallbacks, load from the
environment the documented way and stay as narrowable as a hand-written class.
"""

from __future__ import annotations

import importlib
import logging
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from servicewright.adapters import settings as settings_pkg
from servicewright.adapters.observability import OtelTracingSink, SentryErrorTrackingSink, StdlibLoggingSink
from servicewright.adapters.observability._errors import sentry as sentry_mod
from servicewright.adapters.observability._tracing import otel as otel_mod
from servicewright.adapters.settings import (
    BaseServiceSettings,
    ErrorTrackingSettings,
    LoggingSettings,
    MetricsSettings,
    TracingSettings,
)
from servicewright.core.observability import ObsConfig, ObservabilityManager, ObsSetupContext
from servicewright.core.observability.null import NullErrorTrackingSink, NullTracingSink

pytestmark = pytest.mark.unit

_DSN = "https://key@sentry.invalid/1"


def _ctx(settings: Any, service_name: str = "svc") -> ObsSetupContext:
    return ObsSetupContext(service_name=service_name, app_version="1.2.3", environment="test-env", settings=settings)


class _Settings(BaseServiceSettings):
    """The reporter's subclass: a config of its own, a version, nothing transcribed."""

    model_config = SettingsConfigDict(env_file=".env")

    app_version: str = "1.4.0"
    database_dsn: str


# --------------------------------------------------------------------------- #
# The reporter's scenario
# --------------------------------------------------------------------------- #
def test__base_service_settings__nested_environment_variables__populate_the_sections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    # Arrange: LOGGING__LEVEL=DEBUG TRACING__COLLECTOR_URL=otel:4317 python main.py, with a .env
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DATABASE_DSN=postgresql://db.invalid/app\n")
    monkeypatch.setenv("LOGGING__LEVEL", "DEBUG")
    monkeypatch.setenv("TRACING__COLLECTOR_URL", "otel:4317")

    # Act
    settings = _Settings()

    # Assert
    assert settings.get_app_version() == "1.4.0"
    assert settings.database_dsn == "postgresql://db.invalid/app"
    assert settings.logging is not None
    assert settings.logging.level == "DEBUG"
    # A section that defaults to None is built from its nested variables the moment one is set.
    assert settings.tracing is not None
    assert settings.tracing.collector_url == "otel:4317"
    assert settings.tracing.service_name == ""  # not required: falls back to AppSpec.service_name


def test__base_service_settings__defaults__are_the_inert_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    settings = BaseServiceSettings()

    assert settings.logging == LoggingSettings(level="INFO", use_json=True)
    assert settings.metrics == MetricsSettings(enabled=False, host="0.0.0.0", port=9090, prefix=None)
    assert settings.error_tracking == ErrorTrackingSettings(dsn=None, environment="")
    assert settings.tracing is None
    assert (settings.environment, settings.get_app_version()) == ("local", "0.0.0")


def test__base_service_settings__defaults__configure_logging_and_metrics_only() -> None:
    manager = ObservabilityManager(ObsConfig(logging="stdlib"))

    try:
        manager.configure(BaseServiceSettings(), service_name="svc")

        assert isinstance(manager.logging, StdlibLoggingSink)
        assert getattr(manager.metrics, "backend", None) == "prometheus"
        assert isinstance(manager.tracing, NullTracingSink)
        assert isinstance(manager.error_tracking, NullErrorTrackingSink)
    finally:
        manager.shutdown()


# --------------------------------------------------------------------------- #
# Defaults are the sinks' own fallbacks
# --------------------------------------------------------------------------- #
def test__tracing_settings__service_name_left_empty__otel_resource_falls_back_to_the_spec_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(otel_mod.trace, "set_tracer_provider", installed.append)
    sink = OtelTracingSink()

    try:
        sink.setup(_ctx(BaseServiceSettings(tracing=TracingSettings()), service_name="orders"))
    finally:
        sink.shutdown()

    assert installed[0].resource.attributes["service.name"] == "orders"


def test__tracing_settings__defaults__match_what_the_otel_sink_assumes_for_a_bare_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[Any] = []
    monkeypatch.setattr(otel_mod.trace, "set_tracer_provider", installed.append)

    for section in (TracingSettings(), SimpleNamespace()):
        sink = OtelTracingSink()
        try:
            sink.setup(_ctx(SimpleNamespace(tracing=section)))
        finally:
            sink.shutdown()

    shipped, bare = installed
    assert shipped.resource.attributes == bare.resource.attributes
    assert shipped.sampler.get_description() == bare.sampler.get_description()
    assert len(shipped._active_span_processor._span_processors) == len(bare._active_span_processor._span_processors)


def test__error_tracking_settings__defaults__match_what_the_sentry_sink_assumes_for_a_bare_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_mod.sentry_sdk, "init", lambda **kwargs: init_calls.append(kwargs))
    monkeypatch.setattr(sentry_mod.sentry_sdk, "flush", lambda **kwargs: None)

    for section in (ErrorTrackingSettings(dsn=_DSN), SimpleNamespace(dsn=_DSN)):
        sink = SentryErrorTrackingSink()
        try:
            sink.setup(_ctx(SimpleNamespace(error_tracking=section)))
        finally:
            sink.shutdown()

    shipped, bare = init_calls
    shipped.pop("before_send"), bare.pop("before_send")
    assert shipped == bare
    assert shipped["environment"] == "test-env"  # the empty section-level environment defers to the context


def test__logging_settings__defaults__match_what_the_stdlib_sink_assumes_for_a_bare_section() -> None:
    seen: list[tuple[int, type[logging.Formatter]]] = []

    for section in (LoggingSettings(), SimpleNamespace()):
        sink = StdlibLoggingSink()
        try:
            sink.setup(_ctx(SimpleNamespace(logging=section)))
            handler = sink._handler
            assert handler is not None and handler.formatter is not None
            seen.append((logging.getLogger().level, type(handler.formatter)))
        finally:
            sink.shutdown()

    assert seen[0] == seen[1] == (logging.INFO, seen[0][1])


def test__error_tracking_settings__environment_left_empty__sentry_gets_the_top_level_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sentry_mod.sentry_sdk, "init", lambda **kwargs: init_calls.append(kwargs))
    monkeypatch.setattr(sentry_mod.sentry_sdk, "flush", lambda **kwargs: None)
    settings = BaseServiceSettings(environment="prod-eu", error_tracking=ErrorTrackingSettings(dsn=_DSN))
    manager = ObservabilityManager(ObsConfig(logging=None, tracing=None, metrics=None))

    try:
        manager.configure(settings, service_name="svc")
    finally:
        manager.shutdown()

    assert init_calls[0]["environment"] == "prod-eu"
    assert init_calls[0]["release"] == "0.0.0"


# --------------------------------------------------------------------------- #
# Validation instead of silent fallbacks
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("given", ["debug", "Debug", "DEBUG"])
def test__logging_settings__level_in_any_case__is_normalised(given: str) -> None:
    assert LoggingSettings(level=given).level == "DEBUG"  # type: ignore[arg-type]


def test__logging_settings__unknown_level__is_rejected_at_load() -> None:
    with pytest.raises(ValidationError, match="level"):
        LoggingSettings(level="verbose")  # type: ignore[arg-type]  # the sinks would silently make this INFO


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (TracingSettings, "sample_ratio", 1.5),
        (TracingSettings, "sample_ratio", -0.1),
        (ErrorTrackingSettings, "traces_sample_rate", 2.0),
        (ErrorTrackingSettings, "profiles_sample_rate", -1.0),
        (MetricsSettings, "port", 70000),
    ],
)
def test__section_models__value_outside_what_the_backend_accepts__is_rejected_at_load(
    model: type[Any], field: str, value: float
) -> None:
    with pytest.raises(ValidationError, match=field):
        model(**{field: value})


# --------------------------------------------------------------------------- #
# As overridable as a hand-written class
# --------------------------------------------------------------------------- #
def test__base_service_settings__subclass_sets_a_section_to_none__that_concern_is_off() -> None:
    class Settings(BaseServiceSettings):
        metrics: None = None
        tracing: TracingSettings = TracingSettings(sample_ratio=0.1)

    settings = Settings()

    assert settings.metrics is None
    assert settings.tracing.sample_ratio == 0.1
    assert settings.logging is not None  # untouched sections keep their defaults


def test__base_service_settings__subclass_model_config__keeps_the_nested_delimiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Settings(BaseServiceSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")

    monkeypatch.setenv("APP_LOGGING__LEVEL", "warning")
    monkeypatch.setenv("APP_ERROR_TRACKING__DSN", _DSN)

    settings = Settings()

    assert settings.logging is not None
    assert settings.logging.level == "WARNING"
    assert settings.error_tracking is not None
    assert settings.error_tracking.dsn == _DSN


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #
def test__settings_package__all__is_the_five_models_sorted() -> None:
    assert settings_pkg.__all__ == sorted(settings_pkg.__all__)
    assert set(settings_pkg.__all__) == {
        "BaseServiceSettings",
        "ErrorTrackingSettings",
        "LoggingSettings",
        "MetricsSettings",
        "TracingSettings",
    }


def test__settings_package__extra_missing__raises_the_install_hint() -> None:
    with patch.dict("sys.modules", {"pydantic_settings": None}):
        for name in [name for name in sys.modules if name.startswith("servicewright.adapters.settings")]:
            sys.modules.pop(name)

        with pytest.raises(ImportError, match=r"servicewright\[settings\]"):
            importlib.import_module("servicewright.adapters.settings")
