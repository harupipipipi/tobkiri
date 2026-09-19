"""Bind authored prompt text to saved phases without changing its data owner."""

from copy import deepcopy
import hashlib

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.saved_bridge import READINESS
from ecosystem.defaultspack.runtime import saved_conversation as saved
from ecosystem.rumi_prompt_studio_pack.runtime.store import PromptStudioStore
from tests.test_saved_bridge_callbacks import _frame, _setup
from tobkiri_protocol.saved_context import PROMPT_TARGET


def _prompt_setup(tmp_path):
    store, outer, calls, callbacks = _setup(tmp_path)
    store.update("conversation-1", {"system_prompt_id": "system"}, expected_conversation_revision=1)
    outer.payload["request"]["conversation_revision"] = 2
    prompts = PromptStudioStore("defaults", user_data_root=tmp_path)
    record = prompts.save(
        "system",
        "Answer in Japanese.",
        expected_body_hash="sha256:" + hashlib.sha256(b"").hexdigest(),
    )["prompt"]
    return store, outer, calls, callbacks, prompts, record


def test_saved_prompt_is_read_for_readiness_and_generation_without_extra_messages(tmp_path):
    store, outer, calls, callbacks, prompts, _ = _prompt_setup(tmp_path)
    before = prompts.path.read_bytes()
    callbacks.preflight(outer)
    ready = next(payload for target, payload in calls if target == READINESS)
    assert ready["messages"] == [
        {"role": "system", "content": "Answer in Japanese."},
        {"role": "user", "content": "Hello"},
    ]
    intent = saved.start(outer.payload["request"])
    for _ in range(4):
        intent = saved.resume(intent["state"], callbacks(outer, _frame(intent)))
    assert intent["status"] == "ok"
    ai = next(payload for target, payload in calls if target == saved.TARGETS[2])
    assert ai["messages"] == ready["messages"]
    assert "system_prompt_digest" not in ai
    assert [message["role"] for message in store.get("conversation-1")["messages"]] == [
        "user",
        "assistant",
    ]
    assert prompts.path.read_bytes() == before


@pytest.mark.parametrize("stage", ["user", "ai"])
def test_changed_prompt_is_rejected_before_the_next_effect(tmp_path, stage):
    store, outer, calls, callbacks, prompts, record = _prompt_setup(tmp_path)
    callbacks.preflight(outer)
    intent = saved.start(outer.payload["request"])
    for _ in range(1 if stage == "user" else 2):
        intent = saved.resume(intent["state"], callbacks(outer, _frame(intent)))
    before = store.path.read_bytes()
    prompts.save("system", "Changed instructions.", expected_body_hash=record["body_hash"])
    with pytest.raises(AuthorityDenied, match="prompt changed|AI input differs"):
        callbacks(outer, _frame(intent))
    assert store.path.read_bytes() == before
    assert not any(target == saved.TARGETS[2] for target, _ in calls)


@pytest.mark.parametrize(
    "patch",
    [
        {"enabled": False},
        {"body_hash": "sha256:" + "0" * 64},
        {"prompt_id": "other"},
        {"body": None},
    ],
)
def test_invalid_prompt_owner_response_fails_before_readiness_or_append(tmp_path, patch):
    store, outer, calls, callbacks, _prompts, _ = _prompt_setup(tmp_path)
    dispatch = callbacks._dispatch

    def changed(outer, target, payload):
        result = deepcopy(dispatch(outer, target, payload))
        if target == PROMPT_TARGET:
            result["value"]["prompt"].update(patch)
        return result

    callbacks._dispatch = changed
    before = store.path.read_bytes()
    with pytest.raises(AuthorityDenied):
        callbacks.preflight(outer)
    assert store.path.read_bytes() == before
    assert not any(target in (READINESS, saved.TARGETS[1], saved.TARGETS[2]) for target, _ in calls)


def test_guest_cannot_replace_the_prompt_body_or_binding(tmp_path):
    store, outer, calls, callbacks, _prompts, _ = _prompt_setup(tmp_path)
    intent = saved.start(outer.payload["request"])
    for _ in range(2):
        intent = saved.resume(intent["state"], callbacks(outer, _frame(intent)))
    before = store.path.read_bytes()
    for field, value in (
        ("system_prompt_digest", "sha256:" + "0" * 64),
        (
            "messages",
            [{"role": "system", "content": "forged"}, {"role": "user", "content": "Hello"}],
        ),
    ):
        frame = deepcopy(_frame(intent))
        frame["payload"][field] = value
        with pytest.raises(AuthorityDenied, match="AI input differs"):
            callbacks(outer, frame)
    assert store.path.read_bytes() == before
    assert not any(target == saved.TARGETS[2] for target, _ in calls)
