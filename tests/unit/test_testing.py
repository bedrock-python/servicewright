"""Unit tests for the shipped test doubles (``servicewright.testing``)."""

from __future__ import annotations

import asyncio

import pytest

from servicewright.testing import FakeContainer, FakeEntrypoint, FakeScope, FakeSettings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "section",
    [
        pytest.param("logging", id="logging"),
        pytest.param("error_tracking", id="error-tracking"),
        pytest.param("tracing", id="tracing"),
        pytest.param("metrics", id="metrics"),
    ],
)
def test__fake_settings__default__leaves_every_observability_section_off(section: str) -> None:
    # Act
    value = getattr(FakeSettings(), section)

    # Assert
    assert value is None


def test__fake_settings__default__reports_a_placeholder_version() -> None:
    # Act & Assert
    assert FakeSettings().get_app_version() == "0.0.0-test"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        pytest.param(str, "value", id="by-type"),
        pytest.param("name", 1, id="by-name"),
    ],
)
async def test__fake_scope_get__key_was_provided__resolves_it(key: object, expected: object) -> None:
    # Arrange
    scope = FakeScope({str: "value", "name": 1})

    # Act
    resolved = await scope.get(key)  # type: ignore[arg-type]

    # Assert
    assert resolved == expected


async def test__fake_scope_get__key_was_not_provided__raises_key_error() -> None:
    # Act & Assert
    with pytest.raises(KeyError):
        await FakeScope().get(str)


async def test__fake_container_app_scope__entered__resolves_and_counts_the_scope() -> None:
    # Arrange
    container = FakeContainer({str: "x"})

    # Act
    async with container.app_scope() as scope:
        resolved = await scope.get(str)

    # Assert
    assert resolved == "x"
    assert container.app_scopes_opened == 1


async def test__fake_container_unit_scope__entered_with_context__records_it() -> None:
    # Arrange
    container = FakeContainer()

    # Act
    async with container.unit_scope({"k": "v"}) as scope:
        carried = scope.context  # type: ignore[attr-defined]

    # Assert
    assert carried == {"k": "v"}
    assert container.unit_contexts == [{"k": "v"}]


async def test__fake_entrypoint_serve__run_once__returns_without_waiting() -> None:
    # Arrange
    entrypoint = FakeEntrypoint(run_once=True)

    # Act
    await entrypoint.serve(stop=asyncio.Event())

    # Assert
    assert entrypoint.events == ["serve"]


async def test__fake_entrypoint_serve__long_running__returns_once_stop_is_set() -> None:
    # Arrange
    entrypoint = FakeEntrypoint()
    stop = asyncio.Event()

    async def release() -> None:
        await asyncio.sleep(0)
        stop.set()

    # Act
    await asyncio.gather(entrypoint.serve(stop=stop), release())

    # Assert
    assert entrypoint.events == ["serve"]
