"""Unit tests for the FastAPI entrypoint (servicewright.adapters.fastapi).

The app is built via ``entrypoint.build_app(ctx)`` against a mocked
:class:`FakeContainer`; routes are exercised with ``fastapi.testclient.TestClient``.
The uvicorn server is mocked for serve/drain/stop (no real socket is bound).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from deadline_budget import DeadlineExceededError
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from servicewright import (
    AppSpec,
    Entrypoint,
    ErrorKind,
    Plugin,
    ServerEntrypoint,
    Service,
    ServiceError,
    get_context_value,
)
from servicewright.adapters import _uvicorn as uvicorn_mod
from servicewright.adapters.fastapi import (
    AuthorizationHeader,
    CORSMiddlewareConfig,
    FastApiEntrypoint,
    FastApiPlugin,
    HealthConfig,
    HttpConfig,
    IdempotencyKey,
    LivenessResponse,
    MiddlewareConfig,
    ProblemDetails,
    ReadinessResponse,
    UnitScopeDep,
    UnitScopeMiddleware,
    XFingerprintHeader,
    XUserId,
    current_unit_scope,
    get_unit_scope,
)
from servicewright.core.health import HealthRegistry
from servicewright.core.spec import BootstrapContext, ServiceContext
from servicewright.testing import FakeContainer, FakeScope, FakeSettings

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Helpers / test doubles
# --------------------------------------------------------------------------- #
def _make_service_ctx(
    container: FakeContainer,
    *,
    service_name: str = "svc",
    health: HealthRegistry | None = None,
    settings: Any = None,
    observability: Any = None,
) -> ServiceContext:
    bootstrap = BootstrapContext(
        settings=settings or FakeSettings(),
        service_name=service_name,
        container=container,
        lifecycle=object(),  # type: ignore[arg-type]
    )
    kwargs: dict[str, Any] = {}
    if observability is not None:
        kwargs["observability"] = observability
    return ServiceContext(
        bootstrap=bootstrap,
        app_scope=FakeScope(),
        health=health or HealthRegistry(),
        **kwargs,
    )


async def _build_client(
    ep: FastApiEntrypoint,
    ctx: ServiceContext,
    *,
    raise_server_exceptions: bool = False,
) -> TestClient:
    app = await ep.build_app(ctx)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


class _FakeUvicornServer:
    """Stand-in for ``uvicorn.Server`` that records control-flag mutations."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.should_exit = False
        self.force_exit = False
        self.signals_captured = False
        self.serve_started = False
        self.serve_sockets: Any = None
        self._release = asyncio.Event()

    @contextlib.contextmanager
    def capture_signals(self) -> Any:  # pragma: no cover - replaced by the entrypoint
        """Real uvicorn installs SIGINT/SIGTERM handlers here; record if it runs."""
        self.signals_captured = True
        yield

    async def serve(self, sockets: Any = None) -> None:
        self.serve_started = True
        self.serve_sockets = sockets
        # Block until the entrypoint sets should_exit, mimicking uvicorn's loop.
        while not self.should_exit:
            await asyncio.sleep(0)


class _FakeUvicornConfig:
    def __init__(self, app: Any, **kwargs: Any) -> None:
        self.app = app
        self.kwargs = kwargs


@pytest.fixture
def patched_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``uvicorn.Config``/``uvicorn.Server`` with capturing fakes."""
    created: dict[str, Any] = {}

    def make_server(config: Any) -> _FakeUvicornServer:
        server = _FakeUvicornServer(config)
        created["server"] = server
        return server

    def make_config(app: Any, **kwargs: Any) -> _FakeUvicornConfig:
        config = _FakeUvicornConfig(app, **kwargs)
        created["config"] = config
        return config

    fake_uvicorn = MagicMock()
    fake_uvicorn.Server = make_server
    fake_uvicorn.Config = make_config
    monkeypatch.setattr(uvicorn_mod, "uvicorn", fake_uvicorn)
    return created


# --------------------------------------------------------------------------- #
# HttpConfig
# --------------------------------------------------------------------------- #
def test__http_config__default__uses_the_documented_values() -> None:
    config = HttpConfig()
    assert config.host == "0.0.0.0"
    assert config.port == 8000
    assert config.graceful_timeout == 10.0
    assert config.openapi_url == "/system/openapi.json"
    assert config.docs_url == "/system/docs"
    assert config.redoc_url == "/system/redoc"
    assert config.address == "0.0.0.0:8000"
    assert config.health.liveness_path == "/system/health/livez"
    assert config.health.readiness_path == "/system/health/readyz"


def test__http_config__host_and_port_overridden__reports_the_address() -> None:
    assert HttpConfig(host="127.0.0.1", port=0).address == "127.0.0.1:0"


def test__cors_config__wildcard_origin_with_credentials__raises() -> None:
    with pytest.raises(ValueError, match="allow_credentials=True"):
        CORSMiddlewareConfig(allow_credentials=True, allow_origins=["*"])


def test__cors_config__explicit_origins_with_credentials__is_accepted() -> None:
    cfg = CORSMiddlewareConfig(allow_credentials=True, allow_origins=["https://app.example.com"])
    assert cfg.allow_credentials is True


# --------------------------------------------------------------------------- #
# Entrypoint protocol conformance & attributes
# --------------------------------------------------------------------------- #
def test__fastapi_entrypoint__constructed__satisfies_the_protocol_without_a_unit_scope() -> None:
    ep = FastApiEntrypoint()
    assert isinstance(ep, Entrypoint)
    assert isinstance(ep, ServerEntrypoint)
    assert ep.kind == "http"
    assert ep.essential is True
    # ServerEntrypoint must NOT expose unit_scope (framework owns per-request scope).
    assert not hasattr(ep, "unit_scope")


def test__fastapi_entrypoint__kind_and_essential_overridden__reports_them() -> None:
    ep = FastApiEntrypoint(kind="api", essential=False)
    assert ep.kind == "api"
    assert ep.essential is False


def test__fastapi_entrypoint__before_bind__exposes_its_config_and_no_app() -> None:
    config = HttpConfig(port=12345)
    ep = FastApiEntrypoint(config=config)
    assert ep.config is config
    assert ep.app is None


def test__fastapi_entrypoint__two_instances__do_not_share_a_config() -> None:
    ep1 = FastApiEntrypoint()
    ep2 = FastApiEntrypoint()
    assert ep1.config is not ep2.config  # no shared mutable default


# --------------------------------------------------------------------------- #
# build_app / bind
# --------------------------------------------------------------------------- #
async def test__fastapi_bind__called__builds_and_stores_the_app() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    ctx = _make_service_ctx(FakeContainer())
    await ep.bind(ctx)
    assert ep.app is not None
    assert ep.app.title == "svc"


async def test__fastapi_build_app__title_configured__uses_it() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(title="custom-title"))
    app = await ep.build_app(_make_service_ctx(FakeContainer()))
    assert app.title == "custom-title"


async def test__fastapi_build_app__routers_given__includes_them() -> None:
    router = APIRouter()

    @router.get("/ping")
    async def ping() -> dict[str, str]:
        return {"pong": "ok"}

    ep = FastApiEntrypoint(config=HttpConfig(port=0), routers=(router,))
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"pong": "ok"}


async def test__fastapi_build_app__sync_routes_registerer__runs_it() -> None:
    seen: list[ServiceContext] = []

    def register(app: Any, ctx: ServiceContext) -> None:
        seen.append(ctx)

        @app.get("/registered")
        async def registered() -> dict[str, bool]:
            return {"ok": True}

    ep = FastApiEntrypoint(config=HttpConfig(port=0), routes_registerer=register)
    ctx = _make_service_ctx(FakeContainer())
    client = await _build_client(ep, ctx)
    assert seen == [ctx]
    assert client.get("/registered").json() == {"ok": True}


async def test__fastapi_build_app__async_routes_registerer__runs_it() -> None:
    seen: list[str] = []

    async def register(app: Any, ctx: ServiceContext) -> None:
        seen.append("async")

    ep = FastApiEntrypoint(config=HttpConfig(port=0), routes_registerer=register)
    await ep.build_app(_make_service_ctx(FakeContainer()))
    assert seen == ["async"]


async def test__fastapi_build_app__configure_hook_given__calls_it_last() -> None:
    calls: list[tuple[Any, ServiceContext]] = []

    def configure(app: Any, ctx: ServiceContext) -> None:
        calls.append((app, ctx))

    ep = FastApiEntrypoint(config=HttpConfig(port=0), configure_app=configure)
    ctx = _make_service_ctx(FakeContainer())
    app = await ep.build_app(ctx)
    assert calls == [(app, ctx)]


async def test__fastapi_build_app__default_config__serves_openapi_under_system() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))
    assert client.get("/system/openapi.json").status_code == 200
    # The default root openapi path is NOT served.
    assert client.get("/openapi.json").status_code == 404


# --------------------------------------------------------------------------- #
# Health routes
# --------------------------------------------------------------------------- #
async def test__fastapi_liveness_route__process_is_up__answers_ok() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    client = await _build_client(ep, _make_service_ctx(FakeContainer(), health=HealthRegistry()))
    resp = client.get("/system/health/livez")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test__fastapi_readiness_route__ready_flag_not_set__answers_503() -> None:
    health = HealthRegistry()
    health.ready = False
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    client = await _build_client(ep, _make_service_ctx(FakeContainer(), health=health))
    resp = client.get("/system/health/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unhealthy"}


async def test__fastapi_readiness_route__ready_and_checks_pass__answers_200() -> None:
    health = HealthRegistry()
    health.ready = True

    class _OkCheck:
        async def check(self) -> bool:
            return True

    health.add_check("db", _OkCheck())
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    client = await _build_client(ep, _make_service_ctx(FakeContainer(), health=health))
    resp = client.get("/system/health/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test__fastapi_readiness_route__a_check_fails__answers_503() -> None:
    health = HealthRegistry()
    health.ready = True

    class _FailCheck:
        async def check(self) -> bool:
            return False

    health.add_check("db", _FailCheck())
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    client = await _build_client(ep, _make_service_ctx(FakeContainer(), health=health))
    assert client.get("/system/health/readyz").status_code == 503


async def test__fastapi_health_routes__custom_paths_configured__are_served_there() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0, health=HealthConfig(enabled=False)))
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))
    assert client.get("/system/health/livez").status_code == 404

    ep2 = FastApiEntrypoint(
        config=HttpConfig(port=0, health=HealthConfig(liveness_path="/alive", readiness_path="/ready")),
    )
    client2 = await _build_client(ep2, _make_service_ctx(FakeContainer()))
    assert client2.get("/alive").status_code == 200
    assert client2.get("/ready").status_code == 503


# --------------------------------------------------------------------------- #
# Per-request UnitScope
# --------------------------------------------------------------------------- #
async def test__fastapi_unit_scope__injected_as_a_dependency__resolves() -> None:
    router = APIRouter()

    @router.get("/scoped")
    async def scoped(scope: UnitScopeDep) -> dict[str, Any]:
        return {"val": await scope.get(str)}

    container = FakeContainer(provides={str: "dep-value"})
    ep = FastApiEntrypoint(config=HttpConfig(port=0), routers=(router,))
    client = await _build_client(ep, _make_service_ctx(container))
    resp = client.get("/scoped")
    assert resp.status_code == 200
    assert resp.json() == {"val": "dep-value"}
    assert container.unit_scopes_opened == 1


async def test__fastapi_unit_scope__read_via_current_unit_scope__resolves() -> None:
    router = APIRouter()

    @router.get("/cv")
    async def cv() -> dict[str, Any]:
        scope = current_unit_scope()
        return {"val": await scope.get(str)}

    container = FakeContainer(provides={str: "cv-dep"})
    ep = FastApiEntrypoint(config=HttpConfig(port=0), routers=(router,))
    client = await _build_client(ep, _make_service_ctx(container))
    assert client.get("/cv").json() == {"val": "cv-dep"}


async def test__fastapi_unit_scope__opened__carries_the_request_as_context() -> None:
    router = APIRouter()

    @router.get("/ctx")
    async def ctx_route(scope: UnitScopeDep) -> dict[str, bool]:
        # The protocol does not expose ``context``; the FakeScope test double does.
        fake_scope = cast(FakeScope, scope)
        return {"has_request": fake_scope.context is not None and "request" in fake_scope.context}

    container = FakeContainer()
    ep = FastApiEntrypoint(config=HttpConfig(port=0), routers=(router,))
    client = await _build_client(ep, _make_service_ctx(container))
    assert client.get("/ctx").json() == {"has_request": True}
    assert container.unit_contexts[0] is not None
    assert "request" in container.unit_contexts[0]


async def test__fastapi_unit_scope__one_request__is_opened_exactly_once() -> None:
    router = APIRouter()

    @router.get("/x")
    async def x() -> dict[str, bool]:
        return {"ok": True}

    container = FakeContainer()
    ep = FastApiEntrypoint(config=HttpConfig(port=0), routers=(router,))
    client = await _build_client(ep, _make_service_ctx(container))
    client.get("/x")
    client.get("/x")
    # One scope per request (no double-open).
    assert container.unit_scopes_opened == 2


def test__fastapi_current_unit_scope__called_outside_a_request__raises() -> None:
    with pytest.raises(LookupError, match="No active HTTP unit scope"):
        current_unit_scope()


def test__fastapi_get_unit_scope__middleware_absent__raises() -> None:
    request = MagicMock()
    request.state = MagicMock(spec=[])  # no unit_scope attribute
    with pytest.raises(LookupError, match=r"No active HTTP unit scope on request\.state"):
        get_unit_scope(request)


def test__middleware_config__default__owns_the_unit_scope() -> None:
    assert MiddlewareConfig().unit_scope is True


async def test__fastapi_unit_scope__disabled__opens_no_scope() -> None:
    router = APIRouter()

    @router.get("/x")
    async def x() -> dict[str, bool]:
        return {"ok": True}

    container = FakeContainer()
    ep = FastApiEntrypoint(
        config=HttpConfig(port=0),
        routers=(router,),
        middlewares=MiddlewareConfig(unit_scope=False),
    )
    client = await _build_client(ep, _make_service_ctx(container))

    assert client.get("/x").json() == {"ok": True}
    assert container.unit_scopes_opened == 0


async def test__fastapi_unit_scope__disabled__leaves_the_rest_of_the_stack_in_place() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0), middlewares=MiddlewareConfig(unit_scope=False))
    app = await ep.build_app(_make_service_ctx(FakeContainer()))
    names = [m.cls.__name__ for m in app.user_middleware]
    assert "UnitScopeMiddleware" not in names
    # The context layer is now the outermost; nothing else moved.
    assert names[0] == "ContextMiddleware"


async def test__fastapi_unit_scope__disabled__unit_scope_dep_fails_loudly(caplog: pytest.LogCaptureFixture) -> None:
    router = APIRouter()

    @router.get("/scoped")
    async def scoped(scope: UnitScopeDep) -> dict[str, Any]:
        return {"val": await scope.get(str)}

    ep = FastApiEntrypoint(
        config=HttpConfig(port=0),
        routers=(router,),
        middlewares=MiddlewareConfig(unit_scope=False),
    )
    client = await _build_client(ep, _make_service_ctx(FakeContainer(provides={str: "dep-value"})))
    with caplog.at_level(logging.ERROR):
        resp = client.get("/scoped")

    assert resp.status_code == 500
    assert "MiddlewareConfig.unit_scope" in caplog.text


# --------------------------------------------------------------------------- #
# Exception handlers -> RFC 9457 problem details (default renderer)
# --------------------------------------------------------------------------- #
class _UserMissingError(ServiceError):
    kind = ErrorKind.NOT_FOUND


def _exc_router() -> APIRouter:
    router = APIRouter()

    @router.get("/http-404")
    async def http404() -> None:
        raise HTTPException(status_code=404, detail="not found")

    @router.get("/http-500")
    async def http500() -> None:
        raise HTTPException(status_code=500, detail="boom")

    @router.get("/unhandled")
    async def unhandled() -> None:
        raise RuntimeError("kaboom")

    @router.get("/deadline")
    async def deadline() -> None:
        raise DeadlineExceededError(budget_seconds=1.5, elapsed_seconds=2.5)

    @router.get("/public-error")
    async def public_error() -> None:
        raise _UserMissingError("missing", params={"user_id": "42"})

    @router.get("/private-error")
    async def private_error() -> None:
        raise ServiceError("secret", code="secret_leak", kind=ErrorKind.CONFLICT, public=False)

    @router.get("/needs-query")
    async def needs_query(value: int) -> dict[str, int]:
        return {"value": value}

    return router


async def _exc_client() -> TestClient:
    ep = FastApiEntrypoint(config=HttpConfig(port=0), routers=(_exc_router(),))
    return await _build_client(ep, _make_service_ctx(FakeContainer()))


async def test__validation_error_handler__invalid_body__answers_422_with_a_problem_document() -> None:
    resp = (await _exc_client()).get("/needs-query")
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    body = resp.json()
    assert body["code"] == "validation_error"
    assert body["status"] == 422
    assert body["params"]["errors"][0]["loc"] == ["query", "value"]
    # Document shape matches the RFC 9457 model.
    ProblemDetails.model_validate(body)


async def test__http_exception_handler__4xx__preserves_the_detail() -> None:
    resp = (await _exc_client()).get("/http-404")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "http_error"
    assert body["detail"] == "not found"


async def test__http_exception_handler__5xx__masks_it_to_a_generic_internal_error() -> None:
    resp = (await _exc_client()).get("/http-500")
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "internal_error"
    # The handler's detail must NOT leak through the mask.
    assert "boom" not in resp.text


async def test__unhandled_exception_handler__handler_raises__masks_it_to_a_generic_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = await _exc_client()
    with caplog.at_level(logging.ERROR):
        resp = client.get("/unhandled")
    assert resp.status_code == 500
    assert resp.json()["code"] == "internal_error"
    assert "kaboom" not in resp.text
    assert any("Unhandled exception" in r.message for r in caplog.records)


async def test__deadline_handler__budget_exceeded__answers_504() -> None:
    resp = (await _exc_client()).get("/deadline")
    assert resp.status_code == 504
    body = resp.json()
    assert body["code"] == "deadline_exceeded"
    assert body["status"] == 504
    assert body["params"] == {"budget_seconds": 1.5, "elapsed_seconds": 2.5}


async def test__service_error_handler__public_error__maps_it_to_its_kinds_status() -> None:
    resp = (await _exc_client()).get("/public-error")
    assert resp.status_code == 404
    body = resp.json()
    # Code derived from the subclass name; params pass through.
    assert body["code"] == "user_missing"
    assert body["detail"] == "missing"
    assert body["params"] == {"user_id": "42"}


async def test__service_error_handler__private_error__masks_it_to_500(caplog: pytest.LogCaptureFixture) -> None:
    client = await _exc_client()
    with caplog.at_level(logging.WARNING):
        resp = client.get("/private-error")
    assert resp.status_code == 500
    # The leaking code must NOT appear in the response body — only in the log.
    assert "secret_leak" not in resp.text
    assert resp.json()["code"] == "internal_error"
    assert any("secret_leak" in str(r.__dict__.get("error_code", "")) for r in caplog.records)


async def test__error_renderer__custom_implementation__owns_the_wire_format() -> None:
    """A custom renderer switches the format of EVERY default handler."""
    from servicewright import ErrorInfo, RenderedError

    class EnvelopeRenderer:
        def render(self, info: ErrorInfo) -> RenderedError:
            return RenderedError(
                status_code=info.http_status,
                body={"error": {"code": info.code, "message": info.detail or ""}},
                media_type="application/json",
            )

    ep = FastApiEntrypoint(
        config=HttpConfig(port=0),
        routers=(_exc_router(),),
        error_renderer=EnvelopeRenderer(),
    )
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))

    resp = client.get("/public-error")
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "user_missing", "message": "missing"}}

    resp = client.get("/needs-query")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test__default_exception_handlers__disabled__are_not_installed() -> None:
    ep = FastApiEntrypoint(
        config=HttpConfig(port=0),
        routers=(_exc_router(),),
        default_exception_handlers=False,
    )
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))
    # Without the default validation handler FastAPI returns its own 422 shape.
    resp = client.get("/needs-query")
    assert resp.status_code == 422
    assert "code" not in resp.json()  # default FastAPI shape, not the problem document


async def test__custom_exception_handlers__supplied__are_registered() -> None:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    class MyError(Exception):
        pass

    async def handle_my_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=418, content={"teapot": True})

    router = APIRouter()

    @router.get("/teapot")
    async def teapot() -> None:
        raise MyError

    ep = FastApiEntrypoint(
        config=HttpConfig(port=0),
        routers=(router,),
        exception_handlers={MyError: handle_my_error},
    )
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))
    resp = client.get("/teapot")
    assert resp.status_code == 418
    assert resp.json() == {"teapot": True}


# --------------------------------------------------------------------------- #
# Middleware stack presence & ordering
# --------------------------------------------------------------------------- #
async def test__middleware_stack__default_config__is_installed_in_the_documented_order() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    app = await ep.build_app(_make_service_ctx(FakeContainer()))
    names = [m.cls.__name__ for m in app.user_middleware]
    # Starlette stores middleware in reverse run-order (index 0 runs first).
    # UnitScopeMiddleware was added last -> it runs first (outermost).
    assert names[0] == "UnitScopeMiddleware"
    for expected in (
        "ContextMiddleware",
        "SentryMiddleware",
        "ProcessingTimeMiddleware",
        "LoggingMiddleware",
        "GZipMiddleware",
        "CORSMiddleware",
    ):
        assert expected in names


@pytest.mark.parametrize(
    ("unit_scope", "expected"),
    [
        (True, ["UnitScopeMiddleware", "UnhandledErrorMiddleware"]),
        (False, ["UnhandledErrorMiddleware"]),
    ],
)
async def test__middleware_stack__everything_else_disabled__keeps_only_the_always_on_layers(
    unit_scope: bool,
    expected: list[str],
) -> None:
    middlewares = MiddlewareConfig(unit_scope=unit_scope)
    middlewares.cors.enabled = False
    middlewares.gzip.enabled = False
    middlewares.logging.enabled = False
    middlewares.correlation_id.enabled = False
    middlewares.sentry = False
    middlewares.context = False
    middlewares.processing_time = False

    ep = FastApiEntrypoint(config=HttpConfig(port=0), middlewares=middlewares)
    app = await ep.build_app(_make_service_ctx(FakeContainer()))
    names = [m.cls.__name__ for m in app.user_middleware]
    # The correlated last-resort error handler can never be switched off (a 500
    # must stay renderable whatever else is); the unit scope only steps aside for
    # a framework DI integration that owns the request scope itself.
    assert names == expected


async def test__middleware_stack__custom_middleware_configured__is_added() -> None:
    from starlette.middleware.base import BaseHTTPMiddleware

    class MyMiddleware(BaseHTTPMiddleware):
        pass

    middlewares = MiddlewareConfig(custom=[(MyMiddleware, {})])
    ep = FastApiEntrypoint(config=HttpConfig(port=0), middlewares=middlewares)
    app = await ep.build_app(_make_service_ctx(FakeContainer()))
    names = [m.cls.__name__ for m in app.user_middleware]
    assert "MyMiddleware" in names
    # Custom middleware added first -> runs innermost (last in run order).
    assert names[-1] == "MyMiddleware"


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
async def test__metrics_endpoint__enabled__is_exposed() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0), metrics=True)
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))
    resp = client.get("/system/metrics")
    assert resp.status_code == 200
    assert "# HELP" in resp.text or "# TYPE" in resp.text


async def test__metrics_endpoint__disabled__is_absent() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0), metrics=False)
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))
    assert client.get("/system/metrics").status_code == 404


async def test__metrics_endpoint__instrumentator_missing__raises_with_the_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "prometheus_fastapi_instrumentator" or name.startswith("prometheus_fastapi_instrumentator."):
            raise ImportError("simulated missing extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ep = FastApiEntrypoint(config=HttpConfig(port=0), metrics=True)
    with pytest.raises(ImportError, match=r"servicewright\[fastapi\]"):
        await ep.build_app(_make_service_ctx(FakeContainer()))


# --------------------------------------------------------------------------- #
# OpenTelemetry instrumentation
# --------------------------------------------------------------------------- #
class _OtelSettings(FakeSettings):
    class _Tracing:
        excluded_urls = "/custom"

    tracing: Any = _Tracing()


class _RecordingTracingSink:
    """Tracing-sink double recording ``instrument_fastapi`` calls."""

    backend = "fake"

    def __init__(self) -> None:
        self.instrumented: list[str | None] = []

    def setup(self, ctx: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def tracer(self, name: str) -> Any:
        return None

    def instrument_fastapi(self, app: Any, *, excluded_urls: str | None = None) -> None:
        self.instrumented.append(excluded_urls)


async def test__otel_instrumentation__tracing_configured__instruments_the_app() -> None:
    from servicewright.core.observability import ObservabilityManager

    sink = _RecordingTracingSink()
    manager = ObservabilityManager()
    manager._tracing = sink  # inject the double past configure()

    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    await ep.build_app(_make_service_ctx(FakeContainer(), settings=_OtelSettings(), observability=manager))

    assert len(sink.instrumented) == 1
    excluded = sink.instrumented[0] or ""
    # Settings-supplied exclusions and the health paths are both excluded.
    assert "/custom" in excluded
    assert "/system/health/livez" in excluded


async def test__otel_instrumentation__no_tracing_settings__is_skipped() -> None:
    from servicewright.core.observability import ObservabilityManager

    sink = _RecordingTracingSink()
    manager = ObservabilityManager()
    manager._tracing = sink

    # FakeSettings.tracing is None -> no instrumentation, no error.
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    app = await ep.build_app(_make_service_ctx(FakeContainer(), observability=manager))
    assert app is not None
    assert sink.instrumented == []


async def test__otel_instrumentation__null_tracing_sink__does_nothing() -> None:
    # Otel settings present but tracing disabled/unconfigured (Null sink):
    # build_app must succeed without instrumentation side effects.
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    app = await ep.build_app(_make_service_ctx(FakeContainer(), settings=_OtelSettings()))
    assert app is not None


# --------------------------------------------------------------------------- #
# serve() / drain() / stop() drive uvicorn correctly (mocked)
# --------------------------------------------------------------------------- #
async def test__fastapi_serve__called_before_bind__raises() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(RuntimeError, match=r"serve\(\) called before bind\(\)"):
        await ep.serve(stop=stop)


async def test__fastapi_serve__stop_set__returns_while_still_accepting(patched_uvicorn: dict[str, Any]) -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()

    async def release() -> None:
        while "server" not in patched_uvicorn or not patched_uvicorn["server"].serve_started:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(ep.serve(stop=stop), release())

    server = patched_uvicorn["server"]
    assert server.serve_started is True
    # The Host owns signals: uvicorn's own capture hook must be replaced.
    assert server.signals_captured is False
    # serve() returns while the server is STILL accepting; drain() shuts it down.
    assert server.should_exit is False


async def test__fastapi_serve__building_the_server__leaves_uvicorn_logging_to_the_host(
    patched_uvicorn: dict[str, Any],
) -> None:
    ep = FastApiEntrypoint(config=HttpConfig(host="127.0.0.1", port=0, graceful_timeout=7.0))
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()

    async def release() -> None:
        while "config" not in patched_uvicorn:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(ep.serve(stop=stop), release())

    config = patched_uvicorn["config"]
    assert config.kwargs["host"] == "127.0.0.1"
    assert config.kwargs["port"] == 0
    assert config.kwargs["log_config"] is None
    assert config.kwargs["timeout_graceful_shutdown"] == 7


async def test__fastapi_drain__called__asks_uvicorn_to_stop_accepting(patched_uvicorn: dict[str, Any]) -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()

    async def drive() -> None:
        while "server" not in patched_uvicorn or not patched_uvicorn["server"].serve_started:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(ep.serve(stop=stop), drive())
    # After serve returns, drain is a no-op-safe call that flips should_exit.
    await ep.drain(1.0)
    assert patched_uvicorn["server"].should_exit is True
    assert ep._runner._stopped is True


async def test__fastapi_drain__called_before_serve__does_nothing() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    await ep.drain(1.0)  # no server -> must not raise


async def test__fastapi_stop__called__forces_uvicorn_down(patched_uvicorn: dict[str, Any]) -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()

    async def drive() -> None:
        while "server" not in patched_uvicorn or not patched_uvicorn["server"].serve_started:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(ep.serve(stop=stop), drive())
    await ep.stop()
    server = patched_uvicorn["server"]
    assert server.should_exit is True
    assert server.force_exit is True
    assert ep._runner._stopped is True


async def test__fastapi_stop__called_before_serve__does_nothing() -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    await ep.stop()  # no server -> must not raise


# --------------------------------------------------------------------------- #
# FastApiPlugin
# --------------------------------------------------------------------------- #
def test__fastapi_plugin__constructed__satisfies_the_plugin_protocol() -> None:
    plugin = FastApiPlugin()
    assert isinstance(plugin, Plugin)


def test__fastapi_plugin__constructed__exposes_its_entrypoint() -> None:
    plugin = FastApiPlugin(config=HttpConfig(port=0), kind="api", essential=False)
    ep = plugin.entrypoint
    assert isinstance(ep, FastApiEntrypoint)
    assert ep.kind == "api"
    assert ep.essential is False


def test__fastapi_plugin_on_register__called__adds_its_entrypoint_to_the_host() -> None:
    plugin = FastApiPlugin()
    host = MagicMock()
    plugin.on_register(spec=MagicMock(), host=host)
    host.add_entrypoint.assert_called_once_with(plugin.entrypoint)


# --------------------------------------------------------------------------- #
# Public header / schema surface
# --------------------------------------------------------------------------- #
def test__fastapi_public_aliases__imported__are_all_exported() -> None:
    assert XUserId is not None
    assert IdempotencyKey is not None
    assert AuthorizationHeader is not None
    assert XFingerprintHeader is not None
    assert LivenessResponse(status="ok").status == "ok"
    assert ReadinessResponse(status="unhealthy").status == "unhealthy"


# --------------------------------------------------------------------------- #
# End-to-end-ish: drive a FastApiEntrypoint through a real Host/Service
# --------------------------------------------------------------------------- #
async def test__fastapi_entrypoint__driven_by_a_service__completes_the_lifecycle(
    patched_uvicorn: dict[str, Any],
) -> None:
    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="http-service", create_container=lambda _s: container)
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    service = Service(spec, entrypoints=[ep])

    stop = asyncio.Event()

    async def run_then_stop() -> None:
        while not spec.health.ready:
            await asyncio.sleep(0)
        while "server" not in patched_uvicorn or not patched_uvicorn["server"].serve_started:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(service.run(FakeSettings(), stop=stop), run_then_stop())

    assert ep.app is not None
    assert patched_uvicorn["server"].serve_started is True
    assert patched_uvicorn["server"].should_exit is True
    assert spec.health.ready is False  # flipped off during shutdown
    assert container.app_scopes_opened == 1


async def test__fastapi_plugin__driven_by_a_service__completes_the_lifecycle(patched_uvicorn: dict[str, Any]) -> None:
    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="http-plugin-service", create_container=lambda _s: container)
    plugin = FastApiPlugin(config=HttpConfig(port=0))
    service = Service(spec, plugins=[plugin])

    stop = asyncio.Event()

    async def run_then_stop() -> None:
        while not spec.health.ready:
            await asyncio.sleep(0)
        while "server" not in patched_uvicorn or not patched_uvicorn["server"].serve_started:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(service.run(FakeSettings(), stop=stop), run_then_stop())

    assert patched_uvicorn["server"].serve_started is True
    assert spec.health.ready is False


# --------------------------------------------------------------------------- #
# Context setters (faithful fold of context_integration)
# --------------------------------------------------------------------------- #
def test__is_valid_uuid__uuid_and_non_uuid_values__classifies_them() -> None:
    from servicewright.adapters.fastapi.configurators import is_valid_uuid

    assert is_valid_uuid("123e4567-e89b-12d3-a456-426614174000") is True
    assert is_valid_uuid("not-a-uuid") is False
    assert is_valid_uuid("") is False


def test__default_context_setters__otel_installed__include_the_baggage_setter() -> None:
    from servicewright.adapters.fastapi import context as context_mod

    setters = context_mod.get_default_context_setters()
    names = [type(s).__name__ for s in setters]
    # opentelemetry is installed in the test env -> both setters, structlog last.
    assert names == ["OtelBaggageSetter", "StructlogSetter"]


def test__default_context_setters__otel_missing__omit_the_baggage_setter(monkeypatch: pytest.MonkeyPatch) -> None:
    from servicewright.adapters.fastapi import context as context_mod

    monkeypatch.setattr(context_mod, "OTEL_AVAILABLE", False)
    setters = context_mod.get_default_context_setters()
    assert [type(s).__name__ for s in setters] == ["StructlogSetter"]

    with pytest.raises(ImportError, match="opentelemetry"):
        context_mod.OtelBaggageSetter()


async def test__context_setters__custom_list_configured__replaces_the_defaults() -> None:
    from servicewright.adapters.fastapi.middlewares import ContextMiddleware

    class _Recorder:
        seen: list[dict[str, Any]] = []

        def set(self, context_data: dict[str, Any]) -> Any:
            _Recorder.seen.append(dict(context_data))
            return lambda: None

    middlewares = MiddlewareConfig(context_setters=[_Recorder()])
    ep = FastApiEntrypoint(config=HttpConfig(port=0), middlewares=middlewares)
    app = await ep.build_app(_make_service_ctx(FakeContainer()))

    # The middleware stack carries our custom setter (not the defaults).
    context_layers = [m for m in app.user_middleware if m.cls is ContextMiddleware]
    assert len(context_layers) == 1
    assert [type(s).__name__ for s in context_layers[0].kwargs["context_setters"]] == ["_Recorder"]


def test__otel_baggage_setter__values_supplied__attaches_then_detaches_them() -> None:
    from opentelemetry import baggage as otel_baggage

    from servicewright.adapters.fastapi.context import OtelBaggageSetter

    setter = OtelBaggageSetter()
    remover = setter.set({"request_id": "r1", "user_id": "u1", "trace_id": "t1"})
    # The values ride the current OTel context (propagated by instrumented clients).
    assert otel_baggage.get_baggage("request_id") == "r1"
    assert otel_baggage.get_baggage("user_id") == "u1"
    assert otel_baggage.get_baggage("trace_id") == "t1"
    remover()
    # After the remover runs, the baggage is detached.
    assert otel_baggage.get_baggage("request_id") is None


def test__otel_baggage_setter__no_values__attaches_nothing() -> None:
    from servicewright.adapters.fastapi.context import OtelBaggageSetter

    remover = OtelBaggageSetter().set({})
    remover()  # nothing attached -> nothing to detach, no raise
    remover()  # remover is idempotent


def test__structlog_setter__values_supplied__binds_then_unbinds_them() -> None:
    import structlog

    from servicewright.adapters.fastapi.context import StructlogSetter

    setter = StructlogSetter()
    remover = setter.set({"request_id": "r1", "user_id": "u1", "trace_id": "t1"})
    bound = structlog.contextvars.get_contextvars()
    assert bound.get("request_id") == "r1"
    remover()
    assert "request_id" not in structlog.contextvars.get_contextvars()


def test__structlog_setter__no_values__binds_nothing() -> None:
    from servicewright.adapters.fastapi.context import StructlogSetter

    remover = setter_remover = StructlogSetter().set({})
    setter_remover()  # empty context -> nothing bound, no raise
    assert remover is setter_remover


def test__default_context_setters__inspected__include_the_installed_bridges() -> None:
    from servicewright.adapters.fastapi.context import (
        OtelBaggageSetter,
        StructlogSetter,
        get_default_context_setters,
    )

    setters = get_default_context_setters()
    assert any(isinstance(s, OtelBaggageSetter) for s in setters)
    assert any(isinstance(s, StructlogSetter) for s in setters)


async def test__context_middleware__request_served__binds_the_core_context_store() -> None:
    """Request identifiers are readable via the transport-neutral core store."""
    router = APIRouter()
    seen: dict[str, Any] = {}

    @router.get("/ctx")
    async def read_ctx() -> dict[str, Any]:
        seen["request_id"] = get_context_value("request_id")
        seen["user_id"] = get_context_value("user_id")
        return {}

    ep = FastApiEntrypoint(config=HttpConfig(port=0), routers=(router,))
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))

    client.get("/ctx", headers={"x-request-id": "req-1", "x-user-id": "user-9"})

    assert seen == {"request_id": "req-1", "user_id": "user-9"}
    # Outside the request the store is clean again.
    assert get_context_value("request_id") is None


async def test__context_middleware__log_unsafe_header_value__drops_it() -> None:
    """Log-unsafe / overlong identifiers are not bound; request_id regenerates."""
    from uuid import UUID

    router = APIRouter()
    seen: dict[str, Any] = {}

    @router.get("/ctx")
    async def read_ctx() -> dict[str, Any]:
        seen["request_id"] = get_context_value("request_id")
        seen["user_id"] = get_context_value("user_id")
        return {}

    ep = FastApiEntrypoint(config=HttpConfig(port=0), routers=(router,))
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))

    client.get("/ctx", headers={"x-request-id": "bad{injection}", "x-user-id": "u" * 300})

    # The unsafe request id was replaced by a generated UUID; user id dropped.
    assert UUID(seen["request_id"])
    assert seen["user_id"] is None


# --------------------------------------------------------------------------- #
# drain timeout / hard stop of a still-running task
# --------------------------------------------------------------------------- #
async def test__fastapi_drain__server_hangs__gives_up_after_the_grace(
    patched_uvicorn: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()

    async def drive() -> None:
        while "server" not in patched_uvicorn or not patched_uvicorn["server"].serve_started:
            await asyncio.sleep(0)
        stop.set()

    # Start serve in the background; it will return once stop is set.
    serve_task = asyncio.create_task(ep.serve(stop=stop))
    await drive()
    await serve_task

    # Replace the finished serve task with a never-finishing one so drain times out.
    async def hang() -> None:
        await asyncio.Event().wait()

    ep._runner._serve_task = asyncio.create_task(hang())
    ep._runner._stopped = False
    with caplog.at_level(logging.WARNING):
        await ep.drain(0.01)
    assert any("drain timed out" in r.message for r in caplog.records)
    ep._runner._serve_task.cancel()


async def test__fastapi_stop__serve_task_errored__logs_it(
    patched_uvicorn: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    await ep.bind(_make_service_ctx(FakeContainer()))
    ep._runner._server = uvicorn_mod.uvicorn.Server(uvicorn_mod.uvicorn.Config(ep.app))  # type: ignore[attr-defined]

    started = asyncio.Event()

    async def boom() -> None:
        started.set()
        try:
            await asyncio.Event().wait()  # block until cancelled
        except asyncio.CancelledError:
            # On hard stop the task is cancelled; surface a non-cancel error
            # so the entrypoint's defensive except-Exception path is exercised.
            raise RuntimeError("server task crashed") from None

    ep._runner._serve_task = asyncio.create_task(boom())
    await started.wait()  # task is running (NOT done) when stop() is called
    with caplog.at_level(logging.ERROR):
        await ep.stop()
    assert any("errored during hard stop" in r.message for r in caplog.records)
    assert ep._runner._stopped is True


async def test__fastapi_stop__serve_task_still_running__cancels_it(patched_uvicorn: dict[str, Any]) -> None:
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    await ep.bind(_make_service_ctx(FakeContainer()))

    # Build a server + a long-running task without going through serve()'s stop wait.
    server = uvicorn_mod.uvicorn.Server(uvicorn_mod.uvicorn.Config(ep.app))  # type: ignore[attr-defined]
    ep._runner._server = server

    async def hang() -> None:
        await asyncio.Event().wait()

    ep._runner._serve_task = asyncio.create_task(hang())
    await asyncio.sleep(0)  # let it start
    await ep.stop()
    assert server.should_exit is True
    assert server.force_exit is True
    assert ep._runner._serve_task.cancelled()


# --------------------------------------------------------------------------- #
# OTel inner instrumentor missing -> friendly degrade
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Regression cover for the super-review findings
# --------------------------------------------------------------------------- #
class _ClosingScope(FakeScope):
    """Unit scope that records when it is finalized."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = False


class _ClosingContainer(FakeContainer):
    """Container whose unit scope marks itself closed on exit."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []
        self.scope = _ClosingScope()

    @contextlib.asynccontextmanager
    async def unit_scope(self, context: Any = None) -> Any:
        self.events.append("scope-open")
        try:
            yield self.scope
        finally:
            self.scope.closed = True
            self.events.append("scope-close")


@pytest.fixture
def closing_container() -> _ClosingContainer:
    return _ClosingContainer()


@pytest.fixture
def streaming_app(closing_container: _ClosingContainer) -> FastAPI:
    """An app whose response streams chunks and then runs a background task."""
    from starlette.background import BackgroundTask
    from starlette.responses import StreamingResponse

    app = FastAPI()
    app.add_middleware(UnitScopeMiddleware, container=closing_container)

    @app.get("/stream")
    def stream() -> StreamingResponse:
        scope = closing_container.scope

        async def body() -> Any:
            for _ in range(3):
                await asyncio.sleep(0)
                closing_container.events.append("chunk-after-close" if scope.closed else "chunk")
                yield b"x"

        def audit() -> None:
            closing_container.events.append("background-after-close" if scope.closed else "background")

        return StreamingResponse(body(), background=BackgroundTask(audit))

    return app


def test__unit_scope_middleware__streaming_response__keeps_the_scope_open_for_every_chunk(
    streaming_app: FastAPI,
    closing_container: _ClosingContainer,
) -> None:
    # Act
    response = TestClient(streaming_app).get("/stream")

    # Assert
    assert response.content == b"xxx"
    assert "chunk-after-close" not in closing_container.events


def test__unit_scope_middleware__background_task__runs_before_the_scope_closes(
    streaming_app: FastAPI,
    closing_container: _ClosingContainer,
) -> None:
    # Act
    TestClient(streaming_app).get("/stream")

    # Assert
    assert closing_container.events[-1] == "scope-close"
    assert "background" in closing_container.events


def test__unit_scope_middleware__non_http_scope__passes_through_untouched(
    closing_container: _ClosingContainer,
) -> None:
    # Arrange
    seen: list[str] = []

    async def inner_app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    middleware = UnitScopeMiddleware(inner_app, container=closing_container)

    # Act
    asyncio.run(middleware({"type": "lifespan"}, _noop_receive, _noop_send))

    # Assert
    assert seen == ["lifespan"]
    assert closing_container.events == []


async def _noop_receive() -> Any:  # pragma: no cover - never awaited in these tests
    return {}


async def _noop_send(message: Any) -> None:  # pragma: no cover - never awaited
    return None


@pytest.fixture
async def correlated_client() -> Any:
    """A client over the full default middleware stack."""
    ep = FastApiEntrypoint(config=HttpConfig(port=0))
    app = await ep.build_app(_make_service_ctx(FakeContainer()))

    @app.get("/echo")
    def echo() -> dict[str, Any]:
        return {"request_id": get_context_value("request_id")}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("kaboom")

    return TestClient(app, raise_server_exceptions=False)


async def test__context_middleware__no_client_id__returns_the_id_it_logged(correlated_client: Any) -> None:
    # Act
    response = correlated_client.get("/echo")

    # Assert
    assert response.json()["request_id"] == response.headers["x-request-id"]


async def test__context_middleware__client_supplied_id__is_preserved_end_to_end(correlated_client: Any) -> None:
    # Act
    response = correlated_client.get("/echo", headers={"X-Request-ID": "order-sync-42"})

    # Assert
    assert response.json()["request_id"] == "order-sync-42"
    assert response.headers["x-request-id"] == "order-sync-42"


async def test__unhandled_exception__masked_500__still_carries_the_request_id(correlated_client: Any) -> None:
    # Act
    response = correlated_client.get("/boom")

    # Assert
    assert response.status_code == 500
    assert response.headers.get("x-request-id")


async def test__unhandled_exception__masked_500__does_not_leak_the_message(correlated_client: Any) -> None:
    # Act
    response = correlated_client.get("/boom")

    # Assert
    assert "kaboom" not in response.text


async def test__request_logging__enabled__emits_through_the_stdlib_logging_channel(
    correlated_client: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    caplog.set_level(logging.INFO, logger="servicewright.adapters.fastapi.middlewares.logging")

    # Act
    correlated_client.get("/echo")

    # Assert
    assert [record.message for record in caplog.records] == ["Request started", "Request finished"]


async def test__request_logging__log_level_raised__suppresses_the_request_lines(
    correlated_client: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    caplog.set_level(logging.WARNING, logger="servicewright.adapters.fastapi.middlewares.logging")

    # Act
    correlated_client.get("/echo")

    # Assert
    assert caplog.records == []


async def test__fastapi_entrypoint_bind__ephemeral_port__reports_the_port_the_os_picked() -> None:
    # Arrange
    ep = FastApiEntrypoint(config=HttpConfig(host="127.0.0.1", port=0))

    # Act
    await ep.bind(_make_service_ctx(FakeContainer()))

    # Assert
    try:
        assert isinstance(ep.bound_port, int)
        assert ep.bound_port > 0
    finally:
        await ep.stop()


async def test__fastapi_entrypoint_bind__port_already_taken__raises_instead_of_reporting_ready() -> None:
    # Arrange
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    taken_port = blocker.getsockname()[1]
    ep = FastApiEntrypoint(config=HttpConfig(host="127.0.0.1", port=taken_port))

    # Act & Assert
    try:
        with pytest.raises(OSError):
            await ep.bind(_make_service_ctx(FakeContainer()))
    finally:
        blocker.close()
