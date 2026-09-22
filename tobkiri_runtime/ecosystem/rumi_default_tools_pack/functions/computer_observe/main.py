from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core_runtime.defaultspack_host_contract_adapter import run_host_contract_action


def run(context, args):
    try:
        payload = {
            "app": (args or {}).get("app"),
            "pid": (args or {}).get("pid"),
            "window_id": (args or {}).get("window_id"),
            "window_title": (args or {}).get("window_title"),
        }
        return run_host_contract_action(
            "computer.observe",
            payload,
            source_function_id="computer_observe",
        )
    except Exception as e:
        return {"action": "computer.observe", "error": str(e)}
