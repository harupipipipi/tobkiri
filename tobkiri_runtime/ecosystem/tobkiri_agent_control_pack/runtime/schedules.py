"""Bind plan reminders to the selected public scheduler owner; no timer thread."""

from __future__ import annotations

from typing import Any, Mapping

SCHEDULE_RESOURCE = "tobkiri.resource.schedule.v1"
SCHEDULE_ACTION = "tobkiri.action.schedule.v1"
RESOURCE_OPERATION = "rumi_schedule_store_pack.schedule-resource"
ACTION_OPERATION = "rumi_schedule_store_pack.schedule-action"


class Schedules:
    """Reconcile stable IDs without postponing already scheduled reviews."""

    def __init__(self, client: Any, profile_id: str) -> None:
        self.client = client
        self.profile_id = profile_id

    def reconcile(self, plan: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
        """Use exactly one recurring review record for a nonempty active plan."""
        settings = plan["settings"]
        tracked = bool(plan["goal"] or plan["todos"])
        if not tracked:
            return {**plan["schedule"], "status": "unconfigured"}
        if not settings["executor"] or not settings["reviewer"]:
            return {**plan["schedule"], "status": "unconfigured"}
        enabled = plan["status"] == "active" and settings["enabled"]
        return self.ensure(
            plan["schedule"]["id"],
            "agent-control.review",
            {"plan_id": plan["id"], "generation": plan["generation"]},
            now_ms + settings["interval_seconds"] * 1000,
            settings["interval_seconds"] * 1000,
            enabled,
        )

    def ensure(
        self,
        schedule_id: str,
        action_id: str,
        payload: Mapping[str, Any],
        due_ms: int,
        interval_ms: int,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Read the owner CAS revision before create/update/pause/resume."""
        state = self.client.invoke(
            SCHEDULE_RESOURCE,
            RESOURCE_OPERATION,
            {"operation": "list", "profile_id": self.profile_id},
        )
        revision = state["revision"]
        current = next((x for x in state["schedules"] if x["id"] == schedule_id), None)
        if current is None:
            if not enabled:
                return {"id": schedule_id, "status": "paused", "next_run_at_ms": None}
            return self._action(
                "create",
                revision,
                schedule_id,
                name="Tobkiri work plan",
                action_id=action_id,
                payload=dict(payload),
                next_run_at_ms=due_ms,
                interval_ms=interval_ms,
                max_attempts=3,
            )["schedule"]
        updates: dict[str, Any] = {}
        if current["interval_ms"] != interval_ms:
            updates["interval_ms"] = interval_ms
            # A shorter interval can advance the due time; never postpone it.
            updates["next_run_at_ms"] = min(current["next_run_at_ms"], due_ms)
        if current["payload"] != dict(payload):
            updates["payload"] = dict(payload)
        if updates:
            changed = self._action("update", revision, schedule_id, updates=updates)
            current, revision = changed["schedule"], changed["revision"]
        if not enabled and current["status"] not in {"paused", "cancelled"}:
            current = self._action("pause", revision, schedule_id)["schedule"]
        elif enabled and current["status"] == "paused":
            current = self._action("resume", revision, schedule_id)["schedule"]
        return dict(current)

    def _action(
        self, operation: str, revision: int, schedule_id: str, **arguments: Any
    ) -> dict[str, Any]:
        return self.client.invoke(
            SCHEDULE_ACTION,
            ACTION_OPERATION,
            {
                "operation": operation,
                "profile_id": self.profile_id,
                "schedule_id": schedule_id,
                "expected_revision": revision,
                **arguments,
            },
        )
