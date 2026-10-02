"""Validate independent evidence review; only concrete drift creates guidance."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from tobkiri_protocol.work_plan_v1 import REVIEW_STATES
from .inbox import deliver
from .store import Conflict, digest


def review_snapshot(
    plan: Mapping[str, Any], evidence: Mapping[str, Any], occurrence_id: str
) -> dict[str, Any]:
    """Create bounded reviewer input from latest public evidence and plan state."""
    return {
        "version": "tobkiri.work-plan-review.v1",
        "plan_id": plan["id"],
        "plan_revision": plan["revision"],
        "generation": plan["generation"],
        "goal": deepcopy(plan["goal"]),
        "todos": deepcopy(plan["todos"]),
        "instructions": [
            deepcopy(x)
            for x in plan["inbox"]
            if x["status"] not in {"cancelled", "expired"}
        ],
        "evidence": deepcopy(dict(evidence)),
        "occurrence_id": occurrence_id,
        "executor": plan["settings"]["executor"],
        "reviewer": plan["settings"]["reviewer"],
        "policy": {
            "independent": True,
            "read_only": True,
            "intervention": "drift_guidance_only",
        },
    }


def record_review(
    plan: dict[str, Any],
    snapshot: Mapping[str, Any],
    result: Mapping[str, Any],
    actor: str,
    now_ms: int,
) -> dict[str, Any]:
    """Reject stale or ungrounded findings and retain failure as unverified."""
    if (
        plan["generation"] != snapshot["generation"]
        or plan["revision"] != snapshot["plan_revision"]
        or plan["status"] != "active"
        or not plan["settings"]["enabled"]
    ):
        raise Conflict("review snapshot is stale")
    verdict = result.get("verdict")
    references = result.get("evidence_refs")
    observed = set(snapshot["evidence"].get("reference_ids", []))
    independent = (
        result.get("reviewer_binding") == snapshot["reviewer"]
        and result.get("reviewer_binding") != snapshot["executor"]
        and bool(result.get("run_context_id"))
    )
    valid = (
        verdict in REVIEW_STATES
        and independent
        and isinstance(references, list)
        and all(isinstance(x, str) and x in observed for x in references)
    )
    if verdict in {"on_track", "drift", "blocked"}:
        valid = valid and bool(references)
    if verdict == "drift":
        valid = (
            valid
            and isinstance(result.get("instruction"), str)
            and bool(result["instruction"].strip())
        )
    if not valid:
        verdict = "unverifiable"
    finding_id = digest(
        {
            "goal": snapshot["goal"],
            "todo_ids": result.get("todo_ids", []),
            "condition": result.get("condition_id"),
            "refs": references if isinstance(references, list) else [],
        }
    )
    finding = {
        "verdict": verdict,
        "plan_revision": snapshot["plan_revision"],
        "generation": snapshot["generation"],
        "at_ms": now_ms,
        "evidence_refs": references if valid else [],
        "id": finding_id,
        "occurrence_id": snapshot["occurrence_id"],
        "run_context_id": result.get("run_context_id") if independent else None,
    }
    plan["review"] = finding
    plan["review_occurrences"][snapshot["occurrence_id"]] = deepcopy(finding)
    if verdict == "drift":
        delivered = deliver(
            plan,
            {
                "event_id": f"finding-{finding_id[:24]}",
                "source": "review",
                "finding_id": finding_id,
                "body": result["instruction"],
                "agent_binding": snapshot["executor"],
            },
            actor,
            now_ms,
        )
        finding["delivery_id"] = delivered["event"]["id"]
    elif verdict == "on_track":
        # A read receipt is not improvement. Only an evidence-backed review
        # can resolve previous findings; their immutable delivery history stays.
        plan["history"].append(
            {"kind": "review.on_track", "at_ms": now_ms, "evidence_refs": references}
        )
    return {"status": verdict, "finding": deepcopy(finding)}
