# dishka

```bash
pip install "servicewright[dishka]"
```

[dishka](https://dishka.readthedocs.io/) is the batteries-included DI option. The adapter is a thin
translation layer:

| servicewright | dishka |
| --- | --- |
| `AppScope` | `Scope.APP` |
| `UnitScope` | `Scope.REQUEST` |

## Usage

```python
from dishka import Provider, Scope, make_async_container, provide

from servicewright import AppSpec
from servicewright.adapters.dishka import DishkaContainer


class AppProvider(Provider):
    @provide(scope=Scope.APP)
    async def engine(self) -> AsyncIterator[AsyncEngine]:
        engine = create_async_engine(DSN)
        yield engine
        await engine.dispose()            # runs when the app scope closes

    @provide(scope=Scope.REQUEST)
    async def session(self, engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine) as session:
            yield session                 # closed when the unit scope closes

    @provide(scope=Scope.REQUEST)
    def get_order(self, session: AsyncSession) -> GetOrder:
        return GetOrder(session)


def build_container(settings: Settings) -> DishkaContainer:
    return DishkaContainer(make_async_container(AppProvider()))


spec = AppSpec(service_name="orders", create_container=build_container)
```

That is the whole integration. Every entrypoint now resolves from it:

```python
@router.get("/orders/{order_id}")
async def get_order(order_id: str, scope: UnitScopeDep) -> dict:
    use_case = await scope.get(GetOrder)
    return await use_case.execute(order_id)
```

## Finalization

- `app_scope()` yields a wrapper over the APP-scoped container and awaits `container.close()` on
  exit. The Host closes it **last**, after every entrypoint has drained and stopped, so an
  in-flight request can never find its pool already disposed.
- `unit_scope()` enters `Scope.REQUEST`. Exiting closes it, so dishka runs every REQUEST-scoped
  generator finalizer — sessions get closed, transactions get rolled back.

## Reaching the raw container

Both wrappers expose the underlying dishka objects, which is occasionally useful in tests or when
integrating a library that wants the real thing:

```python
container = DishkaContainer(make_async_container(AppProvider()))
container.container            # the AsyncContainer

async with container.app_scope() as scope:
    scope.container            # the APP-scoped AsyncContainer
```

## Injecting the request

The HTTP adapters pass `{"request": request}` as the unit-scope context. dishka keys its context
by **type**, so that string key is not resolvable — a provider declaring
`from_context(provides=Request, scope=Scope.REQUEST)` will not find it. (When dishka's own
integration owns the request scope — [below](#using-dishkas-own-fastapi-or-litestar-integration) —
its middleware puts `Request` in the context itself and none of this applies.)

If you want the `Request` injectable, remap the context in a subclass:

```python
from collections.abc import Mapping
from typing import Any

from fastapi import Request

from servicewright.adapters.dishka import DishkaContainer


class RequestAwareContainer(DishkaContainer):
    def unit_scope(self, context: Mapping[Any, Any] | None = None):
        remapped: dict[Any, Any] | None = None
        if context and "request" in context:
            remapped = {Request: context["request"]}
        return super().unit_scope(remapped)
```

```python
class AppProvider(Provider):
    request = from_context(provides=Request, scope=Scope.REQUEST)

    @provide(scope=Scope.REQUEST)
    def current_user(self, request: Request) -> User:
        return authenticate(request.headers["authorization"])
```

The same trick works for the gRPC and scheduler contexts — remap whichever keys you care about
onto the types your providers declare.

!!! tip

    Often you do not need this at all. Take the `Request` as a handler parameter and pass what you
    need into the use case, or read correlation values from the
    [context store](../concepts/context.md), which is transport-neutral and works identically in a
    scheduled job.

## Using dishka's own FastAPI or Litestar integration

servicewright's HTTP adapters open the request scope themselves and hand it out as
`UnitScopeDep` / `current_unit_scope()`. dishka's native integrations — `setup_dishka()` with
`FromDishka[...]` handler injection, `@inject`, `DishkaRoute` — open a `Scope.REQUEST` of their
own. Both at once means **two** REQUEST scopes per request: two sessions, two transactions.

Pick one owner. To keep dishka's integration (an existing codebase full of `FromDishka` is the
usual reason), switch the adapter's scope off and install dishka from `configure_app`:

=== "FastAPI"

    ```python
    from dishka.integrations.fastapi import setup_dishka

    from servicewright.adapters.fastapi import FastApiEntrypoint, MiddlewareConfig


    def configure_app(app: FastAPI, ctx: ServiceContext) -> None:
        setup_dishka(ctx.container.container, app)


    http = FastApiEntrypoint(
        routers=(router,),
        middlewares=MiddlewareConfig(unit_scope=False),
        configure_app=configure_app,
    )
    ```

=== "Litestar"

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

`ctx.container.container` is the APP-scoped `AsyncContainer` behind the adapter, so dishka's
middleware and the Host share one container: the Host still opens the application scope first and
closes it last, and every other entrypoint in the process (a scheduler, a consumer) keeps opening
its unit scopes through `DishkaContainer`. Per request, dishka's middleware is the single owner,
with everything its integration provides — `Request` in the context for `from_context(Request)`
providers, a `SESSION` scope for websockets — and it holds the scope open until the response is
fully sent, exactly as the adapter's middleware does.

What changes: `UnitScopeDep`, `request.state.unit_scope`, the Litestar `unit_scope` dependency and
`current_unit_scope()` raise `LookupError` in this mode. Resolve through `FromDishka` instead.

### Both installed by mistake

`DishkaContainer.unit_scope()` refuses to open a second scope for a request that dishka's
middleware has already scoped, so the mistake cannot ship silently: the first request — the
readiness probe included — fails with a `RuntimeError` naming the switch, instead of two sessions
quietly diverging under load.

## Testing

For unit tests you rarely want a real container:

```python
from servicewright.testing import FakeContainer

container = FakeContainer(provides={GetOrder: fake_use_case})
```

See [Testing](../guides/testing.md).
