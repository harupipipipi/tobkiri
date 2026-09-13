from __future__ import annotations

from typing import Any

from blocks._common import error
from blocks.coding.sandbox_common import run_sandbox_observe


def run(input_data: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    if not input_data.get("path"):
        return error("'path' is required", code="INVALID_INPUT")
    return run_sandbox_observe(
        input_data,
        context,
        legacy_operation="sandbox.file_read",
        observe_operation="read",
        payload={
            "path": input_data.get("path"),
            "encoding": input_data.get("encoding") or "utf-8",
        },
    )
