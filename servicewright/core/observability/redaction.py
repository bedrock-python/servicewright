"""Built-in sensitive-data redactors (pure stdlib).

:class:`~servicewright.core.contracts.observability.Redactor` implementations
the manager threads into the logging, error-tracking and tracing sinks. Any
callable ``dict -> dict`` works in their place.

- :class:`KeyRedactor` masks by field *name* (``password``, ``token``, ...).
- :class:`ValueRedactor` lifts a :class:`~servicewright.core.contracts.observability.Masker`
  over every string *value* - PII that no name list can catch.
- :class:`ChainRedactor` composes redactors so the two run together.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..contracts.observability import Masker, Redactor

logger = logging.getLogger(__name__)

DEFAULT_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "credentials",
        "csrf",
        "dsn",
        "password",
        "passwd",
        "private_key",
        "secret",
        "session",
        "set-cookie",
        "token",
    }
)

MASK = "[REDACTED]"


class KeyRedactor:
    """Masks values whose key contains a sensitive fragment (case-insensitive).

    The whole structure is walked — nested dicts **and** values inside lists and
    tuples. That matters because the payloads this redactor is threaded into are
    list-shaped where it counts: a Sentry event keeps stack-frame locals under
    ``exception.values[i].stacktrace.frames[j].vars`` and breadcrumb payloads
    under ``breadcrumbs.values[i].data``, so a redactor that only recursed into
    dicts would mask the flat ``extra`` block and ship the locals in plaintext.
    """

    def __init__(self, sensitive_keys: frozenset[str] | set[str] = DEFAULT_SENSITIVE_KEYS, mask: str = MASK) -> None:
        self._sensitive_keys = frozenset(key.lower() for key in sensitive_keys)
        self._mask = mask

    def __call__(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return a redacted copy of ``data``."""
        return self._redact_mapping(data, frozenset())

    def _redact_mapping(self, data: dict[str, Any], seen: frozenset[int]) -> dict[str, Any]:
        seen = seen | {id(data)}
        redacted: dict[str, Any] = {}
        for key, value in data.items():
            redacted[key] = self._mask if self._is_sensitive(key) else self._redact_value(value, seen)
        return redacted

    def _redact_value(self, value: Any, seen: frozenset[int]) -> Any:
        """Walk containers; leave scalars untouched. Cycles are left as-is."""
        if id(value) in seen:
            return value
        if isinstance(value, dict):
            return self._redact_mapping(value, seen)
        if isinstance(value, list):
            return [self._redact_value(item, seen | {id(value)}) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item, seen | {id(value)}) for item in value)
        return value

    def _is_sensitive(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        lowered = key.lower()
        return any(fragment in lowered for fragment in self._sensitive_keys)


class ValueRedactor:
    """Lifts a value-level :class:`Masker` over every string value in a payload.

    Same traversal as :class:`KeyRedactor` - nested dicts, lists and tuples,
    cycle-safe - but the decision is made by the masker looking at each string
    *value*, not by the field name. Keys are never masked.

    Fail closed: if the masker raises on a value, that value becomes the mask
    (never the raw string), and one warning is logged per redactor instance -
    a broken masker is visible without a log storm and without dropping a
    single log line or event.
    """

    def __init__(self, masker: Masker, mask: str = MASK) -> None:
        self._masker = masker
        self._mask = mask
        self._warned = False

    def __call__(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``data`` with every string value passed through the masker."""
        return self._redact_mapping(data, frozenset())

    def _redact_mapping(self, data: dict[str, Any], seen: frozenset[int]) -> dict[str, Any]:
        seen = seen | {id(data)}
        return {key: self._redact_value(value, seen) for key, value in data.items()}

    def _redact_value(self, value: Any, seen: frozenset[int]) -> Any:
        if isinstance(value, str):
            return self._mask_one(value)
        if id(value) in seen:
            return value
        if isinstance(value, dict):
            return self._redact_mapping(value, seen)
        if isinstance(value, list):
            return [self._redact_value(item, seen | {id(value)}) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact_value(item, seen | {id(value)}) for item in value)
        return value

    def _mask_one(self, value: str) -> str:
        try:
            return self._masker(value)
        except Exception:
            if not self._warned:
                self._warned = True
                logger.warning("value masker raised; emitting the mask instead of the value", exc_info=True)
            return self._mask


class ChainRedactor:
    """Applies redactors left to right: ``ChainRedactor(KeyRedactor(), ValueRedactor(m))``.

    Order matters and the conventional order is key-based first: sensitive
    fields are already collapsed to the mask before the (potentially more
    expensive) value masker sees the payload.
    """

    def __init__(self, *redactors: Redactor) -> None:
        self._redactors = redactors

    def __call__(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return ``data`` passed through every redactor in order."""
        for redactor in self._redactors:
            data = redactor(data)
        return data


__all__ = ["DEFAULT_SENSITIVE_KEYS", "MASK", "ChainRedactor", "KeyRedactor", "ValueRedactor"]
