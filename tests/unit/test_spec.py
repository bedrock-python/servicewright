"""Unit tests for the declarative spec dataclasses (AppSpec and its contexts)."""

from __future__ import annotations

from dataclasses import is_dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from servicewright import AppSpec, BootstrapContext, ServiceContext
from servicewright.core.constants import DEFAULT_CLEANUP_TIMEOUT_SECONDS, DEFAULT_DRAIN_GRACE_SECONDS
from servicewright.core.health import HealthRegistry

pytestmark = pytest.mark.unit


@pytest.fixture
def bootstrap() -> MagicMock:
    context = MagicMock()
    context.service_name = "test-service"
    return context


@pytest.fixture
def service_context(bootstrap: MagicMock) -> ServiceContext[Any, Any]:
    return ServiceContext(bootstrap=bootstrap, app_scope=MagicMock(), health=HealthRegistry())


def test__bootstrap_context__constructed__is_a_dataclass_carrying_the_service_name() -> None:
    # Act
    context = BootstrapContext(
        settings=MagicMock(),
        service_name="test-service",
        container=MagicMock(),
        lifecycle=MagicMock(),
    )

    # Assert
    assert is_dataclass(context)
    assert context.service_name == "test-service"


@pytest.mark.parametrize(
    ("attribute", "bootstrap_attribute"),
    [
        pytest.param("settings", "settings", id="settings"),
        pytest.param("service_name", "service_name", id="service-name"),
        pytest.param("container", "container", id="container"),
        pytest.param("lifecycle", "lifecycle", id="lifecycle"),
    ],
)
def test__service_context__built_from_a_bootstrap__proxies_its_attributes(
    service_context: ServiceContext[Any, Any],
    bootstrap: MagicMock,
    attribute: str,
    bootstrap_attribute: str,
) -> None:
    # Act
    value = getattr(service_context, attribute)

    # Assert
    assert value == getattr(bootstrap, bootstrap_attribute)


def test__service_context__constructed__keeps_its_own_scope_and_health(
    service_context: ServiceContext[Any, Any],
) -> None:
    # Assert
    assert is_dataclass(service_context)
    assert isinstance(service_context.health, HealthRegistry)


def test__app_spec__constructed_with_overrides__keeps_them() -> None:
    # Arrange
    create_container = MagicMock()
    lifecycle = MagicMock()
    observability = MagicMock()

    # Act
    spec = AppSpec(
        service_name="test-service",
        create_container=create_container,
        lifecycle=lifecycle,
        observability=observability,
    )

    # Assert
    assert is_dataclass(spec)
    assert spec.create_container == create_container
    assert spec.lifecycle is lifecycle
    assert spec.observability is observability


def test__app_spec__only_required_fields__fills_the_documented_defaults() -> None:
    # Act
    spec: AppSpec[Any, Any] = AppSpec(service_name="svc", create_container=MagicMock())

    # Assert
    assert isinstance(spec.health, HealthRegistry)
    assert spec.warmers == []
    assert spec.warmers_factory is None


def test__app_spec__only_required_fields__uses_the_default_shutdown_budgets() -> None:
    # Act
    spec: AppSpec[Any, Any] = AppSpec(service_name="svc", create_container=MagicMock())

    # Assert
    assert spec.drain_grace_seconds == DEFAULT_DRAIN_GRACE_SECONDS
    assert spec.cleanup_timeout_seconds == DEFAULT_CLEANUP_TIMEOUT_SECONDS
