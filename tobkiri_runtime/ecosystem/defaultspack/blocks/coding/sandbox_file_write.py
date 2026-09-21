from __future__ import annotations

from typing import Any

from blocks._common import error
from blocks.coding.sandbox_common import run_sandbox_control


def run(input_data: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    if not input_data.get("path"):
        return error("'path' is required", code="INVALID_INPUT")
    if input_data.get("content") is None:
        return error("'content' is required", code="INVALID_INPUT")
    return run_sandbox_control(
        input_data,
        context,
        legacy_operation="sandbox.file_write",
        control_operation="write",
        arguments=lambda sandbox_id, workspace_id: {
            "sandbox_id": sandbox_id,
            "path": str(input_data.get("path") or ""),
            "content": str(input_data.get("content") or ""),
            "old": "",
            "new": "",
            "workspace_id": workspace_id,
        },
    )
