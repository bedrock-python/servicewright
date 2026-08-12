"""Infrastructure warmup orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from ..constants import DEFAULT_WARMUP_TIMEOUT_SECONDS
from ..exceptions import WarmupTimeoutError
from .engine import warmup_async

if TYPE_CHECKING:
    from ..contracts import AsyncWarmer
    from ..spec import ServiceContext

logger = logging.getLogger(__name__)


async def _resolve_warmers(
    source: Sequence[AsyncWarmer] | Awaitable[Sequence[AsyncWarmer]],
) -> Sequence[AsyncWarmer]:
    """Resolve warmers from potentially awaitable source."""
    if asyncio.iscoroutine(source) or isinstance(source, Awaitable):
        return await source
    return source


async def collect_warmers(
    base_warmers: Sequence[AsyncWarmer] | None,
    warmers_factory: Callable[[ServiceContext[Any, Any]], Sequence[AsyncWarmer] | Awaitable[Sequence[AsyncWarmer]]]
    | None,
    app_ctx: ServiceContext[Any, Any],
) -> list[AsyncWarmer]:
    """Collect warmers from base list and factory."""
    warmers: list[AsyncWarmer] = list(base_warmers) if base_warmers else []

    if warmers_factory:
        res = await _resolve_warmers(warmers_factory(app_ctx))
        warmers.extend(res)

    return warmers


async def perform_warmup(
    service_name: str,
    warmers: list[AsyncWarmer],
    timeout: float = DEFAULT_WARMUP_TIMEOUT_SECONDS,
) -> None:
    """Execute warmup with fail-fast behavior."""
    if not warmers:
        return

    if timeout <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")

    try:
        async with asyncio.timeout(timeout):
            await warmup_async(warmers=warmers, raise_on_failure=True)
        logger.info(
            "Infrastructure warmup completed",
            extra={"service": service_name, "warmers_count": len(warmers)},
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except TimeoutError as exc:
        logger.exception(
            "Application warmup timed out",
            extra={
                "service": service_name,
                "timeout": timeout,
                "warmers_count": len(warmers),
            },
        )
        raise WarmupTimeoutError(
            f"Infrastructure warmup for service '{service_name}' did not finish within {timeout} seconds"
        ) from exc
    except Exception as exc:
        logger.exception(
            "Application warmup failed",
            extra={
                "service": service_name,
                "error_type": type(exc).__name__,
                "warmers_count": len(warmers),
            },
        )
        raise
