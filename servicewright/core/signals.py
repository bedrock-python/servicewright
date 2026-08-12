"""OS signal handling for graceful shutdown (win32-safe)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from functools import partial
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

logger = logging.getLogger(__name__)

# Conventional base for signal-terminated exit codes (128 + signum).
SIGNAL_EXIT_CODE_BASE = 128

_HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _force_exit(signum: int) -> None:  # pragma: no cover - terminates the process
    """Terminate the process immediately, skipping every remaining cleanup step.

    Isolated in its own function so tests can substitute it: by definition it
    never returns, and it deliberately bypasses ``atexit``/``finally`` handlers
    because the reason to call it is that one of them is stuck.
    """
    os._exit(SIGNAL_EXIT_CODE_BASE + signum)


def install_signal_handlers(stop_event: asyncio.Event) -> Callable[[], None]:
    """Install SIGINT/SIGTERM handlers and return a callable that removes them.

    The first signal requests a graceful shutdown by setting ``stop_event``. A
    second signal escalates to an immediate ``128 + signum`` exit: cleanup can
    hang (a dead connection pool, a stuck ``stop()``), and an operator pressing
    Ctrl+C twice is asking for the process to die now rather than to re-set an
    event that is already set.

    Installing is best-effort: a failure to register is swallowed so the process
    still runs where signal handling is unavailable (e.g. a non-main thread). On
    Unix the asyncio loop handler is preferred with a ``signal.signal`` fallback;
    on Windows ``signal.signal`` schedules the event onto the loop thread-safely.

    Args:
        stop_event: Event that is set when the first shutdown signal arrives.

    Returns:
        An idempotent callable that restores the previous handlers. Call it once
        the run-loop is over — otherwise the handlers outlive the event loop and
        (on Windows and on the ``signal.signal`` fallback path) keep swallowing
        Ctrl+C for the rest of the process.
    """
    loop = asyncio.get_running_loop()
    signalled = False
    removers: list[Callable[[], None]] = []

    def request_stop(signum: int) -> None:
        nonlocal signalled
        if signalled:
            logger.warning(
                "Second shutdown signal received; exiting immediately",
                extra={"signal": signum, "exit_code": SIGNAL_EXIT_CODE_BASE + signum},
            )
            _force_exit(signum)
            return
        signalled = True
        logger.info("Shutdown signal received", extra={"signal": signum})
        stop_event.set()

    def install_fallback(sig: signal.Signals) -> None:
        """Register through the stdlib ``signal`` module and record the restore."""

        def handler(signum: int, _frame: FrameType | None) -> None:
            request_stop(signum)

        _register(sig, handler, removers)

    if sys.platform != "win32":
        for sig in _HANDLED_SIGNALS:
            try:
                loop.add_signal_handler(sig, partial(request_stop, int(sig)))
            except (RuntimeError, ValueError, NotImplementedError):
                install_fallback(sig)
            else:
                removers.append(partial(_remove_loop_handler, loop, sig))
    else:

        def win_handler(signum: int, _frame: FrameType | None) -> None:
            # The escalation path must not go through the loop: the reason for a
            # second signal is usually that the loop is no longer making progress.
            if signalled:
                request_stop(signum)
                return
            with contextlib.suppress(RuntimeError, ValueError):
                if not loop.is_closed():
                    loop.call_soon_threadsafe(request_stop, signum)

        for sig in _HANDLED_SIGNALS:
            _register(sig, win_handler, removers)

    def uninstall() -> None:
        while removers:
            with contextlib.suppress(Exception):
                removers.pop()()

    return uninstall


def _register(
    sig: signal.Signals,
    handler: Callable[[int, FrameType | None], None],
    removers: list[Callable[[], None]],
) -> None:
    """Install a ``signal.signal`` handler, recording how to restore the old one."""
    try:
        previous = signal.getsignal(sig)
        signal.signal(sig, handler)
    except (RuntimeError, ValueError, OSError):
        return
    removers.append(partial(_restore_handler, sig, previous))


def _restore_handler(sig: signal.Signals, previous: object) -> None:
    if previous is None:  # pragma: no cover - only for handlers set outside Python
        return
    signal.signal(sig, previous)  # type: ignore[arg-type]


def _remove_loop_handler(loop: asyncio.AbstractEventLoop, sig: signal.Signals) -> None:
    if loop.is_closed():
        return
    loop.remove_signal_handler(sig)
