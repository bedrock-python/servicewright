"""Unit tests for the dishka DI adapter (servicewright.adapters.dishka).

Exercises a REAL small dishka container (an APP-scoped singleton + a
REQUEST-scoped dependency keyed off the request context), asserting that the
:class:`DishkaContainer` maps ``AppScope`` <-> ``Scope.APP`` and ``UnitScope``
<-> ``Scope.REQUEST`` and finalizes each tier on scope exit.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from dishka import Provider, Scope, from_context, make_async_container, provide

from servicewright import AppSpec, Service
from servicewright.adapters.dishka import DishkaContainer, DishkaScope
from servicewright.testing import FakeEntrypoint, FakeSettings

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
