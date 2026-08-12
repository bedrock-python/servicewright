"""Common FastAPI header dependency types.

Folded verbatim (behaviour-preserving) from the FastAPI service-runtime
prototype's ``headers`` module.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Header

IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_\-]+$"
IDEMPOTENCY_KEY_MAX_LEN = 128

X_USER_ID_DESCRIPTION = "Canonical user ID injected by the authenticated gateway"
IDEMPOTENCY_KEY_DESCRIPTION = "Idempotency key for the request"
AUTHORIZATION_HEADER_DESCRIPTION = "Authorization header"
X_FINGERPRINT_DESCRIPTION = "Client fingerprint header"

XUserId = Annotated[
    UUID,
    Header(
        alias="X-User-Id",
        description=X_USER_ID_DESCRIPTION,
    ),
]

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        max_length=IDEMPOTENCY_KEY_MAX_LEN,
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description=IDEMPOTENCY_KEY_DESCRIPTION,
    ),
]

AuthorizationHeader = Annotated[
    str | None,
    Header(
        alias="Authorization",
        description=AUTHORIZATION_HEADER_DESCRIPTION,
    ),
]

XFingerprintHeader = Annotated[
    str | None,
    Header(
        alias="X-Fingerprint",
        description=X_FINGERPRINT_DESCRIPTION,
    ),
]


__all__ = [
    "AuthorizationHeader",
    "IdempotencyKey",
    "XFingerprintHeader",
    "XUserId",
]
