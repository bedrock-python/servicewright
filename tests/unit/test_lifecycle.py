"""Unit tests for the lifecycle hook manager."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_lazy_fixtures import lf

from servicewright.core.contracts import LifecycleHookProtocol
from servicewright.core.lifecycle import Lifecycle

pytestmark = pytest.mark.unit


@pytest.fixture
def manager() -> Lifecycle:
    return Lifecycle()


@pytest.fixture
def app_scope() -> MagicMock:
    return MagicMock()


@pytest.fixture
def start_phases() -> tuple[str, str]:
    return ("add_pre_start_hook", "run_pre_start_hooks")


@pytest.fixture
def post_start_phases() -> tuple[str, str]:
    return ("add_post_start_hook", "run_post_start_hooks")


@pytest.fixture
def pre_shutdown_phases() -> tuple[str, str]:
    return ("add_pre_shutdown_hook", "run_pre_shutdown_hooks")


@pytest.fixture
def post_shutdown_phases() -> tuple[str, str]:
    return ("add_post_shutdown_hook", "run_post_shutdown_hooks")


@pytest.mark.parametrize(
    "phase",
    [
        lf("start_phases"),
        lf("post_start_phases"),
        lf("pre_shutdown_phases"),
        lf("post_shutdown_phases"),
    ],
)
async def test__lifecycle__hook_registered_for_a_phase__runs_with_the_app_scope(
    manager: Lifecycle,
    app_scope: MagicMock,
    phase: tuple[str, str],
) -> None:
    # Arrange
    add_hook, run_hooks = phase
    hook = AsyncMock()
    getattr(manager, add_hook)(hook)

    # Act
    await getattr(manager, run_hooks)(app_scope)

    # Assert
    hook.assert_called_once_with(app_scope)


async def test__lifecycle_start_hooks__one_hook_raises__stops_before_the_next(manager: Lifecycle) -> None:
    # Arrange
    first = AsyncMock()
    failing = AsyncMock(side_effect=ValueError("hook boom"))
    never_reached = AsyncMock()
    for hook in (first, failing, never_reached):
        manager.add_pre_start_hook(hook)

    # Act
    with pytest.raises(ValueError, match="hook boom"):
        await manager.run_pre_start_hooks()

    # Assert
    first.assert_called_once()
    never_reached.assert_not_called()


async def test__lifecycle_shutdown_hooks__one_hook_raises__still_runs_the_rest(manager: Lifecycle) -> None:
    # Arrange
    failing = AsyncMock(side_effect=ValueError("first boom"))
    later = AsyncMock(side_effect=ValueError("second boom"))
    manager.add_pre_shutdown_hook(failing)
    manager.add_pre_shutdown_hook(later)

    # Act
    await manager.run_pre_shutdown_hooks()

    # Assert
    later.assert_called_once()


async def test__lifecycle__zero_argument_hook__is_called_without_the_app_scope(
    manager: Lifecycle,
    app_scope: MagicMock,
) -> None:
    # Arrange
    called = False

    async def hook_without_arguments() -> None:
        nonlocal called
        called = True

    # The Lifecycle introspects the signature, so a zero-argument callable is
    # accepted even though it does not satisfy the protocol.
    manager.add_pre_start_hook(cast(LifecycleHookProtocol, hook_without_arguments))

    # Act
    await manager.run_pre_start_hooks(app_scope)

    # Assert
    assert called is True


async def test__lifecycle_shutdown_hooks__hook_is_cancelled__propagates_the_cancellation(
    manager: Lifecycle,
) -> None:
    # Arrange
    async def cancelling_hook(app_scope: Any = None) -> None:
        raise asyncio.CancelledError

    manager.add_pre_shutdown_hook(cancelling_hook)

    # Act & Assert
    # CancelledError must always propagate, even from best-effort shutdown hooks.
    with pytest.raises(asyncio.CancelledError):
        await manager.run_pre_shutdown_hooks(MagicMock())
