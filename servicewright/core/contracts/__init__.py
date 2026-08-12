"""The complete contract surface: pure protocols and author-facing base classes.

Pure ``Protocol`` definitions and stateful author-facing ABCs live in separate
modules so the distinction stays visible; this package re-exports them as the
single import surface for the contract layer.
"""

from __future__ import annotations

from .bases import ScopedEntrypoint as ScopedEntrypoint
from .bases import ServerEntrypoint as ServerEntrypoint
from .container import AppScopeProtocol as AppScopeProtocol
from .container import DependencyContainerProtocol as DependencyContainerProtocol
from .container import UnitScopeProtocol as UnitScopeProtocol
from .entrypoint import Entrypoint as Entrypoint
from .health import HealthCheckerProtocol as HealthCheckerProtocol
from .lifecycle import LifecycleHookProtocol as LifecycleHookProtocol
from .observability import CounterProtocol as CounterProtocol
from .observability import ErrorReporterProtocol as ErrorReporterProtocol
from .observability import HistogramProtocol as HistogramProtocol
from .observability import Redactor as Redactor
from .observability import SpanProtocol as SpanProtocol
from .observability import TracerProtocol as TracerProtocol
from .plugin import Plugin as Plugin
from .settings import BaseServiceSettingsProtocol as BaseServiceSettingsProtocol
from .warmer import AsyncWarmer as AsyncWarmer

__all__ = [
    "AppScopeProtocol",
    "AsyncWarmer",
    "BaseServiceSettingsProtocol",
    "CounterProtocol",
    "DependencyContainerProtocol",
    "Entrypoint",
    "ErrorReporterProtocol",
    "HealthCheckerProtocol",
    "HistogramProtocol",
    "LifecycleHookProtocol",
    "Plugin",
    "Redactor",
    "ScopedEntrypoint",
    "ServerEntrypoint",
    "SpanProtocol",
    "TracerProtocol",
    "UnitScopeProtocol",
]
