"""Async Redis warmer implementation."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ...core.contracts.warmer import AsyncWarmer
from ...core.exceptions import RedisWarmupError

if TYPE_CHECKING:
    pass

# Optional dependencies for Redis warmer.
# To enable enhanced health checks, install redis-client-kit.
try:
    from redis_client_kit import check_async_redis_health as _check_async_redis_health

    REDIS_INFRA_AVAILABLE = True
    check_async_redis_health: Any = _check_async_redis_health
except ImportError:
    REDIS_INFRA_AVAILABLE = False
    check_async_redis_health = None

# Optional dependency: redis-py
try:
    from redis.exceptions import RedisError as _RedisError

    RedisError: Any = _RedisError
except ImportError:
    RedisError = Exception

logger = logging.getLogger(__name__)

DEFAULT_WARMUP_POOL_SIZE = 10
DEFAULT_WARMUP_TIMEOUT = 10.0


@runtime_checkable
class RedisClientProtocol(Protocol):
    """Protocol for async Redis client."""

    async def ping(self) -> object:
        """Ping Redis server."""
        ...

    async def aclose(self) -> None:
        """Close the Redis client."""
        ...


class RedisWarmer(AsyncWarmer):
    """Warmer for Redis connection pool.

    Performs concurrent pings to fill the connection pool and verifies
    connectivity using an optional health check from `redis-client-kit`.
    """

    def __init__(
        self,
        redis_client: RedisClientProtocol,
        max_connections: int | None = None,
        timeout: float = DEFAULT_WARMUP_TIMEOUT,
        priority: int = 0,
        raise_on_failure: bool = True,
    ) -> None:
        """Initialize Redis warmer.

        Args:
            redis_client: Async Redis client instance.
            max_connections: Number of concurrent pings to fill the pool.
                Defaults to DEFAULT_WARMUP_POOL_SIZE if None.
            timeout: Maximum time to wait for the warmup operation in seconds.
            priority: Execution priority (lower value means higher priority).
            raise_on_failure: Whether warmup failure should fail startup.

        Raises:
            ValueError: If max_connections (if provided) or timeout is not positive.
        """
        if max_connections is not None and max_connections <= 0:
            raise ValueError(f"max_connections must be positive, got {max_connections}")
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")

        super().__init__(raise_on_failure=raise_on_failure)
        self._redis_client = redis_client
        self._max_connections = max_connections or DEFAULT_WARMUP_POOL_SIZE
        self._timeout = timeout
        self._priority = priority

    @property
    def priority(self) -> int:
        """Get warmer priority."""
        return self._priority

    async def warmup(self) -> None:
        """Perform Redis warmup.

        Raises:
            RedisWarmupError: If health check fails or pings timeout.
        """
        logger.info("Warming up Redis", extra={"timeout": self._timeout, "max_connections": self._max_connections})

        try:
            async with asyncio.timeout(self._timeout):
                await self._perform_warmup()
        except TimeoutError as e:
            logger.warning("Redis warmup timed out", extra={"timeout": self._timeout})
            raise RedisWarmupError(f"Redis warmup timed out after {self._timeout}s") from e

        logger.info("Redis warmup successful")

    async def _perform_warmup(self) -> None:
        """Internal implementation of Redis warmup."""
        # 1. Initial health check
        is_healthy = await self._check_health()
        if not is_healthy:
            raise RedisWarmupError("Redis health check failed during warmup")

        # 2. Pool warmup (concurrent pings)
        results = await asyncio.gather(
            *(self._redis_client.ping() for _ in range(self._max_connections)),
            return_exceptions=True,
        )

        def is_ping_failed(r: object) -> bool:
            if isinstance(r, Exception):
                return True
            if isinstance(r, dict):
                # For RedisCluster, ping() returns a dict of results from nodes
                return not r or not all(bool(v) for v in r.values())
            return not bool(r)

        failed_count = sum(1 for r in results if is_ping_failed(r))
        if failed_count > 0:
            logger.warning(
                "Some Redis warmup pings failed",
                extra={"total": self._max_connections, "count": failed_count},
            )
            # We treat any failure as a reason to raise since we want "perfect code"
            raise RedisWarmupError(f"{failed_count}/{self._max_connections} Redis warmup pings failed")

    async def _check_health(self) -> bool:
        """Check if Redis is healthy using available tools."""
        if REDIS_INFRA_AVAILABLE and check_async_redis_health is not None:
            # RedisClientProtocol is structurally compatible with Redis/RedisCluster
            # from redis-client-kit
            result = await check_async_redis_health(self._redis_client)
            return bool(result)

        try:
            # Fallback to simple ping if redis-client-kit is not available
            ping_result = await self._redis_client.ping()
            return bool(ping_result)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except RedisError as e:
            logger.warning("Redis ping failed during health check", extra={"error": str(e)})
            return False
