"""Unit tests for the dishka DI adapter (servicewright.adapters.dishka).

Exercises a REAL small dishka container (an APP-scoped singleton + a
REQUEST-scoped dependency keyed off the request context), asserting that the
:class:`DishkaContainer` maps ``AppScope`` <-> ``Scope.APP`` and ``UnitScope``
<-> ``Scope.REQUEST`` and finalizes each tier on scope exit.

The last section drives dishka's OWN FastAPI / Litestar integrations
(``setup_dishka`` + ``FromDishka``) under the servicewright entrypoints with the
adapters' request scope switched off, and checks that the adapter refuses to
double-open the request scope when it is not.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from dishka import AsyncContainer, FromDishka, Provider, Scope, from_context, make_async_container, provide
from dishka.integrations.fastapi import inject as inject_fastapi
from dishka.integrations.fastapi import setup_dishka as setup_dishka_fastapi
from dishka.integrations.litestar import inject as inject_litestar
from dishka.integrations.litestar import setup_dishka as setup_dishka_litestar
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from litestar import Request as LitestarRequest
from litestar import get
from litestar.testing import TestClient as LitestarTestClient

from servicewright import AppSpec, Service
from servicewright.adapters.dishka import DishkaContainer, DishkaScope
from servicewright.adapters.fastapi import FastApiEntrypoint, HttpConfig, MiddlewareConfig
from servicewright.adapters.litestar import LitestarConfig, LitestarEntrypoint
from servicewright.core.health import HealthRegistry
from servicewright.core.spec import BootstrapContext, ServiceContext
from servicewright.testing import FakeEntrypoint, FakeScope, FakeSettings

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Test doubles: a real dishka container
# --------------------------------------------------------------------------- #
class Singleton:
    """An APP-scoped singleton resource."""


class RequestDep:
    """A REQUEST-scoped dependency carrying the request context value."""

    def __init__(self, ctx_value: str) -> None:
        self.ctx_value = ctx_value


def _make_provider(events: list[str]) -> Provider:
    class _Provider(Provider):
        ctx_value = from_context(provides=str, scope=Scope.REQUEST)

        @provide(scope=Scope.APP)
        async def singleton(self) -> AsyncIterator[Singleton]:
            instance = Singleton()
            events.append("app-enter")
            yield instance
            events.append("app-exit")

        @provide(scope=Scope.REQUEST)
        async def request_dep(self, ctx_value: str) -> AsyncIterator[RequestDep]:
            events.append("req-enter")
            yield RequestDep(ctx_value)
            events.append("req-exit")

    return _Provider()


def _make_container(events: list[str] | None = None) -> DishkaContainer:
    return DishkaContainer(make_async_container(_make_provider(events if events is not None else [])))


# --------------------------------------------------------------------------- #
# Protocol conformance
# --------------------------------------------------------------------------- #
def test__dishka_container__constructed__exposes_both_scope_factories() -> None:
    container = _make_container()
    assert hasattr(container, "app_scope")
    assert hasattr(container, "unit_scope")


def test__dishka_container__constructed__exposes_the_wrapped_container() -> None:
    inner = make_async_container(_make_provider([]))
    container = DishkaContainer(inner)
    assert container.container is inner


# --------------------------------------------------------------------------- #
# app_scope: resolves APP singleton + finalizes on exit
# --------------------------------------------------------------------------- #
async def test__app_scope__singleton_requested_twice__returns_one_instance() -> None:
    container = _make_container()
    async with container.app_scope() as scope:
        assert isinstance(scope, DishkaScope)
        s1 = await scope.get(Singleton)
        s2 = await scope.get(Singleton)
        assert isinstance(s1, Singleton)
        assert s1 is s2  # one instance per APP scope
    await container.container.close()  # idempotent: already closed by app_scope exit


async def test__app_scope__exited__runs_the_app_scoped_finalizers() -> None:
    events: list[str] = []
    container = _make_container(events)
    async with container.app_scope() as scope:
        await scope.get(Singleton)
        assert events == ["app-enter"]
    # Exiting app_scope must close the APP scope -> finalizer runs.
    assert events == ["app-enter", "app-exit"]


async def test__app_scope__entered__exposes_the_wrapped_container() -> None:
    container = _make_container()
    async with container.app_scope() as scope:
        assert scope.container is container.container


# --------------------------------------------------------------------------- #
# unit_scope: resolves REQUEST dep from context + finalizes on exit
# --------------------------------------------------------------------------- #
async def test__unit_scope__context_supplied__resolves_request_deps_from_it() -> None:
    container = _make_container()
    async with container.app_scope(), container.unit_scope({str: "ctx-hello"}) as scope:
        dep = await scope.get(RequestDep)
        assert dep.ctx_value == "ctx-hello"
        # The raw context value is resolvable by its key too.
        assert await scope.get(str) == "ctx-hello"


async def test__unit_scope__exited__runs_the_request_scoped_finalizers() -> None:
    events: list[str] = []
    container = _make_container(events)
    async with container.app_scope():
        async with container.unit_scope({str: "v"}) as scope:
            await scope.get(RequestDep)
            assert "req-enter" in events
            assert "req-exit" not in events
        # Exiting unit_scope must close the REQUEST scope -> finalizer runs.
        assert events[-1] == "req-exit"


async def test__unit_scope__no_context_supplied__still_opens() -> None:
    # A REQUEST scope can be opened without a context (no context-keyed deps used).
    container = DishkaContainer(make_async_container(Provider()))
    async with container.app_scope(), container.unit_scope() as scope:
        assert isinstance(scope, DishkaScope)


async def test__unit_scope__two_units__resolve_independent_dependencies() -> None:
    container = _make_container()
    async with container.app_scope():
        async with container.unit_scope({str: "a"}) as scope_a:
            dep_a = await scope_a.get(RequestDep)
        async with container.unit_scope({str: "b"}) as scope_b:
            dep_b = await scope_b.get(RequestDep)
    assert dep_a.ctx_value == "a"
    assert dep_b.ctx_value == "b"
    assert dep_a is not dep_b


async def test__scopes__nested__finalize_request_before_app() -> None:
    events: list[str] = []
    container = _make_container(events)
    async with container.app_scope() as app_scope:
        await app_scope.get(Singleton)
        async with container.unit_scope({str: "v"}) as unit_scope:
            await unit_scope.get(RequestDep)
        # REQUEST finalized first, APP still open.
        assert events == ["app-enter", "req-enter", "req-exit"]
    # APP finalized last on app_scope exit.
    assert events == ["app-enter", "req-enter", "req-exit", "app-exit"]


# --------------------------------------------------------------------------- #
# Drives through a real Host/Service as the container
# --------------------------------------------------------------------------- #
async def test__dishka_container__driven_by_a_real_service__brackets_the_app_scope() -> None:
    events: list[str] = []
    dishka_container = _make_container(events)

    spec: AppSpec[Any, Any] = AppSpec(service_name="dishka-service", create_container=lambda _s: dishka_container)
    ep = FakeEntrypoint(run_once=False)
    service = Service(spec, entrypoints=[ep])

    stop = asyncio.Event()

    async def open_unit_then_stop() -> None:
        while not spec.health.ready:
            await asyncio.sleep(0)
        # The bound entrypoint can open a per-unit dishka REQUEST scope. Resolving
        # the APP-scoped singleton from the REQUEST child triggers its creation.
        async with ep.unit_scope({str: "in-service"}) as scope:
            singleton = await scope.get(Singleton)
            assert isinstance(singleton, Singleton)
            dep = await scope.get(RequestDep)
            assert dep.ctx_value == "in-service"
        stop.set()

    await asyncio.gather(service.run(FakeSettings(), stop=stop), open_unit_then_stop())

    # APP scope opened once and finalized on shutdown; REQUEST finalized inline.
    assert events[0] == "app-enter"
    assert "req-exit" in events
    assert events[-1] == "app-exit"


# --------------------------------------------------------------------------- #
# dishka's own framework integration owns the request scope
# --------------------------------------------------------------------------- #
class CurrentPath:
    """A REQUEST-scoped dependency built from the framework's own ``Request``."""

    def __init__(self, path: str) -> None:
        self.path = path


def _make_fastapi_provider(events: list[str]) -> Provider:
    class _Provider(Provider):
        request = from_context(provides=Request, scope=Scope.REQUEST)

        @provide(scope=Scope.REQUEST)
        async def current_path(self, request: Request) -> AsyncIterator[CurrentPath]:
            events.append("req-enter")
            yield CurrentPath(request.url.path)
            events.append("req-exit")

    return _Provider()


def _make_litestar_provider(events: list[str]) -> Provider:
    class _Provider(Provider):
        request = from_context(provides=LitestarRequest, scope=Scope.REQUEST)

        @provide(scope=Scope.REQUEST)
        async def current_path(self, request: LitestarRequest) -> AsyncIterator[CurrentPath]:
            events.append("req-enter")
            yield CurrentPath(request.url.path)
            events.append("req-exit")

    return _Provider()


class _CountingDishkaContainer(DishkaContainer):
    """Records how often servicewright asks it for a unit scope."""

    def __init__(self, container: AsyncContainer) -> None:
        super().__init__(container)
        self.unit_scopes_opened = 0

    def unit_scope(
        self, context: Mapping[Any, Any] | None = None
    ) -> contextlib.AbstractAsyncContextManager[DishkaScope]:
        self.unit_scopes_opened += 1
        return super().unit_scope(context)


def _make_service_ctx(container: DishkaContainer) -> ServiceContext[Any, Any]:
    bootstrap: BootstrapContext[Any, Any] = BootstrapContext(
        settings=FakeSettings(),
        service_name="svc",
        container=container,
        lifecycle=object(),  # type: ignore[arg-type]
    )
    return ServiceContext(bootstrap=bootstrap, app_scope=FakeScope(), health=HealthRegistry())


def _http_request(state: dict[str, Any] | None = None) -> Request:
    scope: dict[str, Any] = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    if state is not None:
        scope["state"] = state
    return Request(scope)


async def test__unit_scope__request_already_scoped_by_dishkas_own_integration__raises() -> None:
    container = _make_container()
    request = _http_request(state={"dishka_container": object()})

    async with container.app_scope():
        with pytest.raises(RuntimeError, match="unit_scope=False"):
            async with container.unit_scope({"request": request}):
                pass


async def test__unit_scope__request_not_scoped_by_dishkas_own_integration__opens() -> None:
    container = _make_container()

    async with container.app_scope(), container.unit_scope({"request": _http_request()}) as scope:
        assert isinstance(scope, DishkaScope)


async def test__fastapi_entrypoint__dishka_owns_the_request_scope__from_dishka_resolves_through_its_scope() -> None:
    events: list[str] = []
    container = _CountingDishkaContainer(make_async_container(_make_fastapi_provider(events)))
    router = APIRouter()

    @router.get("/path")
    @inject_fastapi
    async def read_path(current_path: FromDishka[CurrentPath]) -> dict[str, str]:
        return {"path": current_path.path}

    ep = FastApiEntrypoint(
        config=HttpConfig(port=0),
        routers=(router,),
        middlewares=MiddlewareConfig(unit_scope=False),
        configure_app=lambda app, ctx: setup_dishka_fastapi(ctx.container.container, app),
    )

    async with container.app_scope():
        resp = TestClient(await ep.build_app(_make_service_ctx(container))).get("/path")

    assert resp.status_code == 200
    assert resp.json() == {"path": "/path"}
    # dishka's middleware opened the one REQUEST scope (with Request in its
    # context); servicewright opened none.
    assert events == ["req-enter", "req-exit"]
    assert container.unit_scopes_opened == 0


async def test__fastapi_entrypoint__dishka_integration_next_to_the_unit_scope__fails_the_first_request_loudly() -> None:
    container = _make_container()
    ep = FastApiEntrypoint(
        config=HttpConfig(port=0),
        configure_app=lambda app, ctx: setup_dishka_fastapi(ctx.container.container, app),
    )

    async with container.app_scope():
        client = TestClient(await ep.build_app(_make_service_ctx(container)), raise_server_exceptions=True)
        with pytest.raises(RuntimeError, match="two REQUEST scopes per request"):
            client.get("/system/health/readyz")


async def test__litestar_entrypoint__dishka_owns_the_request_scope__from_dishka_resolves_through_its_scope() -> None:
    events: list[str] = []
    container = _CountingDishkaContainer(make_async_container(_make_litestar_provider(events)))

    @get("/path")
    @inject_litestar
    async def read_path(current_path: FromDishka[CurrentPath]) -> dict[str, str]:
        return {"path": current_path.path}

    ep = LitestarEntrypoint(
        config=LitestarConfig(port=0, unit_scope=False),
        route_handlers=(read_path,),
        configure_app=lambda app, ctx: setup_dishka_litestar(ctx.container.container, app),
    )

    async with container.app_scope():
        resp = LitestarTestClient(await ep.build_app(_make_service_ctx(container))).get("/path")

    assert resp.status_code == 200
    assert resp.json() == {"path": "/path"}
    assert events == ["req-enter", "req-exit"]
    assert container.unit_scopes_opened == 0


async def test__litestar_entrypoint__dishka_integration_next_to_the_unit_scope__fails_the_first_request_loudly() -> (
    None
):
    container = _make_container()
    ep = LitestarEntrypoint(
        config=LitestarConfig(port=0, litestar_kwargs={"debug": True}),
        configure_app=lambda app, ctx: setup_dishka_litestar(ctx.container.container, app),
    )

    async with container.app_scope():
        resp = LitestarTestClient(await ep.build_app(_make_service_ctx(container))).get("/system/readyz")

    assert resp.status_code == 500
    assert "two REQUEST scopes per request" in resp.text
