"""Lifecycle management for microservices with hook support."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..contracts import AppScopeProtocol, LifecycleHookProtocol

logger = logging.getLogger(__name__)


class Lifecycle:
    """Manages service lifecycle with customizable hooks.

    Provides extension points for custom logic at various
    stages of service lifecycle without requiring inheritance.
    """

    def __init__(self) -> None:
        self._pre_start_hooks: list[LifecycleHookProtocol] = []
        self._post_start_hooks: list[LifecycleHookProtocol] = []
        self._pre_shutdown_hooks: list[LifecycleHookProtocol] = []
        self._post_shutdown_hooks: list[LifecycleHookProtocol] = []

    def add_pre_start_hook(self, hook: LifecycleHookProtocol) -> None:
        """Add hook to execute before service starts.

        Args:
            hook: Async callable to execute before service initialization.
        """
        self._pre_start_hooks.append(hook)

    def add_post_start_hook(self, hook: LifecycleHookProtocol) -> None:
        """Add hook to execute after service starts.

        Args:
            hook: Async callable to execute after service is ready.
        """
        self._post_start_hooks.append(hook)

    def add_pre_shutdown_hook(self, hook: LifecycleHookProtocol) -> None:
        """Add hook to execute before service shutdown.

        Args:
            hook: Async callable to execute before service stops.
        """
        self._pre_shutdown_hooks.append(hook)

    def add_post_shutdown_hook(self, hook: LifecycleHookProtocol) -> None:
        """Add hook to execute after service shutdown.

        Args:
            hook: Async callable to execute after cleanup.
        """
        self._post_shutdown_hooks.append(hook)

    async def run_pre_start_hooks(self, app_scope: AppScopeProtocol | None = None) -> None:
        """Execute all pre-start hooks in registration order.

        Aborts startup if any hook fails.

        Args:
            app_scope: Optional application scope for dependency resolution.
        """
        await self._run_hooks(self._pre_start_hooks, "Pre-start", app_scope, raise_on_failure=True)

    async def run_post_start_hooks(self, app_scope: AppScopeProtocol) -> None:
        """Execute all post-start hooks in registration order.

        Aborts startup if any hook fails.

        Args:
            app_scope: Application scope for dependency resolution.
        """
        await self._run_hooks(self._post_start_hooks, "Post-start", app_scope, raise_on_failure=True)

    async def run_pre_shutdown_hooks(self, app_scope: AppScopeProtocol | None = None) -> None:
        """Execute all pre-shutdown hooks in registration order.

        Continues with other hooks even if one fails to ensure maximum cleanup.

        Args:
            app_scope: Optional application scope for dependency resolution.
        """
        await self._run_hooks(self._pre_shutdown_hooks, "Pre-shutdown", app_scope, raise_on_failure=False)

    async def run_post_shutdown_hooks(self, app_scope: AppScopeProtocol | None = None) -> None:
        """Execute all post-shutdown hooks in registration order.

        Continues with other hooks even if one fails to ensure maximum cleanup.

        Args:
            app_scope: Optional application scope for dependency resolution.
        """
        await self._run_hooks(self._post_shutdown_hooks, "Post-shutdown", app_scope, raise_on_failure=False)

    async def _run_hooks(
        self,
        hooks: list[LifecycleHookProtocol],
        label: str,
        app_scope: AppScopeProtocol | None,
        raise_on_failure: bool = True,
    ) -> None:
        """Internal helper to execute a list of hooks.

        Args:
            hooks: List of async callables to execute.
            label: Label for logging (e.g., 'Pre-start').
            app_scope: Application scope to pass to each hook.
            raise_on_failure: Whether to re-raise exceptions or just log them.
        """
        hooks_to_run = list(hooks)

        for hook in hooks_to_run:
            try:
                # Introspect signature BEFORE calling to avoid catching TypeErrors from hook body
                sig = inspect.signature(hook)
                params = list(sig.parameters.values())

                if len(params) == 0:
                    await hook()  # type: ignore[call-arg]
                else:
                    await hook(app_scope)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                logger.exception(f"{label} hook failed", extra={"hook": repr(hook)})
                if raise_on_failure:
                    raise
