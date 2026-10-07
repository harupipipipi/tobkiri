"""Durable at-least-once instruction delivery and safe-boundary acknowledgement."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from tobkiri_protocol.agent_inbox_v1 import INBOX_VERSION, SAFE_BOUNDARIES
from .goal import text
from .store import Conflict, digest, identifier, integer


def deliver(
    plan: dict[str, Any], values: Mapping[str, Any], actor: str, now_ms: int
) -> dict[str, Any]:
    """Persist one exact event; delivery neither wakes paused work nor completes it."""
    event_id = identifier(values["event_id"])
    binding = identifier(values["agent_binding"])
    source = values.get("source", "manual")
    if source not in {"manual", "scheduled", "review"}:
        raise ValueError("inbox source is invalid")
    if source == "review" and binding == plan["settings"]["reviewer"]:
        raise ValueError("reviewer cannot remind itself")
    value = {
        "version": INBOX_VERSION,
        "id": event_id,
        "body": text(values["body"]),
        "agent_binding": binding,
        "source": source,
        "actor": actor,
        "generation": plan["generation"],
        "goal_revision": plan["goal"]["revision"] if plan["goal"] else None,
        "finding_id": values.get("finding_id"),
        "at_ms": now_ms,
        "status": "pending",
        "input_id": None,
        "lifetime": values.get("lifetime", "until_task_end"),
        "expires_at_ms": values.get("expires_at_ms"),
    }
    if value["lifetime"] not in {"one_turn", "until_task_end"}:
        raise ValueError("instruction lifetime is invalid")
    if value["expires_at_ms"] is not None:
        integer(value["expires_at_ms"])
    if source == "review" and value["finding_id"]:
        pending = next(
            (
                x
                for x in plan["inbox"]
                if x["finding_id"] == value["finding_id"]
                and x["status"] in {"pending", "received", "applied"}
                and x["generation"] == plan["generation"]
                and not x.get("resolved_at_ms")
            ),
            None,
        )
        if pending:
            return {
                "status": pending["status"],
                "event": deepcopy(pending),
                "deduplicated": True,
            }
    old = next((x for x in plan["inbox"] if x["id"] == event_id), None)
    if old:
        compare = {k: v for k, v in value.items() if k not in {"at_ms", "status", "input_id"}}
        if any(old[k] != v for k, v in compare.items()):
            raise Conflict("inbox event ID was rebound")
        return {"status": old["status"], "event": deepcopy(old), "deduplicated": True}
    if len(plan["inbox"]) >= 10000:
        raise ValueError("inbox retention capacity exceeded")
    plan["inbox"].append(value)
    return {"status": "pending", "event": deepcopy(value)}


def prepare_input(plan: dict[str, Any], values: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    """Receive at a real safe boundary; repeated input IDs retain the same batch."""
    if values["boundary"] not in SAFE_BOUNDARIES:
        raise ValueError("instruction boundary is unsafe")
    binding = identifier(values["agent_binding"])
    input_id = identifier(values["input_id"])
    if plan["status"] != "active":
        return {"status": plan["status"], "instructions": [], "context": None}
    result: list[dict[str, Any]] = []
    current_goal = plan["goal"]["revision"] if plan["goal"] else None
    for event in plan["inbox"]:
        if event["agent_binding"] != binding:
            continue
        if event.get("resolved_at_ms"):
            continue
        if (
            event["generation"] != plan["generation"]
            or event["goal_revision"] != current_goal
            or (event["expires_at_ms"] is not None and now_ms >= event["expires_at_ms"])
        ):
            if event["status"] in {"pending", "received"}:
                event["status"] = "expired"
            continue
        if event["status"] == "received" and event["input_id"] != input_id:
            continue  # An ambiguous prior input must be reconciled explicitly.
        if event["status"] in {"pending", "received"}:
            if len(result) >= 98:
                continue
            event["status"] = "received"
            event["input_id"] = input_id
            result.append(deepcopy(event))
        elif event["status"] == "applied" and event["lifetime"] == "until_task_end":
            if len(result) < 98:
                result.append(deepcopy(event))
    return {
        "status": "received",
        "input_id": input_id,
        "instructions": result,
        "context": deepcopy(plan["context"]),
    }


def acknowledge(plan: dict[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically mark only the exact received batch applied, never drift resolved."""
    input_id = identifier(values["input_id"])
    event_ids = values["event_ids"]
    if not isinstance(event_ids, list) or len(event_ids) > 10000:
        raise ValueError("inbox acknowledgement IDs are invalid")
    selected = {identifier(value) for value in event_ids}
    events = [x for x in plan["inbox"] if x["id"] in selected]
    if len(events) != len(selected) or any(
        x["input_id"] != input_id
        or x["status"] not in {"received", "applied"}
        or x["generation"] != plan["generation"]
        for x in events
    ):
        raise Conflict("inbox acknowledgement is stale")
    for event in events:
        event["status"] = "applied"
    return {"status": "applied", "event_ids": sorted(selected)}


def create_reminder(
    plan: dict[str, Any], values: Mapping[str, Any], actor: str, now_ms: int
) -> dict[str, Any]:
    """Normalize explicit scheduled instructions against an IANA timezone."""
    reminder_id = identifier(values["reminder_id"])
    ZoneInfo(values["timezone"])
    if ("delay_seconds" in values) == ("at_ms" in values):
        raise ValueError("provide one relative or normalized absolute time")
    due = (
        now_ms + integer(values["delay_seconds"], minimum=1) * 1000
        if "delay_seconds" in values
        else integer(values["at_ms"], minimum=1)
    )
    interval = integer(values.get("interval_seconds", 0))
    if interval and interval < 60:
        raise ValueError("reminder interval must be at least 60 seconds")
    reminder = {
        "id": reminder_id,
        "body": text(values["body"]),
        "agent_binding": identifier(values["agent_binding"]),
        "timezone": values["timezone"],
        "at_ms": due,
        "interval_seconds": interval,
        "generation": plan["generation"],
        "actor": actor,
        "status": "registered",
        "schedule_id": f"remind-{digest([plan['id'], reminder_id])[:24]}",
    }
    plan.setdefault("reminders", {})[reminder_id] = reminder
    return {"status": "registered", "reminder": deepcopy(reminder)}
