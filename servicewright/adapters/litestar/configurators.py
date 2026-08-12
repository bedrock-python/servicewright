"""Configurators that assemble the Litestar app: health probe routes.

Health routes are driven by the transport-agnostic
:class:`~servicewright.core.health.HealthRegistry`. Liveness always reports ``ok`` (the
process is up); readiness reflects :meth:`HealthRegistry.readiness` (the ``ready``
flag AND every registered check), returning 200 when healthy and 503 otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._imports import (
    HTTP_200_OK,
    HTTP_503_SERVICE_UNAVAILABLE,
    MediaType,
    Response,
    get,
)

if TYPE_CHECKING:
    from litestar.handlers import HTTPRouteHandler

    from ...core.health import HealthRegistry


def build_health_routes(health: HealthRegistry, *, liveness_path: str, readiness_path: str) -> list[HTTPRouteHandler]:
    """Return ``/system`` liveness + readiness route handlers bound to ``health``.

    Args:
        health: The transport-agnostic registry to read state from.
        liveness_path: Route path for the liveness probe.
        readiness_path: Route path for the readiness probe.

    Returns:
        A list of Litestar route handlers ready to pass to ``Litestar(route_handlers=...)``.
    """

    @get(liveness_path, media_type=MediaType.JSON, tags=["health"], include_in_schema=False)
    async def liveness() -> dict[str, str]:
        await health.liveness()
        return {"status": "ok"}

    @get(readiness_path, tags=["health"], include_in_schema=False)
    async def readiness() -> Response:
        report = await health.readiness()
        status_code = HTTP_200_OK if report.healthy else HTTP_503_SERVICE_UNAVAILABLE
        return Response(
            content={"status": "ok" if report.healthy else "unhealthy"},
            status_code=status_code,
            media_type=MediaType.JSON,
        )

    return [liveness, readiness]


__all__ = ["build_health_routes"]
