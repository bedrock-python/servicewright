"""Health probe report data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ProbeStatus(StrEnum):
    """Coarse status of a health probe."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"

    @classmethod
    def from_bool(cls, healthy: bool) -> ProbeStatus:
        """Map a boolean health flag to a probe status."""
        return cls.HEALTHY if healthy else cls.UNHEALTHY


@dataclass(slots=True, frozen=True)
class HealthReport:
    """Result of a liveness or readiness probe.

    Attributes:
        healthy: Aggregate health flag.
        checks: Per-named-check results (empty for liveness).
    """

    healthy: bool
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def status(self) -> ProbeStatus:
        """Coarse probe status derived from :attr:`healthy`."""
        return ProbeStatus.from_bool(self.healthy)
