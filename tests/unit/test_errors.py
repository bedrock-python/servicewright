"""Unit tests for the transport-neutral error taxonomy (servicewright.core.errors)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from servicewright.core.errors import (
    HTTP_STATUS_BY_KIND,
    INTERNAL_ERROR_CODE,
    ErrorInfo,
    ErrorKind,
    ProblemDetailsRenderer,
    ServiceError,
    mask_private_error,
    status_title,
    to_json_safe,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# ServiceError taxonomy
# --------------------------------------------------------------------------- #
class UserMissingError(ServiceError):
    kind = ErrorKind.NOT_FOUND


class QuotaExhaustedError(ServiceError):
    kind = ErrorKind.TOO_MANY_REQUESTS
    code = "quota_exhausted_v2"


def test__service_error__raised_bare__defaults_to_the_internal_kind() -> None:
    exc = ServiceError("boom")
    assert exc.kind is ErrorKind.INTERNAL
    assert exc.code == "service"
    assert exc.public is True


def test__service_error_subclass__no_explicit_code__derives_it_from_the_class_name() -> None:
    exc = UserMissingError("no such user")
    assert exc.code == "user_missing"
    assert exc.kind is ErrorKind.NOT_FOUND
    assert exc.detail == "no such user"


def test__service_error_subclass__explicit_code__wins_over_the_derived_one() -> None:
    assert QuotaExhaustedError().code == "quota_exhausted_v2"


def test__service_error__per_instance_arguments__override_the_class_attributes() -> None:
    exc = UserMissingError(code="gone", kind=ErrorKind.CONFLICT, params={"id": 1}, public=False)
    assert exc.code == "gone"
    assert exc.kind is ErrorKind.CONFLICT
    assert exc.params == {"id": 1}
    assert exc.public is False


def test__error_kind__every_member__maps_to_an_http_status() -> None:
    assert set(HTTP_STATUS_BY_KIND) == set(ErrorKind)


# --------------------------------------------------------------------------- #
# ErrorInfo normalization + masking
# --------------------------------------------------------------------------- #
def test__error_info_from_service_error__built__carries_every_field() -> None:
    exc = UserMissingError("gone", params={"user_id": "42"})
    info = ErrorInfo.from_service_error(exc)
    assert (info.kind, info.code, info.detail, info.public) == (ErrorKind.NOT_FOUND, "user_missing", "gone", True)
    assert info.params == {"user_id": "42"}
    assert info.http_status == 404


def test__error_info_http_status__status_override_set__wins_over_the_kind() -> None:
    info = ErrorInfo(kind=ErrorKind.INVALID, code="validation_error", status_override=422)
    assert info.http_status == 422


def test__mask_private_error__public_error__passes_it_through() -> None:
    info = ErrorInfo(kind=ErrorKind.NOT_FOUND, code="user_missing")
    assert mask_private_error(info) is info


def test__mask_private_error__private_error__collapses_it_to_a_generic_internal() -> None:
    info = ErrorInfo(kind=ErrorKind.CONFLICT, code="secret_leak", detail="secret", params={"x": 1}, public=False)
    masked = mask_private_error(info)
    assert (masked.kind, masked.code, masked.detail, masked.public) == (
        ErrorKind.INTERNAL,
        INTERNAL_ERROR_CODE,
        None,
        True,
    )
    assert masked.params == {}
    assert masked.http_status == 500


# --------------------------------------------------------------------------- #
# ProblemDetailsRenderer (RFC 9457)
# --------------------------------------------------------------------------- #
def test__problem_details_renderer__public_error__produces_an_rfc_9457_document() -> None:
    info = ErrorInfo(kind=ErrorKind.NOT_FOUND, code="user_missing", detail="gone", params={"user_id": "42"})
    rendered = ProblemDetailsRenderer().render(info)
    assert rendered.status_code == 404
    assert rendered.media_type == "application/problem+json"
    assert rendered.body == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "code": "user_missing",
        "detail": "gone",
        "params": {"user_id": "42"},
    }


def test__problem_details_renderer__no_detail_or_params__omits_those_members() -> None:
    rendered = ProblemDetailsRenderer().render(ErrorInfo(kind=ErrorKind.INTERNAL, code=INTERNAL_ERROR_CODE))
    assert rendered.body == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "code": "internal_error",
    }


def test__problem_details_renderer__type_base_configured__builds_the_type_uri() -> None:
    renderer = ProblemDetailsRenderer(type_base="https://errors.example.com/")
    rendered = renderer.render(ErrorInfo(kind=ErrorKind.CONFLICT, code="duplicate"))
    assert rendered.body["type"] == "https://errors.example.com/duplicate"


def test__problem_details_renderer__error_carries_headers__forwards_them() -> None:
    info = ErrorInfo(kind=ErrorKind.TOO_MANY_REQUESTS, code="rate_limited", headers={"Retry-After": "3"})
    assert ProblemDetailsRenderer().render(info).headers == {"Retry-After": "3"}


# --------------------------------------------------------------------------- #
# Regression cover: rendering must be total
# --------------------------------------------------------------------------- #
class _Opaque:
    """A value with no JSON representation at all."""

    def __str__(self) -> str:
        return "opaque-value"


@pytest.fixture
def renderer() -> ProblemDetailsRenderer:
    return ProblemDetailsRenderer()


@pytest.fixture
def recursive_params() -> dict[str, Any]:
    params: dict[str, Any] = {"name": "loop"}
    params["self"] = params
    return params


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(
            uuid.UUID("11111111-2222-3333-4444-555555555555"), "11111111-2222-3333-4444-555555555555", id="uuid"
        ),
        pytest.param(datetime(2026, 8, 11, 12, 30, tzinfo=UTC), "2026-08-11 12:30:00+00:00", id="datetime"),
        pytest.param(Decimal("19.99"), "19.99", id="decimal"),
        pytest.param(_Opaque(), "opaque-value", id="arbitrary-object"),
        pytest.param(float("nan"), "nan", id="not-a-number"),
        pytest.param(float("inf"), "inf", id="infinity"),
        pytest.param(ErrorKind.NOT_FOUND, "not_found", id="enum"),
        pytest.param({1, 2}, [1, 2], id="set"),
        pytest.param(("a", "b"), ["a", "b"], id="tuple"),
        pytest.param(3, 3, id="int-passes-through"),
        pytest.param("plain", "plain", id="str-passes-through"),
        pytest.param(None, None, id="none-passes-through"),
    ],
)
def test__to_json_safe__unserializable_value__is_coerced_to_a_json_primitive(
    value: Any,
    expected: Any,
) -> None:
    # Act
    result = to_json_safe(value)

    # Assert
    assert result == expected


def test__to_json_safe__non_string_mapping_key__is_stringified() -> None:
    # Act
    result = to_json_safe({1: "one"})

    # Assert
    assert result == {"1": "one"}


def test__to_json_safe__self_referencing_structure__is_cut_instead_of_recursing(
    recursive_params: dict[str, Any],
) -> None:
    # Act
    result = to_json_safe(recursive_params)

    # Assert
    assert result == {"name": "loop", "self": "<recursive>"}


def test__problem_details_renderer__unserializable_params__still_renders_the_intended_status(
    renderer: ProblemDetailsRenderer,
) -> None:
    # Arrange
    user_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    info = ErrorInfo(kind=ErrorKind.NOT_FOUND, code="user_missing", params={"user_id": user_id})

    # Act
    rendered = renderer.render(info)

    # Assert
    assert rendered.status_code == 404
    assert rendered.body["params"] == {"user_id": str(user_id)}


def test__problem_details_renderer__unserializable_params__produces_a_serializable_body(
    renderer: ProblemDetailsRenderer,
) -> None:
    # Arrange
    info = ErrorInfo(
        kind=ErrorKind.INVALID,
        code="bad_input",
        params={"id": uuid.uuid4(), "at": datetime.now(UTC), "amount": Decimal("1.5"), "tags": {"a"}},
    )

    # Act
    body = renderer.render(info).body

    # Assert
    assert json.loads(json.dumps(body, allow_nan=False))["code"] == "bad_input"


@pytest.mark.parametrize(
    ("status", "expected_title"),
    [
        pytest.param(404, "Not Found", id="registered-4xx"),
        pytest.param(451, "Unavailable For Legal Reasons", id="registered-rare"),
        pytest.param(499, "Client Error", id="unregistered-4xx-client-closed"),
        pytest.param(520, "Server Error", id="unregistered-5xx-cloudflare"),
        pytest.param(299, "Error", id="unregistered-other"),
    ],
)
def test__status_title__any_status_code__returns_a_title_without_raising(
    status: int,
    expected_title: str,
) -> None:
    # Act
    title = status_title(status)

    # Assert
    assert title == expected_title


def test__problem_details_renderer__non_iana_status_override__renders_that_status(
    renderer: ProblemDetailsRenderer,
) -> None:
    # Arrange
    info = ErrorInfo(kind=ErrorKind.INVALID, code="client_closed", status_override=499)

    # Act
    rendered = renderer.render(info)

    # Assert
    assert rendered.status_code == 499
    assert rendered.body["title"] == "Client Error"


# --------------------------------------------------------------------------- #
# Regression cover: one authoritative attribute set
# --------------------------------------------------------------------------- #
def test__service_error__subclass_relying_on_derivation__reports_the_resolved_code() -> None:
    # Arrange
    class UserMissingError(ServiceError):
        kind = ErrorKind.NOT_FOUND

    # Act
    exc = UserMissingError("no such user")

    # Assert
    assert exc.code == "user_missing"


def test__service_error__constructed_as_private__reports_public_false() -> None:
    # Act
    exc = ServiceError("boom", kind=ErrorKind.CONFLICT, public=False)

    # Assert
    assert exc.public is False


def test__service_error__kind_passed_per_instance__reports_that_kind() -> None:
    # Act
    exc = ServiceError("boom", kind=ErrorKind.CONFLICT)

    # Assert
    assert exc.kind is ErrorKind.CONFLICT


def test__service_error__no_params_given__reports_an_empty_mapping() -> None:
    # Act
    exc = ServiceError("boom")

    # Assert
    assert exc.params == {}


def test__service_error__params_given__does_not_share_the_callers_mapping() -> None:
    # Arrange
    params = {"id": 1}

    # Act
    exc = ServiceError("boom", params=params)
    params["id"] = 2

    # Assert
    assert exc.params == {"id": 1}


def test__error_info_from_service_error__subclass_with_derived_code__carries_it_through() -> None:
    # Arrange
    class QuotaExceededError(ServiceError):
        kind = ErrorKind.TOO_MANY_REQUESTS

    # Act
    info = ErrorInfo.from_service_error(QuotaExceededError("slow down", params={"retry_after": 30}))

    # Assert
    assert (info.kind, info.code, info.params) == (ErrorKind.TOO_MANY_REQUESTS, "quota_exceeded", {"retry_after": 30})


def test__mask_private_error__private_error_with_details__drops_every_leakable_field() -> None:
    # Arrange
    info = ErrorInfo(
        kind=ErrorKind.CONFLICT,
        code="secret_leak",
        detail="internal detail",
        params={"query": "SELECT 1"},
        public=False,
        status_override=418,
        headers={"X-Debug": "yes"},
    )

    # Act
    masked = mask_private_error(info)

    # Assert
    assert (masked.detail, dict(masked.params), masked.status_override, masked.headers) == (None, {}, None, None)
