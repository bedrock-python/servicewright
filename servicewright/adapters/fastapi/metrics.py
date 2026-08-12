"""In-app Prometheus metrics via ``prometheus-fastapi-instrumentator``.

Folded from the prototype's ``_setup_metrics_instrumentator``: when enabled, the
metrics are served by the FastAPI app itself (same port) at ``/system/metrics``,
with the ``/system/*`` namespace excluded from instrumentation. A friendly
ImportError is raised if the optional extra is missing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .config import DEFAULT_METRICS_PATH, MetricsInstrumentatorConfig

if TYPE_CHECKING:
    from ._imports import FastAPI

_INSTALL_HINT_METRICS = (
    "In-app Prometheus metrics require servicewright[fastapi] "
    "(prometheus-fastapi-instrumentator); install it (or pass metrics=False)."
)


def setup_metrics_instrumentator(
    app: FastAPI,
    *,
    config: MetricsInstrumentatorConfig | None = None,
    metrics_path: str = DEFAULT_METRICS_PATH,
) -> None:
    """Instrument ``app`` and expose Prometheus metrics at ``metrics_path``.

    Raises:
        ImportError: If ``prometheus-fastapi-instrumentator`` is not installed.
    """
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT_METRICS) from exc

    cfg = config or MetricsInstrumentatorConfig()

    init_kwargs: dict[str, Any] = {
        "should_instrument_requests_inprogress": True,
        "excluded_handlers": ["/system/*"],
    } | cfg.init_kwargs

    instrument_kwargs: dict[str, Any] = dict(cfg.instrument_kwargs)

    expose_kwargs: dict[str, Any] = {
        "include_in_schema": False,
        "should_gzip": False,
        "endpoint": metrics_path,
        "tags": ["system"],
    } | cfg.expose_kwargs

    Instrumentator(**init_kwargs).instrument(app, **instrument_kwargs).expose(app, **expose_kwargs)


__all__ = ["setup_metrics_instrumentator"]
