"""Unit tests for the gRPC entrypoint (servicewright.adapters.grpc).

The grpc-server-kit server layer is mocked: ``create_async_grpc_server`` and
``bind_server_port`` are patched and a :class:`_FakeAsyncServer` is injected, so
these tests never open a real network socket.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from grpc_server_kit.aio.interceptors import AsyncMetricsInterceptor, RpcCall

from servicewright import AppSpec, Entrypoint, Plugin, Service
from servicewright.adapters.grpc import (
    IDEMPOTENCY_KEY_METADATA,
    GrpcConfig,
    GrpcEntrypoint,
    GrpcHealthBridge,
    GrpcPlugin,
    InterceptorFactory,
    ServicerRegisterer,
    UnitScopeInterceptor,
    current_unit_scope,
    get_client_context,
    get_client_ip,
    get_idempotency_key,
    get_user_agent,
)
from servicewright.adapters.grpc import entrypoint as entrypoint_mod
from servicewright.core.health import HealthRegistry
from servicewright.core.observability import NullCounter, NullHistogram, ObservabilityManager
from servicewright.core.spec import BootstrapContext, ServiceContext
from servicewright.testing import FakeContainer, FakeScope, FakeSettings

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _FakeRawServer:
    """Stand-in for a ``grpc.aio.Server`` used by servicer registration.

    Supports ``add_generic_rpc_handlers`` so the real health servicer (driven by
    ``add_HealthServicer_to_server``) registers without a real gRPC server.
    """

    def __init__(self) -> None:
        self.servicers: list[Any] = []
        self.generic_handlers: list[Any] = []
        self.registered_method_handlers: dict[str, Any] = {}

    def add_generic_rpc_handlers(self, handlers: tuple[Any, ...]) -> None:
        self.generic_handlers.extend(handlers)

    def add_registered_method_handlers(self, service_name: str, method_handlers: Any) -> None:
        self.registered_method_handlers[service_name] = method_handlers


class _FakeAsyncServer:
    """Stand-in for ``grpc_server_kit.aio.AsyncServer``."""

    def __init__(self) -> None:
        self.raw_server = _FakeRawServer()
        self.started = False
        self.stop_calls: list[float | None] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self, grace: float | None) -> None:
        self.stop_calls.append(grace)


class _FakeServicerContext:
    """Minimal ``grpc.aio.ServicerContext`` exposing invocation metadata."""

    def __init__(self, metadata: list[tuple[str, str | bytes]] | None = None) -> None:
        self._metadata = metadata

    def invocation_metadata(self) -> list[tuple[str, str | bytes]] | None:
        return self._metadata


def _make_call(context: Any, method_name: str) -> RpcCall:
    """Build a unary-unary RpcCall for driving an interceptor's around() hook."""
    return RpcCall(
        method_name=method_name,
        request="req",
        context=context,
        request_streaming=False,
        response_streaming=False,
    )


def _make_service_ctx(
    container: FakeContainer,
    *,
    service_name: str = "svc",
    health: HealthRegistry | None = None,
    observability: ObservabilityManager | None = None,
) -> ServiceContext:
    bootstrap = BootstrapContext(
        settings=FakeSettings(),
        service_name=service_name,
        container=container,
        lifecycle=object(),  # type: ignore[arg-type]
    )
    kwargs: dict[str, Any] = {}
    if observability is not None:
        kwargs["observability"] = observability
    return ServiceContext(
        bootstrap=bootstrap,
        app_scope=FakeScope(),
        health=health or HealthRegistry(),
        **kwargs,
    )


@pytest.fixture
def patched_server(monkeypatch: pytest.MonkeyPatch) -> _FakeAsyncServer:
    """Patch ``create_async_grpc_server``/``bind_server_port`` and capture calls."""
    fake_server = _FakeAsyncServer()
    create_calls: list[dict[str, Any]] = []

    def fake_create(**kwargs: Any) -> _FakeAsyncServer:
        create_calls.append(kwargs)
        # Drive the health register callback exactly like the real factory does.
        kwargs["register_servicers"](fake_server.raw_server)
        return fake_server

    def fake_bind(server: Any, settings: Any) -> int:
        return settings.port or 54321

    monkeypatch.setattr(entrypoint_mod, "create_async_grpc_server", fake_create)
    monkeypatch.setattr(entrypoint_mod, "bind_server_port", fake_bind)
    fake_server.create_calls = create_calls  # type: ignore[attr-defined]
    return fake_server


# --------------------------------------------------------------------------- #
# GrpcConfig
# --------------------------------------------------------------------------- #
def test__grpc_config__default__uses_the_documented_values() -> None:
    config = GrpcConfig()
    assert config.host == "0.0.0.0"
    assert config.port == 50051
    # Defaults to the Host's full drain allowance, so making grace_period live
    # cannot silently shorten the drain of a service that never set it.
    assert config.grace_period == 30.0
    # Introspection services expose peers and the API surface unauthenticated,
    # so they are opt-in.
    assert config.enable_reflection is False
    assert config.enable_channelz is False
    assert config.health_service_names == ()
    assert config.reflection_service_names is None
    assert config.ssl_enabled is False
    assert config.address == "0.0.0.0:50051"


def test__grpc_config__host_and_port_overridden__reports_the_address() -> None:
    config = GrpcConfig(host="127.0.0.1", port=0)
    assert config.address == "127.0.0.1:0"


# --------------------------------------------------------------------------- #
# Entrypoint protocol conformance & attributes
# --------------------------------------------------------------------------- #
def test__grpc_entrypoint__constructed__satisfies_the_protocol_without_a_unit_scope() -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    assert isinstance(ep, Entrypoint)
    assert ep.kind == "grpc"
    assert ep.essential is True
    # ServerEntrypoint must NOT expose unit_scope (framework owns per-RPC scope).
    assert not hasattr(ep, "unit_scope")


def test__grpc_entrypoint__kind_and_essential_overridden__reports_them() -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None, kind="api", essential=False)
    assert ep.kind == "api"
    assert ep.essential is False


def test__grpc_entrypoint__before_bind__exposes_its_config_and_no_port() -> None:
    config = GrpcConfig(port=12345)
    ep = GrpcEntrypoint(config=config, servicers=lambda _s, _c: None)
    assert ep.config is config
    assert ep.bound_port is None


# --------------------------------------------------------------------------- #
# bind()
# --------------------------------------------------------------------------- #
async def test__grpc_bind__called__creates_the_server_registers_servicers_and_binds(
    patched_server: _FakeAsyncServer,
) -> None:
    registered: list[tuple[Any, ServiceContext]] = []

    def register(server: Any, ctx: ServiceContext) -> None:
        registered.append((server, ctx))

    config = GrpcConfig(
        port=9999,
        reflection_service_names=["my.Service"],
        enable_reflection=True,
        enable_channelz=True,
    )
    ep = GrpcEntrypoint(config=config, servicers=register)
    ctx = _make_service_ctx(FakeContainer())

    await ep.bind(ctx)

    # Servicer callback got the raw server + the ServiceContext.
    assert registered == [(patched_server.raw_server, ctx)]
    assert ep.bound_port == 9999

    create_kwargs = patched_server.create_calls[0]  # type: ignore[attr-defined]
    assert create_kwargs["settings"] is config
    assert create_kwargs["enable_reflection"] is True
    assert create_kwargs["enable_channelz"] is True
    # Reflection names include the health service name plus the configured one.
    assert create_kwargs["reflection_service_names"] == ["grpc.health.v1.Health", "my.Service"]


async def test__grpc_bind__no_extra_reflection_names__advertises_only_the_health_service(
    patched_server: _FakeAsyncServer,
) -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer()))
    create_kwargs = patched_server.create_calls[0]  # type: ignore[attr-defined]
    assert create_kwargs["reflection_service_names"] == ["grpc.health.v1.Health"]


async def test__grpc_bind__introspection_flags_off__disables_both_services(patched_server: _FakeAsyncServer) -> None:
    config = GrpcConfig(enable_reflection=False, enable_channelz=False)
    ep = GrpcEntrypoint(config=config, servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer()))
    create_kwargs = patched_server.create_calls[0]  # type: ignore[attr-defined]
    assert create_kwargs["enable_reflection"] is False
    assert create_kwargs["enable_channelz"] is False


async def test__grpc_bind__called__installs_the_unit_scope_interceptor_outermost(
    patched_server: _FakeAsyncServer,
) -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer()))
    interceptors = patched_server.create_calls[0]["interceptors"]  # type: ignore[attr-defined]
    assert isinstance(interceptors[0], UnitScopeInterceptor)


async def test__grpc_bind__default_config__installs_the_service_error_mapper(patched_server: _FakeAsyncServer) -> None:
    from servicewright.adapters.grpc import ServiceErrorInterceptor

    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer()))
    interceptors = patched_server.create_calls[0]["interceptors"]  # type: ignore[attr-defined]
    assert any(isinstance(i, ServiceErrorInterceptor) for i in interceptors)


async def test__grpc_bind__error_mapping_disabled__omits_the_mapper(patched_server: _FakeAsyncServer) -> None:
    from servicewright.adapters.grpc import ServiceErrorInterceptor

    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None, map_service_errors=False)
    await ep.bind(_make_service_ctx(FakeContainer()))
    interceptors = patched_server.create_calls[0]["interceptors"]  # type: ignore[attr-defined]
    assert not any(isinstance(i, ServiceErrorInterceptor) for i in interceptors)


async def test__grpc_bind__async_servicer_registerer__awaits_it(patched_server: _FakeAsyncServer) -> None:
    called: list[str] = []

    async def register(server: Any, ctx: ServiceContext) -> None:
        called.append("async-register")

    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=register)
    await ep.bind(_make_service_ctx(FakeContainer()))
    assert called == ["async-register"]


async def test__grpc_bind__static_interceptors_given__appends_them(patched_server: _FakeAsyncServer) -> None:
    static = MagicMock(name="static-interceptor")
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None, interceptors=[static])
    await ep.bind(_make_service_ctx(FakeContainer()))
    interceptors = patched_server.create_calls[0]["interceptors"]  # type: ignore[attr-defined]
    assert static in interceptors
    # Still after the unit scope interceptor.
    assert isinstance(interceptors[0], UnitScopeInterceptor)


async def test__grpc_bind__sync_interceptor_factory__uses_its_result(patched_server: _FakeAsyncServer) -> None:
    extra = MagicMock(name="factory-interceptor")
    seen_ctx: list[ServiceContext] = []

    def factory(ctx: ServiceContext) -> list[Any]:
        seen_ctx.append(ctx)
        return [extra]

    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None, interceptors_factory=factory)
    ctx = _make_service_ctx(FakeContainer())
    await ep.bind(ctx)

    interceptors = patched_server.create_calls[0]["interceptors"]  # type: ignore[attr-defined]
    assert extra in interceptors
    assert seen_ctx == [ctx]


async def test__grpc_bind__async_interceptor_factory__awaits_its_result(patched_server: _FakeAsyncServer) -> None:
    extra = MagicMock(name="async-factory-interceptor")

    async def factory(ctx: ServiceContext) -> list[Any]:
        return [extra]

    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None, interceptors_factory=factory)
    await ep.bind(_make_service_ctx(FakeContainer()))
    interceptors = patched_server.create_calls[0]["interceptors"]  # type: ignore[attr-defined]
    assert extra in interceptors


class _RecordingMetricsSink:
    """Metrics-sink double capturing minted instrument names."""

    backend = "fake"

    def __init__(self) -> None:
        self.counters: list[str] = []
        self.histograms: list[str] = []

    def setup(self, ctx: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def mount(self, app: Any) -> None:
        return None

    def counter(self, name: str, description: str, label_names: tuple[str, ...] = ()) -> NullCounter:
        self.counters.append(name)
        return NullCounter()

    def histogram(
        self,
        name: str,
        description: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> NullHistogram:
        self.histograms.append(name)
        return NullHistogram()


async def test__grpc_bind__metrics_enabled__adds_the_metrics_interceptor(patched_server: _FakeAsyncServer) -> None:
    sink = _RecordingMetricsSink()
    manager = ObservabilityManager()
    manager._metrics = sink  # inject the double past configure()

    ep = GrpcEntrypoint(
        config=GrpcConfig(),
        servicers=lambda _s, _c: None,
        enable_metrics=True,
        metrics_prefix="myprefix",
    )
    await ep.bind(_make_service_ctx(FakeContainer(), service_name="metered", observability=manager))

    interceptors = patched_server.create_calls[0]["interceptors"]  # type: ignore[attr-defined]
    # UnitScope first, the kit's metrics interceptor second.
    assert isinstance(interceptors[0], UnitScopeInterceptor)
    assert isinstance(interceptors[1], AsyncMetricsInterceptor)
    # The recorder minted the frozen instruments with the prefix applied.
    assert sink.counters == ["myprefix_grpc_requests_total"]
    assert sink.histograms == ["myprefix_grpc_request_duration_seconds"]


async def test__grpc_bind__metrics_disabled__omits_the_metrics_interceptor(patched_server: _FakeAsyncServer) -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None, enable_metrics=False)
    await ep.bind(_make_service_ctx(FakeContainer()))
    interceptors = patched_server.create_calls[0]["interceptors"]  # type: ignore[attr-defined]
    assert not any(isinstance(i, AsyncMetricsInterceptor) for i in interceptors)


async def test__grpc_bind__metrics_enabled_without_a_sink__records_into_null_instruments(
    patched_server: _FakeAsyncServer,
) -> None:
    # Metrics concern disabled/unconfigured => the recorder is built over
    # NullObject instruments; binding must not raise.
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None, enable_metrics=True)
    await ep.bind(_make_service_ctx(FakeContainer()))

    interceptors = patched_server.create_calls[0]["interceptors"]  # type: ignore[attr-defined]
    assert isinstance(interceptors[1], AsyncMetricsInterceptor)


class _RecordingRecorder:
    """Recorder double capturing record_request calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str, float]] = []

    def record_request(self, service: str, method: str, status: str, grpc_code: str, duration: float) -> None:
        self.calls.append((service, method, status, grpc_code, duration))


class _FakeRpcContext:
    """Minimal servicer context: code() returns what the handler set (or None)."""

    def __init__(self, code: Any = None) -> None:
        self._code = code

    def code(self) -> Any:
        return self._code


async def test__metrics_interceptor__rpc_served__records_through_the_adapters_recorder() -> None:
    """The adapter's recorder satisfies the kit's GrpcServerMetricsProtocol seam."""
    recorder = _RecordingRecorder()
    interceptor = AsyncMetricsInterceptor(recorder, service_name="svc")
    call = _make_call(_FakeRpcContext(), "/pkg.Svc/Rpc")

    async with interceptor.around(call):
        pass

    (service, method, status, grpc_code, duration) = recorder.calls[0]
    assert (service, method, status, grpc_code) == ("svc", "/pkg.Svc/Rpc", "success", "OK")
    assert duration >= 0.0


def test__grpc_recorder__prometheus_backend__writes_the_frozen_metric_names() -> None:
    """The recorder composes generic instruments into the frozen platform metrics."""
    from prometheus_client import CollectorRegistry

    from servicewright.adapters.grpc.metrics import GrpcServerMetricsRecorder
    from servicewright.adapters.observability._metrics.prometheus import PrometheusMetricsSink

    registry = CollectorRegistry()
    recorder = GrpcServerMetricsRecorder(PrometheusMetricsSink(registry=registry))

    recorder.record_request("pkg.Svc", "Rpc", "ok", "OK", 0.05)
    recorder.record_request("pkg.Svc", "Rpc", "ok", "OK", 0.10)

    total = registry.get_sample_value(
        "grpc_requests_total",
        {"service": "pkg.Svc", "method": "Rpc", "status": "ok", "grpc_code": "OK"},
    )
    assert total == 2.0
    duration_count = registry.get_sample_value(
        "grpc_request_duration_seconds_count", {"service": "pkg.Svc", "method": "Rpc"}
    )
    assert duration_count == 2.0


# --------------------------------------------------------------------------- #
# serve() / drain() / stop()
# --------------------------------------------------------------------------- #
async def test__grpc_serve__called_before_bind__raises() -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(RuntimeError, match="serve\\(\\) called before bind\\(\\)"):
        await ep.serve(stop=stop)


async def test__grpc_serve__called__starts_the_server_and_publishes_health(patched_server: _FakeAsyncServer) -> None:
    health = HealthRegistry()
    health.ready = True
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer(), health=health))

    stop = asyncio.Event()

    async def serve_then_check() -> None:
        # Give serve() a turn to start the server, then release it.
        while not patched_server.started:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(ep.serve(stop=stop), serve_then_check())
    assert patched_server.started is True


async def test__grpc_drain__called__enters_graceful_shutdown_with_the_grace(patched_server: _FakeAsyncServer) -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer()))
    # Spy on the health bridge graceful shutdown.
    bridge_calls: list[str] = []
    assert ep._health is not None
    monkeypatch_graceful(ep._health, bridge_calls)

    await ep.drain(2.5)

    assert patched_server.stop_calls == [2.5]
    assert bridge_calls == ["graceful"]
    assert ep._stopped is True


async def test__grpc_drain__called_before_bind__does_nothing() -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    await ep.drain(1.0)  # no server, must not raise


async def test__grpc_drain__called_after_stop__does_nothing(patched_server: _FakeAsyncServer) -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer()))
    await ep.stop()
    patched_server.stop_calls.clear()
    await ep.drain(1.0)  # already stopped -> no extra stop call
    assert patched_server.stop_calls == []


async def test__grpc_stop__called__hard_stops_the_server(patched_server: _FakeAsyncServer) -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer()))
    await ep.stop()
    assert patched_server.stop_calls == [None]
    assert ep._stopped is True


async def test__grpc_stop__called_before_bind__does_nothing() -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    await ep.stop()  # no server -> no raise


async def test__grpc_stop__called_twice__stops_once(patched_server: _FakeAsyncServer) -> None:
    ep = GrpcEntrypoint(config=GrpcConfig(), servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer()))
    await ep.stop()
    await ep.stop()
    assert patched_server.stop_calls == [None]


def monkeypatch_graceful(bridge: GrpcHealthBridge, calls: list[str]) -> None:
    async def _graceful() -> None:
        calls.append("graceful")

    bridge.enter_graceful_shutdown = _graceful  # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# UnitScopeInterceptor / current_unit_scope
# --------------------------------------------------------------------------- #
def test__grpc_current_unit_scope__called_outside_an_rpc__raises() -> None:
    with pytest.raises(LookupError, match="No active gRPC unit scope"):
        current_unit_scope()


async def test__unit_scope_interceptor__rpc_served__opens_a_scope_and_exposes_it() -> None:
    container = FakeContainer(provides={str: "dep"})
    interceptor = UnitScopeInterceptor(container)
    context = _FakeServicerContext([("x-idempotency-key", "abc")])

    seen_scope: list[Any] = []
    async with interceptor.around(_make_call(context, "/pkg.Service/Method")):
        scope = current_unit_scope()
        seen_scope.append(scope)
        assert await scope.get(str) == "dep"

    assert container.unit_scopes_opened == 1
    # The scope carried the RPC payload as its context.
    unit_ctx = container.unit_contexts[0]
    assert unit_ctx is not None
    assert unit_ctx["grpc_method"] == "/pkg.Service/Method"
    assert unit_ctx["idempotency_key"] == "abc"
    assert seen_scope[0].context == unit_ctx


async def test__unit_scope_interceptor__rpc_finished__resets_the_context_var() -> None:
    container = FakeContainer()
    interceptor = UnitScopeInterceptor(container)

    async with interceptor.around(_make_call(_FakeServicerContext([]), "/m")):
        current_unit_scope()  # resolvable while the RPC is in flight

    # Outside the RPC the context var must be cleared again.
    with pytest.raises(LookupError):
        current_unit_scope()


async def test__unit_scope_interceptor__handler_raises__still_resets_the_context_var() -> None:
    container = FakeContainer()
    interceptor = UnitScopeInterceptor(container)

    with pytest.raises(ValueError, match="boom"):
        async with interceptor.around(_make_call(_FakeServicerContext([]), "/m")):
            raise ValueError("boom")

    with pytest.raises(LookupError):
        current_unit_scope()


async def test__unit_scope_interceptor__rpc_metadata_present__builds_the_full_context() -> None:
    container = FakeContainer()
    interceptor = UnitScopeInterceptor(container)

    metadata: list[tuple[str, str | bytes]] = [
        ("x-idempotency-key", "key-1"),
        ("x-forwarded-for", "10.0.0.1"),
        ("x-user-agent", "my-agent/1.0"),
    ]
    async with interceptor.around(_make_call(_FakeServicerContext(metadata), "/pkg.Svc/Rpc")):
        pass

    unit_ctx = dict(container.unit_contexts[0] or {})
    # No x-request-id in metadata -> the interceptor generates one (a UUID).
    generated_request_id = unit_ctx.pop("request_id")
    assert uuid.UUID(generated_request_id)
    assert unit_ctx == {
        "grpc_method": "/pkg.Svc/Rpc",
        "idempotency_key": "key-1",
        "client_ip": "10.0.0.1",
        "user_agent": "my-agent/1.0",
    }


async def test__unit_scope_interceptor__correlation_metadata__extracts_the_ids() -> None:
    from servicewright import get_context_value

    container = FakeContainer()
    interceptor = UnitScopeInterceptor(container)
    metadata: list[tuple[str, str | bytes]] = [
        ("x-request-id", "req-42"),
        ("x-user-id", "user-9"),
        ("x-tenant-id", "tenant-1"),
        ("x-trace-id", "trace-7"),
    ]

    async with interceptor.around(_make_call(_FakeServicerContext(metadata), "/pkg.Svc/Rpc")):
        # Correlation ids from metadata are readable transport-neutrally (parity with HTTP).
        assert get_context_value("request_id") == "req-42"
        assert get_context_value("user_id") == "user-9"
        assert get_context_value("tenant_id") == "tenant-1"
        assert get_context_value("trace_id") == "trace-7"

    unit_ctx = container.unit_contexts[0]
    assert unit_ctx is not None
    assert unit_ctx["request_id"] == "req-42"


async def test__unit_scope_interceptor__log_unsafe_correlation_id__drops_it() -> None:
    container = FakeContainer()
    interceptor = UnitScopeInterceptor(container)
    metadata: list[tuple[str, str | bytes]] = [
        ("x-request-id", "bad\nid"),  # log-unsafe -> dropped, replaced by a generated one
        ("x-user-id", "u" * 300),  # overlong -> dropped
        ("x-idempotency-key", "bad\nkey"),  # log-unsafe -> dropped
    ]

    async with interceptor.around(_make_call(_FakeServicerContext(metadata), "/pkg.Svc/Rpc")):
        pass

    unit_ctx = container.unit_contexts[0]
    assert unit_ctx is not None
    assert uuid.UUID(unit_ctx["request_id"])  # regenerated, not the unsafe value
    assert "user_id" not in unit_ctx
    assert unit_ctx["idempotency_key"] is None


async def test__unit_scope_interceptor__rpc_served__mirrors_the_context_into_the_core_store() -> None:
    from servicewright import get_context_value

    interceptor = UnitScopeInterceptor(FakeContainer())
    metadata: list[tuple[str, str | bytes]] = [("x-idempotency-key", "key-7")]

    async with interceptor.around(_make_call(_FakeServicerContext(metadata), "/pkg.Svc/Rpc")):
        # Business code reads the RPC payload transport-neutrally.
        assert get_context_value("grpc_method") == "/pkg.Svc/Rpc"
        assert get_context_value("idempotency_key") == "key-7"

    # Outside the RPC the store is clean again.
    assert get_context_value("grpc_method") is None


# --------------------------------------------------------------------------- #
# ServiceErrorInterceptor: kind -> grpc.StatusCode mapping
# --------------------------------------------------------------------------- #
class _AbortedSentinelError(Exception):
    """Raised by the fake context's abort(), standing in for grpc.aio.AbortError."""


class _AbortRecordingContext:
    """Servicer-context double recording abort() calls."""

    def __init__(self) -> None:
        self.aborts: list[tuple[Any, str, Any]] = []

    async def abort(self, code: Any, details: str = "", trailing_metadata: Any = None) -> None:
        self.aborts.append((code, details, trailing_metadata))
        raise _AbortedSentinelError


async def test__service_error_interceptor__service_error_raised__aborts_with_the_mapped_status() -> None:
    import grpc as grpc_lib

    from servicewright import ErrorKind, ServiceError
    from servicewright.adapters.grpc import ERROR_CODE_TRAILING_METADATA, ServiceErrorInterceptor

    class _UserMissingError(ServiceError):
        kind = ErrorKind.NOT_FOUND

    context = _AbortRecordingContext()
    interceptor = ServiceErrorInterceptor()

    with pytest.raises(_AbortedSentinelError):
        async with interceptor.around(_make_call(context, "/pkg.Svc/Rpc")):
            raise _UserMissingError("no such user")

    (code, details, trailing) = context.aborts[0]
    assert code == grpc_lib.StatusCode.NOT_FOUND
    assert details == "no such user"
    assert trailing == ((ERROR_CODE_TRAILING_METADATA, "user_missing"),)


async def test__service_error_interceptor__private_error__masks_it() -> None:
    import grpc as grpc_lib

    from servicewright import ErrorKind, ServiceError
    from servicewright.adapters.grpc import ServiceErrorInterceptor

    context = _AbortRecordingContext()
    interceptor = ServiceErrorInterceptor()

    with pytest.raises(_AbortedSentinelError):
        async with interceptor.around(_make_call(context, "/pkg.Svc/Rpc")):
            raise ServiceError("secret", code="secret_leak", kind=ErrorKind.CONFLICT, public=False)

    (code, details, trailing) = context.aborts[0]
    # Masked: generic INTERNAL, no leaking code or detail.
    assert code == grpc_lib.StatusCode.INTERNAL
    assert "secret" not in details
    assert trailing == (("x-error-code", "internal_error"),)


async def test__service_error_interceptor__other_exception__passes_it_through() -> None:
    from servicewright.adapters.grpc import ServiceErrorInterceptor

    context = _AbortRecordingContext()

    with pytest.raises(RuntimeError, match="boom"):
        async with ServiceErrorInterceptor().around(_make_call(context, "/pkg.Svc/Rpc")):
            raise RuntimeError("boom")

    assert context.aborts == []


# --------------------------------------------------------------------------- #
# GrpcHealthBridge
# --------------------------------------------------------------------------- #
async def test__health_bridge__registry_ready__reports_serving() -> None:
    registry = HealthRegistry()
    registry.ready = True
    bridge = GrpcHealthBridge(registry)

    set_calls: list[tuple[str, Any]] = []

    async def fake_set(name: str, status: Any) -> None:
        set_calls.append((name, status))

    bridge._servicer.set = fake_set  # type: ignore[method-assign]
    await bridge.refresh()

    from grpc_health.v1 import health_pb2

    assert set_calls == [("", health_pb2.HealthCheckResponse.SERVING)]


async def test__health_bridge__registry_not_ready__reports_not_serving() -> None:
    registry = HealthRegistry()
    registry.ready = False
    bridge = GrpcHealthBridge(registry, service_names=("pkg.Service",))

    set_calls: list[tuple[str, Any]] = []

    async def fake_set(name: str, status: Any) -> None:
        set_calls.append((name, status))

    bridge._servicer.set = fake_set  # type: ignore[method-assign]
    await bridge.refresh()

    from grpc_health.v1 import health_pb2

    not_serving = health_pb2.HealthCheckResponse.NOT_SERVING
    # Overall ("") plus the explicit service name, both NOT_SERVING.
    assert set_calls == [("", not_serving), ("pkg.Service", not_serving)]


def test__health_bridge_register__called__adds_the_servicer_to_the_server() -> None:
    bridge = GrpcHealthBridge(HealthRegistry())
    server = _FakeRawServer()
    bridge.register(server)  # type: ignore[arg-type]
    # The real health servicer registered a generic rpc handler on the server.
    assert server.generic_handlers


async def test__health_bridge__graceful_shutdown_entered__pins_not_serving() -> None:
    bridge = GrpcHealthBridge(HealthRegistry())
    called: list[bool] = []

    async def fake_graceful() -> None:
        called.append(True)

    bridge._servicer.enter_graceful_shutdown = fake_graceful  # type: ignore[method-assign]
    await bridge.enter_graceful_shutdown()
    assert called == [True]


# --------------------------------------------------------------------------- #
# metadata helpers
# --------------------------------------------------------------------------- #
def test__get_idempotency_key__header_present__returns_it() -> None:
    ctx = _FakeServicerContext([("X-Idempotency-Key", "idem-123")])
    assert get_idempotency_key(ctx) == "idem-123"


def test__get_idempotency_key__binary_value__decodes_it() -> None:
    ctx = _FakeServicerContext([(IDEMPOTENCY_KEY_METADATA, b"bytes-key")])
    assert get_idempotency_key(ctx) == "bytes-key"


def test__get_idempotency_key__header_absent__returns_none() -> None:
    ctx = _FakeServicerContext([("other", "value")])
    assert get_idempotency_key(ctx) is None


def test__get_idempotency_key__no_metadata_at_all__returns_none() -> None:
    assert get_idempotency_key(_FakeServicerContext(None)) is None


def test__get_client_ip__forwarded_for_present__prefers_it() -> None:
    ctx = _FakeServicerContext([("x-forwarded-for", "1.2.3.4"), ("x-real-ip", "5.6.7.8")])
    assert get_client_ip(ctx) == "1.2.3.4"


def test__get_client_ip__only_real_ip_present__falls_back_to_it() -> None:
    ctx = _FakeServicerContext([("x-real-ip", "5.6.7.8")])
    assert get_client_ip(ctx) == "5.6.7.8"


def test__get_client_ip__no_ip_headers__returns_none() -> None:
    ctx = _FakeServicerContext([("user-agent", "x")])
    assert get_client_ip(ctx) is None


def test__get_client_ip__no_metadata_at_all__returns_none() -> None:
    assert get_client_ip(_FakeServicerContext(None)) is None


def test__get_user_agent__x_user_agent_present__prefers_it() -> None:
    ctx = _FakeServicerContext([("user-agent", "grpc-python"), ("x-user-agent", "custom/2.0")])
    assert get_user_agent(ctx) == "custom/2.0"


def test__get_user_agent__only_user_agent_present__falls_back_to_it() -> None:
    ctx = _FakeServicerContext([("user-agent", "grpc-python")])
    assert get_user_agent(ctx) == "grpc-python"


def test__get_user_agent__no_agent_headers__returns_none() -> None:
    ctx = _FakeServicerContext([("x-forwarded-for", "1.2.3.4")])
    assert get_user_agent(ctx) is None


def test__get_user_agent__no_metadata_at_all__returns_none() -> None:
    assert get_user_agent(_FakeServicerContext(None)) is None


def test__get_client_context__metadata_present__returns_ip_and_agent() -> None:
    ctx = _FakeServicerContext([("x-real-ip", "9.9.9.9"), ("x-user-agent", "ua/1")])
    assert get_client_context(ctx) == ("9.9.9.9", "ua/1")


# --------------------------------------------------------------------------- #
# GrpcPlugin
# --------------------------------------------------------------------------- #
def test__grpc_plugin__constructed__satisfies_the_plugin_protocol() -> None:
    plugin = GrpcPlugin(config=GrpcConfig(), servicers=lambda _s, _c: None)
    assert isinstance(plugin, Plugin)


def test__grpc_plugin__constructed__exposes_its_entrypoint() -> None:
    plugin = GrpcPlugin(config=GrpcConfig(), servicers=lambda _s, _c: None, kind="api", essential=False)
    ep = plugin.entrypoint
    assert isinstance(ep, GrpcEntrypoint)
    assert ep.kind == "api"
    assert ep.essential is False


def test__grpc_plugin_on_register__called__adds_its_entrypoint_to_the_host() -> None:
    plugin = GrpcPlugin(config=GrpcConfig(), servicers=lambda _s, _c: None)
    host = MagicMock()
    plugin.on_register(spec=MagicMock(), host=host)
    host.add_entrypoint.assert_called_once_with(plugin.entrypoint)


# --------------------------------------------------------------------------- #
# Public type aliases are importable / usable as annotations
# --------------------------------------------------------------------------- #
def test__grpc_public_aliases__imported__are_all_exported() -> None:
    assert ServicerRegisterer is not None
    assert InterceptorFactory is not None


# --------------------------------------------------------------------------- #
# End-to-end-ish: drive a GrpcEntrypoint through a real Host/Service
# --------------------------------------------------------------------------- #
async def test__grpc_entrypoint__driven_by_a_service__completes_the_lifecycle(patched_server: _FakeAsyncServer) -> None:
    """Run a Service end-to-end with a real Host and a mocked gRPC server."""
    container = FakeContainer()
    ready_during_serve: list[bool] = []

    def register(server: Any, ctx: ServiceContext) -> None:
        # Servicer registration happens during bind, before readiness flips.
        ready_during_serve.append(ctx.health.ready)

    spec: AppSpec[Any, Any] = AppSpec(service_name="grpc-service", create_container=lambda _s: container)
    ep = GrpcEntrypoint(config=GrpcConfig(port=0), servicers=register)
    service = Service(spec, entrypoints=[ep])

    stop = asyncio.Event()

    async def run_then_stop() -> None:
        while not spec.health.ready:
            await asyncio.sleep(0)
        # Server must have started by the time readiness can be observed serving.
        while not patched_server.started:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(service.run(FakeSettings(), stop=stop), run_then_stop())

    # Lifecycle: bound (readiness False during register), served, drained gracefully.
    assert ready_during_serve == [False]
    assert patched_server.started is True
    # drain() in shutdown stops with the host's drain grace (a real float).
    assert patched_server.stop_calls
    assert spec.health.ready is False
    assert ep.bound_port == 54321
    assert container.app_scopes_opened == 1


async def test__grpc_plugin__driven_by_a_service__completes_the_lifecycle(patched_server: _FakeAsyncServer) -> None:
    """A GrpcPlugin registers its entrypoint and the Host drives it."""
    container = FakeContainer()
    spec: AppSpec[Any, Any] = AppSpec(service_name="grpc-plugin-service", create_container=lambda _s: container)
    plugin = GrpcPlugin(config=GrpcConfig(port=0), servicers=lambda _s, _c: None)
    service = Service(spec, plugins=[plugin])

    stop = asyncio.Event()

    async def run_then_stop() -> None:
        while not spec.health.ready:
            await asyncio.sleep(0)
        while not patched_server.started:
            await asyncio.sleep(0)
        stop.set()

    await asyncio.gather(service.run(FakeSettings(), stop=stop), run_then_stop())

    assert patched_server.started is True
    assert patched_server.stop_calls  # drained/stopped
    assert spec.health.ready is False


# --------------------------------------------------------------------------- #
# Regression cover for the super-review findings
# --------------------------------------------------------------------------- #
class _RecordingHealthServicer:
    """Stand-in for ``health_aio.HealthServicer`` recording pushed statuses."""

    def __init__(self) -> None:
        self.pushed: list[tuple[str, Any]] = []
        self.graceful_shutdowns = 0
        # add_HealthServicer_to_server() reads the RPC methods off the servicer.
        self.Check = MagicMock()
        self.Watch = MagicMock()

    async def set(self, name: str, status: Any) -> None:
        self.pushed.append((name, status))

    async def enter_graceful_shutdown(self) -> None:
        self.graceful_shutdowns += 1


class _ToggleCheck:
    """Health check whose verdict can be flipped mid-flight."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    async def check(self) -> bool:
        return self.healthy


@pytest.fixture
def health_servicer(monkeypatch: pytest.MonkeyPatch) -> _RecordingHealthServicer:
    """Make every ``GrpcHealthBridge`` build a recording servicer."""
    from servicewright.adapters.grpc import health as health_mod

    servicer = _RecordingHealthServicer()
    fake_module = MagicMock()
    fake_module.HealthServicer.return_value = servicer
    monkeypatch.setattr(health_mod, "health_aio", fake_module)
    return servicer


@pytest.fixture
def ready_registry() -> HealthRegistry:
    registry = HealthRegistry()
    registry.ready = True
    return registry


def _pushed_statuses(servicer: _RecordingHealthServicer) -> list[Any]:
    return [status for _name, status in servicer.pushed]


@pytest.mark.parametrize(
    ("check", "expected_serving"),
    [
        pytest.param(None, True, id="no-checks"),
        pytest.param(_ToggleCheck(healthy=True), True, id="check-passes"),
        pytest.param(_ToggleCheck(healthy=False), False, id="check-fails"),
    ],
)
async def test__health_bridge_refresh__registry_verdict__is_pushed_to_the_health_service(
    health_servicer: _RecordingHealthServicer,
    ready_registry: HealthRegistry,
    check: _ToggleCheck | None,
    expected_serving: bool,
) -> None:
    # Arrange
    from servicewright.adapters.grpc._imports import health_pb2

    if check is not None:
        ready_registry.add_check("postgres", check)
    bridge = GrpcHealthBridge(ready_registry)
    expected = (
        health_pb2.HealthCheckResponse.SERVING if expected_serving else health_pb2.HealthCheckResponse.NOT_SERVING
    )

    # Act
    await bridge.refresh()

    # Assert
    assert _pushed_statuses(health_servicer) == [expected]


async def test__health_bridge_refresh__configured_service_names__reports_each_of_them(
    health_servicer: _RecordingHealthServicer,
    ready_registry: HealthRegistry,
) -> None:
    # Arrange
    bridge = GrpcHealthBridge(ready_registry, service_names=("my.pkg.Orders",))

    # Act
    await bridge.refresh()

    # Assert
    assert [name for name, _status in health_servicer.pushed] == ["", "my.pkg.Orders"]


async def test__health_bridge_watch__non_positive_interval__never_polls(
    health_servicer: _RecordingHealthServicer,
    ready_registry: HealthRegistry,
) -> None:
    # Arrange
    bridge = GrpcHealthBridge(ready_registry)

    # Act
    await asyncio.wait_for(bridge.watch(0), timeout=1)

    # Assert
    assert health_servicer.pushed == []


async def test__grpc_entrypoint_serve__dependency_fails_mid_life__health_flips_to_not_serving(
    patched_server: _FakeAsyncServer,
    health_servicer: _RecordingHealthServicer,
    ready_registry: HealthRegistry,
) -> None:
    # Arrange
    from servicewright.adapters.grpc._imports import health_pb2

    check = _ToggleCheck(healthy=True)
    ready_registry.add_check("postgres", check)
    ep = GrpcEntrypoint(
        config=GrpcConfig(port=0, health_refresh_interval=0.01),
        servicers=lambda _s, _c: None,
    )
    await ep.bind(_make_service_ctx(FakeContainer(), health=ready_registry))
    stop = asyncio.Event()
    serving = asyncio.ensure_future(ep.serve(stop=stop))

    # Act
    check.healthy = False
    for _ in range(200):
        await asyncio.sleep(0.01)
        if health_pb2.HealthCheckResponse.NOT_SERVING in _pushed_statuses(health_servicer):
            break
    stop.set()
    await serving

    # Assert
    assert health_pb2.HealthCheckResponse.NOT_SERVING in _pushed_statuses(health_servicer)


async def test__grpc_entrypoint_serve__stop_requested__the_health_poller_does_not_outlive_it(
    patched_server: _FakeAsyncServer,
    health_servicer: _RecordingHealthServicer,
) -> None:
    # Arrange
    ep = GrpcEntrypoint(config=GrpcConfig(port=0, health_refresh_interval=0.01), servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer()))
    stop = asyncio.Event()
    serving = asyncio.ensure_future(ep.serve(stop=stop))
    await asyncio.sleep(0.03)

    # Act
    stop.set()
    await serving
    pushed_at_stop = len(health_servicer.pushed)
    await asyncio.sleep(0.05)

    # Assert
    assert len(health_servicer.pushed) == pushed_at_stop


async def test__grpc_entrypoint_bind__user_interceptors_supplied__error_mapper_stays_innermost(
    patched_server: _FakeAsyncServer,
) -> None:
    # Arrange
    from servicewright.adapters.grpc.errors import ServiceErrorInterceptor

    user_static = MagicMock(name="static")
    user_factory = MagicMock(name="factory")
    ep = GrpcEntrypoint(
        config=GrpcConfig(port=0),
        servicers=lambda _s, _c: None,
        interceptors=[user_static],
        interceptors_factory=lambda _ctx: [user_factory],
    )

    # Act
    await ep.bind(_make_service_ctx(FakeContainer()))

    # Assert
    chain = patched_server.create_calls[0]["interceptors"]
    assert isinstance(chain[0], UnitScopeInterceptor)
    assert isinstance(chain[-1], ServiceErrorInterceptor)
    assert chain.index(user_static) < chain.index(user_factory) < len(chain) - 1


async def test__grpc_entrypoint_bind__error_mapping_disabled__omits_the_mapper(
    patched_server: _FakeAsyncServer,
) -> None:
    # Arrange
    from servicewright.adapters.grpc.errors import ServiceErrorInterceptor

    ep = GrpcEntrypoint(config=GrpcConfig(port=0), servicers=lambda _s, _c: None, map_service_errors=False)

    # Act
    await ep.bind(_make_service_ctx(FakeContainer()))

    # Assert
    chain = patched_server.create_calls[0]["interceptors"]
    assert not any(isinstance(interceptor, ServiceErrorInterceptor) for interceptor in chain)


@pytest.mark.parametrize(
    ("host_grace", "configured_grace", "expected"),
    [
        pytest.param(30.0, 5.0, 5.0, id="entrypoint-setting-shortens-the-drain"),
        pytest.param(2.0, 30.0, 2.0, id="host-allowance-bounds-the-drain"),
    ],
)
async def test__grpc_entrypoint_drain__grace_period_configured__stops_with_the_smaller_budget(
    patched_server: _FakeAsyncServer,
    host_grace: float,
    configured_grace: float,
    expected: float,
) -> None:
    # Arrange
    ep = GrpcEntrypoint(config=GrpcConfig(port=0, grace_period=configured_grace), servicers=lambda _s, _c: None)
    await ep.bind(_make_service_ctx(FakeContainer()))

    # Act
    await ep.drain(host_grace)

    # Assert
    assert patched_server.stop_calls == [expected]


async def test__grpc_entrypoint_bind__no_context_setters_given__installs_the_defaults(
    patched_server: _FakeAsyncServer,
) -> None:
    # Arrange
    from servicewright.adapters.grpc.context import get_default_context_setters

    ep = GrpcEntrypoint(config=GrpcConfig(port=0), servicers=lambda _s, _c: None)

    # Act
    await ep.bind(_make_service_ctx(FakeContainer()))

    # Assert
    unit_scope = patched_server.create_calls[0]["interceptors"][0]
    assert len(unit_scope._context_setters) == len(get_default_context_setters())


async def test__grpc_entrypoint_bind__explicit_empty_context_setters__installs_none(
    patched_server: _FakeAsyncServer,
) -> None:
    # Arrange
    ep = GrpcEntrypoint(config=GrpcConfig(port=0), servicers=lambda _s, _c: None, context_setters=[])

    # Act
    await ep.bind(_make_service_ctx(FakeContainer()))

    # Assert
    unit_scope = patched_server.create_calls[0]["interceptors"][0]
    assert unit_scope._context_setters == ()


async def test__unit_scope_interceptor__context_setter_supplied__receives_the_rpc_correlation_ids() -> None:
    # Arrange
    received: list[dict[str, Any]] = []

    class _Setter:
        def set(self, context_data: dict[str, Any]) -> Any:
            received.append(dict(context_data))
            return lambda: None

    interceptor = UnitScopeInterceptor(FakeContainer(), context_setters=[_Setter()])
    context = _FakeServicerContext([("x-request-id", "req-1"), ("x-user-id", "user-9")])

    # Act
    async with interceptor.around(_make_call(context, "/pkg.Svc/Method")):
        pass

    # Assert
    assert received[0]["request_id"] == "req-1"
    assert received[0]["user_id"] == "user-9"


async def test__unit_scope_interceptor__rpc_finishes__runs_the_setter_cleanup() -> None:
    # Arrange
    removed: list[bool] = []

    class _Setter:
        def set(self, context_data: dict[str, Any]) -> Any:
            return lambda: removed.append(True)

    interceptor = UnitScopeInterceptor(FakeContainer(), context_setters=[_Setter()])
    context = _FakeServicerContext([("x-request-id", "req-1")])

    # Act
    async with interceptor.around(_make_call(context, "/pkg.Svc/Method")):
        pass

    # Assert
    assert removed == [True]


async def test__unit_scope_interceptor__setter_raises__the_rpc_still_completes() -> None:
    # Arrange
    class _BrokenSetter:
        def set(self, context_data: dict[str, Any]) -> Any:
            raise RuntimeError("setter boom")

    interceptor = UnitScopeInterceptor(FakeContainer(), context_setters=[_BrokenSetter()])
    context = _FakeServicerContext([("x-request-id", "req-1")])
    handler_runs = 0

    # Act
    async with interceptor.around(_make_call(context, "/pkg.Svc/Method")):
        handler_runs += 1

    # Assert
    assert handler_runs == 1
