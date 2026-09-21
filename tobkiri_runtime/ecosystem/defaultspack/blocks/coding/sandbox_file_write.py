from __future__ import annotations

from typing import Any

from blocks._common import error
from blocks.coding.sandbox_common import run_sandbox_action


def run(input_data: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    if not input_data.get("path"):
        return error("'path' is required", code="INVALID_INPUT")
    if input_data.get("content") is None:
        return error("'content' is required", code="INVALID_INPUT")
    return run_sandbox_action(
        input_data,
        context,
        lambda manager, workspace: manager.write_file(
            workspace,
            input_data.get("path"),
            input_data.get("content"),
        ),
    )
