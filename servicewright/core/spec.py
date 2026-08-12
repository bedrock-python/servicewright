"""Declarative specification for microservices."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from .constants import DEFAULT_CLEANUP_TIMEOUT_SECONDS, DEFAULT_DRAIN_GRACE_SECONDS
from .contracts import (
    AppScopeProtocol,
    BaseServiceSettingsProtocol,
    DependencyContainerProtocol,
)
from .health import HealthRegistry
from .lifecycle.manager import Lifecycle
from .observability.manager import ObservabilityManager

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from .contracts import AsyncWarmer

    WarmerFactory = Callable[
        ["ServiceContext[Any, Any]"],
        "Sequence[AsyncWarmer] | Awaitable[Sequence[AsyncWarmer]]",
    ]

TSettings = TypeVar("TSettings", bound="BaseServiceSettingsProtocol")
TContainer = TypeVar("TContainer", bound="DependencyContainerProtocol")


@dataclass(slots=True)
class BootstrapContext[TSettings: "BaseServiceSettingsProtocol", TContainer: "DependencyContainerProtocol"]:
    """Context available before the application scope is opened."""

    settings: TSettings
    service_name: str
    container: TContainer
    lifecycle: Lifecycle = field(default_factory=Lifecycle)


@dataclass(slots=True)
class ServiceContext[TSettings: "BaseServiceSettingsProtocol", TContainer: "DependencyContainerProtocol"]:
    """Context available once the application scope is opened."""

    bootstrap: BootstrapContext[TSettings, TContainer]
    app_scope: AppScopeProtocol
    health: HealthRegistry
    # The spec's manager; entrypoints mint their emit handles (recorders,
    # tracers, instrumentation) from here at bind time. Defaults to an
    # all-NullObject manager so hand-built contexts in tests stay cheap.
    observability: ObservabilityManager = field(default_factory=ObservabilityManager)

    @property
    def settings(self) -> TSettings:
        return self.bootstrap.settings

    @property
    def service_name(self) -> str:
        return self.bootstrap.service_name

    @property
    def container(self) -> TContainer:
        return self.bootstrap.container

    @property
    def lifecycle(self) -> Lifecycle:
        return self.bootstrap.lifecycle


@dataclass(slots=True)
class AppSpec[TSettings: "BaseServiceSettingsProtocol", TContainer: "DependencyContainerProtocol"]:
    """Complete transport-neutral declarative description of a microservice."""

    service_name: str
    create_container: Callable[[TSettings], TContainer]
    lifecycle: Lifecycle = field(default_factory=Lifecycle)
    observability: ObservabilityManager = field(default_factory=ObservabilityManager)
    health: HealthRegistry = field(default_factory=HealthRegistry)
    warmers: list[AsyncWarmer] = field(default_factory=list)
    warmers_factory: WarmerFactory | None = None
    drain_grace_seconds: float = DEFAULT_DRAIN_GRACE_SECONDS
    """How long each entrypoint gets to finish in-flight work during drain."""
    cleanup_timeout_seconds: float = DEFAULT_CLEANUP_TIMEOUT_SECONDS
    """Budget for each post-drain step (``stop()``, hooks, observability flush)."""
