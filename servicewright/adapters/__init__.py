"""Concrete framework adapters (extra-gated implementation layer).

This package is side-effect-free: importing it pulls in no third-party
dependency and no sibling adapter. Import a specific adapter subpackage
(``servicewright.adapters.grpc``, ``...fastapi``, ``...apscheduler4``, ...) to use
it; each requires its matching extra.
"""

from __future__ import annotations

__all__: list[str] = []
