import asyncio
from typing import Any

from servicewright import AppSpec, Service
from servicewright.testing import FakeContainer, FakeEntrypoint, FakeSettings


async def test__service__run_end_to_end__executes_every_lifecycle_phase_in_order():
    """Run a Service end-to-end with a real Host, FakeContainer and entrypoints."""
    events: list[str] = []

    async def pre_start(app_scope: Any = None) -> None:
        events.append("pre_start")

    async def post_start(app_scope: Any = None) -> None:
        events.append("post_start")

    async def pre_shutdown(app_scope: Any = None) -> None:
        events.append("pre_shutdown")

    async def post_shutdown(app_scope: Any = None) -> None:
        events.append("post_shutdown")

    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="integration-service", create_container=lambda _settings: container)
    spec.lifecycle.add_pre_start_hook(pre_start)
    spec.lifecycle.add_post_start_hook(post_start)
    spec.lifecycle.add_pre_shutdown_hook(pre_shutdown)
    spec.lifecycle.add_post_shutdown_hook(post_shutdown)

    entrypoint = FakeEntrypoint(kind="http")
    service = Service(spec, entrypoints=[entrypoint])

    stop = asyncio.Event()

    async def run_then_stop() -> None:
        # Let startup complete, then signal shutdown.
        while not spec.health.ready:
            await asyncio.sleep(0)
        assert spec.health.ready is True
        stop.set()

    await asyncio.gather(service.run(FakeSettings(), stop=stop), run_then_stop())

    assert spec.health.ready is False
    assert events == ["pre_start", "post_start", "pre_shutdown", "post_shutdown"]
    assert entrypoint.events == ["bind", "serve", "drain", "stop"]
    assert container.app_scopes_opened == 1
