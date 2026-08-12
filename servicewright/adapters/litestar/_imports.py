"""Lazy import guard for the optional Litestar stack.

Importing this module fails with a friendly message when the ``litestar`` extra
is not installed, so ``import servicewright`` never pays the cost (or the
failure) of the Litestar dependencies. Every Litestar submodule imports its
third-party symbols from here.
"""

from __future__ import annotations

_INSTALL_HINT = "Litestar support requires servicewright[litestar]; install it."

try:
    import uvicorn
    from litestar import Litestar, Request, Response, Router, get
    from litestar.di import Provide
    from litestar.enums import MediaType, ScopeType
    from litestar.handlers import HTTPRouteHandler
    from litestar.middleware import ASGIMiddleware
    from litestar.status_codes import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE
    from litestar.types import ASGIApp, Receive, Scope, Send
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(_INSTALL_HINT) from exc

__all__ = [
    "HTTP_200_OK",
    "HTTP_503_SERVICE_UNAVAILABLE",
    "ASGIApp",
    "ASGIMiddleware",
    "HTTPRouteHandler",
    "Litestar",
    "MediaType",
    "Provide",
    "Receive",
    "Request",
    "Response",
    "Router",
    "Scope",
    "ScopeType",
    "Send",
    "get",
    "uvicorn",
]
