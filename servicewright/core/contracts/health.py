"""Component health-check protocol."""

from __future__ import annotations

from typing import Protocol


class HealthCheckerProtocol(Protocol):
    """Protocol for component health checks (DB, Redis, etc.)."""

    async def check(self) -> bool:
        """Check component health. Returns True if healthy."""
        ...
