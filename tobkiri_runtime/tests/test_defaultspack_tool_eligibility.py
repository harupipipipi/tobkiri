from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.chat.store import ChatStore  # noqa: E402
from domain.chat.run_request import prepare_chat_run  # noqa: E402
from domain.tool.executor import ToolExecutor  # noqa: E402


def _configure_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_STORE_PATH", str(tmp_path / "integrations" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_LOCKS_DIR", str(tmp_path / "integrations" / "event_locks"))
    ChatStore._instance = None


def _conversation() -> dict:
    ChatStore._instance = None
    return ChatStore().create_conversation(model="stub/default")


def _patch_routing(monkeypatch, *, selected_model: str, supports_image_input: bool, supports_tool_calling: bool = True):
    class Decision:
        def __init__(self, model: str) -> None:
            self.selected_model = model
            self.original_model = model
            self.selected_group = "default"
            self.reason_codes = ["test"]
            self.warnings = []
            self.bridge_required = False
            self.bridge_plan = {}

        def to_dict(self) -> dict:
            return {"selected_model": self.selected_model}

    monkeypatch.setattr("domain.chat.run_request.route_model_request", lambda request: Decision(selected_model))
    monkeypatch.setattr(
        "domain.chat.run_request.get_model_capabilities",
        lambda model: {
            "supports_image_input": supports_image_input,
            "supports_vision": supports_image_input,
            "supports_tool_calling": supports_tool_calling,
            "supports_thinking": True,
        },
    )


def _image_tool() -> dict:
    return {
        "tool_id": "vision_tool",
        "name": "vision_tool",
        "summary": "Inspect images",
        "schema": {"parameters": {"type": "object", "properties": {}}},
        "requires_model_capabilities": ["model.image_input"],
        "supports_attachments": True,
    }


def _basic_tool() -> dict:
    return {
        "tool_id": "basic_tool",
        "name": "basic_tool",
        "summary": "Basic tool",
        "schema": {"parameters": {"type": "object", "properties": {}}},
    }


def _image_attachment() -> dict:
    return {
        "id": "img-1",
        "name": "tiny.png",
        "type": "image/png",
        "dataUrl": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axR4xUAAAAASUVORK5CYII=",
        "size": 68,
    }


def test_text_only_model_blocks_image_tools(
    monkeypatch, tmp_path, defaultspack_conversation_owner
):
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation()
    _patch_routing(monkeypatch, selected_model="demo/text", supports_image_input=False)

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "inspect", "attachments": [_image_attachment()]},
            "tools": [_image_tool()],
        },
        {},
    )

    assert prepared.provider_tools == []
    entry = prepared.metadata["tool_filter_result"][0]
    assert entry["reason_code"] == "model_unsupported"


def test_vision_model_allows_image_tools(
    monkeypatch, tmp_path, defaultspack_conversation_owner
):
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation()
    _patch_routing(monkeypatch, selected_model="demo/vision", supports_image_input=True)

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "inspect", "attachments": [_image_attachment()]},
            "tools": [_image_tool()],
        },
        {},
    )

    assert [tool["function"]["name"] for tool in prepared.provider_tools] == ["vision_tool", "assistant_progress"]
    assert prepared.metadata["tool_filter_result"][0]["status"] == "allowed"


def test_blocked_tool_not_sent_to_provider(
    monkeypatch, tmp_path, defaultspack_conversation_owner
):
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation()
    _patch_routing(monkeypatch, selected_model="demo/text", supports_image_input=False)

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "inspect", "attachments": [_image_attachment()]},
            "tools": [_image_tool()],
        },
        {},
    )

    assert prepared.provider_tools == []


def test_non_tool_calling_model_marks_tools_blocked_or_detached(
    monkeypatch, tmp_path, defaultspack_conversation_owner
):
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation()
    _patch_routing(
        monkeypatch,
        selected_model="demo/no-tools",
        supports_image_input=True,
        supports_tool_calling=False,
    )

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "use a tool"},
            "tools": [_basic_tool()],
        },
        {},
    )

    assert prepared.provider_tools == []
    entry = prepared.metadata["tool_filter_result"][0]
    assert entry["tool_name"] == "basic_tool"
    assert entry["status"] == "blocked"
    assert entry["reason_code"] == "model_unsupported"
    assert "model.tool_calling" in entry["required"]["model_capabilities"]
    assert "actual" not in entry


def test_ai_calling_filtered_tool_rejected_at_execution():
    result = ToolExecutor().execute(
        "vision_tool",
        {},
        {
            "tool_filter_result": [
                {
                    "tool_name": "vision_tool",
                    "status": "blocked",
                    "reason_code": "model_unsupported",
                    "reason": "selected model does not support this tool",
                    "required": {"model_capabilities": ["model.image_input"]},
                    "actual": {"model_capabilities": ["model.text"]},
                    "repair_suggestions": ["Switch to a compatible model or disable the tool for this turn."],
                }
            ]
        },
    )

    assert result["status"] == "rejected"
    assert result["code"] == "MODEL_UNSUPPORTED"


def test_capability_tags_saved_in_metadata_not_text(
    monkeypatch, tmp_path, defaultspack_conversation_owner
):
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation()
    _patch_routing(monkeypatch, selected_model="demo/text", supports_image_input=False)

    prepared = prepare_chat_run(
        {
            "conversation_id": conversation["id"],
            "message": {"role": "user", "content": "inspect", "attachments": [_image_attachment()]},
            "tools": [_image_tool()],
        },
        {},
    )

    assert "runtime_capability_snapshot" in prepared.metadata
    assert "model.image_input" not in str(prepared.content)
