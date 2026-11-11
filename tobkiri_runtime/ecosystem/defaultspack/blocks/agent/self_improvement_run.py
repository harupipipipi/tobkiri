"""defaultspack.self_improvement_run — Live self-improvement execution endpoint."""

from __future__ import annotations

import sys
from pathlib import Path

_PACK_ROOT = Path(__file__).resolve().parents[2]
_DEFAULTSPACK_ROOT = _PACK_ROOT.parent / "defaultspack"
for _path in (str(_PACK_ROOT), str(_DEFAULTSPACK_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from blocks._common import ok, error  # noqa: E402


def run(input_data, context=None):
    action = input_data.get("action", "single")
    if action not in {"single", "multi"}:
        return error(f"unknown action: {action}", code="INVALID_INPUT")
    from domain.agent.self_improvement_live_loop import resolve_live_workspace

    try:
        workspace = resolve_live_workspace(
            input_data.get("workspace_root"), input_data.get("workspace_id"), context,
        )
    except Exception as exc:
        response = error(str(exc), code="WORKSPACE_BINDING_INVALID")
        response["_http_status"] = 403
        return response

    if action == "single":
        from domain.agent.self_improvement_live_loop import run_live_improvement
        from domain.agent.self_improvement_runtime import default_mimo_model

        workspace_root = workspace.root
        result = run_live_improvement(
            workspace_root=workspace_root,
            workspace_id=workspace.workspace_id,
            context=context,
            task_id=input_data.get("task_id", "live_01"),
            task_title=input_data.get("task_title", "Live self-improvement"),
            max_tool_calls=int(input_data.get("max_tool_calls", 15)),
            model=input_data.get("model") or default_mimo_model("main", workspace_root=workspace_root),
            state_path=input_data.get("state_path"),
        )
        return ok(result)

    if action == "multi":
        from domain.agent.self_improvement_live_loop import run_multi_task_dogfood

        result = run_multi_task_dogfood(
            workspace_root=workspace.root,
            workspace_id=workspace.workspace_id,
            context=context,
            task_count=int(input_data.get("task_count", 3)),
            state_path=input_data.get("state_path"),
        )
        return ok(result)

    return error(f"unknown action: {action}", code="INVALID_INPUT")
