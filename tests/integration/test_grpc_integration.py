"""Integration tests driving a real in-process gRPC server.

Marked ``integration`` so it does NOT run under ``-m unit``. It exercises the
actual grpc-server-kit primitives (no mocking): a real ``grpc.aio`` server is
created, bound to an ephemeral port and started, then queried over a real
channel — the standard health service in one test, a failing RPC in the other,
which is the only way to see the status grpc.aio itself would have chosen.
"""

from __future__ import annotations

import asyncio
from typing import Any

import grpc
import grpc.aio
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


_SERVICE = "lab.Orders"
_SECRET = "dsn=postgres://user:pw@db/ledger"


def _register_raising_servicer(server: Any, _ctx: Any) -> None:
    """Register one RPC that fails with a plain exception, no proto needed."""

    async def pay(request: bytes, context: Any) -> bytes:
        raise RuntimeError(_SECRET)

    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler(_SERVICE, {"Pay": grpc.unary_unary_rpc_method_handler(pay)}),)
    )


async def test__grpc_entrypoint__servicer_raises_an_unexpected_exception__client_gets_a_masked_internal() -> None:
    # Arrange
    spec: AppSpec[Any, Any] = AppSpec(service_name="grpc-errors-it", create_container=lambda _s: FakeContainer())
    config = GrpcConfig(host="127.0.0.1", port=0, enable_reflection=False, enable_channelz=False)
    ep = GrpcEntrypoint(config=config, servicers=_register_raising_servicer)
    service = Service(spec, entrypoints=[ep])

    stop = asyncio.Event()
    failures: list[grpc.aio.AioRpcError] = []

    async def call_then_stop() -> None:
        while not (spec.health.ready and ep.bound_port):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

        async with grpc.aio.insecure_channel(f"127.0.0.1:{ep.bound_port}") as channel:
            try:
                await channel.unary_unary(f"/{_SERVICE}/Pay")(b"42", timeout=5)
            except grpc.aio.AioRpcError as error:
                failures.append(error)

        stop.set()

    # Act
    await asyncio.gather(service.run(FakeSettings(), stop=stop), call_then_stop())

    # Assert — what grpc.aio would have sent unaided is UNKNOWN plus repr() of the exception.
    error = failures[0]
    assert error.code() is grpc.StatusCode.INTERNAL
    assert error.details() == "internal_error"
    assert dict(error.trailing_metadata() or ())["x-error-code"] == "internal_error"
    assert _SECRET not in str(error.details())
