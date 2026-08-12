"""Unit tests for the transport-agnostic health registry."""

from __future__ import annotations

import asyncio

import pytest
from pytest_lazy_fixtures import lf

from servicewright.core.health import HealthRegistry, HealthReport, ProbeStatus

pytestmark = pytest.mark.unit


class _Check:
    """Health check with a fixed verdict that counts its invocations."""

    def __init__(self, result: bool) -> None:
        self._result = result
        self.calls = 0

    async def check(self) -> bool:
        self.calls += 1
        return self._result


class _RaisingCheck:
    """Health check that blows up instead of answering."""

    async def check(self) -> bool:
        raise RuntimeError("check boom")


class _CancellingCheck:
    """Health check that is cancelled mid-flight."""

    async def check(self) -> bool:
        raise asyncio.CancelledError


@pytest.fixture
def registry() -> HealthRegistry:
    return HealthRegistry()


@pytest.fixture
def ready_registry() -> HealthRegistry:
    registry = HealthRegistry()
    registry.ready = True
    return registry


@pytest.fixture
def passing_check() -> _Check:
    return _Check(True)


@pytest.fixture
def failing_check() -> _Check:
    return _Check(False)


@pytest.mark.parametrize(
    ("healthy", "expected"),
    [
        pytest.param(True, ProbeStatus.HEALTHY, id="healthy"),
        pytest.param(False, ProbeStatus.UNHEALTHY, id="unhealthy"),
    ],
)
def test__probe_status_from_bool__health_flag__maps_to_the_status(healthy: bool, expected: ProbeStatus) -> None:
    # Act & Assert
    assert ProbeStatus.from_bool(healthy) is expected


@pytest.mark.parametrize(
    ("healthy", "expected"),
    [
        pytest.param(True, ProbeStatus.HEALTHY, id="healthy"),
        pytest.param(False, ProbeStatus.UNHEALTHY, id="unhealthy"),
    ],
)
def test__health_report__built_from_a_flag__derives_its_status(healthy: bool, expected: ProbeStatus) -> None:
    # Act & Assert
    assert HealthReport(healthy=healthy).status is expected


async def test__liveness__a_check_is_failing__stays_healthy(
    registry: HealthRegistry,
    failing_check: _Check,
) -> None:
    # Arrange
    registry.add_check("db", failing_check)

    # Act
    report = await registry.liveness()

    # Assert
    # Liveness only says the process is up; it never consults the checks.
    assert report.healthy is True
    assert report.checks == {}


async def test__readiness__ready_flag_not_set__is_unhealthy_but_still_reports_the_checks(
    registry: HealthRegistry,
    passing_check: _Check,
) -> None:
    # Arrange
    registry.add_check("db", passing_check)

    # Act
    report = await registry.readiness()

    # Assert
    assert report.healthy is False
    assert report.checks == {"db": True}


async def test__readiness__ready_flag_set_and_checks_pass__is_healthy(
    ready_registry: HealthRegistry,
    passing_check: _Check,
) -> None:
    # Arrange
    ready_registry.add_check("db", passing_check)

    # Act
    report = await ready_registry.readiness()

    # Assert
    assert report.healthy is True


async def test__readiness__no_checks_registered__follows_the_ready_flag(ready_registry: HealthRegistry) -> None:
    # Act
    report = await ready_registry.readiness()

    # Assert
    assert report.healthy is True
    assert report.checks == {}


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(lf("failing_check"), id="check-returns-false"),
        pytest.param(_RaisingCheck(), id="check-raises"),
    ],
)
async def test__readiness__a_check_does_not_pass__is_unhealthy(
    ready_registry: HealthRegistry,
    check: object,
) -> None:
    # Arrange
    ready_registry.add_check("db", check)  # type: ignore[arg-type]

    # Act
    report = await ready_registry.readiness()

    # Assert
    assert report.healthy is False
    assert report.checks == {"db": False}


async def test__readiness__several_checks__runs_them_concurrently(ready_registry: HealthRegistry) -> None:
    # Arrange
    started = 0
    release = asyncio.Event()

    class _Concurrent:
        async def check(self) -> bool:
            nonlocal started
            started += 1
            if started < 2:
                # Only completes once the sibling has started too.
                await asyncio.wait_for(release.wait(), timeout=1)
            else:
                release.set()
            return True

    ready_registry.add_check("a", _Concurrent())
    ready_registry.add_check("b", _Concurrent())

    # Act
    report = await ready_registry.readiness()

    # Assert
    assert report.healthy is True
    assert started == 2


async def test__readiness__a_check_is_cancelled__propagates_the_cancellation(
    ready_registry: HealthRegistry,
) -> None:
    # Arrange
    ready_registry.add_check("db", _CancellingCheck())

    # Act & Assert
    with pytest.raises(asyncio.CancelledError):
        await ready_registry.readiness()


def test__add_check__name_already_registered__raises(registry: HealthRegistry, passing_check: _Check) -> None:
    # Arrange
    registry.add_check("db", passing_check)

    # Act & Assert
    with pytest.raises(ValueError, match="already registered"):
        registry.add_check("db", _Check(True))


def test__checks_property__mutated_by_the_caller__does_not_affect_the_registry(
    registry: HealthRegistry,
    passing_check: _Check,
) -> None:
    # Arrange
    registry.add_check("db", passing_check)

    # Act
    snapshot = registry.checks
    snapshot["other"] = _Check(True)

    # Assert
    assert "other" not in registry.checks


async def test__readiness__within_the_cache_ttl__does_not_re_run_the_checks(passing_check: _Check) -> None:
    # Arrange
    registry = HealthRegistry(readiness_cache_ttl=60.0)
    registry.ready = True
    registry.add_check("db", passing_check)

    # Act
    await registry.readiness()
    await registry.readiness()

    # Assert
    assert passing_check.calls == 1


async def test__readiness__a_check_was_added__invalidates_the_cache(passing_check: _Check) -> None:
    # Arrange
    registry = HealthRegistry(readiness_cache_ttl=60.0)
    registry.ready = True
    registry.add_check("db", passing_check)
    await registry.readiness()

    # Act
    registry.add_check("db2", _Check(True))
    await registry.readiness()

    # Assert
    assert passing_check.calls == 2
