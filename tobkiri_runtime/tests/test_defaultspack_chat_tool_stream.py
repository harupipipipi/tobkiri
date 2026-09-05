from __future__ import annotations

import sys
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures(
    "defaultspack_conversation_owner", "defaultspack_v4_tool_dispatch"
)


def test_chat_run_engine_streams_tool_call_events_and_final_message(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.tool.executor import ToolExecutor

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools=None, params=None):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "tool_call_start", "id": "call_1", "name": "calculator"}
                yield {"type": "tool_call_delta", "id": "call_1", "name": "calculator", "arguments_chunk": "{\"expression\":\"2+2\"}"}
                yield {"type": "tool_call_end", "id": "call_1", "name": "calculator"}
                yield {"type": "stream_end", "finish_reason": "tool_calls", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                return
            yield {"type": "content_delta", "delta": {"type": "text", "text": "4"}}
            yield {"type": "stream_end", "finish_reason": "stop", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}

        def complete(self, model, messages, tools=None, params=None):
            return {
                "content": [{"type": "text", "text": "4"}],
                "finish_reason": "stop",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

    def fake_execute(self, tool_name, arguments, context):
        return {
            "result": "4",
            "is_error": False,
            "widget": {"type": tool_name, "result": "4"},
        }

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    monkeypatch.setattr(ChatRunEngine, "_provider_supports_stream_tool_calls", staticmethod(lambda _model: True))

    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.5")
    events = list(
        ChatRunEngine(client=FakeClient()).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "use a tool"},
                "tools": [{"kind": "tool", "id": "calculator"}],
                "params": {
                    "tool_selection": {
                        "mode": "manual",
                        "include": ["calculator"],
                    }
                },
            },
            {"principal_capabilities": ["developer"]},
            stream_mode=True,
        )
    )

    event_types = [event["type"] for event in events]
    streamed_run_ids = {
        event["run_id"]
        for event in events
        if event["type"] in {"content_delta", "tool_call_started", "tool_call_delta", "tool_call_completed", "done"}
    }
    assert "tool_call_started" in event_types
    assert "tool_call_delta" in event_types
    assert "tool_call_completed" in event_types
    assert len(streamed_run_ids) == 1
    started = [event for event in events if event["type"] == "tool_call_started"][0]
    completed = [event for event in events if event["type"] == "tool_call_completed"][0]
    assert started["data"]["status"] == "running"
    assert started["data"]["group"] == {"id": "calculation", "label": "計算"}
    assert "display_text" in started["data"]
    assert completed["data"]["status"] == "completed"
    assert completed["data"]["next_step"] == "結果をもとに次の応答へ進みます。"
    final_message = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    assert final_message["raw_text"] == "4"
    ChatStore._instance = None


def test_chat_run_engine_provider_tool_stream_support_uses_injected_client(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.tool.executor import ToolExecutor

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    class GoogleProvider:
        pass

    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.resolve_provider_calls = 0

        def resolve_provider(self, model):
            self.resolve_provider_calls += 1
            return GoogleProvider(), "gemini-test"

        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools=None, params=None):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "tool_call_start", "id": "call_1", "name": "calculator"}
                yield {"type": "tool_call_delta", "id": "call_1", "name": "calculator", "arguments_chunk": "{\"expression\":\"2+2\"}"}
                yield {"type": "tool_call_end", "id": "call_1", "name": "calculator"}
                yield {"type": "stream_end", "finish_reason": "tool_calls"}
                return
            yield {"type": "content_delta", "delta": {"type": "text", "text": "4"}}
            yield {"type": "stream_end", "finish_reason": "stop"}

        def complete(self, model, messages, tools=None, params=None):
            raise AssertionError("complete fallback should not be used when injected client supports streamed tool calls")

    def fake_execute(self, tool_name, arguments, context):
        return {"result": "4", "is_error": False}

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)

    client = FakeClient()
    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.5")
    events = list(
        ChatRunEngine(client=client).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "use a tool"},
                "tools": [{"kind": "tool", "id": "calculator"}],
                "params": {
                    "tool_selection": {
                        "mode": "manual",
                        "include": ["calculator"],
                    }
                },
            },
            {"principal_capabilities": ["developer"]},
            stream_mode=True,
        )
    )

    assert client.resolve_provider_calls >= 1
    assert "tool_call_started" in [event["type"] for event in events]
    assert [event["data"]["message"]["raw_text"] for event in events if event["type"] == "done"][-1] == "4"
    ChatStore._instance = None


def test_stream_with_selected_tools_uses_chat_run_engine_not_legacy_fallback(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.tool.executor import ToolExecutor
    import blocks.chat.stream as stream_module

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools=None, params=None):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "tool_call_start", "id": "call_1", "name": "calculator"}
                yield {"type": "tool_call_delta", "id": "call_1", "name": "calculator", "arguments_chunk": "{\"expression\":\"2+2\"}"}
                yield {"type": "tool_call_end", "id": "call_1", "name": "calculator"}
                yield {"type": "stream_end", "finish_reason": "tool_calls", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
                return
            yield {"type": "content_delta", "delta": {"type": "text", "text": "4"}}
            yield {"type": "stream_end", "finish_reason": "stop", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}

        def complete(self, model, messages, tools=None, params=None):
            return {
                "content": [{"type": "text", "text": "4"}],
                "finish_reason": "stop",
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

    def fake_execute(self, tool_name, arguments, context):
        return {
            "result": "4",
            "is_error": False,
        }

    def fail_legacy_fallback(*_args, **_kwargs):
        raise AssertionError("legacy _fallback_send should not be used for selected tools")

    monkeypatch.setattr(stream_module, "_fallback_send", fail_legacy_fallback)
    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    monkeypatch.setattr(ChatRunEngine, "_provider_supports_stream_tool_calls", staticmethod(lambda _model: True))
    from domain.ai_client.gateway import LLMGateway

    monkeypatch.setattr(
        stream_module,
        "ContractLLMGateway",
        lambda: LLMGateway(client=FakeClient()),
    )

    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.5")
    result = stream_module.run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "use a tool"},
            "tools": [{"kind": "tool", "id": "calculator"}],
            "params": {
                "tool_selection": {
                    "mode": "manual",
                    "include": ["calculator"],
                }
            },
        },
        {"principal_capabilities": ["developer"]},
    )

    events = list(result["events"])
    event_types = [event["type"] for event in events]
    assert event_types.count("tool_call_started") == 1
    assert event_types.count("tool_call_delta") == 1
    assert event_types.count("tool_call_completed") == 1
    assert event_types[-2:] == ["message", "done"]
    ChatStore._instance = None


def test_chat_run_engine_streams_browser_state_events_with_timestamped_tool_result(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.tool.executor import ToolExecutor

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aKkAAAAASUVORK5CYII="
    )

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools=None, params=None):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "tool_call_start", "id": "call_browser_1", "name": "browser_computer"}
                yield {
                    "type": "tool_call_delta",
                    "id": "call_browser_1",
                    "name": "browser_computer",
                    "arguments_chunk": "{\"action\":\"computer.click\"}",
                }
                yield {"type": "tool_call_end", "id": "call_browser_1", "name": "browser_computer"}
                yield {"type": "stream_end", "finish_reason": "tool_calls"}
                return
            yield {"type": "content_delta", "delta": {"type": "text", "text": "clicked"}}
            yield {"type": "stream_end", "finish_reason": "stop"}

        def complete(self, model, messages, tools=None, params=None):
            return {
                "content": [{"type": "text", "text": "clicked"}],
                "finish_reason": "stop",
            }

    def fake_execute(self, tool_name, arguments, context):
        return {
            "result": "browser_computer computer.click completed",
            "is_error": False,
            "widget": {
                "type": "browser_computer",
                "action": "computer.click",
                "executed": True,
                "visual_feedback": {
                    "type": "post_click_screenshot",
                    "screenshot_path": "/tmp/post-click.png",
                    "model_image_path": "/tmp/post-click-model.png",
                    "data_url": png_data_url,
                },
            },
        }

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    monkeypatch.setattr(ChatRunEngine, "_provider_supports_stream_tool_calls", staticmethod(lambda _model: True))

    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.5")
    events = list(
        ChatRunEngine(client=FakeClient()).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "click"},
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "browser_computer",
                            "parameters": {"type": "object", "properties": {}, "required": []},
                        },
                    }
                ],
            },
            {"principal_capabilities": ["developer"]},
            stream_mode=True,
        )
    )

    event_types = [event["type"] for event in events]
    assert "task_failed" not in event_types
    assert "browser_state_invalidated" in event_types
    assert "browser_screenshot" in event_types
    screenshot_event = next(event for event in events if event["type"] == "browser_screenshot")
    assert screenshot_event["data"]["timestamp"]
    assert screenshot_event["data"]["screenshot"]["model_image_path"] == "/tmp/post-click-model.png"
    assert events[-1]["type"] == "done"
    ChatStore._instance = None


def test_chat_run_engine_stops_for_permission_required_tool_result(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.tool.executor import ToolExecutor

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools=None, params=None):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("model should not continue after approval_required tool result")
            yield {"type": "tool_call_start", "id": "call_browser_approval", "name": "browser_computer"}
            yield {
                "type": "tool_call_delta",
                "id": "call_browser_approval",
                "name": "browser_computer",
                "arguments_chunk": "{\"action\":\"computer.click\",\"x\":1,\"y\":2}",
            }
            yield {"type": "tool_call_end", "id": "call_browser_approval", "name": "browser_computer"}
            yield {"type": "stream_end", "finish_reason": "tool_calls"}

        def complete(self, model, messages, tools=None, params=None):
            raise AssertionError("complete should not be called")

    def fake_execute(self, tool_name, arguments, context):
        return {
            "status": "ok",
            "data": {
                "result": "approval required",
                "is_error": False,
                "widget": {
                    "type": "browser_computer",
                    "action": "computer.click",
                    "requires_approval": True,
                    "approval_token": "approval-token-1",
                    "payload": {"x": 1, "y": 2},
                },
            },
        }

    monkeypatch.setattr(ToolExecutor, "execute", fake_execute)
    monkeypatch.setattr(ChatRunEngine, "_provider_supports_stream_tool_calls", staticmethod(lambda _model: True))

    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.5")
    events = list(
        ChatRunEngine(client=FakeClient()).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "click"},
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "browser_computer",
                            "parameters": {"type": "object", "properties": {}, "required": []},
                        },
                    }
                ],
            },
            {"principal_capabilities": ["developer"]},
            stream_mode=True,
        )
    )

    event_types = [event["type"] for event in events]
    assert "approval_requested" in event_types
    approval_event = next(event for event in events if event["type"] == "approval_requested")
    assert approval_event["data"]["approval_token"] == "approval-token-1"
    assert approval_event["data"]["payload"] == {"x": 1, "y": 2}
    final_message = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    assert final_message["raw_text"] == "許可が必要なため、ユーザーが承認するまで待機します。承認後に続行します。"
    assert final_message["metadata"]["pending_approval"]["approval_token"] == "approval-token-1"
    ChatStore._instance = None


def test_chat_run_engine_browser_approval_followup_resumes_one_computer_tool_call(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor
    from types import SimpleNamespace

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    approval.reset_approval_state_for_tests()

    router_calls: list[dict[str, object]] = []

    def fake_router(action, payload, context=None, *, tool_name="computer_use", tool_arguments=None, artifact_root=None, yolo_mode=False):
        router_calls.append(
            {
                "action": action,
                "payload": dict(payload),
                "context": dict(context or {}),
                "tool_name": tool_name,
                "tool_arguments": dict(tool_arguments or {}),
            }
        )
        if not bool((context or {}).get("_tool_server_approved")):
            request = approval.create_approval_request(
                f"tool.{tool_name}",
                "high",
                dict(tool_arguments or {}),
                details={"tool_name": tool_name, "action": action, "payload": dict(payload)},
            )
            return {
                "action": action,
                "tool_name": tool_name,
                "payload": dict(payload),
                "requires_approval": True,
                "approval_required": True,
                "approval_request_id": request["request_id"],
                "request_id": request["request_id"],
                "risk_level": "high",
            }
        return {
            "action": action,
            "apps": [{"name": "Google Chrome"}],
            "is_error": False,
        }

    from ecosystem.defaultspack.domain.host_bridge import computer_router

    monkeypatch.setattr(computer_router, "run_computer_action", fake_router)
    monkeypatch.setattr(ChatRunEngine, "_provider_supports_stream_tool_calls", staticmethod(lambda _model: True))

    def fake_capability_executor(context):
        def execute(_principal_id, request):
            arguments = dict(request.get("args") or {})
            result = fake_router(
                str(arguments.get("action") or "apps"),
                arguments,
                context,
                tool_name="computer_use",
                tool_arguments=arguments,
            )
            return SimpleNamespace(
                success=True,
                output=result,
                error=None,
                error_type="",
            )

        return SimpleNamespace(execute=execute)

    monkeypatch.setattr(
        ToolExecutor,
        "_capability_executor",
        staticmethod(fake_capability_executor),
    )

    class ApprovalClient:
        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools=None, params=None):
            yield {"type": "tool_call_start", "id": "call_browser_approval", "name": "computer_use"}
            yield {
                "type": "tool_call_delta",
                "id": "call_browser_approval",
                "name": "computer_use",
                "arguments_chunk": "{\"action\":\"apps\"}",
            }
            yield {"type": "tool_call_end", "id": "call_browser_approval", "name": "computer_use"}
            yield {"type": "stream_end", "finish_reason": "tool_calls"}

        def complete(self, model, messages, tools=None, params=None):
            raise AssertionError("complete should not be called")

    class ResumeClient:
        def __init__(self):
            self.calls = 0

        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools=None, params=None):
            self.calls += 1
            if self.calls == 1:
                yield {"type": "tool_call_start", "id": "call_browser_resume", "name": "computer_use"}
                yield {
                    "type": "tool_call_delta",
                    "id": "call_browser_resume",
                    "name": "computer_use",
                    "arguments_chunk": "{\"action\":\"apps\"}",
                }
                yield {"type": "tool_call_end", "id": "call_browser_resume", "name": "computer_use"}
                yield {"type": "stream_end", "finish_reason": "tool_calls"}
                return
            if self.calls == 2:
                yield {"type": "content_delta", "delta": {"type": "text", "text": "resumed"}}
                yield {"type": "stream_end", "finish_reason": "stop"}
                return
            raise AssertionError("resume flow should finish after one approved tool call")

        def complete(self, model, messages, tools=None, params=None):
            raise AssertionError("complete should not be called")

    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.5")
    tool_schema = {"kind": "tool", "id": "computer_use"}

    first_events = list(
        ChatRunEngine(client=ApprovalClient()).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "show apps"},
                "tools": [tool_schema],
                "params": {
                    "tool_selection": {
                        "mode": "manual",
                        "include": ["computer_use"],
                    }
                },
            },
            {"principal_capabilities": ["developer"]},
            stream_mode=True,
        )
    )

    approval_event = next(event for event in first_events if event["type"] == "approval_requested")
    request_id = approval_event["data"]["approval_request_id"]
    decision = approval.approve(request_id)
    assert decision["approved"] is True

    resume_client = ResumeClient()
    resumed_events = list(
        ChatRunEngine(client=resume_client).stream(
            {
                "conversation_id": conversation["id"],
                "message": {
                    "role": "user",
                    "content": "ユーザーが許可しました。承認済みの操作を続行してください。",
                    "metadata": {
                        "approval_followup": {
                            "tool_name": "computer_use",
                            "action": "computer.apps",
                            "operation": "computer.apps",
                            "approval_token": decision["token"],
                            "request_id": request_id,
                        },
                        "runtime_content": (
                            "The user approved the pending computer/browser operation.\n"
                            "Continue by calling the exact pending tool once with the approved arguments below.\n"
                            "Tool: computer_use\n"
                            "Operation: computer.apps\n"
                            "Approved arguments JSON:\n"
                            "{\n  \"action\": \"computer.apps\"\n}"
                        ),
                    },
                },
                "tools": [tool_schema],
                "params": {
                    "tool_selection": {
                        "mode": "manual",
                        "include": ["computer_use"],
                    }
                },
            },
            {"principal_capabilities": ["developer"]},
            stream_mode=True,
        )
    )

    # The canonical v4 replay executes the approved host action once.  The
    # provider's repeated tool call is retained as an activity event but is
    # suppressed by the replay guard instead of invoking the host a second
    # time.
    assert len(router_calls) == 1
    assert router_calls[0]["context"]["_tool_server_approved"] is True
    assert router_calls[0]["payload"]["approval_token"] == decision["token"]
    assert router_calls[0]["tool_arguments"] == {
        "action": "computer.apps",
        "approval_token": decision["token"],
    }
    assert resume_client.calls == 2
    duplicate_event = next(
        event
        for event in resumed_events
        if event["type"] == "tool_call_completed"
        and event.get("tool_call_id") == "call_browser_resume"
    )
    assert "Skipped duplicate approval-followup" in duplicate_event["message"]
    final_message = [event["data"]["message"] for event in resumed_events if event["type"] == "done"][-1]
    assert final_message["raw_text"] == "resumed"
    ChatStore._instance = None


def test_approval_followup_tool_use_unwraps_controller_shaped_computer_payload():
    from domain.chat.stream_engine import _approval_followup_tool_use

    tool_use = _approval_followup_tool_use(
        {
            "approval_followup": {
                "tool_name": "computer_use",
                "action": "computer.show_app",
                "operation": "computer.show_app",
                "approval_token": "approval-token",
                "request_id": "apr_1",
                "payload": {
                    "action": "computer.show_app",
                    "payload": {"app": "Vivaldi"},
                },
            }
        }
    )

    assert tool_use is not None
    assert tool_use["input"] == {"app": "Vivaldi", "action": "computer.show_app"}


def test_approval_followup_replay_unwraps_controller_shaped_computer_payload(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.safety import approval

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    approval.reset_approval_state_for_tests()

    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-5.4")
    approved_args = {"action": "computer.show_app", "payload": {"app": "Vivaldi"}}
    request = approval.create_approval_request(
        "computer.show_app",
        "high",
        approved_args,
        details={
            "tool_name": "computer_use",
            "action": "computer.show_app",
            "function_id": "computer.show_app",
            "pack_id": "defaultspack",
            "conversation_id": conversation["id"],
            "arguments": approved_args,
        },
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    captured_calls: list[tuple[str, dict[str, object]]] = []

    def call_handler(name, payload):
        captured_calls.append((name, dict(payload)))
        if name == "defaults.tool.invoke":
            return {"status": "ok", "data": {"result": "shown", "is_error": False}}
        if name == "defaults.ai.complete":
            return {
                "status": "ok",
                "data": {
                    "content": [{"type": "text", "text": "resumed"}],
                    "finish_reason": "stop",
                },
            }
        raise AssertionError(name)

    class SummaryClient:
        def supports_stream(self, model):
            return True

        def stream(self, model, messages, tools=None, params=None):
            yield {"type": "content_delta", "delta": {"type": "text", "text": "resumed"}}
            yield {"type": "stream_end", "finish_reason": "stop"}

        def complete(self, model, messages, tools=None, params=None):
            raise AssertionError("complete should remain behind the v4 call handler")

    tool_schema = {
        "type": "function",
        "function": {
            "name": "computer_use",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
    events = list(
        ChatRunEngine(client=SummaryClient()).stream(
            {
                "conversation_id": conversation["id"],
                "message": {
                    "role": "user",
                    "content": "ユーザーが許可しました。承認済みの操作を続行してください。",
                    "metadata": {
                        "approval_followup": {
                            "tool_name": "computer_use",
                            "action": "computer.show_app",
                            "operation": "computer.show_app",
                            "approval_token": decision["token"],
                            "request_id": request["request_id"],
                            "payload": approved_args,
                        }
                    },
                },
                "tools": [tool_schema],
            },
            {"call_handler": call_handler},
            stream_mode=True,
        )
    )

    invoked = next(
        payload for name, payload in captured_calls if name == "defaults.tool.invoke"
    )
    assert invoked["tool_name"] == "computer_use"
    assert invoked["arguments"] == {
        "action": "computer.show_app",
        "app": "Vivaldi",
        "approval_token": decision["token"],
    }
    assert "payload" not in invoked["arguments"]
    final_message = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    assert final_message["raw_text"] == "resumed"
    ChatStore._instance = None
