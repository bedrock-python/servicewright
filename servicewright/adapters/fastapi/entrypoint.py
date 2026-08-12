"""``FastApiEntrypoint``: a FastAPI app folded onto the servicewright Entrypoint contract.

A faithful fold of the FastAPI service-runtime prototype onto the
Host + Entrypoints model:

- The :class:`AppSpec` stays transport-neutral; the entrypoint takes its own
  :class:`HttpConfig` at construction (never from global settings).
- **The Host owns lifecycle.** Unlike the prototype, the FastAPI app has NO
  lifespan managing the DI container/warmup/registration. ``bind`` just BUILDS
  the app (routes, middleware, exception handlers, health routes, metrics,
  OTel) and stores it; the Host already ran bootstrap -> app_scope -> warmup ->
  pre_start before calling ``bind`` and flips readiness + post_start after.
- **The Host owns signals + the serve loop.** ``bind`` opens the listening
  socket (so a port clash fails startup instead of a half-ready process),
  ``serve`` runs a ``uvicorn.Server`` whose signal capture is neutralized and
  returns — still accepting — once the host ``stop`` event is set. ``drain``
  then closes the listener and lets in-flight requests finish; ``stop`` is a
  hard stop.
- Per-request ``UnitScope`` is provided DI-agnostically by
  :class:`UnitScopeMiddleware` (installed automatically).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ...core.contracts import ServerEntrypoint
from .._uvicorn import UvicornRunner
from ._imports import FastAPI
from .config import HttpConfig, MiddlewareConfig
from .configurators import setup_exception_handlers, setup_health_routes, setup_middleware_stack
from .metrics import setup_metrics_instrumentator
from .observability import instrument_fastapi_app

if TYPE_CHECKING:
    from ...core.errors import HttpErrorRendererProtocol
    from ...core.spec import ServiceContext
    from .exceptions import ExceptionHandler

logger = logging.getLogger(__name__)

# A DI-agnostic route registration callback. It receives the FastAPI app and the
# ServiceContext so it can resolve singletons from ``ctx.app_scope`` and wire
# per-request dependencies via ``get_unit_scope`` / ``current_unit_scope``.
RoutesRegisterer = Callable[["FastAPI", "ServiceContext[Any, Any]"], "None | Awaitable[None]"]

# A final app-configuration hook (called last, after everything else is wired).
ConfigureApp = Callable[["FastAPI", "ServiceContext[Any, Any]"], None]


class FastApiEntrypoint(ServerEntrypoint):
    """A FastAPI server entrypoint driven by the :class:`Host`.

    Args:
        config: Self-contained server configuration (NOT read from settings).
        routers: APIRouter instances to ``include_router`` onto the app.
        routes_registerer: Optional callback to register routes imperatively,
            resolved at ``bind`` time with the :class:`ServiceContext`.
        middlewares: Middleware stack configuration.
        exception_handlers: Extra ``{exc_type: handler}`` mappings appended after
            the default handlers.
        default_exception_handlers: Install the default handlers
            (validation/HTTP/ServiceError/deadline/unhandled). Default ``True``.
        error_renderer: Wire-format renderer used by the default handlers;
            ``None`` = RFC 9457 ``ProblemDetailsRenderer``. Pass your own
            implementation to own the error format (custom envelope,
            localization) across every default handler.
        metrics: Expose in-app Prometheus metrics at ``/system/metrics`` via
            ``prometheus-fastapi-instrumentator`` (requires servicewright[fastapi]).
        configure_app: Final hook called with ``(app, ctx)`` after wiring.
        kind: Telemetry label (default ``"http"``).
        essential: Whether the entrypoint's exit/failure stops the process.
    """

    def __init__(
        self,
        *,
        config: HttpConfig | None = None,
        routers: tuple[Any, ...] = (),
        routes_registerer: RoutesRegisterer | None = None,
        middlewares: MiddlewareConfig | None = None,
        exception_handlers: dict[type[Exception], ExceptionHandler] | None = None,
        default_exception_handlers: bool = True,
        error_renderer: HttpErrorRendererProtocol | None = None,
        metrics: bool = False,
        configure_app: ConfigureApp | None = None,
        kind: str = "http",
        essential: bool = True,
    ) -> None:
        self._config = config if config is not None else HttpConfig()
        self._routers = tuple(routers)
        self._routes_registerer = routes_registerer
        self._middlewares = middlewares if middlewares is not None else MiddlewareConfig()
        self._exception_handlers: dict[type[Exception], ExceptionHandler] = dict(exception_handlers or {})
        self._default_exception_handlers = default_exception_handlers
        self._error_renderer = error_renderer
        self._metrics = metrics
        self._configure_app = configure_app
        self.kind = kind
        self.essential = essential

        self._app: FastAPI | None = None
        self._runner = UvicornRunner(
            host=self._config.host,
            port=self._config.port,
            graceful_timeout=self._config.graceful_timeout,
            uvicorn_kwargs=self._config.uvicorn_kwargs,
            label="FastAPI",
        )

    @property
    def config(self) -> HttpConfig:
        """The server configuration."""
        return self._config

    @property
    def app(self) -> FastAPI | None:
        """The built FastAPI application (``None`` before :meth:`bind`)."""
        return self._app

    @property
    def bound_port(self) -> int | None:
        """The actually bound port (useful when ``config.port == 0``)."""
        return self._runner.bound_port

    async def bind(self, ctx: ServiceContext[Any, Any]) -> None:
        """Build the FastAPI app and open the listening socket.

        The socket is opened here, not in :meth:`serve`, so a port clash aborts
        startup with an ``OSError`` while readiness is still false — instead of
        a process that reports ready and serves nothing. It also makes
        ``port=0`` usable: :attr:`bound_port` reports what the OS picked.
        """
        self._app = await self.build_app(ctx)
        bound_port = self._runner.bind()
        logger.info(
            "FastAPI entrypoint bound",
            extra={"service": ctx.service_name, "address": self._config.address, "port": bound_port},
        )

    async def build_app(self, ctx: ServiceContext[Any, Any]) -> FastAPI:
        """Construct a fully-configured FastAPI app from the :class:`ServiceContext`.

        Exposed for testability: callers can build the app without driving the
        serve loop. The app deliberately has NO container-managing lifespan —
        the Host owns the application scope.
        """
        app = self._create_app(ctx)

        if self._config.health.enabled:
            setup_health_routes(
                app,
                ctx.health,
                liveness_path=self._config.health.liveness_path,
                readiness_path=self._config.health.readiness_path,
            )

        setup_exception_handlers(
            app,
            setup_defaults=self._default_exception_handlers,
            error_renderer=self._error_renderer,
            custom_handlers=self._exception_handlers,
        )

        # OTel must instrument the app BEFORE middleware so it wraps the stack.
        instrument_fastapi_app(app, ctx, middlewares=self._middlewares, health=self._config.health)

        setup_middleware_stack(app, self._middlewares, ctx.container, error_renderer=self._error_renderer)

        for router in self._routers:
            app.include_router(router)

        await self._register_routes(app, ctx)

        if self._metrics:
            setup_metrics_instrumentator(app)

        if self._configure_app is not None:
            self._configure_app(app, ctx)

        return app

    async def serve(self, *, stop: asyncio.Event) -> None:
        """Serve on the bound socket until the host's ``stop`` event is set.

        Returns while the server is still accepting connections: the Host flips
        readiness to false first and only then calls :meth:`drain`, which is
        what closes the listener. Shutting uvicorn down here instead would make
        the readiness endpoint die before the load balancer stops routing, and
        would render the Host's drain grace meaningless.

        A server that dies on its own (a fatal uvicorn error) ends the wait and
        the failure is re-raised, so an essential entrypoint cannot leave the
        process alive and idle.
        """
        if self._app is None:
            raise RuntimeError("serve() called before bind()")
        await self._runner.serve(self._app, stop=stop)

    async def drain(self, grace: float) -> None:
        """Close the listener and let in-flight requests finish within ``grace``."""
        await self._runner.drain(grace)

    async def stop(self) -> None:
        """Hard stop the server immediately (idempotent with :meth:`drain`)."""
        await self._runner.stop()

    def _create_app(self, ctx: ServiceContext[Any, Any]) -> FastAPI:
        params: dict[str, Any] = {
            "title": self._config.title or ctx.service_name,
            "version": self._config.version,
            "openapi_url": self._config.openapi_url,
            "docs_url": self._config.docs_url,
            "redoc_url": self._config.redoc_url,
            "redirect_slashes": self._config.redirect_slashes,
        }
        params.update(self._config.fastapi_kwargs)
        return FastAPI(**params)

    async def _register_routes(self, app: FastAPI, ctx: ServiceContext[Any, Any]) -> None:
        if self._routes_registerer is None:
            return
        result = self._routes_registerer(app, ctx)
        if asyncio.iscoroutine(result):
            await result


class FastApiPlugin:
    """Declarative wiring: register a :class:`FastApiEntrypoint` on the host.

    Pass the same arguments as :class:`FastApiEntrypoint`; ``on_register`` builds
    it and adds it to the host.
    """

    def __init__(
        self,
        *,
        config: HttpConfig | None = None,
        routers: tuple[Any, ...] = (),
        routes_registerer: RoutesRegisterer | None = None,
        middlewares: MiddlewareConfig | None = None,
        exception_handlers: dict[type[Exception], ExceptionHandler] | None = None,
        default_exception_handlers: bool = True,
        error_renderer: HttpErrorRendererProtocol | None = None,
        metrics: bool = False,
        configure_app: ConfigureApp | None = None,
        kind: str = "http",
        essential: bool = True,
    ) -> None:
        self._entrypoint = FastApiEntrypoint(
            config=config,
            routers=routers,
            routes_registerer=routes_registerer,
            middlewares=middlewares,
            exception_handlers=exception_handlers,
            default_exception_handlers=default_exception_handlers,
            error_renderer=error_renderer,
            metrics=metrics,
            configure_app=configure_app,
            kind=kind,
            essential=essential,
        )

    @property
    def entrypoint(self) -> FastApiEntrypoint:
        """The entrypoint that will be registered on the host."""
        return self._entrypoint

    def on_register(self, spec: Any, host: Any) -> None:
        """Append the FastAPI entrypoint to the host."""
        host.add_entrypoint(self._entrypoint)


__all__ = [
    "ConfigureApp",
    "FastApiEntrypoint",
    "FastApiPlugin",
    "RoutesRegisterer",
]
