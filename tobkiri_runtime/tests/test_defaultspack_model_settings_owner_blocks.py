"""Registered model blocks use only the settings owner captured by the Host."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class _SettingsService:
    seen: list[Any] = []

    def __init__(self, *args: Any, settings_owner: Any = None, **kwargs: Any) -> None:
        del args, kwargs
        self.seen.append(settings_owner)

    def get_settings(self) -> dict[str, Any]:
        return {}

    def get_effective_thinking_level(self, **kwargs: Any) -> dict[str, str]:
        del kwargs
        return {"level": "medium"}


def test_route_model_uses_explicit_captured_settings_owner(monkeypatch: Any) -> None:
    import blocks.ai.route_model as block

    owner = object()
    _SettingsService.seen = []
    monkeypatch.setattr(block, "ModelRuntimeSettingsService", _SettingsService)
    monkeypatch.setattr(
        block,
        "route_model_request",
        lambda request: SimpleNamespace(to_dict=lambda: {"model": request.preferred_model}),
    )

    result = block.run({"message": "hello"}, {}, settings_owner=owner)

    assert result["status"] == "ok"
    assert _SettingsService.seen == [owner]


def test_complete_uses_explicit_captured_settings_owner(monkeypatch: Any) -> None:
    import blocks.ai.complete as block

    owner = object()
    _SettingsService.seen = []
    monkeypatch.setattr(block, "ModelRuntimeSettingsService", _SettingsService)
    monkeypatch.setattr(
        block,
        "LLMGateway",
        lambda: SimpleNamespace(complete=lambda payload: {"content": payload["model"]}),
    )

    result = block.run(
        {"model": "model-1", "messages": [{"role": "user", "content": "hello"}]},
        {},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert _SettingsService.seen == [owner]


def test_stream_uses_explicit_captured_settings_owner(monkeypatch: Any) -> None:
    import blocks.ai.stream as block

    owner = object()
    _SettingsService.seen = []
    sent: list[tuple[str, list[dict[str, Any]]]] = []
    monkeypatch.setattr(block, "ModelRuntimeSettingsService", _SettingsService)
    monkeypatch.setattr(block, "stream", lambda payload: [{"content": payload["model_reference"]}])
    monkeypatch.setattr(
        block,
        "StreamHandler",
        lambda context: SimpleNamespace(
            send_chunks=lambda stream_id, chunks: sent.append((stream_id, list(chunks)))
        ),
    )

    result = block.run(
        {"model": "model-1", "messages": [{"role": "user", "content": "hello"}]},
        {},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert sent and sent[0][1] == [{"content": "model-1"}]
    assert _SettingsService.seen == [owner]


def test_provider_command_uses_explicit_captured_settings_owner(monkeypatch: Any) -> None:
    import blocks.ai.provider_command as block

    owner = object()
    _SettingsService.seen = []
    monkeypatch.setattr(block, "ModelRuntimeSettingsService", _SettingsService)

    result = block.run(
        {"settings_owner": "payload-owner"},
        {"settings_owner": "context-owner"},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert _SettingsService.seen == [owner]


def test_model_switch_bridge_uses_explicit_captured_settings_owner(
    monkeypatch: Any,
) -> None:
    import blocks.chat.update_conversation as block

    owner = object()
    _SettingsService.seen = []
    monkeypatch.setattr(block, "ModelRuntimeSettingsService", _SettingsService)
    monkeypatch.setattr(block, "get_model_capabilities", lambda model: {})
    monkeypatch.setattr(
        block,
        "detect_modalities",
        lambda content, metadata: {"has_images": True},
    )
    monkeypatch.setattr(block, "describe_images", lambda **kwargs: {"summary": "image"})

    result = block._with_model_switch_compatibility(
        object(),
        "conversation-1",
        {
            "title": "Conversation",
            "messages": [
                {
                    "content": "image",
                    "metadata": {"attachments": [{"type": "image/png"}]},
                }
            ],
        },
        {"model": "text-model"},
        {"settings_owner": "context-owner"},
        settings_owner=owner,
    )

    assert result["metadata"]["model_switch_vision_bridge_result"] == {
        "summary": "image"
    }
    assert _SettingsService.seen == [owner]
