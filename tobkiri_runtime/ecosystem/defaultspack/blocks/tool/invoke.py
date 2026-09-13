"""Compatibility route that cannot bypass the Capability Plan authority."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from _common import error
from blocks.capability.api import run as run_capability_api


def run(input_data, context):
    """Execute only an already-approved exact invocation."""

    payload = input_data if isinstance(input_data, dict) else {}
    plan_id = str(payload.get("plan_id") or "").strip()
    tool_name = str(payload.get("tool_name") or "").strip()
    arguments = payload.get("arguments")
    if not plan_id:
        return error(
            "Direct Tool invocation is retired; resolve and approve a Capability Plan first",
            "CAPABILITY_PLAN_REQUIRED",
        )
    if not tool_name:
        return error("tool_name is required", "MISSING_PARAM")
    if not isinstance(arguments, dict):
        return error("arguments must be an object", "MISSING_PARAM")
    invocation = {
        "tool_id": tool_name,
        "arguments": arguments,
    }
    return run_capability_api(
        {
            "action": "execute",
            "plan_id": plan_id,
            "invocation": invocation,
            "input": invocation,
        },
        context if isinstance(context, dict) else {},
    )
