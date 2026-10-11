"""Real captured tool owners publish lifecycle progress under the default budget.

Only the pre-existing canonical user/turn and journal reservation are seeded.
No invocation scope, principal, lease, effect result, or progress event is mocked.
"""

import json
import time
from pathlib import Path

import pytest

from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from ecosystem.rumi_tool_broker_pack.runtime import broker
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.progress import TurnProgressJournal
from ecosystem.rumi_workspace_mount_pack.runtime.mounts import WorkspaceMountStore
from tests.conformance_support.host_profile import captured_host_profile
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.turn_progress_v1 import RESOURCE, RESOURCE_OPERATION, payload_digest


@pytest.mark.parametrize(
    "tool_id,arguments,expected",
    [
        ("calculator", {"expression": "2+2"}, "4"),
        ("coding_file_read", {"path": "hello.txt"}, "owned file content"),
    ],
)
def test_real_captured_tool_progress_and_result_use_original_default_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_id: str,
    arguments: dict,
    expected: str,
) -> None:
    edge = {
        "caller_function_id": "shell.tauri.default",
        "target_provider_id": broker.HOST_PROVIDER_FACTORY.function_id,
        "contract_id": broker.CONTRACT,
        "operation_id": broker.OPERATION,
        "authority_mode": "profile_grant",
        "requested_scope_template": {
            "capability": "operation.invoke",
            "dimensions": {"contract": [broker.CONTRACT], "operation": [broker.OPERATION]},
            "quotas": {},
            "exact_request_digest": None,
            "opaque": False,
        },
    }
    with captured_host_profile(tmp_path, monkeypatch, packs=(), edges=[edge], backends=()) as (
        session,
        _,
    ):
        user_data = tmp_path / "user-data"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "hello.txt").write_text("owned file content", encoding="utf-8")
        mounts = WorkspaceMountStore("defaults", user_data_root=user_data)
        mounts.mount("owned", str(workspace), expected_revision=0)
        mounts.select("owned", expected_revision=1)
        conversations = ConversationStore("defaults", user_data_root=user_data)
        conversation = conversations.create({"id": "progress-conversation"}, expected_revision=0)[
            "conversation"
        ]
        initial = {
            "request": {
                "turn_id": "progress-turn",
                "conversation_id": conversation["id"],
                "conversation_revision": conversation["conversation_revision"],
                "content": "owned fixture",
                "tool_selection": {"mode": "none"},
            }
        }
        turns = DurableTurnRuntime("defaults", user_data_root=user_data)
        turn = turns.claim_saved(initial)["turn"]
        user_id = "message:" + canonical_digest(
            [conversation["id"], turn["id"], "user"]
        ).removeprefix("sha256:")
        conversations.append_message(
            conversation["id"],
            {
                "id": user_id,
                "role": "user",
                "content": "owned fixture",
                "parent_id": None,
            },
            expected_conversation_revision=conversation["conversation_revision"],
        )
        conversation = conversations.get(conversation["id"])
        panel_session = "captured-progress-owner"
        context = session.context_for(broker.CONTRACT, broker.OPERATION, panel_session)
        owner = canonical_digest(
            {"principal": context.caller_principal.value, "session": context.caller_session_id}
        )
        capture = canonical_digest(
            {
                "profile_id": context.profile_id,
                "plan_digest": context.plan_digest,
                "security_epoch": context.security_epoch,
            }
        )
        payload = {"tool_id": tool_id, "tool_call_id": "captured-call", "arguments": arguments}
        journal = TurnProgressJournal(turns.path.with_name("progress.sqlite3"))
        identity = journal.begin(
            {
                "turn_id": turn["id"],
                "conversation_id": conversation["id"],
                "conversation_revision": conversation["conversation_revision"],
                "parent_id": user_id,
                "input_digest": turn["input_digest"],
                "request_id": turn["request_id"],
                "ai_input_digest": payload_digest(payload),
            },
            owner=owner,
            capture=capture,
        )
        started = time.monotonic()
        result = session.invoke(
            broker.CONTRACT,
            broker.OPERATION,
            {
                **payload,
                "progress_id": identity,
                "_session_id": panel_session,
            },
        )
        elapsed = time.monotonic() - started
        assert result["status"] == "success" and result["is_error"] is False
        assert expected in json.dumps(result, ensure_ascii=False)
        page = session.invoke(
            RESOURCE,
            RESOURCE_OPERATION,
            {
                "turn_id": turn["id"],
                "conversation_id": conversation["id"],
                "cursor": 0,
                "progress_id": identity,
                "_session_id": panel_session,
            },
        )
        assert page["progress_id"] == identity
        assert page["canonical_turn_status"] == "running"
        assert page["provider_complete"] is True
        assert page["events"][0] == {"cursor": 1, "event": {"type": "tool_started", **payload}}
        completed = page["events"][1]
        assert completed["cursor"] == 2
        assert completed["event"] == {
            "type": "tool_completed",
            "tool_id": tool_id,
            "tool_call_id": "captured-call",
            "status": result["status"],
            "content": json.dumps(
                {key: result.get(key) for key in ("status", "result", "error")},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        }
        assert page["binding"]["input_digest"] == turn["input_digest"]
        assert page["binding"]["request_id"] == turn["request_id"]
        assert len(page["events"]) == 2
        assert elapsed < 60, "actual progress-enabled effect exceeded original default budget"
