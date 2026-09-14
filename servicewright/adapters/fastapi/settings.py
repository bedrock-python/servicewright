"""The FastAPI entrypoint's configuration as settings models.

One pydantic model per config dataclass — :class:`HttpConfig`,
:class:`HealthConfig`, :class:`MiddlewareConfig` and the four middleware configs
under it — with the same field names and the same defaults, and ``to_config()``
on each, so the translation from an environment-loaded settings object into the
dataclasses the entrypoint takes is written here, once, instead of in every
service.

Every class here is a plain ``BaseModel``, never a ``BaseSettings``: a section
is reachable only through the settings object you nest it in, so a bare ``PORT``
or ``HOST`` in a pod cannot reach it. Nest :class:`HttpServerSettings` in your
settings — :class:`~servicewright.adapters.settings.BaseServiceSettings` or a
``BaseSettings`` of your own — and build the entrypoint from it::

    from servicewright.adapters.fastapi import FastApiEntrypoint, HttpServerSettings
    from servicewright.adapters.settings import BaseServiceSettings


    class Settings(BaseServiceSettings):
        server: HttpServerSettings = HttpServerSettings()


    settings = Settings()  # SERVER__PORT=8080 SERVER__UVICORN__TIMEOUT_KEEP_ALIVE=30
    http = FastApiEntrypoint(
        config=settings.server.to_config(version=settings.app_version),
        middlewares=settings.server.middlewares.to_config(),
        routers=(router,),
    )

The dataclass members that are code, not environment, are arguments of
``to_config()`` rather than fields: ``version`` on
:meth:`HttpServerSettings.to_config` (the application's version, which the
settings object carries once already), and ``unit_scope``, ``context_setters``
and ``custom`` on :meth:`MiddlewareSettings.to_config`. The uvicorn knobs are
typed fields on :class:`UvicornSettings`, ``None`` until set and forwarded only
then, so ``HttpConfig.uvicorn_kwargs`` holds exactly what the environment said.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from ._imports import BaseModel, Field, field_validator, model_validator
from .config import (
    DEFAULT_CORRELATION_ID_HEADER,
    DEFAULT_CORS_METHODS,
    DEFAULT_DOCS_URL,
    DEFAULT_GRACEFUL_TIMEOUT_SECONDS,
    DEFAULT_HTTP_HOST,
    DEFAULT_HTTP_PORT,
    DEFAULT_IGNORED_PATHS,
    DEFAULT_LIVENESS_PATH,
    DEFAULT_OPENAPI_URL,
    DEFAULT_READINESS_PATH,
    DEFAULT_REDOC_URL,
    CorrelationIdMiddlewareConfig,
    CORSMiddlewareConfig,
    GZipMiddlewareConfig,
    HealthConfig,
    HttpConfig,
    LoggingMiddlewareConfig,
    MiddlewareConfig,
)


class HealthSettings(BaseModel):
    """``HealthConfig`` as settings: the ``/system`` probe routes."""

    enabled: bool = Field(default=True, description="Serve the liveness and readiness routes")
    liveness_path: str = Field(default=DEFAULT_LIVENESS_PATH, description="Liveness probe path")
    readiness_path: str = Field(default=DEFAULT_READINESS_PATH, description="Readiness probe path")

    def to_config(self) -> HealthConfig:
        return HealthConfig(enabled=self.enabled, liveness_path=self.liveness_path, readiness_path=self.readiness_path)


class LoggingMiddlewareSettings(BaseModel):
    """``LoggingMiddlewareConfig`` as settings; ``ignored_paths`` takes a JSON list."""

    enabled: bool = Field(default=True, description="One structured log line per request")
    ignored_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_IGNORED_PATHS), description="Paths left out of request logging"
    )

    def to_config(self) -> LoggingMiddlewareConfig:
        return LoggingMiddlewareConfig(enabled=self.enabled, ignored_paths=list(self.ignored_paths))


class CorrelationIdMiddlewareSettings(BaseModel):
    """``CorrelationIdMiddlewareConfig`` as settings: how the request id is echoed."""

    enabled: bool = Field(default=True, description="Echo the request id in a response header")
    header_name: str = Field(default=DEFAULT_CORRELATION_ID_HEADER, description="The header it is echoed in")

    def to_config(self) -> CorrelationIdMiddlewareConfig:
        return CorrelationIdMiddlewareConfig(enabled=self.enabled, header_name=self.header_name)


class GZipMiddlewareSettings(BaseModel):
    """``GZipMiddlewareConfig`` as settings."""

    enabled: bool = Field(default=True, description="Compress responses")
    minimum_size: int = Field(default=1000, ge=0, description="Smallest response body compressed, bytes")

    def to_config(self) -> GZipMiddlewareConfig:
        return GZipMiddlewareConfig(enabled=self.enabled, minimum_size=self.minimum_size)


class CORSMiddlewareSettings(BaseModel):
    """``CORSMiddlewareConfig`` as settings; the lists take JSON lists (``ALLOW_ORIGINS='["https://app.example.com"]'``)."""

    enabled: bool = Field(default=True, description="Install Starlette's CORSMiddleware")
    allow_origins: list[str] = Field(default_factory=list, description="Origins allowed; empty allows none")
    allow_credentials: bool = Field(default=False, description="Allow cookies and Authorization across origins")
    allow_methods: list[str] = Field(default_factory=lambda: list(DEFAULT_CORS_METHODS), description="Methods allowed")
    allow_headers: list[str] = Field(default_factory=lambda: ["*"], description="Request headers allowed")
    expose_headers: list[str] = Field(default_factory=list, description="Response headers exposed to the browser")
    max_age: int = Field(default=600, ge=0, description="Seconds a preflight response may be cached")

    @model_validator(mode="after")
    def _validate(self) -> CORSMiddlewareSettings:
        # The dataclass owns the credentials-with-wildcard rule; raising it here puts the field path on it.
        self.to_config()
        return self

    def to_config(self) -> CORSMiddlewareConfig:
        return CORSMiddlewareConfig(
            enabled=self.enabled,
            allow_origins=list(self.allow_origins),
            allow_credentials=self.allow_credentials,
            allow_methods=list(self.allow_methods),
            allow_headers=list(self.allow_headers),
            expose_headers=list(self.expose_headers),
            max_age=self.max_age,
        )


class MiddlewareSettings(BaseModel):
    """``MiddlewareConfig`` as settings: the toggles and the four configured middlewares.

    ``unit_scope``, ``context_setters`` and ``custom`` are not fields: whether the
    DI integration owns the request scope and which middleware classes to append
    is decided by the code that wires them, so they are the keyword arguments of
    :meth:`to_config`, with the dataclass defaults.
    """

    context: bool = Field(default=True, description="ContextMiddleware: correlation ids, bound and echoed")
    sentry: bool = Field(default=True, description="SentryMiddleware: enrich the Sentry scope from the request")
    processing_time: bool = Field(default=True, description="X-Process-Time response header")
    logging: LoggingMiddlewareSettings = Field(default_factory=LoggingMiddlewareSettings)
    correlation_id: CorrelationIdMiddlewareSettings = Field(default_factory=CorrelationIdMiddlewareSettings)
    gzip: GZipMiddlewareSettings = Field(default_factory=GZipMiddlewareSettings)
    cors: CORSMiddlewareSettings = Field(default_factory=CORSMiddlewareSettings)

    def to_config(
        self,
        *,
        unit_scope: bool = True,
        context_setters: list[Any] | None = None,
        custom: Sequence[tuple[type, dict[str, Any]]] = (),
    ) -> MiddlewareConfig:
        """The ``MiddlewareConfig`` these settings describe; the keywords are the members no environment carries."""
        return MiddlewareConfig(
            unit_scope=unit_scope,
            context=self.context,
            sentry=self.sentry,
            processing_time=self.processing_time,
            context_setters=context_setters,
            logging=self.logging.to_config(),
            correlation_id=self.correlation_id.to_config(),
            gzip=self.gzip.to_config(),
            cors=self.cors.to_config(),
            custom=list(custom),
        )


class UvicornSettings(BaseModel):
    """``HttpConfig.uvicorn_kwargs`` as typed fields: the operational uvicorn knobs.

    Every field is ``None`` until set and only a set field reaches
    ``uvicorn.Config``, so uvicorn's own defaults apply to the rest and nothing
    here pins them. The set is the knobs that do something under the runner,
    which pre-binds the socket and drives one ``uvicorn.Server`` directly:
    ``workers`` is read only by ``uvicorn.run()``'s multiprocess supervisor,
    ``reload``, ``uds``/``fd``, ``loop`` and ``log_config`` are ignored or owned
    by the runner, and ``host``, ``port`` and ``timeout_graceful_shutdown`` are
    :class:`HttpServerSettings`'s own. Anything else goes through
    ``HttpServerSettings.uvicorn_kwargs``.
    """

    proxy_headers: bool | None = Field(default=None, description="Trust X-Forwarded-* from forwarded_allow_ips")
    forwarded_allow_ips: list[str] | str | None = Field(
        default=None,
        description="Proxies whose X-Forwarded-* headers are trusted: a list, a comma-separated string or *",
    )
    root_path: str | None = Field(default=None, description="ASGI root_path, for an app served under a prefix")
    server_header: bool | None = Field(default=None, description="Send the Server response header")
    date_header: bool | None = Field(default=None, description="Send the Date response header")
    timeout_keep_alive: int | None = Field(
        default=None, ge=0, description="Seconds an idle keep-alive connection is held"
    )
    backlog: int | None = Field(default=None, ge=0, description="Listen backlog")
    limit_concurrency: int | None = Field(
        default=None, ge=1, description="Concurrent connections or tasks before new requests get a 503"
    )
    limit_max_requests: int | None = Field(
        default=None, ge=1, description="Requests served before the server exits (the Host then stops the process)"
    )
    h11_max_incomplete_event_size: int | None = Field(
        default=None, ge=1, description="Largest incomplete HTTP event (request line plus headers), bytes"
    )
    access_log: bool | None = Field(
        default=None, description="uvicorn's own access log; the logging middleware writes one line per request already"
    )
    log_level: Literal["critical", "error", "warning", "info", "debug", "trace"] | None = Field(
        default=None, description="Level of uvicorn's own loggers; case-insensitive on input"
    )
    ssl_keyfile: str | None = Field(default=None, description="TLS private key path; with ssl_certfile, serves HTTPS")
    ssl_certfile: str | None = Field(default=None, description="TLS certificate path")
    ssl_keyfile_password: str | None = Field(default=None, description="Password of the private key")
    ssl_ca_certs: str | None = Field(default=None, description="CA bundle path, for client certificates")
    ssl_cert_reqs: int | None = Field(
        default=None,
        ge=0,
        le=2,
        description="Client certificate policy, ssl.VerifyMode: 0 none, 1 optional, 2 required",
    )
    ssl_ciphers: str | None = Field(default=None, description="OpenSSL cipher list")

    @field_validator("log_level", mode="before")
    @classmethod
    def _lowercase_level(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    def to_kwargs(self) -> dict[str, Any]:
        """The fields that were set, by uvicorn's names, for ``HttpConfig.uvicorn_kwargs``."""
        return self.model_dump(exclude_none=True)


class HttpServerSettings(BaseModel):
    """``HttpConfig`` as settings, plus the ``middlewares`` section the entrypoint takes beside it.

    ``port=0`` is rejected unless ``allow_ephemeral_port`` says so: ``0`` only ever
    arrives here from configuration, and a pod on an ephemeral port is one no
    probe or load balancer reaches. ``version`` is the argument of
    :meth:`to_config`, not a field: it is the application's version, which the
    settings object carries once already.
    """

    host: str = Field(default=DEFAULT_HTTP_HOST, description="Bind host")
    port: int = Field(default=DEFAULT_HTTP_PORT, ge=0, le=65535, description="Bind port")
    allow_ephemeral_port: bool = Field(
        default=False,
        description="Accept port=0 (ephemeral); off by default, a probe targeting a fixed port cannot reach one",
    )
    graceful_timeout: float = Field(
        default=DEFAULT_GRACEFUL_TIMEOUT_SECONDS,
        description="Seconds uvicorn lets in-flight requests finish on shutdown; 0 waits indefinitely",
    )
    title: str | None = Field(default=None, description="OpenAPI title; None falls back to AppSpec.service_name")
    openapi_url: str = Field(default=DEFAULT_OPENAPI_URL, description="Schema path")
    docs_url: str = Field(default=DEFAULT_DOCS_URL, description="Swagger UI path")
    redoc_url: str = Field(default=DEFAULT_REDOC_URL, description="ReDoc path")
    redirect_slashes: bool = Field(default=False, description="Redirect between /path and /path/")
    fastapi_kwargs: dict[str, Any] = Field(
        default_factory=dict, description="Merged into the FastAPI(...) call; JSON in the environment"
    )
    uvicorn: UvicornSettings = Field(default_factory=UvicornSettings)
    uvicorn_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Merged into the uvicorn.Config(...) call over the uvicorn section, so it wins on a collision; "
        "JSON in the environment",
    )
    health: HealthSettings = Field(default_factory=HealthSettings)
    middlewares: MiddlewareSettings = Field(default_factory=MiddlewareSettings)

    @model_validator(mode="after")
    def _validate_port(self) -> HttpServerSettings:
        if self.port == 0 and not self.allow_ephemeral_port:
            raise ValueError(
                "server port=0 binds an ephemeral port and a probe or load balancer targeting a fixed port will not "
                "reach it; set allow_ephemeral_port=True if that is intended"
            )
        return self

    def to_config(self, *, version: str = "0.0.0") -> HttpConfig:
        """The ``HttpConfig`` these settings describe; ``version`` is the OpenAPI version, your ``app_version``.

        ``middlewares`` is not part of it: the entrypoint takes that beside the
        config, as ``middlewares=settings.server.middlewares.to_config()``.
        """
        return HttpConfig(
            host=self.host,
            port=self.port,
            graceful_timeout=self.graceful_timeout,
            title=self.title,
            version=version,
            openapi_url=self.openapi_url,
            docs_url=self.docs_url,
            redoc_url=self.redoc_url,
            redirect_slashes=self.redirect_slashes,
            fastapi_kwargs=dict(self.fastapi_kwargs),
            uvicorn_kwargs={**self.uvicorn.to_kwargs(), **self.uvicorn_kwargs},
            health=self.health.to_config(),
        )


__all__ = [
    "CORSMiddlewareSettings",
    "CorrelationIdMiddlewareSettings",
    "GZipMiddlewareSettings",
    "HealthSettings",
    "HttpServerSettings",
    "LoggingMiddlewareSettings",
    "MiddlewareSettings",
    "UvicornSettings",
]
