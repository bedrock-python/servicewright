"""Self-contained configuration for :class:`GrpcEntrypoint`.

The :class:`AppSpec` stays transport-neutral, so the gRPC entrypoint owns its
own configuration. ``GrpcConfig`` deliberately exposes the full
``grpc_server_kit.protocols.GrpcServerSettingsProtocol`` surface (host, port,
keepalive, flow-control, SSL, ...) so it can be handed straight to
``create_async_grpc_server`` / ``bind_server_port`` without any service settings.
The other direction — building it from the kit's own settings model — is
:meth:`GrpcConfig.from_settings`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...core.constants import DEFAULT_DRAIN_GRACE_SECONDS

if TYPE_CHECKING:
    from grpc_server_kit.protocols import GrpcServerSettingsProtocol

# Default port / health service name (mirrors the gRPC prototype constants).
DEFAULT_GRPC_HOST = "0.0.0.0"
DEFAULT_GRPC_PORT = 50051

# Default to the Host's full drain allowance: making ``grace_period`` live must
# not silently shorten the drain of services that never set it.
DEFAULT_GRACE_PERIOD_SECONDS = DEFAULT_DRAIN_GRACE_SECONDS
DEFAULT_HEALTH_REFRESH_INTERVAL_SECONDS = 5.0

# The wire name of the standard health service. It is a fixed protobuf service
# name, not a tunable: it is advertised in the reflection listing so `grpcurl`
# can discover the health API.
DEFAULT_HEALTH_SERVICE_NAME = "grpc.health.v1.Health"

# Server tuning defaults (mirrors grpc_server_kit.constants so the config is
# fully self-contained and does not require the kit at import time).
DEFAULT_MAX_SEND_MESSAGE_LENGTH = 4 * 1024 * 1024
DEFAULT_MAX_RECEIVE_MESSAGE_LENGTH = 4 * 1024 * 1024
DEFAULT_MAX_METADATA_SIZE = 16 * 1024
DEFAULT_KEEPALIVE_TIME_MS = 7_200_000
DEFAULT_KEEPALIVE_TIMEOUT_MS = 20_000
DEFAULT_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS = 300_000
DEFAULT_HTTP2_MAX_PINGS_WITHOUT_DATA = 2
DEFAULT_INITIAL_WINDOW_SIZE = 65_535


@dataclass(slots=True)
class GrpcConfig:
    """Configuration for a gRPC server entrypoint.

    Satisfies ``grpc_server_kit.protocols.GrpcServerSettingsProtocol`` so it can
    be consumed directly by the grpc-server-kit primitives.
    """

    host: str = DEFAULT_GRPC_HOST
    port: int = DEFAULT_GRPC_PORT

    grace_period: float = DEFAULT_GRACE_PERIOD_SECONDS
    """Seconds in-flight RPCs may finish in after intake stops, on drain.

    This is the entrypoint's own drain budget. The Host also allots a grace when
    it calls ``drain(grace)`` and aborts the drain shortly after that allowance
    expires, so the effective budget is ``min(host_grace, grace_period)``:
    lowering this value shortens shutdown, raising it above the Host's allowance
    has no effect.
    """

    enable_reflection: bool = False
    """Serve ``grpc.reflection.v1alpha.ServerReflection`` (default: off).

    Reflection lets tools such as ``grpcurl`` resolve symbols from the process's
    descriptor pool without a local ``.proto``. It is a development convenience
    and is unauthenticated unless an interceptor of yours authenticates it, so
    it is opt-in: turn it on for local/staging, leave it off in production.
    """

    enable_channelz: bool = False
    """Serve ``grpc.channelz.v1.Channelz`` (default: off).

    Channelz is gRPC's debug/introspection service. It exposes ``GetServers`` /
    ``GetServerSockets`` / ``GetSocket`` / ``GetTopChannels`` on the SAME port as
    production traffic, revealing connected peers' remote addresses and
    per-socket call and byte counters. It is unauthenticated unless an
    interceptor of yours authenticates it, so it is opt-in.
    """

    health_service_names: tuple[str, ...] = ()
    """Concrete gRPC service names to report health for, besides the overall one.

    The health servicer always reports the overall (empty-string) name — the one
    a plain ``readinessProbe: {grpc: {port: ...}}`` checks. List your own
    services here (e.g. ``("my.pkg.Orders",)``) to also answer
    ``Check(service="my.pkg.Orders")``; unlisted names are answered NOT_FOUND by
    the standard health servicer.
    """

    health_refresh_interval: float = DEFAULT_HEALTH_REFRESH_INTERVAL_SECONDS
    """Seconds between re-evaluations of readiness onto the health service.

    The gRPC health service is a push API: something must re-evaluate the
    :class:`~servicewright.core.health.HealthRegistry` and push the result. The
    entrypoint polls at this interval while serving, so a failing check flips
    the health service to NOT_SERVING within roughly one interval. Set ``0`` to
    disable polling (the status is then only pushed once at startup).
    """

    # Extra service names exposed via reflection (in addition to the health
    # service and the reflection service itself). Typically your own servicers.
    reflection_service_names: list[str] | None = None

    # --- GrpcServerSettingsProtocol surface (server tuning) -----------------
    max_concurrent_rpcs: int | None = None

    keepalive_time_ms: int = DEFAULT_KEEPALIVE_TIME_MS
    keepalive_timeout_ms: int = DEFAULT_KEEPALIVE_TIMEOUT_MS
    keepalive_permit_without_calls: bool = True
    http2_min_recv_ping_interval_without_data_ms: int = DEFAULT_HTTP2_MIN_RECV_PING_INTERVAL_WITHOUT_DATA_MS
    http2_max_pings_without_data: int = DEFAULT_HTTP2_MAX_PINGS_WITHOUT_DATA

    max_send_message_length: int = DEFAULT_MAX_SEND_MESSAGE_LENGTH
    max_receive_message_length: int = DEFAULT_MAX_RECEIVE_MESSAGE_LENGTH
    max_metadata_size: int = DEFAULT_MAX_METADATA_SIZE

    initial_stream_window_size: int = DEFAULT_INITIAL_WINDOW_SIZE
    initial_connection_window_size: int = DEFAULT_INITIAL_WINDOW_SIZE

    max_connection_idle_ms: int | None = None
    max_connection_age_ms: int | None = None
    max_connection_age_grace_ms: int | None = None

    compression_algorithm: str | None = None

    # --- GrpcSslSettingsProtocol surface ------------------------------------
    ssl_enabled: bool = False
    ssl_cert_file: str | None = None
    ssl_key_file: str | None = None
    ssl_ca_file: str | None = None
    ssl_client_auth: bool = False
    ssl_max_cert_size: int | None = None

    def __post_init__(self) -> None:
        """Reject timings the gRPC stack cannot honour."""
        if self.grace_period < 0:
            raise ValueError(f"grace_period must be non-negative, got {self.grace_period}")
        if self.health_refresh_interval < 0:
            raise ValueError(f"health_refresh_interval must be non-negative, got {self.health_refresh_interval}")

    @classmethod
    def from_settings(
        cls,
        settings: GrpcServerSettingsProtocol,
        *,
        health_service_names: tuple[str, ...] = (),
        health_refresh_interval: float = DEFAULT_HEALTH_REFRESH_INTERVAL_SECONDS,
        reflection_service_names: list[str] | None = None,
    ) -> GrpcConfig:
        """Build the config from a grpc-server-kit settings object.

        ``settings`` is anything satisfying the kit's
        ``GrpcServerSettingsProtocol`` — its ``BaseGrpcServerSettings`` pydantic
        model, its stdlib ``GrpcServerConfig``, a model of your own — and every
        field the protocol declares is read from it, defaults included: the kit
        binds ``[::]`` and drains for 5 s where this class binds ``0.0.0.0`` and
        drains for 30 s, and the kit's values win. The mapping lives here so a
        field added to this class cannot go missing from a service's copy of it.

        The keyword arguments are the three fields no settings object carries.
        ``health_service_names`` and ``reflection_service_names`` name your own
        servicers, which is code, not environment. ``health_refresh_interval``
        is deliberately not derived from the kit's ``health.cache_ttl``: the two
        agree while positive and disagree at zero, where ``cache_ttl=0`` means
        "re-check on every probe" and ``health_refresh_interval=0`` means "never
        re-evaluate". The kit's ``metrics_enabled`` has no field here either;
        that switch is ``GrpcEntrypoint(enable_metrics=...)``.
        """
        return cls(
            host=settings.host,
            port=settings.port,
            grace_period=settings.grace_period,
            enable_reflection=settings.enable_reflection,
            enable_channelz=settings.enable_channelz,
            health_service_names=health_service_names,
            health_refresh_interval=health_refresh_interval,
            reflection_service_names=reflection_service_names,
            max_concurrent_rpcs=settings.max_concurrent_rpcs,
            keepalive_time_ms=settings.keepalive_time_ms,
            keepalive_timeout_ms=settings.keepalive_timeout_ms,
            keepalive_permit_without_calls=settings.keepalive_permit_without_calls,
            http2_min_recv_ping_interval_without_data_ms=settings.http2_min_recv_ping_interval_without_data_ms,
            http2_max_pings_without_data=settings.http2_max_pings_without_data,
            max_send_message_length=settings.max_send_message_length,
            max_receive_message_length=settings.max_receive_message_length,
            max_metadata_size=settings.max_metadata_size,
            initial_stream_window_size=settings.initial_stream_window_size,
            initial_connection_window_size=settings.initial_connection_window_size,
            max_connection_idle_ms=settings.max_connection_idle_ms,
            max_connection_age_ms=settings.max_connection_age_ms,
            max_connection_age_grace_ms=settings.max_connection_age_grace_ms,
            compression_algorithm=settings.compression_algorithm,
            ssl_enabled=settings.ssl_enabled,
            ssl_cert_file=settings.ssl_cert_file,
            ssl_key_file=settings.ssl_key_file,
            ssl_ca_file=settings.ssl_ca_file,
            ssl_client_auth=settings.ssl_client_auth,
            ssl_max_cert_size=settings.ssl_max_cert_size,
        )

    @property
    def address(self) -> str:
        """Return the ``host:port`` bind address."""
        return f"{self.host}:{self.port}"
