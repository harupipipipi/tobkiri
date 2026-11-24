from __future__ import annotations

from ecosystem.rumi_default_tools_pack import run_host_contract_action


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
