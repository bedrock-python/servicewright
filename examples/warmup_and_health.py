"""Warmup priority groups and the health registry that gates readiness.

This example shows how to:

1. Write `AsyncWarmer`s and order them: lower `priority` runs first, warmers
   sharing a priority run in parallel, and the printed start/done offsets prove
   both -- peers start together, the next group waits.
2. Rely on fail-fast: one warmer with `raise_on_failure=True` aborts the run
   and later priority groups never execute, while a warmer constructed with
   `raise_on_failure=False` is allowed to fail without stopping anything.
3. See warmup finish BEFORE readiness flips true -- the Host primes
   infrastructure, then binds, then reports ready, so no traffic ever arrives
   at a cold pool.
4. Drive readiness from a `HealthRegistry`: it reports healthy only when the
   Host's `ready` flag AND every registered check pass, so a degraded
   dependency takes the pod out of rotation without killing the process.

Run with: `python examples/warmup_and_health.py`. It needs no infrastructure --
the warmers and checks are in-memory -- and exits 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from servicewright import (
    AppSpec,
    AsyncWarmer,
    DaemonEntrypoint,
    Service,
    WarmupError,
    perform_warmup,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from servicewright import HealthReport, UnitScopeProtocol

SERVICE_NAME = "catalog-service"
WARMUP_DELAY_SECONDS = 0.1


@dataclass(frozen=True)
class Settings:
    """All `BaseServiceSettingsProtocol` asks for; every observability section is off."""

    logging: object | None = None
    metrics: object | None = None
    tracing: object | None = None
    error_tracking: object | None = None

    def get_app_version(self) -> str:
        return "1.0.0"


class Stopwatch:
    """A shared clock: equal start offsets are what parallelism looks like."""

    def __init__(self) -> None:
        self._start = time.perf_counter()

    def restart(self) -> None:
        self._start = time.perf_counter()

    def ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000


class DemoWarmer(AsyncWarmer):
    """A warmer standing in for a real pool/producer/index primer."""

    def __init__(
        self,
        name: str,
        priority: int,
        stopwatch: Stopwatch,
        primed: list[str],
        *,
        fails: bool = False,
        raise_on_failure: bool = True,
    ) -> None:
        super().__init__(raise_on_failure=raise_on_failure)
        self._name = name
        self._priority = priority
        self._stopwatch = stopwatch
        self._primed = primed
        self._fails = fails

    @property
    def priority(self) -> int:
        return self._priority

    async def warmup(self) -> None:
        started = self._stopwatch.ms()
        await asyncio.sleep(WARMUP_DELAY_SECONDS)
        if self._fails:
            print(
                f"[warmup] p{self._priority:<3} {self._name:<15} start +{started:5.0f}ms  "
                f"FAILED (raise_on_failure={self.raise_on_failure})"
            )
            raise WarmupError(f"{self._name} is unreachable")
        self._primed.append(self._name)
        print(
            f"[warmup] p{self._priority:<3} {self._name:<15} start +{started:5.0f}ms  "
            f"done +{self._stopwatch.ms():5.0f}ms"
        )


class ToggleableCheck:
    """Satisfies `HealthCheckerProtocol` structurally: any `async check() -> bool`."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    async def check(self) -> bool:
        return self.healthy


async def idle_until_stop(_scope: UnitScopeProtocol, stop: asyncio.Event) -> None:
    """This service has no work of its own; it exists to hold the probes open."""
    await stop.wait()


class InMemoryScope:
    """An empty dependency scope -- this service resolves nothing."""

    async def get(self, dependency_key: type[Any] | str) -> Any:
        raise KeyError(dependency_key)


class InMemoryContainer:
    """The smallest object satisfying `DependencyContainerProtocol`."""

    @contextlib.asynccontextmanager
    async def app_scope(self) -> AsyncIterator[InMemoryScope]:
        yield InMemoryScope()

    @contextlib.asynccontextmanager
    async def unit_scope(self, context: Mapping[Any, Any] | None = None) -> AsyncIterator[InMemoryScope]:
        yield InMemoryScope()


def silence_library_logs() -> None:
    """Keep the narrative readable.

    The fail-fast section deliberately fails warmers, and the kernel reports
    that at WARNING with a traceback. A NullHandler on the library logger keeps
    stdlib logging's last-resort stderr handler out of the output.
    """
    library_logger = logging.getLogger("servicewright")
    library_logger.addHandler(logging.NullHandler())
    library_logger.propagate = False


def show_report(label: str, report: HealthReport) -> None:
    """Print one probe result the way `/system/health/readyz` would answer it."""
    print(f"[health] {label:<24} status={report.status.value:<9} checks={report.checks}")


async def demo_fail_fast() -> None:
    """Run a doomed warmup set directly through the orchestrator."""
    stopwatch = Stopwatch()
    primed: list[str] = []
    warmers = [
        DemoWarmer("config-cache", 0, stopwatch, primed),
        DemoWarmer("postgres-pool", 10, stopwatch, primed),
        DemoWarmer("redis-cache", 10, stopwatch, primed, fails=True, raise_on_failure=False),
        DemoWarmer("kafka-producer", 10, stopwatch, primed, fails=True),
        DemoWarmer("search-index", 20, stopwatch, primed),
    ]

    try:
        await perform_warmup(SERVICE_NAME, warmers)
    except WarmupError as exc:
        print(f"[warmup] perform_warmup raised {type(exc).__name__}: {exc}")

    print(f"[warmup] primed before the abort: {primed}")
    print("[warmup] 'redis-cache' failed but was optional; 'search-index' (p20) never ran at all")


async def main() -> None:
    silence_library_logs()

    print("--- warmup: priority groups, parallel peers, fail-fast ---")
    await demo_fail_fast()

    print("\n--- the real run: warmers are primed before readiness flips ---")
    stopwatch = Stopwatch()
    primed: list[str] = []
    postgres_check = ToggleableCheck()
    redis_check = ToggleableCheck()

    spec: AppSpec[Settings, InMemoryContainer] = AppSpec(
        service_name=SERVICE_NAME,
        create_container=lambda _settings: InMemoryContainer(),
    )
    spec.warmers.extend(
        [
            DemoWarmer("config-cache", 0, stopwatch, primed),
            DemoWarmer("postgres-pool", 10, stopwatch, primed),
            DemoWarmer("redis-cache", 10, stopwatch, primed),
            DemoWarmer("search-index", 20, stopwatch, primed),
        ]
    )
    spec.health.add_check("postgres", postgres_check)
    spec.health.add_check("redis", redis_check)

    # Both checks already pass, yet readiness is false: the Host has not flipped
    # the `ready` flag, so nothing should be routed here yet.
    show_report("before startup", await spec.health.readiness())

    service = Service(spec, entrypoints=[DaemonEntrypoint(idle_until_stop, kind="idle")])
    stop = asyncio.Event()

    async def probe_then_stop() -> None:
        while not spec.health.ready:
            await asyncio.sleep(0.01)
        print(f"[host]   ready flag flipped true; warmers already primed: {primed}")
        show_report("liveness", await spec.health.liveness())
        show_report("readiness", await spec.health.readiness())

        print("\n--- a dependency degrades ---")
        redis_check.healthy = False
        show_report("readiness (redis down)", await spec.health.readiness())
        print("[host]   the `ready` flag is still True: the process serves, the probe says do-not-route")

        print("\n--- shutdown ---")
        stop.set()

    stopwatch.restart()
    await asyncio.gather(service.run(Settings(), stop=stop), probe_then_stop())

    print(f"[host]   readiness flag after drain: {spec.health.ready}")
    show_report("after shutdown", await spec.health.readiness())


if __name__ == "__main__":
    asyncio.run(main())
