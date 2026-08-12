"""``LitestarEntrypoint``: a Litestar app folded onto the servicewright contract.

A lean, general HTTP :class:`~servicewright.core.contracts.ServerEntrypoint` binding
for Litestar, mirroring the FastAPI entrypoint shape:

- The :class:`AppSpec` stays transport-neutral; the entrypoint takes its own
  :class:`LitestarConfig` at construction (never from global settings).
- **The Host owns lifecycle.** The Litestar app has NO lifespan managing the DI
  container / warmup / registration. ``bind`` just BUILDS the app (route
  handlers, ``/system`` health routes, per-request unit scope middleware, the
  ``get_unit_scope`` dependency, optional ``configure_app`` hook) and stores it.
- **The Host owns signals + the serve loop.** ``bind`` opens the listening socket,
  ``serve`` runs uvicorn and returns — still accepting — once ``stop`` is set,
  and ``drain`` closes the listener. ``drain`` lets in-flight requests finish; ``stop`` is a
  hard stop.
- Per-request ``UnitScope`` is provided DI-agnostically by
  :class:`UnitScopeMiddleware` (installed automatically).

This binding is intentionally framework-pure: it carries NO middleware stack /
context / error-rendering machinery (those are FastAPI-specific folds), so it
stays a clean, reusable general Litestar entrypoint.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ...core.contracts import ServerEntrypoint
from .._uvicorn import UvicornRunner
from ._imports import Litestar, Provide
from .config import LitestarConfig
from .configurators import build_health_routes
from .unit_scope import UnitScopeMiddleware, get_unit_scope

if TYPE_CHECKING:
    from ...core.spec import ServiceContext

logger = logging.getLogger(__name__)

# A DI-agnostic route registration callback. It receives the ServiceContext so it
# can resolve singletons from ``ctx.app_scope`` and wire per-request dependencies
# via ``get_unit_scope`` / ``current_unit_scope``. It returns Litestar route
# handlers / routers to register on the app.
RouteRegisterer = Callable[["ServiceContext[Any, Any]"], "list[Any] | Awaitable[list[Any]]"]

# A final app-configuration hook (called last, after the app is built).
ConfigureApp = Callable[["Litestar", "ServiceContext[Any, Any]"], None]


class LitestarEntrypoint(ServerEntrypoint):
    """A Litestar server entrypoint driven by the :class:`Host`.

    Args:
        config: Self-contained server configuration (NOT read from settings).
        route_handlers: Litestar route handlers / routers to register on the app.
        route_registerer: Optional callback returning extra route handlers,
            resolved at ``bind`` time with the :class:`ServiceContext`.
        configure_app: Final hook called with ``(app, ctx)`` after the app is built.
        kind: Telemetry label (default ``"http"``).
        essential: Whether the entrypoint's exit/failure stops the process.
    """

    def __init__(
        self,
        *,
        config: LitestarConfig | None = None,
        route_handlers: tuple[Any, ...] = (),
        route_registerer: RouteRegisterer | None = None,
        configure_app: ConfigureApp | None = None,
        kind: str = "http",
        essential: bool = True,
    ) -> None:
        self._config = config if config is not None else LitestarConfig()
        self._route_handlers = tuple(route_handlers)
        self._route_registerer = route_registerer
        self._configure_app = configure_app
        self.kind = kind
        self.essential = essential

        self._app: Litestar | None = None
        self._runner = UvicornRunner(
            host=self._config.host,
            port=self._config.port,
            graceful_timeout=self._config.graceful_timeout,
            uvicorn_kwargs=self._config.uvicorn_kwargs,
            label="Litestar",
        )

    @property
    def config(self) -> LitestarConfig:
        """The server configuration."""
        return self._config

    @property
    def app(self) -> Litestar | None:
        """The built Litestar application (``None`` before :meth:`bind`)."""
        return self._app

    @property
    def bound_port(self) -> int | None:
        """The actually bound port (useful when ``config.port == 0``)."""
        return self._runner.bound_port

    async def bind(self, ctx: ServiceContext[Any, Any]) -> None:
        """Build the Litestar app and open the listening socket.

        Binding here (not in :meth:`serve`) turns a port clash into an ``OSError``
        during startup instead of a process that reports ready and serves nothing.
        """
        self._app = await self.build_app(ctx)
        bound_port = self._runner.bind()
        logger.info(
            "Litestar entrypoint bound",
            extra={"service": ctx.service_name, "address": self._config.address, "port": bound_port},
        )

    async def build_app(self, ctx: ServiceContext[Any, Any]) -> Litestar:
        """Construct a fully-configured Litestar app from the :class:`ServiceContext`.

        Exposed for testability: callers can build the app without driving the
        serve loop. The app deliberately has NO container-managing lifespan —
        the Host owns the application scope.
        """
        route_handlers: list[Any] = list(self._route_handlers)
        route_handlers.extend(await self._collect_registered_routes(ctx))

        if self._config.health.enabled:
            route_handlers.extend(
                build_health_routes(
                    ctx.health,
                    liveness_path=self._config.health.liveness_path,
                    readiness_path=self._config.health.readiness_path,
                )
            )

        # User litestar_kwargs are the BASE; the framework-managed keys are then
        # merged ON TOP so they can never be silently dropped (Litestar is built in
        # one call, so unlike FastAPI we cannot add middleware/deps post-construction).
        user_kwargs = dict(self._config.litestar_kwargs)
        user_middleware = list(user_kwargs.pop("middleware", []))
        user_dependencies = dict(user_kwargs.pop("dependencies", {}))
        user_route_handlers = list(user_kwargs.pop("route_handlers", []))
        # The Host owns observability/logging; Litestar must NOT install its own
        # LoggingConfig (it reconfigures the root logger via dictConfig).
        user_kwargs.pop("logging_config", None)

        params: dict[str, Any] = {
            **user_kwargs,
            "route_handlers": [*route_handlers, *user_route_handlers],
            # UnitScopeMiddleware outermost so the per-request scope wraps everything.
            "middleware": [UnitScopeMiddleware(ctx.container), *user_middleware],
            # The unit_scope dependency cannot be overridden by the user.
            "dependencies": {**user_dependencies, "unit_scope": Provide(get_unit_scope, sync_to_thread=False)},
            "logging_config": None,
        }
        app = Litestar(**params)

        if self._configure_app is not None:
            self._configure_app(app, ctx)

        return app

    async def serve(self, *, stop: asyncio.Event) -> None:
        """Serve on the bound socket until the host's ``stop`` event is set.

        Returns while still accepting: the Host flips readiness to false first
        and only then calls :meth:`drain`, which closes the listener.
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

    async def _collect_registered_routes(self, ctx: ServiceContext[Any, Any]) -> list[Any]:
        if self._route_registerer is None:
            return []
        result = self._route_registerer(ctx)
        if inspect.isawaitable(result):
            return list(await result)
        return list(result)


class LitestarPlugin:
    """Declarative wiring: register a :class:`LitestarEntrypoint` on the host.

    Pass the same arguments as :class:`LitestarEntrypoint`; ``on_register`` builds
    it and adds it to the host.
    """

    def __init__(
        self,
        *,
        config: LitestarConfig | None = None,
        route_handlers: tuple[Any, ...] = (),
        route_registerer: RouteRegisterer | None = None,
        configure_app: ConfigureApp | None = None,
        kind: str = "http",
        essential: bool = True,
    ) -> None:
        self._entrypoint = LitestarEntrypoint(
            config=config,
            route_handlers=route_handlers,
            route_registerer=route_registerer,
            configure_app=configure_app,
            kind=kind,
            essential=essential,
        )

    @property
    def entrypoint(self) -> LitestarEntrypoint:
        """The entrypoint that will be registered on the host."""
        return self._entrypoint

    def on_register(self, spec: Any, host: Any) -> None:
        """Append the Litestar entrypoint to the host."""
        host.add_entrypoint(self._entrypoint)


__all__ = [
    "ConfigureApp",
    "LitestarEntrypoint",
    "LitestarPlugin",
    "RouteRegisterer",
]
