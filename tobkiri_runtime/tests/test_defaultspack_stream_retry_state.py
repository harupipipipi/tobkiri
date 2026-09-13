from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterator

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_conversation_owner")


class ScriptedGateway:
    """Serve deterministic provider stream attempts for retry tests."""

    def __init__(self, scripts: list[list[dict[str, Any] | BaseException]]) -> None:
        self.scripts = scripts
        self.calls = 0

    def supports_stream(self, model: str) -> bool:
        del model
        return True

    def resolve_provider(self, model: str) -> tuple[object, str]:
        class OpenAIProvider:
            pass

        return OpenAIProvider(), model

    def stream(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
        del request
        script = self.scripts[self.calls]
        self.calls += 1
        for item in script:
            if isinstance(item, BaseException):
                raise item
            yield item

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        del request
        raise AssertionError("complete fallback should not run")


class ScriptedCompleteGateway:
    """Serve deterministic non-stream provider attempts."""

    def __init__(self, script: list[dict[str, Any] | BaseException]) -> None:
        self.script = script
        self.calls = 0

    def complete(self, request: dict[str, Any]) -> dict[str, Any]:
        del request
        item = self.script[self.calls]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


def fake_jwt() -> str:
    """Build a JWT-shaped test secret without a scanner-triggering literal."""
    return ".".join(("eyJ" + ("a" * 24), "eyJ" + ("b" * 24), "c" * 24))


def prepared_run(*, retry_delay: float = 0) -> Any:
    """Build the minimum prepared run required by the stream engine."""
    from domain.chat.run_request import PreparedChatRun

    provider_tools = [
        {
            "type": "function",
            "function": {
                "name": "coding_file_read",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    return PreparedChatRun(
        conversation_id="conv-stream-retry",
        conversation={"id": "conv-stream-retry"},
        input_data={},
        request_id="req-stream-retry",
        content=[],
        metadata=None,
        user_message={"id": "user-stream-retry"},
        model="openai/gpt-test",
        params={"retry": {"max_attempts": 2, "delays": [retry_delay]}},
        request_context={},
        tool_context={},
        standard_messages=[],
        user_text="read README",
        system_prompt="",
        enrich_info={},
        raw_tools=provider_tools,
        provider_tools=provider_tools,
        tools_called=["coding_file_read"],
        connected_tool_names={"coding_file_read"},
        call_handler=None,
        model_routing={},
    )


def drain_model_turn(generator: Iterator[Any]) -> tuple[list[dict[str, Any]], Any]:
    """Collect yielded events and return a generator's final value."""
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as exc:
            return events, exc.value


@pytest.mark.parametrize("failed_call_id", ["call-1", "failed-call"])
def test_retry_isolates_partial_tool_arguments_between_attempts(
    failed_call_id: str,
) -> None:
    from domain.chat.stream_engine import ChatRunEngine

    gateway = ScriptedGateway(
        [
            [
                {
                    "type": "tool_call_start",
                    "id": failed_call_id,
                    "name": "coding_file_read",
                },
                {
                    "type": "tool_call_delta",
                    "id": failed_call_id,
                    "name": "coding_file_read",
                    "arguments_chunk": '{"path":',
                },
                RuntimeError("503 temporary"),
            ],
            [
                {
                    "type": "tool_call_start",
                    "id": "call-1",
                    "name": "coding_file_read",
                },
                {
                    "type": "tool_call_delta",
                    "id": "call-1",
                    "name": "coding_file_read",
                    "arguments_chunk": '{"path":"README.md"}',
                },
                {
                    "type": "tool_call_end",
                    "id": "call-1",
                    "name": "coding_file_read",
                },
                {"type": "stream_end", "finish_reason": "tool_calls"},
            ],
        ]
    )
    engine = ChatRunEngine(gateway=gateway)

    events, (_response, tool_uses) = drain_model_turn(
        engine._model_turn(
            prepared_run(),
            [{"role": "user", "content": "read README"}],
            None,
        )
    )

    assert gateway.calls == 2
    assert tool_uses == [
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "coding_file_read",
            "input": {"path": "README.md"},
            "provider_attempt": 2,
            "provider_attempt_generation": 2,
        }
    ]
    retry = [event for event in events if event.get("type") == "ai_retry_scheduled"]
    assert retry[0]["data"]["provider_attempt"] == 1
    assert retry[0]["data"]["provider_attempt_generation"] == 1
    discarded = [
        event for event in events if event.get("data", {}).get("provider_attempt_discarded") is True
    ]
    assert [event["data"]["tool_call_id"] for event in discarded] == [failed_call_id]
    assert discarded[0]["data"]["provider_attempt"] == 1
    assert discarded[0]["data"]["provider_attempt_generation"] == 1


def test_retried_tool_activity_keeps_attempts_separate_through_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    gateway = ScriptedGateway(
        [
            [
                {
                    "type": "tool_call_start",
                    "id": "call-1",
                    "name": "coding_file_read",
                },
                {
                    "type": "tool_call_delta",
                    "id": "call-1",
                    "name": "coding_file_read",
                    "arguments_chunk": '{"path":',
                },
                RuntimeError("503 temporary"),
            ],
            [
                {
                    "type": "tool_call_start",
                    "id": "call-1",
                    "name": "coding_file_read",
                },
                {
                    "type": "tool_call_delta",
                    "id": "call-1",
                    "name": "coding_file_read",
                    "arguments_chunk": '{"path":"README.md"}',
                },
                {
                    "type": "tool_call_end",
                    "id": "call-1",
                    "name": "coding_file_read",
                },
                {"type": "stream_end", "finish_reason": "tool_calls"},
            ],
            [
                {"type": "content_delta", "delta": {"text": "done"}},
                {"type": "stream_end", "finish_reason": "stop"},
            ],
        ]
    )
    executed: list[dict[str, Any]] = []

    def call_handler(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert name == "defaults.tool.invoke"
        executed.append(dict(payload["arguments"]))
        return {"status": "ok", "data": {"path": "README.md", "content": "ok"}}

    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-test")
    events = list(
        ChatRunEngine(store=store, gateway=gateway).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "read README"},
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "coding_file_read",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                "params": {"retry": {"max_attempts": 2, "delays": [0]}},
            },
            {"call_handler": call_handler},
            stream_mode=True,
        )
    )

    started = [event for event in events if event["type"] == "tool_call_started"]
    completed = [event for event in events if event["type"] == "tool_call_completed"]
    discarded = [
        event for event in completed if event["data"].get("provider_attempt_discarded") is True
    ]
    succeeded = [
        event for event in completed if event["data"].get("provider_attempt_discarded") is not True
    ]
    final = [event["data"]["message"] for event in events if event["type"] == "done"][-1]

    assert executed == [{"path": "README.md"}]
    assert [event["data"]["provider_attempt_generation"] for event in started] == [1, 2]
    assert discarded[0]["data"]["provider_attempt_generation"] == 1
    assert discarded[0]["data"]["status"] == "failed"
    assert succeeded[0]["data"]["provider_attempt_generation"] == 2
    assert succeeded[0]["data"]["status"] == "completed"
    assert final["raw_text"] == "done"
    stored_started = [event for event in final["events"] if event["type"] == "tool_call_started"]
    stored_completed = [
        event for event in final["events"] if event["type"] == "tool_call_completed"
    ]
    assert [event["provider_attempt_generation"] for event in stored_started] == [1, 2]
    assert [event["provider_attempt_generation"] for event in stored_completed] == [1, 2]
    assert final["tool_logs"] == [
        {
            "tool_name": "coding_file_read",
            "tool_call_id": "call-1",
            "arguments": {"path": "README.md"},
            "result": {"status": "ok", "data": {"path": "README.md", "content": "ok"}},
            "timestamp": final["tool_logs"][0]["timestamp"],
            "provider_attempt": 2,
            "provider_attempt_generation": 2,
        }
    ]
    ChatStore._instance = None


def test_tool_call_accumulator_drops_incomplete_or_malformed_calls() -> None:
    from domain.chat.tool_call_accumulator import ToolCallAccumulator

    accumulator = ToolCallAccumulator()
    accumulator.ingest({"type": "tool_call_start", "id": "incomplete", "name": "coding_file_read"})
    accumulator.ingest(
        {
            "type": "tool_call_delta",
            "id": "incomplete",
            "arguments_chunk": '{"path":',
        }
    )
    accumulator.ingest({"type": "tool_call_start", "id": "malformed", "name": "coding_file_read"})
    accumulator.ingest(
        {
            "type": "tool_call_delta",
            "id": "malformed",
            "arguments_chunk": "not-json",
        }
    )
    accumulator.ingest({"type": "tool_call_end", "id": "malformed", "name": "coding_file_read"})
    accumulator.ingest({"type": "tool_call_start", "id": "valid", "name": "coding_file_read"})
    accumulator.ingest(
        {
            "type": "tool_call_delta",
            "id": "valid",
            "arguments_chunk": '{"path":"README.md"}',
        }
    )
    accumulator.ingest({"type": "tool_call_end", "id": "valid", "name": "coding_file_read"})

    assert accumulator.tool_uses() == [
        {
            "type": "tool_use",
            "id": "valid",
            "name": "coding_file_read",
            "input": {"path": "README.md"},
        }
    ]


def test_tool_call_accumulator_parses_openai_complete_argument_string() -> None:
    from domain.chat.tool_call_accumulator import ToolCallAccumulator

    accumulator = ToolCallAccumulator()
    accumulator.ingest(
        {
            "type": "tool_use",
            "id": "openai-call",
            "name": "repository_context_prepare",
            "input": '{"workspace_root":"/workspace","objective":"find contract"}',
        }
    )

    assert accumulator.tool_uses() == [
        {
            "type": "tool_use",
            "id": "openai-call",
            "name": "repository_context_prepare",
            "input": {
                "workspace_root": "/workspace",
                "objective": "find contract",
            },
        }
    ]


def test_must_use_requires_exact_selected_tool_not_assistant_progress() -> None:
    from domain.chat.stream_engine import _missing_required_tool_ids

    required = {"repository_context_prepare"}
    progress_only = [
        {"tool_name": "assistant_progress", "is_error": False},
        {
            "tool_name": "repository_context_prepare",
            "internal": True,
            "is_error": False,
        },
    ]

    assert _missing_required_tool_ids(required, progress_only) == [
        "repository_context_prepare"
    ]
    assert _missing_required_tool_ids(
        required,
        [
            *progress_only,
            {
                "tool_name": "repository_context_prepare",
                "is_error": False,
            },
        ],
    ) == []
    for failed in (
        {"is_error": True},
        {"status": "failed"},
        {"cancelled": True},
        {"rejected_by_policy": True},
        {"approval_required": True},
        {"result": {"status": "error", "is_error": True}},
    ):
        assert _missing_required_tool_ids(
            required,
            [
                {
                    "tool_name": "repository_context_prepare",
                    **failed,
                }
            ],
        ) == ["repository_context_prepare"]


@pytest.mark.parametrize(
    "arguments_chunk",
    ['{"path":', '"README.md"', '["README.md"]', "not-json"],
)
def test_stream_never_executes_incomplete_or_non_object_tool_arguments(
    arguments_chunk: str,
) -> None:
    from domain.chat.stream_engine import ChatRunEngine

    gateway = ScriptedGateway(
        [
            [
                {
                    "type": "tool_call_start",
                    "id": "unsafe-call",
                    "name": "coding_file_read",
                },
                {
                    "type": "tool_call_delta",
                    "id": "unsafe-call",
                    "name": "coding_file_read",
                    "arguments_chunk": arguments_chunk,
                },
                {
                    "type": "tool_call_end",
                    "id": "unsafe-call",
                    "name": "coding_file_read",
                },
                {"type": "content_delta", "delta": {"text": "safe fallback"}},
                {"type": "stream_end", "finish_reason": "stop"},
            ]
        ]
    )
    engine = ChatRunEngine(gateway=gateway)

    _events, (response, tool_uses) = drain_model_turn(
        engine._model_turn(
            prepared_run(),
            [{"role": "user", "content": "read README"}],
            None,
        )
    )

    assert response["content"] == [{"type": "text", "text": "safe fallback"}]
    assert tool_uses == []


@pytest.mark.parametrize(
    "raw_arguments",
    ["not-json", '"README.md"', '["README.md"]', 7, ["README.md"]],
)
def test_complete_turn_drops_malformed_or_non_object_tool_arguments(
    raw_arguments: Any,
) -> None:
    from domain.chat.stream_engine import ChatRunEngine

    response = {
        "content": [
            {
                "type": "tool_use",
                "id": "unsafe-complete",
                "name": "coding_file_read",
                "input": raw_arguments,
            }
        ],
        "finish_reason": "tool_calls",
    }
    engine = ChatRunEngine(gateway=ScriptedCompleteGateway([response]))

    _events, (returned, tool_uses) = drain_model_turn(
        engine._model_turn_via_complete(
            prepared_run(),
            [{"role": "user", "content": "read README"}],
            None,
        )
    )

    assert returned == response
    assert tool_uses == []


def test_non_stream_run_never_reaches_executor_for_malformed_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine
    from domain.tool.executor import ToolExecutor

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    def fail_if_executed(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("malformed non-stream arguments reached executor")

    monkeypatch.setattr(ToolExecutor, "execute", fail_if_executed)
    gateway = ScriptedCompleteGateway(
        [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "unsafe-non-stream",
                        "name": "coding_file_read",
                        "input": ["README.md"],
                    }
                ],
                "finish_reason": "tool_calls",
            }
        ]
    )
    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-test")

    events = list(
        ChatRunEngine(store=store, gateway=gateway).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "read README"},
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "coding_file_read",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                "params": {"retry": {"max_attempts": 1}},
            },
            {},
            stream_mode=False,
        )
    )

    final = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    assert gateway.calls == 1
    assert final["tool_logs"] == []
    assert not any(event["type"] == "tool_call_started" for event in events)
    ChatStore._instance = None


def test_empty_stream_fallback_drops_malformed_complete_tool_arguments() -> None:
    from domain.chat.stream_engine import ChatRunEngine

    class MalformedFallbackGateway(ScriptedGateway):
        def __init__(self) -> None:
            super().__init__([[{"type": "stream_end", "finish_reason": "stop"}]])
            self.complete_calls = 0

        def complete(self, request: dict[str, Any]) -> dict[str, Any]:
            del request
            self.complete_calls += 1
            return {
                "content": [
                    {
                        "type": "tool_call",
                        "id": "unsafe-fallback",
                        "name": "coding_file_read",
                        "arguments": "not-json",
                    }
                ],
                "finish_reason": "tool_calls",
            }

    gateway = MalformedFallbackGateway()
    engine = ChatRunEngine(gateway=gateway)

    _events, (response, tool_uses) = drain_model_turn(
        engine._model_turn(
            prepared_run(),
            [{"role": "user", "content": "read README"}],
            None,
        )
    )

    assert gateway.complete_calls == 2
    assert all(
        not isinstance(block, dict)
        or block.get("type") not in {"tool_use", "tool_call"}
        for block in response.get("content", [])
    )
    assert tool_uses == []


def test_text_derived_malformed_tool_arguments_are_not_recovered() -> None:
    from domain.chat.stream_engine import _text_tool_call_blocks

    response = {
        "content": [
            {
                "type": "text",
                "text": (
                    '<tool_invocation name="coding_file_read" '
                    'arguments={"path":} />'
                ),
            }
        ]
    }

    assert _text_tool_call_blocks(response, {"coding_file_read"}) == []


def test_execution_boundary_rejects_invalid_arguments_before_policy_or_executor() -> None:
    from domain.chat.stream_engine import ChatRunEngine

    engine = ChatRunEngine(gateway=ScriptedGateway([]))
    engine._before_tool_call = lambda *_args: (_ for _ in ()).throw(
        AssertionError("approval/policy boundary must not run")
    )
    engine._execute_tool = lambda *_args: (_ for _ in ()).throw(
        AssertionError("executor must not run")
    )

    events, response = drain_model_turn(
        engine._execute_tool_use(
            prepared_run(),
            [],
            None,
            None,
            {
                "type": "tool_use",
                "id": "unsafe-boundary",
                "name": "coding_file_read",
                "input": "not-json",
            },
        )
    )

    assert events == []
    assert response["finish_reason"] == "tool_call_rejected"
    assert response["metadata"]["rejection_code"] == "INVALID_TOOL_ARGUMENTS"


def test_same_provider_call_id_starts_again_on_the_next_model_turn() -> None:
    from domain.chat.stream_engine import ChatRunEngine

    tool_attempt = [
        {
            "type": "tool_call_start",
            "id": "reused-call",
            "name": "coding_file_read",
        },
        {
            "type": "tool_call_delta",
            "id": "reused-call",
            "arguments_chunk": '{"path":"README.md"}',
        },
        {"type": "tool_call_end", "id": "reused-call"},
        {"type": "stream_end", "finish_reason": "tool_calls"},
    ]
    engine = ChatRunEngine(gateway=ScriptedGateway([tool_attempt, tool_attempt]))

    first_events, (_first_response, first_tool_uses) = drain_model_turn(
        engine._model_turn(prepared_run(), [], None)
    )
    second_events, (_second_response, second_tool_uses) = drain_model_turn(
        engine._model_turn(prepared_run(), [], None)
    )

    first_started = [event for event in first_events if event["type"] == "tool_call_started"]
    second_started = [event for event in second_events if event["type"] == "tool_call_started"]
    assert len(first_started) == len(second_started) == 1
    assert first_started[0]["data"]["provider_attempt_generation"] == 1
    assert second_started[0]["data"]["provider_attempt_generation"] == 2
    assert first_tool_uses[0]["provider_attempt_generation"] == 1
    assert second_tool_uses[0]["provider_attempt_generation"] == 2


def test_cancel_after_tool_start_emits_terminal_completion_before_stopping() -> None:
    from domain.chat.stream_engine import ChatRunEngine, _ChatCancelled

    engine = ChatRunEngine(gateway=ScriptedGateway([]))
    engine._before_tool_call = lambda *_args: []
    engine._execute_tool = lambda *_args: {"status": "ok", "data": {"path": "README.md"}}
    cancel_checks = 0

    def cancel_after_start() -> bool:
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks >= 2

    engine._external_cancel_checker = cancel_after_start
    generator = engine._execute_tool_use(
        prepared_run(),
        [],
        None,
        None,
        {
            "type": "tool_use",
            "id": "cancelled-call",
            "name": "coding_file_read",
            "input": {"path": "README.md"},
            "provider_attempt_generation": 1,
        },
    )

    started = next(generator)
    completed = next(generator)
    with pytest.raises(_ChatCancelled):
        next(generator)

    assert started["type"] == "tool_call_started"
    assert completed["type"] == "tool_call_completed"
    assert completed["data"]["cancelled"] is True
    assert completed["data"]["is_error"] is True


def test_provider_stream_cancel_closes_started_tool_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    class CancelAfterToolStartGateway(ScriptedGateway):
        def __init__(self) -> None:
            super().__init__([])
            self.engine: ChatRunEngine | None = None

        def stream(self, request: dict[str, Any]) -> Iterator[dict[str, Any]]:
            del request
            self.calls += 1
            yield {
                "type": "tool_call_start",
                "id": "cancel-stream-call",
                "name": "coding_file_read",
            }
            assert self.engine is not None
            self.engine._cancel_event.set()
            yield {
                "type": "tool_call_delta",
                "id": "cancel-stream-call",
                "name": "coding_file_read",
                "arguments_chunk": '{"path":"README.md"}',
            }

    executed: list[dict[str, Any]] = []
    gateway = CancelAfterToolStartGateway()
    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-test")
    engine = ChatRunEngine(store=store, gateway=gateway)
    gateway.engine = engine

    def broken_external_cancel_checker() -> bool:
        raise RuntimeError("cancel checker unavailable")

    engine._external_cancel_checker = broken_external_cancel_checker
    events = list(
        engine.stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "read README"},
                "tools": prepared_run().provider_tools,
                "params": {"retry": {"max_attempts": 2, "delays": [0]}},
            },
            {"call_handler": lambda *_args: executed.append({})},
            stream_mode=True,
        )
    )

    started = [event for event in events if event["type"] == "tool_call_started"]
    completed = [event for event in events if event["type"] == "tool_call_completed"]
    assert executed == []
    assert len(started) == len(completed) == 1
    assert completed[0]["data"]["tool_call_id"] == "cancel-stream-call"
    assert completed[0]["data"]["cancelled"] is True
    assert completed[0]["data"]["executed"] is False
    assert completed[0]["data"]["provider_attempt_discarded"] is True
    assert completed[0]["data"]["provider_attempt_generation"] == 1
    assert any(event["type"] == "cancelled" for event in events)

    ChatStore._instance = None
    reloaded = ChatStore().get_conversation(conversation["id"])
    assistant = reloaded["messages"][-1]
    stored_completed = [
        event for event in assistant["events"] if event["type"] == "tool_call_completed"
    ]
    assert stored_completed[0]["tool_call_id"] == "cancel-stream-call"
    assert stored_completed[0]["cancelled"] is True
    assert stored_completed[0]["provider_attempt_generation"] == 1
    ChatStore._instance = None


def test_retry_discards_thinking_from_failed_attempt() -> None:
    from domain.chat.stream_engine import ChatRunEngine

    gateway = ScriptedGateway(
        [
            [
                {"type": "thinking_delta", "delta": {"text": "discarded thought"}},
                RuntimeError("503 temporary"),
            ],
            [
                {"type": "content_delta", "delta": {"text": "success"}},
                {"type": "stream_end", "finish_reason": "stop"},
            ],
        ]
    )
    engine = ChatRunEngine(gateway=gateway)

    _events, (response, tool_uses) = drain_model_turn(
        engine._model_turn(prepared_run(), [{"role": "user", "content": "hi"}], None)
    )

    assert response["content"] == [{"type": "text", "text": "success"}]
    assert tool_uses == []
    assert "discarded thought" not in "".join(engine._thinking_transcript_parts)


def test_retry_discards_usage_from_failed_attempt() -> None:
    from domain.chat.stream_engine import ChatRunEngine

    gateway = ScriptedGateway(
        [
            [
                {
                    "type": "stream_end",
                    "finish_reason": "length",
                    "usage": {
                        "input_tokens": 999,
                        "output_tokens": 999,
                        "total_tokens": 1998,
                    },
                },
                RuntimeError("503 temporary"),
            ],
            [
                {"type": "content_delta", "delta": {"text": "success"}},
                {
                    "type": "stream_end",
                    "finish_reason": "stop",
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "total_tokens": 4,
                    },
                },
            ],
        ]
    )
    engine = ChatRunEngine(gateway=gateway)

    _events, (response, tool_uses) = drain_model_turn(
        engine._model_turn(prepared_run(), [{"role": "user", "content": "hi"}], None)
    )

    assert response["finish_reason"] == "stop"
    assert response["usage"] == {
        "input_tokens": 3,
        "output_tokens": 1,
        "total_tokens": 4,
    }
    assert tool_uses == []


def test_partial_text_after_tool_result_wins_over_generic_error() -> None:
    from domain.chat.stream_engine import ChatRunEngine

    gateway = ScriptedGateway(
        [
            [
                {"type": "content_delta", "delta": {"text": "partial summary"}},
                RuntimeError("503 after tool result"),
            ]
        ]
    )
    engine = ChatRunEngine(gateway=gateway)
    engine._tool_logs = [{"tool_name": "coding_file_read", "result": {"status": "ok"}}]

    _events, (response, tool_uses) = drain_model_turn(
        engine._model_turn(prepared_run(), [{"role": "user", "content": "hi"}], None)
    )

    assert response["content"] == [{"type": "text", "text": "partial summary"}]
    assert response["finish_reason"] == "error"
    assert response["metadata"]["interrupted"] is True
    assert tool_uses == []


@pytest.mark.parametrize(
    "provider_error",
    [RuntimeError("503 temporary disconnect"), ValueError("invalid provider frame")],
)
def test_provider_failure_persists_visible_partial_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_error: BaseException,
) -> None:
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    class PartialClient:
        def __init__(self) -> None:
            self.calls = 0

        def supports_stream(self, model: str) -> bool:
            del model
            return True

        def stream(
            self,
            model: str,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            params: dict[str, Any] | None = None,
        ) -> Iterator[dict[str, Any]]:
            del model, messages, tools, params
            self.calls += 1
            yield {
                "type": "content_delta",
                "delta": {"type": "text", "text": "valuable partial answer"},
            }
            raise provider_error

        def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            del args, kwargs
            raise AssertionError("visible partial output must not be retried")

    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    client = PartialClient()
    events = list(
        ChatRunEngine(client=client).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "answer this"},
                "tools": [],
                "params": {"retry": {"max_attempts": 2, "delays": [0]}},
            },
            {},
            stream_mode=True,
        )
    )

    assert client.calls == 1
    assert any(event.get("type") == "task_failed" for event in events)
    final = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    assert final["raw_text"] == "valuable partial answer"
    assert final["finish_reason"] == "error"
    assert final["metadata"]["interrupted"] is True
    assert final["metadata"]["interruption_reason"] == "provider_stream_error"
    assert final["metadata"]["provider_error"]["raw_message"] == str(provider_error)

    ChatStore._instance = None
    reloaded = ChatStore().get_conversation(conversation["id"])
    assert reloaded["messages"][-1]["raw_text"] == "valuable partial answer"
    assert reloaded["messages"][-1]["metadata"]["interrupted"] is True
    ChatStore._instance = None


def test_partial_response_metadata_redacts_raw_provider_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    secret = fake_jwt()
    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    gateway = ScriptedGateway(
        [
            [
                {"type": "content_delta", "delta": {"text": "partial"}},
                RuntimeError(f"503 temporary Authorization: Bearer {secret}"),
            ]
        ]
    )
    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-test")
    events = list(
        ChatRunEngine(store=store, gateway=gateway).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "answer this"},
                "tools": [],
                "params": {"retry": {"max_attempts": 2, "delays": [0]}},
            },
            {},
            stream_mode=True,
        )
    )

    final = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    raw_message = final["metadata"]["provider_error"]["raw_message"]
    task_failed = [event for event in events if event["type"] == "task_failed"][-1]
    assert secret not in raw_message
    assert secret not in str(task_failed)
    assert "[redacted]" in raw_message

    ChatStore._instance = None


def test_retry_activity_redacts_raw_provider_secrets() -> None:
    from domain.chat.stream_engine import ChatRunEngine

    secret = fake_jwt()
    gateway = ScriptedGateway(
        [
            [RuntimeError(f"503 temporary authorization=Bearer {secret}")],
            [
                {"type": "content_delta", "delta": {"text": "success"}},
                {"type": "stream_end", "finish_reason": "stop"},
            ],
        ]
    )
    engine = ChatRunEngine(gateway=gateway)

    events, (response, tool_uses) = drain_model_turn(
        engine._model_turn(prepared_run(), [{"role": "user", "content": "hi"}], None)
    )

    retry_event = [event for event in events if event["type"] == "ai_retry_scheduled"][-1]
    assert response["content"] == [{"type": "text", "text": "success"}]
    assert tool_uses == []
    assert secret not in str(retry_event)
    assert "[redacted]" in str(retry_event)


def test_error_redactor_removes_header_scheme_jwt_and_api_key_values() -> None:
    from blocks.chat.send import _redact_error_text

    jwt_secret = fake_jwt()
    api_secret = "api-value-" + ("q" * 32)
    samples = [
        f"Authorization: Bearer {jwt_secret}",
        f'{{"authorization": "Bearer {jwt_secret}"}}',
        f"provider rejected Bearer {jwt_secret}",
        f"provider echoed {jwt_secret}",
        f"x-api-key: {api_secret}",
        f'{{"api_key": "{api_secret}"}}',
    ]

    for sample in samples:
        redacted = _redact_error_text(sample)
        assert jwt_secret not in redacted
        assert api_secret not in redacted
        assert "[redacted]" in redacted


def test_error_redactor_removes_unknown_authorization_schemes_and_values() -> None:
    from blocks.chat.send import _redact_error_text

    secret = "opaque-" + ("v" * 28)
    samples = [
        f"Authorization: Digest {secret}",
        f"Proxy-Authorization=CustomScheme {secret}",
        f'{{"authorization": "Negotiate {secret}"}}',
        f"Authorization: {secret}",
    ]

    for sample in samples:
        redacted = _redact_error_text(sample)
        assert secret not in redacted
        assert "[redacted]" in redacted


def test_error_redactor_preserves_quotes_and_does_not_redact_auth_prose() -> None:
    from blocks.chat.send import _redact_error_text

    secret = "opaque-" + ("w" * 28)
    quoted = _redact_error_text(f'{{"authorization": "Custom {secret}"}}')
    escaped_quote = _redact_error_text(
        f'{{"authorization": "Custom {secret}\\\"suffix"}}'
    )
    prose = (
        "Basic authentication is required; Bearer authentication and bearer "
        "capacity remain available as diagnostic information. Token responsibilities "
        "are documented by the authorization scheme."
    )

    assert quoted == '{"authorization": "[redacted]"}'
    assert escaped_quote == '{"authorization": "[redacted]"}'
    assert _redact_error_text(prose) == prose


def test_error_redactor_removes_standalone_alpha_bearer_credentials() -> None:
    from blocks.chat.send import _redact_error_text

    for scheme, secret in (
        ("Bearer", "abcdefgh"),
        ("Basic", "abcdefghi"),
        ("Token", "abcdefghijklmno"),
    ):
        redacted = _redact_error_text(f"provider rejected {scheme} {secret}")

        assert secret not in redacted
        assert redacted == "provider rejected [redacted]"


def test_error_redactor_removes_short_alpha_credentials_in_assignments_and_quotes() -> None:
    from blocks.chat.send import _redact_error_text

    secret = "abcdefgh"
    samples = [
        f"Authorization: Bearer {secret}",
        f"Proxy-Authorization=Basic {secret}",
        f'{{"authorization": "Token {secret}"}}',
        f'provider rejected "Bearer {secret}"',
    ]

    for sample in samples:
        redacted = _redact_error_text(sample)
        assert secret not in redacted
        assert "[redacted]" in redacted


def test_alpha_bearer_secret_is_redacted_across_stream_retry_and_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    secret = "abcdefgh"
    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None
    gateway = ScriptedGateway(
        [
            [RuntimeError(f"503 temporary: Bearer {secret}")],
            [
                {"type": "content_delta", "delta": {"text": "success"}},
                {"type": "stream_end", "finish_reason": "stop"},
            ],
        ]
    )
    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-test")
    events = list(
        ChatRunEngine(store=store, gateway=gateway).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "answer this"},
                "tools": [],
                "params": {"retry": {"max_attempts": 2, "delays": [0]}},
            },
            {},
            stream_mode=True,
        )
    )

    final = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    assert gateway.calls == 2
    assert final["raw_text"] == "success"
    assert secret not in str(events)
    assert secret not in str(final)
    assert "[redacted]" in str(final["events"])

    ChatStore._instance = None
    reloaded = ChatStore().get_conversation(conversation["id"])
    assert secret not in str(reloaded)
    assert "[redacted]" in str(reloaded["messages"][-1]["events"])
    ChatStore._instance = None


def test_outer_stream_failure_redacts_activity_and_persisted_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    secret = "abcdefghi"
    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-test")
    gateway = ScriptedGateway([[RuntimeError(f"401 provider rejected Bearer {secret}")]])
    events = list(
        ChatRunEngine(store=store, gateway=gateway).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "answer this"},
                "tools": [],
                "params": {"retry": {"max_attempts": 2, "delays": [0]}},
            },
            {},
            stream_mode=True,
        )
    )

    final = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    reloaded = store.get_conversation(conversation["id"])
    assert secret not in str(events)
    assert secret not in str(final)
    assert secret not in str(reloaded["messages"][-1])
    assert "[redacted]" in str(final["metadata"]["error"])
    ChatStore._instance = None


def test_non_stream_retry_redacts_activity_and_persisted_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from domain.chat.store import ChatStore
    from domain.chat.stream_engine import ChatRunEngine

    secret = "abcdefghijklmno"
    storage_path = tmp_path / "user_data" / "shared" / "chat" / "conversations.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(storage_path))
    ChatStore._instance = None

    store = ChatStore()
    conversation = store.create_conversation(model="openai/gpt-test")
    gateway = ScriptedCompleteGateway(
        [
            RuntimeError(f"503 provider rejected Token {secret}"),
            {
                "content": [{"type": "text", "text": "success"}],
                "finish_reason": "stop",
                "usage": {},
                "metadata": {},
            },
        ]
    )
    events = list(
        ChatRunEngine(store=store, gateway=gateway).stream(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "answer this"},
                "tools": [],
                "params": {"retry": {"max_attempts": 2, "delays": [0]}},
            },
            {},
            stream_mode=False,
        )
    )

    final = [event["data"]["message"] for event in events if event["type"] == "done"][-1]
    assert gateway.calls == 2
    assert final["raw_text"] == "success"
    assert secret not in str(events)
    assert secret not in str(final)
    assert "[redacted]" in str(final["events"])
    ChatStore._instance = None
    reloaded = ChatStore().get_conversation(conversation["id"])
    assert secret not in str(reloaded["messages"][-1])
    assert "[redacted]" in str(reloaded["messages"][-1]["events"])
    ChatStore._instance = None


def test_stream_retry_backoff_remains_cancellable() -> None:
    from domain.chat.stream_engine import ChatRunEngine, _ChatCancelled

    gateway = ScriptedGateway(
        [[RuntimeError("503 temporary")], [{"type": "stream_end", "finish_reason": "stop"}]]
    )
    engine = ChatRunEngine(gateway=gateway)
    cancel_checks = 0

    def cancelled_during_backoff() -> bool:
        nonlocal cancel_checks
        cancel_checks += 1
        return cancel_checks >= 3

    engine._external_cancel_checker = cancelled_during_backoff
    generator = engine._model_turn(
        prepared_run(retry_delay=1),
        [{"role": "user", "content": "hi"}],
        None,
    )

    retry_event = next(generator)
    assert retry_event["type"] == "ai_retry_scheduled"
    with pytest.raises(_ChatCancelled):
        next(generator)
    assert gateway.calls == 1
