"""Live MiMo-driven self-improvement loop.

Drives a real model through tool calls to inspect, patch, test, and commit
improvements to defaultspack.  Unlike the smoke tests, this hits the actual
MiMo API and executes real tools.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_DEFAULTSPACK_ROOT = str(Path(__file__).resolve().parents[2])
if _DEFAULTSPACK_ROOT not in sys.path:
    sys.path.insert(0, _DEFAULTSPACK_ROOT)

from domain.agent.self_improvement_runtime import MIMO_ROLE_MAP, create_mimo_profile  # noqa: E402
from domain.ai_client.api_key_store import read_provider_api_key  # noqa: E402
from domain.ai_client.providers.xiaomi_mimo_token_plan_provider import (  # noqa: E402
    XiaomiMimoTokenPlanSgpProvider,
)
from domain.coding.git_ops import GitOps  # noqa: E402
from domain.tool.schema_adapter import adapt_tool_definition  # noqa: E402


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


def _adapt_tools() -> list[dict[str, Any]]:
    return [adapt_tool_definition(t) for t in TOOL_DEFINITIONS]


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


def _execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_root: Path,
) -> dict[str, Any]:
    """Execute a tool by dispatching to the appropriate block function."""
    arguments = _normalize_args(tool_name, arguments)
    try:
        if tool_name == "coding_file_read":
            from blocks.coding.file_read import run as file_read_run
            return file_read_run(arguments, {"workspace_root": str(workspace_root)})

        if tool_name == "coding_file_patch":
            from blocks.coding.file_patch import run as file_patch_run
            ctx = {
                "workspace_root": str(workspace_root),
                "_tool_server_approved": True,
                "_tool_server_approval_token_valid": True,
            }
            return file_patch_run(arguments, ctx)

        if tool_name == "coding_terminal_exec":
            command = arguments.get("command", "")
            cwd = arguments.get("cwd") or str(workspace_root)
            timeout = int(arguments.get("timeout", 60))
            proc = subprocess.run(
                command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            )
            return {
                "status": "ok",
                "data": {
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout[-2000:] if proc.stdout else "",
                    "stderr": proc.stderr[-1000:] if proc.stderr else "",
                },
            }

        if tool_name == "coding_git_commit":
            from blocks.coding.git_commit import run as git_commit_run
            ctx = {
                "workspace_root": str(workspace_root),
                "_tool_server_approved": True,
                "_tool_server_approval_token_valid": True,
            }
            return git_commit_run(arguments, ctx)

        if tool_name == "coding_git_status":
            git = GitOps(workspace_root)
            return {"status": "ok", "data": git.status()}

        if tool_name == "coding_file_search":
            import glob as globmod
            pattern = arguments.get("pattern", "*")
            matches = sorted(globmod.glob(str(workspace_root / "**" / pattern), recursive=True))
            rel = [str(Path(m).relative_to(workspace_root)) for m in matches[:50]]
            return {"status": "ok", "data": {"matches": rel, "count": len(rel)}}

        return {"status": "error", "error": {"code": "UNKNOWN_TOOL", "message": f"unknown tool: {tool_name}"}}
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


def run_live_improvement(
    *,
    workspace_root: str | Path | None = None,
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
    workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
    state_path = Path(state_path) if state_path else workspace_root / "user_data" / "shared" / "self_improvement" / "live_state.json"

    runtime = create_mimo_profile(workspace_root=workspace_root, state_path=state_path)
    runtime.bootstrap()

    provider = _configured_mimo_provider()
    if not provider._api_key:
        return {
            "success": False,
            "task_id": task_id,
            "error": "MIMO_API_KEY not set. Export XIAOMI_MIMO_TOKEN_PLAN_SGP_API_KEY or MIMO_API_KEY.",
        }

    runtime.add_task(task_id, task_title, expected_outcome="one small fix committed")
    runtime.start_task(task_id)

    tools = _adapt_tools()
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

                runtime.record_tool_call(tool_name, arguments)
                tool_calls_made += 1

                result = _execute_tool(tool_name, arguments, workspace_root)
                result_str = json.dumps(result, ensure_ascii=False, default=str)[:1500]

                if tool_name == "coding_file_read" and result.get("status") == "ok":
                    path = arguments.get("path", "")
                    if path and path not in files_read:
                        files_read.append(path)

                if tool_name == "coding_file_patch" and result.get("status") == "ok":
                    path = arguments.get("path", "")
                    if path and path not in files_modified:
                        files_modified.append(path)

                if tool_name == "coding_terminal_exec" and result.get("status") == "ok":
                    exit_code = result.get("data", {}).get("exit_code", -1)
                    if "pytest" in arguments.get("command", ""):
                        last_test_exit_code = exit_code
                        runtime.record_test_result(arguments["command"], exit_code, result_str[:500])

                if tool_name == "coding_git_commit" and result.get("status") == "ok":
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
            test_cmd = f"{sys.executable} -m pytest test_utils.py -v --tb=short"
            proc = subprocess.run(test_cmd, shell=True, cwd=str(workspace_root), capture_output=True, text=True, timeout=30)
            last_test_exit_code = proc.returncode
            runtime.record_test_result(test_cmd, proc.returncode, (proc.stdout + proc.stderr)[:500])

            if proc.returncode == 0:
                git = GitOps(workspace_root)
                commit_msg = f"auto: improve {', '.join(files_modified)}"
                commit_result = git.commit(commit_msg, paths=files_modified)
                last_commit_hash = commit_result.get("commit_hash", "")
                runtime.record_commit(last_commit_hash, commit_msg, paths=files_modified)
                runtime.record_tool_call("coding_git_commit (auto)", {"message": commit_msg, "paths": files_modified})
        except Exception as fallback_exc:
            if not error_message:
                error_message = f"auto-commit fallback failed: {fallback_exc}"

    elapsed = time.time() - start_time

    if error_message:
        runtime.fail_task(task_id, error_message)
    else:
        runtime.complete_task(task_id, {
            "files_read": files_read,
            "files_modified": files_modified,
            "test_exit_code": last_test_exit_code,
            "commit_hash": last_commit_hash,
            "tool_calls_made": tool_calls_made,
            "model": full_model,
        })

    return {
        "success": not bool(error_message) and bool(last_commit_hash),
        "task_id": task_id,
        "commit_hash": last_commit_hash,
        "files_modified": files_modified,
        "files_read": files_read,
        "test_exit_code": last_test_exit_code,
        "tool_calls_made": tool_calls_made,
        "model": full_model,
        "elapsed_seconds": round(elapsed, 2),
        "error": error_message or None,
    }


def run_multi_task_dogfood(
    *,
    workspace_root: str | Path | None = None,
    task_count: int = 3,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run multiple self-improvement tasks in sequence.

    Returns aggregate results.
    """
    workspace_root = Path(workspace_root) if workspace_root else Path.cwd()
    state_path = Path(state_path) if state_path else workspace_root / "user_data" / "shared" / "self_improvement" / "live_state.json"

    tasks = [
        ("live_01_read_and_fix", "Read one file and fix a minor issue"),
        ("live_02_add_docstring", "Add or improve a docstring"),
        ("live_03_clean_dead_code", "Remove dead code or unused imports"),
    ][:task_count]

    results = []
    for task_id, task_title in tasks:
        result = run_live_improvement(
            workspace_root=workspace_root,
            task_id=task_id,
            task_title=task_title,
            state_path=state_path,
        )
        results.append(result)

    successful = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]

    return {
        "total_tasks": len(results),
        "successful": len(successful),
        "failed": len(failed),
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
