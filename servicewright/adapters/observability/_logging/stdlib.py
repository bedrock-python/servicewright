"""Zero-dependency stdlib logging backend (plain text or JSON lines).

The reduced fallback when structlog is not wanted/installed: one root handler,
level from settings, optional JSON rendering via ``json.dumps``. The redactor
runs over the JSON payload (including ``extra`` fields).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ..base import LoggingSink

if TYPE_CHECKING:
    from ....core.contracts.observability import Redactor
    from ....core.observability.config import ObsSetupContext

_PLAIN_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# logging.LogRecord attributes that are plumbing, not user payload.
_RESERVED_RECORD_KEYS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class _JsonFormatter(logging.Formatter):
    """Renders each record as one JSON line: core fields + ``extra`` payload."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _RESERVED_RECORD_KEYS and not key.startswith("_")
            }
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if self._redactor is not None:
            payload = self._redactor(payload)
        return json.dumps(payload, default=str, ensure_ascii=False)


class StdlibLoggingSink(LoggingSink):
    """stdlib backend driven by ``settings.logging`` (level, JSON or plain)."""

    backend = "stdlib"

    def __init__(self) -> None:
        self._handler: logging.Handler | None = None

    def setup(self, ctx: ObsSetupContext) -> None:
        """Install one root handler with the configured formatter and level."""
        logging_settings = getattr(ctx.settings, "logging", None)
        if logging_settings is None or self._handler is not None:
            return

        level = _coerce_level(getattr(logging_settings, "level", "INFO"))
        use_json = bool(getattr(logging_settings, "use_json", True))

        handler = logging.StreamHandler()
        formatter: logging.Formatter = (
            _JsonFormatter(redactor=ctx.redactor) if use_json else logging.Formatter(_PLAIN_FORMAT)
        )
        handler.setFormatter(formatter)

        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(level)
        self._handler = handler

    def shutdown(self) -> None:
        """Detach the installed handler."""
        if self._handler is None:
            return
        logging.getLogger().removeHandler(self._handler)
        self._handler = None


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelName(str(level).upper())
    return resolved if isinstance(resolved, int) else logging.INFO


__all__ = ["StdlibLoggingSink"]
