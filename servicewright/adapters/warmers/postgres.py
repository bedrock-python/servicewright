"""Async Postgres warmer implementation."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ...core.contracts.warmer import AsyncWarmer
from ...core.exceptions import PostgresWarmupError

if TYPE_CHECKING:
    pass

try:
    from sqlalchemy import text as _text
    from sqlalchemy.exc import SQLAlchemyError as _SQLAlchemyError

    SQLALCHEMY_AVAILABLE = True
    text: Any = _text
    SQLAlchemyError: Any = _SQLAlchemyError
except ImportError:
    SQLALCHEMY_AVAILABLE = False
    text = None
    SQLAlchemyError = Exception

logger = logging.getLogger(__name__)

DEFAULT_WARMUP_TIMEOUT = 10.0
DEFAULT_WARMUP_QUERY = "SELECT 1"


class PostgresWarmer(AsyncWarmer):
    """Warmer for Postgres connection pool.

    Executes a simple query (default SELECT 1) to ensure connections are
    established and the database is responsive.
    """

    def __init__(
        self,
        session_manager: Any,
        query: str = DEFAULT_WARMUP_QUERY,
        timeout: float = DEFAULT_WARMUP_TIMEOUT,
        priority: int = 0,
        raise_on_failure: bool = True,
    ) -> None:
        """Initialize Postgres warmer.

        Args:
            session_manager: Async Postgres manager instance.
            query: SQL query to execute for warmup.
            timeout: Maximum time to wait for the warmup operation in seconds.
            priority: Execution priority (lower value means higher priority).
            raise_on_failure: Whether warmup failure should fail startup.

        Raises:
            ValueError: If timeout is not positive.
            PostgresWarmupError: If sqlalchemy is not installed.
        """
        if not SQLALCHEMY_AVAILABLE:
            raise PostgresWarmupError("sqlalchemy is required for Postgres warmup")

        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")

        super().__init__(raise_on_failure=raise_on_failure)
        self._session_manager = session_manager
        self._query = query
        self._timeout = timeout
        self._priority = priority

    @property
    def priority(self) -> int:
        """Get warmer priority."""
        return self._priority

    async def warmup(self) -> None:
        """Perform Postgres warmup.

        Raises:
            PostgresWarmupError: If warmup fails or times out.
        """
        logger.info("Warming up Postgres", extra={"query": self._query, "timeout": self._timeout})

        try:
            async with asyncio.timeout(self._timeout):
                await self._perform_warmup()
        except TimeoutError as e:
            logger.warning("Postgres warmup timed out", extra={"timeout": self._timeout})
            raise PostgresWarmupError(f"Postgres warmup timed out after {self._timeout}s") from e
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except SQLAlchemyError as e:
            logger.warning("Postgres warmup failed", extra={"error": str(e)})
            raise PostgresWarmupError(f"Postgres warmup failed: {e}") from e
        except Exception as e:
            if isinstance(e, PostgresWarmupError):
                raise
            logger.exception("Unexpected error during Postgres warmup", extra={"error": str(e)})
            raise PostgresWarmupError(f"Postgres warmup failed due to unexpected error: {e}") from e

        logger.info("Postgres warmup successful")

    async def _perform_warmup(self) -> None:
        """Internal implementation of Postgres warmup."""
        if text is None:
            raise PostgresWarmupError("sqlalchemy is required for Postgres warmup")

        async with self._session_manager.get_session() as session:
            await session.execute(text(self._query))
