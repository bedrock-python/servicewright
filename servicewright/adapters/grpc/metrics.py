"""gRPC server request metrics: the recorder and its frozen wire names.

The gRPC adapter OWNS its metric vocabulary (the kernel is transport-neutral
and exposes only generic instruments). These names and label sets are a wire
contract with dashboards and alerts — do not rename.

The recorder composes backend-agnostic instruments minted from the app's
metrics sink, so it works identically over prometheus, datadog or any custom
backend (and records into no-ops when metrics are disabled).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...core.observability.naming import make_metric_name

if TYPE_CHECKING:
    from ...core.observability.sinks import MetricsSinkProtocol

GRPC_REQUESTS_TOTAL = "grpc_requests_total"
GRPC_REQUESTS_TOTAL_LABELS = ("service", "method", "status", "grpc_code")

GRPC_REQUEST_DURATION_SECONDS = "grpc_request_duration_seconds"
GRPC_REQUEST_DURATION_LABELS = ("service", "method")

DEFAULT_GRPC_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class GrpcServerMetricsRecorder:
    """The frozen 5-arg gRPC-server request recorder over generic instruments."""

    def __init__(
        self,
        sink: MetricsSinkProtocol,
        *,
        prefix: str | None = None,
        buckets: tuple[float, ...] = DEFAULT_GRPC_BUCKETS,
    ) -> None:
        self._requests_total = sink.counter(
            make_metric_name(GRPC_REQUESTS_TOTAL, prefix),
            "Total number of gRPC requests",
            GRPC_REQUESTS_TOTAL_LABELS,
        )
        self._request_duration = sink.histogram(
            make_metric_name(GRPC_REQUEST_DURATION_SECONDS, prefix),
            "gRPC request duration in seconds",
            GRPC_REQUEST_DURATION_LABELS,
            buckets=buckets,
        )

    def record_request(self, service: str, method: str, status: str, grpc_code: str, duration: float) -> None:
        """Record one served RPC."""
        self._requests_total.inc(service=service, method=method, status=status, grpc_code=grpc_code)
        self._request_duration.observe(duration, service=service, method=method)


__all__ = [
    "DEFAULT_GRPC_BUCKETS",
    "GRPC_REQUESTS_TOTAL",
    "GRPC_REQUESTS_TOTAL_LABELS",
    "GRPC_REQUEST_DURATION_LABELS",
    "GRPC_REQUEST_DURATION_SECONDS",
    "GrpcServerMetricsRecorder",
]
