"""The settings contract as pydantic-settings models (``[settings]`` extra).

One model per observability section, carrying the defaults the built-in sinks
fall back to, and :class:`BaseServiceSettings` composing them into an
environment-loaded settings class that satisfies
:class:`~servicewright.core.contracts.BaseServiceSettingsProtocol`. Importing
this package requires ``servicewright[settings]``.
"""

from __future__ import annotations

from .models import BaseServiceSettings as BaseServiceSettings
from .models import ErrorTrackingSettings as ErrorTrackingSettings
from .models import LoggingSettings as LoggingSettings
from .models import MetricsSettings as MetricsSettings
from .models import TracingSettings as TracingSettings

__all__ = [
    "BaseServiceSettings",
    "ErrorTrackingSettings",
    "LoggingSettings",
    "MetricsSettings",
    "TracingSettings",
]
