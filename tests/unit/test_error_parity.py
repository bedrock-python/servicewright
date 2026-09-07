"""Both transports owe a caller the same conclusion about the same failure.

The masking rule is transport-neutral by design, so it is asserted here as one
property over both adapters rather than twice in their own modules. The way it
broke last time was a gap on one side only: an exception that was not a
``ServiceError`` reached a gRPC caller with its message while HTTP masked it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import grpc
import grpc.aio
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from grpc_server_kit.aio.interceptors import RpcCall
from pytest_lazy_fixtures import lf

from servicewright import ErrorKind, ServiceError
from servicewright.adapters.fastapi import FastApiEntrypoint, HttpConfig
from servicewright.adapters.grpc import (
    ERROR_CODE_TRAILING_METADATA,
    GRPC_STATUS_BY_KIND,
    ServiceErrorInterceptor,
    UnhandledErrorInterceptor,
)
from servicewright.core.errors import HTTP_STATUS_BY_KIND, INTERNAL_ERROR_CODE
from servicewright.core.health import HealthRegistry
from servicewright.core.spec import BootstrapContext, ServiceContext
from servicewright.testing import FakeContainer, FakeScope, FakeSettings

pytestmark = pytest.mark.unit

SECRET = "dsn=postgres://user:pw@db/ledger"


class OrderNotFoundError(ServiceError):
    kind = ErrorKind.NOT_FOUND


@dataclass(frozen=True, slots=True)
class Conclusion:
    """What one caller learns from one failed call, in transport-neutral terms.

    Attributes:
        kind: The failure category the received status maps back to.
        code: The machine-readable code the caller can branch on.
        detail: The human-readable message, if any.
        wire_text: Every byte the caller can read, for leak assertions.
    """

    kind: ErrorKind
    code: str
    detail: str
    wire_text: str


Probe = Callable[[Exception], Awaitable[Conclusion]]


def _kind_of(status: Any, table: Mapping[ErrorKind, Any]) -> ErrorKind:
    """Reverse the transport's own mapping table; both are injective."""
    return next(kind for kind, mapped in table.items() if mapped == status)


def _service_ctx() -> ServiceContext:
    bootstrap = BootstrapContext(
        settings=FakeSettings(),
        service_name="parity",
        container=FakeContainer(),
        lifecycle=object(),  # type: ignore[arg-type]
    )
    return ServiceContext(bootstrap=bootstrap, app_scope=FakeScope(), health=HealthRegistry())


class _AbortRecordingContext:
    """Servicer-context double recording abort() and raising what gRPC raises."""

    def __init__(self) -> None:
        self.aborts: list[tuple[Any, str, Any]] = []

    async def abort(self, code: Any, details: str = "", trailing_metadata: Any = None) -> None:
        self.aborts.append((code, details, trailing_metadata))
        raise grpc.aio.AbortError(details)


@pytest.fixture
def over_http() -> Probe:
    """Ask a route that raises, through the entrypoint's default handlers."""

    async def call(exc: Exception) -> Conclusion:
        router = APIRouter()

        @router.get("/pay")
        async def pay() -> None:
            raise exc

        ep = FastApiEntrypoint(config=HttpConfig(port=0), routers=(router,))
        app = await ep.build_app(_service_ctx())
        response = TestClient(app, raise_server_exceptions=False).get("/pay")

        body = response.json()
        return Conclusion(
            kind=_kind_of(response.status_code, HTTP_STATUS_BY_KIND),
            code=body["code"],
            detail=body.get("detail", ""),
            wire_text=response.text,
        )

    return call


@pytest.fixture
def over_grpc() -> Probe:
    """Ask a servicer that raises, through the interceptors the entrypoint installs."""

    async def call(exc: Exception) -> Conclusion:
        context = _AbortRecordingContext()
        rpc = RpcCall(
            method_name="/lab.Orders/Pay",
            request=b"",
            context=context,
            request_streaming=False,
            response_streaming=False,
        )

        # The order GrpcEntrypoint composes them in; test_grpc.py asserts it.
        with contextlib.suppress(grpc.aio.AbortError):
            async with UnhandledErrorInterceptor().around(rpc), ServiceErrorInterceptor().around(rpc):
                raise exc

        # Exactly one status: the net must not re-abort a mapped domain error.
        assert len(context.aborts) == 1
        (status, details, trailing) = context.aborts[0]
        return Conclusion(
            kind=_kind_of(status, GRPC_STATUS_BY_KIND),
            code=dict(trailing)[ERROR_CODE_TRAILING_METADATA],
            detail=details,
            wire_text=f"{details} {trailing}",
        )

    return call


@pytest.mark.parametrize("probe", [lf("over_http"), lf("over_grpc")], ids=["http", "grpc"])
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            ServiceError(SECRET, code="ledger_corrupted", kind=ErrorKind.INTERNAL, public=False),
            id="declared-private",
        ),
        pytest.param(RuntimeError(SECRET), id="never-declared"),
    ],
)
async def test__masked_failure__raised_over_either_transport__tells_the_caller_only_internal_error(
    probe: Probe,
    failure: Exception,
) -> None:
    # Act
    conclusion = await probe(failure)

    # Assert
    assert conclusion.kind is ErrorKind.INTERNAL
    assert conclusion.code == INTERNAL_ERROR_CODE
    assert SECRET not in conclusion.wire_text
    assert "ledger_corrupted" not in conclusion.wire_text


@pytest.mark.parametrize("probe", [lf("over_http"), lf("over_grpc")], ids=["http", "grpc"])
async def test__public_service_error__raised_over_either_transport__reaches_the_caller_intact(
    probe: Probe,
) -> None:
    # Act
    conclusion = await probe(OrderNotFoundError("no order with id 42"))

    # Assert
    assert conclusion.kind is ErrorKind.NOT_FOUND
    assert conclusion.code == "order_not_found"
    assert conclusion.detail == "no order with id 42"
