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
    monkeypatch.setattr(
        block,
        "get_model_capabilities",
        lambda model, **kwargs: {},
    )
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


def test_search_models_uses_captured_owner_and_forwards_snapshot(
    monkeypatch: Any,
) -> None:
    import blocks.ai.search_models as block

    owner = object()
    _SettingsService.seen = []
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(block, "ModelRuntimeSettingsService", _SettingsService)
    monkeypatch.setattr(
        block,
        "search_models",
        lambda filters, *, settings: captured.append(settings) or {"models": []},
    )

    result = block.run(
        {"settings_owner": "payload-owner"},
        {"settings_owner": "context-owner"},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert _SettingsService.seen == [owner]
    assert captured == [{}]


def test_get_model_capabilities_uses_captured_owner_and_forwards_snapshot(
    monkeypatch: Any,
) -> None:
    import blocks.ai.get_model_capabilities as block

    owner = object()
    _SettingsService.seen = []
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(block, "ModelRuntimeSettingsService", _SettingsService)
    monkeypatch.setattr(
        block,
        "get_model_capabilities",
        lambda profile_id, *, settings: captured.append(settings)
        or {"profile_id": profile_id},
    )

    result = block.run(
        {"profile_id": "provider/model", "settings_owner": "payload-owner"},
        {"settings_owner": "context-owner"},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert _SettingsService.seen == [owner]
    assert captured == [{}]


def test_recommend_model_uses_captured_owner_and_forwards_snapshot(
    monkeypatch: Any,
) -> None:
    import blocks.ai.recommend_model as block

    owner = object()
    _SettingsService.seen = []
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(block, "ModelRuntimeSettingsService", _SettingsService)
    monkeypatch.setattr(
        block,
        "recommend_model",
        lambda request, *, profiles, settings: captured.append(settings)
        or {"selected_model": None},
    )

    result = block.run(
        {"settings_owner": "payload-owner"},
        {"settings_owner": "context-owner"},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert _SettingsService.seen == [owner]
    assert captured == [{}]


def test_model_catalog_uses_only_the_supplied_settings_snapshot(
    monkeypatch: Any,
) -> None:
    from ecosystem.defaultspack.backend.ai_client import provider_catalog
    from domain.ai_client import model_search
    from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService

    settings = {"snapshot": "captured-owner"}
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(provider_catalog, "list_profile_catalog", lambda: [])
    monkeypatch.setattr(provider_catalog, "list_model_catalog", lambda *args: [])
    monkeypatch.setattr(
        ModelRuntimeSettingsService,
        "get_settings",
        lambda self: (_ for _ in ()).throw(AssertionError("ambient read")),
    )
    monkeypatch.setattr(
        ModelRuntimeSettingsService,
        "runtime_defined_profiles",
        lambda self, value: seen.append(value)
        or [
            {
                "profile_id": "captured/model",
                "provider_id": "captured",
                "model_id": "model",
                "type": "chat",
                "configured": True,
                "supports_tool_calling": True,
            }
        ],
    )

    profiles = model_search.get_profile_catalog(settings=settings)
    capabilities = model_search.get_model_capabilities(
        "captured/model",
        settings=settings,
    )

    assert [profile["profile_id"] for profile in profiles] == ["captured/model"]
    assert capabilities is not None
    assert capabilities["supports_tool_calling"] is True
    assert seen == [settings, settings]


def test_ai_client_profile_catalog_does_not_read_ambient_settings(
    monkeypatch: Any,
) -> None:
    from domain.ai_client import client as client_module
    from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService

    settings = {"snapshot": "captured-owner"}
    seen: list[dict[str, Any]] = []
    client = object.__new__(client_module.AIClient)
    client._profiles = {}
    monkeypatch.setattr(
        client_module.AIClient,
        "_active_provider_ids",
        lambda self: {"captured"},
    )
    monkeypatch.setattr(
        client_module.AIClient,
        "_api_key_bound_profiles",
        lambda self: [],
    )
    monkeypatch.setattr(client_module, "build_profile_catalog", lambda **kwargs: [])
    monkeypatch.setattr(
        ModelRuntimeSettingsService,
        "get_settings",
        lambda self: (_ for _ in ()).throw(AssertionError("ambient read")),
    )
    monkeypatch.setattr(
        ModelRuntimeSettingsService,
        "runtime_defined_profiles",
        lambda self, value: seen.append(value)
        or [
            {
                "profile_id": "captured/model",
                "provider_id": "captured",
            }
        ],
    )

    profiles = client.list_profiles(settings=settings)

    assert [profile["profile_id"] for profile in profiles] == ["captured/model"]
    assert seen == [settings]


def test_gateway_routing_normalization_does_not_read_ambient_settings(
    monkeypatch: Any,
) -> None:
    from domain.ai_client import model_runtime_settings
    from domain.ai_client.provider_routing_settings import (
        normalize_gateway_routing_settings,
    )

    monkeypatch.setattr(
        model_runtime_settings.ModelRuntimeSettingsService,
        "get_settings",
        lambda self: (_ for _ in ()).throw(AssertionError("ambient read")),
    )

    normalized = normalize_gateway_routing_settings()

    assert normalized["gateway_routing_target"] == "all"
    assert normalized["gateway_allow_fallbacks"] is True


def test_remote_task_block_uses_only_captured_settings_owner(
    monkeypatch: Any,
) -> None:
    import blocks.remote.task_create as block

    owner = object()
    seen: list[Any] = []

    class Gateway:
        def __init__(self, *, settings_owner: Any = None) -> None:
            seen.append(settings_owner)

        def create_task(self, input_data: Any, context: Any) -> dict[str, bool]:
            del input_data, context
            return {"created": True}

    monkeypatch.setattr(block, "RemoteTaskGateway", Gateway)

    result = block.run(
        {"settings_owner": "payload-owner"},
        {"settings_owner": "context-owner"},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert seen == [owner]


def test_company_supervisor_block_uses_only_captured_settings_owner(
    monkeypatch: Any,
) -> None:
    import blocks.company.supervisor_tick as block

    owner = object()
    seen: list[Any] = []

    class Supervisor:
        def __init__(self, *, settings_owner: Any = None) -> None:
            seen.append(settings_owner)

        def tick(self, company_id: str, **kwargs: Any) -> dict[str, str]:
            del kwargs
            return {"company_id": company_id}

    monkeypatch.setattr(block, "CompanySupervisor", Supervisor)

    result = block.run(
        {"company_id": "company-1", "settings_owner": "payload-owner"},
        {"settings_owner": "context-owner"},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert seen == [owner]


def test_company_message_router_binds_owner_to_default_dispatcher(
    monkeypatch: Any,
) -> None:
    from domain.company import message_router

    owner = object()
    seen: list[Any] = []

    class Dispatcher:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs.get("settings_owner"))

    monkeypatch.setattr(message_router, "CompanyRunDispatcher", Dispatcher)

    message_router.CompanyMessageRouter(
        company_store=object(),
        runtime_store=object(),
        settings_owner=owner,
    )

    assert seen == [owner]


def test_subagent_message_block_uses_only_captured_settings_owner(
    monkeypatch: Any,
) -> None:
    import blocks.subagent_team.messages as block

    owner = object()
    seen: list[Any] = []

    class Service:
        def __init__(self, *, settings_owner: Any = None) -> None:
            seen.append(settings_owner)

        def send_message(self, *args: Any, **kwargs: Any) -> dict[str, bool]:
            del args, kwargs
            return {"sent": True}

    monkeypatch.setattr(block, "SubagentTeamService", Service)

    result = block.run(
        {
            "company_id": "company-1",
            "action": "send",
            "settings_owner": "payload-owner",
        },
        {"settings_owner": "context-owner"},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert seen == [owner]


def test_subagent_creator_binds_owner_to_nested_team_service(
    monkeypatch: Any,
) -> None:
    from domain.subagent_team import creator_service

    owner = object()
    seen: list[Any] = []

    class TeamService:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs.get("settings_owner"))

    monkeypatch.setattr(
        "domain.subagent_team.service.SubagentTeamService",
        TeamService,
    )

    service = creator_service.CreatorService(
        company_store=object(),
        runtime_store=object(),
        settings_owner=owner,
    )
    service._team_service()

    assert seen == [owner]


def test_company_mention_binds_owner_to_message_runtime(monkeypatch: Any) -> None:
    from domain.company import mention, message_router

    owner = object()
    seen: list[Any] = []

    class Runtime:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs.get("settings_owner"))

        def post_message(self, *args: Any, **kwargs: Any) -> dict[str, bool]:
            del args, kwargs
            return {"sent": True}

    monkeypatch.setattr(message_router, "CompanySlackRuntime", Runtime)

    result = mention.CompanyMentionService(
        object(),
        settings_owner=owner,
    ).create_message_task("company-1", content="hello")

    assert result == {"sent": True}
    assert seen == [owner]


def test_company_dispatch_service_binds_owner_to_dispatcher(
    monkeypatch: Any,
) -> None:
    from domain.company import dispatch

    owner = object()
    seen: list[Any] = []

    class Dispatcher:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs.get("settings_owner"))

    monkeypatch.setattr(dispatch, "CompanyRunDispatcher", Dispatcher)

    dispatch.CompanyDispatchService(object(), settings_owner=owner)

    assert seen == [owner]


def test_agent_engine_binds_owner_to_ai_completion(monkeypatch: Any) -> None:
    from domain.agent import engine as engine_module
    import blocks.ai.complete as complete_block

    owner = object()
    seen: list[Any] = []

    def complete(input_data: Any, context: Any, *, settings_owner: Any = None):
        del input_data, context
        seen.append(settings_owner)
        return {"status": "ok"}

    monkeypatch.setattr(complete_block, "run", complete)

    engine_module.AgentEngine(settings_owner=owner)._ai_complete(
        [],
        "model-1",
        {},
    )

    assert seen == [owner]
