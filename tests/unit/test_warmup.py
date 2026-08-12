"""Unit tests for warmer collection and the warmup orchestrator."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from servicewright.core.contracts import AsyncWarmer
from servicewright.core.exceptions import WarmupTimeoutError
from servicewright.core.warmup import collect_warmers, perform_warmup

pytestmark = pytest.mark.unit

SERVICE_NAME = "test-service"


@pytest.fixture
def app_ctx() -> MagicMock:
    return MagicMock()


@pytest.fixture
def warmers() -> list[AsyncWarmer]:
    return [MagicMock()]


async def test__collect_warmers__base_list_and_factory__returns_both(app_ctx: MagicMock) -> None:
    # Arrange
    base_warmer = MagicMock()
    factory_warmer = MagicMock()
    warmers_factory = AsyncMock(return_value=[factory_warmer])

    # Act
    collected = await collect_warmers([base_warmer], warmers_factory, app_ctx)

    # Assert
    assert collected == [base_warmer, factory_warmer]
    warmers_factory.assert_called_once_with(app_ctx)


async def test__collect_warmers__nothing_configured__returns_an_empty_list(app_ctx: MagicMock) -> None:
    # Act
    collected = await collect_warmers(None, None, app_ctx)

    # Assert
    assert collected == []


async def test__collect_warmers__synchronous_factory__uses_its_return_value_directly(app_ctx: MagicMock) -> None:
    # Arrange
    warmer = MagicMock()
    warmers_factory = MagicMock(return_value=[warmer])

    # Act
    collected = await collect_warmers(None, warmers_factory, app_ctx)

    # Assert
    assert collected == [warmer]


@patch("servicewright.core.warmup.orchestrator.warmup_async")
async def test__perform_warmup__warmers_given__runs_them_fail_fast(
    mock_warmup_async: MagicMock,
    warmers: list[AsyncWarmer],
) -> None:
    # Act
    await perform_warmup(SERVICE_NAME, warmers)

    # Assert
    mock_warmup_async.assert_called_once_with(warmers=warmers, raise_on_failure=True)


@patch("servicewright.core.warmup.orchestrator.warmup_async")
async def test__perform_warmup__no_warmers__does_nothing(mock_warmup_async: MagicMock) -> None:
    # Act
    await perform_warmup(SERVICE_NAME, [])

    # Assert
    mock_warmup_async.assert_not_called()


@patch("servicewright.core.warmup.orchestrator.warmup_async")
async def test__perform_warmup__a_warmer_fails__propagates_the_error(
    mock_warmup_async: MagicMock,
    warmers: list[AsyncWarmer],
) -> None:
    # Arrange
    mock_warmup_async.side_effect = ValueError("warmup boom")

    # Act & Assert
    with pytest.raises(ValueError, match="warmup boom"):
        await perform_warmup(SERVICE_NAME, warmers)


async def test__perform_warmup__non_positive_timeout__raises(warmers: list[AsyncWarmer]) -> None:
    # Act & Assert
    with pytest.raises(ValueError, match="timeout must be positive"):
        await perform_warmup(SERVICE_NAME, warmers, timeout=0)


@patch("servicewright.core.warmup.orchestrator.warmup_async")
async def test__perform_warmup__warmup_outlasts_the_timeout__raises_warmup_timeout_error(
    mock_warmup_async: MagicMock,
    warmers: list[AsyncWarmer],
) -> None:
    # Arrange
    async def never_finishes(**_kwargs: Any) -> None:
        await asyncio.sleep(1)

    mock_warmup_async.side_effect = never_finishes

    # Act & Assert
    with pytest.raises(WarmupTimeoutError):
        await perform_warmup(SERVICE_NAME, warmers, timeout=0.01)


@patch("servicewright.core.warmup.orchestrator.warmup_async")
async def test__perform_warmup__cancelled__propagates_the_cancellation(
    mock_warmup_async: MagicMock,
    warmers: list[AsyncWarmer],
) -> None:
    # Arrange
    mock_warmup_async.side_effect = asyncio.CancelledError

    # Act & Assert
    with pytest.raises(asyncio.CancelledError):
        await perform_warmup(SERVICE_NAME, warmers)
