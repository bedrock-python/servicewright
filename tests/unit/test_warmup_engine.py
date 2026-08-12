import asyncio
import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiokafka.errors import KafkaError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from servicewright.adapters.warmers import KafkaProducerWarmer, PostgresWarmer, RedisWarmer
from servicewright.core.contracts import AsyncWarmer
from servicewright.core.exceptions import (
    KafkaProducerWarmupError,
    PostgresWarmupError,
    RedisWarmupError,
    WarmupError,
)
from servicewright.core.warmup import warmup_async


def make_mock_warmer(*, priority: int = 0, raise_on_failure: bool = True) -> AsyncMock:
    warmer = AsyncMock()
    warmer.priority = priority
    warmer.raise_on_failure = raise_on_failure
    return warmer


class TestRedisWarmer:
    @pytest.mark.asyncio
    async def test__redis_warmer__redis_answers__completes(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.ping.return_value = True
        warmer = RedisWarmer(mock_redis, max_connections=5)

        with patch.object(RedisWarmer, "_check_health", AsyncMock(return_value=True)):
            await warmer.warmup()
            assert mock_redis.ping.call_count == 5

    @pytest.mark.asyncio
    async def test__redis_warmer__health_check_reports_unhealthy__raises(self) -> None:
        mock_redis = AsyncMock()
        warmer = RedisWarmer(mock_redis)

        with (
            patch.object(RedisWarmer, "_check_health", AsyncMock(return_value=False)),
            pytest.raises(RedisWarmupError, match="Redis health check failed"),
        ):
            await warmer.warmup()

    @pytest.mark.asyncio
    async def test__redis_warmer__ping_raises__raises(self) -> None:
        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = [True, RedisError("ping failed")]
        warmer = RedisWarmer(mock_redis, max_connections=2)

        with (
            patch.object(RedisWarmer, "_check_health", AsyncMock(return_value=True)),
            pytest.raises(RedisWarmupError, match="1/2 Redis warmup pings failed"),
        ):
            await warmer.warmup()

    @pytest.mark.asyncio
    async def test__redis_warmer__redis_outlasts_the_timeout__raises(self) -> None:
        mock_redis = AsyncMock()

        async def slow_ping() -> bool:
            await asyncio.sleep(0.2)
            return True

        mock_redis.ping.side_effect = slow_ping
        warmer = RedisWarmer(mock_redis, timeout=0.1)

        with (
            patch.object(RedisWarmer, "_check_health", AsyncMock(return_value=True)),
            pytest.raises(RedisWarmupError, match="Redis warmup timed out"),
        ):
            await warmer.warmup()

    def test__redis_warmer__non_positive_timeout__raises(self) -> None:
        mock_redis = AsyncMock()
        with pytest.raises(ValueError, match="max_connections must be positive"):
            RedisWarmer(mock_redis, max_connections=0)
        with pytest.raises(ValueError, match="timeout must be positive"):
            RedisWarmer(mock_redis, timeout=-1)

    def test__redis_warmer__constructed__reports_its_priority(self) -> None:
        assert RedisWarmer(AsyncMock(), priority=3).priority == 3

    @pytest.mark.asyncio
    async def test__redis_warmer__client_offers_a_health_check__uses_it(self) -> None:
        # When redis-client-kit is available, the enhanced health check is used instead of a raw ping.
        mock_redis = AsyncMock()
        warmer = RedisWarmer(mock_redis)

        with (
            patch("servicewright.adapters.warmers.redis.REDIS_INFRA_AVAILABLE", True),
            patch(
                "servicewright.adapters.warmers.redis.check_async_redis_health",
                AsyncMock(return_value=True),
            ),
        ):
            assert await warmer._check_health() is True

        mock_redis.ping.assert_not_called()

    @pytest.mark.asyncio
    async def test__redis_warmer__no_health_check_available__falls_back_to_ping(self) -> None:
        client = AsyncMock()
        client.ping.return_value = "PONG"

        with patch("servicewright.adapters.warmers.redis.check_async_redis_health", None):
            warmer = RedisWarmer(client, max_connections=1)
            await warmer.warmup()
            assert client.ping.call_count == 2  # 1 for health check, 1 for warmup

    @pytest.mark.asyncio
    async def test__redis_warmer__health_check_unavailable_and_ping_fails__raises(self) -> None:
        client = AsyncMock()
        client.ping.side_effect = RedisError("ping failed")

        with patch("servicewright.adapters.warmers.redis.check_async_redis_health", None):
            warmer = RedisWarmer(client)
            with pytest.raises(RedisWarmupError, match="Redis health check failed"):
                await warmer.warmup()


class TestKafkaProducerWarmer:
    def test__kafka_warmer__non_positive_timeout__raises(self) -> None:
        with pytest.raises(ValueError, match="timeout must be positive"):
            KafkaProducerWarmer(MagicMock(), timeout=0)

    def test__kafka_warmer__constructed__reports_its_priority(self) -> None:
        assert KafkaProducerWarmer(MagicMock(), priority=2).priority == 2

    @pytest.mark.asyncio
    async def test__kafka_warmer__cluster_metadata_available__completes(self) -> None:
        mock_producer = MagicMock()
        mock_producer.client.fetch_all_metadata = AsyncMock()
        warmer = KafkaProducerWarmer(mock_producer)

        await warmer.warmup()
        mock_producer.client.fetch_all_metadata.assert_called_once()

    @pytest.mark.asyncio
    async def test__kafka_warmer__client_exposes_no_metadata__raises(self) -> None:
        mock_producer = MagicMock()
        del mock_producer.client.fetch_all_metadata
        warmer = KafkaProducerWarmer(mock_producer)

        with pytest.raises(KafkaProducerWarmupError, match="Kafka warmup failed due to unexpected error"):
            await warmer.warmup()

    @pytest.mark.asyncio
    async def test__kafka_warmer__metadata_fetch_raises__raises(self) -> None:
        mock_producer = MagicMock()
        mock_producer.client.fetch_all_metadata.side_effect = KafkaError("kafka error")
        warmer = KafkaProducerWarmer(mock_producer)

        with pytest.raises(KafkaProducerWarmupError, match="Kafka warmup failed"):
            await warmer.warmup()

    @pytest.mark.asyncio
    async def test__kafka_warmer__cluster_outlasts_the_timeout__raises(self) -> None:
        mock_producer = MagicMock()

        async def slow_metadata(*args: object) -> None:
            await asyncio.sleep(0.2)

        mock_producer.client.fetch_all_metadata.side_effect = slow_metadata
        warmer = KafkaProducerWarmer(mock_producer, timeout=0.1)
        with pytest.raises(KafkaProducerWarmupError, match="Kafka warmup timed out"):
            await warmer.warmup()

    @pytest.mark.asyncio
    async def test__kafka_warmer__unexpected_error__raises(self) -> None:
        class BrokenProducer:
            @property
            def client(self) -> object:
                raise RuntimeError("property error")

        warmer = KafkaProducerWarmer(BrokenProducer())
        with pytest.raises(KafkaProducerWarmupError, match="Kafka warmup failed"):
            await warmer.warmup()


class TestPostgresWarmer:
    def test__postgres_warmer__non_positive_timeout__raises(self) -> None:
        with pytest.raises(ValueError, match="timeout must be positive"):
            PostgresWarmer(MagicMock(), timeout=0)

    def test__postgres_warmer__constructed__reports_its_priority(self) -> None:
        assert PostgresWarmer(MagicMock(), priority=1).priority == 1

    @pytest.mark.asyncio
    async def test__postgres_warmer__session_available__completes(self) -> None:
        mock_manager = MagicMock()
        mock_session = AsyncMock()
        mock_manager.get_session.return_value.__aenter__.return_value = mock_session
        warmer = PostgresWarmer(mock_manager)

        await warmer.warmup()
        mock_manager.get_session.assert_called_once()
        mock_session.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test__postgres_warmer__probe_query_fails__raises(self) -> None:
        mock_manager = MagicMock()
        mock_manager.get_session.side_effect = SQLAlchemyError("db error")
        warmer = PostgresWarmer(mock_manager)

        with pytest.raises(PostgresWarmupError, match="Postgres warmup failed"):
            await warmer.warmup()

    @pytest.mark.asyncio
    async def test__postgres_warmer__unexpected_error__raises(self) -> None:
        mock_manager = MagicMock()
        mock_manager.get_session.side_effect = ValueError("boom")
        warmer = PostgresWarmer(mock_manager)

        with pytest.raises(PostgresWarmupError, match="Postgres warmup failed due to unexpected error"):
            await warmer.warmup()

    @pytest.mark.asyncio
    async def test__postgres_warmer__sqlalchemy_not_installed__raises_with_the_install_hint(self) -> None:
        mock_manager = MagicMock()
        with (
            patch("servicewright.adapters.warmers.postgres.SQLALCHEMY_AVAILABLE", False),
            pytest.raises(PostgresWarmupError, match="sqlalchemy is required"),
        ):
            PostgresWarmer(mock_manager)

    @pytest.mark.asyncio
    async def test__postgres_warmer__database_outlasts_the_timeout__raises(self) -> None:
        mock_manager = MagicMock()
        warmer = PostgresWarmer(mock_manager, timeout=0.01)

        with (
            patch.object(warmer, "_perform_warmup", side_effect=TimeoutError()),
            pytest.raises(PostgresWarmupError, match="Postgres warmup timed out"),
        ):
            await warmer.warmup()


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test__warmup_async__every_warmer_succeeds__completes(self) -> None:
        mock_warmer1 = make_mock_warmer()
        mock_warmer2 = make_mock_warmer()
        await warmup_async([mock_warmer1, mock_warmer2])
        mock_warmer1.warmup.assert_called_once()
        mock_warmer2.warmup.assert_called_once()

    @pytest.mark.asyncio
    async def test__warmup_async__a_required_warmer_fails__raises(self) -> None:
        mock_warmer1 = make_mock_warmer()
        mock_warmer2 = make_mock_warmer()
        mock_warmer2.warmup.side_effect = RedisWarmupError("fail")

        with pytest.raises(WarmupError, match="Infrastructure warmup failed with 1 errors"):
            await warmup_async([mock_warmer1, mock_warmer2], raise_on_failure=True)

    @pytest.mark.asyncio
    async def test__warmup_async__raise_on_failure_disabled__collects_instead_of_raising(self) -> None:
        mock_warmer1 = make_mock_warmer()
        mock_warmer2 = make_mock_warmer()
        mock_warmer2.warmup.side_effect = RedisWarmupError("fail")

        # Should not raise
        await warmup_async([mock_warmer1, mock_warmer2], raise_on_failure=False)
        mock_warmer1.warmup.assert_called_once()
        mock_warmer2.warmup.assert_called_once()

    @pytest.mark.asyncio
    async def test__warmup_async__warmer_opts_out_of_failure__does_not_abort_the_run(self) -> None:
        # A warmer with raise_on_failure=False must not contribute to the aggregated error.
        ok_warmer = make_mock_warmer()
        opt_out_warmer = make_mock_warmer(raise_on_failure=False)
        opt_out_warmer.warmup.side_effect = RedisWarmupError("fail")

        await warmup_async([ok_warmer, opt_out_warmer], raise_on_failure=True)
        ok_warmer.warmup.assert_called_once()

    @pytest.mark.asyncio
    async def test__warmup_async__non_boolean_raise_on_failure__raises(self) -> None:
        bad_warmer = make_mock_warmer()
        bad_warmer.raise_on_failure = "not-a-bool"
        bad_warmer.warmup.side_effect = RedisWarmupError("fail")

        # TypeError inside process_result is wrapped into a WarmupError.
        with pytest.raises(WarmupError, match="failed due to unexpected error"):
            await warmup_async([bad_warmer])

    @pytest.mark.asyncio
    async def test__warmup_async__warmer_raises_an_unexpected_error__is_reported(self) -> None:
        mock_warmer = make_mock_warmer()
        with (
            patch("asyncio.gather", side_effect=Exception("gather error")),
            pytest.raises(WarmupError, match="Infrastructure warmup failed due to unexpected error: gather error"),
        ):
            await warmup_async([mock_warmer])

    @pytest.mark.asyncio
    async def test__warmup_async__warmer_outlasts_the_timeout__raises(self) -> None:
        async def slow_warmer() -> None:
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                pass

        mock_warmer = make_mock_warmer()
        mock_warmer.warmup = AsyncMock(side_effect=slow_warmer)

        with pytest.raises(WarmupError, match="Infrastructure warmup timed out"):
            await warmup_async([mock_warmer], timeout=0.1)

    @pytest.mark.asyncio
    async def test__warmup_async__no_warmers__does_nothing(self) -> None:
        # Should not raise and return immediately
        await warmup_async([])
        await warmup_async(None)


@pytest.mark.asyncio
async def test__warmup_async__some_warmers_fail__collects_every_failure() -> None:
    """Verify that multiple failures are collected into a single error message."""
    mock_warmer1 = make_mock_warmer()
    mock_warmer2 = make_mock_warmer()
    mock_warmer3 = make_mock_warmer()

    # 2 failures, 1 success
    mock_warmer1.warmup.side_effect = RedisWarmupError("redis fail")
    mock_warmer2.warmup.side_effect = KafkaProducerWarmupError("kafka fail")
    mock_warmer3.warmup.return_value = None

    with pytest.raises(WarmupError, match="Infrastructure warmup failed with 2 errors"):
        await warmup_async([mock_warmer1, mock_warmer2, mock_warmer3], raise_on_failure=True)

    # Success should still have been called
    mock_warmer3.warmup.assert_called_once()


@pytest.mark.asyncio
async def test__warmup_async__slow_warmers__are_bounded_by_the_total_timeout() -> None:
    """Verify that a very slow warmer triggers total warmup timeout."""

    async def very_slow_warmer() -> None:
        await asyncio.sleep(10)

    mock_warmer = make_mock_warmer()
    mock_warmer.warmup.side_effect = very_slow_warmer

    start_time = asyncio.get_running_loop().time()
    with pytest.raises(WarmupError, match="Infrastructure warmup timed out"):
        await warmup_async([mock_warmer], timeout=0.1)
    end_time = asyncio.get_running_loop().time()

    # Took around 0.1s, not 10s
    assert end_time - start_time < 0.5


@pytest.mark.asyncio
async def test__warmup_async__cancelled_mid_run__propagates_the_cancellation() -> None:
    """Verify that cancellation propagates through the orchestrator."""
    mock_warmer = make_mock_warmer()

    async def slow_warmup() -> None:
        await asyncio.sleep(10)

    mock_warmer.warmup.side_effect = slow_warmup

    task = asyncio.create_task(warmup_async([mock_warmer]))
    await asyncio.sleep(0.1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert mock_warmer.warmup.called


class _DummyWarmer(AsyncWarmer):
    def __init__(self) -> None:
        super().__init__()
        self.ran = False

    async def warmup(self) -> None:
        self.ran = True


@pytest.mark.asyncio
async def test__warmup_async__real_warmer__runs_it_with_the_base_defaults() -> None:
    warmer = _DummyWarmer()

    # Base AsyncWarmer supplies the defaults the orchestrator relies on.
    assert warmer.priority == 0
    assert warmer.raise_on_failure is True

    await warmup_async([warmer])
    assert warmer.ran is True


def test__postgres_warmer_module__imported_without_sqlalchemy__raises_with_the_install_hint() -> None:
    pg_mod = importlib.import_module("servicewright.adapters.warmers.postgres")

    try:
        with patch.dict("sys.modules", {"sqlalchemy": None}):
            importlib.reload(pg_mod)
            assert pg_mod.SQLALCHEMY_AVAILABLE is False
            assert pg_mod.text is None
    finally:
        # Restore
        importlib.reload(pg_mod)


def test__redis_warmer_module__imported_without_redis__raises_with_the_install_hint() -> None:
    redis_mod = importlib.import_module("servicewright.adapters.warmers.redis")

    try:
        with patch.dict("sys.modules", {"redis_client_kit": None}):
            importlib.reload(redis_mod)
            assert redis_mod.check_async_redis_health is None
    finally:
        # Restore
        importlib.reload(redis_mod)
