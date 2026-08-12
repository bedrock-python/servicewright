"""Lazy import guard for the optional FastAPI/HTTP stack.

Importing this module fails with a friendly message when the ``fastapi`` extra
is not installed, so ``import servicewright`` never pays the cost (or the
failure) of the FastAPI dependencies. Every HTTP submodule imports its
third-party symbols from here.
"""

from __future__ import annotations

_INSTALL_HINT = "FastAPI support requires servicewright[fastapi]; install it."

try:
    import uvicorn
    from fastapi import FastAPI, Request, status
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.middleware.gzip import GZipMiddleware
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.responses import JSONResponse, Response
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(_INSTALL_HINT) from exc

__all__ = [
    "CORSMiddleware",
    "FastAPI",
    "GZipMiddleware",
    "JSONResponse",
    "Request",
    "Response",
    "StarletteHTTPException",
    "status",
    "uvicorn",
]
