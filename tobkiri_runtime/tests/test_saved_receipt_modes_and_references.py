"""Saved append evidence binds both immutable references and approval preference."""

import importlib.util
from pathlib import Path
import pytest
from tobkiri_protocol.canonical import canonical_digest

PATH = (
    Path(__file__).resolve().parents[1]
    / "ecosystem/rumi_conversation_store_pack/runtime/saved_receipt.py"
)
SPEC = importlib.util.spec_from_file_location("dual_saved_receipt_test", PATH)
receipt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipt)


def data(mode):
    row = {
        "kind": "chat",
        "id": "reference",
        "label": "Reference",
        "conversation_ids": ["reference"],
        "snapshot_digest": "sha256:" + "a" * 64,
        "member_count": 1,
        "membership_complete": True,
    }
    snapshot = {
        "kind": "tobkiri.chat.reference.snapshot.v1",
        "profile_id": "profile",
        "store_revision": 1,
        "project_revision": 1,
        "references": [row],
        "snapshot_time": 1,
        "expires_at": 600001,
        "next_cursor": None,
        "truncated": False,
    }
    initial = {
        "request": {
            "turn_id": "turn",
            "conversation_id": "source",
            "conversation_revision": 1,
            "content": "hello",
            "action_approval_mode": mode,
            "chat_references": [
                {"kind": "chat", "profile_id": "profile", "id": "reference"}
            ],
            "resolved_chat_references": snapshot,
        }
    }
    metadata = {
        "turn_id": "turn",
        "action_approval_mode": mode,
        "chat_references": [row],
    }

    def message(role, content, parent=None):
        return {
            "id": "message:"
            + canonical_digest(["source", "turn", role]).removeprefix("sha256:"),
            "role": role,
            "content": content,
            "status": "complete",
            "metadata": dict(metadata),
            "parent_id": parent,
        }

    user = message("user", "hello")
    assistant = message("assistant", "hi", user["id"])
    return initial, user, assistant


@pytest.mark.parametrize("mode", ["ask", "agent", "full"])
def test_both_preferences_and_reference_evidence_bind_user_and_assistant(mode):
    initial, user, assistant = data(mode)
    first = receipt.append_receipt(None, initial, "source", 1, user)
    final = receipt.append_receipt(first, initial, "source", 2, assistant)
    assert final["input_digest"] == canonical_digest(initial)
    assert final["assistant_message_id"] == assistant["id"]


@pytest.mark.parametrize("mode", ["ask", "agent", "full"])
@pytest.mark.parametrize("missing", ["action_approval_mode", "chat_references"])
def test_omitting_either_bound_metadata_field_rejects_append(mode, missing):
    initial, user, assistant = data(mode)
    user["metadata"].pop(missing)
    with pytest.raises(ValueError, match="identity"):
        receipt.append_receipt(None, initial, "source", 1, user)
