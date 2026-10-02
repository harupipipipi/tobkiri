"""Read-only compatibility preview for prompt composition edge state."""

from __future__ import annotations

from blocks._common import error, ok
from domain.prompt.usage import toggle_prompt_edge


def run(input_data: dict, context: dict) -> dict:
    """Preview an edge change without writing either legacy or owner state."""
    del context
    try:
        return ok(toggle_prompt_edge(input_data, preview=True))
    except Exception as exc:
        return error(str(exc), "PROMPT_TOGGLE_PREVIEW_FAILED")
