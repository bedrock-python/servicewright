"""Integration test driving a real in-process gRPC server.

Marked ``integration`` so it does NOT run under ``-m unit``. It exercises the
actual grpc-server-kit primitives (no mocking): a real ``grpc.aio`` server is
created, bound to an ephemeral port, started, the standard health service is
queried over a real channel, then the entrypoint is drained and stopped.
"""

from __future__ import annotations

import asyncio
from typing import Any

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

from servicewright import AppSpec, Service
from servicewright.adapters.grpc import GrpcConfig, GrpcEntrypoint
from servicewright.testing import FakeContainer, FakeSettings

pytestmark = pytest.mark.integration


async def test__grpc_entrypoint__real_server__serves_rpcs_then_drains() -> None:
    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="grpc-it-service", create_container=lambda _s: container)

    # Ephemeral port, channelz off to keep the surface minimal; reflection off
    # so we do not depend on reflection service names beyond the health service.
    config = GrpcConfig(host="127.0.0.1", port=0, enable_reflection=False, enable_channelz=False)
    ep = GrpcEntrypoint(config=config, servicers=lambda _server, _ctx: None)
    service = Service(spec, entrypoints=[ep])

    stop = asyncio.Event()
    health_status: list[Any] = []

    async def probe_then_stop() -> None:
        # Wait for readiness + a bound port + a started server.
        while not (spec.health.ready and ep.bound_port):
            await asyncio.sleep(0.01)
        # Give serve() a moment to start the server.
        await asyncio.sleep(0.05)

        async with grpc.aio.insecure_channel(f"127.0.0.1:{ep.bound_port}") as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            response = await stub.Check(health_pb2.HealthCheckRequest(service=""))
            health_status.append(response.status)

        stop.set()

    await asyncio.gather(service.run(FakeSettings(), stop=stop), probe_then_stop())

    # While serving and ready, the overall health service reported SERVING.
    assert health_status == [health_pb2.HealthCheckResponse.SERVING]
    # After shutdown, readiness is flipped off and the port was actually bound.
    assert spec.health.ready is False
    assert ep.bound_port is not None and ep.bound_port > 0
