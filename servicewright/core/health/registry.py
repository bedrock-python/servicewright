"""Transport-agnostic health registry for liveness/readiness probes."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from .report import HealthReport

if TYPE_CHECKING:
    from ..contracts import HealthCheckerProtocol

logger = logging.getLogger(__name__)


class HealthRegistry:
    """Holds readiness state and named component health checks.

    Liveness reflects only that the process is alive and is independent of the
    registered checks. Readiness requires both the ``ready`` flag (flipped on
    by the Host once serving) and every registered check passing.
    """

    def __init__(self, *, readiness_cache_ttl: float = 0.0) -> None:
        """Initialize the registry.

        Args:
            readiness_cache_ttl: When > 0, readiness results are cached for this
                many seconds to avoid hammering downstream checks.
        """
        self.ready: bool = False
        self._checks: dict[str, HealthCheckerProtocol] = {}
        self._readiness_cache_ttl = readiness_cache_ttl
        self._cached_report: HealthReport | None = None
        self._cached_at: float = 0.0

    def add_check(self, name: str, check: HealthCheckerProtocol) -> None:
        """Register a named component health check.

        Args:
            name: Unique label for the check (e.g. ``"postgres"``).
            check: Object implementing :class:`HealthCheckerProtocol`.
        """
        if name in self._checks:
            raise ValueError(f"Health check '{name}' is already registered")
        self._checks[name] = check
        self._invalidate_cache()

    @property
    def checks(self) -> dict[str, HealthCheckerProtocol]:
        """Return a copy of the registered checks mapping."""
        return dict(self._checks)

    def _invalidate_cache(self) -> None:
        self._cached_report = None
        self._cached_at = 0.0

    async def liveness(self) -> HealthReport:
        """Report process liveness (always healthy while the loop runs)."""
        return HealthReport(healthy=True, checks={})

    async def readiness(self) -> HealthReport:
        """Report readiness: ``ready`` flag AND all checks passing.

        Checks run concurrently; any check raising is treated as a failure.
        """
        if (
            self._readiness_cache_ttl > 0
            and self._cached_report is not None
            and (time.monotonic() - self._cached_at) < self._readiness_cache_ttl
        ):
            return self._cached_report

        results = await self._run_checks()
        healthy = self.ready and all(results.values())
        report = HealthReport(healthy=healthy, checks=results)

        if self._readiness_cache_ttl > 0:
            self._cached_report = report
            self._cached_at = time.monotonic()

        return report

    async def _run_checks(self) -> dict[str, bool]:
        if not self._checks:
            return {}

        names = list(self._checks)
        outcomes = await asyncio.gather(
            *(self._safe_check(name) for name in names),
            return_exceptions=False,
        )
        return dict(zip(names, outcomes, strict=True))

    async def _safe_check(self, name: str) -> bool:
        check = self._checks[name]
        try:
            return bool(await check.check())
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.exception("Health check failed", extra={"check": name})
            return False
