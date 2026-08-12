"""Common lifecycle manager exports."""

from __future__ import annotations

from ..contracts import LifecycleHookProtocol
from .manager import Lifecycle

__all__ = ["Lifecycle", "LifecycleHookProtocol"]
