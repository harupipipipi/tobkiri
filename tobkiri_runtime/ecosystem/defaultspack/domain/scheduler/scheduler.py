from __future__ import annotations

from typing import Any

from core_runtime.runtime_events import utc_now
from tobkiri_protocol.settings_state import SettingsOwnerPort

from .delivery import deliver
from .job_store import SchedulerJobStore
from .runner import SchedulerRunner
from .schedule_parser import is_due, iso, parse_next_run
from .security import SchedulerPolicyError, validate_scheduler_enabled


class Scheduler:
    def __init__(
        self,
        store: SchedulerJobStore | None = None,
        runner: SchedulerRunner | None = None,
        *,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> None:
        self.store = store or SchedulerJobStore()
        self.runner = runner or SchedulerRunner(settings_owner=settings_owner)

    def tick(self) -> dict[str, Any]:
        try:
            validate_scheduler_enabled()
        except SchedulerPolicyError as exc:
            return {"ran": [], "count": 0, "status": "error", "error": str(exc)}
        ran = []
        for job in self.store.list():
            if not job.get("enabled", True) or not is_due(job.get("next_run_at", "")):
                continue
            next_run_at = ""
            if job.get("kind") != "one_shot":
                try:
                    next_run_at = iso(parse_next_run(str(job.get("schedule") or "now")))
                except ValueError as exc:
                    self.store.update(
                        job["job_id"],
                        {
                            "enabled": False,
                            "last_error": f"invalid schedule: {exc}",
                            "updated_at": utc_now(),
                        },
                    )
                    continue
            result = self.runner.run(job)
            delivery = deliver(job, result)
            record = {"job_id": job["job_id"], "result": result, "delivery": delivery, "created_at": utc_now()}
            self.store.append_run(job["job_id"], record)
            updates = {"last_run_at": utc_now()}
            if job.get("kind") == "one_shot":
                updates["enabled"] = False
            else:
                updates["next_run_at"] = next_run_at
            self.store.update(job["job_id"], updates)
            ran.append(record)
        return {"ran": ran, "count": len(ran)}

    def run_now(self, job_id: str) -> dict[str, Any]:
        try:
            validate_scheduler_enabled()
        except SchedulerPolicyError as exc:
            return {"status": "error", "error": str(exc)}
        job = self.store.get(job_id)
        if not job:
            return {"status": "error", "error": "job not found"}
        result = self.runner.run(job)
        record = {"job_id": job_id, "result": result, "delivery": deliver(job, result), "created_at": utc_now()}
        self.store.append_run(job_id, record)
        self.store.update(job_id, {"last_run_at": utc_now()})
        return record
