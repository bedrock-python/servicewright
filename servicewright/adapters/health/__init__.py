"""Concrete infrastructure health checks (extra-gated).

These are imported on demand so that importing this package never
requires the ``postgres`` or ``redis`` extras. Import the concrete check from its
submodule, which soft-imports its SDK and raises a friendly error if missing::

    from servicewright.adapters.health.postgres import PostgresHealthCheck
    from servicewright.adapters.health.redis import RedisHealthCheck

Each check satisfies :class:`~servicewright.core.contracts.HealthCheckerProtocol`
so it slots straight into ``HealthRegistry.add_check(name, check)``.
"""

from __future__ import annotations

__all__: list[str] = []
