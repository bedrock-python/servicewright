"""Protocols for pluggable context extraction and propagation.

``ContextSetter`` is the transport-neutral protocol owned by
:mod:`servicewright.core.context` (re-exported here for convenience);
``ContextExtractor`` is HTTP-specific (it reads the ASGI scope and headers).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from ....core.context import ContextSetter

if TYPE_CHECKING:
    from starlette.datastructures import Headers
    from starlette.types import Scope


class ContextExtractor(Protocol):
    """Extracts context values from the request scope and headers."""

    def __call__(self, scope: Scope, headers: Headers) -> dict[str, Any]:
        """Return a dictionary of context values to set."""
        ...


__all__ = ["ContextExtractor", "ContextSetter"]
