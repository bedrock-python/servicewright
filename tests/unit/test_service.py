"""Unit tests for the Service facade and the zero-dependency builtin entrypoints."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from servicewright import AppSpec, DaemonEntrypoint, OneShotEntrypoint, Service, run
from servicewright.testing import FakeContainer, FakeEntrypoint, FakeSettings

pytestmark = pytest.mark.unit


@pytest.fixture
def container() -> FakeContainer:
    return FakeContainer()


@pytest.fixture
def spec(container: FakeContainer) -> AppSpec[Any, Any]:
    return AppSpec(service_name="svc", create_container=lambda _s: container)


def test__service__built_with_entrypoints_and_plugins__exposes_them(spec: AppSpec[Any, Any]) -> None:
    # Arrange
    entrypoint = FakeEntrypoint()
    plugin = object()

    # Act
    service = Service(spec, entrypoints=[entrypoint], plugins=[plugin])  # type: ignore[list-item]

    # Assert
    assert service.entrypoints == [entrypoint]
    assert service.plugins == [plugin]


async def test__service_run__essential_entrypoint_exits__drives_the_full_lifecycle(spec: AppSpec[Any, Any]) -> None:
    # Arrange
    entrypoint = FakeEntrypoint(run_once=True)
    service = Service(spec, entrypoints=[entrypoint])

    # Act
    await service.run(FakeSettings())

    # Assert
    assert entrypoint.events == ["bind", "serve", "drain", "stop"]
    assert spec.health.ready is False


async def test__module_level_run__called__drives_the_service(spec: AppSpec[Any, Any]) -> None:
    # Arrange
    entrypoint = FakeEntrypoint(run_once=True)

    # Act
    await run(Service(spec, entrypoints=[entrypoint]), FakeSettings())

    # Assert
    assert entrypoint.events[0] == "bind"


async def test__one_shot_entrypoint__run__executes_the_job_inside_one_unit_scope(spec: AppSpec[Any, Any]) -> None:
    # Arrange
    container = FakeContainer(provides={str: "dep"})
    spec.create_container = lambda _s: container
    resolved: list[str] = []

    async def job(scope: Any) -> None:
        resolved.append(await scope.get(str))

    # Act
    await Service(spec, entrypoints=[OneShotEntrypoint(job)]).run(FakeSettings())

    # Assert
    assert resolved == ["dep"]
    assert container.unit_scopes_opened == 1


async def test__daemon_entrypoint__run__loops_until_stop_is_set(
    spec: AppSpec[Any, Any],
    container: FakeContainer,
) -> None:
    # Arrange
    iterations = 0

    async def loop(scope: Any, stop: asyncio.Event) -> None:
        nonlocal iterations
        while not stop.is_set():
            iterations += 1
            if iterations >= 3:
                stop.set()
            await asyncio.sleep(0)

    # Act
    await Service(spec, entrypoints=[DaemonEntrypoint(loop)]).run(FakeSettings(), stop=asyncio.Event())

    # Assert
    assert iterations == 3
    assert container.unit_scopes_opened == 1


@pytest.mark.parametrize(
    ("entrypoint", "expected_kind"),
    [
        pytest.param(OneShotEntrypoint(lambda _s: None, kind="batch", essential=False), "batch", id="one-shot"),  # type: ignore[arg-type,return-value]
        pytest.param(DaemonEntrypoint(lambda _s, _e: None, kind="stream", essential=False), "stream", id="daemon"),  # type: ignore[arg-type,return-value]
    ],
)
def test__builtin_entrypoint__kind_and_essential_overridden__reports_them(
    entrypoint: Any,
    expected_kind: str,
) -> None:
    # Assert
    assert entrypoint.kind == expected_kind
    assert entrypoint.essential is False
