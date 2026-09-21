from __future__ import annotations

from typing import Any

from blocks.coding.sandbox_common import run_sandbox_action


def run(input_data: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return run_sandbox_action(
        input_data,
        context,
        lambda manager, workspace: manager.export_artifacts(workspace, input_data.get("paths")),
    )
