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
    active_ids = {
        event["finding_id"]
        for event in plan["inbox"]
        if event["source"] == "review"
        and event["status"] not in {"cancelled", "expired"}
        and not event.get("resolved_at_ms")
        and event["generation"] == plan["generation"]
    }
    findings: dict[str, dict[str, Any]] = {}
    for finding in plan["review_occurrences"].values():
        if finding["id"] not in active_ids or finding["verdict"] != "drift":
            continue
        prior = findings.get(finding["id"])
        if prior is None or finding["plan_revision"] > prior["plan_revision"]:
            findings[finding["id"]] = deepcopy(finding)
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
            if x["status"] not in {"cancelled", "expired"} and not x.get("resolved_at_ms")
        ],
        "unresolved_findings": list(findings.values()),
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
    if not isinstance(verdict, str):
        verdict = "unverifiable"
    references = result.get("evidence_refs")
    condition = result.get("condition_id")
    item_ids = result.get("todo_ids", [])
    resolved_ids = result.get("resolved_finding_ids", [])
    observed = set(snapshot["evidence"].get("reference_ids", []))
    independent = (
        result.get("reviewer_binding") == snapshot["reviewer"]
        and result.get("reviewer_binding") != snapshot["executor"]
        and bool(result.get("run_context_id"))
    )
    valid = (
        isinstance(verdict, str)
        and verdict in REVIEW_STATES
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
            and isinstance(condition, str)
            and 0 < len(condition) <= 256
            and isinstance(item_ids, list)
            and len(item_ids) <= 1000
            and all(
                isinstance(item, str) and item in {todo["id"] for todo in plan["todos"]}
                for item in item_ids
            )
        )
    unresolved = {entry["id"]: entry for entry in snapshot["unresolved_findings"]}
    if verdict == "on_track":
        valid = valid and isinstance(resolved_ids, list) and len(resolved_ids) <= 100
        valid = valid and all(
            isinstance(value, str) and value in unresolved for value in resolved_ids
        )
    if not valid:
        verdict = "unverifiable"
    finding_id = digest(
        {
            "goal": snapshot["goal"],
            "generation": snapshot["generation"],
            "todo_ids": sorted(set(item_ids)) if valid and verdict == "drift" else [],
            "condition": condition if isinstance(condition, str) else None,
        }
    )
    finding = {
        "verdict": verdict,
        "plan_revision": snapshot["plan_revision"],
        "generation": snapshot["generation"],
        "goal_revision": snapshot["goal"]["revision"] if snapshot["goal"] else None,
        "conversation_revision": snapshot["evidence"].get("conversation_revision"),
        "condition_id": condition if isinstance(condition, str) else None,
        "todo_ids": sorted(set(item_ids)) if valid and verdict == "drift" else [],
        "at_ms": now_ms,
        "evidence_refs": references if valid else [],
        "id": finding_id,
        "occurrence_id": snapshot["occurrence_id"],
        "run_context_id": result.get("run_context_id") if independent else None,
        "model_resolution": deepcopy(result.get("model_resolution")) if independent else None,
    }
    plan["review"] = finding
    if verdict == "drift":
        previous = next(
            (
                event
                for event in reversed(plan["inbox"])
                if event["finding_id"] == finding_id and event.get("resolved_at_ms")
            ),
            None,
        )
        delivery_id = (
            digest([finding_id, previous["resolution_review_occurrence"]])[:24]
            if previous
            else finding_id[:24]
        )
        delivered = deliver(
            plan,
            {
                "event_id": f"finding-{delivery_id}",
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
        assert isinstance(references, list)
        # A read receipt is not improvement. Only an evidence-backed review
        # can resolve previous findings; their immutable delivery history stays.
        resolved = []
        revision = snapshot["evidence"].get("conversation_revision")
        for value in set(resolved_ids):
            prior = unresolved[value]
            if (
                type(revision) is not int
                or type(prior.get("conversation_revision")) is not int
                or revision <= prior["conversation_revision"]
                or not set(references) - set(prior["evidence_refs"])
                or prior["generation"] != plan["generation"]
                or prior.get("goal_revision") != finding["goal_revision"]
            ):
                continue
            for event in plan["inbox"]:
                if (
                    event["source"] == "review"
                    and event["finding_id"] == value
                    and event["generation"] == plan["generation"]
                    and not event.get("resolved_at_ms")
                    and event["status"] not in {"cancelled", "expired"}
                ):
                    event["resolved_at_ms"] = now_ms
                    event["resolution_review_occurrence"] = snapshot["occurrence_id"]
            resolved.append(value)
        finding["resolved_finding_ids"] = sorted(resolved)
        plan["history"].append(
            {
                "kind": "review.on_track",
                "at_ms": now_ms,
                "evidence_refs": references,
                "conversation_revision": revision,
                "resolved_finding_ids": sorted(resolved),
                "occurrence_id": snapshot["occurrence_id"],
            }
        )
    plan["review_occurrences"][snapshot["occurrence_id"]] = deepcopy(finding)
    return {"status": verdict, "finding": deepcopy(finding)}
