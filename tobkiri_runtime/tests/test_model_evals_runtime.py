"""External-QA-oriented evaluation runtime specifications."""

from __future__ import annotations

from ecosystem.rumi_model_evals_pack.runtime.evaluator import (
    create_plan_operation,
    create_score_operation,
)


def test_eval_plan_never_executes_or_grants_authority() -> None:
    result = create_plan_operation(None)(
        "plan",
        {
            "suite_id": "fixture",
            "attempts": 2,
            "targets": [
                {
                    "target_id": "model-a",
                    "model_profile_id": "profile-a",
                    "fixture_id": "prompt-a",
                }
            ],
        },
    )

    assert result["executes"] is False
    assert result["approval_required"] is True
    assert len(result["operations"]) == 2
    assert all(
        item["authority_granted"] is False for item in result["operations"]
    )


def test_unknown_evidence_blocks_promotion_without_becoming_failure() -> None:
    result = create_score_operation(None)(
        "score",
        {
            "observations": [
                {"status": "passed", "cost": 1, "latency_ms": 10},
                {"status": "unknown"},
            ],
            "thresholds": {"minimum_samples": 1, "minimum_pass_rate": 1.0},
        },
    )

    assert result["passed"] == 1
    assert result["failed"] == 0
    assert result["unknown_count"] == 1
    assert result["promotion"]["eligible"] is False
    assert result["promotion"]["complete_evidence"] is False

