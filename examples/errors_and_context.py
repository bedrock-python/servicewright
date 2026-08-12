"""The transport-neutral error taxonomy and the correlation store.

This example shows how to:

1. Raise typed business errors: a `ServiceError` subclass sets `kind` (and the
   machine `code` auto-derives from the class name), so ONE error renders as
   404 over HTTP and `NOT_FOUND` over gRPC without the business code knowing
   which transport is serving it.
2. Render a failure two ways from the same `ErrorInfo`: the default
   `ProblemDetailsRenderer` (RFC 9457 `application/problem+json`) and a custom
   renderer owning its own envelope -- one class swaps the wire format for
   every default exception handler at once.
3. Mask a `public=False` error with `mask_private_error`: the client sees a
   generic `internal_error` 500, while the real code and params stay available
   for the log.
4. Bind correlation identifiers once with `bind_context`, read them anywhere in
   the async call tree with `get_context_value`, and hand them to outbound
   calls as headers with `propagation_metadata()`.

Run with: `python examples/errors_and_context.py`. It needs no transport, no
server and no dependencies beyond the pure kernel, and exits 0.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from servicewright import (
    ErrorInfo,
    ErrorKind,
    ProblemDetailsRenderer,
    RenderedError,
    ServiceError,
    bind_context,
    current_context,
    get_context_value,
    is_safe_context_id,
    mask_private_error,
    propagation_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

PROBLEM_TYPE_BASE = "https://errors.example.com/orders"
REQUEST_ID = "01J9Z6P2QF-req-7781"


class OrderMissingError(ServiceError):
    """Public business error; `code` auto-derives to `order_missing`."""

    kind = ErrorKind.NOT_FOUND


class LedgerDriftError(ServiceError):
    """An internal invariant breach: real cause logged, never shown to clients."""

    kind = ErrorKind.CONFLICT
    code = "ledger_drift"
    public = False


class CompactEnvelopeRenderer:
    """A custom `HttpErrorRendererProtocol` owning the whole wire format.

    Every default handler (validation, HTTPException, ServiceError, deadline,
    unhandled) renders through the configured renderer, so this one class also
    localizes messages from `code` + `params` for the entire surface.
    """

    def __init__(self, messages: Mapping[str, str]) -> None:
        self._messages = dict(messages)

    def render(self, info: ErrorInfo) -> RenderedError:
        return RenderedError(
            status_code=info.http_status,
            body={
                "ok": False,
                "error": {
                    "code": info.code,
                    "message": self._messages.get(info.code, info.detail or "Request failed."),
                    "params": dict(info.params),
                },
            },
            media_type="application/json",
        )


def show(label: str, rendered: RenderedError) -> None:
    """Print one rendered response the way a client would receive it."""
    print(f"[{label}] {rendered.status_code} {rendered.media_type}")
    print(f"[{label}] {json.dumps(rendered.body, sort_keys=True)}")


async def charge_card(amount: str) -> None:
    """A nested callee: it reads the correlation context it never was passed."""
    print(f"[payments] charging {amount} for user_id={get_context_value('user_id')!r}")


async def main() -> None:
    problem_details = ProblemDetailsRenderer(type_base=PROBLEM_TYPE_BASE)
    envelope = CompactEnvelopeRenderer({"order_missing": "We could not find that order."})

    print("--- one error, normalized once ---")
    error = OrderMissingError("No such order.", params={"order_id": "999"})
    info = ErrorInfo.from_service_error(error)
    print(f"[taxonomy] raised {type(error).__name__}: code={info.code!r} kind={info.kind} public={info.public}")
    print(f"[taxonomy] http_status={info.http_status} (a gRPC transport maps the same kind to NOT_FOUND)")

    print("\n--- rendered by the default RFC 9457 renderer ---")
    show("problem", problem_details.render(info))

    print("\n--- the same ErrorInfo through a custom renderer ---")
    show("envelope", envelope.render(info))

    print("\n--- a public=False error is masked before it ever reaches a renderer ---")
    private = LedgerDriftError("Ledger totals drifted by 0.03", params={"account": "acc-7"})
    truth = ErrorInfo.from_service_error(private)
    masked = mask_private_error(truth)
    print(f"[log]      logged truth: code={truth.code!r} kind={truth.kind} params={dict(truth.params)}")
    print(f"[masked]   client view:  code={masked.code!r} kind={masked.kind} params={dict(masked.params)}")
    show("masked", problem_details.render(masked))

    print("\n--- every kind already has a transport status ---")
    for kind in ErrorKind:
        status = ErrorInfo(kind=kind, code="x").http_status
        print(f"[kinds]    {kind.value:<20} -> HTTP {status}")

    print("\n--- correlation: bind once, read anywhere in the task tree ---")
    with bind_context(request_id=REQUEST_ID, user_id="usr-42", tenant_id="acme", trace_id=None):
        print(f"[handler]  get_context_value('request_id') -> {get_context_value('request_id')!r}")
        print(f"[handler]  current_context() -> {current_context()}")
        # `trace_id=None` above was skipped, not bound -- absent means absent.
        print(f"[handler]  get_context_value('trace_id') -> {get_context_value('trace_id')!r}")
        await charge_card("19.99")

        print("\n--- outbound propagation: the correlation chain continues downstream ---")
        print(f"[outbound] propagation_metadata() -> {propagation_metadata()}")
        custom: dict[str, Any] = propagation_metadata({"request_id": "x-correlation-id", "tenant_id": "x-tenant"})
        print(f"[outbound] propagation_metadata(custom keys) -> {custom}")

    print(f"\n[after]    the block reset everything: request_id -> {get_context_value('request_id')!r}")

    print("\n--- identifiers are semi-trusted input, so transports filter them ---")
    for candidate in (REQUEST_ID, "req\nid: injected-log-line", "x" * 300):
        print(f"[guard]    is_safe_context_id({candidate[:22]!r}...) -> {is_safe_context_id(candidate)}")


if __name__ == "__main__":
    asyncio.run(main())
