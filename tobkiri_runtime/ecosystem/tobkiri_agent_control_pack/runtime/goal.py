"""Goal changes preserve constraints and prepare an atomic context projection."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .store import Conflict, digest, identifier, integer

REPLACEMENT_WARNING = (
    "Changing the whole goal rebuilds the goal instructions and summary. "
    "Cached input may not be reusable and details may leave the active context. "
    "The original conversation and persistent memory are retained. "
    "Restoring context does not undo external effects."
)


def text(value: Any) -> str:
    """Require bounded nonempty instruction text."""
    if not isinstance(value, str) or not value.strip() or len(value) > 32000:
        raise ValueError("text is empty or too long")
    return value.strip()


def text_list(value: Any) -> list[str]:
    """Validate bounded explicit criteria and preserved constraints."""
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("criteria must be a bounded list")
    return [text(item) for item in value]


def set_goal(
    plan: dict[str, Any], values: Mapping[str, Any], actor: str, now_ms: int
) -> dict[str, Any]:
    """Set an initial goal only; existing goals use prepare/commit replacement."""
    if plan["goal"] is not None:
        raise Conflict("existing goal requires replacement preview")
    goal = {
        "id": identifier(values["goal_id"]),
        "revision": 1,
        "body": text(values["body"]),
        "criteria": text_list(values.get("criteria", [])),
        "constraints": text_list(values.get("constraints", [])),
        "status": "active",
        "creator": actor,
        "subgoals": [],
    }
    plan["goal"] = goal
    plan["context"] = projection(goal, values["conversation_revision"], [])
    plan["history"].append(
        {
            "kind": "goal.created",
            "actor": actor,
            "at_ms": now_ms,
            "goal": deepcopy(goal),
        }
    )
    return {"status": "committed"}


def refine_goal(
    plan: dict[str, Any], values: Mapping[str, Any], actor: str, now_ms: int
) -> dict[str, Any]:
    """Add an explicit subgoal without replacing or weakening the parent."""
    if not plan["goal"]:
        raise ValueError("goal is not configured")
    goal = plan["goal"]
    goal["subgoals"].append(
        {
            "id": identifier(values["subgoal_id"]),
            "body": text(values["body"]),
            "actor": actor,
        }
    )
    goal["revision"] += 1
    plan["context"] = projection(
        goal, values["conversation_revision"], plan["context"]["summary_refs"]
    )
    plan["history"].append(
        {
            "kind": "goal.refined",
            "actor": actor,
            "at_ms": now_ms,
            "goal": deepcopy(goal),
        }
    )
    return {"status": "committed"}


def prepare_replace(
    plan: dict[str, Any], values: Mapping[str, Any], actor: str, now_ms: int
) -> dict[str, Any]:
    """Bind an immutable preview to goal, plan and conversation revisions."""
    if not plan["goal"]:
        raise ValueError("goal is not configured")
    body = text(values["body"])
    preview = {
        "id": identifier(values["preview_id"]),
        "actor": actor,
        "plan_revision": plan["revision"] + 1,
        "goal_revision": plan["goal"]["revision"],
        "conversation_revision": integer(values["conversation_revision"], minimum=1),
        "body": body,
        "criteria": text_list(values.get("criteria", [])),
        "expires_at_ms": now_ms + 5 * 60 * 1000,
        "old_goal": deepcopy(plan["goal"]),
        "affected_todo_ids": [item["id"] for item in plan["todos"]],
        "warning": REPLACEMENT_WARNING,
    }
    preview["digest"] = digest(preview)
    plan["previews"][preview["id"]] = preview
    plan["replacement_preview"] = deepcopy(preview)
    return {"status": "prepared", "preview": deepcopy(preview)}


def commit_replace(
    plan: dict[str, Any],
    values: Mapping[str, Any],
    actor: str,
    now_ms: int,
    prepared_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Commit goal and context together only after a validated compaction result."""
    preview = plan["previews"].get(identifier(values["preview_id"]))
    if not preview or preview["digest"] != values["preview_digest"]:
        raise Conflict("replacement preview is unknown")
    if (
        preview["actor"] != actor
        or preview["plan_revision"] != plan["revision"]
        or preview["goal_revision"] != plan["goal"]["revision"]
        or preview["conversation_revision"] != values["conversation_revision"]
        or now_ms >= preview["expires_at_ms"]
    ):
        raise Conflict("replacement preview is stale")
    expected_constraints = plan["goal"]["constraints"]
    if (
        prepared_context.get("conversation_revision")
        != preview["conversation_revision"]
        or prepared_context.get("goal_body") != preview["body"]
        or prepared_context.get("preserved_constraints") != expected_constraints
        or not prepared_context.get("transcript_refs")
        or not isinstance(prepared_context.get("summary"), str)
    ):
        raise ValueError("context migration is unverified")
    checkpoint = {
        "goal": deepcopy(plan["goal"]),
        "context": deepcopy(plan["context"]),
        "at_ms": now_ms,
    }
    goal = plan["goal"]
    goal.update(
        {
            "body": preview["body"],
            "criteria": preview["criteria"],
            "revision": goal["revision"] + 1,
            "subgoals": [],
        }
    )
    plan["generation"] += 1
    plan["context"] = {
        **projection(
            goal,
            preview["conversation_revision"],
            list(prepared_context["transcript_refs"]),
        ),
        "summary": prepared_context["summary"],
    }
    for todo in plan["todos"]:
        if todo["status"] != "cancelled":
            todo["status"] = "needs_review"
            todo["reason"] = "goal_replaced"
    for event in plan["inbox"]:
        if event["status"] in {"pending", "received"}:
            event["status"] = "expired"
    plan["history"].append(
        {
            "kind": "goal.replaced",
            "actor": actor,
            "at_ms": now_ms,
            "checkpoint": checkpoint,
            "goal": deepcopy(goal),
        }
    )
    plan["previews"].pop(preview["id"])
    plan["replacement_preview"] = None
    return {"status": "committed", "context_status": "ready_next_boundary"}


def projection(
    goal: Mapping[str, Any], conversation_revision: int, summary_refs: list[Any]
) -> dict[str, Any]:
    """Separate goal provenance from base safety rules and transcript ownership."""
    return {
        "version": "tobkiri.context-projection.v1",
        "goal_revision": goal["revision"],
        "conversation_revision": integer(conversation_revision, minimum=1),
        "goal_section": {
            "provenance": "delegated_work_plan",
            "body": goal["body"],
            "criteria": deepcopy(goal["criteria"]),
            "constraints": deepcopy(goal["constraints"]),
            "subgoals": deepcopy(goal["subgoals"]),
        },
        "summary_refs": deepcopy(summary_refs),
    }
