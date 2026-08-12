"""Integration test driving a real in-process uvicorn server.

Marked ``integration`` so it does NOT run under ``-m unit``. It exercises the
actual FastAPI + uvicorn stack (no mocking): a real ``uvicorn.Server`` is bound
to an ephemeral port through a real ``Host``/``Service``, ``/system/readyz`` is
queried over a real httpx client, then the service is gracefully shut down.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest

from servicewright import AppSpec, Service
from servicewright.adapters.fastapi import FastApiEntrypoint, HttpConfig
from servicewright.testing import FakeContainer, FakeSettings

pytestmark = pytest.mark.integration


def _free_port() -> int:
    """Reserve and release an ephemeral TCP port (robust on win32)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test__fastapi_entrypoint__real_server__serves_requests_then_drains() -> None:
    port = _free_port()
    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="http-it-service", create_container=lambda _s: container)

    ep = FastApiEntrypoint(config=HttpConfig(host="127.0.0.1", port=port, graceful_timeout=2.0))
    service = Service(spec, entrypoints=[ep])

    stop = asyncio.Event()
    readiness: list[int] = []

    async def probe_then_stop() -> None:
        # Wait for readiness; then poll the real server until it accepts requests.
        while not spec.health.ready:
            await asyncio.sleep(0.01)

        base_url = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
            for _ in range(200):  # ~2s max startup wait
                try:
                    resp = await client.get("/system/health/readyz")
                except httpx.TransportError:
                    await asyncio.sleep(0.01)
                    continue
                readiness.append(resp.status_code)
                break

        stop.set()

    await asyncio.gather(service.run(FakeSettings(), stop=stop), probe_then_stop())

    # The ready service answered the readiness probe with 200.
    assert readiness == [200]
    # After graceful shutdown, readiness is flipped off and the app was built.
    assert spec.health.ready is False
    assert ep.app is not None
