# Errors

A multi-entrypoint runtime needs one business error to render correctly over *any* transport: 404
over HTTP, `NOT_FOUND` over gRPC, with a single rule for what may reach the client. That is three
replaceable pieces — the exception, the normalized view, and the renderer.

## Raise a typed error

```python
from servicewright import ErrorKind, ServiceError


class UserMissingError(ServiceError):
    kind = ErrorKind.NOT_FOUND        # code auto-derives: "user_missing"


raise UserMissingError("no such user", params={"user_id": user_id})
```

The FastAPI adapter renders a 404 RFC 9457 `application/problem+json` document; the gRPC adapter
aborts with `NOT_FOUND` and puts the machine code in the `x-error-code` trailing metadata. The
resolved values are always what the instance reports, so catching works:

```python
except ServiceError as exc:
    if exc.code == "user_missing":     # not the class default — the resolved code
        ...
```

`ErrorKind` maps to both status vocabularies:

| Kind | HTTP | gRPC |
| --- | --- | --- |
| `INVALID` | 400 | `INVALID_ARGUMENT` |
| `UNAUTHENTICATED` | 401 | `UNAUTHENTICATED` |
| `FORBIDDEN` | 403 | `PERMISSION_DENIED` |
| `NOT_FOUND` | 404 | `NOT_FOUND` |
| `CONFLICT` | 409 | `ALREADY_EXISTS` |
| `PRECONDITION_FAILED` | 412 | `FAILED_PRECONDITION` |
| `TOO_MANY_REQUESTS` | 429 | `RESOURCE_EXHAUSTED` |
| `DEADLINE_EXCEEDED` | 504 | `DEADLINE_EXCEEDED` |
| `UNAVAILABLE` | 503 | `UNAVAILABLE` |
| `NOT_IMPLEMENTED` | 501 | `UNIMPLEMENTED` |
| `INTERNAL` | 500 | `INTERNAL` |

## Public and private errors

```python
raise ServiceError("row 412 violates fk_orders_user", kind=ErrorKind.CONFLICT, public=False)
```

`public=False` masks the error on **every** transport: the client sees a generic internal error and
the real code, detail and params go to the log only. Masking strips everything — detail, params,
extra headers and any status override — so nothing can leak through a custom renderer.

## Own the wire format

Every default handler renders through one pluggable renderer, so a single implementation switches
the whole surface — a custom envelope, messages localized from `code` + `params`, extra members:

```python
from servicewright import ErrorInfo, RenderedError


class MyEnvelopeRenderer:
    def render(self, info: ErrorInfo) -> RenderedError:
        return RenderedError(
            status_code=info.http_status,
            body={"error": {"code": info.code, "message": self._localize(info)}},
            media_type="application/json",
        )


http = FastApiEntrypoint(config=..., error_renderer=MyEnvelopeRenderer())
```

Rendering is **total**: it may never be the thing that fails. `params` that are not JSON primitives
(a `UUID`, a `datetime`, a `Decimal`, a set, a self-referencing dict) are coerced rather than
raising, and a status code outside the IANA registry (499, 520) still renders — the helpers
`to_json_safe` and `status_title` are exported for custom renderers to reuse.

## The default handlers

`FastApiEntrypoint` installs five, all rendering through the configured renderer:

| Situation | Result |
| --- | --- |
| Request validation failed | 422, `code="validation_error"`, sanitized field errors |
| `HTTPException` (4xx) | that status, detail and headers preserved |
| `HTTPException` (5xx) | masked to a generic internal error |
| `ServiceError` | its kind's status; masked when `public=False` |
| Deadline exceeded | 504, `code="deadline_exceeded"` |
| Anything unhandled | masked 500 — logged with the request id, and the response carries it too |

Register handlers for your own exception types with the native argument, or switch the defaults off
entirely:

```python
FastApiEntrypoint(
    exception_handlers={MyLegacyError: my_handler},
    default_exception_handlers=False,
)
```
