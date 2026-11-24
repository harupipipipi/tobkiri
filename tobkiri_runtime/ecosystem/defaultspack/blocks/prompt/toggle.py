"""Compatibility projection for pack-owned prompt composition edge state."""

from __future__ import annotations

from blocks._common import error, ok
from domain.prompt.usage import toggle_prompt_edge


def run(input_data: dict, context: dict) -> dict:
    """Validate the runtime graph, then persist through the global owner."""
    if context.get("_tool_server_approved") is not True:
        return error("Prompt edge mutation requires approval", "PROMPT_STUDIO_DENIED")
    try:
        return ok(toggle_prompt_edge(input_data, preview=False))
    except Exception as exc:
        return error(str(exc), "PROMPT_TOGGLE_FAILED")
