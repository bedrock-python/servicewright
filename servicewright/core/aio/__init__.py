"""The loop-owning kernel: the :class:`Host` run-loop.

Reserved (per servicewright's aio convention) for the single process-loop owner
that has no sync twin. Every other core domain that mixes sync and async members
co-locates its async half as ``<domain>/aio.py`` instead of nesting here.
"""

from __future__ import annotations

from .host import Host as Host

__all__ = ["Host"]
