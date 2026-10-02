from __future__ import annotations

from typing import Any

from blocks.coding.sandbox_common import run_sandbox_observe


def run(input_data: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return run_sandbox_observe(
        input_data,
        context,
        legacy_operation="sandbox.diff_preview",
        observe_operation="diff",
    )
