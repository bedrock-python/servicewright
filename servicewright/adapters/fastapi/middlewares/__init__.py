"""Vendored ASGI middleware slice (formerly asgi-middlewares-kit).

servicewright was the kit's only consumer, so the used slice lives here now:
context propagation, structured request logging, processing-time header and
Sentry scope enrichment, plus the ``ContextExtractor`` / ``ContextSetter``
protocols. The dead ``CSRFMiddleware`` (and its ``itsdangerous`` dependency)
was deliberately left behind.
"""

from __future__ import annotations

from .context import (
    ContextMiddleware,
    get_context,
    get_context_value,
    set_context_value,
)
from .errors import UnhandledErrorMiddleware
from .logging import LoggingMiddleware
from .processing_time import ProcessingTimeMiddleware
from .protocols import ContextExtractor, ContextSetter
from .sentry import SentryMiddleware

__all__ = [
    "ContextExtractor",
    "ContextMiddleware",
    "ContextSetter",
    "LoggingMiddleware",
    "ProcessingTimeMiddleware",
    "SentryMiddleware",
    "UnhandledErrorMiddleware",
    "get_context",
    "get_context_value",
    "set_context_value",
]
