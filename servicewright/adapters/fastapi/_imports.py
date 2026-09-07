"""Lazy import guard for the optional FastAPI/HTTP stack.

Importing this module fails with a friendly message when the ``fastapi`` extra
is not installed, so ``import servicewright`` never pays the cost (or the
failure) of the FastAPI dependencies. Every HTTP submodule imports its
third-party symbols from here -- ``starlette``, ``pydantic`` and
``deadline_budget`` included, none of which is a name the extras table mentions.
A submodule that reaches for one of them directly gets there first and reports
it by its own name, which is what issue #51 was.

``TYPE_CHECKING``-only imports are exempt: they never run.
"""

from __future__ import annotations

_INSTALL_HINT = "FastAPI support requires servicewright[fastapi]; install it."

try:
    import uvicorn
    from deadline_budget import DeadlineExceededError
    from fastapi import Depends, FastAPI, Header, Request, status
    from fastapi.exceptions import RequestValidationError
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.middleware.gzip import GZipMiddleware
    from pydantic import BaseModel, ConfigDict, Field
    from starlette.datastructures import Headers, MutableHeaders
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.responses import JSONResponse, Response
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(_INSTALL_HINT) from exc

__all__ = [
    "BaseModel",
    "CORSMiddleware",
    "ConfigDict",
    "DeadlineExceededError",
    "Depends",
    "FastAPI",
    "Field",
    "GZipMiddleware",
    "Header",
    "Headers",
    "JSONResponse",
    "MutableHeaders",
    "Request",
    "RequestValidationError",
    "Response",
    "StarletteHTTPException",
    "status",
    "uvicorn",
]
