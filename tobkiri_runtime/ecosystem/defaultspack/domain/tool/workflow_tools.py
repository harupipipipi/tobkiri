from __future__ import annotations

import json
import time
import uuid
from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.tool.executor import ToolExecutor

from ._agent_os_common import err, ok, workspace


def _store_dir(context: dict[str, Any] | None):
    ws = workspace(context)
    path = ws.resolve(".workflows", allow_root=True)
    path.mkdir(parents=True, exist_ok=True)
    return ws, path


def workflow_define(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    workflow_id = str(arguments.get("workflow_id") or "workflow_" + uuid.uuid4().hex[:10])
    steps = arguments.get("steps")
    if not isinstance(steps, list):
        return err("'steps' must be a list", "INVALID_INPUT")
    ws, root = _store_dir(context)
    record = {"workflow_id": workflow_id, "name": arguments.get("name") or workflow_id, "steps": steps, "updated_at": time.time()}
    (root / f"{workflow_id}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return ok(record)


def _load_workflow(workflow_id: str, context: dict[str, Any] | None) -> dict[str, Any] | None:
    _ws, root = _store_dir(context)
    path = root / f"{workflow_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def workflow_run(
    arguments: dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    settings_owner: SettingsOwnerPort | None = None,
) -> dict[str, Any]:
    workflow_id = str(arguments.get("workflow_id") or "")
    workflow = _load_workflow(workflow_id, context) if workflow_id else arguments
    if not isinstance(workflow, dict) or not isinstance(workflow.get("steps"), list):
        return err("workflow not found or invalid", "WORKFLOW_NOT_FOUND")
    run_id = "run_" + uuid.uuid4().hex[:10]
    outputs: dict[str, Any] = {}
    executor = ToolExecutor(settings_owner=settings_owner)
    run_context = dict(context or {})
    for step in workflow["steps"]:
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool") or "")
        step_id = str(step.get("id") or tool)
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        result = executor.execute(tool, args, run_context)
        outputs[step_id] = result
        if result.get("is_error"):
            status = "failed"
            break
    else:
        status = "completed"
    record = {"run_id": run_id, "workflow_id": workflow.get("workflow_id") or workflow_id, "status": status, "outputs": outputs}
    ws, root = _store_dir(context)
    runs = root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return ok(record)


def workflow_status(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    run_id = str(arguments.get("run_id") or "")
    if not run_id:
        return err("'run_id' is required", "INVALID_INPUT")
    _ws, root = _store_dir(context)
    path = root / "runs" / f"{run_id}.json"
    if not path.exists():
        return err("workflow run not found", "RUN_NOT_FOUND")
    return ok(json.loads(path.read_text(encoding="utf-8")))


def workflow_retry(
    arguments: dict[str, Any],
    context: dict[str, Any] | None = None,
    *,
    settings_owner: SettingsOwnerPort | None = None,
) -> dict[str, Any]:
    workflow_id = str(arguments.get("workflow_id") or "")
    return workflow_run(
        {"workflow_id": workflow_id},
        context,
        **({"settings_owner": settings_owner} if settings_owner is not None else {}),
    )


def workflow_cancel(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    run_id = str(arguments.get("run_id") or "")
    if not run_id:
        return err("'run_id' is required", "INVALID_INPUT")
    _ws, root = _store_dir(context)
    path = root / "runs" / f"{run_id}.json"
    if not path.exists():
        return err("workflow run not found", "RUN_NOT_FOUND")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["status"] = "cancelled"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return ok(record)
