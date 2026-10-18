"""Project explicit edit receipts from an owned saved transcript for display."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.file_edit_receipts import file_edit_receipt_from_tool_result
from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.saved_tools import saved_tool_logs, saved_tool_messages


def present_message_file_edits(
    message: Mapping[str, Any], *, profile_id: str | None
) -> dict[str, Any]:
    """Add display-only sidecars without changing canonical append evidence.

    Only the exact owner transcript/log projection is admitted. No metadata is
    inferred from tool arguments, read artifacts, generic previews or text.
    """
    shown = dict(message)
    logs = message.get("tool_logs")
    if not isinstance(logs, list):
        return shown
    public_logs = [
        {key: value for key, value in log.items() if key != "file_edit_receipt"}
        if isinstance(log, Mapping)
        else log
        for log in logs
    ]
    shown["tool_logs"] = public_logs
    metadata = message.get("metadata")
    if (
        not profile_id
        or message.get("role") != "assistant"
        or message.get("status") != "complete"
        or not isinstance(metadata, Mapping)
        or not isinstance(metadata.get("turn_id"), str)
    ):
        return shown
    try:
        trace = saved_tool_messages(metadata.get("saved_tool_messages", []))
        expected = saved_tool_logs(trace)
        if not trace or canonical_json(logs) != canonical_json(expected):
            return shown
    except (ValueError, TypeError):
        return shown
    for log in public_logs:
        if not isinstance(log, dict):
            continue
        receipt = file_edit_receipt_from_tool_result(
            str(log.get("tool_name") or ""), log.get("result")
        )
        if receipt is not None and receipt["profile_id"] == profile_id:
            log["file_edit_receipt"] = receipt
    return shown
