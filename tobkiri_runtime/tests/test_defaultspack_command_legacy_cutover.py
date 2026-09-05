from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from blocks.ui.commands import run  # noqa: E402


def test_legacy_command_catalog_is_read_only_v1_projection() -> None:
    result = run({"_method": "GET"}, {})

    assert result["status"] == "ok"
    assert result["data"]["deprecated"] is True
    assert len(result["data"]["commands"]) == 55
    assert all(
        command.get("canonical_id")
        for command in result["data"]["commands"]
    )


def test_legacy_command_execution_cannot_bypass_v1() -> None:
    result = run(
        {"_method": "POST", "command": "home_title", "args": {"value": "x"}},
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "COMMAND_PROTOCOL_V1_REQUIRED"
