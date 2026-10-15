"""Owned saved content projects to Timeline without rewriting canonical logs."""

from copy import deepcopy
import json
from typing import Any

import pytest

from core_runtime.file_edit_receipts import committed_file_edit_receipt
from ecosystem.defaultspack.defaultspack.file_edit_presentation import (
    present_message_file_edits,
)
from ecosystem.defaultspack.defaultspack.conversation_record_presentation import (
    present_conversation_record,
)
from core_runtime.bootstrap.saved_bridge import project_saved_tool_result
from ecosystem.rumi_tool_result_pack.runtime.normalizer import (
    create_normalize_operation,
)
from tobkiri_protocol.saved_tools import saved_tool_logs
from tobkiri_protocol.turn_progress_v1 import validate_event


def owned_message() -> tuple[dict[str, Any], dict[str, Any]]:
    """Use actual finite normalizer and saved/progress content wire formats."""
    receipt = committed_file_edit_receipt(
        mutation_id="actual-owner",
        operation="create",
        profile_id="profile",
        workspace_id="workspace",
        root_identity=("/not-exposed", 1, 2),
        path="demo.txt",
        before=None,
        after=b"first\nsecond\n",
    )
    normalized = create_normalize_operation(None)(
        "normalize",
        {
            "tool_id": "coding_file_create",
            "tool_call_id": "call-1",
            "executor_provider_instance_id": "native-owner",
            "executor_content_hash": "sha256:" + "a" * 64,
            "value": {
                "result": json.dumps(
                    {
                        "created": True,
                        "workspace_id": "workspace",
                        "path": "demo.txt",
                        "file_edit_receipt": receipt,
                    }
                ),
                "is_error": False,
                "widget": None,
            },
        },
    )
    projected = project_saved_tool_result(normalized)
    validate_event({"type": "tool_completed", "status": "success", **projected})
    trace = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "coding_file_create",
                        "arguments": json.dumps({"path": "demo.txt"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": projected["content"]},
    ]
    return {
        "id": "message-1",
        "role": "assistant",
        "status": "complete",
        "content": "Created demo.txt",
        "metadata": {
            "turn_id": "turn-1",
            "saved_tool_messages": trace,
        },
        "tool_logs": saved_tool_logs(trace),
    }, receipt


def test_finite_owner_progress_saved_wire_and_public_projection_agree() -> None:
    message, receipt = owned_message()
    original = deepcopy(message)
    shown = present_message_file_edits(message, profile_id="profile")
    assert shown["tool_logs"][0]["file_edit_receipt"] == receipt
    assert message == original
    assert "file_edit_receipt" not in message["tool_logs"][0]
    assert saved_tool_logs(message["metadata"]["saved_tool_messages"]) == message["tool_logs"]
    record = present_conversation_record(
        {
            "conversation": {
                "id": "conversation-1",
                "messages": [message],
            }
        },
        profile_id="profile",
    )
    assert record["messages"][0]["tool_logs"][0]["file_edit_receipt"] == receipt
    assert record["messages"][0]["conversation_id"] == "conversation-1"


@pytest.mark.parametrize("profile", [None, "foreign"])
def test_captured_profile_is_required_and_cannot_be_rebound(
    profile: str | None,
) -> None:
    message, _ = owned_message()
    shown = present_message_file_edits(message, profile_id=profile)
    assert "file_edit_receipt" not in shown["tool_logs"][0]


@pytest.mark.parametrize("change", ["user", "failed", "missing_trace", "changed_log", "forged_top"])
def test_nonowner_or_unfinished_log_never_becomes_confirmed(change: str) -> None:
    message, receipt = owned_message()
    if change == "user":
        message["role"] = "user"
    elif change == "failed":
        message["status"] = "failed"
    elif change == "missing_trace":
        message["metadata"].pop("saved_tool_messages")
    elif change == "changed_log":
        message["tool_logs"][0]["result"] = "file changed 100 lines"
    else:
        message["tool_logs"][0]["file_edit_receipt"] = receipt
    shown = present_message_file_edits(message, profile_id="profile")
    assert "file_edit_receipt" not in shown["tool_logs"][0]


def test_read_list_failed_preview_and_unknown_contract_are_not_edits() -> None:
    message, _ = owned_message()
    for name in ("list_modules", "coding_file_read", "coding_file_patch", "unknown"):
        changed = deepcopy(message)
        changed["metadata"]["saved_tool_messages"][0]["tool_calls"][0]["function"]["name"] = name
        changed["tool_logs"] = saved_tool_logs(changed["metadata"]["saved_tool_messages"])
        assert (
            "file_edit_receipt"
            not in present_message_file_edits(changed, profile_id="profile")["tool_logs"][0]
        )
