"""Unit tests for OS signal handling: graceful first signal, forced second."""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from servicewright.core import signals as signals_mod
from servicewright.core.signals import SIGNAL_EXIT_CODE_BASE, install_signal_handlers

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.unit

IS_WINDOWS = sys.platform == "win32"


@pytest.fixture
def stop() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def forced_exits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Capture force-exit requests instead of killing the test process."""
    calls: list[int] = []
    monkeypatch.setattr(signals_mod, "_force_exit", calls.append)
    return calls


@pytest.fixture
async def captured_handlers(monkeypatch: pytest.MonkeyPatch) -> dict[int, Any]:
    """Capture the handlers the installer registers, on either platform.

    Async on purpose: the POSIX branch patches the *running* loop, which only
    exists inside the test's own loop.
    """
    registered: dict[int, Any] = {}

    if IS_WINDOWS:
        monkeypatch.setattr(signal, "getsignal", lambda sig: signal.SIG_DFL)
        monkeypatch.setattr(signal, "signal", lambda sig, handler: registered.setdefault(int(sig), handler))
    else:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda sig, callback, *args: registered.setdefault(int(sig), callback),
        )
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: True)
    return registered


def _fire(handlers: dict[int, Any], sig: signal.Signals) -> None:
    """Invoke a captured handler the way its platform would."""
    handler = handlers[int(sig)]
    if IS_WINDOWS:
        handler(int(sig), None)
    else:
        handler()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test__install_signal_handlers__first_signal__sets_the_stop_event(
    stop: asyncio.Event,
    captured_handlers: dict[int, Any],
    forced_exits: list[int],
    sig: signal.Signals,
) -> None:
    # Arrange
    install_signal_handlers(stop)

    # Act
    _fire(captured_handlers, sig)
    await asyncio.sleep(0)  # win32 schedules onto the loop

    # Assert
    assert stop.is_set() is True
    assert forced_exits == []


async def test__install_signal_handlers__second_signal__forces_exit_with_the_signal_code(
    stop: asyncio.Event,
    captured_handlers: dict[int, Any],
    forced_exits: list[int],
) -> None:
    # Arrange
    install_signal_handlers(stop)
    _fire(captured_handlers, signal.SIGINT)
    await asyncio.sleep(0)

    # Act
    _fire(captured_handlers, signal.SIGINT)
    await asyncio.sleep(0)

    # Assert
    assert forced_exits == [int(signal.SIGINT)]
    assert SIGNAL_EXIT_CODE_BASE + int(signal.SIGINT) == 130


async def test__install_signal_handlers__stop_then_a_different_signal__still_forces_exit(
    stop: asyncio.Event,
    captured_handlers: dict[int, Any],
    forced_exits: list[int],
) -> None:
    # Arrange
    install_signal_handlers(stop)
    _fire(captured_handlers, signal.SIGTERM)
    await asyncio.sleep(0)

    # Act
    _fire(captured_handlers, signal.SIGINT)
    await asyncio.sleep(0)

    # Assert
    assert forced_exits == [int(signal.SIGINT)]


async def test__install_signal_handlers__uninstall_called__restores_the_previous_handlers(
    stop: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    removed: list[int] = []

    if IS_WINDOWS:
        sentinel = signal.SIG_IGN
        restored: dict[int, Any] = {}
        monkeypatch.setattr(signal, "getsignal", lambda sig: sentinel)

        def fake_signal(sig: signal.Signals, handler: Any) -> Any:
            if handler is sentinel:
                restored[int(sig)] = handler
                removed.append(int(sig))
            return sentinel

        monkeypatch.setattr(signal, "signal", fake_signal)
    else:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback, *args: None)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(int(sig)))

    uninstall = install_signal_handlers(stop)

    # Act
    uninstall()

    # Assert
    assert sorted(removed) == sorted([int(signal.SIGINT), int(signal.SIGTERM)])


async def test__install_signal_handlers__uninstall_called_twice__is_a_no_op(
    stop: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    removals: list[int] = []

    if IS_WINDOWS:
        monkeypatch.setattr(signal, "getsignal", lambda sig: signal.SIG_DFL)
        monkeypatch.setattr(signal, "signal", lambda sig, handler: removals.append(int(sig)))
    else:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback, *args: None)
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removals.append(int(sig)))

    uninstall = install_signal_handlers(stop)
    uninstall()
    before = len(removals)

    # Act
    uninstall()

    # Assert
    assert len(removals) == before


@pytest.mark.skipif(IS_WINDOWS, reason="the loop-handler path is POSIX-only")
async def test__install_signal_handlers__loop_registration_unavailable__falls_back_to_signal_module(
    stop: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    loop = asyncio.get_running_loop()

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise NotImplementedError("no loop handlers here")

    monkeypatch.setattr(loop, "add_signal_handler", unavailable)
    fallback = MagicMock(return_value=signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", fallback)

    # Act
    install_signal_handlers(stop)

    # Assert
    assert fallback.called


async def test__install_signal_handlers__registration_always_fails__does_not_raise(
    stop: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    monkeypatch.setattr(signal, "signal", MagicMock(side_effect=ValueError("bad")))
    if not IS_WINDOWS:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", MagicMock(side_effect=RuntimeError("no")))

    # Act
    uninstall: Callable[[], None] = install_signal_handlers(stop)
    uninstall()

    # Assert
    assert stop.is_set() is False
