"""Dependency-ordered Todo state and explicit evidence-based completion."""

from __future__ import annotations

from typing import Any, Mapping

from .goal import text, text_list
from .store import Conflict, identifier, integer


def add_todo(
    plan: dict[str, Any], values: Mapping[str, Any], actor: str, now_ms: int
) -> dict[str, Any]:
    """Add one dependency-bound item, rejecting unknown dependencies and cycles."""
    item_id = identifier(values["item_id"])
    if len(plan["todos"]) >= 1000 or any(x["id"] == item_id for x in plan["todos"]):
        raise ValueError("Todo identity or capacity is invalid")
    dependencies = values.get("dependencies", [])
    if not isinstance(dependencies, list) or len(dependencies) > 1000:
        raise ValueError("Todo dependencies are invalid")
    item = {
        "id": item_id,
        "revision": 1,
        "body": text(values["body"]),
        "criteria": text_list(values.get("criteria", [])),
        "dependencies": [identifier(x) for x in dependencies],
        "order": integer(values.get("order", len(plan["todos"]))),
        "status": "pending",
        "actor": actor,
        "at_ms": now_ms,
        "goal_revision": plan["goal"]["revision"] if plan["goal"] else None,
        "evidence": [],
        "reason": None,
        "run_id": None,
    }
    plan["todos"].append(item)
    validate_graph(plan["todos"])
    return {"status": "committed", "todo": item}


def validate_graph(items: list[dict[str, Any]]) -> None:
    """Validate all links and detect dependency cycles deterministically."""
    by_id = {item["id"]: item for item in items}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise ValueError("Todo dependency cycle")
        if item_id in visited:
            return
        if item_id not in by_id:
            raise ValueError("Todo dependency is unknown")
        visiting.add(item_id)
        for dependency in by_id[item_id]["dependencies"]:
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in by_id:
        visit(item_id)


def next_item(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    """Select a ready item immediately, independent of the review interval."""
    by_id = {item["id"]: item for item in plan["todos"]}
    if any(item["status"] == "in_progress" for item in by_id.values()):
        return None
    ready = [
        item
        for item in by_id.values()
        if item["status"] == "pending"
        and all(by_id[key]["status"] == "done" for key in item["dependencies"])
    ]
    return min(ready, key=lambda item: (item["order"], item["id"])) if ready else None


def claim(plan: dict[str, Any], item_id: str, run_id: str) -> dict[str, Any]:
    """Claim only the current ready item; lost claims are reconciled, not rerun."""
    item = next_item(plan)
    if not item or item["id"] != item_id:
        raise Conflict("Todo is not ready for a claim")
    item.update({"status": "in_progress", "run_id": run_id, "revision": item["revision"] + 1})
    return {"status": "claimed", "todo": item}


def record_result(
    plan: dict[str, Any], item_id: str, run_id: str, result: Mapping[str, Any]
) -> dict[str, Any]:
    """Distinguish wait, cancel, failure and evidence-verified completion."""
    item = next(x for x in plan["todos"] if x["id"] == item_id)
    current_goal = plan["goal"]["revision"] if plan["goal"] else None
    if (
        item["status"] != "in_progress"
        or item["run_id"] != run_id
        or item["goal_revision"] != current_goal
    ):
        raise Conflict("Todo result binding is stale")
    status = result.get("status")
    if status in {"waiting", "waiting_user", "waiting_approval", "blocked"}:
        item.update({"status": "blocked", "reason": status})
    elif status == "cancelled":
        item.update({"status": "cancelled", "reason": "executor_cancelled"})
    elif status != "completed":
        item.update({"status": "blocked", "reason": "execution_failed"})
    else:
        evidence = result.get("evidence")
        criteria = result.get("verified_criteria")
        valid = (
            isinstance(evidence, list)
            and bool(evidence)
            and all(
                isinstance(x, dict) and x.get("reference") and x.get("verified") is True
                for x in evidence
            )
            and isinstance(criteria, list)
            and bool(item["criteria"])
            and set(criteria) == set(item["criteria"])
        )
        item.update(
            {
                "status": "done" if valid else "needs_review",
                "reason": None if valid else "completion_unverified",
                "evidence": evidence if isinstance(evidence, list) else [],
            }
        )
    item["revision"] += 1
    return {"status": item["status"], "todo": item}
