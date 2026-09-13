from __future__ import annotations

import base64
import shlex
import sys
from typing import Any

from domain.coding.terminal_policy import SHELL_ESCAPE_MARKERS
from domain.tool_policy.internal_context import (
    internal_tool_decision_allows,
    tool_server_approval_context_is_internal,
)

from ._agent_os_common import err, now_slug, ok, workspace

MANAGED_RUNTIME_NOT_READY = "MANAGED_RUNTIME_NOT_READY"
MAX_SCRIPT_PATH_BYTES = 2 * 1024 * 1024


def sandbox_exec(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    plan = _command_plan(arguments)
    if plan.get("error"):
        return err(plan["error"], plan["code"])
    argv = plan["argv"]
    if not argv:
        return err("'command' is required", "INVALID_INPUT")
    timeout_ms = _timeout_ms(arguments)
    sandbox_id = str(arguments.get("sandbox_id") or "").strip()
    if sandbox_id:
        return _sandbox_exec_call(sandbox_id, arguments, argv, timeout_ms, context)

    template_id = str(arguments.get("template_id") or "tool.ephemeral")
    create = _sandbox_api().run(
        {
            "_handler": "sandboxes_create",
            "template_id": template_id,
            "provider_id": str(arguments.get("provider_id") or "auto"),
            "name": str(arguments.get("name") or f"Ephemeral Sandbox {now_slug()}"),
        },
        context or {},
    )
    if create.get("status") != "ok":
        return err(
            "Managed sandbox runtime is not ready; sandbox_exec will not fall back to host execution.",
            MANAGED_RUNTIME_NOT_READY,
            argv=argv,
            template_id=template_id,
            provider_id=str(arguments.get("provider_id") or "auto"),
            runtime_error=create.get("error"),
        )
    sandbox_id = str((create.get("data") or {}).get("sandbox_id") or "")
    if not sandbox_id:
        return err("Managed sandbox runtime did not return a sandbox id.", MANAGED_RUNTIME_NOT_READY, argv=argv, template_id=template_id)
    try:
        return _sandbox_exec_call(sandbox_id, arguments, argv, timeout_ms, context)
    finally:
        _sandbox_api().run(
            {"_handler": "sandbox_delete", "sandbox_id": sandbox_id, "confirm_destructive": True},
            context or {},
        )


def sandbox_files_apply_patch(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    payload = dict(arguments or {})
    sandbox_id = str(payload.get("sandbox_id") or "").strip()
    if not sandbox_id:
        return err("'sandbox_id' is required", "INVALID_INPUT")
    payload["sandbox_id"] = sandbox_id
    payload["_handler"] = "sandbox_files_apply_patch"
    return _sandbox_api().run(payload, context or {})


def sandbox_files_read(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(arguments or {})
    sandbox_id = str(payload.get("sandbox_id") or "").strip()
    if not sandbox_id:
        return err("'sandbox_id' is required", "INVALID_INPUT")
    if not str(payload.get("path") or "").strip():
        return err("'path' is required", "INVALID_INPUT")
    payload["sandbox_id"] = sandbox_id
    payload["_handler"] = "sandbox_files_read"
    return _sandbox_api().run(payload, context or {})


def sandbox_port_expose(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    payload = dict(arguments or {})
    sandbox_id = str(payload.get("sandbox_id") or "").strip()
    if not sandbox_id:
        return err("'sandbox_id' is required", "INVALID_INPUT")
    if payload.get("port") is None:
        return err("'port' is required", "INVALID_INPUT")
    payload["sandbox_id"] = sandbox_id
    payload["_handler"] = "sandbox_port_expose"
    return _sandbox_api().run(payload, context or {})


def python_exec(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    code = arguments.get("code")
    script_path = arguments.get("script_path")
    if not code and not script_path:
        return err("'code' or 'script_path' is required", "INVALID_INPUT")
    try:
        if script_path:
            ws = workspace(context)
            resolved = ws.resolve(str(script_path), must_exist=True)
            script_path = ws.relative(resolved)
        if code:
            return sandbox_exec(
                {
                    "argv": ["python", "-c", str(code)],
                    "timeout": arguments.get("timeout") or 30,
                    "template_id": arguments.get("template_id") or "coding.python",
                    "provider_id": arguments.get("provider_id") or "auto",
                },
                context,
            )
        return _script_path_exec(arguments, context, runtime_argv=["python"], script_path=str(script_path), template_id="coding.python")
    except Exception as exc:
        return err(str(exc), "PYTHON_EXEC_FAILED")


def node_exec(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    approval_error = _require_server_side_approval(context)
    if approval_error is not None:
        return approval_error
    code = arguments.get("code")
    script_path = arguments.get("script_path")
    if not code and not script_path:
        return err("'code' or 'script_path' is required", "INVALID_INPUT")
    try:
        if script_path:
            ws = workspace(context)
            resolved = ws.resolve(str(script_path), must_exist=True)
            script_path = ws.relative(resolved)
        if code:
            return sandbox_exec(
                {
                    "argv": ["node", "-e", str(code)],
                    "timeout": arguments.get("timeout") or 30,
                    "template_id": arguments.get("template_id") or "coding.node",
                    "provider_id": arguments.get("provider_id") or "auto",
                },
                context,
            )
        return _script_path_exec(arguments, context, runtime_argv=["node"], script_path=str(script_path), template_id="coding.node")
    except Exception as exc:
        return err(str(exc), "NODE_EXEC_FAILED")


def package_install_plan(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    packages = arguments.get("packages")
    manager = str(arguments.get("manager") or "pip").lower()
    if isinstance(packages, str):
        packages = shlex.split(packages)
    if not isinstance(packages, list):
        packages = []
    command = {
        "pip": [sys.executable, "-m", "pip", "install", *packages],
        "npm": ["npm", "install", *packages],
        "pnpm": ["pnpm", "add", *packages],
    }.get(manager, [manager, *packages])
    return ok({"manager": manager, "packages": packages, "command": command, "executes": False})


def _require_server_side_approval(context: dict[str, Any] | None) -> dict[str, Any] | None:
    if internal_tool_decision_allows(context):
        return None
    if tool_server_approval_context_is_internal(context):
        return None
    return err("sandbox execution requires a server-side approval decision", "SANDBOX_APPROVAL_REQUIRED")


def _sandbox_api():
    try:
        from ecosystem.defaultspack.blocks.sandbox import api
    except ModuleNotFoundError:
        from blocks.sandbox import api  # type: ignore
    return api


def _script_path_exec(
    arguments: dict[str, Any],
    context: dict[str, Any] | None,
    *,
    runtime_argv: list[str],
    script_path: str,
    template_id: str,
) -> dict[str, Any]:
    ws = workspace(context)
    resolved = ws.resolve(script_path, must_exist=True)
    relative_path = ws.relative(resolved)
    if resolved.stat().st_size > MAX_SCRIPT_PATH_BYTES:
        return err("script_path is too large for sandbox staging", "SCRIPT_PATH_TOO_LARGE", script_path=relative_path)

    api = _sandbox_api()
    create = api.run(
        {
            "_handler": "sandboxes_create",
            "template_id": str(arguments.get("template_id") or template_id),
            "provider_id": str(arguments.get("provider_id") or "auto"),
            "name": str(arguments.get("name") or f"Script Sandbox {now_slug()}"),
        },
        context or {},
    )
    if create.get("status") != "ok":
        return err(
            "Managed sandbox runtime is not ready; script_path will not fall back to host execution.",
            MANAGED_RUNTIME_NOT_READY,
            script_path=relative_path,
            template_id=str(arguments.get("template_id") or template_id),
            provider_id=str(arguments.get("provider_id") or "auto"),
            runtime_error=create.get("error"),
        )
    sandbox_id = str((create.get("data") or {}).get("sandbox_id") or "")
    if not sandbox_id:
        return err("Managed sandbox runtime did not return a sandbox id.", MANAGED_RUNTIME_NOT_READY, script_path=relative_path)

    try:
        patch = api.run(
            {
                "_handler": "sandbox_files_apply_patch",
                "sandbox_id": sandbox_id,
                "files": [
                    {
                        "path": relative_path,
                        "content_base64": base64.b64encode(resolved.read_bytes()).decode("ascii"),
                    }
                ],
            },
            context or {},
        )
        if patch.get("status") != "ok":
            patch_error = patch.get("error") if isinstance(patch.get("error"), dict) else {}
            return err(
                "Managed sandbox runtime could not stage script_path.",
                str(patch_error.get("code") or "SANDBOX_SCRIPT_STAGE_FAILED"),
                script_path=relative_path,
                runtime_error=patch.get("error"),
            )
        return _sandbox_exec_call(
            sandbox_id,
            {
                **arguments,
                "argv": [*runtime_argv, relative_path],
                "cwd": arguments.get("cwd") or ".",
            },
            [*runtime_argv, relative_path],
            _timeout_ms(arguments),
            context,
        )
    finally:
        api.run(
            {"_handler": "sandbox_delete", "sandbox_id": sandbox_id, "confirm_destructive": True},
            context or {},
        )


def _sandbox_exec_call(
    sandbox_id: str,
    arguments: dict[str, Any],
    argv: list[str],
    timeout_ms: int,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    return _sandbox_api().run(
        {
            "_handler": "sandbox_exec",
            "sandbox_id": sandbox_id,
            "argv": argv,
            "cwd": arguments.get("cwd") or ".",
            "env": arguments.get("env") or {},
            "stdin": arguments.get("stdin"),
            "timeout_ms": timeout_ms,
            "client_request_id": str(arguments.get("client_request_id") or f"sandbox-exec-{now_slug()}"),
        },
        context or {},
    )


def _timeout_ms(arguments: dict[str, Any]) -> int:
    try:
        timeout_ms = arguments.get("timeout_ms")
        if timeout_ms is not None:
            return int(timeout_ms)
        return int(arguments.get("timeout") or 60) * 1000
    except (TypeError, ValueError):
        return 60_000


def _command_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    argv = arguments.get("argv")
    if argv is None:
        argv = arguments.get("command")
    if isinstance(argv, (list, tuple)):
        return {"argv": [str(part) for part in argv if str(part) != ""]}
    if isinstance(argv, str):
        stripped = argv.strip()
        if not stripped:
            return {"argv": []}
        if any(marker in stripped for marker in SHELL_ESCAPE_MARKERS):
            return {
                "error": "sandbox_exec accepts argv arrays; shell syntax is not allowed in command strings",
                "code": "SANDBOX_SHELL_STRING_REJECTED",
            }
        try:
            posix = sys.platform != "win32"
            parts = shlex.split(stripped, posix=posix)
            if not posix:
                parts = [_strip_matching_quotes(part) for part in parts]
            return {"argv": parts}
        except ValueError as exc:
            return {"error": f"invalid command string: {exc}", "code": "INVALID_INPUT"}
    return {"error": "'command' must be a string or argv array", "code": "INVALID_INPUT"}


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
