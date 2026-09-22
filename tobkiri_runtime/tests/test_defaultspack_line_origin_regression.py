from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_conversation_owner")

from domain.external.adapters import line as line_adapter_module  # noqa: E402
from domain.external.adapters.line import LineResponseAdapter  # noqa: E402
from domain.external.audience_policy import AudiencePolicy  # noqa: E402
from domain.external.audience_policy_registry import AudiencePolicyRegistry  # noqa: E402
from domain.external.normalizer import normalize_line_event  # noqa: E402
from domain.external.source_store import ExternalSourceStore  # noqa: E402
from domain.external.response import RumiResponse  # noqa: E402
from domain.external.response_planner import ResponsePlanner  # noqa: E402
from domain.external.targeting import origin_from_external_event  # noqa: E402
from domain.integrations.store import IntegrationConversationStore  # noqa: E402
from domain.chat.store import ChatStore  # noqa: E402
from domain.webhook.endpoint_store import WebhookEndpointStore  # noqa: E402


SECRET = "line-secret"


def _signed_line_payload(payload: dict[str, Any], *, raw_body: bytes | None = None) -> dict[str, Any]:
    raw = raw_body or json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = base64.b64encode(hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).digest()).decode("ascii")
    return {
        **payload,
        "_raw_body_base64": base64.b64encode(raw).decode("ascii"),
        "_headers": {"x-line-signature": signature},
    }


def _install_line_endpoint(
    monkeypatch,
    tmp_path,
    *,
    enabled: bool = True,
    response: dict[str, Any] | None = None,
    input_profile_id: str = "line.default",
    conversation: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    endpoint_path = tmp_path / "endpoints.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_WEBHOOK_ENDPOINTS_PATH", str(endpoint_path))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_EXTERNAL_SOURCES_PATH", str(tmp_path / "external_sources.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(tmp_path / "frontend_settings.json"))
    from domain.external.token_store import set_external_token

    assert set_external_token(
        "line",
        SECRET,
        token_id="channel-secret",
        kind="channel_secret",
    )["success"]
    assert set_external_token(
        "line",
        "line-access-token",
        token_id="channel-access",
        kind="channel_access_token",
    )["success"]
    store = WebhookEndpointStore(endpoint_path)
    store.upsert(
        {
            "id": "line-main",
            "kind": "line",
            "input_profile_id": input_profile_id,
            "audience_policy_id": "line.production",
            "response_profile_id": "line.default",
            "security": {"mode": "provider_signature"},
            "conversation": dict(conversation or {"strategy": "external_key", "model": "stub/default"}),
            "response": dict(response or {}),
            "metadata": dict(metadata or {}),
            "enabled": enabled,
        }
    )


def _remember_line_group(monkeypatch, tmp_path, *, group_id: str = "Cgroup", allow_push: bool = False) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_EXTERNAL_SOURCES_PATH", str(tmp_path / "external_sources.json"))
    event = normalize_line_event(
        {
            "type": "message",
            "source": {"type": "group", "groupId": group_id, "userId": "Uactor"},
            "message": {"id": "m-remember", "type": "text", "text": "hello"},
        },
        verified=True,
        destination="Udestination",
    )
    origin = origin_from_external_event(event)
    store = ExternalSourceStore()
    store.record_origin(origin, verified=True)
    store.update_source("line", "group", group_id, enabled=True, allow_push=allow_push)


def _seed_line_group_conversation(monkeypatch, tmp_path, *, group_id: str = "Cgroup", assistant_text: str = "Which option should I use?") -> dict[str, Any]:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_STORE_PATH", str(tmp_path / "integrations" / "conversations.json"))
    chat_store = ChatStore()
    conversation = IntegrationConversationStore().get_or_create_conversation(
        provider="line",
        external_key=f"line:group:{group_id}",
        title=f"line {group_id}",
        metadata={"seed": True},
        chat_store=chat_store,
        model="stub/default",
    )
    chat_store.add_message(
        conversation["id"],
        {
            "role": "assistant",
            "content": [{"type": "text", "text": assistant_text}],
            "raw_text": assistant_text,
        },
    )
    return conversation


def test_line_route_uses_endpoint_enabled_flag(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=False)
    payload = {"destination": "Udest", "events": []}

    result = line_block.run(_signed_line_payload(payload), {})

    assert result["status"] == "error"
    assert result["_http_status"] == 403


def test_line_route_sends_webhook_acknowledgement_when_reply_token_and_access_token_exist(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        response={"mode": "computer_use_line_biz"},
    )
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "line-access-token")
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    calls: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        captured["context"] = context
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {"provider": "line", "messages": [{"type": "text", "text": "done"}]},
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(
        line_adapter_module,
        "post_json",
        lambda url, headers, body: calls.append({"url": url, "headers": headers, "body": body}) or {"ok": True},
    )
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-ack",
                "replyToken": "reply-ack",
                "source": {"type": "user", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    event_result = result["data"]["events"][0]
    assert calls == [
        {
            "url": "https://api.line.me/v2/bot/message/reply",
            "headers": {"Authorization": "Bearer line-access-token"},
            "body": {"replyToken": "reply-ack", "messages": [{"type": "text", "text": "\u5c4a\u3044\u305f\u3088\uff01"}]},
        }
    ]
    assert event_result["acknowledgement"]["sent"] is True
    assert event_result["acknowledgement"]["text"] == "\u5c4a\u3044\u305f\u3088\uff01"
    assert captured["context"]["line_webhook_acknowledgement"]["sent"] is True
    assert event_result["reply"] == {"sent": False, "reason": "LINE reply token already used for webhook acknowledgement"}


def test_line_computer_use_fake_receive_acknowledges_and_preserves_japanese_prompt(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402
    from blocks.chat import send as chat_send  # noqa: E402

    chat_url = "https://chat.line.biz/Uaccount/chat/Cchat"
    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        input_profile_id="line.computer_use",
        conversation={"strategy": "external_key", "model": "stub/default"},
        response={
            "mode": "computer_use_line_biz",
            "line_biz_chat_url": chat_url,
            "auto_approve_computer_use": True,
        },
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_STORE_PATH", str(tmp_path / "integrations" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_LOCKS_DIR", str(tmp_path / "integrations" / "event_locks"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "line-access-token")
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    calls: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}

    def fake_send_run(request, context):
        captured["request"] = request
        captured["context"] = context
        return {
            "status": "ok",
            "data": {
                "id": "assistant-local",
                "content": [{"type": "text", "text": "local complete"}],
            },
        }

    monkeypatch.setattr(chat_send, "run", fake_send_run)
    monkeypatch.setattr(
        line_adapter_module,
        "post_json",
        lambda url, headers, body: calls.append({"url": url, "headers": headers, "body": body}) or {"ok": True},
    )
    source_text = "\u306a\u3093\u304byoutube music\u3067lofi girl\u6d41\u3057\u3066\u30fc"
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-fake-receive",
                "replyToken": "reply-fake-receive",
                "source": {"type": "user", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": source_text},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    event_result = result["data"]["events"][0]
    sent_message = calls[0]["body"]["messages"][0]["text"]
    user_content = captured["request"]["message"]["content"]
    runtime_content = captured["request"]["message"]["metadata"]["external"]["runtime_content"]
    assert event_result["status"] == "ok"
    assert event_result["acknowledgement"]["sent"] is True
    assert sent_message == "\u5c4a\u3044\u305f\u3088\uff01"
    assert user_content == source_text
    assert source_text in runtime_content
    assert "\u7e3a" not in user_content
    assert chat_url not in user_content
    assert chat_url in runtime_content
    assert captured["context"]["computer_use_target_app"] == "Google Chrome"
    assert captured["context"]["computer_use_target_title"] == "LINE Chat"


def test_line_runtime_prompt_is_hidden_from_stored_user_message(monkeypatch, tmp_path):
    from domain.chat.run_request import prepare_chat_run  # noqa: E402
    from domain.chat.store import ChatStore  # noqa: E402

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default", tags=["integration:line"])
    conversation = store.update_conversation(conversation["id"], {"title": "line Uactor"}) or conversation
    source_text = "\u306a\u3093\u304bYouTube Music\u3067Lofi Girl\u6d41\u3057\u3066\u30fc"
    runtime_content = (
        "Use computer_use in Google Chrome to open https://chat.line.biz/Uaccount/chat/Cchat "
        "and reply in Japanese inside LINE Official Account Manager.\n\n"
        "[External source: line source message]\n"
        + source_text
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {
                "role": "user",
                "content": source_text,
                "metadata": {
                    "source": "external_integration",
                    "external": {
                        "provider": "line",
                        "source_text": source_text,
                        "runtime_content": runtime_content,
                    },
                },
            },
            "tools": ["computer_use", "browser_computer"],
        },
        {"external_chat_history_mode": "current_turn"},
    )

    assert prepared.user_message["raw_text"] == source_text
    assert prepared.user_message["content"] == [{"type": "text", "text": source_text}]
    assert "Use computer_use in Google Chrome" not in prepared.user_message["raw_text"]
    assert prepared.standard_messages[-1]["content"] == runtime_content


def test_line_computer_use_natural_message_invokes_line_biz_send_tools(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402
    from domain.chat.store import ChatStore  # noqa: E402

    chat_url = "https://chat.line.biz/Uaccount/chat/Cchat"
    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        input_profile_id="line.computer_use",
        conversation={"strategy": "external_key", "model": "stub/default"},
        response={
            "mode": "computer_use_line_biz",
            "line_biz_chat_url": chat_url,
            "auto_approve_computer_use": True,
        },
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_STORE_PATH", str(tmp_path / "integrations" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_LOCKS_DIR", str(tmp_path / "integrations" / "event_locks"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "line-access-token")
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})

    line_calls: list[dict[str, Any]] = []
    ai_messages: list[list[dict[str, Any]]] = []
    tool_invocations: list[dict[str, Any]] = []
    source_text = "\u306a\u3093\u304bYouTube Music\u3067Lofi Girl\u6d41\u3057\u3066\u30fc"

    def call_handler(name, payload):
        if name == "defaults.ai.complete":
            ai_messages.append(payload["messages"])
            if len(ai_messages) == 1:
                return {
                    "status": "ok",
                    "data": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "type-reply",
                                "name": "browser_computer",
                                "input": {
                                    "action": "computer.type",
                                    "payload": {"text": "Lofi Girl\u3092\u6d41\u3057\u307e\u3059\u306d\u3002"},
                                },
                            }
                        ],
                        "finish_reason": "tool_use",
                    },
                }
            if len(ai_messages) == 2:
                return {
                    "status": "ok",
                    "data": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "click-send",
                                "name": "browser_computer",
                                "input": {
                                    "action": "computer.click",
                                    "payload": {"normalized_x": 512, "normalized_y": 930, "physical": True},
                                },
                            }
                        ],
                        "finish_reason": "tool_use",
                    },
                }
            return {
                "status": "ok",
                "data": {
                    "content": [{"type": "text", "text": "LINE Biz reply sent"}],
                    "finish_reason": "stop",
                },
            }
        if name == "defaults.tool.invoke":
            tool_invocations.append(payload)
            return {"status": "ok", "data": {"result": "ok", "is_error": False}}
        raise AssertionError(name)

    monkeypatch.setattr(
        line_adapter_module,
        "post_json",
        lambda url, headers, body: line_calls.append({"url": url, "headers": headers, "body": body}) or {"ok": True},
    )
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-line-biz-send",
                "replyToken": "reply-line-biz-send",
                "source": {"type": "user", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": source_text},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {"call_handler": call_handler})

    event_result = result["data"]["events"][0]
    conversation = ChatStore().get_conversation(event_result["conversation_id"])
    user_message = next(message for message in conversation["messages"] if message["role"] == "user")
    first_model_prompt = json.dumps(ai_messages[0], ensure_ascii=False)
    assert event_result["status"] == "ok"
    assert line_calls[0]["body"]["messages"][0]["text"] == "\u5c4a\u3044\u305f\u3088\uff01"
    assert user_message["raw_text"] == source_text
    assert "Use computer_use" not in user_message["raw_text"]
    assert source_text in first_model_prompt
    assert chat_url in first_model_prompt
    assert any(
        call["tool_name"] == "browser_computer"
        and call["arguments"]["action"] == "computer.type"
        and "Lofi Girl" in call["arguments"]["payload"]["text"]
        for call in tool_invocations
    )
    assert any(
        call["tool_name"] == "browser_computer"
        and call["arguments"]["action"] == "computer.click"
        and call["arguments"]["payload"].get("physical") is True
        for call in tool_invocations
    )


def test_line_computer_use_fake_webhook_runs_three_browser_tasks_and_acknowledges(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402
    from blocks.chat import send as chat_send  # noqa: E402

    chat_url = "https://chat.line.biz/Uaccount/chat/Cchat"
    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        input_profile_id="line.computer_use",
        conversation={
            "strategy": "external_key",
            "model": "gitlawb-opengateway/mimo-v2-omni",
        },
        response={
            "mode": "computer_use_line_biz",
            "line_biz_chat_url": chat_url,
            "auto_approve_computer_use": True,
            "background_processing": True,
        },
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_STORE_PATH", str(tmp_path / "integrations" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_LOCKS_DIR", str(tmp_path / "integrations" / "event_locks"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "line-access-token")
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})

    captured_invocations: list[tuple[dict[str, Any], dict[str, Any]]] = []
    captured_lock = threading.Lock()
    completed = threading.Event()

    def fake_send_run(request, context):
        with captured_lock:
            captured_invocations.append((request, context))
            if len(captured_invocations) >= 3:
                completed.set()
        return {
            "status": "ok",
            "data": {
                "id": f"assistant-{len(captured_invocations)}",
                "content": [{"type": "text", "text": "LINE Biz reply sent"}],
            },
        }

    line_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(chat_send, "run", fake_send_run)
    monkeypatch.setattr(
        line_adapter_module,
        "post_json",
        lambda url, headers, body: line_calls.append({"url": url, "headers": headers, "body": body}) or {"ok": True},
    )
    tasks = [
        "\u306a\u3093\u304bYouTube Music\u3067Lofi Girl\u6d41\u3057\u3066\u30fc",
        "Gemini\u3067\u3061\u3087\u3063\u3068\u96d1\u8ac7\u3057\u3066",
        "X\u3061\u3087\u3063\u3068\u898b\u3066\u30a4\u30f3\u30bf\u30fc\u30cd\u30c3\u30c8\u89e6\u3063\u3066",
    ]

    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": f"evt-three-tasks-{index}",
                "replyToken": f"reply-three-tasks-{index}",
                "source": {"type": "user", "userId": "Uactor"},
                "message": {"id": f"m-{index}", "type": "text", "text": task},
            }
            for index, task in enumerate(tasks, start=1)
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    assert result["status"] == "ok"
    assert [event["status"] for event in result["data"]["events"]] == ["accepted", "accepted", "accepted"]
    assert [event["acknowledgement"]["text"] for event in result["data"]["events"]] == ["\u5c4a\u3044\u305f\u3088\uff01"] * 3
    assert completed.wait(timeout=2)
    assert len(captured_invocations) == 3
    assert len(line_calls) == 3
    assert [call["body"]["messages"][0]["text"] for call in line_calls] == ["\u5c4a\u3044\u305f\u3088\uff01"] * 3
    captured_contents = [request["message"]["content"] for request, _context in captured_invocations]
    assert sorted(captured_contents) == sorted(tasks)
    for request, context in captured_invocations:
        user_content = request["message"]["content"]
        runtime_content = request["message"]["metadata"]["external"]["runtime_content"]
        assert user_content in tasks
        assert chat_url not in user_content
        assert "Use browser_computer only" not in user_content
        assert "Open this URL directly" not in user_content
        assert "Then reply with exactly: Complete" not in user_content
        assert user_content in runtime_content
        assert chat_url in runtime_content
        assert request["params"]["retry"]["max_attempts"] == 5
        assert request["params"]["retry"]["delays"] == [5, 15, 30, 60]
        assert request["params"]["thinking_level"] == "high"
        assert request["tools"] == ["computer_use", "browser_computer", "web_search"]
        assert context["computer_use_target_app"] == "Google Chrome"
        assert context["computer_use_target_title"] == "LINE Chat"
        assert context["line_background_processing"] is True
        assert context["profile_policy"]["max_tool_calls"] == 30
        assert "yolo_mode" not in context["profile_policy"]


def test_line_route_does_not_acknowledge_normal_line_reply_mode(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, metadata={"require_group_mention": False})
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "line-access-token")
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    calls: list[dict[str, Any]] = []

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        return {
            "status": "ok",
            "assistant_text": "normal reply",
            "response_plan": {"provider": "line", "messages": [{"type": "text", "text": "normal reply"}]},
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(
        line_adapter_module,
        "post_json",
        lambda url, headers, body: calls.append({"url": url, "headers": headers, "body": body}) or {"ok": True},
    )
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-normal",
                "replyToken": "reply-normal",
                "source": {"type": "user", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    event_result = result["data"]["events"][0]
    assert event_result["acknowledgement"]["sent"] is False
    assert event_result["reply"]["sent"] is True
    assert calls == [
        {
            "url": "https://api.line.me/v2/bot/message/reply",
            "headers": {"Authorization": "Bearer line-access-token"},
            "body": {"replyToken": "reply-normal", "messages": [{"type": "text", "text": "normal reply"}]},
        }
    ]


def test_line_route_preserves_top_level_destination_and_endpoint_policy(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, metadata={"require_group_mention": False})
    _remember_line_group(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision=None, context, send_response, mentioned=False):
        captured["event"] = event
        captured["input_profile_id"] = input_profile_id
        captured["audience_policy"] = audience_policy
        captured["audience_decision"] = audience_decision
        captured["context"] = context
        captured["send_response"] = send_response
        captured["mentioned"] = mentioned
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {"provider": "line", "messages": [{"type": "text", "text": "done"}]},
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": True})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-1",
                "replyToken": "reply-1",
                "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }
    raw_body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    result = line_block.run(_signed_line_payload(payload, raw_body=raw_body), {})

    assert result["status"] == "ok"
    assert captured["event"].workspace.id == "Udestination"
    assert captured["input_profile_id"] == "line.default"
    assert captured["context"]["webhook_endpoint"]["id"] == "line-main"
    assert captured["context"]["output_profile_id"] == "line.default"
    assert captured["audience_decision"].allowed is True
    assert captured["audience_policy"]["require"] == {"verified": True, "message_types": ["text"]}
    assert captured["audience_policy"]["allow"][0]["scope"] == {"type": "group", "id": "Cgroup"}
    saved = ExternalSourceStore().get("line", "group", "Cgroup")
    assert saved is not None
    assert saved["enabled"] is True
    assert saved["actor_last_seen"] == "Uactor"
    assert saved["allow_push"] is False


def test_line_route_applies_endpoint_response_context(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        response={
            "mode": "computer_use_line_biz",
            "prompt_prefix": "Use computer_use in Google Chrome and reply in LINE Biz.",
            "target_app": "Google Chrome",
            "target_title": "LINE",
            "auto_approve_computer_use": True,
        },
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        captured["input_profile_id"] = input_profile_id
        captured["audience_decision"] = audience_decision
        captured["context"] = context
        captured["mentioned"] = mentioned
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {
                "provider": "line",
                "messages": [],
                "metadata": {"response_action_plan": {"type": "store_only", "external_reply": False}},
            },
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": True})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-1",
                "replyToken": "reply-1",
                "source": {"type": "user", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    assert result["status"] == "ok"
    assert captured["input_profile_id"] == "line.default"
    assert captured["context"]["external_prompt_prefix"] == "Use computer_use in Google Chrome and reply in LINE Biz."
    assert captured["context"]["computer_use_target_app"] == "Google Chrome"
    assert captured["context"]["computer_use_target_title"] == "LINE"
    assert "yolo_mode" not in captured["context"].get("profile_policy", {})
    assert captured["context"]["response_prompt_decision"]["action"] == "store_only"
    assert captured["context"]["response_prompt_decision"]["sensitivity"] == "local_only"


def test_line_route_builds_line_biz_prompt_from_chat_url(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    chat_url = "https://chat.line.biz/U3615315e56cd0c7fd8dc296f60b6f149/chat/Ca3b2e13a28d5459f0b82057b6cd6033b"
    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        response={
            "mode": "computer_use_line_biz",
            "line_biz_chat_url": chat_url,
        },
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        captured["context"] = context
        captured["audience_decision"] = audience_decision
        captured["mentioned"] = mentioned
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {
                "provider": "line",
                "messages": [],
                "metadata": {"response_action_plan": {"type": "store_only", "external_reply": False}},
            },
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": True})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-1",
                "replyToken": "reply-1",
                "source": {"type": "user", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    assert result["status"] == "ok"
    assert chat_url in captured["context"]["external_prompt_prefix"]
    assert "LINE Official Account Manager" in captured["context"]["external_prompt_prefix"]
    assert "computer.windows" in captured["context"]["external_prompt_prefix"]
    assert "computer.select_window" in captured["context"]["external_prompt_prefix"]
    assert "computer.context" in captured["context"]["external_prompt_prefix"]
    assert "active_window" in captured["context"]["external_prompt_prefix"]
    assert "visible desktop Chrome window" in captured["context"]["external_prompt_prefix"]
    assert "external source message below is already the customer message" in captured["context"]["external_prompt_prefix"]
    assert "Treat the visible LINE Biz chat history only as the destination UI" in captured["context"]["external_prompt_prefix"]
    assert "reply exactly with some text" in captured["context"]["external_prompt_prefix"]
    assert "large red circular reply button" in captured["context"]["external_prompt_prefix"]
    assert "physical=true" in captured["context"]["external_prompt_prefix"]
    assert "will not open the composer or press Send" in captured["context"]["external_prompt_prefix"]
    assert "Do not use Ctrl+A" in captured["context"]["external_prompt_prefix"]
    assert "do not type it again" in captured["context"]["external_prompt_prefix"]
    assert "not the small dropdown arrow" in captured["context"]["external_prompt_prefix"]
    assert "Do not keep scrolling through the transcript repeatedly" in captured["context"]["external_prompt_prefix"]
    assert captured["context"]["computer_use_target_app"] == "Google Chrome"
    assert captured["context"]["computer_use_target_title"] == "LINE Chat"
    assert captured["context"]["computer_use_physical_clicks"] is True
    assert captured["context"]["computer_use_reply_surface"] == "line_biz"
    assert captured["context"]["user_requested_computer_use"] is True
    assert captured["context"]["external_chat_history_mode"] == "current_turn"
    assert captured["context"]["response_prompt_decision"]["action"] == "store_only"


def test_line_biz_context_defaults_clicks_to_physical():
    from domain.tool.executor import _computer_use_payload_with_context_defaults  # noqa: E402

    payload = _computer_use_payload_with_context_defaults(
        "computer.click",
        {"x": 100, "y": 200},
        {
            "computer_use_target_app": "Google Chrome",
            "computer_use_target_title": "LINE",
            "computer_use_physical_clicks": True,
        },
    )

    assert payload["app"] == "Google Chrome"
    assert payload["title"] == "LINE"
    assert payload["physical"] is True


def test_line_biz_context_preserves_explicit_virtual_click():
    from domain.tool.executor import _computer_use_payload_with_context_defaults  # noqa: E402

    payload = _computer_use_payload_with_context_defaults(
        "computer.click",
        {"x": 100, "y": 200, "physical": False},
        {
            "computer_use_target_app": "Google Chrome",
            "computer_use_target_title": "LINE",
            "computer_use_physical_clicks": True,
        },
    )

    assert payload["physical"] is False


def test_line_computer_use_background_processing_is_opt_in(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        input_profile_id="line.computer_use",
        conversation={"strategy": "external_key", "model": "google/gemma-4-31b-it"},
        response={
            "mode": "computer_use_line_biz",
            "background_processing": True,
            "line_biz_chat_url": "https://chat.line.biz/Uaccount/chat/Cchat",
        },
    )
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    captured: dict[str, Any] = {}

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        started.set()
        release.wait(timeout=5)
        captured["input_profile_id"] = input_profile_id
        captured["context"] = context
        captured["send_response"] = send_response
        finished.set()
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {
                "provider": "line",
                "messages": [],
                "metadata": {"response_action_plan": {"type": "store_only", "external_reply": False}},
            },
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": False})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-bg",
                "replyToken": "reply-1",
                "source": {"type": "user", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    start = time.monotonic()
    result = line_block.run(_signed_line_payload(payload), {})
    elapsed = time.monotonic() - start

    event_result = result["data"]["events"][0]
    assert elapsed < 1
    assert event_result["status"] == "accepted"
    assert event_result["background_processing"] is True
    assert event_result["event_id"] == "evt-bg"
    assert event_result["reply"] == {"sent": False, "reason": "LINE event accepted for background processing"}
    assert started.wait(timeout=2)
    release.set()
    assert finished.wait(timeout=2)
    assert captured["input_profile_id"] == "line.computer_use"
    assert captured["send_response"] is True
    assert captured["context"]["line_background_processing"] is True
    assert captured["context"]["user_requested_computer_use"] is True


def test_line_background_processing_flag_does_not_affect_normal_line_mode(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        response={"mode": "same_response", "background_processing": True},
    )
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    captured: dict[str, Any] = {}

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        captured["called"] = True
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {"provider": "line", "messages": [{"type": "text", "text": "done"}]},
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": True})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-sync",
                "replyToken": "reply-1",
                "source": {"type": "user", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    assert captured["called"] is True
    assert result["data"]["events"][0]["status"] == "ok"
    assert "background_processing" not in result["data"]["events"][0]


def test_line_computer_use_group_message_ignores_unaddressed_text(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        input_profile_id="line.computer_use",
        conversation={"strategy": "external_key", "model": "google/gemma-4-31b-it"},
        response={"mode": "computer_use_line_biz"},
    )
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": False})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-group-1",
                "replyToken": "reply-1",
                "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    event_result = result["data"]["events"][0]
    assert event_result["status"] == "ignored"
    assert event_result["reason"] == "LINE group/room message was not addressed to Rumi"
    assert event_result["line_addressing"]["addressed"] is False
    assert event_result["event"]["metadata"]["line_mention"]["mentioned"] is False
    assert event_result["event"]["metadata"]["line_mention"]["addressed"] is False
    assert event_result["event"]["metadata"]["line_mention"]["require_group_mention"] is True


def test_line_default_group_message_ignores_unaddressed_text(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, input_profile_id="line.default")
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    monkeypatch.setattr(
        line_block,
        "dispatch_external_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run without mention")),
    )
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-default-group",
                "replyToken": "reply-default-group",
                "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    event_result = result["data"]["events"][0]
    assert event_result["status"] == "ignored"
    assert event_result["line_addressing"]["addressed"] is False
    assert event_result["event"]["metadata"]["line_mention"]["addressed"] is False
    assert event_result["event"]["metadata"]["line_mention"]["require_group_mention"] is True


def test_line_default_group_message_ignores_plain_rumi_when_trigger_not_configured(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, input_profile_id="line.default")
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    monkeypatch.setattr(
        line_block,
        "dispatch_external_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run without trigger")),
    )
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-default-group-rumi-name",
                "replyToken": "reply-default-group-rumi-name",
                "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "rumi, \u4eca\u65e5\u306e\u8981\u7d04\u3057\u3066"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    event_result = result["data"]["events"][0]
    assert event_result["status"] == "ignored"
    assert event_result["line_addressing"]["addressed"] is False
    assert event_result["line_addressing"]["signals"] == []
    assert event_result["event"]["metadata"]["line_mention"]["mentioned"] is False
    assert event_result["event"]["metadata"]["line_mention"]["addressed"] is False


def test_line_default_group_message_dispatches_for_hash_trigger(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, input_profile_id="line.default")
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    captured: dict[str, Any] = {}

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        captured["event"] = event
        captured["mentioned"] = mentioned
        captured["context"] = context
        captured["audience_decision"] = audience_decision
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {"provider": "line", "messages": []},
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": False})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-default-group-trigger",
                "replyToken": "reply-default-group-trigger",
                "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "# \u4eca\u65e5\u306e\u8981\u7d04\u3057\u3066"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    assert result["data"]["events"][0]["status"] == "ok"
    assert captured["mentioned"] is True
    assert captured["audience_decision"].allowed is True
    assert captured["event"].metadata["line_addressing"]["addressed"] is True
    assert captured["event"].metadata["line_addressing"]["signals"] == ["trigger_word"]
    assert captured["event"].metadata["line_addressing"]["trigger"] == "#"
    assert captured["event"].metadata["line_mention"]["mentioned"] is False
    assert captured["event"].metadata["line_mention"]["addressed"] is True
    assert captured["context"]["line_reply_deadline_seconds"] == 60
    assert "LINE reply tokens expire about 1 minute" in captured["context"]["external_prompt_suffix"]


def test_line_default_group_message_dispatches_for_multiword_trigger_case_insensitively(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        input_profile_id="line.default",
        metadata={"line_trigger_words": ["rumi bot"]},
    )
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    captured: dict[str, Any] = {}

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        captured["mentioned"] = mentioned
        captured["event"] = event
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {"provider": "line", "messages": []},
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": False})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-default-group-trigger-multiword",
                "replyToken": "reply-default-group-trigger-multiword",
                "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "RUMI BOT summarize this"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    assert result["data"]["events"][0]["status"] == "ok"
    assert captured["mentioned"] is True
    assert captured["event"].metadata["line_addressing"]["addressed"] is True
    assert captured["event"].metadata["line_addressing"]["trigger"] == "rumi bot"
    assert captured["event"].metadata["line_mention"]["addressed"] is True


def test_line_group_message_ignores_recent_rumi_context_without_trigger(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, input_profile_id="line.default")
    _seed_line_group_conversation(monkeypatch, tmp_path, assistant_text="Which option should I use?")
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    monkeypatch.setattr(
        line_block,
        "dispatch_external_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run without trigger")),
    )
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": False})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-default-group-followup",
                "replyToken": "reply-default-group-followup",
                "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "option A please"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    event_result = result["data"]["events"][0]
    assert event_result["status"] == "ignored"
    assert event_result["line_addressing"]["addressed"] is False
    assert event_result["line_addressing"]["signals"] == []


def test_line_direct_user_message_still_dispatches_without_mention(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, input_profile_id="line.default")
    monkeypatch.setattr(line_block.AudiencePolicyRegistry, "resolve", lambda self, policy_id, event=None: {"default": "allow"})
    captured: dict[str, Any] = {}

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        captured["called"] = True
        captured["mentioned"] = mentioned
        captured["event"] = event
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {"provider": "line", "messages": []},
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": False})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-user-no-mention",
                "replyToken": "reply-user-no-mention",
                "source": {"type": "user", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    assert result["data"]["events"][0]["status"] == "ok"
    assert captured["called"] is True
    assert captured["mentioned"] is False
    assert captured["event"].metadata["line_mention"]["require_group_mention"] is False


def test_line_computer_use_group_message_dispatches_when_saved_group_mentions_bot(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        input_profile_id="line.computer_use",
        conversation={"strategy": "external_key", "model": "google/gemma-4-31b-it"},
        response={"mode": "computer_use_line_biz"},
    )
    _remember_line_group(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        captured["event"] = event
        captured["audience_policy"] = audience_policy
        captured["audience_decision"] = audience_decision
        captured["mentioned"] = mentioned
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {
                "provider": "line",
                "messages": [],
                "metadata": {"response_action_plan": {"type": "store_only", "external_reply": False}},
            },
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(LineResponseAdapter, "send", lambda self, plan, event=None, context=None: {"sent": False})
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-group-mention",
                "replyToken": "reply-1",
                "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
                "message": {
                    "id": "m1",
                    "type": "text",
                    "text": "@bot hello",
                    "mention": {
                        "mentionees": [
                            {
                                "index": 0,
                                "length": 4,
                                "type": "user",
                                "userId": "Udestination",
                                "isSelf": True,
                            }
                        ]
                    },
                },
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    assert result["data"]["events"][0]["status"] == "ok"
    assert captured["mentioned"] is True
    assert captured["audience_policy"]["require"]["mention"] is True
    assert captured["event"].metadata["line_mention"]["mentioned"] is True
    assert captured["event"].metadata["line_mention"]["require_group_mention"] is True


def test_line_computer_use_group_mention_from_unknown_source_is_denied(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(
        monkeypatch,
        tmp_path,
        enabled=True,
        input_profile_id="line.computer_use",
        conversation={"strategy": "external_key", "model": "google/gemma-4-31b-it"},
        response={"mode": "computer_use_line_biz"},
    )

    def fail_dispatch(*args, **kwargs):
        raise AssertionError("dispatch_external_event should not run for denied source")

    monkeypatch.setattr(line_block, "dispatch_external_event", fail_dispatch)

    def fail_send(*args, **kwargs):
        raise AssertionError("reply should not send for denied source")

    monkeypatch.setattr(LineResponseAdapter, "send", fail_send)
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-unknown-mention",
                "replyToken": "reply-unknown-mention",
                "source": {"type": "group", "groupId": "Cunknown", "userId": "Uactor"},
                "message": {
                    "id": "m1",
                    "type": "text",
                    "text": "@bot hello",
                    "mention": {
                        "mentionees": [
                            {
                                "index": 0,
                                "length": 4,
                                "type": "user",
                                "userId": "Udestination",
                                "isSelf": True,
                            }
                        ]
                    },
                },
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    denied = result["data"]["events"][0]
    assert denied["status"] == "denied"
    assert denied["policy"]["reason"] == "default deny"
    assert denied["event"]["metadata"]["line_mention"] == {
        "mentioned": True,
        "require_group_mention": True,
        "addressed": True,
    }
    saved = ExternalSourceStore().get("line", "group", "Cunknown")
    assert saved is not None
    assert saved["enabled"] is False


def test_line_route_empty_events_ack_ok_without_dispatch(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, metadata={"require_group_mention": False})
    monkeypatch.setattr(
        line_block,
        "dispatch_external_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run")),
    )

    result = line_block.run(_signed_line_payload({"destination": "Udest", "events": []}), {})

    assert result["status"] == "ok"
    assert result["data"]["events"] == []


def test_line_route_processes_signed_raw_payload_only(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, metadata={"require_group_mention": False})
    monkeypatch.setattr(
        line_block,
        "dispatch_external_event",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unsigned parsed payload should not dispatch")),
    )
    parsed_payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "source": {"type": "group", "groupId": "Cinjected", "userId": "Uactor"},
                "message": {"id": "m-injected", "type": "text", "text": "hello"},
            }
        ],
    }
    signed_raw = json.dumps({"destination": "Udestination", "events": []}, separators=(",", ":")).encode("utf-8")

    result = line_block.run(_signed_line_payload(parsed_payload, raw_body=signed_raw), {})

    assert result["status"] == "ok"
    assert result["data"]["events"] == []
    assert ExternalSourceStore().get("line", "group", "Cinjected") is None


def test_line_route_unknown_verified_source_is_saved_disabled_and_denied(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, metadata={"require_group_mention": False})

    def fail_dispatch(*args, **kwargs):
        raise AssertionError("dispatch_external_event should not run for denied source")

    def fail_send(*args, **kwargs):
        raise AssertionError("LINE reply should not be sent for denied source")

    monkeypatch.setattr(line_block, "dispatch_external_event", fail_dispatch)
    monkeypatch.setattr(LineResponseAdapter, "send", fail_send)
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-unknown",
                "replyToken": "reply-unknown",
                "source": {"type": "group", "groupId": "Cunknown", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    assert result["status"] == "ok"
    denied = result["data"]["events"][0]
    assert denied["status"] == "denied"
    assert denied["policy"]["reason"] == "default deny"
    assert denied["reply"] == {"sent": False, "reason": "audience policy denied"}
    saved = ExternalSourceStore().get("line", "group", "Cunknown")
    assert saved is not None
    assert saved["enabled"] is False
    assert saved["allow_push"] is False
    assert saved["verified_last_seen"] is True


def test_line_route_frontend_push_to_saved_origin_reaches_adapter(monkeypatch, tmp_path):
    from blocks.integrations import line as line_block  # noqa: E402

    _install_line_endpoint(monkeypatch, tmp_path, enabled=True, metadata={"require_group_mention": False})
    _remember_line_group(monkeypatch, tmp_path, group_id="Cgroup", allow_push=True)
    settings_path = tmp_path / "frontend_settings.json"
    settings_path.write_text(
        json.dumps({"external_output": {"output_send_mode": "push_to_saved_origin"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(settings_path))
    calls: list[dict[str, Any]] = []

    def fake_dispatch(event, *, input_profile_id, audience_policy, audience_decision, context, send_response, mentioned=False):
        assert audience_decision.allowed is True
        assert context["send_mode"] == "push_to_saved_origin"
        return {
            "status": "ok",
            "assistant_text": "done",
            "response_plan": {"provider": "line", "messages": [{"type": "text", "text": "done"}]},
        }

    monkeypatch.setattr(line_block, "dispatch_external_event", fake_dispatch)
    monkeypatch.setattr(line_adapter_module, "read_external_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(
        line_adapter_module,
        "post_json",
        lambda url, headers, body: calls.append({"url": url, "body": body}) or {"ok": True},
    )
    payload = {
        "destination": "Udestination",
        "events": [
            {
                "type": "message",
                "mode": "active",
                "webhookEventId": "evt-push",
                "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
                "message": {"id": "m1", "type": "text", "text": "hello"},
            }
        ],
    }

    result = line_block.run(_signed_line_payload(payload), {})

    assert result["status"] == "ok"
    assert result["data"]["events"][0]["reply"]["sent"] is True
    assert calls[0]["url"].endswith("/message/push")
    assert calls[0]["body"] == {"to": "Cgroup", "messages": [{"type": "text", "text": "done"}]}


def test_external_sources_api_toggles_enabled_and_push(monkeypatch, tmp_path):
    from blocks.external import sources as sources_block  # noqa: E402

    monkeypatch.setenv("RUMI_DEFAULTSPACK_EXTERNAL_SOURCES_PATH", str(tmp_path / "external_sources.json"))
    _remember_line_group(monkeypatch, tmp_path, group_id="Cmanaged")

    result = sources_block.run(
        {
            "_method": "POST",
            "key": "line:group:Cmanaged",
            "enabled": True,
            "allow_push": True,
            "label": "Managed LINE group",
        },
        {},
    )

    assert result["status"] == "ok"
    source = result["data"]["source"]
    assert source["enabled"] is True
    assert source["allow_push"] is True
    assert source["label"] == "Managed LINE group"
    listed = sources_block.run({"_method": "GET"}, {})
    assert listed["data"]["sources"][0]["source_id"] == "Cmanaged"


def test_line_default_policy_denies_unknown_source(monkeypatch, tmp_path):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_EXTERNAL_SOURCES_PATH", str(tmp_path / "external_sources.json"))
    event = normalize_line_event(
        {
            "type": "message",
            "source": {"type": "group", "groupId": "Cunknown", "userId": "Uactor"},
            "message": {"id": "m1", "type": "text", "text": "hello"},
        },
        verified=True,
        destination="Udest",
    )

    policy = AudiencePolicyRegistry().resolve("line.production", event=event)
    decision = AudiencePolicy(policy).evaluate(event)

    assert decision.allowed is False
    assert decision.reason == "default deny"


def test_line_adapter_replies_to_group_origin_without_user_push(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(line_adapter_module, "read_external_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(
        line_adapter_module,
        "post_json",
        lambda url, headers, body: calls.append({"url": url, "body": body}) or {"ok": True},
    )
    event = normalize_line_event(
        {
            "type": "message",
            "replyToken": "reply-group",
            "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
            "message": {"id": "m1", "type": "text", "text": "hello"},
        },
        verified=True,
    )

    result = LineResponseAdapter().send({"messages": [{"type": "text", "text": "reply"}]}, event=event)

    assert result["sent"] is True
    assert calls[0]["url"].endswith("/message/reply")
    assert calls[0]["body"] == {"replyToken": "reply-group", "messages": [{"type": "text", "text": "reply"}]}


def test_line_adapter_missing_reply_token_does_not_push_actor_user(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(line_adapter_module, "read_external_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(line_adapter_module, "post_json", lambda url, headers, body: calls.append(body) or {"ok": True})
    event = normalize_line_event(
        {
            "type": "message",
            "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
            "message": {"id": "m1", "type": "text", "text": "hello"},
        },
        verified=True,
    )

    result = LineResponseAdapter().send({"messages": [{"type": "text", "text": "reply"}]}, event=event)

    assert result["sent"] is False
    assert result["reason"] == "missing reply token"
    assert calls == []


def test_line_adapter_push_to_origin_uses_group_and_room_ids(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(line_adapter_module, "read_external_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(
        line_adapter_module,
        "post_json",
        lambda url, headers, body: calls.append({"url": url, "body": body}) or {"ok": True},
    )
    group_event = normalize_line_event(
        {
            "type": "message",
            "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
            "message": {"id": "m1", "type": "text", "text": "hello"},
        },
        verified=True,
    )
    room_event = normalize_line_event(
        {
            "type": "message",
            "source": {"type": "room", "roomId": "Rroom", "userId": "Uactor"},
            "message": {"id": "m2", "type": "text", "text": "hello"},
        },
        verified=True,
    )

    LineResponseAdapter().send({"messages": [{"type": "text", "text": "group"}]}, event=group_event, context={"send_mode": "push_to_origin", "allow_push": True})
    LineResponseAdapter().send({"messages": [{"type": "text", "text": "room"}]}, event=room_event, context={"send_mode": "push_to_origin", "allow_push": True})

    assert calls[0]["url"].endswith("/message/push")
    assert calls[0]["body"]["to"] == "Cgroup"
    assert calls[1]["body"]["to"] == "Rroom"


def test_line_adapter_standby_does_not_push_by_default(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(line_adapter_module, "read_external_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(line_adapter_module, "post_json", lambda url, headers, body: calls.append(body) or {"ok": True})
    event = normalize_line_event(
        {
            "type": "message",
            "mode": "standby",
            "source": {"type": "group", "groupId": "Cgroup", "userId": "Uactor"},
            "message": {"id": "m1", "type": "text", "text": "hello"},
        },
        verified=True,
    )

    result = LineResponseAdapter().send(
        {"messages": [{"type": "text", "text": "reply"}]},
        event=event,
        context={"send_mode": "push_to_origin", "allow_push": True},
    )

    assert result["sent"] is False
    assert result["reason"] == "push not allowed"
    assert calls == []


def test_line_reply_sends_max_five_messages_without_joining(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(line_adapter_module, "read_external_token", lambda *args, **kwargs: "token")
    monkeypatch.setattr(line_adapter_module, "post_json", lambda url, headers, body: calls.append(body) or {"ok": True})
    event = normalize_line_event(
        {
            "type": "message",
            "replyToken": "reply-user",
            "source": {"type": "user", "userId": "Uuser"},
            "message": {"id": "m1", "type": "text", "text": "hello"},
        },
        verified=True,
    )
    plan = {"messages": [{"type": "text", "text": f"part-{index}"} for index in range(7)]}

    LineResponseAdapter().send(plan, event=event)

    assert [item["text"] for item in calls[0]["messages"]] == ["part-0", "part-1", "part-2", "part-3", "part-4"]


def test_line_planner_limits_messages_to_five():
    plan = ResponsePlanner("line").plan(RumiResponse(text="x" * 26000))

    assert len(plan["messages"]) == 5
    assert plan["fallbacks"][0]["reason"] == "text message count limit exceeded"


def test_line_image_message_marked_as_unsupported_attachment():
    event = normalize_line_event(
        {
            "type": "message",
            "source": {"type": "user", "userId": "Uuser"},
            "message": {"id": "m-image", "type": "image"},
        },
        verified=True,
    )

    assert event.metadata["attachments"] == [
        {
            "provider": "line",
            "message_id": "m-image",
            "message_type": "image",
            "content_api": "https://api-data.line.me/v2/bot/message/m-image/content",
            "retrieval": "line_content_api",
            "status": "unsupported_inline",
        }
    ]
