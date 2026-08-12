"""Metric naming discipline (pure stdlib, backend- and transport-neutral)."""

from __future__ import annotations

import re

_PREFIX_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def make_metric_name(name: str, prefix: str | None = None) -> str:
    """Combine ``prefix`` and ``name`` per metric identifier conventions.

    Raises:
        ValueError: If the prefix is not a valid metric name fragment.
    """
    if prefix:
        if not _PREFIX_PATTERN.match(prefix):
            raise ValueError(
                f"Invalid metric prefix: {prefix!r}. Prefixes must start with a letter or "
                "underscore and contain only letters, numbers, or underscores."
            )
        return f"{prefix}_{name}"
    return name


__all__ = ["make_metric_name"]
