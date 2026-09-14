"""The FastAPI entrypoint's configuration as settings, written down once.

Regression cover for issue #61: ``servicewright[settings]`` stopped at the four
observability sections, so every HTTP service carried a hand-written transcription
of ``HttpConfig``, ``HealthConfig`` and ``MiddlewareConfig`` and a block copying
values from one into the other. These pin that the shipped models name exactly the
dataclass fields, carry their defaults, load from the environment the documented
way, and type only the uvicorn knobs the runner actually honours.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterator
from dataclasses import fields
from typing import Any

import pytest
import uvicorn
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from servicewright.adapters import fastapi as fastapi_pkg
from servicewright.adapters.fastapi import (
    CorrelationIdMiddlewareConfig,
    CorrelationIdMiddlewareSettings,
    CORSMiddlewareConfig,
    CORSMiddlewareSettings,
    FastApiEntrypoint,
    GZipMiddlewareConfig,
    GZipMiddlewareSettings,
    HealthConfig,
    HealthSettings,
    HttpConfig,
    HttpServerSettings,
    LoggingMiddlewareConfig,
    LoggingMiddlewareSettings,
    MiddlewareConfig,
    MiddlewareSettings,
    UvicornSettings,
)
from servicewright.adapters.fastapi import settings as settings_module
from servicewright.adapters.settings import BaseServiceSettings

pytestmark = pytest.mark.unit


class _Settings(BaseServiceSettings):
    """The reporter's settings: ``BaseServiceSettings`` plus the one line that adds the HTTP section."""

    server: HttpServerSettings = HttpServerSettings()


class _Middleware:
    """A custom ASGI middleware class, as far as ``MiddlewareConfig.custom`` is concerned."""


@pytest.fixture
def uvicorn_loggers_restored() -> Iterator[None]:
    """``uvicorn.Config(...)`` configures uvicorn's loggers as a side effect; undo it."""
    loggers = [logging.getLogger(name) for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")]
    saved = [(lg, lg.level, list(lg.handlers), lg.propagate) for lg in loggers]
    yield
    for lg, level, handlers, propagate in saved:
        lg.setLevel(level)
        lg.handlers = handlers
        lg.propagate = propagate


# --------------------------------------------------------------------------- #
# The reporter's scenario
# --------------------------------------------------------------------------- #
def test__http_server_settings__the_hand_built_block__equals_to_config_field_for_field() -> None:
    # Arrange: the values nine services carried from their own models into HttpConfig by hand
    server = HttpServerSettings(
        host="127.0.0.1",
        port=8080,
        graceful_timeout=15.0,
        health={"enabled": True, "liveness_path": "/livez", "readiness_path": "/readyz"},  # type: ignore[arg-type]
        uvicorn={"proxy_headers": True, "forwarded_allow_ips": "*", "timeout_keep_alive": 30},  # type: ignore[arg-type]
    )

    # Act: what their http.py did
    by_hand = HttpConfig(
        host=server.host,
        port=server.port,
        graceful_timeout=float(server.graceful_timeout),
        version="1.4.0",
        health=HealthConfig(
            enabled=server.health.enabled,
            liveness_path=server.health.liveness_path,
            readiness_path=server.health.readiness_path,
        ),
        uvicorn_kwargs={"proxy_headers": True, "forwarded_allow_ips": "*", "timeout_keep_alive": 30},
    )

    # Assert
    assert server.to_config(version="1.4.0") == by_hand


def test__base_service_settings__nested_server_variables__reach_the_config_and_uvicorn(
    monkeypatch: pytest.MonkeyPatch, uvicorn_loggers_restored: None
) -> None:
    # Arrange: SERVER__* in the pod, through BaseServiceSettings' own "__" nesting
    monkeypatch.setenv("SERVER__HOST", "127.0.0.1")
    monkeypatch.setenv("SERVER__PORT", "8080")
    monkeypatch.setenv("SERVER__GRACEFUL_TIMEOUT", "15")
    monkeypatch.setenv("SERVER__DOCS_URL", "/docs")
    monkeypatch.setenv("SERVER__HEALTH__READINESS_PATH", "/readyz")
    monkeypatch.setenv("SERVER__UVICORN__TIMEOUT_KEEP_ALIVE", "30")
    monkeypatch.setenv("SERVER__UVICORN__PROXY_HEADERS", "false")
    monkeypatch.setenv("SERVER__UVICORN__FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setenv("SERVER__UVICORN__LIMIT_CONCURRENCY", "512")
    monkeypatch.setenv("SERVER__UVICORN__ACCESS_LOG", "false")
    monkeypatch.setenv("SERVER__UVICORN_KWARGS", '{"ws_max_size": 1024}')
    monkeypatch.setenv("SERVER__MIDDLEWARES__GZIP__ENABLED", "false")
    monkeypatch.setenv("SERVER__MIDDLEWARES__CORS__ALLOW_ORIGINS", '["https://app.example.com"]')

    # Act
    settings = _Settings()
    config = settings.server.to_config(version=settings.app_version)
    middlewares = settings.server.middlewares.to_config()
    entrypoint = FastApiEntrypoint(config=config, middlewares=middlewares)
    uvicorn_config = entrypoint._runner._build_server(app=object()).config

    # Assert: the dataclasses
    assert config == HttpConfig(
        host="127.0.0.1",
        port=8080,
        graceful_timeout=15.0,
        docs_url="/docs",
        health=HealthConfig(readiness_path="/readyz"),
        uvicorn_kwargs={
            "proxy_headers": False,
            "forwarded_allow_ips": "*",
            "timeout_keep_alive": 30,
            "limit_concurrency": 512,
            "access_log": False,
            "ws_max_size": 1024,
        },
    )
    assert middlewares == MiddlewareConfig(
        gzip=GZipMiddlewareConfig(enabled=False),
        cors=CORSMiddlewareConfig(allow_origins=["https://app.example.com"]),
    )
    # Assert: the uvicorn.Config the runner builds carries every value under its own name, typed
    assert uvicorn_config.timeout_keep_alive == 30
    assert uvicorn_config.proxy_headers is False
    assert uvicorn_config.forwarded_allow_ips == "*"
    assert uvicorn_config.limit_concurrency == 512
    assert uvicorn_config.access_log is False
    assert uvicorn_config.ws_max_size == 1024
    assert uvicorn_config.timeout_graceful_shutdown == 15


def test__base_service_settings__bare_variables__cannot_reach_the_server_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: what an unrelated process in the pod, or the platform, might export
    for name, value in {"HOST": "10.0.0.1", "PORT": "1", "WORKERS": "8", "TIMEOUT_KEEP_ALIVE": "1"}.items():
        monkeypatch.setenv(name, value)
    for name, value in {"ENABLED": "false", "ALLOW_ORIGINS": '["*"]', "READINESS_PATH": "/x"}.items():
        monkeypatch.setenv(name, value)

    class LeakySection(BaseSettings):
        port: int = 8000

    # Act / Assert
    assert LeakySection().port == 1  # what a BaseSettings section would do
    settings = _Settings()
    assert settings.server.to_config() == HttpConfig()
    assert settings.server.middlewares.to_config() == MiddlewareConfig()


def test__settings_models__none__is_a_base_settings() -> None:
    for name in settings_module.__all__:
        assert not issubclass(getattr(settings_module, name), BaseSettings), name


# --------------------------------------------------------------------------- #
# The models are the dataclasses, written once
# --------------------------------------------------------------------------- #
# Every (model, dataclass) pair, with the members one side has and the other has
# not, each with its reason.
_PAIRS: list[tuple[type[Any], type[Any], dict[str, str], dict[str, str]]] = [
    (
        HttpServerSettings,
        HttpConfig,
        {
            "allow_ephemeral_port": "validation only: port=0 has to be said on purpose",
            "uvicorn": "the typed knobs behind HttpConfig.uvicorn_kwargs",
            "middlewares": "MiddlewareConfig, the entrypoint's own argument, not an HttpConfig field",
        },
        {"version": "the argument of to_config(): the application's version, which settings carry once already"},
    ),
    (HealthSettings, HealthConfig, {}, {}),
    (
        MiddlewareSettings,
        MiddlewareConfig,
        {},
        {
            "unit_scope": "whether the DI integration owns the request scope: code, a keyword of to_config()",
            "context_setters": "ContextSetter objects: code, a keyword of to_config()",
            "custom": "middleware classes and their kwargs: code, a keyword of to_config()",
        },
    ),
    (LoggingMiddlewareSettings, LoggingMiddlewareConfig, {}, {}),
    (CorrelationIdMiddlewareSettings, CorrelationIdMiddlewareConfig, {}, {}),
    (GZipMiddlewareSettings, GZipMiddlewareConfig, {}, {}),
    (CORSMiddlewareSettings, CORSMiddlewareConfig, {}, {}),
]


@pytest.mark.parametrize(
    ("model", "dataclass", "model_only", "dataclass_only"), _PAIRS, ids=[pair[0].__name__ for pair in _PAIRS]
)
def test__every_model__names_the_dataclass_fields_or_lists_the_difference_with_its_reason(
    model: type[Any], dataclass: type[Any], model_only: dict[str, str], dataclass_only: dict[str, str]
) -> None:
    dataclass_fields = {field.name for field in fields(dataclass)}
    assert model_only.keys() <= set(model.model_fields)
    assert dataclass_only.keys() <= dataclass_fields
    assert set(model.model_fields) - model_only.keys() == dataclass_fields - dataclass_only.keys()


def test__defaults__are_the_dataclass_defaults() -> None:
    assert HttpServerSettings().to_config() == HttpConfig()
    assert MiddlewareSettings().to_config() == MiddlewareConfig()


def test__http_server_settings__every_field_set__carries_across() -> None:
    server = HttpServerSettings(
        host="127.0.0.1",
        port=8080,
        graceful_timeout=15.0,
        title="Orders",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        redirect_slashes=True,
        fastapi_kwargs={"debug": True},
        uvicorn=UvicornSettings(timeout_keep_alive=30),
        uvicorn_kwargs={"ws_max_size": 1024},
        health=HealthSettings(enabled=False, liveness_path="/livez", readiness_path="/readyz"),
    )

    assert server.to_config(version="1.4.0") == HttpConfig(
        host="127.0.0.1",
        port=8080,
        graceful_timeout=15.0,
        title="Orders",
        version="1.4.0",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        redirect_slashes=True,
        fastapi_kwargs={"debug": True},
        uvicorn_kwargs={"timeout_keep_alive": 30, "ws_max_size": 1024},
        health=HealthConfig(enabled=False, liveness_path="/livez", readiness_path="/readyz"),
    )


def test__middleware_settings__every_field_set__carries_across_and_the_keywords_fill_the_rest() -> None:
    setters = [object()]
    middlewares = MiddlewareSettings(
        context=False,
        sentry=False,
        processing_time=False,
        logging=LoggingMiddlewareSettings(enabled=False, ignored_paths=["/internal"]),
        correlation_id=CorrelationIdMiddlewareSettings(enabled=False, header_name="X-Correlation-ID"),
        gzip=GZipMiddlewareSettings(enabled=False, minimum_size=1),
        cors=CORSMiddlewareSettings(
            enabled=False,
            allow_origins=["https://app.example.com"],
            allow_credentials=True,
            allow_methods=["GET"],
            allow_headers=["X-A"],
            expose_headers=["X-B"],
            max_age=1,
        ),
    )

    config = middlewares.to_config(unit_scope=False, context_setters=setters, custom=[(_Middleware, {"option": 1})])

    assert config == MiddlewareConfig(
        unit_scope=False,
        context=False,
        sentry=False,
        processing_time=False,
        context_setters=setters,
        logging=LoggingMiddlewareConfig(enabled=False, ignored_paths=["/internal"]),
        correlation_id=CorrelationIdMiddlewareConfig(enabled=False, header_name="X-Correlation-ID"),
        gzip=GZipMiddlewareConfig(enabled=False, minimum_size=1),
        cors=CORSMiddlewareConfig(
            enabled=False,
            allow_origins=["https://app.example.com"],
            allow_credentials=True,
            allow_methods=["GET"],
            allow_headers=["X-A"],
            expose_headers=["X-B"],
            max_age=1,
        ),
        custom=[(_Middleware, {"option": 1})],
    )


def test__to_config__called_twice__shares_no_dict_or_list_between_the_configs() -> None:
    server = HttpServerSettings(fastapi_kwargs={"debug": True})
    first, second = server.to_config(), server.to_config()
    first.fastapi_kwargs["lifespan"] = object()
    first.uvicorn_kwargs["ws_max_size"] = 1
    server.middlewares.to_config().cors.allow_origins.append("https://leak.example.com")

    assert second.fastapi_kwargs == {"debug": True}
    assert second.uvicorn_kwargs == {}
    assert server.middlewares.to_config().cors.allow_origins == []


# --------------------------------------------------------------------------- #
# The uvicorn knobs
# --------------------------------------------------------------------------- #
# Every typed field set away from uvicorn's default.
_UVICORN_VALUES: dict[str, Any] = {
    "proxy_headers": False,
    "forwarded_allow_ips": ["10.0.0.1"],
    "root_path": "/api",
    "server_header": False,
    "date_header": False,
    "timeout_keep_alive": 30,
    "backlog": 4096,
    "limit_concurrency": 512,
    "limit_max_requests": 10_000,
    "h11_max_incomplete_event_size": 32 * 1024,
    "access_log": False,
    "log_level": "warning",
    "ssl_keyfile": "server.key",
    "ssl_certfile": "server.crt",
    "ssl_keyfile_password": "secret",
    "ssl_ca_certs": "ca.crt",
    "ssl_cert_reqs": 2,
    "ssl_ciphers": "ECDHE+AESGCM",
}

# uvicorn.Config parameters a reader would look for here, each with why it is not a field.
_NOT_TYPED = {
    "workers": "read only by uvicorn.run()'s multiprocess supervisor; Server.serve() is single-process",
    "host": "HttpServerSettings.host",
    "port": "HttpServerSettings.port",
    "timeout_graceful_shutdown": "HttpServerSettings.graceful_timeout",
    "log_config": "the Host owns logging; the runner passes None",
    "loop": "run_sync(loop=...) owns the loop; Server.serve() runs in the current one",
    "reload": "the development reloader, which needs uvicorn.run() and an import string",
    "uds": "the runner binds its own TCP socket",
    "fd": "the runner binds its own TCP socket",
    "env_file": "this is the environment layer",
}


def test__uvicorn_settings__every_field__is_a_uvicorn_config_parameter_landing_under_its_own_name(
    uvicorn_loggers_restored: None,
) -> None:
    parameters = inspect.signature(uvicorn.Config).parameters
    assert set(_UVICORN_VALUES) == set(UvicornSettings.model_fields)
    assert set(UvicornSettings.model_fields) <= set(parameters)

    config = uvicorn.Config(object(), log_config=None, **UvicornSettings(**_UVICORN_VALUES).to_kwargs())

    for name, value in _UVICORN_VALUES.items():
        assert getattr(config, name) == value, name


def test__uvicorn_settings__knobs_the_runner_ignores_or_owns__are_not_fields_and_are_listed_with_a_reason() -> None:
    parameters = inspect.signature(uvicorn.Config).parameters
    assert _NOT_TYPED.keys() <= set(parameters)
    assert not _NOT_TYPED.keys() & set(UvicornSettings.model_fields)


def test__uvicorn_settings__unset_fields__stay_out_of_the_kwargs() -> None:
    assert UvicornSettings().to_kwargs() == {}
    assert UvicornSettings(timeout_keep_alive=30).to_kwargs() == {"timeout_keep_alive": 30}
    assert HttpServerSettings().to_config().uvicorn_kwargs == {}


def test__http_server_settings__uvicorn_kwargs__win_over_the_typed_section_on_a_collision() -> None:
    server = HttpServerSettings(
        uvicorn=UvicornSettings(timeout_keep_alive=30), uvicorn_kwargs={"timeout_keep_alive": 5}
    )
    assert server.to_config().uvicorn_kwargs == {"timeout_keep_alive": 5}


@pytest.mark.parametrize("given", ["warning", "Warning", "WARNING"])
def test__uvicorn_settings__log_level_in_any_case__is_normalised(given: str) -> None:
    assert UvicornSettings(log_level=given).to_kwargs() == {"log_level": "warning"}  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Validation instead of silent fallbacks
# --------------------------------------------------------------------------- #
def test__http_server_settings__port_zero__rejected_naming_the_flag() -> None:
    # 0 only ever arrives from configuration, and a pod on an ephemeral port is one no probe reaches.
    with pytest.raises(ValidationError, match=r"ephemeral port.*allow_ephemeral_port=True"):
        HttpServerSettings(port=0)


def test__http_server_settings__port_zero_with_allow_ephemeral_port__accepted() -> None:
    assert HttpServerSettings(port=0, allow_ephemeral_port=True).to_config().port == 0


@pytest.mark.parametrize("allow_ephemeral_port", [False, True])
def test__http_server_settings__fixed_port__unaffected_by_allow_ephemeral_port(allow_ephemeral_port: bool) -> None:
    assert HttpServerSettings(port=8000, allow_ephemeral_port=allow_ephemeral_port).to_config().port == 8000


def test__base_service_settings__server_port_zero_from_environment__behaves_as_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVER__PORT", "0")

    with pytest.raises(ValidationError, match=r"allow_ephemeral_port=True"):
        _Settings()

    monkeypatch.setenv("SERVER__ALLOW_EPHEMERAL_PORT", "true")
    assert _Settings().server.port == 0


def test__cors_middleware_settings__credentials_with_wildcard_origin__rejected_at_load_with_the_field_path() -> None:
    with pytest.raises(ValidationError, match=r"allow_credentials=True"):
        CORSMiddlewareSettings(allow_credentials=True, allow_origins=["*"])

    with pytest.raises(ValidationError, match=r"server\.middlewares\.cors"):
        _Settings(server={"middlewares": {"cors": {"allow_credentials": True, "allow_origins": ["*"]}}})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (HttpServerSettings, "port", 70000),
        (UvicornSettings, "limit_concurrency", 0),
        (UvicornSettings, "ssl_cert_reqs", 3),
        (UvicornSettings, "log_level", "verbose"),
        (GZipMiddlewareSettings, "minimum_size", -1),
        (CORSMiddlewareSettings, "max_age", -1),
    ],
)
def test__section_models__value_outside_what_the_server_accepts__is_rejected_at_load(
    model: type[Any], field: str, value: Any
) -> None:
    with pytest.raises(ValidationError, match=field):
        model(**{field: value})


# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #
def test__settings_module__all__is_the_eight_models_sorted_and_re_exported_by_the_adapter() -> None:
    assert settings_module.__all__ == sorted(settings_module.__all__)
    assert set(settings_module.__all__) == {
        "CORSMiddlewareSettings",
        "CorrelationIdMiddlewareSettings",
        "GZipMiddlewareSettings",
        "HealthSettings",
        "HttpServerSettings",
        "LoggingMiddlewareSettings",
        "MiddlewareSettings",
        "UvicornSettings",
    }
    assert set(settings_module.__all__) <= set(fastapi_pkg.__all__)


def test__settings_config_dict__on_the_parent__keeps_reaching_the_section(monkeypatch: pytest.MonkeyPatch) -> None:
    class Settings(BaseServiceSettings):
        model_config = SettingsConfigDict(env_prefix="APP_")

        server: HttpServerSettings = HttpServerSettings()

    monkeypatch.setenv("APP_SERVER__PORT", "8080")

    assert Settings().server.port == 8080
