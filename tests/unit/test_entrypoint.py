"""Unit tests for the entrypoint author base classes and the Plugin protocol."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from servicewright import AppSpec, Entrypoint, Plugin, ScopedEntrypoint, ServerEntrypoint
from servicewright.core.health import HealthRegistry
from servicewright.core.spec import BootstrapContext, ServiceContext
from servicewright.testing import FakeContainer, FakeSettings

pytestmark = pytest.mark.unit


class _Server(ServerEntrypoint):
    """Minimal ServerEntrypoint: the framework owns the per-request scope."""

    def __init__(self) -> None:
        self.served = False

    async def serve(self, *, stop: asyncio.Event) -> None:
        self.served = True
        await stop.wait()


class _Scoped(ScopedEntrypoint):
    """Minimal ScopedEntrypoint: the entrypoint opens the per-unit scope."""

    async def serve(self, *, stop: asyncio.Event) -> None:
        await stop.wait()


def _service_ctx(container: FakeContainer) -> ServiceContext[Any, Any]:
    bootstrap = BootstrapContext(
        settings=FakeSettings(),
        service_name="svc",
        container=container,
        lifecycle=object(),  # type: ignore[arg-type]
    )
    return ServiceContext(bootstrap=bootstrap, app_scope=object(), health=HealthRegistry())  # type: ignore[arg-type]


@pytest.fixture
def server() -> _Server:
    return _Server()


@pytest.fixture
def scoped() -> _Scoped:
    return _Scoped()


def test__server_entrypoint__subclassed__satisfies_the_entrypoint_protocol(server: _Server) -> None:
    # Assert
    assert isinstance(server, Entrypoint)
    assert (server.kind, server.essential) == ("server", True)


def test__server_entrypoint__subclassed__does_not_expose_a_unit_scope(server: _Server) -> None:
    # Assert
    # The double-scope rule is enforced by type: a framework already opens the
    # per-request scope, so this base must not offer a second one.
    assert not hasattr(server, "unit_scope")


async def test__server_entrypoint__lifecycle_methods_not_overridden__are_no_ops(server: _Server) -> None:
    # Arrange
    stop = asyncio.Event()
    stop.set()

    # Act
    await server.bind(_service_ctx(FakeContainer()))
    await server.serve(stop=stop)
    await server.drain(1.0)
    await server.stop()

    # Assert
    assert server.served is True


def test__scoped_entrypoint_unit_scope__called_before_bind__raises(scoped: _Scoped) -> None:
    # Act & Assert
    with pytest.raises(RuntimeError, match="before bind"):
        scoped.unit_scope()


async def test__scoped_entrypoint_unit_scope__opened_after_bind__resolves_from_the_container(
    scoped: _Scoped,
) -> None:
    # Arrange
    container = FakeContainer(provides={str: "hello"})
    await scoped.bind(_service_ctx(container))

    # Act
    async with scoped.unit_scope({"request": "x"}) as scope:
        resolved = await scope.get(str)

    # Assert
    assert resolved == "hello"
    assert container.unit_contexts == [{"request": "x"}]


async def test__scoped_entrypoint__drain_and_stop_not_overridden__are_no_ops(scoped: _Scoped) -> None:
    # Act & Assert
    await scoped.drain(1.0)
    await scoped.stop()


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        pytest.param("plugin", True, id="implements-on-register"),
        pytest.param("plain-object", False, id="does-not"),
    ],
)
def test__plugin_protocol__runtime_checked__matches_only_on_register_implementors(
    candidate: str,
    expected: bool,
) -> None:
    # Arrange
    class _MyPlugin:
        def on_register(self, spec: AppSpec[Any, Any], host: Any) -> None:
            return None

    instance: object = _MyPlugin() if candidate == "plugin" else object()

    # Act
    result = isinstance(instance, Plugin)

    # Assert
    assert result is expected
