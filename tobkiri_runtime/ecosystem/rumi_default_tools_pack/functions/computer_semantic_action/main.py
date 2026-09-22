from __future__ import annotations

from ecosystem.rumi_default_tools_pack import run_host_contract_action


def run(context, args):
    """Request a semantic action through the captured Host contract."""
    del context
    a = args or {}
    payload = {
        "app": a.get("app"),
        "pid": a.get("pid"),
        "window_id": a.get("window_id"),
        "intent": a.get("intent", ""),
    }
    if a.get("element_id"):
        payload["element_id"] = a["element_id"]
    elif a.get("point"):
        payload["point"] = a["point"]
    return run_host_contract_action(
        "computer.semantic_action",
        payload,
        source_function_id="computer_semantic_action",
    )
