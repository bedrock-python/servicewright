"""structlog logging backend: JSON/console rendering + stdlib bridging.

Both ``structlog``-native loggers and plain ``logging`` records render through
one processor chain (``ProcessorFormatter``), so third-party library logs come
out in the same format. The cross-cutting redactor runs as a processor on every
event dict.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..base import LoggingSink

try:
    import structlog
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("structlog logging requires servicewright[observability]; install it.") from exc

if TYPE_CHECKING:
    from ....core.contracts.observability import Redactor
    from ....core.observability.config import ObsSetupContext


class StructlogLoggingSink(LoggingSink):
    """structlog backend driven by ``settings.logging`` (level, JSON or console)."""

    backend = "structlog"

    def __init__(self) -> None:
        self._handler: logging.Handler | None = None

    def setup(self, ctx: ObsSetupContext) -> None:
        """Configure structlog and route the root logger through its renderer."""
        logging_settings = getattr(ctx.settings, "logging", None)
        if logging_settings is None or self._handler is not None:
            return

        level = _coerce_level(getattr(logging_settings, "level", "INFO"))
        use_json = bool(getattr(logging_settings, "use_json", True))

        shared_processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
        ]
        if ctx.redactor is not None:
            shared_processors.append(_redaction_processor(ctx.redactor))
        shared_processors.extend(
            [
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
            ]
        )

        renderer = structlog.processors.JSONRenderer() if use_json else structlog.dev.ConsoleRenderer()

        structlog.configure(
            processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        formatter = structlog.stdlib.ProcessorFormatter(
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
            foreign_pre_chain=shared_processors,
        )
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)

        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(level)
        self._handler = handler

    def shutdown(self) -> None:
        """Detach the installed handler and reset structlog defaults."""
        if self._handler is None:
            return
        logging.getLogger().removeHandler(self._handler)
        self._handler = None
        structlog.reset_defaults()


def _redaction_processor(redactor: Redactor) -> Any:
    def redact(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        return redactor(event_dict)

    return redact


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    return resolved if isinstance(resolved, int) else logging.INFO


__all__ = ["StructlogLoggingSink"]
