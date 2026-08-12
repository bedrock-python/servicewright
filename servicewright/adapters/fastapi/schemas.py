"""Pydantic response models for HTTP health probes and error documents."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LivenessResponse(BaseModel):
    """Liveness probe body."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    """Readiness probe body."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "unhealthy"]


class ProblemDetails(BaseModel):
    """RFC 9457 Problem Details document (``application/problem+json``).

    The shape produced by the default
    :class:`~servicewright.core.errors.ProblemDetailsRenderer`; extra members
    are allowed per the RFC (custom renderers may add their own extensions).
    """

    model_config = ConfigDict(extra="allow")

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    code: str | None = None
    params: dict[str, Any] | None = Field(default=None)


__all__ = [
    "LivenessResponse",
    "ProblemDetails",
    "ReadinessResponse",
]
