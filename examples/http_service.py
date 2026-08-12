"""A real HTTP service: FastAPI entrypoint, health probes, graceful drain.

This example shows how to:

1. Run a `FastApiEntrypoint` under the Host. The entrypoint carries its own
   `HttpConfig` (never global settings), builds the app during `bind`, and runs
   uvicorn with uvicorn's own signal capture neutralized -- the Host owns
   signals and the whole lifecycle.
2. Resolve per-request dependencies from the unit scope the adapter opens
   around every request (`UnitScopeDep`), here backed by a real dishka
   container: `Scope.APP` <-> app scope, `Scope.REQUEST` <-> unit scope.
3. Get `/system/health/livez` and `/system/health/readyz` for free, driven by
   the very same `HealthRegistry` the Host flips, and see a `ServiceError`
   render as an RFC 9457 `application/problem+json` document on the wire.
4. Drain gracefully: `serve()` returns while the server is still accepting, so
   readiness flips to false BEFORE `drain()` closes the listener -- a load
   balancer removes the pod while in-flight requests still finish.

The interleaved `Request started` / `Request finished` lines below come from the
adapter's `LoggingMiddleware`, which logs through the configured logging sink and
carries the `request_id` the context middleware bound (the same id the response
returns in `X-Request-ID`).

Run with: `python examples/http_service.py`. It starts a real uvicorn server on
an ephemeral loopback port, drives it with an httpx client, shuts down
gracefully and exits 0.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx
from dishka import Provider, Scope, make_async_container, provide
from fastapi import APIRouter

from servicewright import AppSpec, ErrorKind, Service, ServiceError, UnitScopeProtocol
from servicewright.adapters.dishka import DishkaContainer
from servicewright.adapters.fastapi import FastApiEntrypoint, HttpConfig, UnitScopeDep

SERVICE_NAME = "orders-service"
GRACEFUL_TIMEOUT_SECONDS = 2.0
STARTUP_POLL_ATTEMPTS = 200
STARTUP_POLL_INTERVAL = 0.01


@dataclass(frozen=True)
class Settings:
    """All `BaseServiceSettingsProtocol` asks for; every observability section is off."""

    logging: object | None = None
    metrics: object | None = None
    tracing: object | None = None
    error_tracking: object | None = None

    def get_app_version(self) -> str:
        return "1.0.0"


class OrderMissingError(ServiceError):
    """Business error; the code auto-derives to `order_missing`."""

    kind = ErrorKind.NOT_FOUND


class OrderRepository:
    """An APP-scoped singleton -- one instance for the whole process."""

    def __init__(self) -> None:
        self._orders = {"42": {"id": "42", "item": "anvil", "status": "shipped"}}

    def get(self, order_id: str) -> dict[str, str]:
        order = self._orders.get(order_id)
        if order is None:
            raise OrderMissingError("No such order.", params={"order_id": order_id})
        return order


class AppProvider(Provider):
    """The dishka wiring: one APP-scoped repository."""

    @provide(scope=Scope.APP)
    def order_repository(self) -> OrderRepository:
        return OrderRepository()


router = APIRouter()


async def load_order(scope: UnitScopeProtocol, order_id: str) -> dict[str, str]:
    """Transport-free business code: all it needs is a scope to resolve from."""
    repository = await scope.get(OrderRepository)
    return repository.get(order_id)


@router.get("/orders/{order_id}")
async def get_order(order_id: str, scope: UnitScopeDep) -> dict[str, str]:
    """The route: hand the per-request unit scope to the business code."""
    return await load_order(scope, order_id)


def build_container(_settings: Settings) -> DishkaContainer:
    """The AppSpec's container factory: a dishka container behind the adapter."""
    return DishkaContainer(make_async_container(AppProvider()))


async def wait_until_serving(client: httpx.AsyncClient, path: str) -> None:
    """Poll the readiness route until the socket actually accepts connections."""
    for _ in range(STARTUP_POLL_ATTEMPTS):
        try:
            await client.get(path)
        except httpx.TransportError:
            await asyncio.sleep(STARTUP_POLL_INTERVAL)
            continue
        return
    raise RuntimeError("server did not start in time")


async def call_service(base_url: str, ready_flag: str) -> None:
    """Drive the running server over a real HTTP client."""
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
        await wait_until_serving(client, "/system/health/readyz")

        print(f"\n--- calling the service on {base_url} (readiness flag={ready_flag}) ---")
        for path in ("/orders/42", "/orders/999", "/system/health/livez", "/system/health/readyz"):
            response = await client.get(path)
            content_type = response.headers.get("content-type", "")
            print(f"[client] GET {path:<26} -> {response.status_code} {content_type}")
            print(f"[client]     {json.dumps(response.json(), sort_keys=True)}")


async def main() -> None:
    spec: AppSpec[Settings, DishkaContainer] = AppSpec(
        service_name=SERVICE_NAME,
        create_container=build_container,
    )

    http = FastApiEntrypoint(
        config=HttpConfig(
            host="127.0.0.1",
            # 0 = let the OS pick; the entrypoint reports it as `bound_port`
            # once `bind()` has opened the socket.
            port=0,
            title=SERVICE_NAME,
            graceful_timeout=GRACEFUL_TIMEOUT_SECONDS,
        ),
        routers=(router,),
    )
    service = Service(spec, entrypoints=[http])

    # Supplying `stop` is the embedding path: the Host installs no signal
    # handlers. A deployed service omits it and lets SIGTERM set this event.
    stop = asyncio.Event()

    async def drive_then_stop() -> None:
        while not spec.health.ready:
            await asyncio.sleep(STARTUP_POLL_INTERVAL)
        await call_service(f"http://127.0.0.1:{http.bound_port}", str(spec.health.ready))
        print("\n--- graceful shutdown ---")
        stop.set()

    print(f"[server] starting {SERVICE_NAME} on 127.0.0.1 (ephemeral port)")
    await asyncio.gather(service.run(Settings(), stop=stop), drive_then_stop())

    print(f"[server] uvicorn drained; readiness flag is now {spec.health.ready}")
    # The built app outlives the run, which is what makes `build_app` testable.
    app: Any = http.app
    print(f"[server] the app built at bind() is still inspectable: title={app.title!r}")


if __name__ == "__main__":
    asyncio.run(main())
