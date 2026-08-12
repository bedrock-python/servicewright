"""PostgreSQL readiness check via SQLAlchemy ``SELECT 1`` (extra-gated).

Folded from the FastAPI service-runtime prototype's database checker, adapted to
the servicewright :class:`~servicewright.core.contracts.HealthCheckerProtocol`
(``async check() -> bool``). Requires the ``postgres`` extra (SQLAlchemy); the
SDK is soft-imported with a friendly error pointing at ``servicewright[postgres]``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import contextlib
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

DEFAULT_DB_CHECK_TIMEOUT = 5.0


class _SessionProtocol(Protocol):
    """Duck-typed subset of a SQLAlchemy ``AsyncSession`` used by the check."""

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Awaitable[Any]:
        """Execute a statement and return an awaitable result."""
        ...


class SessionMakerProtocol(Protocol):
    """Duck-typed async session maker (``async_sessionmaker``-compatible).

    Any callable returning an async context manager that yields a session with
    an awaitable ``execute`` method works; no concrete SQLAlchemy type required.
    """

    def __call__(self) -> contextlib.AbstractAsyncContextManager[_SessionProtocol]:
        """Open a new async session as an async context manager."""
        ...


def _load_text() -> Any:
    """Soft-import SQLAlchemy's ``text`` with a friendly error."""
    try:
        from sqlalchemy import text
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError("PostgreSQL health check requires servicewright[postgres]; install it.") from exc
    return text


class PostgresHealthCheck:
    """Health check that runs ``SELECT 1`` against a SQLAlchemy async database.

    Satisfies :class:`~servicewright.core.contracts.HealthCheckerProtocol`, so it
    can be registered via ``HealthRegistry.add_check("postgres", check)``.

    Args:
        session_maker: Async session maker (e.g. ``async_sessionmaker``).
        timeout: Maximum seconds to wait for the probe query.

    Raises:
        ValueError: If ``timeout`` is not positive.
        ImportError: At construction time if the ``postgres`` extra is missing.
    """

    def __init__(self, session_maker: SessionMakerProtocol, timeout: float = DEFAULT_DB_CHECK_TIMEOUT) -> None:
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self._session_maker = session_maker
        self._timeout = timeout
        self._text = _load_text()

    async def check(self) -> bool:
        """Return ``True`` when ``SELECT 1`` succeeds within the timeout."""
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session_maker() as session:
                    await session.execute(self._text("SELECT 1"))
                    return True
        except TimeoutError:
            logger.warning("PostgreSQL health check timed out", extra={"timeout": self._timeout})
            return False
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            logger.exception("PostgreSQL health check failed")
            return False


__all__ = [
    "DEFAULT_DB_CHECK_TIMEOUT",
    "PostgresHealthCheck",
    "SessionMakerProtocol",
]
