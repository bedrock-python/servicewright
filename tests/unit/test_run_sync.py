"""``run_sync`` and ``event_loop_factory``: the process entry point picks the event loop."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import pytest

from servicewright import AppSpec, Service, event_loop_factory, run_sync
from servicewright.testing import FakeContainer, FakeEntrypoint, FakeSettings

pytestmark = pytest.mark.unit


class _LoopProbe(FakeEntrypoint):
    """Serves once and records which loop implementation it ran on."""

    def __init__(self) -> None:
        super().__init__(run_once=True)
        self.loop_module: str | None = None

    async def serve(self, *, stop: asyncio.Event) -> None:
        self.loop_module = type(asyncio.get_running_loop()).__module__
        await super().serve(stop=stop)


class _Crashing(FakeEntrypoint):
    async def serve(self, *, stop: asyncio.Event) -> None:
        raise RuntimeError("listener died")


def _service(entrypoint: FakeEntrypoint) -> Service[Any, Any]:
    spec: AppSpec[Any, Any] = AppSpec(service_name="svc", create_container=lambda _s: FakeContainer())
    return Service(spec, entrypoints=[entrypoint])


# --------------------------------------------------------------------------- #
# event_loop_factory
# --------------------------------------------------------------------------- #
def test__event_loop_factory__asyncio__returns_none() -> None:
    assert event_loop_factory("asyncio") is None


@pytest.mark.parametrize("loop", ["uvloop", "auto"])
def test__event_loop_factory__uvloop_installed__returns_uvloops_factory(loop: str) -> None:
    uvloop = pytest.importorskip("uvloop")

    assert event_loop_factory(loop) is uvloop.new_event_loop  # type: ignore[arg-type]


def test__event_loop_factory__auto_without_uvloop__falls_back_to_asyncio() -> None:
    with patch.dict("sys.modules", {"uvloop": None}):
        assert event_loop_factory("auto") is None


def test__event_loop_factory__uvloop_without_uvloop__raises_with_the_install_hint() -> None:
    with patch.dict("sys.modules", {"uvloop": None}), pytest.raises(ImportError, match=r"servicewright\[uvloop\]"):
        event_loop_factory("uvloop")


def test__event_loop_factory__unknown_name__raises() -> None:
    with pytest.raises(ValueError, match="Unknown event loop 'trio'"):
        event_loop_factory("trio")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# run_sync
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("loop", "expected_module"),
    [("uvloop", "uvloop"), ("auto", "uvloop"), ("asyncio", "asyncio")],
)
def test__run_sync__loop_selected__runs_the_service_on_that_loop(loop: str, expected_module: str) -> None:
    # Arrange
    if expected_module == "uvloop":
        pytest.importorskip("uvloop")
    probe = _LoopProbe()

    # Act
    run_sync(_service(probe), FakeSettings(), loop=loop)  # type: ignore[arg-type]

    # Assert
    assert probe.loop_module is not None
    assert probe.loop_module.split(".")[0] == expected_module
    assert probe.events == ["bind", "serve", "drain", "stop"]


def test__service_run_sync__called__is_the_module_level_entry_point() -> None:
    # Arrange
    probe = _LoopProbe()

    # Act
    _service(probe).run_sync(FakeSettings(), loop="asyncio")

    # Assert
    assert probe.events == ["bind", "serve", "drain", "stop"]


def test__run_sync__essential_entrypoint_raises__propagates_after_cleanup() -> None:
    # Arrange
    crashing = _Crashing(run_once=True)

    # Act / Assert
    with pytest.raises(RuntimeError, match="listener died"):
        run_sync(_service(crashing), FakeSettings(), loop="asyncio")
    assert crashing.events == ["bind", "drain", "stop"]


def test__host__service_ready__logs_the_event_loop_implementation(caplog: pytest.LogCaptureFixture) -> None:
    # Arrange
    probe = _LoopProbe()

    # Act
    with caplog.at_level(logging.INFO, logger="servicewright.core.aio.host"):
        run_sync(_service(probe), FakeSettings(), loop="asyncio")

    # Assert
    ready = next(record for record in caplog.records if record.message == "Service ready")
    assert getattr(ready, "event_loop", "").startswith("asyncio.")
