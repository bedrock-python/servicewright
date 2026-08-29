"""The concrete backends are importable from ``servicewright.adapters.observability``.

Regression cover for issue #16: the sinks the docs tell you to construct were
only reachable through the private ``_metrics`` / ``_tracing`` / ``_errors`` /
``_logging`` packages. The public path resolves them lazily, so the package
stays importable with no extra installed.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from unittest.mock import patch

import pytest

from servicewright.adapters import observability as observability_pkg

pytestmark = pytest.mark.unit

_BACKENDS = {
    "OtelTracingSink": "servicewright.adapters.observability._tracing.otel",
    "PrometheusMetricsSink": "servicewright.adapters.observability._metrics.prometheus",
    "SentryErrorTrackingSink": "servicewright.adapters.observability._errors.sentry",
    "StdlibLoggingSink": "servicewright.adapters.observability._logging.stdlib",
    "StructlogLoggingSink": "servicewright.adapters.observability._logging.structlog",
}


@pytest.mark.parametrize(("name", "module_name"), sorted(_BACKENDS.items()))
def test__observability_package__backend_read_from_the_public_path__is_the_backend_class(
    name: str, module_name: str
) -> None:
    # Act
    public = getattr(observability_pkg, name)

    # Assert
    assert public is getattr(importlib.import_module(module_name), name)
    assert name in observability_pkg.__all__


def test__observability_package__dir__lists_the_backends() -> None:
    assert set(_BACKENDS) <= set(dir(observability_pkg))


def test__observability_package__unknown_attribute__raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="has no attribute 'DatadogMetricsSink'"):
        observability_pkg.DatadogMetricsSink  # noqa: B018


def test__observability_package__extra_missing__raises_the_install_hint_for_that_backend_only() -> None:
    # Arrange: the sentry backend is imported afresh with its SDK unavailable
    with patch.dict("sys.modules", {"sentry_sdk": None}):
        sys.modules.pop(_BACKENDS["SentryErrorTrackingSink"], None)

        # Act / Assert
        with pytest.raises(ImportError, match=r"servicewright\[sentry\]"):
            observability_pkg.SentryErrorTrackingSink  # noqa: B018
        assert observability_pkg.StdlibLoggingSink is not None


def test__observability_package__imported__pulls_in_no_sdk() -> None:
    # Arrange: a fresh interpreter, so nothing imported by the test session leaks in
    code = (
        "import sys; import servicewright.adapters.observability; "
        "leaked = sorted(m for m in sys.modules "
        "if m.split('.')[0] in {'sentry_sdk', 'prometheus_client', 'structlog', 'opentelemetry'}); "
        "print(leaked); sys.exit(1 if leaked else 0)"
    )

    # Act
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    # Assert
    assert result.returncode == 0, result.stdout + result.stderr
