"""Lifecycle hook protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .container import AppScopeProtocol


class LifecycleHookProtocol(Protocol):
    """Protocol for lifecycle hook callbacks."""

    async def __call__(self, app_scope: AppScopeProtocol | None = None) -> None:
        """Execute lifecycle hook."""
        ...
