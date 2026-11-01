from __future__ import annotations

from typing import Any

from domain.tool_policy.internal_context import (
    internal_tool_decision_allows,
    tool_server_approval_context_is_internal,
)
from domain.ui_compiler.service import commit_ui_plan, compile_ui_plan
from domain.tool.ui_compiler_runtime import RecursiveUIBuildOrchestrator
from domain.tool.ui_compiler_runtime.orchestrator import backend_from_context
from domain.ui_compiler import UICompilerArtifactStore


def ui_compile_plan(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    del context
    return compile_ui_plan(arguments)


def ui_commit_plan(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return commit_ui_plan(
        arguments,
        workspace_root=_trusted_workspace(context),
        authorized=_authorized(context),
    )


def ui_build_recursive(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return RecursiveUIBuildOrchestrator(agent_backend=backend_from_context(context)).run(
        arguments,
        workspace_root=_trusted_workspace(context),
        authorized=_authorized(context),
        context=context,
    )


def ui_generate_foundation(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _run_stage("tool_ui_generate_foundation", arguments, context)


def ui_generate_candidates(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _run_stage("tool_ui_generate_candidates", arguments, context)


def ui_render_matrix(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _run_stage("tool_ui_render_matrix", arguments, context)


def ui_inspect_compression(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _run_stage("tool_ui_inspect_compression", arguments, context)


def ui_select_candidates(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _run_stage("tool_ui_select_candidates", arguments, context)


def ui_compose_page(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _run_stage("tool_ui_compose_page", arguments, context)


def ui_verify_recursive_build(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _run_stage("tool_ui_verify_recursive_build", arguments, context)


def ui_generation_status(arguments: dict[str, Any] | None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    workspace = _trusted_workspace(context)
    if not workspace:
        return _error("trusted workspace is required", "WORKSPACE_REQUIRED")
    data = arguments if isinstance(arguments, dict) else {}
    run_id = str(data.get("run_id") or data.get("runId") or "").strip()
    if not run_id:
        return _error("run_id is required", "INVALID_REQUEST")
    try:
        status = UICompilerArtifactStore(f"{workspace}/.rumi/ui").read_generation_status(run_id)
    except Exception as exc:
        return _error(str(exc), "STATUS_READ_FAILED")
    return {
        "status": "ok",
        "data": status,
        "widget": {"type": "ui_generation_status", "run_id": run_id, "status": status},
    }


def _run_stage(
    stage: str,
    arguments: dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    data = dict(arguments or {}) if isinstance(arguments, dict) else {}
    raw_options = data.get("options")
    options = dict(raw_options) if isinstance(raw_options, dict) else {}
    stop_after = _STAGE_TO_STOP_AFTER.get(stage)
    if stop_after:
        options["stopAfter"] = stop_after
        data["options"] = options
    result = ui_build_recursive(data, context)
    if isinstance(result, dict):
        widget = result.get("widget")
        if isinstance(widget, dict):
            widget["type"] = stage
        result_data = result.get("data")
        if isinstance(result_data, dict):
            result_data["stage"] = stage
    return result


_STAGE_TO_STOP_AFTER = {
    "tool_ui_generate_foundation": "foundation",
    "tool_ui_generate_candidates": "candidates",
    "tool_ui_render_matrix": "renderMatrix",
    "tool_ui_inspect_compression": "inspectCompression",
    "tool_ui_select_candidates": "selectCandidates",
    "tool_ui_compose_page": "composePage",
}


def _authorized(context: dict[str, Any] | None) -> bool:
    return tool_server_approval_context_is_internal(context) or internal_tool_decision_allows(context)


def _trusted_workspace(context: dict[str, Any] | None) -> str | None:
    if not isinstance(context, dict):
        return None
    raw = context.get("workspace_root") or context.get("conversation_workspace_dir")
    return str(raw) if raw else None


def _error(message: str, code: str) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code, "message": message}}
