"""Value-level masking: the Masker seam, ValueRedactor/ChainRedactor, per-surface routing."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from servicewright.core.contracts import Masker, Redactor
from servicewright.core.observability import (
    MASK,
    ChainRedactor,
    KeyRedactor,
    ObservabilityManager,
    ValueRedactor,
)
from servicewright.testing import FakeSettings

pytestmark = pytest.mark.unit


def _mask_emails(value: str) -> str:
    return "<email>" if "@" in value else value


# --------------------------------------------------------------------------- #
# ValueRedactor
# --------------------------------------------------------------------------- #
def test__value_redactor__string_values_everywhere__are_masked() -> None:
    redactor = ValueRedactor(_mask_emails)
    payload = {
        "message": "user alex@example.com logged in",
        "nested": {"contact": "a@b.c"},
        "items": ["x", "b@c.d", 42],
        "pair": ("e@f.g", None),
    }
    result = redactor(payload)
    assert result["message"] == "<email>"
    assert result["nested"]["contact"] == "<email>"
    assert result["items"] == ["x", "<email>", 42]
    assert result["pair"] == ("<email>", None)


def test__value_redactor__keys_and_non_strings__are_left_alone() -> None:
    redactor = ValueRedactor(lambda value: "MASKED")
    result = redactor({"a@b.c": 1, "count": 2, "flag": True, "none": None})
    # Keys are never masked; non-string values pass through untouched.
    assert result == {"a@b.c": 1, "count": 2, "flag": True, "none": None}


def test__value_redactor__original_payload__is_not_mutated() -> None:
    payload = {"message": "a@b.c", "nested": {"contact": "a@b.c"}}
    ValueRedactor(_mask_emails)(payload)
    assert payload == {"message": "a@b.c", "nested": {"contact": "a@b.c"}}


def test__value_redactor__cyclic_payload__does_not_recurse_forever() -> None:
    payload: dict[str, Any] = {"message": "a@b.c"}
    payload["self"] = payload
    result = ValueRedactor(_mask_emails)(payload)
    assert result["message"] == "<email>"


def test__value_redactor__masker_raises__value_becomes_the_mask_not_the_original(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(value: str) -> str:
        raise RuntimeError("model not loaded")

    redactor = ValueRedactor(broken)
    with caplog.at_level(logging.WARNING):
        result = redactor({"message": "secret-pii", "other": "also-pii"})
    # Fail closed: never the raw value.
    assert result == {"message": MASK, "other": MASK}
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1  # once per redactor, not per value


def test__value_redactor__custom_mask__is_used_on_failure() -> None:
    def broken(value: str) -> str:
        raise RuntimeError("boom")

    assert ValueRedactor(broken, mask="***")({"key": "value"}) == {"key": "***"}


# --------------------------------------------------------------------------- #
# ChainRedactor
# --------------------------------------------------------------------------- #
def test__chain_redactor__key_first_then_value__masker_never_sees_redacted_fields() -> None:
    seen: list[str] = []

    def spy_masker(value: str) -> str:
        seen.append(value)
        return value

    chain = ChainRedactor(KeyRedactor(), ValueRedactor(spy_masker))
    result = chain({"password": "hunter2", "message": "hello a@b.c"})
    # The key redactor collapsed the password before the value masker ran.
    assert result["password"] == MASK
    assert "hunter2" not in seen
    assert MASK in seen


def test__chain_redactor__empty__is_identity() -> None:
    payload = {"key": "value"}
    assert ChainRedactor()(payload) == payload


# --------------------------------------------------------------------------- #
# Per-surface routing in the manager
# --------------------------------------------------------------------------- #
class _CtxSink:
    """Captures the setup context; the manager treats provided instances as unconditional."""

    backend = "fake"

    def __init__(self) -> None:
        self.ctx: Any = None

    def setup(self, ctx: Any) -> None:
        self.ctx = ctx

    def shutdown(self) -> None: ...


def _manager_with_sinks(**redactor_kwargs: Any) -> tuple[ObservabilityManager, dict[str, _CtxSink]]:
    sinks = {name: _CtxSink() for name in ("logging", "error_tracking", "tracing", "metrics")}
    manager = ObservabilityManager(
        logging=sinks["logging"],  # type: ignore[arg-type]
        error_tracking=sinks["error_tracking"],  # type: ignore[arg-type]
        tracing=sinks["tracing"],  # type: ignore[arg-type]
        metrics=sinks["metrics"],  # type: ignore[arg-type]
        **redactor_kwargs,
    )
    manager.configure(FakeSettings(), service_name="svc")
    return manager, sinks


def test__manager__global_redactor__reaches_every_payload_surface_but_not_metrics() -> None:
    redactor = KeyRedactor()
    _, sinks = _manager_with_sinks(redactor=redactor)
    assert sinks["logging"].ctx.redactor is redactor
    assert sinks["error_tracking"].ctx.redactor is redactor
    assert sinks["tracing"].ctx.redactor is redactor
    # Metrics carry no payloads: their ctx never advertises a redactor.
    assert sinks["metrics"].ctx.redactor is None


def test__manager__surface_override__wins_over_the_global_redactor() -> None:
    cheap = KeyRedactor()
    expensive = ValueRedactor(_mask_emails)
    _, sinks = _manager_with_sinks(redactor=cheap, error_redactor=expensive)
    assert sinks["logging"].ctx.redactor is cheap
    assert sinks["tracing"].ctx.redactor is cheap
    assert sinks["error_tracking"].ctx.redactor is expensive


def test__manager__surface_redactor_without_a_global__only_that_surface_gets_it() -> None:
    only_traces = KeyRedactor()
    _, sinks = _manager_with_sinks(trace_redactor=only_traces)
    assert sinks["tracing"].ctx.redactor is only_traces
    assert sinks["logging"].ctx.redactor is None
    assert sinks["error_tracking"].ctx.redactor is None


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #
def test__masker_and_redactors__satisfy_their_protocols() -> None:
    assert Masker.__name__ == "Masker"
    value_redactor: Redactor = ValueRedactor(_mask_emails)
    chain: Redactor = ChainRedactor(KeyRedactor(), value_redactor)
    assert chain({"password": "x"})["password"] == MASK
