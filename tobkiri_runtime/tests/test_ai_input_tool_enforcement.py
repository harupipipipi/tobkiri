from __future__ import annotations

import sys
from pathlib import Path

DEFAULTSPACK_ROOT = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from blocks.tool.invoke import run as invoke_tool  # noqa: E402


def test_direct_tool_call_requires_a_capability_plan() -> None:
    result = invoke_tool(
        {"tool_name": "computer_use", "arguments": {"action": "noop"}},
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "CAPABILITY_PLAN_REQUIRED"
