"""Self-contained configuration for the APScheduler 3.x :class:`SchedulerEntrypoint`.

Mirrors the ``apscheduler4`` adapter's public field set so a service migrates
3->4 by changing one import line. The only divergence is ``coalesce``, which is a
plain ``bool`` in APScheduler 3.x (vs the v4 ``CoalescePolicy`` enum).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...core.contracts import UnitScopeProtocol
    from ._imports import Trigger

# The user's job callable: ``func(scope, *args, **kwargs)``. The first parameter
# is the per-job DI scope minted by the entrypoint; ``args``/``kwargs`` follow.
ScheduledJobFunc = Callable[..., Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """Description of a single scheduled job (APScheduler 3.x).

    Attributes:
        id: Unique identifier for the job (also the APScheduler job id).
        func: Async callable invoked as ``func(scope, *args, **kwargs)`` where
            ``scope`` is the per-job :class:`UnitScopeProtocol`.
        trigger: APScheduler 3.x :class:`~apscheduler.triggers.base.BaseTrigger`.
        args: Positional arguments passed after ``scope``.
        kwargs: Keyword arguments passed to ``func``.
        max_instances: Maximum number of concurrent running instances for this
            job (maps to APScheduler 3.x ``add_job(max_instances=...)``).
        misfire_grace_time: Seconds after which a misfired run is skipped.
        coalesce: Whether missed runs are collapsed into one (APScheduler 3.x
            ``add_job(coalesce=...)``, a plain ``bool``).
    """

    id: str
    func: Callable[[UnitScopeProtocol], Awaitable[None]]
    trigger: Trigger
    args: Sequence[Any] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    max_instances: int | None = None
    misfire_grace_time: float | None = None
    coalesce: bool | None = None


__all__ = [
    "ScheduledJob",
    "ScheduledJobFunc",
]
