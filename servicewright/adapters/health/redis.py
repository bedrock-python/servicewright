"""Redis readiness check via ``PING`` (extra-gated).

Folded from the FastAPI service-runtime prototype's Redis checker, adapted to the
servicewright :class:`~servicewright.core.contracts.HealthCheckerProtocol`
(``async check() -> bool``). Requires the ``redis`` extra; the SDK is
soft-imported with a friendly error pointing at ``servicewright[redis]``.

The client is duck-typed: only an awaitable ``ping()`` is required, so any
redis-py async client (single instance or cluster) and structurally-compatible
wrappers work.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

DEFAULT_REDIS_CHECK_TIMEOUT = 5.0


class RedisClientProtocol(Protocol):
    """Duck-typed async Redis client; only an awaitable ``ping`` is needed."""

    def ping(self) -> Awaitable[object]:
        """Ping the server; the awaited result is treated as truthy on success."""
        ...


def _ensure_redis_installed() -> None:
    """Soft-import redis to raise a friendly error when the extra is missing."""
    try:
        import redis.asyncio  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError("Redis health check requires servicewright[redis]; install it.") from exc


class RedisHealthCheck:
    """Health check that pings a Redis server within a timeout.

    Satisfies :class:`~servicewright.core.contracts.HealthCheckerProtocol`, so it
    can be registered via ``HealthRegistry.add_check("redis", check)``.

    Args:
        client: Async Redis client exposing an awaitable ``ping``.
        timeout: Maximum seconds to wait for the ``PING`` response.

    Raises:
        ValueError: If ``timeout`` is not positive.
        ImportError: At construction time if the ``redis`` extra is missing.
    """

    def __init__(self, client: RedisClientProtocol, timeout: float = DEFAULT_REDIS_CHECK_TIMEOUT) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        _ensure_redis_installed()
        self._client = client
        self._timeout = timeout

    async def check(self) -> bool:
        """Return ``True`` when ``PING`` returns a truthy result within timeout."""
        try:
            async with asyncio.timeout(self._timeout):
                result = await self._client.ping()
                return bool(result)
        except TimeoutError:
            logger.warning("Redis health check timed out", extra={"timeout": self._timeout})
            return False
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.exception("Redis health check failed")
            return False


__all__ = [
    "DEFAULT_REDIS_CHECK_TIMEOUT",
    "RedisClientProtocol",
    "RedisHealthCheck",
]
