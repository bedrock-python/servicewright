"""Unit tests for the Litestar entrypoint (servicewright.adapters.litestar).

The app is built via ``entrypoint.build_app(ctx)`` against a mocked
:class:`FakeContainer`; routes are exercised with ``litestar.testing.TestClient``.
The uvicorn server is mocked for serve/drain/stop (no real socket is bound).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from litestar import get
from litestar.di import Provide
from litestar.testing import TestClient

from servicewright import AppSpec, Entrypoint, Plugin, ServerEntrypoint, Service
from servicewright.adapters import _uvicorn as uvicorn_mod
from servicewright.adapters.litestar import (
    HealthConfig,
    LitestarConfig,
    LitestarEntrypoint,
    LitestarPlugin,
    UnitScopeMiddleware,
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
) -> ServiceContext:
    bootstrap = BootstrapContext(
        settings=FakeSettings(),
        service_name=service_name,
        container=container,
        lifecycle=object(),  # type: ignore[arg-type]
    )
    return ServiceContext(
        bootstrap=bootstrap,
        app_scope=FakeScope(),
        health=health or HealthRegistry(),
    )


async def _build_client(ep: LitestarEntrypoint, ctx: ServiceContext) -> TestClient:
    app = await ep.build_app(ctx)
    return TestClient(app=app)


class _FakeUvicornServer:
    """Stand-in for ``uvicorn.Server`` that records control-flag mutations."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.should_exit = False
        self.force_exit = False
        self.signals_captured = False
        self.serve_started = False

    @contextlib.contextmanager
    def capture_signals(self) -> Any:  # pragma: no cover - replaced by the entrypoint
        """Real uvicorn installs SIGINT/SIGTERM handlers here; record if it runs."""
        self.signals_captured = True
        yield

    async def serve(self, sockets: Any = None) -> None:
        self.serve_started = True
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
# LitestarConfig
# --------------------------------------------------------------------------- #
def test__litestar_config__default__uses_the_documented_values() -> None:
    config = LitestarConfig()
    assert config.host == "0.0.0.0"
    assert config.port == 8000
    assert config.graceful_timeout == 10.0
    assert config.address == "0.0.0.0:8000"
    assert config.health.liveness_path == "/system/livez"
    assert config.health.readiness_path == "/system/readyz"


def test__litestar_config__host_and_port_overridden__reports_the_address() -> None:
    assert LitestarConfig(host="127.0.0.1", port=0).address == "127.0.0.1:0"


def test__litestar_config__two_instances__do_not_share_mutable_defaults() -> None:
    c1 = LitestarConfig()
    c2 = LitestarConfig()
    assert c1.litestar_kwargs is not c2.litestar_kwargs
    assert c1.health is not c2.health


# --------------------------------------------------------------------------- #
# Entrypoint protocol conformance & attributes
# --------------------------------------------------------------------------- #
def test__litestar_entrypoint__constructed__satisfies_the_protocol_without_a_unit_scope() -> None:
    ep = LitestarEntrypoint()
    assert isinstance(ep, Entrypoint)
    assert isinstance(ep, ServerEntrypoint)
    assert ep.kind == "http"
    assert ep.essential is True
    # ServerEntrypoint must NOT expose unit_scope (framework owns per-request scope).
    assert not hasattr(ep, "unit_scope")


def test__litestar_entrypoint__kind_and_essential_overridden__reports_them() -> None:
    ep = LitestarEntrypoint(kind="litestar", essential=False)
    assert ep.kind == "litestar"
    assert ep.essential is False


def test__litestar_entrypoint__before_bind__exposes_its_config_and_no_app() -> None:
    config = LitestarConfig(port=12345)
    ep = LitestarEntrypoint(config=config)
    assert ep.config is config
    assert ep.app is None


def test__litestar_entrypoint__two_instances__do_not_share_a_config() -> None:
    ep1 = LitestarEntrypoint()
    ep2 = LitestarEntrypoint()
    assert ep1.config is not ep2.config


# --------------------------------------------------------------------------- #
# build_app / bind
# --------------------------------------------------------------------------- #
async def test__litestar_bind__called__builds_and_stores_the_app() -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    ctx = _make_service_ctx(FakeContainer())
    await ep.bind(ctx)
    assert ep.app is not None


async def test__litestar_build_app__route_handlers_given__registers_them() -> None:
    @get("/ping")
    async def ping() -> dict[str, str]:
        return {"pong": "ok"}

    ep = LitestarEntrypoint(config=LitestarConfig(port=0), route_handlers=(ping,))
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))
    resp = client.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"pong": "ok"}


async def test__litestar_build_app__sync_route_registerer__runs_it() -> None:
    seen: list[ServiceContext] = []

    @get("/registered")
    async def registered() -> dict[str, bool]:
        return {"ok": True}

    def register(ctx: ServiceContext) -> list[Any]:
        seen.append(ctx)
        return [registered]

    ep = LitestarEntrypoint(config=LitestarConfig(port=0), route_registerer=register)
    ctx = _make_service_ctx(FakeContainer())
    client = await _build_client(ep, ctx)
    assert seen == [ctx]
    assert client.get("/registered").json() == {"ok": True}


async def test__litestar_build_app__async_route_registerer__runs_it() -> None:
    @get("/async-route")
    async def async_route() -> dict[str, bool]:
        return {"ok": True}

    async def register(ctx: ServiceContext) -> list[Any]:
        return [async_route]

    ep = LitestarEntrypoint(config=LitestarConfig(port=0), route_registerer=register)
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))
    assert client.get("/async-route").json() == {"ok": True}


async def test__litestar_build_app__configure_hook_given__calls_it_last() -> None:
    calls: list[tuple[Any, ServiceContext]] = []

    def configure(app: Any, ctx: ServiceContext) -> None:
        calls.append((app, ctx))

    ep = LitestarEntrypoint(config=LitestarConfig(port=0), configure_app=configure)
    ctx = _make_service_ctx(FakeContainer())
    app = await ep.build_app(ctx)
    assert calls == [(app, ctx)]


async def test__litestar_build_app__extra_kwargs_given__forwards_them() -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0, litestar_kwargs={"debug": True}))
    app = await ep.build_app(_make_service_ctx(FakeContainer()))
    assert app.debug is True


# --------------------------------------------------------------------------- #
# Health routes
# --------------------------------------------------------------------------- #
async def test__litestar_liveness_route__process_is_up__answers_ok() -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    client = await _build_client(ep, _make_service_ctx(FakeContainer(), health=HealthRegistry()))
    resp = client.get("/system/livez")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test__litestar_readiness_route__ready_flag_not_set__answers_503() -> None:
    health = HealthRegistry()
    health.ready = False
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    client = await _build_client(ep, _make_service_ctx(FakeContainer(), health=health))
    resp = client.get("/system/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unhealthy"}


async def test__litestar_readiness_route__ready_and_checks_pass__answers_200() -> None:
    health = HealthRegistry()
    health.ready = True

    class _OkCheck:
        async def check(self) -> bool:
            return True

    health.add_check("db", _OkCheck())
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    client = await _build_client(ep, _make_service_ctx(FakeContainer(), health=health))
    resp = client.get("/system/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test__litestar_readiness_route__a_check_fails__answers_503() -> None:
    health = HealthRegistry()
    health.ready = True

    class _FailCheck:
        async def check(self) -> bool:
            return False

    health.add_check("db", _FailCheck())
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    client = await _build_client(ep, _make_service_ctx(FakeContainer(), health=health))
    assert client.get("/system/readyz").status_code == 503


async def test__litestar_health_routes__custom_paths_configured__are_served_there() -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0, health=HealthConfig(enabled=False)))
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))
    assert client.get("/system/livez").status_code == 404

    ep2 = LitestarEntrypoint(
        config=LitestarConfig(port=0, health=HealthConfig(liveness_path="/alive", readiness_path="/ready")),
    )
    client2 = await _build_client(ep2, _make_service_ctx(FakeContainer()))
    assert client2.get("/alive").status_code == 200
    assert client2.get("/ready").status_code == 503


# --------------------------------------------------------------------------- #
# Per-request UnitScope
# --------------------------------------------------------------------------- #
async def test__litestar_unit_scope__injected_as_a_dependency__resolves() -> None:
    @get("/scoped")
    async def scoped(unit_scope: Any) -> dict[str, Any]:
        return {"val": await unit_scope.get(str)}

    container = FakeContainer(provides={str: "dep-value"})
    ep = LitestarEntrypoint(config=LitestarConfig(port=0), route_handlers=(scoped,))
    client = await _build_client(ep, _make_service_ctx(container))
    resp = client.get("/scoped")
    assert resp.status_code == 200
    assert resp.json() == {"val": "dep-value"}
    assert container.unit_scopes_opened == 1


async def test__litestar_unit_scope__read_via_current_unit_scope__resolves() -> None:
    @get("/cv")
    async def cv() -> dict[str, Any]:
        scope = current_unit_scope()
        return {"val": await scope.get(str)}

    container = FakeContainer(provides={str: "cv-dep"})
    ep = LitestarEntrypoint(config=LitestarConfig(port=0), route_handlers=(cv,))
    client = await _build_client(ep, _make_service_ctx(container))
    assert client.get("/cv").json() == {"val": "cv-dep"}


async def test__litestar_unit_scope__opened__carries_the_request_as_context() -> None:
    @get("/ctx")
    async def ctx_route() -> dict[str, bool]:
        scope = current_unit_scope()
        ctx = getattr(scope, "context", None)
        return {"has_request": ctx is not None and "request" in ctx}

    container = FakeContainer()
    ep = LitestarEntrypoint(config=LitestarConfig(port=0), route_handlers=(ctx_route,))
    client = await _build_client(ep, _make_service_ctx(container))
    assert client.get("/ctx").json() == {"has_request": True}
    assert container.unit_contexts[0] is not None
    assert "request" in container.unit_contexts[0]


async def test__litestar_unit_scope__one_request__is_opened_exactly_once() -> None:
    @get("/x")
    async def x() -> dict[str, bool]:
        return {"ok": True}

    container = FakeContainer()
    ep = LitestarEntrypoint(config=LitestarConfig(port=0), route_handlers=(x,))
    client = await _build_client(ep, _make_service_ctx(container))
    client.get("/x")
    client.get("/x")
    assert container.unit_scopes_opened == 2


def test__litestar_current_unit_scope__called_outside_a_request__raises() -> None:
    with pytest.raises(LookupError, match="No active Litestar unit scope"):
        current_unit_scope()


def test__litestar_get_unit_scope__middleware_absent__raises() -> None:
    request = MagicMock()
    request.scope = {"state": {}}
    with pytest.raises(LookupError, match="No active Litestar unit scope on the connection state"):
        get_unit_scope(request)


def test__litestar_get_unit_scope__no_scope_on_the_connection__raises() -> None:
    request = MagicMock()
    request.scope = {}  # no state at all
    with pytest.raises(LookupError, match="No active Litestar unit scope"):
        get_unit_scope(request)


def test__litestar_config__default__owns_the_unit_scope() -> None:
    assert LitestarConfig().unit_scope is True


async def test__litestar_unit_scope__disabled__opens_no_scope() -> None:
    @get("/x")
    async def x() -> dict[str, bool]:
        return {"ok": True}

    container = FakeContainer()
    ep = LitestarEntrypoint(config=LitestarConfig(port=0, unit_scope=False), route_handlers=(x,))
    client = await _build_client(ep, _make_service_ctx(container))

    assert client.get("/x").json() == {"ok": True}
    assert container.unit_scopes_opened == 0


async def test__litestar_unit_scope__disabled__installs_neither_the_middleware_nor_the_dependency() -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0, unit_scope=False))
    app = await ep.build_app(_make_service_ctx(FakeContainer()))
    assert not any(isinstance(m, UnitScopeMiddleware) for m in app.middleware)
    assert "unit_scope" not in app.dependencies


async def test__litestar_unit_scope__disabled__releases_the_reserved_dependency_name() -> None:
    async def my_unit_scope() -> str:
        return "mine"

    @get("/mine")
    async def mine(unit_scope: str) -> dict[str, str]:
        return {"unit_scope": unit_scope}

    config = LitestarConfig(
        port=0,
        unit_scope=False,
        litestar_kwargs={"dependencies": {"unit_scope": Provide(my_unit_scope)}},
    )
    ep = LitestarEntrypoint(config=config, route_handlers=(mine,))
    client = await _build_client(ep, _make_service_ctx(FakeContainer()))

    assert client.get("/mine").json() == {"unit_scope": "mine"}


# --------------------------------------------------------------------------- #
# serve() / drain() / stop() drive uvicorn correctly (mocked)
# --------------------------------------------------------------------------- #
async def test__litestar_serve__called_before_bind__raises() -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(RuntimeError, match=r"serve\(\) called before bind\(\)"):
        await ep.serve(stop=stop)


async def test__litestar_serve__stop_set__returns_while_still_accepting(patched_uvicorn: dict[str, Any]) -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
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


async def test__litestar_serve__building_the_server__leaves_uvicorn_logging_to_the_host(
    patched_uvicorn: dict[str, Any],
) -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(host="127.0.0.1", port=0, graceful_timeout=7.0))
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


async def test__litestar_serve__extra_uvicorn_kwargs__forwards_them(patched_uvicorn: dict[str, Any]) -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0, uvicorn_kwargs={"workers": 1}))
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()

    async def release() -> None:
        while "config" not in patched_uvicorn:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(ep.serve(stop=stop), release())
    assert patched_uvicorn["config"].kwargs["workers"] == 1


async def test__litestar_drain__called__asks_uvicorn_to_stop_accepting(patched_uvicorn: dict[str, Any]) -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()

    async def drive() -> None:
        while "server" not in patched_uvicorn or not patched_uvicorn["server"].serve_started:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(ep.serve(stop=stop), drive())
    await ep.drain(1.0)
    assert patched_uvicorn["server"].should_exit is True
    assert ep._runner._stopped is True


async def test__litestar_drain__called_before_serve__does_nothing() -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    await ep.drain(1.0)  # no server -> must not raise


async def test__litestar_stop__called__forces_uvicorn_down(patched_uvicorn: dict[str, Any]) -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
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


async def test__litestar_stop__called_before_serve__does_nothing() -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    await ep.stop()  # no server -> must not raise


async def test__litestar_drain__server_hangs__gives_up_after_the_grace(
    patched_uvicorn: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    await ep.bind(_make_service_ctx(FakeContainer()))

    stop = asyncio.Event()

    async def drive() -> None:
        while "server" not in patched_uvicorn or not patched_uvicorn["server"].serve_started:
            await asyncio.sleep(0)
        stop.set()

    serve_task = asyncio.create_task(ep.serve(stop=stop))
    await drive()
    await serve_task

    async def hang() -> None:
        await asyncio.Event().wait()

    ep._runner._serve_task = asyncio.create_task(hang())
    ep._runner._stopped = False
    with caplog.at_level(logging.WARNING):
        await ep.drain(0.01)
    assert any("drain timed out" in r.message for r in caplog.records)
    ep._runner._serve_task.cancel()


async def test__litestar_stop__serve_task_errored__logs_it(
    patched_uvicorn: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    await ep.bind(_make_service_ctx(FakeContainer()))
    ep._runner._server = uvicorn_mod.uvicorn.Server(uvicorn_mod.uvicorn.Config(ep.app))  # type: ignore[attr-defined]

    started = asyncio.Event()

    async def boom() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("server task crashed") from None

    ep._runner._serve_task = asyncio.create_task(boom())
    await started.wait()
    with caplog.at_level(logging.ERROR):
        await ep.stop()
    assert any("errored during hard stop" in r.message for r in caplog.records)
    assert ep._runner._stopped is True


async def test__litestar_stop__serve_task_still_running__cancels_it(patched_uvicorn: dict[str, Any]) -> None:
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
    await ep.bind(_make_service_ctx(FakeContainer()))

    server = uvicorn_mod.uvicorn.Server(uvicorn_mod.uvicorn.Config(ep.app))  # type: ignore[attr-defined]
    ep._runner._server = server

    async def hang() -> None:
        await asyncio.Event().wait()

    ep._runner._serve_task = asyncio.create_task(hang())
    await asyncio.sleep(0)
    await ep.stop()
    assert server.should_exit is True
    assert server.force_exit is True
    assert ep._runner._serve_task.cancelled()


# --------------------------------------------------------------------------- #
# LitestarPlugin
# --------------------------------------------------------------------------- #
def test__litestar_plugin__constructed__satisfies_the_plugin_protocol() -> None:
    plugin = LitestarPlugin()
    assert isinstance(plugin, Plugin)


def test__litestar_plugin__constructed__exposes_its_entrypoint() -> None:
    plugin = LitestarPlugin(config=LitestarConfig(port=0), kind="litestar", essential=False)
    ep = plugin.entrypoint
    assert isinstance(ep, LitestarEntrypoint)
    assert ep.kind == "litestar"
    assert ep.essential is False


def test__litestar_plugin_on_register__called__adds_its_entrypoint_to_the_host() -> None:
    plugin = LitestarPlugin()
    host = MagicMock()
    plugin.on_register(spec=MagicMock(), host=host)
    host.add_entrypoint.assert_called_once_with(plugin.entrypoint)


# --------------------------------------------------------------------------- #
# End-to-end-ish: drive a LitestarEntrypoint through a real Host/Service
# --------------------------------------------------------------------------- #
async def test__litestar_entrypoint__driven_by_a_service__completes_the_lifecycle(
    patched_uvicorn: dict[str, Any],
) -> None:
    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="litestar-service", create_container=lambda _s: container)
    ep = LitestarEntrypoint(config=LitestarConfig(port=0))
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
    assert spec.health.ready is False
    assert container.app_scopes_opened == 1


async def test__litestar_plugin__driven_by_a_service__completes_the_lifecycle(patched_uvicorn: dict[str, Any]) -> None:
    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="litestar-plugin-service", create_container=lambda _s: container)
    plugin = LitestarPlugin(config=LitestarConfig(port=0))
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
