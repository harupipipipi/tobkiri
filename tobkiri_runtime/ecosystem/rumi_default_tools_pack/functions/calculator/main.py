from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from functions._tool_common import tool_result
from ecosystem.rumi_default_tools_pack.domain.tool.calculator import calculate


def run(context: dict, args: dict) -> dict:
    """Evaluate the legacy Calculator entry through its owned implementation."""
    expression = args.get("expression", "")
    calculation = _safe_calculate(expression)
    if calculation["is_error"]:
        return tool_result(calculation["error"], is_error=True)
    return tool_result("Calculated: {} = {}".format(expression, calculation["result"]))


def _safe_calculate(expression: str) -> dict:
    try:
        result = calculate(expression)
    except ValueError as exc:
        return {"is_error": True, "error": f"Calculator error: {exc}"}
    return {"is_error": False, "result": result}
