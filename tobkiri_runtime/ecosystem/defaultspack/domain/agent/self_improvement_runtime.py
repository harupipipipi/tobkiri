from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


MODEL_ROLES = ("main", "vision", "fast")

MIMO_PROFILE_ID = "defaultspack.mimo_coding_company"
MIMO_COMPANY_ID = "mimo-coding-company"

MIMO_ROLE_MAP: dict[str, str] = {
    "main": "xiaomi-token-plan-sgp/mimo-v2.5-pro",
    "vision": "xiaomi-token-plan-sgp/mimo-v2-omni",
    "fast": "xiaomi-token-plan-sgp/mimo-v2-flash",
}

SELF_IMPROVEMENT_TOOLS = [
    "coding_file_read",
    "coding_file_search",
    "coding_file_list",
    "coding_file_write",
    "coding_file_create",
    "coding_file_patch",
    "coding_file_restore",
    "coding_git_status",
    "coding_git_diff",
    "coding_git_commit",
    "coding_git_push",
    "coding_github_pr_create",
    "coding_terminal_exec",
    "rumi_api",
    "todo",
    "subagent",
    "web_search",
]

SELF_IMPROVEMENT_TASK_STATES = (
    "pending", "running", "approval_pending", "completed", "failed", "skipped",
)

RESTRICTED_COMMIT_PATHS = frozenset({".env", ".env.local", ".env.production"})


class ModelRoleValidationError(ValueError):
    pass


class SelfImprovingDefaultspackRuntime:
    """Provider-agnostic self-improvement runtime for defaultspack v2.

    This runtime is not tied to any specific model provider.
    Profiles (like MimoCodingCompanyProfile) supply the role→model map.
    """

    def __init__(
        self,
        profile_id: str,
        role_map: dict[str, str],
        tool_allowlist: list[str] | None = None,
        workspace_root: str | Path | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        self.profile_id = profile_id
        self.role_map = dict(role_map)
        self.tool_allowlist = list(tool_allowlist or SELF_IMPROVEMENT_TOOLS)
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
        self._state_path = Path(state_path) if state_path else self._default_state_path()
        self._validate_role_map()

    def _default_state_path(self) -> Path:
        override = os.environ.get("RUMI_SELF_IMPROVEMENT_STATE_PATH", "").strip()
        if override:
            return Path(override)
        return self.workspace_root / "user_data" / "shared" / "self_improvement" / "state.json"

    def _validate_role_map(self) -> None:
        for role in MODEL_ROLES:
            if role not in self.role_map:
                raise ModelRoleValidationError(f"missing role in role_map: {role}")

    def manifest(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "runtime": "SelfImprovingDefaultspackRuntime",
            "role_map": dict(self.role_map),
            "tool_allowlist": list(self.tool_allowlist),
            "non_stop": True,
            "can_run_24_7": True,
            "model_self_selection": {
                "enabled": True,
                "role_map": dict(self.role_map),
            },
            "tool_policy": {
                "allowlist": list(self.tool_allowlist),
                "denylist": [],
            },
        }

    def status(self) -> dict[str, Any]:
        state = self._load_state()
        return {
            "profile_id": self.profile_id,
            "runtime": "SelfImprovingDefaultspackRuntime",
            "role_map": dict(self.role_map),
            "running": state.get("running", False),
            "current_task": state.get("current_task"),
            "completed_count": len(state.get("completed_tasks", [])),
            "failed_count": len(state.get("failed_tasks", [])),
            "approval_waiting_count": len(state.get("approval_waiting", [])),
            "last_tool_call": state.get("last_tool_call"),
            "last_model_error": state.get("last_model_error"),
            "last_test_result": state.get("last_test_result"),
            "last_commit": state.get("last_commit"),
            "dirty_files": state.get("dirty_files", []),
            "tasks": state.get("tasks", []),
            "events": state.get("events", []),
            "last_self_improvement_result": state.get("last_self_improvement_result"),
            "updated_at": _timestamp(),
        }

    def bootstrap(self, *, model: str | None = None) -> dict[str, Any]:
        state = self._load_state()
        state["running"] = True
        state["started_at"] = _timestamp()
        state["profile_id"] = self.profile_id
        state["role_map"] = dict(self.role_map)
        state.setdefault("tasks", [])
        state.setdefault("completed_tasks", [])
        state.setdefault("failed_tasks", [])
        state.setdefault("events", [])
        state.setdefault("approval_waiting", [])
        self._save_state(state)
        return self.status()

    def add_task(
        self,
        task_id: str,
        title: str,
        *,
        expected_outcome: str = "",
        tools_used: list[str] | None = None,
    ) -> dict[str, Any]:
        state = self._load_state()
        task = {
            "task_id": task_id,
            "title": title,
            "status": "pending",
            "expected_outcome": expected_outcome,
            "tools_used": tools_used or [],
            "started_at": None,
            "ended_at": None,
            "failures": [],
            "result": None,
        }
        tasks = state.setdefault("tasks", [])
        tasks.append(task)
        self._save_state(state)
        return task

    def start_task(self, task_id: str) -> dict[str, Any]:
        state = self._load_state()
        task = self._find_task(state, task_id)
        task["status"] = "running"
        task["started_at"] = _timestamp()
        state["current_task"] = task_id
        self._record_event(state, "task_started", {"task_id": task_id})
        self._save_state(state)
        return task

    def complete_task(self, task_id: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
        state = self._load_state()
        task = self._find_task(state, task_id)
        task["status"] = "completed"
        task["ended_at"] = _timestamp()
        task["result"] = result
        state["current_task"] = None
        completed = state.setdefault("completed_tasks", [])
        completed.append(task_id)
        state["last_self_improvement_result"] = result
        self._record_event(state, "task_completed", {"task_id": task_id, "result": result})
        self._save_state(state)
        return task

    def wait_for_approval(self, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Persist the existing tool request without completing or replaying it."""
        state = self._load_state()
        task = self._find_task(state, task_id)
        task["status"] = "approval_pending"
        task["result"] = dict(result)
        state["running"] = False
        state["current_task"] = task_id
        waiting = state.setdefault("approval_waiting", [])
        request_id = str(result.get("approval_request_id") or "")
        if not any(item.get("approval_request_id") == request_id for item in waiting):
            waiting.append({"task_id": task_id, "approval_request_id": request_id})
        self._record_event(state, "approval_pending", {
            "task_id": task_id, "approval_request_id": request_id,
        })
        self._save_state(state)
        return task

    def fail_task(self, task_id: str, failure: str) -> dict[str, Any]:
        state = self._load_state()
        task = self._find_task(state, task_id)
        task["status"] = "failed"
        task["ended_at"] = _timestamp()
        task["failures"].append(failure)
        state["current_task"] = None
        failed = state.setdefault("failed_tasks", [])
        failed.append(task_id)
        self._record_event(state, "task_failed", {"task_id": task_id, "failure": failure})
        self._save_state(state)
        return task

    def record_tool_call(self, tool_name: str, result: dict[str, Any] | None = None) -> None:
        state = self._load_state()
        state["last_tool_call"] = {
            "tool": tool_name,
            "timestamp": _timestamp(),
            "result_summary": str(result)[:200] if result else None,
        }
        self._record_event(state, "tool_call", {"tool": tool_name})
        self._save_state(state)

    def record_test_result(self, test_command: str, exit_code: int, output: str) -> None:
        state = self._load_state()
        state["last_test_result"] = {
            "command": test_command,
            "exit_code": exit_code,
            "output": output[:500],
            "timestamp": _timestamp(),
        }
        self._record_event(state, "test_run", {"command": test_command, "exit_code": exit_code})
        self._save_state(state)

    def record_commit(self, commit_hash: str, message: str, paths: list[str] | None = None) -> None:
        state = self._load_state()
        state["last_commit"] = {
            "commit_hash": commit_hash,
            "message": message,
            "paths": paths,
            "timestamp": _timestamp(),
        }
        self._record_event(state, "commit", {"commit_hash": commit_hash, "paths": paths})
        self._save_state(state)

    def record_model_error(self, error: str) -> None:
        state = self._load_state()
        state["last_model_error"] = {"error": error, "timestamp": _timestamp()}
        self._record_event(state, "model_error", {"error": error})
        self._save_state(state)

    def pause(self) -> dict[str, Any]:
        state = self._load_state()
        state["running"] = False
        state["paused_at"] = _timestamp()
        self._record_event(state, "paused", {})
        self._save_state(state)
        return self.status()

    def resume(self) -> dict[str, Any]:
        state = self._load_state()
        state["running"] = True
        state.pop("paused_at", None)
        self._record_event(state, "resumed", {})
        self._save_state(state)
        return self.status()

    def stop(self) -> dict[str, Any]:
        state = self._load_state()
        state["running"] = False
        state["stopped_at"] = _timestamp()
        state["current_task"] = None
        self._record_event(state, "stopped", {})
        self._save_state(state)
        return self.status()

    def generate_report(self) -> dict[str, Any]:
        state = self._load_state()
        completed = state.get("completed_tasks", [])
        failed = state.get("failed_tasks", [])
        tasks = state.get("tasks", [])
        events = state.get("events", [])
        friction_points = [
            event for event in events
            if event.get("kind") in ("model_error", "task_failed")
        ]
        return {
            "profile_id": self.profile_id,
            "total_tasks": len(tasks),
            "completed": len(completed),
            "failed": len(failed),
            "friction_points": friction_points,
            "last_test_result": state.get("last_test_result"),
            "last_commit": state.get("last_commit"),
            "events": events[-50:],
            "generated_at": _timestamp(),
        }

    def _find_task(self, state: dict[str, Any], task_id: str) -> dict[str, Any]:
        for task in state.get("tasks", []):
            if task.get("task_id") == task_id:
                return task
        raise KeyError(f"task not found: {task_id}")

    def _record_event(self, state: dict[str, Any], kind: str, data: dict[str, Any]) -> None:
        events = state.setdefault("events", [])
        events.append({"kind": kind, "data": data, "timestamp": _timestamp()})

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, **state, "updated_at": _timestamp()}
        self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def create_mimo_profile(
    workspace_root: str | Path | None = None,
    state_path: str | Path | None = None,
) -> SelfImprovingDefaultspackRuntime:
    """Create a SelfImprovingDefaultspackRuntime with the MiMo coding company profile."""
    return SelfImprovingDefaultspackRuntime(
        profile_id=MIMO_PROFILE_ID,
        role_map=resolve_mimo_role_map(workspace_root=workspace_root),
        tool_allowlist=list(SELF_IMPROVEMENT_TOOLS),
        workspace_root=workspace_root,
        state_path=state_path,
    )


def resolve_mimo_role_map(workspace_root: str | Path | None = None) -> dict[str, str]:
    """Resolve MiMo self-improvement model roles from local company/profile state."""
    role_map = dict(MIMO_ROLE_MAP)
    for candidate in _mimo_role_map_sources(workspace_root):
        role_map.update(candidate)
    return {role: role_map[role] for role in MODEL_ROLES}


def default_mimo_model(role: str = "main", workspace_root: str | Path | None = None) -> str:
    role = str(role or "main").strip()
    if role not in MODEL_ROLES:
        role = "main"
    return resolve_mimo_role_map(workspace_root=workspace_root)[role]


def _mimo_role_map_sources(workspace_root: str | Path | None) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    root = Path(workspace_root) if workspace_root else Path.cwd()
    for path in _mimo_profile_state_paths(root):
        data = _read_json_object(path)
        if not data:
            continue
        sources.extend(_extract_role_maps(data))
    return sources


def _mimo_profile_state_paths(workspace_root: Path) -> list[Path]:
    paths = [
        workspace_root / "user_data" / "shared" / "companies" / "companies.json",
        workspace_root / "ecosystem" / "defaultspack" / "user_data" / "shared" / "companies" / "companies.json",
        workspace_root / "tobkiri_runtime" / "ecosystem" / "defaultspack" / "user_data" / "shared" / "companies" / "companies.json",
        workspace_root / "user_data" / "shared" / "mimo_coding_company" / "codex_manager_status.json",
        workspace_root / "tobkiri_runtime" / "user_data" / "shared" / "mimo_coding_company" / "codex_manager_status.json",
    ]
    company_store = os.environ.get("RUMI_DEFAULTSPACK_COMPANY_STORE_PATH", "").strip()
    if company_store:
        path = Path(company_store)
        paths.append(path if path.suffix == ".json" else path / "companies.json")
    return paths


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_role_maps(data: dict[str, Any]) -> list[dict[str, str]]:
    maps: list[dict[str, str]] = []
    maps.extend(_extract_role_maps_from_mapping(data))

    provider = data.get("provider")
    if isinstance(provider, dict):
        maps.extend(_extract_role_maps_from_mapping(provider))

    companies = data.get("companies")
    if isinstance(companies, dict):
        company = companies.get(MIMO_COMPANY_ID)
        if isinstance(company, dict):
            maps.extend(_extract_role_maps_from_mapping(company))
            metadata = company.get("metadata")
            if isinstance(metadata, dict):
                maps.extend(_extract_role_maps_from_mapping(metadata))
            settings = company.get("settings")
            if isinstance(settings, dict):
                maps.extend(_extract_role_maps_from_mapping(settings))
    return maps


def _extract_role_maps_from_mapping(data: dict[str, Any]) -> list[dict[str, str]]:
    maps: list[dict[str, str]] = []
    for key in ("role_map", "models", "model_roles", "mimo_role_map", "self_improvement_role_map"):
        value = data.get(key)
        if isinstance(value, dict):
            normalized = _normalize_role_map(value)
            if normalized:
                maps.append(normalized)
    metadata_models = {
        "main": data.get("main_model"),
        "vision": data.get("vision_model"),
        "fast": data.get("fast_model"),
    }
    normalized = _normalize_role_map(metadata_models)
    if normalized:
        maps.append(normalized)
    return maps


def _normalize_role_map(value: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for role in MODEL_ROLES:
        candidate = str(value.get(role) or "").strip()
        if candidate:
            normalized[role] = candidate
    return normalized


def validate_model_for_role(
    role: str,
    model_id: str,
    capabilities: dict[str, Any],
) -> None:
    """Validate that a model is suitable for a given role.

    Raises ModelRoleValidationError if the model is unsuitable.
    """
    if role == "main" and not capabilities.get("tool_calls"):
        raise ModelRoleValidationError(
            f"model {model_id} does not support tool_calls, required for main/coding role"
        )
    if role == "vision" and not capabilities.get("vision"):
        raise ModelRoleValidationError(
            f"model {model_id} does not support vision, required for vision/browser QA role"
        )
