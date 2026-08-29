"""The Host kernel: lifecycle ordering, run-loop, drain and readiness."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, TypeVar

from ..constants import DEFAULT_DRAIN_TIMEOUT_BUFFER_SECONDS
from ..exceptions import CleanupTimeoutError, DrainTimeoutError, ServiceWrightError
from ..signals import install_signal_handlers
from ..spec import BootstrapContext, ServiceContext
from ..warmup.orchestrator import collect_warmers, perform_warmup

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence

    from ..contracts import (
        BaseServiceSettingsProtocol,
        DependencyContainerProtocol,
        Entrypoint,
        Plugin,
    )
    from ..spec import AppSpec

TSettings = TypeVar("TSettings", bound="BaseServiceSettingsProtocol")
TContainer = TypeVar("TContainer", bound="DependencyContainerProtocol")

logger = logging.getLogger(__name__)


class Host[TSettings: "BaseServiceSettingsProtocol", TContainer: "DependencyContainerProtocol"]:
    """Runs an :class:`AppSpec` plus a list of :class:`Entrypoint` drivers.

    Owns the unified lifecycle: Bootstrap -> Warmup -> Ready -> Serve -> Drain
    -> Cleanup. It treats every entrypoint identically and never branches on
    ``kind``.

    The run-loop reports failure by raising, so the process exit code is
    meaningful: an essential entrypoint that dies during ``serve`` propagates its
    exception out of :meth:`run` (after cleanup), and a shutdown step that blows
    past its budget raises :class:`DrainTimeoutError`/:class:`CleanupTimeoutError`
    when nothing else is already propagating.
    """

    def __init__(self, spec: AppSpec[TSettings, TContainer]) -> None:
        self.spec = spec
        self._entrypoints: list[Entrypoint] = []
        self._bound: list[Entrypoint] = []

    def add_entrypoint(self, entrypoint: Entrypoint) -> None:
        """Append an entrypoint (typically from a plugin's ``on_register``)."""
        self._entrypoints.append(entrypoint)

    def bootstrap(self, settings: TSettings) -> BootstrapContext[TSettings, TContainer]:
        """Build the container (the application scope is not yet entered)."""
        return BootstrapContext(
            settings=settings,
            service_name=self.spec.service_name,
            container=self.spec.create_container(settings),
            lifecycle=self.spec.lifecycle,
        )

    async def run(
        self,
        settings: TSettings,
        entrypoints: Iterable[Entrypoint] = (),
        *,
        plugins: Iterable[Plugin] = (),
        stop: asyncio.Event | None = None,
    ) -> None:
        """Run the full lifecycle, blocking until ``stop`` is set.

        Args:
            settings: Service settings.
            entrypoints: Drivers to run.
            plugins: Plugins applied via ``on_register`` before the run-loop.
            stop: Externally supplied stop event. When provided, OS signal
                handlers are NOT installed (the embedding/test path).

        Raises:
            Exception: Whatever an essential entrypoint raised while serving, or
                whatever startup raised — after cleanup has run.
            ServiceWrightError: If a shutdown step exceeded its budget and no
                other exception is propagating.
        """
        self._entrypoints = list(entrypoints)
        self._bound = []
        for plugin in plugins:
            plugin.on_register(self.spec, self)

        self.spec.observability.configure(settings, service_name=self.spec.service_name)
        uninstall_signal_handlers: Callable[[], None] | None = None
        timeouts: list[ServiceWrightError] = []
        try:
            bootstrap_ctx = self.bootstrap(settings)

            if stop is None:
                stop = asyncio.Event()
                uninstall_signal_handlers = install_signal_handlers(stop)

            await self._run_with_app_scope(bootstrap_ctx, stop)
        finally:
            if uninstall_signal_handlers is not None:
                uninstall_signal_handlers()
            timeouts.extend(await self._final_cleanup())

        # Only reachable when nothing else is propagating: a shutdown that blew
        # its budget must not mask the failure that caused the shutdown.
        if timeouts:
            raise timeouts[0]

    async def _final_cleanup(self) -> list[ServiceWrightError]:
        """Flush observability and run post-shutdown hooks, both time-boxed."""
        budget = self.spec.cleanup_timeout_seconds
        timeouts: list[ServiceWrightError] = []
        # Sink shutdown flushes (traces, sentry events) and joins the metrics
        # server thread — off the loop so it can never block the last steps.
        timeouts.extend(
            await self._shutdown_step(
                asyncio.to_thread(self.spec.observability.shutdown),
                budget=budget,
                error=CleanupTimeoutError,
                phase="observability shutdown",
            )
        )
        timeouts.extend(
            await self._shutdown_step(
                self.spec.lifecycle.run_post_shutdown_hooks(None),
                budget=budget,
                error=CleanupTimeoutError,
                phase="post-shutdown hooks",
            )
        )
        return timeouts

    async def _run_with_app_scope(
        self,
        bootstrap_ctx: BootstrapContext[TSettings, TContainer],
        stop: asyncio.Event,
    ) -> None:
        async with bootstrap_ctx.container.app_scope() as app_scope:
            service_ctx: ServiceContext[TSettings, TContainer] = ServiceContext(
                bootstrap=bootstrap_ctx,
                app_scope=app_scope,
                health=self.spec.health,
                observability=self.spec.observability,
            )
            timeouts: list[ServiceWrightError] = []
            try:
                if await self._startup(service_ctx, self._entrypoints, stop):
                    await self._serve(self._entrypoints, stop)
            finally:
                timeouts = await self._shutdown_in_scope(service_ctx)

            if timeouts:
                raise timeouts[0]

    async def _startup(
        self,
        service_ctx: ServiceContext[TSettings, TContainer],
        entrypoints: Sequence[Entrypoint],
        stop: asyncio.Event,
    ) -> bool:
        """Warm up, bind every entrypoint and flip readiness.

        Returns:
            ``True`` when the service is fully started and should serve.
            ``False`` when ``stop`` arrived first — startup is abandoned at the
            next phase boundary rather than binding ports and flipping readiness
            on a process that has already been asked to terminate.
        """
        if stop.is_set():
            return self._abandon_startup("before warmup")

        await self._collect_and_warmup(service_ctx, stop)
        if stop.is_set():
            return self._abandon_startup("during warmup")

        await self.spec.lifecycle.run_pre_start_hooks(service_ctx.app_scope)
        for entrypoint in entrypoints:
            if stop.is_set():
                return self._abandon_startup("during bind")
            # Recorded before the await: a bind that fails halfway through has
            # already allocated something, so it must still be torn down.
            self._bound.append(entrypoint)
            await entrypoint.bind(service_ctx)

        if stop.is_set():
            return self._abandon_startup("after bind")

        self.spec.health.ready = True
        await self.spec.lifecycle.run_post_start_hooks(service_ctx.app_scope)
        logger.info(
            "Service ready",
            extra={"service": self.spec.service_name, "event_loop": _loop_implementation()},
        )
        return True

    def _abandon_startup(self, phase: str) -> bool:
        logger.info(
            "Stop requested during startup; skipping serve",
            extra={"service": self.spec.service_name, "phase": phase},
        )
        return False

    async def _collect_and_warmup(
        self,
        service_ctx: ServiceContext[TSettings, TContainer],
        stop: asyncio.Event,
    ) -> None:
        """Prime every warmer, abandoning the wait as soon as ``stop`` is set."""
        warmers = await collect_warmers(
            base_warmers=self.spec.warmers,
            warmers_factory=self.spec.warmers_factory,
            app_ctx=service_ctx,
        )
        warmup = asyncio.ensure_future(perform_warmup(self.spec.service_name, list(warmers)))
        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({warmup, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter

        if warmup.done():
            await warmup  # Propagate a warmup failure to the caller of run().
            return

        warmup.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warmup
        logger.warning(
            "Warmup abandoned: stop requested",
            extra={"service": self.spec.service_name},
        )

    async def _serve(self, entrypoints: Sequence[Entrypoint], stop: asyncio.Event) -> None:
        """Serve every entrypoint until ``stop``; re-raise an essential failure."""
        if not entrypoints:
            await stop.wait()
            return

        async def runner(entrypoint: Entrypoint) -> None:
            try:
                await entrypoint.serve(stop=stop)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Entrypoint serve failed",
                    extra={"service": self.spec.service_name, "kind": entrypoint.kind},
                )
                if entrypoint.essential:
                    stop.set()
                    raise
                return
            if entrypoint.essential and not stop.is_set():
                logger.info(
                    "Essential entrypoint exited; stopping service",
                    extra={"service": self.spec.service_name, "kind": entrypoint.kind},
                )
                stop.set()

        failure: BaseException | None = None
        try:
            async with asyncio.TaskGroup() as group:
                for entrypoint in entrypoints:
                    group.create_task(runner(entrypoint))
        except* Exception as eg:
            logger.exception(
                "Serve loop aborted by entrypoint failure",
                extra={"service": self.spec.service_name, "errors": len(eg.exceptions)},
            )
            failure = _sole_cause(eg)

        # Only essential failures reach here (runner() swallows the rest), and an
        # essential failure must reach the caller: a process that dies mid-serve
        # may not exit 0, or every exit-code-based supervisor reads it as success.
        if failure is not None:
            raise failure

    async def _shutdown_in_scope(
        self,
        service_ctx: ServiceContext[TSettings, TContainer],
    ) -> list[ServiceWrightError]:
        """Drain, stop and run pre-shutdown hooks; return any budget overruns."""
        # Stop routing first (k8s/LB) before we stop accepting work.
        self.spec.health.ready = False

        grace = self.spec.drain_grace_seconds
        budget = self.spec.cleanup_timeout_seconds
        timeouts: list[ServiceWrightError] = []

        for entrypoint in reversed(self._bound):
            timeouts.extend(
                await self._shutdown_step(
                    entrypoint.drain(grace),
                    budget=grace + DEFAULT_DRAIN_TIMEOUT_BUFFER_SECONDS,
                    error=DrainTimeoutError,
                    phase="drain",
                    kind=entrypoint.kind,
                )
            )

        for entrypoint in reversed(self._bound):
            timeouts.extend(
                await self._shutdown_step(
                    entrypoint.stop(),
                    budget=budget,
                    error=CleanupTimeoutError,
                    phase="stop",
                    kind=entrypoint.kind,
                )
            )

        timeouts.extend(
            await self._shutdown_step(
                self.spec.lifecycle.run_pre_shutdown_hooks(service_ctx.app_scope),
                budget=budget,
                error=CleanupTimeoutError,
                phase="pre-shutdown hooks",
            )
        )

        logger.info("Service shutdown complete", extra={"service": self.spec.service_name})
        return timeouts

    async def _shutdown_step(
        self,
        step: Coroutine[object, object, None] | Awaitable[None],
        *,
        budget: float,
        error: type[ServiceWrightError],
        phase: str,
        kind: str | None = None,
    ) -> list[ServiceWrightError]:
        """Await one shutdown step under a budget, never letting it abort the rest.

        A failing step is logged and skipped so the remaining entrypoints still
        get their turn; a step that exceeds ``budget`` is returned to the caller,
        which raises it once every other step has had its chance.
        """
        extra = {"service": self.spec.service_name, "phase": phase, "kind": kind, "budget": budget}
        try:
            await asyncio.wait_for(step, timeout=budget)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except TimeoutError as exc:
            timeout_error = error(f"{phase} did not finish within {budget}s")
            timeout_error.__cause__ = exc
            logger.warning("Shutdown step timed out", extra=extra, exc_info=timeout_error)
            return [timeout_error]
        except Exception:
            logger.exception("Shutdown step failed", extra=extra)
        return []


def _loop_implementation() -> str:
    """The running loop's ``module.Class`` (``uvloop.Loop``, ``asyncio.unix_events._UnixSelectorEventLoop``)."""
    loop_type = type(asyncio.get_running_loop())
    return f"{loop_type.__module__}.{loop_type.__qualname__}"


def _sole_cause(group: BaseExceptionGroup[Exception]) -> BaseException:
    """Unwrap a group holding a single failure, so callers see the real error.

    A one-entrypoint crash should surface as ``RuntimeError: ...`` rather than as
    a nested ``ExceptionGroup``; genuine multi-failures keep the group.
    """
    leaves: list[BaseException] = []
    pending: list[BaseException] = list(group.exceptions)
    while pending:
        exc = pending.pop()
        if isinstance(exc, BaseExceptionGroup):
            pending.extend(exc.exceptions)
        else:
            leaves.append(exc)
    return leaves[0] if len(leaves) == 1 else group
