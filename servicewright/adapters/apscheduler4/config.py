"""Self-contained configuration for :class:`SchedulerEntrypoint`.

The :class:`AppSpec` stays transport-neutral, so the scheduler entrypoint owns
its own job descriptions. ``ScheduledJob`` is a faithful fold of the prototype's
``AsyncWorkerSchedule`` onto the Host + Entrypoints model: the job callable is
**scope-first** (it receives the per-job :class:`UnitScopeProtocol` as its first
argument) so a scheduled job structurally equals an HTTP request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...core.contracts import UnitScopeProtocol
    from ._imports import CoalescePolicy, Trigger

# The user's job callable: ``func(scope, *args, **kwargs)``. The first parameter
# is the per-job DI scope minted by the entrypoint; ``args``/``kwargs`` follow.
ScheduledJobFunc = Callable[..., Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """Description of a single scheduled job.

    Attributes:
        id: Unique identifier for the job (also the APScheduler schedule id).
        func: Async callable invoked as ``func(scope, *args, **kwargs)`` where
            ``scope`` is the per-job :class:`UnitScopeProtocol`.
        trigger: APScheduler :class:`~apscheduler.abc.Trigger` driving the job.
        args: Positional arguments passed after ``scope``.
        kwargs: Keyword arguments passed to ``func``.
        max_instances: Maximum number of concurrent running jobs for this id
            (maps to APScheduler v4 ``configure_task(max_running_jobs=...)``).
        misfire_grace_time: Seconds after which a misfired run is skipped.
        coalesce: APScheduler v4 :class:`~apscheduler.CoalescePolicy` controlling
            how missed runs are collapsed.
    """

    id: str
    func: Callable[[UnitScopeProtocol], Awaitable[None]]
    trigger: Trigger
    args: Sequence[Any] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    max_instances: int | None = None
    misfire_grace_time: float | None = None
    coalesce: CoalescePolicy | None = None


__all__ = [
    "ScheduledJob",
    "ScheduledJobFunc",
]
