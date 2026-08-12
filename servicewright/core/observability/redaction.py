"""Built-in sensitive-data redactor (pure stdlib).

A minimal :class:`~servicewright.core.contracts.observability.Redactor`
implementation the manager threads into BOTH the logging and error-tracking
sinks. Any callable ``dict -> dict`` works in its place.
"""

from __future__ import annotations

from typing import Any

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


__all__ = ["DEFAULT_SENSITIVE_KEYS", "MASK", "KeyRedactor"]
