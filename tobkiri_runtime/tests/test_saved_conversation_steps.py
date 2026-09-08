"""Real owner writes with isolated saved-turn computation, not native acceptance."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from ecosystem.defaultspack.runtime import saved_conversation as saved
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from tobkiri_host.continuation_chain import ChainIdentity, ContinuationChains
from tobkiri_host.continuation_envelope import (
    validate_continuation_request,
    validate_continuation_result,
)
from tobkiri_protocol.canonical import canonical_json


def _setup(tmp_path: Path) -> tuple[ConversationStore, dict[str, Any]]:
    store = ConversationStore("defaults", user_data_root=tmp_path)
    store.create(
        {"id": "conversation-1", "model_reference": "model-profile-1"}, expected_revision=0
    )
    return store, {
        "turn_id": "turn-1",
        "conversation_id": "conversation-1",
        "conversation_revision": 1,
        "content": "Hello",
    }


def _owner(store: ConversationStore, intent: dict[str, Any]) -> dict[str, Any]:
    payload = intent["payload"]
    if intent["hop"] == 0:
        value = {"conversation": store.get(payload["conversation_id"])}
    else:
        value = store.append_message(
            payload["conversation_id"],
            payload["message"],
            expected_conversation_revision=payload["expected_conversation_revision"],
        )
    return {"status": "ok", "value": value}


def test_four_steps_preserve_owner_revisions_and_validate_v2_frames(tmp_path: Path) -> None:
    store, request = _setup(tmp_path)
    identity = ChainIdentity("domain", "host-request", "sha256:" + "a" * 64, 60.0)
    chains = ContinuationChains(clock=lambda: 1.0)
    intent = saved.start(request)
    previous = None
    permit = None
    calls = []
    for hop in range(4):
        assert intent["hop"] == hop
        assert tuple(intent["target"].values()) == saved.TARGETS[hop]
        assert "profile_id" not in intent["payload"]
        frame = {
            "kind": "tobkiri.packvm.continuation.request.v2",
            "version": 2,
            "request_id": identity.request_id,
            "binding_digest": identity.binding_digest,
            "hop": hop,
            "nonce": str(hop) * 48,
            "previous_digest": previous,
            "target": intent["target"],
            "payload": intent["payload"],
            "state": intent["state"],
        }
        checked = validate_continuation_request(
            canonical_json(frame),
            identity=identity,
            hop=hop,
            previous_digest=previous,
            target=saved.TARGETS[hop],
        )
        if permit is None:
            chains.start(identity, frame=checked.frame, nonce=checked.nonce)
        else:
            chains.advance(permit, frame=checked.frame, nonce=checked.nonce)
        calls.append(deepcopy(intent["payload"]))
        if hop == 2:
            assert intent["payload"]["messages"] == [{"role": "user", "content": "Hello"}]
            assert intent["payload"]["model_reference"] == "model-profile-1"
            outcome = {"status": "ok", "value": {"status": "ok", "output": "Hi"}}
        else:
            outcome = _owner(store, intent)
        result = {
            "kind": "tobkiri.packvm.continuation.result.v2",
            "version": 2,
            "request_digest": checked.digest,
            "outcome": outcome,
        }
        verified = validate_continuation_result(canonical_json(result), request=checked)
        permit = chains.take(identity, nonce=checked.nonce, result=verified.frame)
        previous = verified.digest
        intent = saved.resume(intent["state"], outcome)
    chains.finish(permit)
    assert intent["status"] == "ok"
    assert intent["conversation_revision"] == 3
    assert [call.get("expected_conversation_revision") for call in calls] == [None, 1, None, 2]
    messages = store.get("conversation-1")["messages"]
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Hello"),
        ("assistant", "Hi"),
    ]
    assert messages[1]["parent_id"] == messages[0]["id"]
    assert intent["message"] == messages[1]
    with pytest.raises(ValueError):
        chains.start(identity, frame=checked.frame, nonce=checked.nonce)


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
def test_failed_or_lost_result_never_emits_a_retry(tmp_path: Path, stage: int) -> None:
    store, request = _setup(tmp_path)
    intent = saved.start(request)
    for hop in range(stage):
        outcome = (
            {"status": "ok", "value": {"status": "ok", "output": "Hi"}}
            if hop == 2
            else _owner(store, intent)
        )
        intent = saved.resume(intent["state"], outcome)
    # A write may have committed even though its response never arrived.
    if stage in (1, 3):
        _owner(store, intent)
    result = saved.resume(
        intent["state"], {"status": "error", "error": {"code": "TIMEOUT", "message": "unknown"}}
    )
    assert result["status"] == "error"
    assert "target" not in result
    assert result["reconciliation_required"] == (stage in (1, 3))
    assert result["user_persistence"] == ["not_written", "unknown", "saved", "saved"][stage]
    assert result["assistant_persistence"] == ("unknown" if stage == 3 else "not_written")
    assert len(store.get("conversation-1")["messages"]) == [0, 1, 1, 2][stage]


def test_stale_read_and_duplicate_turn_never_write_or_call_ai(tmp_path: Path) -> None:
    store, request = _setup(tmp_path)
    first = saved.start(request)
    append = saved.resume(first["state"], _owner(store, first))
    _owner(store, append)
    stale_request = saved.start({**request, "turn_id": "new-turn"})
    stale = saved.resume(stale_request["state"], _owner(store, stale_request))
    assert stale["error"]["code"] == "CONVERSATION_REVISION_CONFLICT"
    replay = saved.start({**request, "conversation_revision": 2})
    result = saved.resume(replay["state"], _owner(store, replay))
    assert result["error"]["code"] == "TURN_RECONCILIATION_REQUIRED"
    assert result["reconciliation_required"] is True
    assert result["user_persistence"] == "unknown"
    assert result["assistant_persistence"] == "unknown"
    assert result["user_message_id"] == append["payload"]["message"]["id"]
    assert len(store.get("conversation-1")["messages"]) == 1


@pytest.mark.parametrize("change", ["id", "content", "parent_id", "revision", "action", "shape"])
def test_wrong_write_acknowledgement_stops_with_uncertain_persistence(
    tmp_path: Path, change: str
) -> None:
    store, request = _setup(tmp_path)
    first = saved.start(request)
    append = saved.resume(first["state"], _owner(store, first))
    outcome = _owner(store, append)
    if change == "revision":
        outcome["value"]["conversation_revision"] = 1
    elif change == "action":
        outcome["value"]["action"] = "created"
    elif change == "shape":
        outcome["value"]["message"] = []
    else:
        outcome["value"]["message"][change] = "wrong"
    result = saved.resume(append["state"], outcome)
    assert result["status"] == "error"
    assert result["user_persistence"] == "unknown"
    assert result["reconciliation_required"] is True


def test_branch_context_uses_only_selected_ancestry(tmp_path: Path) -> None:
    store, request = _setup(tmp_path)
    for message in (
        {"id": "root", "role": "user", "content": "root"},
        {"id": "off-branch", "role": "assistant", "content": "excluded", "parent_id": "root"},
        {"id": "selected", "role": "assistant", "content": "selected", "parent_id": "root"},
    ):
        store.append_message(
            "conversation-1",
            message,
            expected_conversation_revision=store.get("conversation-1")["conversation_revision"],
        )
    first = saved.start({**request, "conversation_revision": 4})
    append = saved.resume(first["state"], _owner(store, first))
    assert append["payload"]["message"]["parent_id"] == "selected"
    ai = saved.resume(append["state"], _owner(store, append))
    assert [item["content"] for item in ai["payload"]["messages"]] == ["root", "selected", "Hello"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_id", "other"),
        ("root", "/tmp"),
        ("approved", True),
        ("state", {}),
        ("model_reference", "other"),
    ],
)
def test_start_rejects_caller_authority_and_context_injection(
    tmp_path: Path, field: str, value: Any
) -> None:
    _, request = _setup(tmp_path)
    with pytest.raises(ValueError):
        saved.start({**request, field: value})


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"model_reference": ""}, "MODEL_REFERENCE_REQUIRED"),
        ({"agent_id": "agent"}, "CONTEXT_RESOLUTION_REQUIRED"),
        ({"system_prompt_id": "prompt"}, "CONTEXT_RESOLUTION_REQUIRED"),
    ],
)
def test_missing_context_fails_before_writing(
    tmp_path: Path, patch: dict[str, Any], code: str
) -> None:
    store, request = _setup(tmp_path)
    updated = store.update("conversation-1", patch, expected_conversation_revision=1)
    first = saved.start(
        {**request, "conversation_revision": updated["conversation"]["conversation_revision"]}
    )
    result = saved.resume(first["state"], _owner(store, first))
    assert result["error"]["code"] == code
    assert result["user_persistence"] == "not_written"
    assert store.get("conversation-1")["messages"] == []


def test_saved_step_imports_and_runs_with_isolated_stdlib_only(tmp_path: Path) -> None:
    _, request = _setup(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            "import json,runpy,sys; m=runpy.run_path(sys.argv[1]); print(json.dumps(m['tobkiri_packvm_invoke']('saved_complete', {'request':json.loads(sys.argv[2])})))",
            str(Path(saved.__file__).resolve()),
            json.dumps(request),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert json.loads(completed.stdout) == saved.start(request)


def test_oversize_ai_context_is_rejected_before_user_write(tmp_path: Path) -> None:
    store, request = _setup(tmp_path)
    store.append_message(
        "conversation-1",
        {"id": "old", "role": "user", "content": "x" * 33000},
        expected_conversation_revision=1,
    )
    first = saved.start({**request, "conversation_revision": 2})
    result = saved.resume(first["state"], _owner(store, first))
    assert result["status"] == "error"
    assert result["user_persistence"] == "not_written"
    assert len(store.get("conversation-1")["messages"]) == 1


@pytest.mark.parametrize(
    "value",
    [
        {"status": "error", "output": "not a success"},
        {"status": "ok", "output": "", "tool_intents": []},
        {"status": "ok", "output": "x" * 80000},
        {"status": "ok", "output": "tool request", "tool_intents": [{"operation": "execute"}]},
    ],
)
def test_invalid_or_tool_ai_result_preserves_user_without_assistant_write(
    tmp_path: Path, value: dict[str, Any]
) -> None:
    store, request = _setup(tmp_path)
    first = saved.start(request)
    append = saved.resume(first["state"], _owner(store, first))
    ai = saved.resume(append["state"], _owner(store, append))
    result = saved.resume(ai["state"], {"status": "ok", "value": value})
    assert result["status"] == "error"
    assert result["user_persistence"] == "saved"
    assert result["assistant_persistence"] == "not_written"
    assert "target" not in result
    assert len(store.get("conversation-1")["messages"]) == 1


def test_next_write_uses_owner_returned_revision_not_local_increment(tmp_path: Path) -> None:
    store, request = _setup(tmp_path)
    first = saved.start(request)
    append = saved.resume(first["state"], _owner(store, first))
    result = _owner(store, append)
    result["value"]["conversation_revision"] = 17
    ai = saved.resume(append["state"], result)
    assistant = saved.resume(
        ai["state"], {"status": "ok", "value": {"status": "ok", "output": "Hi"}}
    )
    assert assistant["payload"]["expected_conversation_revision"] == 17
