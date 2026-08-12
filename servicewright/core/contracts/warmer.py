"""Async infrastructure warmer base (stateful ABC)."""

from __future__ import annotations

from abc import ABC, abstractmethod


class AsyncWarmer(ABC):
    """Base class for async infrastructure warmers."""

    def __init__(self, *, raise_on_failure: bool = True) -> None:
        """Initialize base warmer behavior."""
        self._raise_on_failure = raise_on_failure

    @property
    def raise_on_failure(self) -> bool:
        """Whether orchestrator should raise when this warmer fails."""
        return self._raise_on_failure

    @property
    def priority(self) -> int:
        """Priority of the warmer (lower value means higher priority).

        Warmers with higher priority (lower values) are executed first.
        Warmers with the same priority are executed in parallel.
        """
        return 0

    @abstractmethod
    async def warmup(self) -> None:
        """Perform infrastructure warmup.

        This method should initialize connection pools, fetch metadata,
        or perform any other operations to prepare the infrastructure
        for high-load production traffic.

        Raises:
            WarmupError: If warmup operation fails and cannot be recovered.
        """
        raise NotImplementedError
