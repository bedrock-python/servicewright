"""Dishka DI binding (``[dishka]`` extra).

Maps servicewright's two-tier scope model onto dishka's ``Scope.APP`` /
``Scope.REQUEST``. Importing this package requires ``servicewright[dishka]``.
"""

from __future__ import annotations

from .container import DishkaContainer as DishkaContainer
from .container import DishkaScope as DishkaScope

__all__ = ["DishkaContainer", "DishkaScope"]
