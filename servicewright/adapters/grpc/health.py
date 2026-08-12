"""Bridge the standard gRPC health service to the servicewright HealthRegistry.

The grpc ``grpc.health.v1.Health`` servicer is registered on the server and its
status is driven from the transport-agnostic :class:`HealthRegistry`: it reports
``SERVING`` exactly while :meth:`HealthRegistry.readiness` is healthy — the
``ready`` flag AND every registered check — and ``NOT_SERVING`` otherwise, which
is the same verdict the HTTP ``/readyz`` route returns.

The gRPC health service is a PUSH API (the servicer answers from a stored
status), so a poller has to re-evaluate readiness: :meth:`GrpcHealthBridge.watch`
does that on an interval, and :class:`GrpcEntrypoint` runs it for the lifetime of
``serve()``. On drain the bridge enters graceful shutdown, which pins every
tracked name to ``NOT_SERVING`` so load balancers stop routing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ._imports import grpc, health_aio, health_pb2, health_pb2_grpc

if TYPE_CHECKING:
    from ...core.health import HealthRegistry

logger = logging.getLogger(__name__)

# Empty service name == overall server health (the convention checked by k8s).
_OVERALL_SERVICE = ""

_SERVING = health_pb2.HealthCheckResponse.SERVING
_NOT_SERVING = health_pb2.HealthCheckResponse.NOT_SERVING


class GrpcHealthBridge:
    """Owns the aio health servicer and syncs it with a :class:`HealthRegistry`."""

    def __init__(self, registry: HealthRegistry, *, service_names: tuple[str, ...] = ()) -> None:
        self._registry = registry
        self._servicer = health_aio.HealthServicer()
        # Report status for the overall server plus any concrete service names.
        self._service_names: tuple[str, ...] = (_OVERALL_SERVICE, *service_names)

    def register(self, server: grpc.aio.Server) -> None:
        """Add the health servicer to ``server``."""
        health_pb2_grpc.add_HealthServicer_to_server(self._servicer, server)

    async def refresh(self) -> None:
        """Evaluate readiness and push the verdict onto the health servicer.

        Runs every registered health check (same contract as the HTTP readiness
        route), so a dependency outage flips the health service to
        ``NOT_SERVING``.
        """
        report = await self._registry.readiness()
        status = _SERVING if report.healthy else _NOT_SERVING
        for name in self._service_names:
            await self._servicer.set(name, status)

    async def watch(self, interval: float) -> None:
        """Re-evaluate readiness every ``interval`` seconds until cancelled.

        A refresh that fails is logged and retried on the next tick: a health
        poller must never take the server down.
        """
        if interval <= 0:
            return
        while True:
            await asyncio.sleep(interval)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("gRPC health refresh failed")

    async def enter_graceful_shutdown(self) -> None:
        """Flip every tracked service to ``NOT_SERVING`` for draining."""
        await self._servicer.enter_graceful_shutdown()


__all__ = ["GrpcHealthBridge"]
