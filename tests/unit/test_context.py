"""Unit tests for the transport-neutral context store (servicewright.core.context)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from servicewright.core.context import (
    STANDARD_PROPAGATION_HEADERS,
    bind_context,
    bind_context_values,
    current_context,
    get_context_value,
    is_safe_context_id,
    propagation_metadata,
    reset_context_value,
    set_context_value,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def bound_request() -> Iterator[None]:
    """Bind a typical request context and unbind it after the test."""
    remove = bind_context_values({"request_id": "r1", "user_id": "u1", "trace_id": "t1"})
    yield
    remove()


@pytest.mark.parametrize(
    ("default", "expected"),
    [
        pytest.param(None, None, id="implicit-none"),
        pytest.param("fallback", "fallback", id="explicit-default"),
    ],
)
def test__get_context_value__key_was_never_set__returns_the_default(default: str | None, expected: str | None) -> None:
    # Act
    value = get_context_value("never_set_key", default) if default is not None else get_context_value("never_set_key")

    # Assert
    assert value == expected


def test__set_context_value__then_read__returns_what_was_set() -> None:
    # Arrange
    token = set_context_value("roundtrip_key", "value-1")

    # Act
    value = get_context_value("roundtrip_key")

    # Assert
    assert value == "value-1"
    reset_context_value("roundtrip_key", token)


def test__reset_context_value__called_with_its_token__clears_the_value() -> None:
    # Arrange
    token = set_context_value("roundtrip_key", "value-1")

    # Act
    reset_context_value("roundtrip_key", token)

    # Assert
    assert get_context_value("roundtrip_key") is None


def test__bind_context_values__several_values__binds_all_of_them() -> None:
    # Act
    remove = bind_context_values({"request_id": "r1", "user_id": "u1"})

    # Assert
    assert (get_context_value("request_id"), get_context_value("user_id")) == ("r1", "u1")
    remove()


def test__bind_context_values__remover_called__clears_every_bound_value() -> None:
    # Arrange
    remove = bind_context_values({"request_id": "r1", "user_id": "u1"})

    # Act
    remove()

    # Assert
    assert (get_context_value("request_id"), get_context_value("user_id")) == (None, None)


def test__bind_context_values__value_is_none__does_not_bind_that_key() -> None:
    # Act
    remove = bind_context_values({"request_id": "r1", "client_ip": None})

    # Assert
    assert "client_ip" not in current_context()
    remove()


def test__bind_context_values__remover_called_twice__is_a_no_op() -> None:
    # Arrange
    remove = bind_context_values({"request_id": "r1"})
    remove()

    # Act
    remove()

    # Assert
    assert get_context_value("request_id") is None


def test__bind_context__nested_binding__restores_the_outer_value_on_exit() -> None:
    # Arrange
    outer = bind_context_values({"request_id": "outer"})

    # Act
    with bind_context(request_id="inner"):
        inner_value = get_context_value("request_id")

    # Assert
    assert inner_value == "inner"
    assert get_context_value("request_id") == "outer"
    outer()


def test__current_context__values_bound__snapshots_exactly_those(bound_request: None) -> None:
    # Act
    snapshot = current_context()

    # Assert
    assert snapshot["request_id"] == "r1"
    assert snapshot["trace_id"] == "t1"


async def test__context_store__two_concurrent_tasks__each_sees_its_own_binding() -> None:
    # Arrange
    async def unit(value: str) -> str:
        with bind_context(request_id=value):
            await asyncio.sleep(0)
            return str(get_context_value("request_id"))

    # Act
    results = await asyncio.gather(unit("task-a"), unit("task-b"))

    # Assert
    assert results == ["task-a", "task-b"]


# --------------------------------------------------------------------------- #
# propagation_metadata: current context -> outbound headers
# --------------------------------------------------------------------------- #
def test__propagation_metadata__standard_ids_bound__maps_them_to_headers(bound_request: None) -> None:
    # Act
    metadata = propagation_metadata()

    # Assert
    assert metadata == {"x-request-id": "r1", "x-user-id": "u1", "x-trace-id": "t1"}


def test__propagation_metadata__only_some_ids_bound__omits_the_rest() -> None:
    # Act
    with bind_context(request_id="r1"):
        metadata = propagation_metadata()

    # Assert
    assert metadata == {"x-request-id": "r1"}


def test__propagation_metadata__nothing_bound__returns_an_empty_mapping() -> None:
    # Act & Assert
    assert propagation_metadata() == {}


def test__propagation_metadata__custom_key_mapping__uses_it(bound_request: None) -> None:
    # Act
    with bind_context(job_id="job-9"):
        metadata = propagation_metadata({"job_id": "x-job-id"})

    # Assert
    assert metadata == {"x-job-id": "job-9"}


def test__standard_propagation_headers__inspected__mirror_what_the_transports_extract() -> None:
    # Assert
    assert set(STANDARD_PROPAGATION_HEADERS.values()) == {
        "x-request-id",
        "x-user-id",
        "x-tenant-id",
        "x-trace-id",
    }


# --------------------------------------------------------------------------- #
# is_safe_context_id: correlation-id hygiene
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("123e4567-e89b-12d3-a456-426614174000", id="uuid"),
        pytest.param("user_42", id="underscored"),
        pytest.param("a/b+c=d:e f.g", id="punctuation-used-by-real-ids"),
    ],
)
def test__is_safe_context_id__ordinary_identifier__is_accepted(value: str) -> None:
    # Act & Assert
    assert is_safe_context_id(value) is True


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("x" * 257, id="overlong"),
        pytest.param("bad\nvalue", id="newline-would-forge-a-log-line"),
        pytest.param("bad{value}", id="unexpected-punctuation"),
    ],
)
def test__is_safe_context_id__log_unsafe_value__is_rejected(value: str) -> None:
    # Act & Assert
    assert is_safe_context_id(value) is False
