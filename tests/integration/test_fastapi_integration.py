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
from fastapi import APIRouter

from servicewright import AppSpec, Service, propagation_metadata
from servicewright.adapters.fastapi import FastApiEntrypoint, HttpConfig, HttpServerSettings
from servicewright.adapters.settings import BaseServiceSettings
from servicewright.testing import FakeContainer, FakeSettings

pytestmark = pytest.mark.integration


class _Settings(BaseServiceSettings):
    server: HttpServerSettings = HttpServerSettings()


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


async def test__fastapi_entrypoint__request_outlives_the_drain_grace__uvicorn_cancels_it_at_the_grace() -> None:
    # Arrange
    port = _free_port()
    spec: AppSpec[Any, Any] = AppSpec(
        service_name="http-it-service", create_container=lambda _s: FakeContainer(), drain_grace_seconds=1.0
    )
    router = APIRouter()
    handler_finished = False

    @router.get("/report")
    async def report() -> dict[str, str]:
        nonlocal handler_finished
        await asyncio.sleep(5.0)
        handler_finished = True
        return {"status": "done"}

    ep = FastApiEntrypoint(config=HttpConfig(host="127.0.0.1", port=port), routers=(router,))
    stop = asyncio.Event()

    async def request_then_stop() -> httpx.Response:
        while not spec.health.ready:
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as client:
            request = asyncio.create_task(client.get("/report"))
            await asyncio.sleep(0.2)
            stop.set()
            return await request

    # Act
    _, response = await asyncio.gather(
        Service(spec, entrypoints=[ep]).run(FakeSettings(), stop=stop), request_then_stop()
    )

    # Assert
    assert response.status_code == 500, "uvicorn cancels the request itself, not a hard stop that orphans it"
    assert handler_finished is False


async def test__fastapi_entrypoint__context_headers_from_the_environment__forwards_only_the_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    monkeypatch.setenv("SERVER__MIDDLEWARES__CONTEXT_HEADERS", '{"x-request-id":"request_id"}')
    port = _free_port()
    spec: AppSpec[Any, Any] = AppSpec(service_name="http-it-service", create_container=lambda _s: FakeContainer())
    router = APIRouter()

    @router.get("/outbound")
    async def outbound() -> dict[str, str]:
        return propagation_metadata()

    ep = FastApiEntrypoint(
        config=HttpConfig(host="127.0.0.1", port=port),
        middlewares=_Settings().server.middlewares.to_config(),
        routers=(router,),
    )
    stop = asyncio.Event()

    async def request_then_stop() -> httpx.Response:
        while not spec.health.ready:
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=2.0) as client:
            try:
                return await client.get(
                    "/outbound", headers={"X-Request-ID": "req-1", "X-User-Id": "user-9", "X-Tenant-Id": "tenant-3"}
                )
            finally:
                stop.set()

    # Act
    _, response = await asyncio.gather(
        Service(spec, entrypoints=[ep]).run(FakeSettings(), stop=stop), request_then_stop()
    )

    # Assert
    assert response.json() == {"x-request-id": "req-1"}
