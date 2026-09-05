from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .artifact_store import UICompilerArtifactStore
from .models import UICompilerConfig
from .planner import RecursiveUIPlanner

RUN_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def compile_ui_plan(arguments: dict[str, Any] | None) -> dict[str, Any]:
    data = arguments if isinstance(arguments, dict) else {}
    unsupported = _unsupported_keys(
        data,
        {"ui_tree", "uiTree", "root", "page", "config", "run_id", "runId", "persist"},
    )
    if unsupported:
        return _error(f"unsupported request keys: {', '.join(unsupported)}", "INVALID_REQUEST")
    if _truthy(data.get("persist")):
        return _error(
            "persist is not supported by the read-only compile endpoint",
            "PERSIST_NOT_SUPPORTED_ON_COMPILE_ENDPOINT",
        )
    root_payload = _root_payload(data)
    if not isinstance(root_payload, dict):
        return _error("ui_tree object is required", "INVALID_UI_TREE")

    try:
        config = UICompilerConfig.from_dict(data.get("config") or {})
        run_id = _validate_run_id(data.get("run_id") or data.get("runId"))
        plan = RecursiveUIPlanner(config).plan(root_payload, run_id=run_id)
    except (RecursionError, TypeError, ValueError) as exc:
        return _error(str(exc), "INVALID_UI_PLAN")

    if not plan.is_executable():
        return _error(
            "UI plan is not executable",
            "PLAN_NOT_EXECUTABLE",
            data={
                "diagnostics": [item.to_dict() for item in plan.diagnostics],
                "partialPlan": plan.to_dict(),
            },
        )

    payload: dict[str, Any] = {"plan": plan.to_dict()}
    return {
        "status": "ok",
        "data": payload,
        "widget": {
            "type": "ui_compile_plan",
            "run_id": plan.run_id,
            "summary": payload["plan"]["summary"],
        },
    }


def commit_ui_plan(
    arguments: dict[str, Any] | None,
    *,
    workspace_root: str | Path | None,
    authorized: bool,
) -> dict[str, Any]:
    if not authorized:
        return _error(
            "commit requires a verified internal tool approval context",
            "APPROVAL_REQUIRED",
            data={"approval_required": True},
        )
    if workspace_root is None:
        return _error("trusted workspace is required", "WORKSPACE_REQUIRED")
    data = arguments if isinstance(arguments, dict) else {}
    unsupported = _unsupported_keys(
        data,
        {"ui_tree", "uiTree", "root", "page", "config", "run_id", "runId", "idempotency_key", "idempotencyKey"},
    )
    if unsupported:
        return _error(f"unsupported request keys: {', '.join(unsupported)}", "INVALID_REQUEST")
    root_payload = _root_payload(data)
    if not isinstance(root_payload, dict):
        return _error("ui_tree object is required", "INVALID_UI_TREE")

    try:
        config = UICompilerConfig.from_dict(data.get("config") or {})
        run_id = _validate_run_id(data.get("run_id") or data.get("runId"))
        idempotency_key = _validate_idempotency_key(data.get("idempotency_key") or data.get("idempotencyKey"))
        plan = RecursiveUIPlanner(config).plan(root_payload, run_id=run_id)
    except (RecursionError, TypeError, ValueError) as exc:
        return _error(str(exc), "INVALID_UI_PLAN")

    if not plan.is_executable():
        return _error(
            "invalid UI plan cannot be committed",
            "PLAN_NOT_EXECUTABLE",
            data={
                "diagnostics": [item.to_dict() for item in plan.diagnostics],
                "partialPlan": plan.to_dict(),
            },
        )

    try:
        workspace = Path(workspace_root).expanduser().resolve()
        if not workspace.is_absolute() or not workspace.is_dir():
            return _error("trusted workspace is unavailable", "WORKSPACE_REQUIRED")
        artifacts = UICompilerArtifactStore(workspace / ".rumi" / "ui").save_plan(
            plan,
            idempotency_key=idempotency_key,
        )
    except (OSError, ValueError) as exc:
        return _error(str(exc), "ARTIFACT_WRITE_FAILED")

    return {
        "status": "ok",
        "data": {"plan": plan.to_dict(), "artifacts": artifacts},
        "widget": {
            "type": "ui_commit_plan",
            "run_id": plan.run_id,
            "summary": plan.to_dict()["summary"],
            "artifacts": artifacts,
        },
    }


def _root_payload(data: dict[str, Any]) -> Any:
    return data.get("ui_tree") or data.get("uiTree") or data.get("root") or data.get("page")


def _unsupported_keys(data: dict[str, Any], allowed: set[str]) -> list[str]:
    return sorted(str(key) for key in data if str(key) not in allowed)


def _validate_run_id(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) > 80 or not RUN_ID_RE.fullmatch(raw):
        raise ValueError("invalid run_id")
    return raw


def _validate_idempotency_key(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if not IDEMPOTENCY_KEY_RE.fullmatch(raw):
        raise ValueError("invalid idempotency_key")
    return raw


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _error(message: str, code: str, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "error": {"code": code, "message": message}}
    if data:
        payload["data"] = data
    return payload
