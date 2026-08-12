"""Async Kafka warmer implementation."""

import asyncio
import logging
from typing import Any

from aiokafka.errors import KafkaError

from ...core.contracts.warmer import AsyncWarmer
from ...core.exceptions import KafkaProducerWarmupError

logger = logging.getLogger(__name__)

DEFAULT_WARMUP_TIMEOUT = 10.0


class KafkaProducerWarmer(AsyncWarmer):
    """Warmer for Kafka producer.

    Ensures the producer is ready to send messages by fetching metadata
    via the internal client.
    """

    def __init__(
        self,
        producer: Any,
        timeout: float = DEFAULT_WARMUP_TIMEOUT,
        priority: int = 0,
        raise_on_failure: bool = True,
    ) -> None:
        """Initialize Kafka producer warmer.

        Args:
            producer: Async Kafka producer instance.
            timeout: Maximum time to wait for the warmup operation in seconds.
            priority: Execution priority (lower value means higher priority).
            raise_on_failure: Whether warmup failure should fail startup.

        Raises:
            ValueError: If timeout is not positive.
        """
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")

        super().__init__(raise_on_failure=raise_on_failure)
        self._producer = producer
        self._timeout = timeout
        self._priority = priority

    @property
    def priority(self) -> int:
        """Get warmer priority."""
        return self._priority

    async def warmup(self) -> None:
        """Perform Kafka warmup.

        Raises:
            KafkaProducerWarmupError: If metadata fetch fails or times out.
        """
        logger.info("Warming up Kafka", extra={"timeout": self._timeout})

        try:
            async with asyncio.timeout(self._timeout):
                await self._perform_warmup()
        except TimeoutError as e:
            logger.warning("Kafka warmup timed out", extra={"timeout": self._timeout})
            raise KafkaProducerWarmupError(f"Kafka warmup timed out after {self._timeout}s") from e
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except KafkaError as e:
            logger.warning("Kafka warmup failed", extra={"error": str(e)})
            raise KafkaProducerWarmupError(f"Kafka warmup failed: {e}") from e
        except Exception as e:
            logger.exception("Unexpected error during Kafka warmup")
            raise KafkaProducerWarmupError(f"Kafka warmup failed due to unexpected error: {e}") from e

        logger.info("Kafka warmup successful")

    async def _perform_warmup(self) -> None:
        """Internal implementation of Kafka warmup."""
        await self._producer.client.fetch_all_metadata()
