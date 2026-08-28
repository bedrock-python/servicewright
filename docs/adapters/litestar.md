# Litestar

```bash
pip install "servicewright[litestar]"
```

A lean HTTP entrypoint for [Litestar](https://litestar.dev/). It gives you the lifecycle, the
per-request DI scope and the health probes, and then gets out of the way.

```python
from litestar import get

from servicewright import Service
from servicewright.adapters.litestar import LitestarConfig, LitestarEntrypoint


@get("/orders/{order_id:str}")
async def get_order(order_id: str) -> dict:
    return {"id": order_id}


http = LitestarEntrypoint(config=LitestarConfig(port=8000), route_handlers=(get_order,))
service = Service(spec, entrypoints=[http])
```

!!! info "Deliberately smaller than the FastAPI adapter"

    This binding carries **no** platform middleware stack, no correlation-id handling, no error
    renderer and no Sentry integration. Those are folds of a FastAPI-specific service runtime, and
    reproducing them here would mean guessing at Litestar idioms.

    What you get is a clean, general Litestar entrypoint. Litestar's own middleware, exception
    handlers, DTOs and plugins work exactly as documented upstream.

    If you want the batteries, use the [FastAPI adapter](fastapi.md).

## `LitestarConfig`

| Field | Default | Meaning |
| --- | --- | --- |
| `host` | `"0.0.0.0"` | Bind host |
| `port` | `8000` | Bind port (`0` picks a free one) |
| `graceful_timeout` | `10.0` | uvicorn's own shutdown timeout |
| `litestar_kwargs` | `{}` | Forwarded to `Litestar(...)` |
| `uvicorn_kwargs` | `{}` | Forwarded to `uvicorn.Config(...)` |
| `health` | `HealthConfig()` | `enabled`, `liveness_path`, `readiness_path` |
| `unit_scope` | `True` | Open a unit scope per request and provide the `unit_scope` dependency; `False` hands the request scope to your DI integration ([below](#letting-your-di-integration-own-the-scope)) |

Health defaults here are `/system/livez` and `/system/readyz` — shorter than the FastAPI
adapter's, which nests them under `/system/health/`.

## Per-request dependency scope

`UnitScopeMiddleware` is installed as the outermost middleware and opens one
[unit scope](../concepts/dependency-injection.md) per request. The entrypoint also registers a
`unit_scope` dependency app-wide, so handlers can simply declare it:

```python
from servicewright import UnitScopeProtocol


@get("/orders/{order_id:str}")
async def get_order(order_id: str, unit_scope: UnitScopeProtocol) -> dict:
    use_case = await unit_scope.get(GetOrder)
    return await use_case.execute(order_id)
```

From code that has no handler parameters:

```python
from servicewright.adapters.litestar import current_unit_scope

scope = current_unit_scope()
```

The `unit_scope` dependency name is reserved — a user-supplied dependency of that name is
overridden, so the scope can never be shadowed by accident.

### Letting your DI integration own the scope

If your DI library's own Litestar integration already opens a request scope — dishka's
`setup_dishka()` with `FromDishka` / `@inject` handlers, for instance — switch the adapter's scope
off so the two never open two scopes per request:

```python
from dishka.integrations.litestar import setup_dishka

from servicewright.adapters.litestar import LitestarConfig, LitestarEntrypoint


def configure_app(app: Litestar, ctx: ServiceContext) -> None:
    setup_dishka(ctx.container.container, app)


http = LitestarEntrypoint(
    config=LitestarConfig(unit_scope=False),
    route_handlers=(get_order,),
    configure_app=configure_app,
)
```

With `unit_scope=False` the adapter installs neither `UnitScopeMiddleware` nor the `unit_scope`
dependency — the name is yours again — and `current_unit_scope()` raises `LookupError`. The health
probes and the lifecycle are unaffected. See
[dishka](dishka.md#using-dishkas-own-fastapi-or-litestar-integration) for the full picture,
including what happens if both end up installed.

## Merging your own Litestar options

Litestar is constructed in a single call, so the adapter merges carefully. Your
`litestar_kwargs` are the base, and the framework-managed keys are layered on top:

| Key | Behaviour |
| --- | --- |
| `route_handlers` | yours are appended after the adapter's (health routes, your `route_handlers` argument) |
| `middleware` | `UnitScopeMiddleware` is placed first, yours follow (`unit_scope=False`: yours only) |
| `dependencies` | merged, with `unit_scope` always winning (`unit_scope=False`: the name is not reserved) |
| `logging_config` | **forced to `None`** |
| everything else | passed through untouched |

!!! warning "Litestar's `LoggingConfig` is disabled on purpose"

    It reconfigures the root logger through `dictConfig`, which would silently undo the logging
    backend the Host installed. Configure logging through
    [`ObsConfig`](../concepts/observability.md) instead.

## Registering routes

```python
# 1. Directly
LitestarEntrypoint(route_handlers=(get_order, create_order))


# 2. When routes need the ServiceContext
async def register_routes(ctx: ServiceContext) -> list:
    catalog = await ctx.app_scope.get(Catalog)
    return [build_handler(catalog)]


LitestarEntrypoint(route_registerer=register_routes)


# 3. The final hook
def configure_app(app: Litestar, ctx: ServiceContext) -> None:
    ...


LitestarEntrypoint(configure_app=configure_app)
```

## Lifecycle notes

Identical in shape to the FastAPI adapter: no container-managing lifespan, the socket opens in
`bind()`, `serve()` returns while still accepting, `drain()` closes the listener.

```python
http.bound_port   # what the OS picked when port=0
http.app          # the built Litestar instance, or None before bind
await http.build_app(ctx)   # build without serving, for tests
```

## Plugin form

```python
from servicewright.adapters.litestar import LitestarPlugin

service = Service(spec, plugins=[LitestarPlugin(route_handlers=(get_order,))])
```
