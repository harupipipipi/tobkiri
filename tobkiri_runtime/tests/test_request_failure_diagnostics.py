"""Finite observations cannot disclose attacker-controlled error details."""

from __future__ import annotations

import json
import math
import urllib.error
from typing import Mapping

import pytest

from core_runtime.request_failure_diagnostics import (
    RequestFailureDiagnostics as HostFailureDiagnostics,
)
from ecosystem.rumi_provider_adapters_pack.runtime.streaming import (
    RequestFailureDiagnostics as PackFailureDiagnostics,
)


@pytest.fixture(params=[HostFailureDiagnostics, PackFailureDiagnostics],
                ids=["host", "pack"])
def diagnostics_type(request: pytest.FixtureRequest) -> type:
    """Exercise identical finite evidence requirements at both boundaries."""
    return request.param

_REQUEST = "request." + "a" * 32
_PARENT = "request." + "b" * 32


@pytest.mark.parametrize(
    ("error", "reason", "bucket"),
    [
        (TimeoutError("canary"), "timeout", "none"),
        (InterruptedError("canary"), "cancelled", "none"),
        (PermissionError("canary"), "policy", "none"),
        (ConnectionResetError("canary"), "network", "none"),
        (ValueError("canary"), "parser", "none"),
        (RuntimeError("canary"), "unknown", "none"),
        (
            urllib.error.HTTPError("canary", 429, "canary", {}, None),
            "http_status",
            "rate_limit",
        ),
        (
            urllib.error.HTTPError("canary", 403, "canary", {}, None),
            "http_status",
            "auth",
        ),
        (
            urllib.error.HTTPError("canary", 503, "canary", {}, None),
            "http_status",
            "server",
        ),
    ],
)
def test_exception_projection_contains_only_finite_fields(
    diagnostics_type: type,
    error: Exception,
    reason: str,
    bucket: str,
) -> None:
    """Only finite fields survive arbitrary messages and exception causes."""
    records = []
    diagnostics = diagnostics_type(
        _REQUEST, parent_request_id=_PARENT, sink=records.append
    )
    error.__cause__ = RuntimeError("secret-inner-canary")
    diagnostics.stage("headers")
    diagnostics.failure(error)
    diagnostics.failure(ValueError("second secret"))
    assert len(records) == 1
    record = records[0]
    assert record["reason"] == reason and record["http_bucket"] == bucket
    assert record["request_id"] == _REQUEST and record["parent_request_id"] == _PARENT
    assert record["stage"] == "headers"
    assert set(record) == {
        "event",
        "request_id",
        "parent_request_id",
        "reason",
        "stage",
        "http_bucket",
        "elapsed_ms",
        "stage_elapsed_ms",
    }
    assert "canary" not in json.dumps(record)
    for field in ("elapsed_ms", "stage_elapsed_ms"):
        assert type(record[field]) is int and 0 <= record[field] <= 86_400_000


@pytest.mark.parametrize("value", [True, math.nan, math.inf, -100, "canary", []])
def test_invalid_fields_and_clocks_remain_bounded(
    diagnostics_type: type, value: object,
) -> None:
    """Forged identities and unsupported scalars normalize to fixed values."""
    records = []
    diagnostics = diagnostics_type(
        value, parent_request_id=value, clock=lambda: value, sink=records.append
    )
    diagnostics.stage(value)
    diagnostics.interrupted(value)
    diagnostics.failure(RuntimeError("canary"), reason=value)
    assert records[0]["request_id"] == records[0]["parent_request_id"] == "unknown"
    assert records[0]["stage"] == "prepare" and records[0]["reason"] == "unknown"
    assert records[0]["elapsed_ms"] == 0


def test_independent_monotonic_duration_and_latched_interrupt(
    diagnostics_type: type,
) -> None:
    """A trusted first observation survives later exception translation."""
    records = []
    clock = iter([10.0, 12.0, 15.0])
    diagnostics = diagnostics_type(
        _REQUEST, clock=lambda: next(clock), sink=records.append
    )
    diagnostics.stage("body")
    diagnostics.interrupted("policy")
    diagnostics.interrupted("cancelled")
    diagnostics.failure(OSError("secret"))
    assert records[0]["reason"] == "policy"
    assert records[0]["elapsed_ms"] == 5000
    assert records[0]["stage_elapsed_ms"] == 3000


def test_failed_logging_does_not_replace_the_request_failure(
    diagnostics_type: type,
) -> None:
    """A failing private sink cannot escape into transport behavior."""

    def fail(_record: Mapping[str, object]) -> None:
        raise RuntimeError("logger secret")

    diagnostics = diagnostics_type(_REQUEST, sink=fail)
    diagnostics.failure(ValueError("original secret"))
