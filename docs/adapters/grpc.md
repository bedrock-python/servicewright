# gRPC

```bash
pip install "servicewright[grpc]"
```

A gRPC server entrypoint built on
[grpc-server-kit](https://pypi.org/project/grpc-server-kit/), with the standard health service,
per-RPC DI scopes, correlation from metadata and `ServiceError` mapping already wired.

```python
from servicewright import Service
from servicewright.adapters.grpc import GrpcConfig, GrpcEntrypoint


def register_servicers(server, ctx) -> None:
    orders_pb2_grpc.add_OrdersServicer_to_server(OrdersServicer(), server)


grpc_ep = GrpcEntrypoint(
    config=GrpcConfig(port=50051),
    servicers=register_servicers,
)

service = Service(spec, entrypoints=[grpc_ep])
```

`servicers` is called during `bind` with the raw `grpc.aio.Server` and the `ServiceContext`, so it
can resolve singletons from `ctx.app_scope`. It may be async.

## Per-RPC dependency scope

`UnitScopeInterceptor` is added first — the outermost interceptor — so every downstream
interceptor and your servicer run inside a live [unit scope](../concepts/dependency-injection.md):

```python
from servicewright.adapters.grpc import current_unit_scope


class OrdersServicer(orders_pb2_grpc.OrdersServicer):
    async def GetOrder(self, request, context):
        scope = current_unit_scope()
        use_case = await scope.get(GetOrder)
        order = await use_case.execute(request.order_id)
        return orders_pb2.Order(id=order.id)
```

The scope stays open for the **whole** RPC, including the full response stream of a streaming
call.

## Correlation from metadata

The interceptor extracts and binds these into the [context store](../concepts/context.md):

| Context key | From metadata |
| --- | --- |
| `request_id` | `x-request-id` (generated when absent) |
| `user_id` | `x-user-id` |
| `tenant_id` | `x-tenant-id` |
| `trace_id` | `x-trace-id` |
| `idempotency_key` | `x-idempotency-key` |
| `client_ip` | `x-forwarded-for`, then `x-real-ip` |
| `user_agent` | `user-agent` / `x-user-agent` |
| `grpc_method` | the RPC's method name |

Helpers for reading them straight off a servicer context:

```python
from servicewright.adapters.grpc import (
    get_client_context,
    get_client_ip,
    get_idempotency_key,
    get_user_agent,
)
```

## Error mapping

A `ServiceError` raised anywhere in your servicer is converted into the matching abort:

```python
raise OrderNotFoundError("no such order")
# → the RPC aborts with NOT_FOUND
# → the machine code travels in the "x-error-code" trailing metadata
```

Non-public errors are masked exactly like over HTTP: the client sees a generic `INTERNAL` and the
real code only reaches your logs. The full mapping table is in
[Errors](../concepts/errors.md#the-kinds).

Turn it off with `map_service_errors=False`.

## Interceptor ordering

`grpc.aio` hands control in list order, so the first entry is the **outermost** wrapper. The
entrypoint assembles the chain like this:

```
UnitScopeInterceptor          ← outermost: everything below sees a live scope
  metrics interceptor         ← records the status the client actually receives
    your static interceptors
    your interceptors_factory results
      ServiceErrorInterceptor ← innermost: closest to the servicer
        your servicer
```

!!! warning "Why `ServiceErrorInterceptor` is innermost"

    A `ServiceError` has to become a mapped abort **before** any interceptor of yours sees it. A
    generic exception handler — grpc-server-kit's `AsyncExceptionHandlerInterceptor`, for
    instance, which maps unknown exceptions to `INTERNAL` — would otherwise swallow every domain
    error and turn a deliberate `NOT_FOUND` into a 500-equivalent.

    It still sits *inside* the metrics interceptor, so aborts are recorded with their real status.

```python
GrpcEntrypoint(
    config=config,
    servicers=register_servicers,
    interceptors=[AuthInterceptor()],
    interceptors_factory=lambda ctx: [RateLimitInterceptor(ctx.app_scope)],
)
```

## Health, reflection and channelz

The standard `grpc.health.v1.Health` service is always registered, driven by the same
[`HealthRegistry`](../concepts/health.md) as the HTTP probes:

```yaml
readinessProbe:
  grpc:
    port: 50051
```

Because the gRPC health API is a *push* API, the entrypoint polls readiness every
`health_refresh_interval` seconds while serving, so a dependency failing mid-life flips the status
to `NOT_SERVING` within roughly one interval. On drain, every tracked name is pinned to
`NOT_SERVING` so load balancers stop routing.

By default only the overall (empty-string) name is reported — the one a plain gRPC readiness probe
checks. List your own services to answer `Check` for them too:

```python
GrpcConfig(health_service_names=("my.pkg.Orders", "my.pkg.Payments"))
```

!!! danger "Reflection and channelz are off by default"

    Both serve on the **same port as production traffic** and are unauthenticated unless one of
    your interceptors authenticates them.

    - **Reflection** lets tools like `grpcurl` resolve symbols without a local `.proto`. Handy in
      dev and staging.
    - **Channelz** exposes `GetServers` / `GetServerSockets` / `GetSocket` / `GetTopChannels`,
      revealing connected peers' addresses and per-socket call and byte counters.

    ```python
    GrpcConfig(enable_reflection=True, enable_channelz=True)   # not in production
    ```

## Metrics

```python
GrpcEntrypoint(config=config, servicers=register_servicers, enable_metrics=True)
```

Records through whatever metrics backend the app configured — a null object when metrics are off,
so the flag is always safe:

| Metric | Labels |
| --- | --- |
| `grpc_requests_total` | `service`, `method`, `status`, `grpc_code` |
| `grpc_request_duration_seconds` | `service`, `method` |

Prefix them with `metrics_prefix="myapp"`.

## `GrpcConfig`

The main knobs:

| Field | Default | Meaning |
| --- | --- | --- |
| `host` | `"0.0.0.0"` | Bind host |
| `port` | `50051` | Bind port (`0` picks a free one) |
| `grace_period` | `30.0` | The entrypoint's own drain budget |
| `enable_reflection` | `False` | Serve server reflection |
| `enable_channelz` | `False` | Serve channelz |
| `reflection_service_names` | `None` | Extra names to advertise via reflection |
| `health_service_names` | `()` | Concrete services to report health for |
| `health_refresh_interval` | `5.0` | Readiness poll interval; `0` disables polling |
| `max_concurrent_rpcs` | `None` | Server-side concurrency cap |

Plus the full server-tuning surface — keepalive (`keepalive_time_ms`, `keepalive_timeout_ms`,
`keepalive_permit_without_calls`), HTTP/2 ping policy, message and metadata size limits, flow
control windows, connection age limits, compression — and TLS (`ssl_enabled`, `ssl_cert_file`,
`ssl_key_file`, `ssl_ca_file`, `ssl_client_auth`). See the
[API reference](../reference/adapters.md) for every field.

`GrpcConfig` satisfies grpc-server-kit's settings protocol, so it can be handed straight to the
kit's primitives.

## Drain semantics

The effective drain budget is `min(host_grace, config.grace_period)`:

- the Host's `drain_grace_seconds` bounds the shutdown;
- `grace_period` can only shorten it.

Raising `grace_period` above the Host's allowance has no effect — raise
[`AppSpec.drain_grace_seconds`](../concepts/lifecycle.md#budgets) instead.

## Lifecycle notes

- **`bind()`** creates the server, registers your servicers and the health servicer, and binds the
  port. A clash aborts startup.
- **`serve()`** starts the server, starts the health poller, and waits for the stop event — still
  accepting.
- **`drain(grace)`** stops the poller, pins health to `NOT_SERVING` and lets in-flight RPCs finish.
- **`stop()`** is the hard stop.

```python
grpc_ep.bound_port   # what the OS picked when port=0
```

grpc-server-kit's own `run_async_grpc_server` helper — which installs its own signal handlers and
blocks — is deliberately **not** used. The Host owns signals.

## Plugin form

```python
from servicewright.adapters.grpc import GrpcPlugin

service = Service(spec, plugins=[GrpcPlugin(config=config, servicers=register_servicers)])
```
