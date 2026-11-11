"""Live MiMo-driven self-improvement loop.

Drives a real model through tool calls to inspect, patch, test, and commit
improvements to defaultspack.  Unlike the smoke tests, this hits the actual
MiMo API and executes real tools.

Every tool uses the selected canonical workspace and the normal coding gates.
Pending approval stops the cycle and is handled by the existing tool followup.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

_DEFAULTSPACK_ROOT = str(Path(__file__).resolve().parents[2])
if _DEFAULTSPACK_ROOT not in sys.path:
    sys.path.insert(0, _DEFAULTSPACK_ROOT)

from core_runtime.runtime_locks import LockTimeout, NamedLock  # noqa: E402
from domain.agent.self_improvement_runtime import (  # noqa: E402
    MIMO_ROLE_MAP,
    SelfImprovingDefaultspackRuntime,
    create_mimo_profile,
)
from domain.ai_client.api_key_store import read_provider_api_key  # noqa: E402
from domain.ai_client.providers.xiaomi_mimo_token_plan_provider import (  # noqa: E402
    XiaomiMimoTokenPlanSgpProvider,
)
from domain.coding import contract_adapter  # noqa: E402
from domain.tool.schema_adapter import adapt_tool_definition  # noqa: E402
from domain.tool_policy.internal_context import sanitize_untrusted_tool_context  # noqa: E402


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "coding_file_read",
        "description": "Read a file from the workspace. Returns file content.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace root"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "coding_file_patch",
        "description": "Patch a file by replacing old text with new text. Requires exact match of old text.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to workspace root"},
                "old": {"type": "string", "description": "Exact text to find and replace"},
                "new": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "coding_terminal_exec",
        "description": "Execute a terminal command. Use for running tests, git commands, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (optional)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "coding_git_commit",
        "description": "Commit files to git. Use paths to commit only specific files.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths to stage and commit",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "coding_git_status",
        "description": "Get git working tree status.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "coding_file_search",
        "description": "Search for files matching a pattern in the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to search for"},
            },
            "required": ["pattern"],
        },
    },
]

DEFAULT_MIMO_MAIN_MODEL = MIMO_ROLE_MAP["main"]
DEFAULT_MIMO_VISION_MODEL = MIMO_ROLE_MAP["vision"]


def _adapt_tools(workspace_id: str) -> list[dict[str, Any]]:
    """Expose the Host-bound workspace in every model tool schema."""
    tools = []
    for definition in TOOL_DEFINITIONS:
        parameters = definition["parameters"]
        bound = {
            **definition,
            "input_schema": {
                **parameters,
                "properties": {
                    **parameters["properties"],
                    "workspace_id": {
                        "type": "string",
                        "const": workspace_id,
                        "description": "The selected task workspace; cannot be changed",
                    },
                },
                "required": [*parameters.get("required", []), "workspace_id"],
            },
        }
        tools.append(adapt_tool_definition(bound))
    return tools


def _configured_mimo_provider() -> XiaomiMimoTokenPlanSgpProvider:
    """Build the MiMo provider with a credential supplied by the caller."""

    api_key = read_provider_api_key("xiaomi-token-plan-sgp", "legacy") or ""
    return XiaomiMimoTokenPlanSgpProvider(api_key=api_key)


def _normalize_args(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Fix common MiMo argument quirks."""
    args = dict(arguments)

    if tool_name == "coding_file_patch":
        if "oldText" in args and "old" not in args:
            args["old"] = args.pop("oldText")
        if "newText" in args and "new" not in args:
            args["new"] = args.pop("newText")

    if tool_name == "coding_git_commit":
        paths = args.get("paths")
        if isinstance(paths, str):
            try:
                parsed = json.loads(paths)
                if isinstance(parsed, list):
                    args["paths"] = parsed
                else:
                    args["paths"] = [paths]
            except json.JSONDecodeError:
                args["paths"] = [paths]

    return args


@dataclass(frozen=True)
class LiveWorkspaceBinding:
    """The existing Host-selected mount used by one live cycle."""

    workspace_id: str
    root: Path
    mount_revision: int


def resolve_live_workspace(
    workspace_root: str | Path | None = None,
    workspace_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> LiveWorkspaceBinding:
    """Validate selectors against canonical list/get without creating a mount."""
    context = context if isinstance(context, dict) else {}
    snapshot = contract_adapter.invoke_coding_contract(
        contract_adapter.WORKSPACE_RESOURCE, "list", {},
    )
    selected = str(snapshot.get("selected_workspace_id") or "").strip()
    if not selected:
        raise PermissionError("A selected canonical workspace is required")
    for claimed in (workspace_id, context.get("workspace_id"), context.get("selected_workspace_id")):
        if claimed not in (None, "") and str(claimed).strip() != selected:
            raise PermissionError("Workspace ID does not match the selected canonical workspace")
    mount = contract_adapter.invoke_coding_contract(
        contract_adapter.WORKSPACE_RESOURCE, "get", {"workspace_id": selected},
    )
    if not isinstance(mount, dict) or mount.get("id") != selected:
        raise PermissionError("Selected workspace mount is unavailable")
    metadata = mount.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("trusted") is not True:
        raise PermissionError("Selected workspace must already be trusted")
    revision = mount.get("mount_revision")
    if type(revision) is not int or revision < 1:
        raise PermissionError("Selected workspace mount revision is unavailable")
    root_path = str(mount.get("root_path") or "").strip()
    if not root_path or not Path(root_path).is_absolute() or Path(root_path).is_symlink():
        raise PermissionError("Selected workspace root is invalid")
    root = Path(root_path).resolve(strict=True)
    if not root.is_dir():
        raise PermissionError("Selected workspace root is not a directory")
    identity = metadata.get("directory_identity")
    root_stat = root.stat()
    if identity is not None and identity != [root_stat.st_dev, root_stat.st_ino]:
        raise PermissionError("Selected workspace directory identity has changed")
    for claimed in (workspace_root, context.get("workspace_root")):
        if claimed not in (None, "") and Path(claimed).expanduser().resolve(strict=True) != root:
            raise PermissionError("Workspace root does not match the selected canonical mount")
    return LiveWorkspaceBinding(selected, root, revision)


def _future_tool_context(context: dict[str, Any] | None) -> dict[str, Any]:
    # Approval to start this loop never grants approval to future model tools.
    clean = sanitize_untrusted_tool_context(context)
    return {key: clean[key] for key in ("principal_id", "user_id", "session_id", "conversation_id") if key in clean}


def _execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    workspace: LiveWorkspaceBinding,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch model arguments through real coding blocks and their gates."""
    try:
        current = resolve_live_workspace(workspace.root, workspace.workspace_id)
        if current != workspace:
            raise PermissionError("Selected workspace mount changed during this task")
        arguments = _normalize_args(tool_name, arguments)
        claimed_id = arguments.get("workspace_id")
        if claimed_id not in (None, "") and claimed_id != workspace.workspace_id:
            raise PermissionError("A model tool cannot replace the task workspace")
        if arguments.get("workspace_root") not in (None, ""):
            if Path(arguments["workspace_root"]).resolve(strict=True) != workspace.root:
                raise PermissionError("A model tool cannot replace the task workspace")
        definition = next((item for item in TOOL_DEFINITIONS if item["name"] == tool_name), None)
        if definition is None:
            return {"status": "error", "error": {"code": "UNKNOWN_TOOL", "message": f"unknown tool: {tool_name}"}}
        # Drop model-supplied approval tokens, transport metadata and authority
        # flags. Exact approved arguments are replayed by the existing followup.
        allowed_keys = definition["parameters"]["properties"]
        arguments = {key: value for key, value in arguments.items() if key in allowed_keys}
        arguments["workspace_id"] = workspace.workspace_id
        if tool_name == "coding_terminal_exec":
            cwd = (workspace.root / Path(arguments.get("cwd") or ".")).resolve(strict=True)
            if not cwd.is_relative_to(workspace.root) or not cwd.is_dir():
                raise PermissionError("Working directory must be inside the workspace")
            arguments["cwd"] = cwd.relative_to(workspace.root).as_posix()
        block_name = tool_name.removeprefix("coding_")
        block = import_module(f"blocks.coding.{block_name}")
        return block.run(arguments, {
            **_future_tool_context(context),
            "workspace_id": workspace.workspace_id,
            "workspace_root": str(workspace.root),
        })
    except Exception as exc:
        return {"status": "error", "error": {"code": "TOOL_ERROR", "message": str(exc)}}


def _build_system_prompt(workspace_root: Path) -> str:
    return (
        "You are a self-improving coding agent.\n"
        "Task: find ONE small safe improvement in the codebase, make it, test it, commit it.\n\n"
        "IMPORTANT — minimize tool calls:\n"
        "1. Read ONE file (coding_file_read).\n"
        "2. Patch it (coding_file_patch) — make ONE small change.\n"
        "3. Run tests (coding_terminal_exec: python -m pytest <test_file> -v).\n"
        "4. If tests pass, commit (coding_git_commit with paths=[<changed_file>]).\n"
        "5. STOP after committing. Do NOT explore further.\n\n"
        "Good changes: remove unused import, fix typo, add docstring, remove dead code.\n"
        "Do NOT modify .env, .git, test files, or config files.\n"
        f"Workspace: {workspace_root}\n"
    )


def _wait_for_tool_approval(
    runtime: SelfImprovingDefaultspackRuntime,
    task_id: str,
    tool_name: str,
    tool_result: dict[str, Any],
    progress: dict[str, Any],
) -> dict[str, Any]:
    """Keep the normal coding request and let its existing followup execute it."""
    from domain.safety import approval

    data = tool_result["data"]
    request_id = str(data.get("approval_request_id") or "")
    request = approval.get_approval_request(request_id)
    if not isinstance(request, dict):
        raise RuntimeError("Tool approval request is unavailable")
    details = request.get("details") or {}
    result = {
        **progress,
        **data,
        "success": False,
        "status": "approval_pending",
        "approval_required": True,
        "tool_name": details.get("tool_name") or tool_name,
        "function_id": details.get("function_id") or tool_name,
        "arguments": details.get("arguments") or {},
        "error": None,
    }
    runtime.wait_for_approval(task_id, result)
    return result


def run_live_improvement(
    *,
    workspace_root: str | Path | None = None,
    workspace_id: str | None = None,
    context: dict[str, Any] | None = None,
    task_id: str = "live_01",
    task_title: str = "Live self-improvement: find and fix one small issue",
    max_tool_calls: int = 15,
    model: str = DEFAULT_MIMO_MAIN_MODEL,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run one live self-improvement cycle with MiMo v2.5 Pro.

    Returns a result dict with:
      success, task_id, commit_hash, files_modified, test_exit_code,
      tool_calls_made, model, elapsed_seconds, error
    """
    try:
        workspace = resolve_live_workspace(workspace_root, workspace_id, context)
        workspace_root = workspace.root
        state_path = (
            Path(state_path).expanduser().resolve()
            if state_path
            else workspace_root / "user_data" / "shared" / "self_improvement" / "live_state.json"
        )
        state_path = state_path.resolve()
        if not state_path.is_relative_to(workspace_root):
            raise PermissionError("Task state must be inside the selected workspace")
    except Exception as exc:
        return {
            "success": False, "task_id": task_id,
            "error": str(exc), "error_code": "WORKSPACE_BINDING_INVALID",
        }
    tool_context = _future_tool_context(context)

    runtime = create_mimo_profile(workspace_root=workspace_root, state_path=state_path)
    try:
        # Reserve the existing task before any model request. Concurrent API
        # retries cannot both pass an empty task list and repeat mutations.
        with NamedLock(state_path.parent / "locks", f"{state_path.name}.live", timeout_ms=0):
            tasks = runtime.status().get("tasks", [])
            for task in tasks:
                if task.get("task_id") != task_id:
                    continue
                previous = task.get("result")
                if isinstance(previous, dict):
                    if previous.get("workspace_id", workspace.workspace_id) != workspace.workspace_id:
                        raise PermissionError("Stored task belongs to another workspace")
                    # The normal coding followup owns the pending one-shot.
                    return {
                        "task_id": task_id,
                        "success": bool(previous.get("commit_hash")),
                        "status": task.get("status"),
                        **previous,
                    }
                return {
                    "success": False, "task_id": task_id,
                    "status": task.get("status"), "error": "TASK_ALREADY_STARTED",
                }
            if any(task.get("status") == "running" for task in tasks):
                return {"success": False, "task_id": task_id, "error": "TASK_ALREADY_RUNNING"}
            provider = _configured_mimo_provider()
            if not provider._api_key:
                return {
                    "success": False,
                    "task_id": task_id,
                    "error": "MIMO_API_KEY not set. Export XIAOMI_MIMO_TOKEN_PLAN_SGP_API_KEY or MIMO_API_KEY.",
                }
            runtime.bootstrap()
            runtime.add_task(task_id, task_title, expected_outcome="one small fix committed")
            runtime.start_task(task_id)
    except LockTimeout:
        return {"success": False, "task_id": task_id, "error": "TASK_ALREADY_RUNNING"}
    except PermissionError as exc:
        return {
            "success": False, "task_id": task_id,
            "error": str(exc), "error_code": "WORKSPACE_BINDING_INVALID",
        }

    tools = _adapt_tools(workspace.workspace_id)
    system_prompt = _build_system_prompt(workspace_root)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    start_time = time.time()
    tool_calls_made = 0
    files_read: list[str] = []
    files_modified: list[str] = []
    last_commit_hash = ""
    last_test_exit_code = -1
    error_message = ""

    selected_model = str(model or DEFAULT_MIMO_MAIN_MODEL)
    api_model = selected_model.split("/", 1)[1] if "/" in selected_model else selected_model
    full_model = f"{provider.provider_id}/{api_model}"

    def progress() -> dict[str, Any]:
        return {
            "task_id": task_id,
            "workspace_id": workspace.workspace_id,
            "commit_hash": last_commit_hash,
            "files_modified": list(files_modified),
            "files_read": list(files_read),
            "test_exit_code": last_test_exit_code,
            "tool_calls_made": tool_calls_made,
            "model": full_model,
            "elapsed_seconds": round(time.time() - start_time, 2),
        }

    try:
        for turn in range(max_tool_calls + 1):
            if tool_calls_made >= max_tool_calls:
                break

            response = provider.complete(api_model, messages, tools, {"thinking_level": "medium", "max_tokens": 4096})

            content = response.get("content", [])
            text_parts = [c for c in content if c.get("type") == "text"]
            tool_uses = [c for c in content if c.get("type") == "tool_use"]

            assistant_text = "\n".join(p.get("text", "") for p in text_parts)

            if not tool_uses:
                messages.append({"role": "assistant", "content": assistant_text})
                break

            openai_tool_calls = []
            for tu in tool_uses:
                tc_id = tu.get("id", "")
                tc_name = tu.get("name", "")
                raw_input = tu.get("input", "{}")
                if isinstance(raw_input, dict):
                    tc_args = json.dumps(raw_input, ensure_ascii=False)
                else:
                    tc_args = str(raw_input)
                openai_tool_calls.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": tc_name, "arguments": tc_args},
                })

            messages.append({
                "role": "assistant",
                "content": assistant_text,
                "tool_calls": openai_tool_calls,
            })

            for tool_use in tool_uses:
                tool_name = tool_use.get("name", "")
                raw_input = tool_use.get("input", "{}")
                if isinstance(raw_input, str):
                    try:
                        arguments = json.loads(raw_input)
                    except json.JSONDecodeError:
                        arguments = {}
                else:
                    arguments = dict(raw_input)

                runtime.record_tool_call(tool_name, {"workspace_id": workspace.workspace_id})
                tool_calls_made += 1

                result = _execute_tool(tool_name, arguments, workspace, tool_context)
                if result.get("data", {}).get("approval_required"):
                    return _wait_for_tool_approval(
                        runtime, task_id, tool_name, result, progress(),
                    )
                result_str = json.dumps(result, ensure_ascii=False, default=str)[:1500]
                executed = (
                    result.get("status") == "ok"
                    and not result.get("data", {}).get("approval_required")
                )

                if tool_name == "coding_file_read" and executed:
                    path = arguments.get("path", "")
                    if path and path not in files_read:
                        files_read.append(path)

                if tool_name == "coding_file_patch" and executed:
                    path = arguments.get("path", "")
                    if path and path not in files_modified:
                        files_modified.append(path)

                if tool_name == "coding_terminal_exec" and executed:
                    exit_code = result.get("data", {}).get("exit_code", -1)
                    if "pytest" in arguments.get("command", ""):
                        last_test_exit_code = exit_code
                        runtime.record_test_result(arguments["command"], exit_code, result_str[:500])

                if tool_name == "coding_git_commit" and executed:
                    data = result.get("data", {})
                    last_commit_hash = data.get("commit_hash", "")
                    runtime.record_commit(last_commit_hash, arguments.get("message", ""), paths=data.get("paths"))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_use.get("id", ""),
                    "content": result_str,
                })

                if tool_calls_made >= max_tool_calls:
                    break

    except Exception as exc:
        error_message = str(exc)
        runtime.record_model_error(error_message)

    if not last_commit_hash and files_modified and not error_message:
        try:
            test_cmd = f'"{sys.executable}" -m pytest test_utils.py -v --tb=short'
            tested = _execute_tool(
                "coding_terminal_exec", {"command": test_cmd}, workspace, tool_context,
            )
            if tested.get("data", {}).get("approval_required"):
                return _wait_for_tool_approval(
                    runtime, task_id, "coding_terminal_exec", tested, progress(),
                )
            last_test_exit_code = tested.get("data", {}).get("exit_code", -1)
            runtime.record_test_result(test_cmd, last_test_exit_code, str(tested)[:500])

            if last_test_exit_code == 0:
                commit_msg = f"auto: improve {', '.join(files_modified)}"
                committed = _execute_tool(
                    "coding_git_commit",
                    {"message": commit_msg, "paths": files_modified}, workspace, tool_context,
                )
                if committed.get("data", {}).get("approval_required"):
                    return _wait_for_tool_approval(
                        runtime, task_id, "coding_git_commit", committed, progress(),
                    )
                last_commit_hash = committed.get("data", {}).get("commit_hash", "")
                if last_commit_hash:
                    runtime.record_commit(last_commit_hash, commit_msg, paths=files_modified)
        except Exception as fallback_exc:
            if not error_message:
                error_message = f"auto-commit fallback failed: {fallback_exc}"

    final_result = {
        **progress(),
        "success": not bool(error_message) and bool(last_commit_hash),
        "error": error_message or None,
    }

    if error_message:
        runtime.fail_task(task_id, error_message)
    else:
        runtime.complete_task(task_id, final_result)

    return final_result


def run_multi_task_dogfood(
    *,
    workspace_root: str | Path | None = None,
    workspace_id: str | None = None,
    context: dict[str, Any] | None = None,
    task_count: int = 3,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run multiple self-improvement tasks in sequence.

    Returns aggregate results.
    """
    tasks = [
        ("live_01_read_and_fix", "Read one file and fix a minor issue"),
        ("live_02_add_docstring", "Add or improve a docstring"),
        ("live_03_clean_dead_code", "Remove dead code or unused imports"),
    ][:task_count]

    results = []
    for task_id, task_title in tasks:
        result = run_live_improvement(
            workspace_root=workspace_root,
            workspace_id=workspace_id,
            context=context,
            task_id=task_id,
            task_title=task_title,
            state_path=state_path,
        )
        results.append(result)
        if result.get("approval_required"):
            break

    successful = [r for r in results if r.get("success")]
    pending = [r for r in results if r.get("approval_required")]
    failed = [r for r in results if not r.get("success") and not r.get("approval_required")]

    return {
        "total_tasks": len(results),
        "successful": len(successful),
        "failed": len(failed),
        "approval_waiting": len(pending),
        "results": results,
        "generated_at": _timestamp(),
    }


def run_vision_qa(
    *,
    image_path: str | Path | None = None,
    image_data_url: str | None = None,
    question: str = "Describe this UI screenshot. Identify any layout issues, overlapping elements, or visual bugs.",
    model: str = DEFAULT_MIMO_VISION_MODEL,
) -> dict[str, Any]:
    """Run MiMo Omni vision QA on an image.

    Returns: {success, answer, model, usage, error}
    """
    import base64

    provider = _configured_mimo_provider()
    if not provider._api_key:
        return {"success": False, "error": "MIMO_API_KEY not set"}

    selected_model = str(model or DEFAULT_MIMO_VISION_MODEL)
    api_model = selected_model.split("/", 1)[1] if "/" in selected_model else selected_model

    if image_path and not image_data_url:
        raw = Path(image_path).read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        suffix = Path(image_path).suffix.lower()
        mime = {"png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(suffix, "image/png")
        image_data_url = f"data:{mime};base64,{b64}"

    if not image_data_url:
        return {"success": False, "error": "No image provided"}

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        },
    ]

    try:
        resp = provider.complete(api_model, messages, [], {"thinking_level": "none", "max_tokens": 512})
        content = resp.get("content", [])
        answer = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        return {
            "success": True,
            "answer": answer,
            "model": f"{provider.provider_id}/{api_model}",
            "usage": resp.get("usage", {}),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "model": f"{provider.provider_id}/{api_model}"}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Live MiMo self-improvement loop")
    parser.add_argument("--workspace", default=".", help="Workspace root directory")
    parser.add_argument("--task-id", default="live_01", help="Task ID")
    parser.add_argument("--task-title", default="Live self-improvement: find and fix one small issue")
    parser.add_argument("--max-tool-calls", type=int, default=15)
    parser.add_argument("--model", default=DEFAULT_MIMO_MAIN_MODEL)
    parser.add_argument("--multi", type=int, default=0, help="Run N tasks (0=single)")
    args = parser.parse_args()

    if args.multi > 0:
        result = run_multi_task_dogfood(
            workspace_root=args.workspace,
            task_count=args.multi,
        )
    else:
        result = run_live_improvement(
            workspace_root=args.workspace,
            task_id=args.task_id,
            task_title=args.task_title,
            max_tool_calls=args.max_tool_calls,
            model=args.model,
        )

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
