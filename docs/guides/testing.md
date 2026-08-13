# Testing

`servicewright.testing` ships in-memory doubles with no dependencies, so most tests need no
infrastructure at all.

```python
from servicewright.testing import FakeContainer, FakeEntrypoint, FakeScope, FakeSettings
```

| Double | Stands in for | Records |
| --- | --- | --- |
| `FakeSettings` | your settings object | — (all observability sections off) |
| `FakeScope` | an app or unit scope | the `context` it was opened with |
| `FakeContainer` | your DI container | `app_scopes_opened`, `unit_scopes_opened`, `unit_contexts` |
| `FakeEntrypoint` | any entrypoint | `events` — the `bind → serve → drain → stop` order |

## Running a whole service

Pass your own `stop` event. That is the embedding path, and it means **no signal handlers are
installed** — vital in a test suite.

```python
import asyncio

from servicewright import AppSpec, Service
from servicewright.testing import FakeContainer, FakeEntrypoint, FakeSettings


async def test__service__stop_set__runs_the_full_lifecycle_in_order() -> None:
    entrypoint = FakeEntrypoint()
    spec = AppSpec(service_name="test", create_container=lambda settings: FakeContainer())
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(
        Service(spec, entrypoints=[entrypoint]).run(FakeSettings(), stop=stop),
        stop_soon(),
    )

    assert entrypoint.events == ["bind", "serve", "drain", "stop"]
```

`FakeEntrypoint(run_once=True)` returns immediately from `serve`, which models an essential
entrypoint exiting — useful for asserting that the service shuts itself down.

## Testing an HTTP app without a server

`build_app()` gives you the fully-configured FastAPI app — middleware stack, error handlers,
health routes — without binding a socket.

```python
import httpx
from fastapi import APIRouter

from servicewright import BootstrapContext, HealthRegistry, ServiceContext
from servicewright.adapters.fastapi import FastApiEntrypoint, UnitScopeDep
from servicewright.testing import FakeContainer, FakeScope, FakeSettings

router = APIRouter()


@router.get("/orders/{order_id}")
async def get_order(order_id: str, scope: UnitScopeDep) -> dict:
    use_case = await scope.get(GetOrder)
    return await use_case.execute(order_id)


def make_service_context(container: FakeContainer) -> ServiceContext:
    bootstrap = BootstrapContext(
        settings=FakeSettings(),
        service_name="test",
        container=container,
    )
    return ServiceContext(bootstrap=bootstrap, app_scope=FakeScope(), health=HealthRegistry())


async def test__get_order__exists__returns_it() -> None:
    container = FakeContainer(provides={GetOrder: FakeGetOrder()})
    app = await FastApiEntrypoint(routers=(router,)).build_app(make_service_context(container))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/orders/42")

    assert response.status_code == 200
    assert response.json() == {"id": "42"}
    assert response.headers["x-request-id"]        # the context middleware minted one
```

The same context object drives the probes, so readiness is testable directly:

```python
ctx = make_service_context(container)
app = await FastApiEntrypoint().build_app(ctx)
...
assert (await client.get("/system/health/readyz")).status_code == 503

ctx.health.ready = True
assert (await client.get("/system/health/readyz")).status_code == 200
```

## Asserting on DI scopes

`FakeContainer` records what happened, which is how you check that each unit of work really got
its own scope:

```python
assert container.unit_scopes_opened == 1
assert container.unit_contexts == [{"job_id": "sweep", "run_id": ANY}]
```

## Testing an entrypoint directly

No Host needed — drive the four methods:

```python
entrypoint = MyEntrypoint()
await entrypoint.bind(make_service_context(container))

stop = asyncio.Event()
task = asyncio.create_task(entrypoint.serve(stop=stop))
await something_to_happen()

stop.set()
await task
await entrypoint.drain(1.0)
await entrypoint.stop()
```

## Integration tests against a real port

Use `port=0` and read back what the OS picked:

```python
http = FastApiEntrypoint(config=HttpConfig(host="127.0.0.1", port=0))
await http.bind(ctx)

async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{http.bound_port}") as client:
    ...
```

No hardcoded ports means no flaky suite when two tests run in parallel.

## Testing your business logic

The point of the architecture is that most of your tests do not touch servicewright at all:

```python
async def test__get_order__missing__raises_not_found() -> None:
    use_case = GetOrder(FakeOrderRepository(orders={}))

    with pytest.raises(OrderNotFoundError) as exc_info:
        await use_case.execute("42")

    assert exc_info.value.kind is ErrorKind.NOT_FOUND
    assert exc_info.value.code == "order_not_found"
```

A `ServiceError` is a plain exception. Assert on `kind`, `code`, `public` and `params`, and let
the adapter tests cover the mapping to 404 and `NOT_FOUND`.

## pytest setup

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

With `asyncio_mode = "auto"` you can drop `@pytest.mark.asyncio` from every test.
