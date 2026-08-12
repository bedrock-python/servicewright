"""Self-contained configuration for :class:`LitestarEntrypoint`.

The :class:`AppSpec` stays transport-neutral, so the Litestar entrypoint owns its
own configuration. Config is taken AT CONSTRUCTION (never read from global
settings). This binding is deliberately lean and general (no platform middleware
stack / context machinery): just server params, app/uvicorn kwargs, and the
``/system`` health probe paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_LITESTAR_HOST = "0.0.0.0"
DEFAULT_LITESTAR_PORT = 8000
DEFAULT_GRACEFUL_TIMEOUT_SECONDS = 10.0

DEFAULT_LIVENESS_PATH = "/system/livez"
DEFAULT_READINESS_PATH = "/system/readyz"


@dataclass(slots=True)
class HealthConfig:
    """Configuration for the ``/system`` health probe routes."""

    enabled: bool = True
    liveness_path: str = DEFAULT_LIVENESS_PATH
    readiness_path: str = DEFAULT_READINESS_PATH


@dataclass(slots=True)
class LitestarConfig:
    """Self-contained configuration for a Litestar HTTP server entrypoint.

    Taken at construction, never read from global settings, so the AppSpec stays
    transport-neutral.
    """

    host: str = DEFAULT_LITESTAR_HOST
    port: int = DEFAULT_LITESTAR_PORT
    graceful_timeout: float = DEFAULT_GRACEFUL_TIMEOUT_SECONDS

    # Litestar app construction (forwarded to ``Litestar(**litestar_kwargs)``).
    litestar_kwargs: dict[str, Any] = field(default_factory=dict)

    # uvicorn server construction (forwarded to ``uvicorn.Config(**uvicorn_kwargs)``).
    uvicorn_kwargs: dict[str, Any] = field(default_factory=dict)

    # Components.
    health: HealthConfig = field(default_factory=HealthConfig)

    @property
    def address(self) -> str:
        """Return the ``host:port`` bind address."""
        return f"{self.host}:{self.port}"


__all__ = [
    "DEFAULT_GRACEFUL_TIMEOUT_SECONDS",
    "DEFAULT_LITESTAR_HOST",
    "DEFAULT_LITESTAR_PORT",
    "DEFAULT_LIVENESS_PATH",
    "DEFAULT_READINESS_PATH",
    "HealthConfig",
    "LitestarConfig",
]
