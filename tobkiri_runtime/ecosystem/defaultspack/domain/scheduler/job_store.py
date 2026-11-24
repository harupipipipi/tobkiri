from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from blocks._common import gen_id
from core_runtime.runtime_events import utc_now as utc_now_text
from core_runtime.runtime_state import append_jsonl, atomic_write_json

from .models import SchedulerJob
from .schedule_parser import iso, parse_next_run
from .security import resolve_jobs_path, validate_no_agent_job


def default_scheduler_dir() -> Path:
    """Return the durable scheduler directory for this Defaultspack install."""
    override = os.environ.get("RUMI_DEFAULTSPACK_SCHEDULER_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    jobs_path = resolve_jobs_path()
    if jobs_path is not None:
        return jobs_path.parent
    user_data = os.environ.get("RUMI_USER_DATA", "").strip()
    if user_data:
        return (
            Path(user_data).expanduser()
            / "defaultspack"
            / "shared"
            / "scheduler"
        )
    return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "scheduler"


class SchedulerJobStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_scheduler_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)
        self.jobs_path = self.root / "jobs.json"
        if not self.jobs_path.exists():
            atomic_write_json(self.jobs_path, {"schema_version": 1, "jobs": {}})

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {"schema_version": 1, "jobs": {}}
        if not isinstance(payload.get("jobs"), dict):
            payload["jobs"] = {}
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.jobs_path, payload)

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        validate_no_agent_job(data)
        payload = self.load()
        now = utc_now_text()
        job = SchedulerJob(
            job_id=data.get("job_id") or gen_id("job_"),
            name=str(data.get("name") or data.get("prompt") or "Scheduled job")[:120],
            kind=str(data.get("kind") or "one_shot"),
            schedule=str(data.get("schedule") or data.get("when") or "now"),
            prompt=str(data.get("prompt") or ""),
            target_conversation_id=str(data.get("target_conversation_id") or data.get("conversation_id") or ""),
            model=str(data.get("model") or ""),
            system_prompt_id=str(data.get("system_prompt_id") or ""),
            agent_id=str(data.get("agent_id") or "main"),
            session_target=str(data.get("session_target") or "fresh"),
            context_from=list(data.get("context_from") or []),
            skills=list(data.get("skills") or []),
            enabled_toolsets=list(data.get("enabled_toolsets") or data.get("toolsets") or []),
            runtime_profile_key=str(data.get("runtime_profile_key") or ""),
            deliver=str(data.get("deliver") or "local"),
            no_agent=bool(data.get("no_agent", False)),
            script=_normalize_script(data.get("script")),
            timeout_seconds=_normalize_timeout(data.get("timeout_seconds")),
            params=dict(data.get("params") if isinstance(data.get("params"), dict) else {}),
            enabled=bool(data.get("enabled", True)),
            next_run_at=iso(parse_next_run(str(data.get("schedule") or data.get("when") or "now"))),
            created_at=now,
            updated_at=now,
        )
        payload["jobs"][job.job_id] = job.to_dict()
        self.save(payload)
        return job.to_dict()

    def list(self) -> list[dict[str, Any]]:
        return list(self.load()["jobs"].values())

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.load()["jobs"].get(job_id)

    def update(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        payload = self.load()
        job = payload["jobs"].get(job_id)
        if not job:
            return None
        prospective = dict(job)
        prospective.update(updates or {})
        validate_no_agent_job(prospective)
        if "script" in updates:
            updates["script"] = _normalize_script(updates.get("script"))
        if "timeout_seconds" in updates:
            updates["timeout_seconds"] = _normalize_timeout(updates.get("timeout_seconds"))
        job.update(updates or {})
        job["updated_at"] = utc_now_text()
        if "schedule" in updates:
            job["next_run_at"] = iso(parse_next_run(str(job.get("schedule") or "now")))
        payload["jobs"][job_id] = job
        self.save(payload)
        return job

    def delete(self, job_id: str) -> bool:
        payload = self.load()
        existed = payload["jobs"].pop(job_id, None) is not None
        if existed:
            self.save(payload)
        return existed

    def append_run(self, job_id: str, record: dict[str, Any]) -> None:
        append_jsonl(self.root / "runs" / f"{job_id}.jsonl", record)


def _normalize_script(value: Any) -> list[str] | None:
    if value in (None, ""):
        return None
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return None


def _normalize_timeout(value: Any) -> int:
    try:
        timeout = int(value or 60)
    except (TypeError, ValueError):
        return 60
    return max(1, min(timeout, 3600))
