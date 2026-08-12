"""Unit tests for the infrastructure health checks (servicewright.adapters.health).

Both :class:`PostgresHealthCheck` and :class:`RedisHealthCheck` satisfy
:class:`HealthCheckerProtocol` (``async check() -> bool``) and integrate with
:class:`HealthRegistry`. SQLAlchemy sessions and the redis client are faked, so
no real database/cache is required.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from servicewright.adapters.health.postgres import PostgresHealthCheck
from servicewright.adapters.health.redis import RedisHealthCheck
from servicewright.core.health import HealthRegistry

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Postgres test doubles
# --------------------------------------------------------------------------- #
class _FakeSession:
    def __init__(self, *, on_execute: Any = None) -> None:
        self._on_execute = on_execute
        self.executed: list[str] = []

    async def execute(self, statement: Any) -> Any:
        self.executed.append(str(statement))
        if self._on_execute is not None:
            return await self._on_execute()
        return object()


class _FakeSessionMaker:
    """Mimics ``async_sessionmaker``: callable yielding an async-cm session."""

    def __init__(self, *, on_execute: Any = None) -> None:
        self._on_execute = on_execute
        self.session = _FakeSession(on_execute=on_execute)
        self.calls = 0

    def __call__(self) -> contextlib.AbstractAsyncContextManager[_FakeSession]:
        self.calls += 1

        @contextlib.asynccontextmanager
        async def _cm() -> Any:
            yield self.session

        return _cm()


# --------------------------------------------------------------------------- #
# PostgresHealthCheck
# --------------------------------------------------------------------------- #
async def test__postgres_health_check__query_succeeds__reports_healthy() -> None:
    maker = _FakeSessionMaker()
    check = PostgresHealthCheck(maker)
    assert await check.check() is True
    assert maker.calls == 1
    assert maker.session.executed == ["SELECT 1"]


async def test__postgres_health_check__query_raises__reports_unhealthy() -> None:
    async def boom() -> Any:
        raise ConnectionError("db down")

    check = PostgresHealthCheck(_FakeSessionMaker(on_execute=boom))
    assert await check.check() is False


async def test__postgres_health_check__query_outlasts_the_timeout__reports_unhealthy() -> None:
    async def slow() -> Any:
        await asyncio.sleep(10)

    check = PostgresHealthCheck(_FakeSessionMaker(on_execute=slow), timeout=0.01)
    assert await check.check() is False


async def test__postgres_health_check__query_times_out__logs_it(caplog: pytest.LogCaptureFixture) -> None:
    async def slow() -> Any:
        await asyncio.sleep(10)

    check = PostgresHealthCheck(_FakeSessionMaker(on_execute=slow), timeout=0.01)
    with caplog.at_level(logging.WARNING):
        assert await check.check() is False
    assert any("timed out" in r.message for r in caplog.records)


async def test__postgres_health_check__unexpected_error__logs_it(caplog: pytest.LogCaptureFixture) -> None:
    async def boom() -> Any:
        raise RuntimeError("unexpected")

    check = PostgresHealthCheck(_FakeSessionMaker(on_execute=boom))
    with caplog.at_level(logging.ERROR):
        assert await check.check() is False
    assert any("PostgreSQL health check failed" in r.message for r in caplog.records)


def test__postgres_health_check__non_positive_timeout__raises() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        PostgresHealthCheck(_FakeSessionMaker(), timeout=0)


async def test__postgres_health_check__cancelled__propagates_the_cancellation() -> None:
    async def cancel() -> Any:
        raise asyncio.CancelledError

    check = PostgresHealthCheck(_FakeSessionMaker(on_execute=cancel))
    with pytest.raises(asyncio.CancelledError):
        await check.check()


# --------------------------------------------------------------------------- #
# Redis test doubles
# --------------------------------------------------------------------------- #
class _FakeRedis:
    def __init__(self, *, result: Any = True, raises: BaseException | None = None, slow: bool = False) -> None:
        self._result = result
        self._raises = raises
        self._slow = slow
        self.pings = 0

    async def ping(self) -> Any:
        self.pings += 1
        if self._slow:
            await asyncio.sleep(10)
        if self._raises is not None:
            raise self._raises
        return self._result


# --------------------------------------------------------------------------- #
# RedisHealthCheck
# --------------------------------------------------------------------------- #
async def test__redis_health_check__ping_answers__reports_healthy() -> None:
    client = _FakeRedis(result=True)
    check = RedisHealthCheck(client)
    assert await check.check() is True
    assert client.pings == 1


async def test__redis_health_check__ping_returns_falsy__reports_unhealthy() -> None:
    check = RedisHealthCheck(_FakeRedis(result=False))
    assert await check.check() is False


async def test__redis_health_check__ping_raises__reports_unhealthy() -> None:
    check = RedisHealthCheck(_FakeRedis(raises=ConnectionError("redis down")))
    assert await check.check() is False


async def test__redis_health_check__ping_outlasts_the_timeout__reports_unhealthy() -> None:
    check = RedisHealthCheck(_FakeRedis(slow=True), timeout=0.01)
    assert await check.check() is False


async def test__redis_health_check__ping_times_out__logs_it(caplog: pytest.LogCaptureFixture) -> None:
    check = RedisHealthCheck(_FakeRedis(slow=True), timeout=0.01)
    with caplog.at_level(logging.WARNING):
        assert await check.check() is False
    assert any("timed out" in r.message for r in caplog.records)


async def test__redis_health_check__unexpected_error__logs_it(caplog: pytest.LogCaptureFixture) -> None:
    check = RedisHealthCheck(_FakeRedis(raises=RuntimeError("boom")))
    with caplog.at_level(logging.ERROR):
        assert await check.check() is False
    assert any("Redis health check failed" in r.message for r in caplog.records)


def test__redis_health_check__non_positive_timeout__raises() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        RedisHealthCheck(_FakeRedis(), timeout=-1)


async def test__redis_health_check__cancelled__propagates_the_cancellation() -> None:
    check = RedisHealthCheck(_FakeRedis(raises=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await check.check()


# --------------------------------------------------------------------------- #
# HealthRegistry integration (readiness reflects the checks)
# --------------------------------------------------------------------------- #
async def test__health_registry__infra_checks_pass__reports_ready() -> None:
    registry = HealthRegistry()
    registry.ready = True
    registry.add_check("postgres", PostgresHealthCheck(_FakeSessionMaker()))
    registry.add_check("redis", RedisHealthCheck(_FakeRedis(result=True)))

    report = await registry.readiness()
    assert report.healthy is True
    assert report.checks == {"postgres": True, "redis": True}


async def test__health_registry__an_infra_check_fails__reports_not_ready() -> None:
    registry = HealthRegistry()
    registry.ready = True
    registry.add_check("postgres", PostgresHealthCheck(_FakeSessionMaker()))
    registry.add_check("redis", RedisHealthCheck(_FakeRedis(result=False)))

    report = await registry.readiness()
    assert report.healthy is False
    assert report.checks["postgres"] is True
    assert report.checks["redis"] is False


async def test__health_package__imported_without_the_extras__does_not_raise() -> None:
    # Importing servicewright.core.health must never require the postgres/redis extras.
    import servicewright.adapters.health
    import servicewright.core.health

    assert hasattr(servicewright.core.health, "HealthRegistry")
    assert servicewright.adapters.health.__all__ == []
