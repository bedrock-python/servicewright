"""Request processing time measurement middleware."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.datastructures import MutableHeaders

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_PROCESS_TIME_HEADER = "X-Process-Time"


class ProcessingTimeMiddleware:
    """Adds the total request processing time to a response header."""

    def __init__(self, app: ASGIApp, header_name: str = DEFAULT_PROCESS_TIME_HEADER) -> None:
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.perf_counter_ns()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                process_time_sec = (time.perf_counter_ns() - start_time) / 1_000_000_000
                headers = MutableHeaders(scope=message)
                headers.append(self.header_name, str(process_time_sec))
            await send(message)

        await self.app(scope, receive, send_wrapper)


__all__ = ["DEFAULT_PROCESS_TIME_HEADER", "ProcessingTimeMiddleware"]
