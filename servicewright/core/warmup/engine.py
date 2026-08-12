"""Infrastructure warmup orchestrator (async)."""

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Sequence
from typing import Any

from ..contracts.warmer import AsyncWarmer
from ..exceptions import WarmupError

logger = logging.getLogger(__name__)


async def warmup_async(
    warmers: Sequence[AsyncWarmer] | None = None,
    raise_on_failure: bool = True,
    timeout: float | None = None,
) -> None:
    """Perform asynchronous warmup of provided infrastructure warmers.

    Executes warmers in dependency order based on their priority.
    Warmers with the same priority are executed in parallel.
    Failures in one warmer do not cancel other warmers in the same priority group.

    Args:
        warmers: A sequence of warmer instances to execute.
        raise_on_failure: If True, raises WarmupError if any warmer fails.
            If False, only logs warnings.
        timeout: Maximum time to wait for all warmers to complete.

    Raises:
        WarmupError: If any warmer fails and raise_on_failure is True.
    """
    if not warmers:
        logger.debug("No infrastructure warmers provided")
        return

    logger.info(
        "Starting infrastructure warmup",
        extra={
            "count": len(warmers),
            "timeout": timeout,
            "warmers": [type(w).__name__ for w in warmers],
        },
    )

    # Sort and group warmers by priority, using index as tie-breaker to avoid comparing warmers themselves
    indexed_warmers: list[tuple[int, int, AsyncWarmer]] = [
        (int(warmer.priority), index, warmer) for index, warmer in enumerate(warmers)
    ]

    indexed_warmers.sort(key=lambda x: (x[0], x[1]))
    priority_groups: list[list[AsyncWarmer]] = [
        [item[2] for item in group] for _, group in itertools.groupby(indexed_warmers, key=lambda x: x[0])
    ]

    def process_result(warmer: AsyncWarmer, result: Any) -> Exception | None:
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, KeyboardInterrupt | SystemExit):
            raise result
        if isinstance(result, Exception):
            warmer_raise_on_failure = warmer.raise_on_failure
            if not isinstance(warmer_raise_on_failure, bool):
                raise TypeError(f"raise_on_failure must be bool, got {type(warmer_raise_on_failure).__name__}")
            logger.warning(
                "Warmer failed",
                extra={
                    "warmer_type": type(warmer).__name__,
                    "error": str(result),
                    "type": type(result).__name__,
                    "raise_on_failure": warmer_raise_on_failure,
                },
            )
            if warmer_raise_on_failure:
                return result
        return None

    all_exceptions: list[Exception] = []

    async def run_groups() -> None:
        for group in priority_groups:
            # Execute group in parallel without cancelling each other on failure
            results = await asyncio.gather(
                *(w.warmup() for w in group),
                return_exceptions=True,
            )
            for warmer, result in zip(group, results, strict=True):
                exc = process_result(warmer, result)
                if exc:
                    all_exceptions.append(exc)
            if all_exceptions and raise_on_failure:
                break

    try:
        if timeout is not None:
            async with asyncio.timeout(timeout):
                await run_groups()
        else:
            async with contextlib.nullcontext():
                await run_groups()

    except TimeoutError as e:
        logger.exception("Infrastructure warmup timed out", extra={"timeout": timeout})
        if raise_on_failure:
            raise WarmupError(f"Infrastructure warmup timed out after {timeout}s") from e
    except (WarmupError, asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        # Unexpected errors outside of execution logic
        logger.exception("Unexpected error during infrastructure warmup", extra={"error_type": type(e).__name__})
        if raise_on_failure:
            raise WarmupError(f"Infrastructure warmup failed due to unexpected error: {e}") from e

    if all_exceptions and raise_on_failure:
        raise WarmupError(f"Infrastructure warmup failed with {len(all_exceptions)} errors")

    if not all_exceptions:
        logger.info("Infrastructure warmup completed successfully")
    else:
        logger.warning("Infrastructure warmup completed with some failures")
