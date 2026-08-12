"""``GrpcEntrypoint``: a gRPC server folded onto the servicewright Entrypoint contract.

This is a faithful fold of the gRPC service-runtime prototype's
``run_async_grpc_app`` + ``_create_grpc_server`` + ``_collect_interceptors``
onto the Host + Entrypoints model:

- The :class:`AppSpec` stays transport-neutral; the entrypoint takes its own
  :class:`GrpcConfig`.
- The Host owns the run-loop and signals. ``bind`` creates the server, ``serve``
  starts it and waits on the host's ``stop`` event, ``drain``/``stop`` perform
  graceful / hard shutdown. The grpc-server-kit ``run_async_grpc_server`` helper
  (which installs its own signals and blocks) is intentionally NOT used.
- Per-RPC ``UnitScope`` is provided DI-agnostically by ``UnitScopeInterceptor``
  (added first, automatically), exposing the scope via :func:`current_unit_scope`.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from ...core.contracts import ServerEntrypoint
from ._imports import AsyncMetricsInterceptor, AsyncServer, bind_server_port, create_async_grpc_server, grpc
from .config import DEFAULT_HEALTH_SERVICE_NAME, GrpcConfig
from .context import get_default_context_setters
from .errors import ServiceErrorInterceptor
from .health import GrpcHealthBridge
from .interceptors import UnitScopeInterceptor
from .metrics import GrpcServerMetricsRecorder

if TYPE_CHECKING:
    from ...core.context import ContextSetter
    from ...core.spec import ServiceContext

logger = logging.getLogger(__name__)

# A DI-agnostic servicer registration callback. It receives the raw gRPC server
# and the ServiceContext so it can resolve singletons from ``ctx.app_scope`` and
# wire per-RPC dependencies via ``current_unit_scope`` inside servicer methods.
ServicerRegisterer = Callable[["grpc.aio.Server", "ServiceContext[Any, Any]"], "None | Awaitable[None]"]

InterceptorFactory = Callable[
    ["ServiceContext[Any, Any]"],
    "Sequence[grpc.aio.ServerInterceptor] | Awaitable[Sequence[grpc.aio.ServerInterceptor]]",
]


class GrpcEntrypoint(ServerEntrypoint):
    """A gRPC server entrypoint driven by the :class:`Host`.

    Args:
        config: Self-contained server configuration (NOT read from settings).
        servicers: Callback registering servicers on the gRPC server.
        interceptors: Static interceptors. They wrap the servicer *inside* the
            unit-scope and metrics interceptors but *outside* the service-error
            mapper, so a generic exception handler of yours (e.g. the kit's
            ``AsyncExceptionHandlerInterceptor``) sees an already-mapped
            ``AbortError`` instead of swallowing the domain error.
        interceptors_factory: Optional callback returning extra interceptors,
            resolved at ``bind`` time with the :class:`ServiceContext`.
        context_setters: Bridges that push the per-RPC context (request id, user
            id, trace id) into systems keeping their own store — structlog
            contextvars, OTel Baggage. Defaults to whichever of those is
            installed; pass an explicit sequence (possibly empty) to override.
        map_service_errors: Convert raised
            :class:`~servicewright.core.errors.ServiceError` into the mapped
            ``grpc.StatusCode`` abort (non-public errors masked). Default
            ``True``.
        enable_metrics: Add the RPC metrics interceptor, recording through the
            app's configured metrics sink (``ObsConfig(metrics=...)`` + the
            matching extra, e.g. servicewright[metrics] for prometheus).
        metrics_prefix: Optional metric name prefix.
        kind: Telemetry label (default ``"grpc"``).
        essential: Whether the entrypoint's exit/failure stops the process.
    """

    def __init__(
        self,
        *,
        config: GrpcConfig,
        servicers: ServicerRegisterer,
        interceptors: Sequence[grpc.aio.ServerInterceptor] = (),
        interceptors_factory: InterceptorFactory | None = None,
        context_setters: Sequence[ContextSetter] | None = None,
        map_service_errors: bool = True,
        enable_metrics: bool = False,
        metrics_prefix: str | None = None,
        kind: str = "grpc",
        essential: bool = True,
    ) -> None:
        self._config = config
        self._servicers = servicers
        self._static_interceptors: list[grpc.aio.ServerInterceptor] = list(interceptors)
        self._interceptors_factory = interceptors_factory
        self._context_setters = context_setters
        self._map_service_errors = map_service_errors
        self._enable_metrics = enable_metrics
        self._metrics_prefix = metrics_prefix
        self.kind = kind
        self.essential = essential

        self._server: AsyncServer | None = None
        self._health: GrpcHealthBridge | None = None
        self._health_watcher: asyncio.Task[None] | None = None
        self._bound_port: int | None = None
        self._stopped = False

    @property
    def config(self) -> GrpcConfig:
        """The server configuration."""
        return self._config

    @property
    def bound_port(self) -> int | None:
        """The actually bound port (useful when ``config.port == 0``)."""
        return self._bound_port

    async def bind(self, ctx: ServiceContext[Any, Any]) -> None:
        """Create the server, register servicers + health, and bind the port."""
        interceptors = await self._collect_interceptors(ctx)
        self._health = GrpcHealthBridge(ctx.health, service_names=self._config.health_service_names)

        server = create_async_grpc_server(
            interceptors=interceptors,
            settings=self._config,
            register_servicers=self._health.register,
            enable_reflection=self._config.enable_reflection,
            reflection_service_names=self._reflection_service_names(),
            enable_channelz=self._config.enable_channelz,
        )

        await self._run_servicers(server.raw_server, ctx)

        self._bound_port = bind_server_port(server, self._config)
        self._server = server
        logger.info(
            "gRPC entrypoint bound",
            extra={"service": ctx.service_name, "address": self._config.address, "port": self._bound_port},
        )

    async def serve(self, *, stop: asyncio.Event) -> None:
        """Start the server and run until the host's ``stop`` event is set.

        Returns while the server is still accepting: the Host flips readiness to
        false and only then calls :meth:`drain`. The health poller runs for
        exactly this window, so a dependency that fails mid-life flips the gRPC
        health service to ``NOT_SERVING`` without any traffic being refused.
        """
        if self._server is None:
            raise RuntimeError("serve() called before bind()")
        await self._server.start()
        if self._health is not None:
            await self._health.refresh()
            self._health_watcher = asyncio.create_task(self._health.watch(self._config.health_refresh_interval))
        logger.info("gRPC server started", extra={"address": self._config.address})
        try:
            await stop.wait()
        finally:
            await self._cancel_health_watcher()

    async def drain(self, grace: float) -> None:
        """Stop accepting RPCs and let in-flight ones finish within ``grace``.

        The effective budget is ``min(grace, config.grace_period)``: the Host's
        allowance bounds the shutdown, and the entrypoint's own setting can only
        shorten it.
        """
        if self._server is None or self._stopped:
            return
        await self._cancel_health_watcher()
        if self._health is not None:
            await self._health.enter_graceful_shutdown()
        await self._server.stop(min(grace, self._config.grace_period))
        self._stopped = True

    async def stop(self) -> None:
        """Hard stop the server immediately (idempotent with ``drain``)."""
        await self._cancel_health_watcher()
        if self._server is None or self._stopped:
            return
        await self._server.stop(None)
        self._stopped = True

    async def _cancel_health_watcher(self) -> None:
        """Stop the readiness poller, tolerating a never-started one."""
        watcher, self._health_watcher = self._health_watcher, None
        if watcher is None:
            return
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher

    async def _collect_interceptors(self, ctx: ServiceContext[Any, Any]) -> list[grpc.aio.ServerInterceptor]:
        # grpc.aio hands control in list order, so the first entry is the
        # OUTERMOST wrapper. UnitScopeInterceptor is therefore first: every
        # downstream interceptor and the servicer see a live per-RPC scope.
        setters = get_default_context_setters() if self._context_setters is None else self._context_setters
        interceptors: list[grpc.aio.ServerInterceptor] = [UnitScopeInterceptor(ctx.container, context_setters=setters)]

        if self._enable_metrics:
            recorder = GrpcServerMetricsRecorder(ctx.observability.metrics, prefix=self._metrics_prefix)
            interceptors.append(AsyncMetricsInterceptor(recorder, service_name=ctx.service_name))

        interceptors.extend(self._static_interceptors)

        if self._interceptors_factory is not None:
            result = self._interceptors_factory(ctx)
            interceptors.extend(await _resolve(result))

        # Innermost, i.e. closest to the servicer: a ServiceError must become a
        # mapped abort BEFORE any user interceptor sees it. A generic exception
        # handler placed by the user (the kit's AsyncExceptionHandlerInterceptor
        # maps unknown exceptions to INTERNAL) would otherwise swallow every
        # domain error and turn a deliberate NOT_FOUND into INTERNAL. It stays
        # inside the metrics interceptor, so aborts are still recorded with the
        # status the client actually receives.
        if self._map_service_errors:
            interceptors.append(ServiceErrorInterceptor())

        return interceptors

    async def _run_servicers(self, server: grpc.aio.Server, ctx: ServiceContext[Any, Any]) -> None:
        result = self._servicers(server, ctx)
        if asyncio.iscoroutine(result):
            await result

    def _reflection_service_names(self) -> list[str]:
        names = [DEFAULT_HEALTH_SERVICE_NAME]
        if self._config.reflection_service_names:
            names.extend(self._config.reflection_service_names)
        return names


async def _resolve(
    source: Sequence[grpc.aio.ServerInterceptor] | Awaitable[Sequence[grpc.aio.ServerInterceptor]],
) -> list[grpc.aio.ServerInterceptor]:
    """Resolve a possibly-awaitable interceptor source into a concrete list."""
    if inspect.isawaitable(source):
        resolved: Sequence[grpc.aio.ServerInterceptor] = await source
    else:
        resolved = source
    return list(resolved)


class GrpcPlugin:
    """Declarative wiring: register a :class:`GrpcEntrypoint` on the host.

    Pass the same arguments as :class:`GrpcEntrypoint`; ``on_register`` builds it
    and adds it to the host.
    """

    def __init__(
        self,
        *,
        config: GrpcConfig,
        servicers: ServicerRegisterer,
        interceptors: Sequence[grpc.aio.ServerInterceptor] = (),
        interceptors_factory: InterceptorFactory | None = None,
        context_setters: Sequence[ContextSetter] | None = None,
        map_service_errors: bool = True,
        enable_metrics: bool = False,
        metrics_prefix: str | None = None,
        kind: str = "grpc",
        essential: bool = True,
    ) -> None:
        self._entrypoint = GrpcEntrypoint(
            config=config,
            servicers=servicers,
            interceptors=interceptors,
            interceptors_factory=interceptors_factory,
            context_setters=context_setters,
            map_service_errors=map_service_errors,
            enable_metrics=enable_metrics,
            metrics_prefix=metrics_prefix,
            kind=kind,
            essential=essential,
        )

    @property
    def entrypoint(self) -> GrpcEntrypoint:
        """The entrypoint that will be registered on the host."""
        return self._entrypoint

    def on_register(self, spec: Any, host: Any) -> None:
        """Append the gRPC entrypoint to the host."""
        host.add_entrypoint(self._entrypoint)


__all__ = [
    "GrpcEntrypoint",
    "GrpcPlugin",
    "InterceptorFactory",
    "ServicerRegisterer",
]
