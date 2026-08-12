"""Prometheus metrics backend: instrument factory + optional exposition.

Implements the generic instrument seam (:class:`CounterProtocol` /
:class:`HistogramProtocol`): transport adapters compose their recorders from
these instruments and own their metric names — this module knows nothing about
gRPC, HTTP or any other transport.

Exposition follows the settings: when ``settings.metrics.enabled`` is true the
sink starts a standalone WSGI ``/metrics`` server on ``settings.metrics.host`` /
``port`` (for socket-less processes); server entrypoints may additionally expose
in-app metrics themselves (e.g. the FastAPI instrumentator).
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import TYPE_CHECKING, Any
from wsgiref.simple_server import make_server

from ..base import MetricsSink

try:
    from prometheus_client import REGISTRY, Counter, Histogram, make_wsgi_app
    from prometheus_client.exposition import _SilentHandler
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError("Prometheus metrics require servicewright[metrics]; install it.") from exc

if TYPE_CHECKING:
    from http.server import HTTPServer

    from prometheus_client import CollectorRegistry

    from ....core.observability.config import ObsSetupContext

logger = logging.getLogger(__name__)

_METRICS_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class PrometheusCounter:
    """Counter instrument over a ``prometheus_client.Counter``."""

    def __init__(self, counter: Any) -> None:
        self._counter = counter

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """Increment by ``amount`` for the given label values."""
        target = self._counter.labels(**labels) if labels else self._counter
        target.inc(amount)


class PrometheusHistogram:
    """Histogram instrument over a ``prometheus_client.Histogram``."""

    def __init__(self, histogram: Any) -> None:
        self._histogram = histogram

    def observe(self, value: float, **labels: str) -> None:
        """Record one observation for the given label values."""
        target = self._histogram.labels(**labels) if labels else self._histogram
        target.observe(value)


# Instruments are cached per REGISTRY, not per sink: a collector's name is
# owned by the registry it is registered in, so two sinks writing to the same
# registry must hand out the same instrument. Caching on the instance instead
# would make the second sink in a process (a second AppSpec, a second Host, or
# one resolved by name next to one passed in) re-create collectors that already
# exist and fail with "Duplicated timeseries in CollectorRegistry".
_INSTRUMENTS: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()
_INSTRUMENTS_LOCK = threading.Lock()


def _instrument_cache(registry: CollectorRegistry | None) -> dict[str, Any]:
    """Return the instrument cache belonging to ``registry`` (default when None)."""
    target = registry if registry is not None else REGISTRY
    with _INSTRUMENTS_LOCK:
        cache = _INSTRUMENTS.get(target)
        if cache is None:
            cache = {}
            _INSTRUMENTS[target] = cache
        return cache


class PrometheusMetricsSink(MetricsSink):
    """Prometheus backend: an instrument factory over the (given or default) registry.

    Instruments are cached per registry — prometheus collectors register into a
    registry by name, so a second construction with the same name raises a
    duplicate error; repeated requests (two gRPC entrypoints sharing a recorder
    shape, or a second service in the same process) return the same instrument.

    Args:
        registry: Registry for instruments and the exposition app. ``None`` (the
            default, used by the registry-resolver) means the process-global
            default ``REGISTRY`` — out-of-process emitters (DB pools, clients)
            write there, so one scrape sees the whole service.
    """

    backend = "prometheus"

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self._registry = registry
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def _instruments(self) -> dict[str, Any]:
        """The instruments already registered in this sink's registry."""
        return _instrument_cache(self._registry)

    def setup(self, ctx: ObsSetupContext) -> None:
        """Start the standalone exposition server when settings enable it."""
        metrics_settings = getattr(ctx.settings, "metrics", None)
        if metrics_settings is None or not getattr(metrics_settings, "enabled", False):
            return
        with self._lock:
            if self._server is not None:
                return
            app = make_wsgi_app(self._registry) if self._registry is not None else make_wsgi_app()
            # make_server (not start_http_server) keeps manual control over the
            # server lifecycle so shutdown() can stop it deterministically.
            self._server = make_server(
                host=metrics_settings.host,
                port=metrics_settings.port,
                app=app,
                handler_class=_SilentHandler,
            )
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
        logger.info(
            "Prometheus metrics server started",
            extra={"host": metrics_settings.host, "port": metrics_settings.port},
        )

    def shutdown(self) -> None:
        """Stop the exposition server (no-op when it never started)."""
        with self._lock:
            if self._server is None:
                return
            try:
                self._server.shutdown()
                self._server.server_close()
            finally:
                self._server = None
                if self._thread is not None:
                    self._thread.join(timeout=_METRICS_SHUTDOWN_TIMEOUT_SECONDS)
                    if self._thread.is_alive():
                        logger.warning("Prometheus metrics server thread did not exit within timeout")
                    self._thread = None
        logger.info("Prometheus metrics server stopped")

    def counter(self, name: str, description: str, label_names: tuple[str, ...] = ()) -> PrometheusCounter:
        """Mint (or reuse) a counter registered under ``name``."""
        if name not in self._instruments:
            kwargs = {"registry": self._registry} if self._registry is not None else {}
            self._instruments[name] = PrometheusCounter(Counter(name, description, list(label_names), **kwargs))
        instrument = self._instruments[name]
        if not isinstance(instrument, PrometheusCounter):
            raise TypeError(f"Metric {name!r} is already registered as a different instrument type")
        return instrument

    def histogram(
        self,
        name: str,
        description: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] | None = None,
    ) -> PrometheusHistogram:
        """Mint (or reuse) a histogram registered under ``name``."""
        if name not in self._instruments:
            kwargs: dict[str, Any] = {"registry": self._registry} if self._registry is not None else {}
            if buckets is not None:
                kwargs["buckets"] = list(buckets)
            self._instruments[name] = PrometheusHistogram(Histogram(name, description, list(label_names), **kwargs))
        instrument = self._instruments[name]
        if not isinstance(instrument, PrometheusHistogram):
            raise TypeError(f"Metric {name!r} is already registered as a different instrument type")
        return instrument


__all__ = ["PrometheusCounter", "PrometheusHistogram", "PrometheusMetricsSink"]
