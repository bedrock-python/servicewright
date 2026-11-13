"""structlog logging backend: JSON/console rendering + stdlib bridging.

Both ``structlog``-native loggers and plain ``logging`` records render through
one processor chain (``ProcessorFormatter``), so third-party library logs come
out in the same format — payload included: ``ExtraAdder`` merges a stdlib
record's ``extra`` into the event dict, which is where servicewright's own log
lines keep their fields. The cross-cutting redactor runs as a processor on every
event dict, after that merge.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, TextIO

from ..base import LoggingSink

try:
    import structlog
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("structlog logging requires servicewright[observability]; install it.") from exc

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ....core.contracts.observability import Redactor
    from ....core.observability.config import ObsSetupContext


class StructlogLoggingSink(LoggingSink):
    """structlog backend driven by ``settings.logging`` (level, JSON or console).

    Args:
        event_key: Key that holds a line's text in JSON output, for a log pipeline that reads
            it from somewhere other than ``event`` (``"message"`` for ELK). ``None`` (the
            default) takes ``settings.logging.event_key``, and ``"event"`` when the section has
            no such field. Console output always keeps ``event``, which is what the console
            renderer reads. The text replaces a field of the same name on the line.
        extra_processors: structlog processors run on every line, structlog-native and stdlib
            alike, after ``ExtraAdder`` and before the redactor, so whatever they add is
            redacted like the rest of the payload.
        stream: Where the lines are written. ``None`` (the default) is stderr.
    """

    backend = "structlog"

    def __init__(
        self,
        *,
        event_key: str | None = None,
        extra_processors: Sequence[structlog.typing.Processor] = (),
        stream: TextIO | None = None,
    ) -> None:
        self._event_key = event_key
        self._extra_processors = tuple(extra_processors)
        self._stream = stream
        self._handler: logging.Handler | None = None

    def setup(self, ctx: ObsSetupContext) -> None:
        """Configure structlog and route the root logger through its renderer."""
        logging_settings = getattr(ctx.settings, "logging", None)
        if logging_settings is None or self._handler is not None:
            return

        level = _coerce_level(getattr(logging_settings, "level", "INFO"))
        use_json = bool(getattr(logging_settings, "use_json", True))
        event_key = self._event_key if self._event_key is not None else getattr(logging_settings, "event_key", "event")

        shared_processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            # A foreign record carries its payload in ``record.__dict__``, and structlog merges it
            # only when asked to. Without this the library's own lines lose every field they carry
            # — the FastAPI access log's method/path/status, a failing warmer's name — since those
            # are all emitted as ``logger.info(..., extra={...})``.
            structlog.stdlib.ExtraAdder(),
            *self._extra_processors,
        ]
        # After ExtraAdder and the caller's processors on purpose: a recovered or added field is payload
        # like any other and must face the redactor, or a secret passed through ``extra`` would bypass it.
        if ctx.redactor is not None:
            shared_processors.append(_redaction_processor(ctx.redactor))
        shared_processors.extend(
            [
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
            ]
        )

        renderer = structlog.processors.JSONRenderer() if use_json else structlog.dev.ConsoleRenderer()
        # JSON only, since ConsoleRenderer reads ``event``; and right before the renderer, since the
        # processors ahead of it may read ``event`` too.
        rename = [structlog.processors.EventRenamer(event_key)] if use_json and event_key != "event" else []

        structlog.configure(
            processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        formatter = structlog.stdlib.ProcessorFormatter(
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *rename, renderer],
            foreign_pre_chain=shared_processors,
        )
        handler = logging.StreamHandler(self._stream)
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
