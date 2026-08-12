"""Unit tests for the Host kernel: lifecycle ordering, failure propagation, budgets."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

from servicewright import AppSpec, CleanupTimeoutError, DrainTimeoutError, Host
from servicewright.core.contracts import AsyncWarmer, LifecycleHookProtocol, ScopedEntrypoint
from servicewright.core.spec import BootstrapContext
from servicewright.testing import FakeContainer, FakeEntrypoint, FakeSettings

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.unit

# Drain/cleanup budget used by the timeout tests; any hang is far longer.
FAST_BUDGET = 0.02


@pytest.fixture
def container() -> FakeContainer:
    return FakeContainer()


@pytest.fixture
def observability() -> MagicMock:
    return MagicMock()


@pytest.fixture
def spec(container: FakeContainer) -> AppSpec[Any, Any]:
    return AppSpec(service_name="svc", create_container=lambda _s: container)


@pytest.fixture
def host(spec: AppSpec[Any, Any]) -> Host[Any, Any]:
    return Host(spec)


@pytest.fixture
def stop() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def stop_when_ready(spec: AppSpec[Any, Any], stop: asyncio.Event) -> Callable[[], asyncio.Future[None]]:
    """Return a driver coroutine factory that stops the host once it is ready."""

    async def driver() -> None:
        while not spec.health.ready:
            await asyncio.sleep(0)
        stop.set()

    return lambda: asyncio.ensure_future(driver())


class _Failing(ScopedEntrypoint):
    """Essential entrypoint whose serve always raises."""

    kind = "http"
    essential = True

    def __init__(self, message: str = "essential boom") -> None:
        super().__init__()
        self._message = message

    async def serve(self, *, stop: asyncio.Event) -> None:
        # Yield once so siblings are actually awaiting when the group aborts.
        await asyncio.sleep(0)
        raise ValueError(self._message)


class _Recording(ScopedEntrypoint):
    """Entrypoint recording its lifecycle calls, with injectable behaviour."""

    def __init__(
        self,
        *,
        kind: str = "rec",
        essential: bool = True,
        serve_error: Exception | None = None,
        drain_error: BaseException | None = None,
        stop_error: BaseException | None = None,
        drain_delay: float = 0.0,
        stop_delay: float = 0.0,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.essential = essential
        self.events: list[str] = []
        self._serve_error = serve_error
        self._drain_error = drain_error
        self._stop_error = stop_error
        self._drain_delay = drain_delay
        self._stop_delay = stop_delay

    async def bind(self, ctx: Any) -> None:
        await super().bind(ctx)
        self.events.append("bind")

    async def serve(self, *, stop: asyncio.Event) -> None:
        self.events.append("serve")
        if self._serve_error is not None:
            raise self._serve_error
        await stop.wait()

    async def drain(self, grace: float) -> None:
        self.events.append("drain")
        if self._drain_error is not None:
            raise self._drain_error
        if self._drain_delay:
            await asyncio.sleep(self._drain_delay)

    async def stop(self) -> None:
        self.events.append("stop")
        if self._stop_error is not None:
            raise self._stop_error
        if self._stop_delay:
            await asyncio.sleep(self._stop_delay)


def _warmer(*, priority: int = 0, raise_on_failure: bool = True, error: Exception | None = None) -> AsyncWarmer:
    class _Warmer:
        def __init__(self) -> None:
            self.priority = priority
            self.raise_on_failure = raise_on_failure
            self.warmed = False

        async def warmup(self) -> None:
            if error is not None:
                raise error
            self.warmed = True

    return cast(AsyncWarmer, _Warmer())


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #
def test__host_bootstrap__spec_with_container_factory__returns_bootstrap_context(
    host: Host[Any, Any],
    container: FakeContainer,
) -> None:
    # Act
    ctx = host.bootstrap(FakeSettings())

    # Assert
    assert isinstance(ctx, BootstrapContext)
    assert ctx.container is container
    assert ctx.service_name == "svc"


def test__host_add_entrypoint__called__appends_to_the_run_list(host: Host[Any, Any]) -> None:
    # Arrange
    entrypoint = FakeEntrypoint()

    # Act
    host.add_entrypoint(entrypoint)

    # Assert
    assert host._entrypoints == [entrypoint]


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
async def test__host_run__happy_path__orders_hooks_warmup_and_entrypoint_phases(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
    observability: MagicMock,
    container: FakeContainer,
    stop_when_ready: Callable[[], asyncio.Future[None]],
) -> None:
    # Arrange
    spec.observability = observability
    order: list[str] = []

    def _hook(name: str) -> LifecycleHookProtocol:
        async def hook(app_scope: Any = None) -> None:
            order.append(name)

        return hook

    spec.lifecycle.add_pre_start_hook(_hook("pre_start"))
    spec.lifecycle.add_post_start_hook(_hook("post_start"))
    spec.lifecycle.add_pre_shutdown_hook(_hook("pre_shutdown"))
    spec.lifecycle.add_post_shutdown_hook(_hook("post_shutdown"))
    warmer = _warmer()
    spec.warmers.append(warmer)
    entrypoint = FakeEntrypoint(kind="http")

    # Act
    await asyncio.gather(host.run(FakeSettings(), [entrypoint], stop=stop), stop_when_ready())

    # Assert
    assert order == ["pre_start", "post_start", "pre_shutdown", "post_shutdown"]
    assert entrypoint.events == ["bind", "serve", "drain", "stop"]
    assert warmer.warmed is True  # type: ignore[attr-defined]
    assert spec.health.ready is False
    observability.configure.assert_called_once()
    observability.shutdown.assert_called_once()
    assert container.app_scopes_opened == 1


async def test__host_run__warmer_registered__primes_it_before_readiness_flips(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
    stop_when_ready: Callable[[], asyncio.Future[None]],
) -> None:
    # Arrange
    ready_during_warmup: list[bool] = []

    class _ReadinessProbe:
        priority = 0
        raise_on_failure = True

        async def warmup(self) -> None:
            ready_during_warmup.append(spec.health.ready)

    spec.warmers.append(cast(AsyncWarmer, _ReadinessProbe()))

    # Act
    await asyncio.gather(host.run(FakeSettings(), [FakeEntrypoint()], stop=stop), stop_when_ready())

    # Assert
    assert ready_during_warmup == [False]


async def test__host_run__no_entrypoints__blocks_until_stop_is_set(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
    stop_when_ready: Callable[[], asyncio.Future[None]],
) -> None:
    # Act
    await asyncio.gather(host.run(FakeSettings(), [], stop=stop), stop_when_ready())

    # Assert
    assert spec.health.ready is False


async def test__host_run__essential_entrypoint_returns__stops_the_remaining_ones(
    host: Host[Any, Any],
    stop: asyncio.Event,
) -> None:
    # Arrange
    one_shot = FakeEntrypoint(kind="oneshot", essential=True, run_once=True)
    daemon = FakeEntrypoint(kind="daemon", essential=False)

    # Act
    await host.run(FakeSettings(), [one_shot, daemon], stop=stop)

    # Assert
    assert stop.is_set()
    assert daemon.events == ["bind", "serve", "drain", "stop"]


async def test__host_run__plugin_registers_entrypoint_warmer_and_check__all_take_effect(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
) -> None:
    # Arrange
    entrypoint = FakeEntrypoint(kind="plugin-ep", run_once=True)
    warmer = _warmer()

    class _Check:
        async def check(self) -> bool:
            return True

    class _Plugin:
        def on_register(self, app_spec: AppSpec[Any, Any], target: Host[Any, Any]) -> None:
            target.add_entrypoint(entrypoint)
            app_spec.warmers.append(warmer)
            app_spec.health.add_check("mycheck", _Check())

    # Act
    await host.run(FakeSettings(), [], plugins=[_Plugin()], stop=stop)

    # Assert
    assert entrypoint.events == ["bind", "serve", "drain", "stop"]
    assert warmer in spec.warmers
    assert "mycheck" in spec.health.checks


# --------------------------------------------------------------------------- #
# Failure propagation — a crash may never look like a graceful stop
# --------------------------------------------------------------------------- #
async def test__host_run__essential_entrypoint_crashes__raises_the_original_exception(
    host: Host[Any, Any],
    stop: asyncio.Event,
) -> None:
    # Act & Assert
    with pytest.raises(ValueError, match="essential boom"):
        await host.run(FakeSettings(), [_Failing()], stop=stop)


async def test__host_run__essential_entrypoint_crashes__still_runs_the_whole_cleanup(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
    observability: MagicMock,
) -> None:
    # Arrange
    spec.observability = observability
    post_shutdown_ran = False

    async def post_shutdown(app_scope: Any = None) -> None:
        nonlocal post_shutdown_ran
        post_shutdown_ran = True

    spec.lifecycle.add_post_shutdown_hook(post_shutdown)
    sibling = FakeEntrypoint(kind="daemon", essential=False)

    # Act
    with pytest.raises(ValueError, match="essential boom"):
        await host.run(FakeSettings(), [_Failing(), sibling], stop=stop)

    # Assert
    assert stop.is_set()
    assert spec.health.ready is False
    assert sibling.events == ["bind", "serve", "drain", "stop"]
    observability.shutdown.assert_called_once()
    assert post_shutdown_ran is True


async def test__host_run__two_essential_entrypoints_crash__raises_an_exception_group(
    host: Host[Any, Any],
    stop: asyncio.Event,
) -> None:
    # Act & Assert
    with pytest.raises(BaseExceptionGroup) as exc_info:
        await host.run(FakeSettings(), [_Failing("first boom"), _Failing("second boom")], stop=stop)

    assert len(exc_info.value.exceptions) == 2


async def test__host_run__essential_entrypoint_crashes__cancels_a_busy_sibling(
    host: Host[Any, Any],
    stop: asyncio.Event,
) -> None:
    # Arrange
    cancelled = False

    class _Busy(ScopedEntrypoint):
        kind = "busy"
        essential = False

        async def serve(self, *, stop: asyncio.Event) -> None:
            nonlocal cancelled
            try:
                # Never watches `stop`, so only the group can end it.
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled = True
                raise

    # Act
    with pytest.raises(ValueError, match="essential boom"):
        await host.run(FakeSettings(), [_Failing(), _Busy()], stop=stop)

    # Assert
    assert cancelled is True


async def test__host_run__non_essential_entrypoint_crashes__keeps_the_others_serving(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
    stop_when_ready: Callable[[], asyncio.Future[None]],
) -> None:
    # Arrange
    failing = _Recording(kind="broken", essential=False, serve_error=ValueError("non-essential boom"))
    healthy = FakeEntrypoint(kind="http", essential=True)

    # Act
    await asyncio.gather(host.run(FakeSettings(), [failing, healthy], stop=stop), stop_when_ready())

    # Assert
    assert failing.events == ["bind", "serve", "drain", "stop"]
    assert healthy.events == ["bind", "serve", "drain", "stop"]


async def test__host_run__warmup_fails__propagates_and_still_runs_cleanup(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
    observability: MagicMock,
) -> None:
    # Arrange
    spec.observability = observability
    spec.warmers.append(_warmer(error=ValueError("warmup boom")))
    entrypoint = FakeEntrypoint()

    # Act
    with pytest.raises(Exception, match="warmup"):
        await host.run(FakeSettings(), [entrypoint], stop=stop)

    # Assert
    assert entrypoint.events == [], "an entrypoint that never bound must not be torn down"
    assert spec.health.ready is False
    observability.shutdown.assert_called_once()


async def test__host_run__bind_fails__still_drains_and_stops_that_entrypoint(
    host: Host[Any, Any],
    stop: asyncio.Event,
) -> None:
    # Arrange
    class _BadBind(_Recording):
        async def bind(self, ctx: Any) -> None:
            self.events.append("bind")
            raise ValueError("bind boom")

    bad = _BadBind(kind="bad")
    never_reached = _Recording(kind="later")

    # Act
    with pytest.raises(ValueError, match="bind boom"):
        await host.run(FakeSettings(), [bad, never_reached], stop=stop)

    # Assert
    assert bad.events == ["bind", "drain", "stop"], "a half-bound entrypoint must be cleaned up"
    assert never_reached.events == []


# --------------------------------------------------------------------------- #
# Stop-awareness during startup
# --------------------------------------------------------------------------- #
async def test__host_run__stop_already_set__never_binds_and_never_reports_ready(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
) -> None:
    # Arrange
    entrypoint = FakeEntrypoint()
    ready_flips: list[bool] = []
    spec.lifecycle.add_post_start_hook(lambda app_scope=None: ready_flips.append(spec.health.ready))  # type: ignore[arg-type,func-returns-value]
    stop.set()

    # Act
    await host.run(FakeSettings(), [entrypoint], stop=stop)

    # Assert
    assert entrypoint.events == []
    assert ready_flips == []
    assert spec.health.ready is False


async def test__host_run__stop_arrives_during_warmup__abandons_warmup_and_skips_serve(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
) -> None:
    # Arrange
    warmup_completed = False

    class _SlowWarmer:
        priority = 0
        raise_on_failure = True

        async def warmup(self) -> None:
            nonlocal warmup_completed
            stop.set()
            await asyncio.sleep(30)
            warmup_completed = True

    spec.warmers.append(cast(AsyncWarmer, _SlowWarmer()))
    entrypoint = FakeEntrypoint()

    # Act
    await asyncio.wait_for(host.run(FakeSettings(), [entrypoint], stop=stop), timeout=5)

    # Assert
    assert warmup_completed is False
    assert entrypoint.events == []
    assert spec.health.ready is False


# --------------------------------------------------------------------------- #
# Shutdown budgets
# --------------------------------------------------------------------------- #
async def test__host_run__drain_exceeds_its_grace__raises_drain_timeout_error(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
    stop_when_ready: Callable[[], asyncio.Future[None]],
) -> None:
    # Arrange
    spec.drain_grace_seconds = FAST_BUDGET
    slow = _Recording(kind="http", drain_delay=30)

    # Act & Assert
    with pytest.raises(DrainTimeoutError):
        await asyncio.gather(host.run(FakeSettings(), [slow], stop=stop), stop_when_ready())

    assert spec.health.ready is False


async def test__host_run__stop_exceeds_the_cleanup_budget__raises_cleanup_timeout_error(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
    stop_when_ready: Callable[[], asyncio.Future[None]],
) -> None:
    # Arrange
    spec.cleanup_timeout_seconds = FAST_BUDGET
    slow = _Recording(kind="http", stop_delay=30)

    # Act & Assert
    with pytest.raises(CleanupTimeoutError):
        await asyncio.gather(host.run(FakeSettings(), [slow], stop=stop), stop_when_ready())


async def test__host_run__one_entrypoint_hangs_in_stop__the_others_are_still_stopped(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
    stop_when_ready: Callable[[], asyncio.Future[None]],
) -> None:
    # Arrange
    spec.cleanup_timeout_seconds = FAST_BUDGET
    hanging = _Recording(kind="hangs", stop_delay=30)
    healthy = _Recording(kind="healthy", essential=False)

    # Act
    with pytest.raises(CleanupTimeoutError):
        await asyncio.gather(host.run(FakeSettings(), [hanging, healthy], stop=stop), stop_when_ready())

    # Assert
    assert healthy.events == ["bind", "serve", "drain", "stop"]


async def test__host_run__serve_crashes_and_cleanup_times_out__reports_the_serve_failure(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
) -> None:
    # Arrange
    spec.cleanup_timeout_seconds = FAST_BUDGET

    class _FailingWithSlowStop(_Failing):
        async def stop(self) -> None:
            await asyncio.sleep(30)

    # Act & Assert
    # A timed-out cleanup must never mask the failure that caused the shutdown.
    with pytest.raises(ValueError, match="essential boom"):
        await host.run(FakeSettings(), [_FailingWithSlowStop()], stop=stop)


@pytest.mark.parametrize(
    "entrypoint",
    [
        pytest.param(_Recording(kind="bad-drain", drain_error=ValueError("drain boom")), id="drain-raises"),
        pytest.param(_Recording(kind="bad-stop", stop_error=ValueError("stop boom")), id="stop-raises"),
    ],
)
async def test__host_run__shutdown_step_raises__is_logged_and_the_run_completes(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    stop: asyncio.Event,
    stop_when_ready: Callable[[], asyncio.Future[None]],
    entrypoint: _Recording,
) -> None:
    # Act
    await asyncio.gather(host.run(FakeSettings(), [entrypoint], stop=stop), stop_when_ready())

    # Assert
    assert spec.health.ready is False


@pytest.mark.parametrize(
    "entrypoint",
    [
        pytest.param(_Recording(kind="cancel-drain", drain_error=asyncio.CancelledError()), id="drain-cancelled"),
        pytest.param(_Recording(kind="cancel-stop", stop_error=asyncio.CancelledError()), id="stop-cancelled"),
    ],
)
async def test__host_run__shutdown_step_is_cancelled__propagates_the_cancellation(
    host: Host[Any, Any],
    stop: asyncio.Event,
    stop_when_ready: Callable[[], asyncio.Future[None]],
    entrypoint: _Recording,
) -> None:
    # Act & Assert
    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(host.run(FakeSettings(), [entrypoint], stop=stop), stop_when_ready())


# --------------------------------------------------------------------------- #
# Signal wiring
# --------------------------------------------------------------------------- #
async def test__host_run__no_stop_event_supplied__installs_and_uninstalls_signal_handlers(
    spec: AppSpec[Any, Any],
    host: Host[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    installed: list[asyncio.Event] = []
    uninstalled: list[bool] = []

    def fake_install(stop_event: asyncio.Event) -> Callable[[], None]:
        installed.append(stop_event)
        stop_event.set()  # Immediately request shutdown so run() returns.
        return lambda: uninstalled.append(True)

    monkeypatch.setattr("servicewright.core.aio.host.install_signal_handlers", fake_install)

    # Act
    await host.run(FakeSettings(), [])

    # Assert
    assert len(installed) == 1
    assert uninstalled == [True], "handlers must not outlive the run-loop"
    assert spec.health.ready is False


async def test__host_run__external_stop_event_supplied__does_not_install_signal_handlers(
    host: Host[Any, Any],
    stop: asyncio.Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    installs: list[asyncio.Event] = []

    def fake_install(stop_event: asyncio.Event) -> Callable[[], None]:
        installs.append(stop_event)
        return lambda: None

    monkeypatch.setattr("servicewright.core.aio.host.install_signal_handlers", fake_install)
    stop.set()

    # Act
    await host.run(FakeSettings(), [], stop=stop)

    # Assert
    assert installs == []
