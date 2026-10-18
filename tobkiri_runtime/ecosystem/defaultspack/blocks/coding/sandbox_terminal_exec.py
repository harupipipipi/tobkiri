"""Execute a digest-pinned command in the selected coding sandbox."""

from __future__ import annotations

import shlex
from typing import Any

from blocks._common import error, ok
from blocks.coding.sandbox_common import run_sandbox_control


def run(
    input_data: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Route sandbox execution through its receipt-gated service owner."""

    if input_data.get("network") or input_data.get("network_enabled"):
        return ok(
            {
                "requires_approval": True,
                "approval_required": True,
                "operation": "sandbox.network.request",
                "message": "This sandbox service does not provide network access.",
                "sandbox_only": True,
                "host_modified": False,
            }
        )
    try:
        command = _command(input_data)
    except ValueError as exc:
        return error(str(exc), code="INVALID_INPUT")
    image = str(input_data.get("image") or "").strip()
    if not image:
        return error("'image' is required", code="INVALID_INPUT")
    if str(input_data.get("cwd") or ".") != ".":
        return error(
            "this sandbox contract only executes from the workspace root",
            code="INVALID_INPUT",
        )
    timeout = max(
        1,
        min(
            300,
            int(input_data.get("timeout") or input_data.get("timeout_seconds") or 60),
        ),
    )
    return run_sandbox_control(
        input_data,
        context,
        legacy_operation="sandbox.terminal_exec",
        control_operation="execute",
        arguments=lambda sandbox_id, workspace_id: {
            "sandbox_id": sandbox_id,
            "image": image,
            "command": command,
            "timeout": timeout,
            "workspace_id": workspace_id,
        },
    )


def _command(input_data: dict[str, Any]) -> list[str]:
    argv = input_data.get("argv")
    if isinstance(argv, list) and argv:
        return [str(item) for item in argv]
    command = str(input_data.get("command") or "").strip()
    if not command:
        raise ValueError("'command' or 'argv' is required")
    parsed = shlex.split(command)
    if not parsed:
        raise ValueError("command is empty")
    return parsed
