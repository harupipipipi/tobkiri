"""Regression tests for the coding approval-followup deterministic replay.

When the UI delivers an ``approval_followup`` whose token + tool_name +
request_id resolve to an approved pending tool, the chat engine must replay
that exact pending tool **once** with the stored approved arguments before the
model speaks. This removes the previous reliance on the model deciding to
re-issue the tool call from natural-language hints, which produced the
``executed_tools=[]`` hallucinated commit-success bug where the model
described a successful git commit while the underlying ``git log -1`` still
pointed at the previous commit.

These tests pin the deterministic-replay contract:

* The ``approval_required`` helper must store the original arguments inside
  the approval request so the followup path can replay them later.
* When the engine receives a valid approval-followup it must call the tool
  once with the stored arguments and the approved token, persist the synthetic
  assistant tool_use + tool_result on the active draft, strip provider tools
  for the remainder of the turn, and surface the executed tool name in the
  finalised assistant ``metadata.executed_tools``.
* When the followup is missing/expired/tampered the engine falls through to
  the existing model-driven path so non-followup turns never regress.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures(
    "defaultspack_conversation_owner", "defaultspack_v4_tool_dispatch"
)


class _NoToolFakeClient:
    """Fake AI client that returns a text-only response without calling tools."""

    def __init__(self, recorded):
        self._recorded = recorded

    def complete(self, model, messages, tools=None, params=None):
        self._recorded.setdefault("complete_calls", []).append(
            {
                "model": model,
                "tools": list(tools or []),
                "messages": list(messages or []),
            }
        )
        return {
            "content": [{"type": "text", "text": "Commit summary: hash=abc1234."}],
            "finish_reason": "stop",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    def supports_stream(self, model):
        return False

    def stream(self, model, messages, tools=None, params=None):  # pragma: no cover - unused
        if False:
            yield {}


def _coding_git_commit_tool_def():
    return {
        "tool_id": "coding_git_commit",
        "name": "coding_git_commit",
        "risk": "high",
        "requires_approval": True,
        "capability_grants": ["git.write"],
        "execution": {
            "type": "rumi_function",
            "qualified_name": "defaultspack:coding_git_commit",
        },
    }


def test_approval_required_embeds_args_in_details_for_replay():
    """``approval_required`` must persist the original arguments under
    ``details["arguments"]`` so the followup path can recover them later."""
    from blocks.coding._approval import approval_required
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    args = {"message": "fix typo", "paths": ["a.txt", "b.txt"]}
    payload = approval_required(
        "git.commit",
        "high",
        args=args,
        message=args["message"],
        tool_name="coding_git_commit",
    )
    request = approval.get_approval_request(payload["approval_request_id"])
    assert request is not None
    assert request["details"]["arguments"] == args
    # The args_hash must reflect the stored arguments so replay verification works.
    assert request["args_hash"] == approval.hash_arguments(args)


def test_approval_required_strips_token_and_transport_keys_from_stored_args():
    """``approval_token`` and transport-only keys must not contaminate the
    stored args, otherwise the args_hash baked into the token would never
    match a deterministic replay."""
    from blocks.coding._approval import approval_required
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    args = {
        "message": "fix typo",
        "approval_token": "tok_should_be_stripped",
        "_headers": {"X-Rumi-Approval": "stale"},
    }
    payload = approval_required("git.commit", "high", args=args, message="fix typo")
    request = approval.get_approval_request(payload["approval_request_id"])
    assert request["details"]["arguments"] == {"message": "fix typo"}


def test_coding_approval_infers_replay_tool_and_conversation():
    from blocks.coding._approval import approval_required
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    payload = approval_required(
        "file.write",
        "high",
        args={
            "path": "debug-e2e.txt",
            "content": "approved",
            "conversation_id": "debug-e2e-conversation",
        },
    )
    request = approval.get_approval_request(payload["approval_request_id"])
    assert request["details"]["tool_name"] == "coding_file_write"
    assert request["details"]["function_id"] == "coding_file_write"
    assert request["details"]["conversation_id"] == "debug-e2e-conversation"


def test_executor_approval_required_tool_response_embeds_replayable_args():
    """Generic executor approval requests must also persist replayable args.

    Without this, chat approval-followup falls through to the model path for
    mutation tools like ``coding_file_patch``, which then drifts and burns the
    approval token on mismatched arguments.
    """
    from domain.safety import approval
    from domain.tool.executor import _approval_required_tool_response

    approval.reset_approval_state_for_tests()
    tool_def = {
        "name": "coding_file_patch",
        "risk": "high",
        "requires_approval": True,
    }
    args = {
        "path": "executor.py",
        "old": "before",
        "new": "after",
        "approval_token": "strip-me",
        "_headers": {"X-Rumi-Approval": "stale"},
    }
    response = _approval_required_tool_response(tool_def, args)
    request_id = response["widget"]["approval_request_id"]
    request = approval.get_approval_request(request_id)
    assert request is not None
    assert request["details"]["tool_name"] == "coding_file_patch"
    assert request["details"]["arguments"] == {
        "path": "executor.py",
        "old": "before",
        "new": "after",
    }
    assert request["args_hash"] == approval.hash_arguments(request["details"]["arguments"])


def test_browser_computer_approval_persists_replayable_screenshot_args():
    """Display arguments can differ from the approval hash arguments, but the
    stored followup arguments must stay replayable or deterministic approval
    replay falls through to another model turn and can re-request open_url."""
    from domain.safety import approval
    from domain.tool.executor import _approval_required_tool_response

    approval.reset_approval_state_for_tests()
    tool_def = {
        "name": "browser_computer",
        "risk": "high",
        "requires_approval": True,
    }
    original_args = {
        "action": "computer.screenshot",
        "payload": {"app": "ChatGPT Atlas"},
    }

    response = _approval_required_tool_response(
        tool_def,
        original_args,
        {"conversation_id": "conv_browser_screenshot"},
    )

    request = approval.get_approval_request(response["widget"]["approval_request_id"])
    assert request is not None
    assert request["operation"] == "computer.screenshot"
    assert request["details"]["tool_name"] == "browser_computer"
    assert request["details"]["arguments"] == {
        "action": "computer.screenshot",
        "payload": {"app": "ChatGPT Atlas"},
    }
    assert request["args_hash"] == approval.hash_arguments(request["details"]["arguments"])


def _make_conversation_with_followup(tmp_path, monkeypatch, *, args, token, request_id, tool_name):
    """Build a chat conversation whose latest user message carries a valid
    approval-followup metadata block targeting ``tool_name``."""
    from domain.chat.store import ChatStore

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    return store, conversation


def _approve_pending(args, *, tool_name, operation):
    """Drive ``approval_required`` + ``approve`` to obtain a real signed token
    that targets ``operation`` for ``args``."""
    from blocks.coding._approval import approval_required
    from domain.safety import approval

    payload = approval_required(
        operation,
        "high",
        args=args,
        message=str(args.get("message") or ""),
        tool_name=tool_name,
    )
    request_id = payload["approval_request_id"]
    decision = approval.approve(request_id)
    assert decision["approved"] is True, decision
    return decision["token"], request_id


def test_approval_followup_replays_browser_computer_action_operation(monkeypatch):
    """Browser/computer approvals use action operations like
    ``browser.open_url``. They still need deterministic replay so the model does
    not have to rediscover and reissue the pending action after approval."""
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    payload = {
        "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "profile_id": "default",
        "persistent": False,
        "target_app": "",
    }
    stored_args = {"action": "browser.open_url", "payload": payload}
    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        stored_args,
        details={
            "tool_name": "computer_use",
            "action": "browser.open_url",
            "function_id": "browser.open_url",
            "arguments": stored_args,
            "pack_id": "defaultspack",
            "conversation_id": "conv_browser",
        },
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    prepared = PreparedChatRun(
        conversation_id="conv_browser",
        conversation={},
        input_data={},
        request_id="run_browser",
        content=[],
        metadata={
            "approval_followup": {
                "approval_token": decision["token"],
                "tool_name": "computer_use",
                "request_id": request["request_id"],
                "operation": "browser.open_url",
                "action": "browser.open_url",
            }
        },
        user_message={},
        model="stub/default",
        params={},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[{"name": "computer_use"}],
        tools_called=[],
        connected_tool_names={"computer_use"},
        call_handler=None,
        model_routing={},
    )
    engine = ChatRunEngine()
    invoked = []

    def fake_execute_tool(prepared_arg, tool_name, tool_call_id, arguments):
        invoked.append(
            {
                "prepared": prepared_arg,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": dict(arguments),
            }
        )
        return {"action": "browser.open_url", "opened": True, "is_error": False}

    monkeypatch.setattr(engine, "_execute_tool", fake_execute_tool)
    working_messages: list[dict] = []
    replay = engine._replay_approval_followup_if_present(
        prepared,
        working_messages,
        prepared.chat_ir,
        None,
    )

    events = []
    try:
        while True:
            events.append(next(replay))
    except StopIteration as stop:
        replay_result = stop.value

    assert replay_result is None
    assert [event["phase"] for event in events] == ["tool_call_started", "tool_call_completed"]
    assert invoked == [
        {
            "prepared": prepared,
            "tool_name": "computer_use",
            "tool_call_id": request["request_id"],
            "arguments": {
                "action": "browser.open_url",
                **payload,
                "approval_token": decision["token"],
            },
        }
    ]
    assert prepared.provider_tools == []
    assert prepared.tool_context["approval_replayed"]["request_id"] == request["request_id"]
    assert "approval_token" not in repr(working_messages)


@pytest.mark.parametrize(
    ("operation", "stored_args", "inline_payload"),
    [
        ("tool.browser_companion", {"action": "session"}, {}),
        (
            "page.click",
            {
                "action": "page.click",
                "client_id": "client-1",
                "element_id": "el-7",
            },
            {"element_id": "tampered-inline-value"},
        ),
    ],
)
def test_browser_companion_followup_replays_authoritative_stored_arguments(
    monkeypatch,
    operation,
    stored_args,
    inline_payload,
):
    """Resume Browser Companion only from its signed server-stored request."""
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    request = approval.create_approval_request(
        operation,
        "high",
        stored_args,
        details={
            "tool_name": "browser_companion",
            "action": operation,
            "function_id": operation,
            "arguments": stored_args,
            "pack_id": "defaultspack",
            "conversation_id": "conv-browser-companion",
        },
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    prepared = PreparedChatRun(
        conversation_id="conv-browser-companion",
        conversation={},
        input_data={},
        request_id="run-browser-companion-followup",
        content=[],
        metadata={
            "approval_followup": {
                "approval_token": decision["token"],
                "tool_name": "browser_companion",
                "request_id": request["request_id"],
                "operation": operation,
                "action": operation,
                "payload": inline_payload,
            }
        },
        user_message={},
        model="stub/default",
        params={},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[{"name": "browser_companion"}],
        tools_called=[],
        connected_tool_names={"browser_companion"},
        call_handler=None,
        model_routing={},
    )
    engine = ChatRunEngine()
    invoked = []

    def fake_execute_tool(prepared_arg, tool_name, tool_call_id, arguments):
        invoked.append(
            {
                "prepared": prepared_arg,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": dict(arguments),
            }
        )
        return {
            "result": "Browser Companion completed the approved operation.",
            "is_error": False,
            "widget": {"action": stored_args["action"]},
        }

    monkeypatch.setattr(engine, "_execute_tool", fake_execute_tool)
    working_messages: list[dict] = []
    replay = engine._replay_approval_followup_if_present(
        prepared,
        working_messages,
        prepared.chat_ir,
        None,
    )

    events = []
    try:
        while True:
            events.append(next(replay))
    except StopIteration as stop:
        replay_result = stop.value

    assert replay_result is None
    assert [event["phase"] for event in events] == [
        "tool_call_started",
        "tool_call_completed",
    ]
    assert invoked == [
        {
            "prepared": prepared,
            "tool_name": "browser_companion",
            "tool_call_id": request["request_id"],
            "arguments": {**stored_args, "approval_token": decision["token"]},
        }
    ]
    assert prepared.provider_tools == []
    assert prepared.tool_context["approval_replayed"]["request_id"] == request["request_id"]
    assert approval.get_approval_request(request["request_id"])["status"] == "consumed"
    assert inline_payload != stored_args
    assert "approval_token" not in repr(working_messages)


def test_approval_followup_replays_browser_computer_nested_payload_token(monkeypatch):
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    payload = {
        "url": "https://www.google.com",
        "profile_id": "default",
        "persistent": False,
        "target_app": "ChatGPT Atlas",
    }
    stored_args = {"action": "browser.open_url", "payload": payload}
    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        stored_args,
        details={
            "tool_name": "browser_computer",
            "action": "browser.open_url",
            "function_id": "browser.open_url",
            "arguments": stored_args,
            "pack_id": "defaultspack",
            "conversation_id": "conv_browser_computer",
        },
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    prepared = PreparedChatRun(
        conversation_id="conv_browser_computer",
        conversation={},
        input_data={},
        request_id="run_browser_computer",
        content=[],
        metadata={
            "approval_followup": {
                "approval_token": decision["token"],
                "tool_name": "browser_computer",
                "request_id": request["request_id"],
                "operation": "browser.open_url",
                "action": "browser.open_url",
            }
        },
        user_message={},
        model="stub/default",
        params={},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[{"name": "browser_computer"}],
        tools_called=[],
        connected_tool_names={"browser_computer"},
        call_handler=None,
        model_routing={},
    )
    engine = ChatRunEngine()
    invoked = []

    def fake_execute_tool(prepared_arg, tool_name, tool_call_id, arguments):
        invoked.append(dict(arguments))
        return {"action": "browser.open_url", "opened": True, "is_error": False}

    monkeypatch.setattr(engine, "_execute_tool", fake_execute_tool)
    working_messages: list[dict] = []
    replay = engine._replay_approval_followup_if_present(
        prepared,
        working_messages,
        prepared.chat_ir,
        None,
    )
    try:
        while True:
            next(replay)
    except StopIteration as stop:
        replay_result = stop.value

    assert replay_result is None
    assert invoked == [
        {
            "action": "browser.open_url",
            "payload": {**payload, "approval_token": decision["token"]},
            "approval_token": decision["token"],
        }
    ]
    assert "approval_token" not in repr(working_messages)


def test_consumed_browser_computer_approval_followup_does_not_fall_through_to_model(monkeypatch):
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    stored_args = {
        "action": "computer.screenshot",
        "payload": {"app": "ChatGPT Atlas"},
    }
    request = approval.create_approval_request(
        "computer.screenshot",
        "high",
        stored_args,
        details={
            "tool_name": "browser_computer",
            "action": "computer.screenshot",
            "function_id": "computer.screenshot",
            "arguments": stored_args,
            "pack_id": "defaultspack",
            "conversation_id": "conv_browser_computer",
        },
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True
    consumed = approval.verify_execution_token(
        decision["token"],
        "computer.screenshot",
        approval.hash_arguments(stored_args),
        consume=True,
        pack_id="defaultspack",
        conversation_id="conv_browser_computer",
    )
    assert consumed.valid is True
    assert approval.get_approval_request(request["request_id"])["status"] == "consumed"

    prepared = PreparedChatRun(
        conversation_id="conv_browser_computer",
        conversation={},
        input_data={},
        request_id="run_browser_computer",
        content=[],
        metadata={
            "approval_followup": {
                "approval_token": decision["token"],
                "tool_name": "browser_computer",
                "request_id": request["request_id"],
                "tool_call_id": "call_screenshot",
                "operation": "computer.screenshot",
                "action": "computer.screenshot",
                "payload": stored_args,
            }
        },
        user_message={
            "metadata": {
                "approval_followup": {
                    "approval_token": decision["token"],
                    "tool_name": "browser_computer",
                    "request_id": request["request_id"],
                    "tool_call_id": "call_screenshot",
                    "operation": "computer.screenshot",
                    "action": "computer.screenshot",
                    "payload": stored_args,
                }
            }
        },
        model="stub/default",
        params={},
        request_context={"user_requested_computer_use": True},
        tool_context={"user_requested_computer_use": True},
        standard_messages=[],
        user_text="",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[{"name": "browser_computer"}],
        tools_called=[],
        connected_tool_names={"browser_computer"},
        call_handler=None,
        model_routing={},
    )
    engine = ChatRunEngine()

    def fail_execute_tool(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("consumed approval followup must not execute the tool again")

    def fail_model_turn(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("consumed approval followup must not fall through to the model")

    monkeypatch.setattr(engine, "_execute_tool", fail_execute_tool)
    monkeypatch.setattr(engine, "_model_turn", fail_model_turn)

    execution = engine._execute(prepared, None)
    events = []
    try:
        while True:
            events.append(next(execution))
    except StopIteration as stop:
        result = stop.value

    assert result["finish_reason"] == "stop"
    assert result["metadata"]["approval_followup"]["status"] == "consumed"
    assert "処理済み" in result["content"][0]["text"]
    assert [event.get("phase") for event in events] == ["approval_followup_consumed"]


def test_approval_followup_keeps_computer_tools_for_user_requested_computer_use(monkeypatch):
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    payload = {
        "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "profile_id": "default",
        "persistent": False,
        "target_app": "Vivaldi",
    }
    stored_args = {"action": "browser.open_url", "payload": payload}
    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        stored_args,
        details={
            "tool_name": "computer_use",
            "action": "browser.open_url",
            "function_id": "browser.open_url",
            "arguments": stored_args,
            "pack_id": "defaultspack",
            "conversation_id": "conv_browser",
        },
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    provider_tools = [{"name": "computer_use"}]
    prepared = PreparedChatRun(
        conversation_id="conv_browser",
        conversation={},
        input_data={},
        request_id="run_browser",
        content=[],
        metadata={
            "approval_followup": {
                "approval_token": decision["token"],
                "tool_name": "computer_use",
                "request_id": request["request_id"],
                "operation": "browser.open_url",
                "action": "browser.open_url",
            }
        },
        user_message={},
        model="stub/default",
        params={},
        request_context={"user_requested_computer_use": True},
        tool_context={},
        standard_messages=[],
        user_text="",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=list(provider_tools),
        tools_called=[],
        connected_tool_names={"computer_use"},
        call_handler=None,
        model_routing={},
    )
    engine = ChatRunEngine()
    monkeypatch.setattr(
        engine,
        "_execute_tool",
        lambda prepared_arg, tool_name, tool_call_id, arguments: {
            "action": "browser.open_url",
            "opened": True,
            "is_error": False,
        },
    )

    replay = engine._replay_approval_followup_if_present(prepared, [], prepared.chat_ir, None)
    try:
        while True:
            next(replay)
    except StopIteration as stop:
        assert stop.value is None

    assert prepared.provider_tools == provider_tools
    assert "_attached_provider_tools_snapshot" not in prepared.tool_context
    assert prepared.tool_context["approval_replayed"]["request_id"] == request["request_id"]


def test_approval_followup_replays_display_named_job_resume(monkeypatch):
    """Runtime approval can store a manifest display name while followup uses
    the executable tool id. The replay guard must accept that only when both
    names resolve to the same registered tool manifest."""
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    args = {"job_id": "job_123"}
    request = approval.create_approval_request(
        "tool.Job Resume",
        "medium",
        args,
        details={
            "tool_name": "Job Resume",
            "function_id": "tool.Job Resume",
            "operation": "tool.Job Resume",
            "arguments": args,
            "pack_id": "defaultspack",
            "conversation_id": "conv_job_resume",
        },
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    prepared = PreparedChatRun(
        conversation_id="conv_job_resume",
        conversation={},
        input_data={},
        request_id="run_job_resume",
        content=[],
        metadata={
            "approval_followup": {
                "approval_token": decision["token"],
                "tool_name": "job_resume",
                "request_id": request["request_id"],
                "operation": "tool.Job Resume",
            }
        },
        user_message={},
        model="stub/default",
        params={},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[{"name": "job_resume"}],
        tools_called=[],
        connected_tool_names={"job_resume"},
        call_handler=None,
        model_routing={},
    )
    engine = ChatRunEngine()
    invoked = []

    def fake_execute_tool(prepared_arg, tool_name, tool_call_id, arguments):
        invoked.append(
            {
                "prepared": prepared_arg,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": dict(arguments),
            }
        )
        return {"status": "ok", "is_error": False}

    monkeypatch.setattr(engine, "_execute_tool", fake_execute_tool)
    replay = engine._replay_approval_followup_if_present(
        prepared,
        [],
        prepared.chat_ir,
        None,
    )

    events = []
    try:
        while True:
            events.append(next(replay))
    except StopIteration as stop:
        replay_result = stop.value

    assert replay_result is None
    assert [event["phase"] for event in events] == ["tool_call_started", "tool_call_completed"]
    assert invoked == [
        {
            "prepared": prepared,
            "tool_name": "job_resume",
            "tool_call_id": request["request_id"],
            "arguments": {**args, "approval_token": decision["token"]},
        }
    ]
    assert prepared.provider_tools == []
    assert prepared.tool_context["approval_replayed"]["request_id"] == request["request_id"]


def test_hidden_authority_followup_continues_to_model_without_job_resume(monkeypatch):
    """Authority resume metadata should retry the interrupted model request
    directly instead of executing the public ``job_resume`` tool."""
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine

    prepared = PreparedChatRun(
        conversation_id="conv_authority_resume",
        conversation={},
        input_data={},
        request_id="run_authority_resume",
        content=[],
        metadata={
            "authority_followup": {
                "request_id": "auth_resume_1",
                "permission_id": "model.invoke",
                "hidden": True,
            },
            "chat_display": {
                "hidden": True,
                "reason": "authority_followup",
            },
        },
        user_message={
            "metadata": {
                "authority_followup": {
                    "request_id": "auth_resume_1",
                    "permission_id": "model.invoke",
                    "hidden": True,
                },
                "chat_display": {
                    "hidden": True,
                    "reason": "authority_followup",
                },
            }
        },
        model="stub/default",
        params={},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[{"name": "job_resume"}],
        tools_called=[],
        connected_tool_names={"job_resume"},
        call_handler=None,
        model_routing={},
    )
    engine = ChatRunEngine()
    captured_model_turns = []

    def fail_execute_tool_use(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("authority resume must not execute job_resume")

    def fake_model_turn(prepared_arg, working_messages, draft):
        captured_model_turns.append(
            {
                "prepared": prepared_arg,
                "messages": list(working_messages),
            }
        )
        if False:
            yield {}
        return {"content": [{"type": "text", "text": "ok"}], "finish_reason": "stop"}, []

    monkeypatch.setattr(engine, "_execute_tool_use", fail_execute_tool_use)
    monkeypatch.setattr(engine, "_model_turn", fake_model_turn)

    execution = engine._execute(prepared, None)
    try:
        while True:
            next(execution)
    except StopIteration as stop:
        result = stop.value

    assert result["finish_reason"] == "stop"
    assert len(captured_model_turns) == 1
    assert prepared.tool_context["authority_resume_followup_applied"] == {
        "request_id": "auth_resume_1",
        "tool_name": "job_resume",
    }


def test_authority_resume_reaches_first_computer_use_tool_call(monkeypatch):
    """After model/API approval, the hidden authority resume must reach the
    original provider-tool path and allow the model's first computer-use call
    to start."""
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine

    approval_tokens = {
        "model.invoke": {
            "request_id": "auth_model_1",
            "approval_token": "tok_model",
            "permission_id": "model.invoke",
        },
        "api_key.use": {
            "request_id": "auth_api_1",
            "approval_token": "tok_api",
            "permission_id": "api_key.use",
        },
        "network.egress": {
            "request_id": "auth_network_1",
            "approval_token": "tok_network",
            "permission_id": "network.egress",
        },
    }
    metadata = {
        "authority_followup": {
            "approval_token": "tok_model",
            "request_id": "auth_model_1",
            "permission_id": "model.invoke",
            "approvals": list(approval_tokens.values()),
            "hidden": True,
        },
        "chat_display": {
            "hidden": True,
            "reason": "authority_followup",
        },
    }
    prepared = PreparedChatRun(
        conversation_id="conv_authority_computer_resume",
        conversation={},
        input_data={},
        request_id="run_authority_computer_resume",
        content=[],
        metadata=metadata,
        user_message={"metadata": metadata},
        model="openai/gpt-4o-mini",
        params={},
        request_context={
            "authority_resume_followup": True,
            "authority": {
                "principal_id": "profile:work",
                "conversation_id": "conv_authority_computer_resume",
                "request_id": "auth_model_1",
                "permission_id": "model.invoke",
                "approval_tokens": approval_tokens,
            },
        },
        tool_context={},
        standard_messages=[],
        user_text="",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[{"name": "job_resume"}, {"name": "computer_use"}],
        tools_called=[],
        connected_tool_names={"job_resume", "computer_use"},
        call_handler=None,
        model_routing={"selected_model": "openai/gpt-4o-mini"},
    )
    engine = ChatRunEngine()
    captured_model_turns = []
    executed = []

    def fake_model_turn(prepared_arg, working_messages, draft):
        captured_model_turns.append(
            {
                "tools": list(prepared_arg.provider_tools or []),
                "messages": list(working_messages),
            }
        )
        if len(captured_model_turns) == 1:
            if False:
                yield {}
            return (
                {"content": [{"type": "text", "text": ""}], "finish_reason": "tool_calls"},
                [
                    {
                        "id": "call_atlas_open_google",
                        "name": "computer_use",
                        "input": {
                            "action": "browser.open_url",
                            "url": "https://google.com",
                            "target_app": "atlas",
                        },
                    }
                ],
            )
        if False:
            yield {}
        return {"content": [{"type": "text", "text": "opened"}], "finish_reason": "stop"}, []

    def fake_execute_tool(prepared_arg, tool_name, tool_call_id, arguments):
        executed.append(
            {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": dict(arguments or {}),
            }
        )
        return {"result": {"ok": True}, "is_error": False}

    monkeypatch.setattr(engine, "_model_turn", fake_model_turn)
    monkeypatch.setattr(engine, "_execute_tool", fake_execute_tool)

    events = []
    execution = engine._execute(prepared, None)
    try:
        while True:
            event = next(execution)
            events.append(event)
    except StopIteration as stop:
        result = stop.value

    assert result["finish_reason"] == "stop"
    assert len(captured_model_turns) >= 1
    assert captured_model_turns[0]["tools"] == [{"name": "job_resume"}, {"name": "computer_use"}]
    assert prepared.tool_context["authority_resume_followup_applied"] == {
        "request_id": "auth_model_1",
        "tool_name": "job_resume",
    }
    assert executed == [
        {
            "tool_name": "computer_use",
            "tool_call_id": "call_atlas_open_google",
            "arguments": {
                "action": "browser.open_url",
                "url": "https://google.com",
                "target_app": "atlas",
            },
        }
    ]
    started = [event for event in events if event.get("type") == "tool_call_started"]
    assert started
    assert started[0]["tool_name"] == "computer_use"
    assert started[0]["tool_call_id"] == "call_atlas_open_google"


def test_hidden_authority_followup_for_non_model_permissions_skips_job_resume(monkeypatch):
    """Related Authority approvals such as network/API-key use also retry the
    model request directly."""
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine

    prepared = PreparedChatRun(
        conversation_id="conv_authority_network_resume",
        conversation={},
        input_data={},
        request_id="run_authority_network_resume",
        content=[],
        metadata={
            "authority_followup": {
                "request_id": "auth_network_1",
                "permission_id": "network.egress",
                "hidden": True,
            },
            "chat_display": {
                "hidden": True,
                "reason": "authority_followup",
            },
        },
        user_message={
            "metadata": {
                "authority_followup": {
                    "request_id": "auth_network_1",
                    "permission_id": "network.egress",
                    "hidden": True,
                },
                "chat_display": {
                    "hidden": True,
                    "reason": "authority_followup",
                },
            }
        },
        model="stub/default",
        params={},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[{"name": "job_resume"}],
        tools_called=[],
        connected_tool_names={"job_resume"},
        call_handler=None,
        model_routing={},
    )
    engine = ChatRunEngine()
    captured_model_turns = []

    def fail_execute_tool_use(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("authority resume must not execute job_resume")

    def fake_model_turn(prepared_arg, working_messages, draft):
        captured_model_turns.append(list(working_messages))
        if False:
            yield {}
        return {"content": [{"type": "text", "text": "ok"}], "finish_reason": "stop"}, []

    monkeypatch.setattr(engine, "_execute_tool_use", fail_execute_tool_use)
    monkeypatch.setattr(engine, "_model_turn", fake_model_turn)

    execution = engine._execute(prepared, None)
    try:
        while True:
            next(execution)
    except StopIteration as stop:
        result = stop.value

    assert result["finish_reason"] == "stop"
    assert len(captured_model_turns) == 1
    assert prepared.tool_context["authority_resume_followup_applied"] == {
        "request_id": "auth_network_1",
        "tool_name": "job_resume",
    }


def test_approval_followup_deterministically_replays_tool_once(tmp_path, monkeypatch):
    """End-to-end: approval-followup must execute the pending tool exactly once
    with the stored args + token, surface ``executed_tools`` on the assistant
    message, strip provider tools so the model only summarises, and keep the
    one-shot token replay-safe afterwards."""
    from blocks.chat.stream import run as stream_run
    import domain.chat.stream_engine as engine_module
    from domain.chat.store import ChatStore
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    args = {"message": "fix typo", "paths": ["a.txt"]}
    original_tool_call_id = "call_commit_original"
    token, request_id = _approve_pending(
        args, tool_name="coding_git_commit", operation="tool.coding_git_commit",
    )
    # Sanity check: the approval request must be visible from the same module
    # instance that the stream engine will import as ``domain.safety.approval``.
    assert approval.get_approval_request(request_id) is not None
    store, conversation = _make_conversation_with_followup(
        tmp_path, monkeypatch,
        args=args, token=token, request_id=request_id, tool_name="coding_git_commit",
    )

    recorded = {}
    import blocks.chat.stream as stream_module
    monkeypatch.setattr(engine_module, "AIClient", lambda: _NoToolFakeClient(recorded))
    monkeypatch.setattr(stream_module, "AIClient", lambda: _NoToolFakeClient(recorded))

    # Make the post-replay summary turn deterministic and provider-independent:
    # patch ``_complete_turn`` so it does not depend on which AI provider /
    # gateway path the engine happens to take after replay. Whatever the model
    # routing resolves to, the summary will always be the same fixed text and
    # the recorded ``tools`` list still surfaces whether provider tools were
    # stripped.
    from domain.chat.stream_engine import ChatRunEngine

    def _fake_complete_turn(self, prepared, messages):
        recorded.setdefault("complete_calls", []).append(
            {
                "model": prepared.model,
                "tools": list(prepared.provider_tools or []),
                "messages": list(messages or []),
            }
        )
        return {
            "content": [{"type": "text", "text": "Commit summary: hash=abc1234."}],
            "finish_reason": "stop",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(ChatRunEngine, "_complete_turn", _fake_complete_turn)

    invoked = []

    def _fake_execute(self, tool_name, arguments, context):
        invoked.append({"tool_name": tool_name, "arguments": dict(arguments)})
        # Mimic the real ``coding_git_commit`` tool: consuming the approval
        # token so the one-shot replay contract (token is single-use) is
        # exercised end-to-end without depending on the real tool body.
        token = str((arguments or {}).get("approval_token") or "").strip()
        if token:
            try:
                from domain.safety import approval as _approval_mod
                replay_args = {k: v for k, v in arguments.items() if k != "approval_token"}
                _approval_mod.verify_execution_token(
                    token,
                    "tool.coding_git_commit",
                    _approval_mod.hash_arguments(replay_args),
                    consume=True,
                )
            except Exception:
                pass
        return {
            "result": "Commit created",
            "is_error": False,
            "widget": None,
            "data": {"commit_hash": "abc1234"},
        }

    with patch.object(ToolExecutor, "execute", _fake_execute):
        result = stream_run(
            {
                "conversation_id": conversation["id"],
                "message": {
                    "role": "user",
                    "content": "ユーザーが許可しました。承認済みの操作を続行してください。",
                    "metadata": {
                        "approval_followup": {
                            "approval_token": token,
                            "operation": "tool.coding_git_commit",
                            "request_id": request_id,
                            "tool_call_id": original_tool_call_id,
                            "tool_name": "coding_git_commit",
                            "action": "git.commit",
                        },
                    },
                },
                "tools": [],
            },
            {},
        )
        # Drain the SSE generator while the executor patch is active so the
        # replay-stage tool invocation is captured by ``invoked``.
        events = list(result["events"])

    assert result.get("_sse") is True, result
    # Tool must have been replayed exactly once with the stored args + token.
    assert len(invoked) == 1, invoked
    assert invoked[0]["tool_name"] == "coding_git_commit"
    assert invoked[0]["arguments"]["message"] == args["message"]
    assert invoked[0]["arguments"]["paths"] == args["paths"]
    assert invoked[0]["arguments"]["approval_token"] == token

    started = [event for event in events if event.get("type") == "tool_call_started"]
    completed = [event for event in events if event.get("type") == "tool_call_completed"]
    assert len(started) == 1
    assert started[0].get("approval_replay") is True
    assert started[0].get("tool_name") == "coding_git_commit"
    assert started[0].get("tool_call_id") == original_tool_call_id
    assert len(completed) == 1
    assert completed[0].get("approval_replay") is True
    assert completed[0].get("tool_call_id") == original_tool_call_id

    # The model turn must have run with provider_tools stripped, otherwise
    # the model could re-call the pending tool from the same followup turn.
    assert recorded.get("complete_calls"), "model was never invoked for the summary"
    assert recorded["complete_calls"][0]["tools"] == []
    replay_messages = recorded["complete_calls"][0]["messages"]
    assistant_tool_message = next(message for message in replay_messages if message.get("role") == "assistant" and message.get("tool_calls"))
    tool_result_message = next(message for message in replay_messages if message.get("role") == "tool")
    assert assistant_tool_message["tool_calls"][0]["id"] == original_tool_call_id
    assert tool_result_message["tool_call_id"] == original_tool_call_id

    # The finalised assistant message must surface the deterministically
    # executed tool, which is the user-visible signal that ``executed_tools=[]``
    # hallucination is fixed.
    done_events = [event for event in events if event.get("type") == "done"]
    assert done_events, events
    final_message = done_events[-1]["message"]
    assert final_message["metadata"]["executed_tools"] == ["coding_git_commit"]
    assert final_message["raw_text"] == "Commit summary: hash=abc1234."

    # Token is now consumed; replaying the same followup must not run the tool
    # a second time.
    args_hash = approval.hash_arguments(args)
    verification = approval.verify_execution_token(
        token, "tool.coding_git_commit", args_hash, consume=False,
    )
    assert verification.valid is False
    ChatStore._instance = None


def test_approval_followup_without_token_falls_through_to_model(tmp_path, monkeypatch):
    """Without an ``approval_followup`` block the engine must keep the existing
    model-driven path: no synthetic replay, provider_tools untouched."""
    from blocks.chat.stream import run as stream_run
    import domain.chat.stream_engine as engine_module
    from domain.chat.store import ChatStore
    from domain.tool.executor import ToolExecutor

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    recorded = {}
    import blocks.chat.stream as stream_module
    monkeypatch.setattr(engine_module, "AIClient", lambda: _NoToolFakeClient(recorded))
    monkeypatch.setattr(stream_module, "AIClient", lambda: _NoToolFakeClient(recorded))

    invoked = []

    def _fake_execute(self, tool_name, arguments, context):  # pragma: no cover - shouldn't run
        invoked.append({"tool_name": tool_name, "arguments": dict(arguments)})
        return {"result": "noop", "is_error": False, "widget": None}

    with patch.object(ToolExecutor, "execute", _fake_execute):
        result = stream_run(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "hello"},
                "tools": [],
            },
            {},
        )
        events = list(result["events"])

    assert result.get("_sse") is True
    # No replay must happen when followup metadata is absent.
    assert invoked == []
    done_events = [event for event in events if event.get("type") == "done"]
    assert done_events
    final_message = done_events[-1]["message"]
    assert final_message["metadata"]["executed_tools"] == []
    ChatStore._instance = None


def test_approval_followup_with_invalid_token_falls_through(tmp_path, monkeypatch):
    """A tampered or unknown approval token must not trigger the replay path
    so we never execute a tool the user did not approve."""
    from blocks.chat.stream import run as stream_run
    import domain.chat.stream_engine as engine_module
    from domain.chat.store import ChatStore
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    approval.reset_approval_state_for_tests()
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    recorded = {}
    import blocks.chat.stream as stream_module
    monkeypatch.setattr(engine_module, "AIClient", lambda: _NoToolFakeClient(recorded))
    monkeypatch.setattr(stream_module, "AIClient", lambda: _NoToolFakeClient(recorded))

    invoked = []

    def _fake_execute(self, tool_name, arguments, context):  # pragma: no cover - must not run
        invoked.append(tool_name)
        return {"result": "noop", "is_error": False, "widget": None}

    with patch.object(ToolExecutor, "execute", _fake_execute):
        result = stream_run(
            {
                "conversation_id": conversation["id"],
                "message": {
                    "role": "user",
                    "content": "ユーザーが許可しました。",
                    "metadata": {
                        "approval_followup": {
                            "approval_token": "garbage.token",
                            "operation": "tool.coding_git_commit",
                            "request_id": "apr_unknown",
                            "tool_name": "coding_git_commit",
                        },
                    },
                },
                "tools": [],
            },
            {},
        )
        events = list(result["events"])

    assert result.get("_sse") is True
    # Invalid token must fall through to the model path: no synthetic replay.
    assert invoked == []
    done_events = [event for event in events if event.get("type") == "done"]
    assert done_events
    final_message = done_events[-1]["message"]
    assert final_message["metadata"]["executed_tools"] == []
    ChatStore._instance = None


def test_approval_followup_tool_name_mismatch_falls_through(tmp_path, monkeypatch):
    """When the original approval request stored ``tool_name`` but the
    followup metadata targets a different tool, the engine must NOT replay
    the stored tool. Otherwise an attacker (or a stale UI) could reuse a
    valid token approved for tool A to invoke tool B with the same args.

    The engine must fall through to the regular model-driven path: no
    synthetic execution, no synthetic tool_use/tool_result on the chain,
    and ``executed_tools`` empty on the finalised assistant message.
    """
    from blocks.chat.stream import run as stream_run
    import domain.chat.stream_engine as engine_module
    from domain.chat.store import ChatStore
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    args = {"message": "fix typo", "paths": ["a.txt"]}
    # Approval request explicitly records ``tool_name="coding_git_commit"``.
    token, request_id = _approve_pending(
        args, tool_name="coding_git_commit", operation="tool.coding_git_commit",
    )
    assert approval.get_approval_request(request_id) is not None

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    recorded = {}
    import blocks.chat.stream as stream_module
    monkeypatch.setattr(engine_module, "AIClient", lambda: _NoToolFakeClient(recorded))
    monkeypatch.setattr(stream_module, "AIClient", lambda: _NoToolFakeClient(recorded))

    invoked = []

    def _fake_execute(self, tool_name, arguments, context):  # pragma: no cover - must not run
        invoked.append(tool_name)
        return {"result": "noop", "is_error": False, "widget": None}

    with patch.object(ToolExecutor, "execute", _fake_execute):
        result = stream_run(
            {
                "conversation_id": conversation["id"],
                "message": {
                    "role": "user",
                    "content": "ユーザーが許可しました。",
                    "metadata": {
                        "approval_followup": {
                            "approval_token": token,
                            "operation": "tool.coding_git_commit",
                            "request_id": request_id,
                            # Mismatch: original request stored
                            # ``coding_git_commit`` but the followup targets
                            # a different tool.
                            "tool_name": "coding_git_push",
                        },
                    },
                },
                "tools": [],
            },
            {},
        )
        events = list(result["events"])

    assert result.get("_sse") is True
    # Tool-name mismatch must abort replay before any synthetic execution.
    assert invoked == []
    started = [event for event in events if event.get("type") == "tool_call_started"]
    assert started == [], "no synthetic tool_call_started must be emitted on mismatch"
    done_events = [event for event in events if event.get("type") == "done"]
    assert done_events
    final_message = done_events[-1]["message"]
    assert final_message["metadata"]["executed_tools"] == []
    # The token must remain unconsumed - mismatch should not burn it.
    args_hash = approval.hash_arguments(args)
    verification = approval.verify_execution_token(
        token, "tool.coding_git_commit", args_hash, consume=False,
    )
    assert verification.valid is True
    ChatStore._instance = None


def test_approval_followup_replay_with_nested_approval_required_short_circuits(tmp_path, monkeypatch):
    """If the replayed tool result itself reports ``approval_required`` (a
    chained / nested approval), the engine must surface the approval path
    directly and NOT advance to the natural-language summary turn. Letting
    the model speak in that state would produce the exact same hallucinated
    success the deterministic replay was introduced to prevent.

    Pinned behaviour:

    * an ``approval_requested`` event is emitted immediately after the
      synthetic ``tool_call_completed`` event;
    * the AI client is never called for a summary turn (the model loop is
      short-circuited);
    * the finalised assistant message carries ``finish_reason=approval_required``
      so the UI keeps the approval gate visible.
    """
    from blocks.chat.stream import run as stream_run
    import domain.chat.stream_engine as engine_module
    from domain.chat.store import ChatStore
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    args = {"message": "fix typo", "paths": ["a.txt"]}
    token, request_id = _approve_pending(
        args, tool_name="coding_git_commit", operation="tool.coding_git_commit",
    )

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    recorded = {}
    import blocks.chat.stream as stream_module
    monkeypatch.setattr(engine_module, "AIClient", lambda: _NoToolFakeClient(recorded))
    monkeypatch.setattr(stream_module, "AIClient", lambda: _NoToolFakeClient(recorded))

    # Patch ``_complete_turn`` to fail loudly if the model is ever asked to
    # speak: the short-circuit must keep us out of the summary turn.
    from domain.chat.stream_engine import ChatRunEngine

    summary_calls: list[dict] = []

    def _fail_complete_turn(self, prepared, messages):  # pragma: no cover - must not run
        summary_calls.append({"model": prepared.model})
        raise AssertionError(
            "model summary turn must not run when replay surfaces approval_required"
        )

    monkeypatch.setattr(ChatRunEngine, "_complete_turn", _fail_complete_turn)

    invoked: list[dict] = []

    def _fake_execute(self, tool_name, arguments, context):
        invoked.append({"tool_name": tool_name, "arguments": dict(arguments)})
        # Simulate a chained / nested approval: the tool consumed its own
        # one-shot token but its result still reports another approval is
        # required (e.g. the underlying capability layer rejected the
        # current scope and raised a fresh approval gate).
        return {
            "result": "secondary approval required",
            "is_error": False,
            "widget": None,
            "requires_approval": True,
            "approval_required": True,
            "approval_request_id": "apr_nested_demo",
            "risk_level": "high",
            "action": "git.commit",
            "payload": {
                "action": "git.commit",
                "payload": dict(arguments),
            },
            "message": "secondary approval required",
        }

    with patch.object(ToolExecutor, "execute", _fake_execute):
        result = stream_run(
            {
                "conversation_id": conversation["id"],
                "message": {
                    "role": "user",
                    "content": "ユーザーが許可しました。続行してください。",
                    "metadata": {
                        "approval_followup": {
                            "approval_token": token,
                            "operation": "tool.coding_git_commit",
                            "request_id": request_id,
                            "tool_name": "coding_git_commit",
                        },
                    },
                },
                "tools": [],
            },
            {},
        )
        events = list(result["events"])

    assert result.get("_sse") is True
    # Replay still ran exactly once, but the summary turn never started.
    assert len(invoked) == 1
    assert summary_calls == []

    started = [event for event in events if event.get("type") == "tool_call_started"]
    completed = [event for event in events if event.get("type") == "tool_call_completed"]
    approval_events = [event for event in events if event.get("type") == "approval_requested"]
    assert len(started) == 1 and started[0].get("approval_replay") is True
    assert len(completed) == 1 and completed[0].get("approval_replay") is True
    # The approval_requested event must follow the tool_call_completed event,
    # not be swallowed by the summary turn.
    assert approval_events, events
    assert approval_events[0].get("tool_name") == "coding_git_commit"
    # Defensive scrub: the chained-approval simulation above returns
    # ``payload=dict(arguments)`` which includes the outer (now spent)
    # one-shot ``approval_token``. The bubbled-up approval payload must
    # NOT carry that token forward, otherwise UIs / downstream loggers
    # would see the spent credential and a malicious component could
    # attempt to replay it. The chained approval must mint its own
    # token, never recycle ours.
    nested_payload = approval_events[0].get("payload") or {}
    assert isinstance(nested_payload, dict)
    assert "approval_token" not in nested_payload, approval_events[0]
    assert "approval_token" not in nested_payload.get("payload", {}), approval_events[0]
    assert approval_events[0].get("approval_token") != token, approval_events[0]

    done_events = [event for event in events if event.get("type") == "done"]
    assert done_events
    final_message = done_events[-1]["message"]
    # The replayed tool *was* executed deterministically (single shot), so it
    # must still surface in executed_tools, but the assistant must remain in
    # the approval-waiting state instead of summarising success.
    assert final_message["metadata"]["executed_tools"] == ["coding_git_commit"]
    assert final_message.get("finish_reason") == "approval_required"
    ChatStore._instance = None


def test_approval_followup_replay_with_tool_blocked_recovery_short_circuits(tmp_path, monkeypatch):
    """If the replayed tool reports a recovery kind that blocks further
    automation (``visible_window_required`` / ``focus_required``), the
    engine must emit the same ``tool_blocked`` status the model-driven path
    emits and short-circuit the model loop. Otherwise the model would
    speak as if the operation succeeded while the underlying tool never
    reached the host.
    """
    from blocks.chat.stream import run as stream_run
    import domain.chat.stream_engine as engine_module
    from domain.chat.store import ChatStore
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    args = {"message": "focus me", "paths": ["a.txt"]}
    token, request_id = _approve_pending(
        args, tool_name="coding_git_commit", operation="tool.coding_git_commit",
    )

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    recorded = {}
    import blocks.chat.stream as stream_module
    monkeypatch.setattr(engine_module, "AIClient", lambda: _NoToolFakeClient(recorded))
    monkeypatch.setattr(stream_module, "AIClient", lambda: _NoToolFakeClient(recorded))

    from domain.chat.stream_engine import ChatRunEngine

    summary_calls: list[dict] = []

    def _fail_complete_turn(self, prepared, messages):  # pragma: no cover - must not run
        summary_calls.append({"model": prepared.model})
        raise AssertionError(
            "model summary turn must not run when replay surfaces tool_blocked"
        )

    monkeypatch.setattr(ChatRunEngine, "_complete_turn", _fail_complete_turn)

    invoked: list[dict] = []

    def _fake_execute(self, tool_name, arguments, context):
        invoked.append({"tool_name": tool_name, "arguments": dict(arguments)})
        return {
            "result": "target window not visible",
            "is_error": True,
            "widget": None,
            "recovery": {"kind": "visible_window_required"},
        }

    with patch.object(ToolExecutor, "execute", _fake_execute):
        result = stream_run(
            {
                "conversation_id": conversation["id"],
                "message": {
                    "role": "user",
                    "content": "ユーザーが許可しました。続行してください。",
                    "metadata": {
                        "approval_followup": {
                            "approval_token": token,
                            "operation": "tool.coding_git_commit",
                            "request_id": request_id,
                            "tool_name": "coding_git_commit",
                        },
                    },
                },
                "tools": [],
            },
            {},
        )
        events = list(result["events"])

    assert result.get("_sse") is True
    assert len(invoked) == 1
    assert summary_calls == []

    started = [event for event in events if event.get("type") == "tool_call_started"]
    completed = [event for event in events if event.get("type") == "tool_call_completed"]
    blocked_status = [
        event for event in events
        if event.get("type") == "status"
        and (
            event.get("recovery_kind") == "visible_window_required"
            or event.get("phase") == "tool_blocked"
        )
    ]
    assert len(started) == 1 and started[0].get("approval_replay") is True
    assert len(completed) == 1 and completed[0].get("approval_replay") is True
    assert blocked_status, events

    done_events = [event for event in events if event.get("type") == "done"]
    assert done_events
    final_message = done_events[-1]["message"]
    # Replay ran once, so the executed tool surfaces, but the assistant must
    # remain in the blocked-recovery state instead of summarising success.
    assert final_message["metadata"]["executed_tools"] == ["coding_git_commit"]
    assert final_message.get("finish_reason") in {"tool_blocked", "stop"}, final_message
    ChatStore._instance = None


def test_approval_followup_token_cannot_replay_twice(monkeypatch):
    """The one-shot approval token must be replay-safe across separate
    chat runs: once the first ``approval_followup`` has executed the
    pending tool (which consumes the token via ``verify_execution_token``
    and flips the request status to ``consumed``), a *second* chat run
    that carries the same ``approval_token`` + ``request_id`` must NOT
    execute the tool a second time or fall through to a regular
    model-driven turn. Hidden approval followups are deterministic replay
    requests; a consumed one must stop instead of letting the model
    rediscover and reissue stale tools.
    """
    from domain.chat.run_request import PreparedChatRun
    from domain.chat.stream_engine import ChatRunEngine
    from domain.safety import approval

    approval.reset_approval_state_for_tests()
    args = {"message": "fix typo", "paths": ["a.txt"]}
    token, request_id = _approve_pending(
        args, tool_name="coding_git_commit", operation="tool.coding_git_commit",
    )

    invoked: list[dict] = []
    model_turns: list[dict] = []

    def make_prepared(run_id: str) -> PreparedChatRun:
        metadata = {
            "approval_followup": {
                "approval_token": token,
                "operation": "tool.coding_git_commit",
                "request_id": request_id,
                "tool_name": "coding_git_commit",
            },
        }
        return PreparedChatRun(
            conversation_id="conv_replay_twice",
            conversation={},
            input_data={},
            request_id=run_id,
            content=[],
            metadata=metadata,
            user_message={"metadata": metadata},
            model="stub/default",
            params={},
            request_context={},
            tool_context={},
            standard_messages=[],
            user_text="",
            system_prompt="",
            enrich_info={},
            raw_tools=[],
            provider_tools=[{"name": "coding_git_commit"}],
            tools_called=[],
            connected_tool_names={"coding_git_commit"},
            call_handler=None,
            model_routing={},
        )

    def _fake_execute_tool(prepared, tool_name, tool_call_id, arguments):
        invoked.append(
            {
                "run_id": prepared.request_id,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": dict(arguments),
            }
        )
        # Real coding tools consume the one-shot token via
        # ``verify_execution_token(consume=True)``; mirror that here so the
        # approval store transitions to ``consumed`` exactly the way the
        # production tool body does, exercising the replay-safety contract
        # end-to-end without depending on the real tool implementation.
        tok = str((arguments or {}).get("approval_token") or "").strip()
        if tok:
            try:
                from domain.safety import approval as _approval_mod

                replay_args = {k: v for k, v in arguments.items() if k != "approval_token"}
                _approval_mod.verify_execution_token(
                    tok,
                    "tool.coding_git_commit",
                    _approval_mod.hash_arguments(replay_args),
                    consume=True,
                )
            except Exception:
                pass
        return {
            "result": "Commit created",
            "is_error": False,
            "widget": None,
            "data": {"commit_hash": "abc1234"},
        }

    def _fake_model_turn(prepared, working_messages, draft):
        model_turns.append(
            {
                "run_id": prepared.request_id,
                "tools": list(prepared.provider_tools or []),
                "messages": list(working_messages),
            }
        )
        if False:
            yield {}
        return {
            "content": [{"type": "text", "text": "ok"}],
            "finish_reason": "stop",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }, []

    first_engine = ChatRunEngine()
    monkeypatch.setattr(first_engine, "_execute_tool", _fake_execute_tool)
    monkeypatch.setattr(first_engine, "_model_turn", _fake_model_turn)
    first_execution = first_engine._execute(make_prepared("run_first"), None)
    first_events = []
    try:
        while True:
            first_events.append(next(first_execution))
    except StopIteration as stop:
        first_result = stop.value

    # The approval store must now report the request as consumed and
    # ``verify_execution_token`` must reject the same token, before the
    # second chat run is even attempted.
    request_after_first = approval.get_approval_request(request_id)
    assert request_after_first is not None
    assert request_after_first["status"] == "consumed"
    verification_after_first = approval.verify_execution_token(
        token,
        "tool.coding_git_commit",
        approval.hash_arguments(args),
        consume=False,
    )
    assert verification_after_first.valid is False

    second_engine = ChatRunEngine()

    def fail_execute_tool(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("consumed approval followup must not execute the tool again")

    def fail_model_turn(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("consumed approval followup must not enter another model turn")

    monkeypatch.setattr(second_engine, "_execute_tool", fail_execute_tool)
    monkeypatch.setattr(second_engine, "_model_turn", fail_model_turn)
    second_execution = second_engine._execute(make_prepared("run_second"), None)
    second_events = []
    try:
        while True:
            second_events.append(next(second_execution))
    except StopIteration as stop:
        second_result = stop.value

    # First run replayed the tool exactly once.
    assert first_result["finish_reason"] == "stop"
    started_first = [event for event in first_events if event.get("type") == "tool_call_started"]
    assert len(started_first) == 1
    assert started_first[0].get("approval_replay") is True

    # Second run: the same token must not produce a synthetic replay event,
    # invoke the tool again, or enter another model turn.
    started_second = [event for event in second_events if event.get("type") == "tool_call_started"]
    replay_second = [event for event in started_second if event.get("approval_replay") is True]
    assert replay_second == [], second_events
    assert started_second == []
    assert len(invoked) == 1, invoked
    assert len(model_turns) == 1

    # The second assistant message must be a terminal duplicate-followup
    # response: no synthetic execution surfaces in ``executed_tools``.
    assert [event.get("phase") for event in second_events] == ["approval_followup_consumed"]
    assert second_result["finish_reason"] == "stop"
    assert second_result["metadata"]["approval_followup"]["status"] == "consumed"


def test_approval_followup_replay_keeps_attached_tools_metadata_truthful(tmp_path, monkeypatch):
    """The replay path suppresses ``provider_tools`` for the *summary turn
    only* so the model cannot re-issue another tool call from the same
    followup. The finalised assistant ``metadata.attached_tools`` and
    ``metadata.attached_tool_count`` must still reflect the truthful set
    of tools the conversation was started with - otherwise auditors and
    UI surfaces would see ``attached_tools=[]`` for a turn that was
    actually attached to coding tools, masking tool-policy bugs.
    """
    from blocks.chat.stream import run as stream_run
    import domain.chat.stream_engine as engine_module
    from domain.chat.store import ChatStore
    from domain.chat.run_request import prepare_chat_run as _real_prepare_chat_run
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    args = {"message": "fix typo", "paths": ["a.txt"]}
    token, request_id = _approve_pending(
        args, tool_name="coding_git_commit", operation="tool.coding_git_commit",
    )

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")

    # Inject a non-empty ``provider_tools`` list on the prepared run so the
    # suppression in the replay path is observable AND the metadata
    # snapshot path is exercised. Going through ``prepare_chat_run``
    # naturally would require a full tool-policy + eligibility setup that
    # is not the subject of this regression.
    fake_tool_def = {
        "type": "function",
        "function": {
            "name": "coding_git_commit",
            "description": "Stage + commit changes",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    def _wrapped_prepare(input_data, context):
        prepared = _real_prepare_chat_run(input_data, context)
        prepared.provider_tools = [fake_tool_def]
        seen = {name for name in prepared.tools_called or []}
        if "coding_git_commit" not in seen:
            prepared.tools_called = list(prepared.tools_called or []) + ["coding_git_commit"]
        return prepared

    monkeypatch.setattr(engine_module, "prepare_chat_run", _wrapped_prepare)

    recorded = {}
    import blocks.chat.stream as stream_module
    monkeypatch.setattr(engine_module, "AIClient", lambda: _NoToolFakeClient(recorded))
    monkeypatch.setattr(stream_module, "AIClient", lambda: _NoToolFakeClient(recorded))

    from domain.chat.stream_engine import ChatRunEngine

    def _fake_complete_turn(self, prepared, messages):
        recorded.setdefault("complete_calls", []).append(
            {
                "model": prepared.model,
                "tools": list(prepared.provider_tools or []),
                "messages": messages,
            }
        )
        return {
            "content": [{"type": "text", "text": "Commit summary: hash=abc1234."}],
            "finish_reason": "stop",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(ChatRunEngine, "_complete_turn", _fake_complete_turn)

    invoked: list[dict] = []

    def _fake_execute(self, tool_name, arguments, context):
        invoked.append({"tool_name": tool_name, "arguments": dict(arguments)})
        return {
            "result": "Commit created",
            "is_error": False,
            "widget": None,
            "data": {"commit_hash": "abc1234"},
        }

    with patch.object(ToolExecutor, "execute", _fake_execute):
        result = stream_run(
            {
                "conversation_id": conversation["id"],
                "message": {
                    "role": "user",
                    "content": "ユーザーが許可しました。続行してください。",
                    "metadata": {
                        "approval_followup": {
                            "approval_token": token,
                            "operation": "tool.coding_git_commit",
                            "request_id": request_id,
                            "tool_name": "coding_git_commit",
                        },
                    },
                },
                "tools": [],
            },
            {},
        )
        events = list(result["events"])

    assert result.get("_sse") is True
    # Replay must have run once and the summary turn must have run with
    # provider_tools suppressed (so the model cannot re-issue another tool
    # call from the same followup turn).
    assert len(invoked) == 1
    assert recorded.get("complete_calls"), "summary turn never ran"
    assert recorded["complete_calls"][0]["tools"] == []

    done_events = [event for event in events if event.get("type") == "done"]
    assert done_events
    final_message = done_events[-1]["message"]
    metadata = final_message["metadata"]
    # Despite the transient suppression, the truthful attached-tool set
    # must remain on the finalised metadata.
    assert "coding_git_commit" in metadata["attached_tools"]
    assert metadata["attached_tool_count"] == 1
    assert "coding_git_commit" in metadata["attached_provider_tools"]
    assert metadata["executed_tools"] == ["coding_git_commit"]
    # The synthetic tool_use block fed to the summary turn must NOT carry
    # the approval token: the model context view of the turn would
    # otherwise expose the one-shot signed token to any downstream
    # serialiser, log, or provider trace. We walk both shapes the chat
    # backend can emit - Anthropic-style ``content``-list-of-blocks and
    # OpenAI-style ``tool_calls[*].function.arguments`` (JSON) - so the
    # leak check is non-vacuous even when ``_append_assistant_tool_use_message``
    # routes through the OpenAI-style ``tool_calls`` field.
    summary_messages = recorded["complete_calls"][0].get("messages")
    assert isinstance(summary_messages, list), recorded["complete_calls"][0]
    assert summary_messages, "summary turn must run with a non-empty message chain"
    saw_synthetic_tool_call = False
    for msg in summary_messages:
        if not isinstance(msg, dict):
            continue
        # Anthropic-style content blocks (``[{"type": "tool_use", "input": {...}}, ...]``).
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"tool_use", "tool_call"}:
                    block_input = block.get("input") or block.get("arguments") or {}
                    if isinstance(block_input, dict):
                        assert "approval_token" not in block_input, block
                        saw_synthetic_tool_call = True
        # OpenAI-style ``tool_calls`` field with JSON-encoded arguments.
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                raw_arguments = fn.get("arguments") if "arguments" in fn else call.get("arguments")
                if isinstance(raw_arguments, str):
                    try:
                        decoded = __import__("json").loads(raw_arguments)
                    except Exception:
                        decoded = {}
                else:
                    decoded = raw_arguments if isinstance(raw_arguments, dict) else {}
                if isinstance(decoded, dict):
                    assert "approval_token" not in decoded, call
                    saw_synthetic_tool_call = True
                # The serialised JSON must not carry the literal token even
                # when the assertion above is bypassed by an exotic shape.
                if isinstance(raw_arguments, str):
                    assert token not in raw_arguments, call
        # Tool-result messages must not echo the token in their content
        # text either - downstream serialisers / loggers would otherwise
        # see the spent token in the model context view.
        if msg.get("role") == "tool":
            tool_content = msg.get("content")
            if isinstance(tool_content, str):
                assert token not in tool_content, msg
    assert saw_synthetic_tool_call, summary_messages
    ChatStore._instance = None


def test_debug_replay_accepts_only_exact_coding_operation_tool_identity():
    # Importing stream_engine initializes its compatibility facades, so keep
    # this pure helper assertion after the stateful replay fixtures above.
    from domain.chat.stream_engine import _approval_replay_operation_allowed

    assert _approval_replay_operation_allowed("file.write", "coding_file_write")
    assert _approval_replay_operation_allowed("git.commit", "coding_git_commit")
    assert not _approval_replay_operation_allowed("file.write", "coding_file_delete")
    assert not _approval_replay_operation_allowed("model.invoke", "coding_model_invoke")
