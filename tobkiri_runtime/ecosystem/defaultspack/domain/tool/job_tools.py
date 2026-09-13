from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ._agent_os_common import err, ok, workspace
from .research_tools import wide_research


def _job_dir(context: dict[str, Any] | None):
    ws = workspace(context)
    path = ws.resolve(".jobs", allow_root=True)
    path.mkdir(parents=True, exist_ok=True)
    return ws, path


def _index_path(context: dict[str, Any] | None):
    return _job_dir(context)[1] / "index.json"


def _load(context: dict[str, Any] | None) -> dict[str, Any]:
    path = _index_path(context)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(context: dict[str, Any] | None, jobs: dict[str, Any]) -> None:
    path = _index_path(context)
    path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def job_create(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    kind = str(arguments.get("kind") or "task")
    job_id = str(arguments.get("job_id") or "job_" + uuid.uuid4().hex[:12])
    jobs = _load(context)
    record: dict[str, Any] = {
        "job_id": job_id,
        "kind": kind,
        "status": "queued",
        "input": arguments.get("input") if isinstance(arguments.get("input"), dict) else {},
        "result": {},
        "artifact_dir": f"jobs/{job_id}",
        "conversation_id": (context or {}).get("conversation_id") if isinstance(context, dict) else None,
        "created_at": _ts(),
        "updated_at": _ts(),
        "events": [{"at": _ts(), "type": "created"}],
    }
    if arguments.get("run_immediately", True) is not False:
        record["status"] = "running"
        record["events"].append({"at": _ts(), "type": "started"})
        if kind == "wide_research":
            tool_input = dict(record["input"])
            tool_input.setdefault("query", arguments.get("query") or tool_input.get("query") or "Research")
            tool_input.setdefault("output_path", f"jobs/{job_id}/report.md")
            result = wide_research(tool_input, context)
            record["result"] = result.get("widget", {}).get("data", {})
        record["status"] = "completed"
        record["events"].append({"at": _ts(), "type": "completed"})
        record["updated_at"] = _ts()
    jobs[job_id] = record
    _save(context, jobs)
    return ok(record)


def job_status(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    job_id = str(arguments.get("job_id") or "")
    jobs = _load(context)
    if not job_id:
        return ok({"jobs": list(jobs.values())})
    record = jobs.get(job_id)
    if not record:
        return err("job not found", "JOB_NOT_FOUND")
    return ok(record)


def job_cancel(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    job_id = str(arguments.get("job_id") or "")
    jobs = _load(context)
    record = jobs.get(job_id)
    if not record:
        return err("job not found", "JOB_NOT_FOUND")
    record["status"] = "cancelled"
    record["updated_at"] = _ts()
    record.setdefault("events", []).append({"at": _ts(), "type": "cancelled"})
    _save(context, jobs)
    return ok(record)


def job_resume(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    job_id = str(arguments.get("job_id") or "")
    jobs = _load(context)
    record = jobs.get(job_id)
    if not record:
        return err("job not found", "JOB_NOT_FOUND")
    record["status"] = "queued"
    record["updated_at"] = _ts()
    record.setdefault("events", []).append({"at": _ts(), "type": "resumed"})
    _save(context, jobs)
    return ok(record)


def job_artifacts(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    job_id = str(arguments.get("job_id") or "")
    if not job_id:
        return err("'job_id' is required", "INVALID_INPUT")
    ws = workspace(context)
    artifact_dir = ws.resolve(f"jobs/{job_id}", allow_root=True)
    entries = []
    if artifact_dir.exists():
        entries = [{"path": ws.relative(path), "size": path.stat().st_size} for path in artifact_dir.rglob("*") if path.is_file()]
    return ok({"job_id": job_id, "artifacts": entries})


def job_history(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    result = job_status(arguments, context)
    data = result.get("widget", {}).get("data", {})
    if "events" in data:
        return ok({"job_id": data.get("job_id"), "events": data.get("events", [])})
    return result
