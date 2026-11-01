"""External-QA-oriented AI pipeline specifications."""

from __future__ import annotations

from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
    create_failover_operation,
    create_prepare_operation,
)


def test_prepare_preserves_absolute_deadline_and_opaque_handle() -> None:
    result = create_prepare_operation(None)(
        "prepare",
        {
            "request_id": "request-1",
            "decision_time": 100.0,
            "deadline": 120.0,
            "credential_handle": "credential:opaque",
            "profile_id": "defaults-profile",
            "requirements": {"modalities": ["text"], "unknown": True},
        },
    )

    assert result["deadline"] == 120.0
    assert result["credential_handle"] == "credential:opaque"
    assert result["profile_id"] == "defaults-profile"
    assert result["requirements"] == {"modalities": ["text"]}


def test_failover_requires_every_replay_safety_condition() -> None:
    operation = create_failover_operation(None)
    base = {
        "allow_failover": True,
        "idempotency_key": "idempotent",
        "tools": [],
        "error_code": "provider_unavailable",
        "attempt": 1,
        "candidate_count": 2,
        "decision_time": 100.0,
        "deadline": 120.0,
    }

    assert operation("decide", base)["allowed"] is True
    assert operation("decide", {**base, "tools": [{"name": "write"}]})[
        "allowed"
    ] is False
