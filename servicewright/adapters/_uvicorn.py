"""Shared uvicorn driver for the ASGI entrypoints (FastAPI, Litestar).

Both ASGI adapters run the same server the same way, and the way matters: the
Host's lifecycle contract says ``serve()`` returns *while still accepting* so
readiness can flip before anything stops listening, and ``drain(grace)`` is what
closes the listener. Keeping one driver means that ordering — and the two
uvicorn quirks it has to work around — exist in exactly one place:

- **The socket is opened at bind time.** uvicorn binds inside ``serve()`` and
  reports failure with ``sys.exit(1)``; from a detached task that ``SystemExit``
  tears down the event loop and skips every shutdown step, after the process has
  already reported itself ready. Binding first turns a port clash into an
  ordinary ``OSError`` during startup, and makes ``port=0`` usable.
- **uvicorn's signal capture is neutralized.** ``Server.serve()`` wraps itself in
  ``capture_signals()``, which installs its own SIGINT/SIGTERM handlers and
  re-raises the signal with the default disposition on the way out — killing the
  process before the Host can drain, run hooks and flush telemetry. (The
  ``install_signal_handlers`` attribute that adapters used to overwrite was
  removed from uvicorn in 0.29 and silently did nothing.)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

try:
    import uvicorn
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("ASGI entrypoints require servicewright[fastapi] or servicewright[litestar].") from exc

logger = logging.getLogger(__name__)

DEFAULT_LISTEN_BACKLOG = 2048


@contextlib.contextmanager
def _no_signal_capture() -> Iterator[None]:
    """Replacement for ``uvicorn.Server.capture_signals``: install nothing."""
    yield


class UvicornRunner:
    """Drives one uvicorn server through the Host's bind/serve/drain/stop phases.

    Args:
        host: Interface to bind.
        port: Port to bind; ``0`` lets the OS choose (see :attr:`bound_port`).
        graceful_timeout: Seconds uvicorn lets in-flight requests finish during
            shutdown.
        uvicorn_kwargs: Extra ``uvicorn.Config`` keyword arguments.
        label: Transport name used in log records.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        graceful_timeout: float,
        uvicorn_kwargs: dict[str, Any] | None = None,
        label: str = "ASGI",
    ) -> None:
        self._host = host
        self._port = port
        self._graceful_timeout = graceful_timeout
        self._uvicorn_kwargs = dict(uvicorn_kwargs or {})
        self._label = label

        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._socket: socket.socket | None = None
        self._bound_port: int | None = None
        self._stopped = False

    @property
    def bound_port(self) -> int | None:
        """The port actually bound (``None`` before :meth:`bind`)."""
        return self._bound_port

    @property
    def server(self) -> uvicorn.Server | None:
        """The uvicorn server (``None`` before :meth:`serve`)."""
        return self._server

    def bind(self) -> int:
        """Open the listening socket and return the bound port.

        Raises:
            OSError: If the address cannot be bound — startup fails here rather
                than leaving a ready-but-not-listening process behind.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self._host, self._port))
            sock.listen(DEFAULT_LISTEN_BACKLOG)
        except OSError:
            sock.close()
            raise
        sock.set_inheritable(True)
        self._socket = sock
        self._bound_port = sock.getsockname()[1]
        return self._bound_port

    async def serve(self, app: Any, *, stop: asyncio.Event) -> None:
        """Serve ``app`` on the bound socket until ``stop`` is set.

        Returns while the server is still accepting connections; a server that
        dies on its own ends the wait and its failure is re-raised.
        """
        if self._socket is None:
            raise RuntimeError("serve() called before bind()")

        self._server = self._build_server(app)
        self._serve_task = asyncio.create_task(self._server.serve(sockets=[self._socket]))
        logger.info("%s server started", self._label, extra={"host": self._host, "port": self._bound_port})

        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({self._serve_task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter

        if self._serve_task.done():
            await self._reraise_server_failure(self._serve_task)

    async def drain(self, grace: float) -> None:
        """Close the listener and let in-flight requests finish within ``grace``."""
        if self._server is None or self._serve_task is None or self._stopped:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(self._serve_task), timeout=grace)
        except TimeoutError:
            logger.warning(
                "%s drain timed out", self._label, extra={"host": self._host, "port": self._bound_port, "grace": grace}
            )
        self._stopped = True

    async def stop(self) -> None:
        """Force the server down and release the socket (idempotent)."""
        if self._server is not None:
            self._server.should_exit = True
            self._server.force_exit = True
        if self._serve_task is not None and not self._serve_task.done():
            self._serve_task.cancel()
            try:
                await self._serve_task
            except (asyncio.CancelledError, SystemExit):
                pass
            except Exception:
                logger.exception("%s server task errored during hard stop", self._label)
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._stopped = True

    def _build_server(self, app: Any) -> uvicorn.Server:
        params: dict[str, Any] = {
            "host": self._host,
            "port": self._port,
            "log_config": None,
            "timeout_graceful_shutdown": self._graceful_timeout_seconds(),
        }
        params.update(self._uvicorn_kwargs)
        server = uvicorn.Server(uvicorn.Config(app, **params))
        # The Host owns OS signals; uvicorn must not install its own handlers.
        server.capture_signals = _no_signal_capture  # type: ignore[method-assign]
        return server

    def _graceful_timeout_seconds(self) -> int | None:
        """The configured grace as whole seconds (``None`` = wait indefinitely).

        uvicorn takes whole seconds, and truncating would turn a sub-second
        grace into ``0`` — which cancels in-flight requests immediately.
        """
        if self._graceful_timeout <= 0:
            return None
        return max(1, math.ceil(self._graceful_timeout))

    @staticmethod
    async def _reraise_server_failure(serve_task: asyncio.Task[None]) -> None:
        """Surface a server-task failure as a normal exception."""
        try:
            await serve_task
        except SystemExit as exc:
            raise RuntimeError(f"uvicorn exited with code {exc.code}") from exc


__all__ = ["DEFAULT_LISTEN_BACKLOG", "UvicornRunner"]
