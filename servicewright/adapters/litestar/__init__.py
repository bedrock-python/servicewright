"""Litestar entrypoint adapter (``[litestar]`` extra)."""

from __future__ import annotations

from .config import HealthConfig, LitestarConfig
from .configurators import build_health_routes
from .entrypoint import (
    ConfigureApp,
    LitestarEntrypoint,
    LitestarPlugin,
    RouteRegisterer,
)
from .unit_scope import (
    UnitScopeMiddleware,
    current_unit_scope,
    get_unit_scope,
)

__all__ = [
    "ConfigureApp",
    "HealthConfig",
    "LitestarConfig",
    "LitestarEntrypoint",
    "LitestarPlugin",
    "RouteRegisterer",
    "UnitScopeMiddleware",
    "build_health_routes",
    "current_unit_scope",
    "get_unit_scope",
]
