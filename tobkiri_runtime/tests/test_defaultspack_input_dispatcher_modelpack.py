from __future__ import annotations

import json
import sys
import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_conversation_owner")

from domain.chat.store import ChatStore  # noqa: E402
from domain.chat.run_request import prepare_chat_run  # noqa: E402
from domain.external.token_store import read_external_token, set_external_token  # noqa: E402
from domain.function_runtime.dispatcher import run_defaultspack_function  # noqa: E402
from domain.input import RumiInputEnvelope, dispatch_input, submit_input  # noqa: E402
from domain.webhook.endpoint_store import WebhookEndpointStore  # noqa: E402
from domain.webhook.inbound import handle_inbound_webhook  # noqa: E402
from domain.ai_client.model_call import call_model  # noqa: E402
from domain.ai_client.model_pack_store import ModelPackStore  # noqa: E402
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService  # noqa: E402
from domain.ai_client.model_router import ModelRoutingDecision, ModelRoutingRequest, route_model_request  # noqa: E402
from domain.ai_client.client import AIClient  # noqa: E402
from domain.ai_client.model_pack import ModelPack  # noqa: E402
from domain.ai_client.rumi_process import default_rumi_model_pack  # noqa: E402


_V4_DIRECT_PROVIDER_TEST_REASON = (
    "Retired: Pack v4 routes provider calls through ContractLLMGateway; "
    "legacy direct AIClient model-pack execution is no longer a runtime contract."
)


@pytest.fixture
def steer_runtime(monkeypatch):
    """Provide an explicit canonical turn owner for instruction delivery tests."""
    from domain.chat import steer as steer_module
    from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnRuntime

    runtime = TurnRuntime()

    def invoke(contract_id, operation, payload):
        if contract_id == steer_module.CONVERSATION_RESOURCE:
            assert operation == "get"
            return {
                "id": payload["conversation_id"],
                "conversation_revision": 1,
            }
        if contract_id == steer_module.TURN_RESOURCE:
            if operation == "get":
                return runtime.get(payload["turn_id"])
            assert operation == "list"
            return {"turns": runtime.list(conversation_id=payload.get("conversation_id"))}
        if contract_id == steer_module.TURN_ACTION:
            if operation == "begin":
                return runtime.begin(payload)
            if operation == "steer":
                return runtime.steer(
                    payload["turn_id"],
                    payload["guidance"],
                    expected_revision=payload["expected_revision"],
                )
        raise AssertionError(f"unexpected turn contract call: {contract_id}/{operation}")

    monkeypatch.setattr(steer_module, "_invoke", invoke)
    return runtime


def _configure_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_STORE_PATH", str(tmp_path / "integrations" / "conversations.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_INTEGRATIONS_LOCKS_DIR", str(tmp_path / "integrations" / "event_locks"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_STEER_STORE_PATH", str(tmp_path / "chat" / "steer_queue.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_WEBHOOK_ENDPOINTS_PATH", str(tmp_path / "webhooks" / "endpoints.json"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_SECRETS_DIR", str(tmp_path / "secrets"))
    ChatStore._instance = None


def _conversation(tmp_path: Path) -> dict:
    ChatStore._instance = None
    return ChatStore().create_conversation(model="stub/default")


def test_chat_owner_module_identity_survives_domain_import_probe(
    monkeypatch, tmp_path
):
    """Keep the collected ChatStore bound to the selected owner after a probe."""
    from tests.test_browser_companion import _defaultspack_domain_module

    import domain.chat.store as chat_store_module

    collected_chat_store = ChatStore
    original_path = tuple(sys.path)
    _defaultspack_domain_module("domain.tool.executor")

    assert sys.modules["domain.chat.store"] is chat_store_module
    assert collected_chat_store is chat_store_module.ChatStore
    assert tuple(sys.path) == original_path

    _configure_paths(monkeypatch, tmp_path)
    conversation = collected_chat_store().create_conversation(model="stub/default")
    assert conversation["model"] == "stub/default"


def _fake_route_decision(model: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        selected_model=model,
        original_model=model,
        selected_group="default",
        reason_codes=["test"],
        warnings=[],
        bridge_required=False,
        bridge_plan={},
        utility_models={},
        explanation="test",
    )


def _tiny_image_attachment() -> dict:
    return {
        "id": "img-1",
        "name": "tiny.png",
        "type": "image/png",
        "dataUrl": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axR4xUAAAAASUVORK5CYII=",
        "size": 68,
    }


def test_input_envelope_accepts_target_delivery_attachments():
    envelope = RumiInputEnvelope.from_dict(
        {
            "input": "hello",
            "target": {"conversation_id": "conv-1"},
            "delivery": {"action_id": "run.instruction"},
            "attachments": [{"id": "a1"}],
        }
    )

    assert envelope.target == {"conversation_id": "conv-1"}
    assert envelope.delivery == {"action_id": "run.instruction"}
    assert envelope.attachments == [{"id": "a1"}]


def test_submit_input_defaults_to_chat_message(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation(tmp_path)

    monkeypatch.setattr(
        "blocks.chat.send.run",
        lambda request, context: {
            "status": "ok",
            "data": {"id": "assistant-1", "content": [{"type": "text", "text": "hi"}]},
        },
    )

    result = submit_input(
        {
            "role": "user",
            "input": "hello",
            "chat": {"conversation_id": conversation["id"]},
            "source": {"kind": "internal", "provider": "internal"},
            "target": {"conversation_id": conversation["id"], "direct": True},
        },
        {},
    )

    assert result["status"] == "ok"
    assert result["action_id"] == "chat.message"
    assert result["conversation_id"] == conversation["id"]


def test_generic_webhook_delivery_chat_message(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation(tmp_path)
    WebhookEndpointStore().upsert(
        {
            "id": "generic-chat",
            "kind": "generic",
            "input_profile_id": "generic.webhook.default",
            "enabled": True,
            "target": {"conversation_id": conversation["id"], "direct": True},
            "default_delivery": {"action_id": "chat.message"},
            "allowed_delivery_actions": ["chat.message", "run.instruction"],
        }
    )
    set_external_token("generic", "secret", token_id="generic-chat", kind="webhook_shared_secret")
    monkeypatch.setattr(
        "blocks.chat.send.run",
        lambda request, context: {
            "status": "ok",
            "data": {"id": "assistant-2", "content": [{"type": "text", "text": "sent"}]},
        },
    )

    result = handle_inbound_webhook(
        "generic-chat",
        {"text": "hello", "_headers": {"x-rumi-webhook-token": "secret"}, "action_id": "chat.message"},
        {},
    )

    assert result["status"] == "ok"
    assert result["result"]["action_id"] == "chat.message"
    assert result["result"]["conversation_id"] == conversation["id"]


def test_generic_webhook_delivery_run_instruction(monkeypatch, tmp_path, steer_runtime):
    del steer_runtime
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation(tmp_path)
    WebhookEndpointStore().upsert(
        {
            "id": "generic-steer",
            "kind": "generic",
            "input_profile_id": "generic.webhook.default",
            "enabled": True,
            "target": {"conversation_id": conversation["id"]},
            "default_delivery": {"action_id": "run.instruction"},
        }
    )
    set_external_token("generic", "secret", token_id="generic-steer", kind="webhook_shared_secret")

    result = handle_inbound_webhook(
        "generic-steer",
        {"text": "please continue", "_headers": {"x-rumi-webhook-token": "secret"}},
        {},
    )

    assert result["status"] == "ok"
    assert result["result"]["action_id"] == "run.instruction"
    assert result["result"]["instruction"]["conversation_id"] == conversation["id"]


def test_generic_webhook_rejects_disallowed_delivery_action(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    _conversation(tmp_path)
    WebhookEndpointStore().upsert(
        {
            "id": "generic-locked",
            "kind": "generic",
            "input_profile_id": "generic.webhook.default",
            "enabled": True,
            "allowed_delivery_actions": ["chat.message"],
        }
    )
    set_external_token("generic", "secret", token_id="generic-locked", kind="webhook_shared_secret")

    result = handle_inbound_webhook(
        "generic-locked",
        {"text": "nope", "_headers": {"x-rumi-webhook-token": "secret"}, "action_id": "run.instruction"},
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "WEBHOOK_DELIVERY_ACTION_NOT_ALLOWED"


def test_input_endpoint_create_returns_localhost_url_with_secret(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("DEFAULTS_HTTP_PORT", "9911")

    result = run_defaultspack_function(
        "input_endpoint_create",
        {"shared_secret": "super-secret", "ttl_seconds": 120, "action_id": "run.instruction"},
        {},
    )

    data = result["data"]
    assert data["localhost_url"].startswith("http://localhost:9911/api/webhooks/inbound/")
    assert data["shared_secret"] == "super-secret"
    assert read_external_token("generic", token_id=data["endpoint_id"], kind="webhook_shared_secret") == "super-secret"


def test_input_endpoint_default_allowed_actions_only_allows_default(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)

    result = run_defaultspack_function(
        "input_endpoint_create",
        {"shared_secret": "secret", "ttl_seconds": 120},
        {},
    )

    endpoint = result["data"]["endpoint"]
    assert endpoint["default_delivery"]["action_id"] == "chat.message"
    assert endpoint["allowed_delivery_actions"] == ["chat.message"]


def test_input_endpoint_rejects_agent_delegate_override_when_not_allowed(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)

    created = run_defaultspack_function(
        "input_endpoint_create",
        {"shared_secret": "secret", "ttl_seconds": 120},
        {},
    )

    result = handle_inbound_webhook(
        created["data"]["endpoint_id"],
        {"text": "delegate this", "_headers": {"x-rumi-webhook-token": "secret"}, "action_id": "agent.delegate"},
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "WEBHOOK_DELIVERY_ACTION_NOT_ALLOWED"


def test_input_endpoint_non_generic_kind_secret_verifies(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation(tmp_path)
    monkeypatch.setattr(
        "blocks.chat.send.run",
        lambda request, context: {
            "status": "ok",
            "data": {"id": "assistant-kind", "content": [{"type": "text", "text": "sent"}]},
        },
    )

    created = run_defaultspack_function(
        "input_endpoint_create",
        {
            "kind": "local_agent_input",
            "shared_secret": "kind-secret",
            "target": {"conversation_id": conversation["id"], "direct": True},
        },
        {},
    )
    endpoint_id = created["data"]["endpoint_id"]

    assert read_external_token("local_agent_input", token_id=endpoint_id, kind="webhook_shared_secret") == "kind-secret"
    assert read_external_token("generic", token_id=endpoint_id, kind="webhook_shared_secret") == ""

    result = handle_inbound_webhook(
        endpoint_id,
        {"text": "hello", "_headers": {"x-rumi-webhook-token": "kind-secret"}},
        {},
    )

    assert result["status"] == "ok"
    assert result["result"]["action_id"] == "chat.message"


def test_input_endpoint_delete_removes_secret_for_endpoint_kind(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    created = run_defaultspack_function(
        "input_endpoint_create",
        {"kind": "local_agent_input", "shared_secret": "kind-secret"},
        {},
    )
    endpoint_id = created["data"]["endpoint_id"]

    result = run_defaultspack_function("input_endpoint_delete", {"endpoint_id": endpoint_id}, {})

    assert result["status"] == "ok"
    assert read_external_token("local_agent_input", token_id=endpoint_id, kind="webhook_shared_secret") == ""


def test_input_endpoint_ttl_expired_rejected(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    WebhookEndpointStore().upsert(
        {
            "id": "expired-webhook",
            "kind": "generic",
            "input_profile_id": "generic.webhook.default",
            "enabled": True,
            "expires_at": 1,
        }
    )
    set_external_token("generic", "secret", token_id="expired-webhook", kind="webhook_shared_secret")

    result = handle_inbound_webhook(
        "expired-webhook",
        {"text": "expired", "_headers": {"x-rumi-webhook-token": "secret"}},
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "WEBHOOK_EXPIRED"
    assert result["_http_status"] == 410


def test_agent_delegate_action_starts_agent_with_tools_params_capabilities(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    seen: dict[str, object] = {}

    def fake_execute(input_data, context):
        seen["input_data"] = input_data
        seen["context"] = context
        return {"status": "ok", "data": {"execution_id": "agent-1", "status": "queued"}}

    monkeypatch.setattr("blocks.agent.execute.run", fake_execute)

    result = dispatch_input(
        {
            "input": "",
            "target": {"conversation_id": "conv-1"},
            "delivery": {"action_id": "agent.delegate"},
            "params": {
                "delegate": {
                    "task": "check the docs",
                    "tools": ["web_search"],
                    "required_capabilities": ["runtime.workspace"],
                    "params": {"mode": "review"},
                }
            },
        },
        {},
    )

    assert result["status"] == "ok"
    assert seen["input_data"]["tools"] == ["web_search"]
    assert seen["input_data"]["required_capabilities"] == ["runtime.workspace"]
    assert seen["input_data"]["params"] == {"mode": "review"}


def test_agent_delegate_provider_error_returns_visible_safe_failure(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    secret = "sk-test-secret"

    def fake_execute(input_data, context):
        return {
            "status": "ok",
            "data": {
                "execution_id": "agent-provider-fail",
                "status": "error",
                "result": {
                    "execution_id": "agent-provider-fail",
                    "status": "error",
                    "error": "provider error: API key " + secret,
                },
            },
        }

    monkeypatch.setattr("blocks.agent.execute.run", fake_execute)

    result = dispatch_input(
        {
            "input": "",
            "delivery": {"action_id": "agent.delegate"},
            "params": {"delegate": {"task": "check the docs"}},
        },
        {},
    )

    assert result["status"] == "error"
    assert result["code"] == "DELEGATE_PROVIDER_ERROR"
    assert result["assistant_text"]
    assert result["error"] == result["assistant_text"]
    assert result["delegate"]["execution_id"] == "agent-provider-fail"
    assert result["delegate"]["status"] == "error"
    assert result["result"]["error_redacted"] is True
    serialized = json.dumps(result, ensure_ascii=False)
    assert secret not in serialized
    assert "API key" not in serialized


def test_agent_delegate_real_execute_receives_required_capabilities_and_context(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)

    def fake_ai(self, messages, model, context, tools=None):
        return {"status": "ok", "data": {"content": "delegated"}}

    monkeypatch.setattr("domain.agent.engine.AgentEngine._ai_complete", fake_ai)

    result = dispatch_input(
        {
            "input": "",
            "target": {"conversation_id": "conv-1", "direct": True},
            "delivery": {"action_id": "agent.delegate"},
            "attachments": [{"id": "att-1", "name": "notes.md", "type": "text/markdown"}],
            "params": {
                "delegate": {
                    "task": "check the docs",
                    "required_capabilities": ["runtime.workspace"],
                    "params": {"mode": "review"},
                }
            },
        },
        {"conversation_workspace_dir": str(tmp_path)},
    )

    context = result["result"]["result"]["context"]
    assert result["status"] == "ok"
    assert context["required_capabilities"] == ["runtime.workspace"]
    assert context["params"] == {"mode": "review"}
    assert context["attachments"][0]["name"] == "notes.md"
    assert context["target"] == {"conversation_id": "conv-1", "direct": True}
    assert context["delivery"]["action_id"] == "agent.delegate"


def test_model_pack_selects_vision_member_for_images():
    profiles = [
        {"profile_id": "demo/text", "qualified_model_id": "demo/text", "provider_id": "demo", "model_id": "text", "type": "chat", "configured": True, "supports_vision": False, "supports_tool_calling": False, "supports_thinking": False},
        {"profile_id": "demo/vision", "qualified_model_id": "demo/vision", "provider_id": "demo", "model_id": "vision", "type": "chat", "configured": True, "supports_vision": True, "supports_tool_calling": True, "supports_thinking": True},
    ]
    settings = {"model_packs": [{"id": "triage", "members": [{"model": "demo/text"}, {"model": "demo/vision"}]}], "preferred_model_group": "default", "model_groups": {"default": {"allowed_models": []}}}

    decision = route_model_request(
        ModelRoutingRequest(has_images=True, preferred_model="modelpack/triage", settings=settings),
        profiles=profiles,
    )

    assert decision.selected_model == "demo/vision"
    assert "model_pack_selected" in decision.reason_codes


def test_model_pack_selects_tool_member_for_tool_calling():
    profiles = [
        {"profile_id": "demo/text", "qualified_model_id": "demo/text", "provider_id": "demo", "model_id": "text", "type": "chat", "configured": True, "supports_vision": False, "supports_tool_calling": False, "supports_thinking": False},
        {"profile_id": "demo/tool", "qualified_model_id": "demo/tool", "provider_id": "demo", "model_id": "tool", "type": "chat", "configured": True, "supports_vision": False, "supports_tool_calling": True, "supports_thinking": True},
    ]
    settings = {"model_packs": [{"id": "triage", "members": [{"model": "demo/text"}, {"model": "demo/tool"}]}], "preferred_model_group": "default", "model_groups": {"default": {"allowed_models": []}}}

    decision = route_model_request(
        ModelRoutingRequest(requires_tool_calling=True, preferred_model="modelpack/triage", settings=settings),
        profiles=profiles,
    )

    assert decision.selected_model == "demo/tool"


def test_model_pack_no_compatible_member_warns_or_errors():
    profiles = [
        {"profile_id": "demo/text", "qualified_model_id": "demo/text", "provider_id": "demo", "model_id": "text", "type": "chat", "configured": True, "supports_vision": False, "supports_tool_calling": False, "supports_thinking": False},
    ]
    settings = {"model_packs": [{"id": "triage", "members": [{"model": "demo/text"}]}], "preferred_model_group": "default", "model_groups": {"default": {"allowed_models": []}}}

    decision = route_model_request(
        ModelRoutingRequest(has_images=True, preferred_model="modelpack/triage", settings=settings),
        profiles=profiles,
    )

    assert decision.selected_model == "modelpack/triage"
    assert "no_model_pack_member_satisfied_capabilities" in decision.warnings
    assert "model_pack_no_compatible_member" in decision.reason_codes


def test_model_pack_does_not_silently_pick_text_model_for_images():
    profiles = [
        {"profile_id": "demo/text", "qualified_model_id": "demo/text", "provider_id": "demo", "model_id": "text", "type": "chat", "configured": True, "supports_vision": False, "supports_tool_calling": False, "supports_thinking": False},
    ]
    settings = {"model_packs": [{"id": "triage", "members": [{"model": "demo/text"}]}], "preferred_model_group": "default", "model_groups": {"default": {"allowed_models": []}}}

    selection = ModelPackStore(settings).get("modelpack/triage")
    assert selection is not None
    from domain.ai_client.model_pack_router import select_model_pack

    result = select_model_pack(selection, {"has_images": True}, settings=settings, profiles=profiles)

    assert result is not None
    assert result.selected_model == ""
    assert result.ordered_members == []
    assert "no_model_pack_member_satisfied_capabilities" in result.warnings


def test_model_pack_explicit_image_condition_allows_unknown_capability_member():
    settings = {
        "model_packs": [
            {
                "id": "conditional",
                "members": [
                    {"model": "plain/text", "conditions": {"has_images": False}},
                    {"model": "vision/omni", "conditions": {"has_images": True}},
                ],
            }
        ],
        "preferred_model_group": "default",
        "model_groups": {"default": {"allowed_models": []}},
    }

    selection = ModelPackStore(settings).get("modelpack/conditional")
    assert selection is not None
    from domain.ai_client.model_pack_router import select_model_pack

    result = select_model_pack(selection, {"has_images": True}, settings=settings, profiles=[])

    assert result is not None
    assert result.selected_model == "vision/omni"
    assert [member["model"] for member in result.ordered_members] == ["vision/omni"]


@pytest.mark.skip(reason=_V4_DIRECT_PROVIDER_TEST_REASON)
def test_model_pack_fallback_chain(monkeypatch, tmp_path):
    settings_path = tmp_path / "frontend_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "models": {
                    "model_packs": [
                        {
                            "id": "fallback-pack",
                            "members": [
                                {"model": "demo/primary", "fallback_on": ["any"]},
                                {"model": "demo/backup"},
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(AIClient, "_settings_path", lambda self: settings_path)

    class PrimaryProvider:
        def complete(self, model_name, messages, tools, params):
            raise RuntimeError("rate limit")

    class BackupProvider:
        def complete(self, model_name, messages, tools, params):
            return {"content": [{"type": "text", "text": "backup ok"}], "metadata": {}}

    def fake_resolve(self, model):
        if model == "demo/primary":
            return PrimaryProvider(), "primary"
        if model == "demo/backup":
            return BackupProvider(), "backup"
        raise AssertionError(model)

    monkeypatch.setattr(AIClient, "resolve_provider", fake_resolve)

    response = AIClient().complete("modelpack/fallback-pack", [{"role": "user", "content": "hi"}], [], {})

    assert response["content"][0]["text"] == "backup ok"
    assert response["metadata"]["model_pack"]["pack_id"] == "fallback-pack"


@pytest.mark.skip(reason=_V4_DIRECT_PROVIDER_TEST_REASON)
def test_model_pack_review_chain_uses_isolated_reviewer(monkeypatch, tmp_path):
    settings_path = tmp_path / "frontend_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "models": {
                    "model_packs": [
                        {
                            "id": "review-pack",
                            "mode": "review_chain",
                            "members": [
                                {"model": "demo/generator", "metadata": {"role": "generator", "thinking_level": "medium"}},
                                {"model": "demo/reviewer", "metadata": {"role": "reviewer", "thinking_level": "medium"}},
                            ],
                            "budget": {"max_review_rounds": 2},
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(AIClient, "_settings_path", lambda self: settings_path)

    class DemoProvider:
        def __init__(self):
            self.calls = []

        def complete(self, model_name, messages, tools, params):
            self.calls.append({"model": model_name, "messages": messages, "tools": tools, "params": params})
            if model_name == "generator":
                return {"content": [{"type": "text", "text": "RUMI_REASONING_BRIEF: concise\nDRAFT_RESPONSE: draft ok"}]}
            if model_name == "reviewer":
                payload = messages[-1]["content"]
                assert "draft ok" in payload
                assert "reviewer_context_rule" in payload
                assert tools == []
                return {"content": [{"type": "text", "text": json.dumps({"pass": True, "score": 96, "issues": [], "required_changes": []})}]}
            raise AssertionError(model_name)

    provider = DemoProvider()

    def fake_resolve(self, model):
        if model.startswith("demo/"):
            return provider, model.split("/", 1)[1]
        raise AssertionError(model)

    monkeypatch.setattr(AIClient, "resolve_provider", fake_resolve)

    response = AIClient().complete("modelpack/review-pack", [{"role": "user", "content": "implement a larger change"}], [], {})
    process = response["metadata"]["rumi_process"]

    assert response["content"][0]["text"] == "draft ok"
    assert response["finish_reason"] == "stop"
    assert process["review"]["approved"] is True
    assert [event["phase"] for event in process["events"]] == ["generator", "reviewer"]
    assert [call["model"] for call in provider.calls] == ["generator", "reviewer"]
    assert provider.calls[0]["params"]["thinking_level"] == "medium"
    assert provider.calls[1]["tools"] == []


@pytest.mark.skip(reason=_V4_DIRECT_PROVIDER_TEST_REASON)
def test_model_pack_review_chain_quarantines_unmarked_generator_scratch(monkeypatch, tmp_path):
    settings_path = tmp_path / "frontend_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "models": {
                    "model_packs": [
                        {
                            "id": "review-pack",
                            "mode": "review_chain",
                            "members": [
                                {"model": "demo/generator", "metadata": {"role": "generator"}},
                                {"model": "demo/reviewer", "metadata": {"role": "reviewer"}},
                            ],
                            "budget": {"max_review_rounds": 1},
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(AIClient, "_settings_path", lambda self: settings_path)

    class DemoProvider:
        def __init__(self):
            self.calls = []

        def complete(self, model_name, messages, tools, params):
            self.calls.append({"model": model_name, "messages": messages, "tools": tools, "params": params})
            if model_name == "generator":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": "RUMI_REASONING_BRIEF: private scratch\nscratch text that must never ship",
                        }
                    ]
                }
            raise AssertionError("reviewer must not run after an unmarked draft")

    provider = DemoProvider()

    def fake_resolve(self, model):
        if model.startswith("demo/"):
            return provider, model.split("/", 1)[1]
        raise AssertionError(model)

    monkeypatch.setattr(AIClient, "resolve_provider", fake_resolve)

    response = AIClient().complete("modelpack/review-pack", [{"role": "user", "content": "implement a larger change"}], [], {})
    text = response["content"][0]["text"]
    process = response["metadata"]["rumi_process"]

    assert response["finish_reason"] == "draft_quarantine"
    assert process["review"]["reason"] == "missing_final_response_marker"
    assert "RUMI_REASONING_BRIEF" not in text
    assert "scratch text that must never ship" not in text
    assert [call["model"] for call in provider.calls] == ["generator"]


@pytest.mark.skip(reason=_V4_DIRECT_PROVIDER_TEST_REASON)
def test_model_pack_deepthink_chain_selects_harness_tools_separate_from_model_tools(monkeypatch, tmp_path):
    settings_path = tmp_path / "frontend_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "models": {
                    "model_packs": [
                        {
                            "id": "review-pack",
                            "mode": "review_chain",
                            "members": [
                                {"model": "demo/generator", "metadata": {"role": "generator", "thinking_level": "medium"}},
                                {"model": "demo/reviewer", "metadata": {"role": "reviewer", "thinking_level": "medium"}},
                            ],
                            "budget": {
                                "deepthink_max_review_iterations": 1,
                                "deepthink_user_rejection_review_cycles": 0,
                                "deepthink_max_sections": 2,
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(AIClient, "_settings_path", lambda self: settings_path)

    class DemoProvider:
        def __init__(self):
            self.calls = []

        def complete(self, model_name, messages, tools, params):
            self.calls.append({"model": model_name, "messages": messages, "tools": tools, "params": params})
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if model_name == "generator":
                assert params["thinking_level"] == "medium"
                if "Plan the response before writing it" in system:
                    return {"content": [{"type": "text", "text": json.dumps({"structure": ["A", "B"], "key_points": ["k"], "risks": ["r"]})}]}
                if "Write one visible pseudo DeepThinking step" in system:
                    if "harness tool selection" in user:
                        assert "web_search" in user
                        assert "vision_tool_ids" in user
                    return {"content": [{"type": "text", "text": json.dumps({"thinking": "check", "output": "next"})}]}
                if "section only" in system:
                    return {"content": [{"type": "text", "text": "section draft"}]}
                return {"content": [{"type": "text", "text": "final ok"}]}
            if model_name == "reviewer":
                assert tools == []
                return {"content": [{"type": "text", "text": json.dumps({"pass": True, "score": 92, "issues": [], "required_changes": []})}]}
            raise AssertionError(model_name)

    provider = DemoProvider()

    def fake_resolve(self, model):
        if model.startswith("demo/"):
            return provider, model.split("/", 1)[1]
        raise AssertionError(model)

    monkeypatch.setattr(
        "domain.ai_client.model_pack_router.get_model_capabilities",
        lambda model, profiles=None: {"supports_tool_calling": True, "supports_thinking": True} if str(model).startswith("demo/") else {},
    )
    monkeypatch.setattr(AIClient, "resolve_provider", fake_resolve)

    response = AIClient().complete(
        "modelpack/review-pack",
        [{"role": "user", "content": "implement a larger change"}],
        [{"type": "function", "function": {"name": "web_search"}}],
        {"deepthink_enabled": True},
    )
    process = response["metadata"]["rumi_process"]

    assert response["content"][0]["text"] == "final ok"
    assert response["finish_reason"] == "stop"
    assert process["mode"] == "deepthink"
    assert process["deepthink_enabled"] is True
    assert "数時間" in process["warnings"][0]
    assert process["tooling"]["model_tool_ids"] == ["web_search"]
    assert "deepthink_planner" in process["tooling"]["harness_tool_ids"]
    assert process["tooling"]["vision_tool_ids"] == []
    assert process["tooling"]["model_tools_are_separate_from_harness_tools"] is True
    assert any(event["phase"] == "deepthink_notes" and "harness tool selection" in event["metadata"]["label"] for event in process["events"])
    assert provider.calls[-1]["model"] == "reviewer"
    assert provider.calls[-1]["tools"] == []


def _run_deepthink_json_repair_case(monkeypatch, tmp_path, malformed_phase: str):
    settings_path = tmp_path / "frontend_settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "models": {
                    "model_packs": [
                        {
                            "id": "repair-pack",
                            "mode": "review_chain",
                            "members": [
                                {"model": "demo/generator", "metadata": {"role": "generator", "thinking_level": "medium"}},
                                {"model": "demo/reviewer", "metadata": {"role": "reviewer", "thinking_level": "medium"}},
                            ],
                            "budget": {
                                "deepthink_max_review_iterations": 1,
                                "deepthink_user_rejection_review_cycles": 0,
                                "deepthink_max_sections": 1,
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(AIClient, "_settings_path", lambda self: settings_path)

    class DemoProvider:
        def __init__(self):
            self.calls = []
            self.plan_broken = False
            self.note_broken = False
            self.review_broken = False

        def complete(self, model_name, messages, tools, params):
            self.calls.append({"model": model_name, "messages": messages, "tools": tools, "params": params})
            system = messages[0]["content"]
            user = messages[-1]["content"]
            if "Repair malformed JSON" in system:
                assert tools == []
                if '"structure"' in user:
                    return {"content": [{"type": "text", "text": json.dumps({"structure": ["Repaired"], "key_points": ["k"], "risks": []})}]}
                if '"thinking"' in user:
                    return {"content": [{"type": "text", "text": json.dumps({"thinking": "repaired note", "output": "safe note"})}]}
                if '"pass"' in user:
                    return {"content": [{"type": "text", "text": json.dumps({"pass": True, "score": 91, "issues": [], "required_changes": []})}]}
                raise AssertionError(user)
            if model_name == "generator":
                if "Plan the response before writing it" in system:
                    if malformed_phase == "planner" and not self.plan_broken:
                        self.plan_broken = True
                        return {"content": [{"type": "text", "text": "{structure: [broken]"}]}
                    return {"content": [{"type": "text", "text": json.dumps({"structure": ["A"], "key_points": ["k"], "risks": []})}]}
                if "Write one visible pseudo DeepThinking step" in system:
                    if malformed_phase == "note" and not self.note_broken:
                        self.note_broken = True
                        return {"content": [{"type": "text", "text": "thinking: broken note"}]}
                    return {"content": [{"type": "text", "text": json.dumps({"thinking": "check", "output": "next"})}]}
                if "section only" in system:
                    return {"content": [{"type": "text", "text": "section draft"}]}
                return {"content": [{"type": "text", "text": "final ok"}]}
            if model_name == "reviewer":
                assert tools == []
                if malformed_phase == "reviewer" and not self.review_broken:
                    self.review_broken = True
                    return {"content": [{"type": "text", "text": "pass yes score 91"}]}
                return {"content": [{"type": "text", "text": json.dumps({"pass": True, "score": 91, "issues": [], "required_changes": []})}]}
            raise AssertionError(model_name)

    provider = DemoProvider()

    def fake_resolve(self, model):
        if model.startswith("demo/"):
            return provider, model.split("/", 1)[1]
        raise AssertionError(model)

    monkeypatch.setattr(AIClient, "resolve_provider", fake_resolve)
    response = AIClient().complete(
        "modelpack/repair-pack",
        [{"role": "user", "content": "implement a larger change"}],
        [],
        {"deepthink_enabled": True},
    )
    return response, provider


@pytest.mark.skip(reason=_V4_DIRECT_PROVIDER_TEST_REASON)
def test_deepthink_repairs_malformed_planner_json(monkeypatch, tmp_path):
    response, provider = _run_deepthink_json_repair_case(monkeypatch, tmp_path, "planner")
    process = response["metadata"]["rumi_process"]

    assert response["content"][0]["text"] == "final ok"
    assert process["deepthink"]["plan"]["structure"] == ["Repaired"]
    assert any(event["phase"] == "json_repair" and "planner JSON repair" in event["metadata"]["label"] for event in process["events"])
    assert provider.plan_broken is True


@pytest.mark.skip(reason=_V4_DIRECT_PROVIDER_TEST_REASON)
def test_deepthink_repairs_malformed_public_note_json(monkeypatch, tmp_path):
    response, provider = _run_deepthink_json_repair_case(monkeypatch, tmp_path, "note")
    process = response["metadata"]["rumi_process"]

    assert response["content"][0]["text"] == "final ok"
    assert {"thinking": "repaired note", "output": "safe note"} in process["deepthink"]["notes"]
    assert any(event["phase"] == "json_repair" and "public note JSON repair" in event["metadata"]["label"] for event in process["events"])
    assert provider.note_broken is True


@pytest.mark.skip(reason=_V4_DIRECT_PROVIDER_TEST_REASON)
def test_deepthink_repairs_malformed_reviewer_json(monkeypatch, tmp_path):
    response, provider = _run_deepthink_json_repair_case(monkeypatch, tmp_path, "reviewer")
    process = response["metadata"]["rumi_process"]

    assert response["content"][0]["text"] == "final ok"
    assert process["review"]["approved"] is True
    assert any(event["phase"] == "json_repair" and "reviewer JSON repair" in event["metadata"]["label"] for event in process["events"])
    assert provider.review_broken is True


def test_rumi_harness_tool_selection_only_adds_vision_tools_for_model_visible_images():
    from domain.ai_client import rumi_process

    without_images = rumi_process.select_harness_tools([{"role": "user", "content": "hello"}], [])
    with_images = rumi_process.select_harness_tools(
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]}],
        [],
    )

    assert without_images["vision_tool_ids"] == []
    assert "vision_zoom" in with_images["vision_tool_ids"]
    assert with_images["separate_from_model_tools"] is True


@pytest.mark.skip(reason=_V4_DIRECT_PROVIDER_TEST_REASON)
def test_builtin_rumi_model_pack_uses_available_runtime_model(monkeypatch, tmp_path):
    settings_path = tmp_path / "frontend_settings.json"
    settings_path.write_text(json.dumps({"models": {}}), encoding="utf-8")
    monkeypatch.setattr(AIClient, "_settings_path", lambda self: settings_path)

    class DemoProvider:
        def __init__(self):
            self.calls = []

        def complete(self, model_name, messages, tools, params):
            self.calls.append({"model": model_name, "messages": messages, "tools": tools, "params": params})
            return {"content": [{"type": "text", "text": "DRAFT_RESPONSE: runtime ok"}]}

    provider = DemoProvider()
    client = AIClient()
    client._providers = {"stub": object(), "google": provider}
    monkeypatch.setattr(AIClient, "_provider_requires_authority", staticmethod(lambda *args, **kwargs: False))

    monkeypatch.setattr(
        AIClient,
        "list_models",
        lambda self: [{"id": "google/gemini-2.5-flash", "provider": "google", "configured": True}],
    )

    def fake_resolve(self, model):
        if model == "google/gemini-2.5-flash":
            return provider, "gemini-2.5-flash"
        raise AssertionError(model)

    monkeypatch.setattr(AIClient, "resolve_provider", fake_resolve)

    response = client.complete("modelpack/rumi", [{"role": "user", "content": "hello"}], [], {})

    assert response["content"][0]["text"] == "runtime ok"
    assert response["metadata"]["rumi_process"]["base_model"] == "google/gemini-2.5-flash"
    assert response["metadata"]["rumi_process"]["intended_base_model"] == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert response["metadata"]["rumi_process"]["resolved_base_model"] == "google/gemini-2.5-flash"
    assert response["metadata"]["rumi_process"]["fallback_reason"] == "intended_base_model_unavailable_using_active_provider_fallback"
    assert [call["model"] for call in provider.calls] == ["gemini-2.5-flash"]


def test_builtin_rumi_explicit_override_wins_for_review_chain_and_deepthink(monkeypatch):
    client = AIClient()
    captured = []

    def fake_review_chain(self, composite, members, messages, tools=None, params=None):
        captured.append(
            {
                "models": [member["model"] for member in members],
                "deepthink_enabled": bool((params or {}).get("deepthink_enabled")),
            }
        )
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(AIClient, "_complete_review_chain", fake_review_chain)
    default_pack = ModelPack.from_dict(default_rumi_model_pack(base_model="openai/default-chat"))

    for deepthink_enabled in (False, True):
        response = client._complete_model_pack(
            default_pack,
            [{"role": "user", "content": "hello"}],
            [],
            {
                "deepthink_enabled": deepthink_enabled,
                "rumi_base_model_override": "anthropic/explicit-override",
            },
        )
        assert response["content"][0]["text"] == "ok"

    assert captured == [
        {
            "models": ["anthropic/explicit-override", "anthropic/explicit-override"],
            "deepthink_enabled": False,
        },
        {
            "models": ["anthropic/explicit-override", "anthropic/explicit-override"],
            "deepthink_enabled": True,
        },
    ]


def test_model_pack_store_materializes_builtin_rumi_with_runtime_base_model(monkeypatch):
    monkeypatch.setattr(
        ModelRuntimeSettingsService,
        "_runtime_rumi_base_model",
        lambda self, settings=None: "google/gemini-2.5-flash",
    )

    pack = ModelPackStore({"model_packs": [], "composite_models": []}).get("modelpack/rumi")

    assert pack is not None
    assert [member.model for member in pack.members] == ["google/gemini-2.5-flash", "google/gemini-2.5-flash"]
    assert pack.metadata["base_model"] == "google/gemini-2.5-flash"
    assert pack.metadata["intended_base_model"] == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert pack.metadata["resolved_base_model"] == "google/gemini-2.5-flash"
    assert pack.metadata["fallback_reason"] == "intended_base_model_unavailable_using_active_provider_fallback"


def test_rumi_provider_default_is_not_process_model():
    from domain.ai_client.providers.rumi_provider import RumiProvider

    seen = {}

    class FakeClient:
        def __init__(self):
            self._providers = {"openai": object()}

        def complete(self, model, messages, tools, params):
            seen["model"] = model
            seen["params"] = params
            return {"content": [{"type": "text", "text": "fallback"}]}

    provider = RumiProvider(FakeClient())

    assert RumiProvider._is_rumi_process_model("rumi/default") is False
    assert RumiProvider._is_rumi_process_model("rumi/rumi") is True
    assert RumiProvider._is_rumi_process_model("rumi/mimo") is True

    response = provider.complete("default", [{"role": "user", "content": "hi"}], [], {})

    assert response["content"][0]["text"] == "fallback"
    assert seen["model"] == "openai/gpt-4o"


def test_rumi_provider_mimo_requires_intended_base_model():
    from domain.ai_client.providers.rumi_provider import RumiProvider

    seen = {}

    class FakeClient:
        def __init__(self):
            self._providers = {}

        def complete(self, model, messages, tools, params):
            seen["model"] = model
            seen["params"] = params
            return {"content": [{"type": "text", "text": "process"}]}

    provider = RumiProvider(FakeClient())

    response = provider.complete("mimo", [{"role": "user", "content": "hi"}], [], {})

    assert response["content"][0]["text"] == "process"
    assert seen["model"] == "modelpack/rumi"
    assert seen["params"]["rumi_base_model_override"] == "xiaomi-token-plan-sgp/mimo-v2.5-pro"
    assert seen["params"]["rumi_require_intended_base_model"] is True


def test_model_call_uses_required_capabilities(monkeypatch):
    seen: dict[str, object] = {}

    def fake_route(request, profiles=None):
        del profiles
        seen["requires_tool_calling"] = request.requires_tool_calling
        return _fake_route_decision("demo/tool")

    monkeypatch.setattr("domain.ai_client.model_call.route_model_request", fake_route)
    monkeypatch.setattr("domain.ai_client.model_call.get_model_capabilities", lambda model: {"supports_tool_calling": True})
    monkeypatch.setattr("domain.ai_client.model_call.LLMGateway.complete", lambda self, request: {"content": [{"type": "text", "text": "ok"}]})

    result = call_model({"question": "hello", "required_capabilities": ["model.tool_calling"]})

    assert result["status"] == "ok"
    assert result["model"] == "demo/tool"
    assert seen["requires_tool_calling"] is True


def test_model_call_requires_image_input_routes_to_vision_model(monkeypatch):
    seen: dict[str, object] = {}

    def fake_route(request, profiles=None):
        del profiles
        seen["has_images"] = request.has_images
        return _fake_route_decision("demo/vision")

    monkeypatch.setattr("domain.ai_client.model_call.route_model_request", fake_route)
    monkeypatch.setattr("domain.ai_client.model_call.get_model_capabilities", lambda model: {"supports_vision": True, "supports_image_input": True})
    monkeypatch.setattr("domain.ai_client.model_call.LLMGateway.complete", lambda self, request: {"content": [{"type": "text", "text": "ok"}]})

    result = call_model({"question": "hello", "required_capabilities": ["model.image_input"]})

    assert result["status"] == "ok"
    assert result["model"] == "demo/vision"
    assert seen["has_images"] is True


def test_model_call_uses_fast_required_capability(monkeypatch):
    seen: dict[str, object] = {}

    def fake_route(request, profiles=None):
        del profiles
        seen["requires_fast"] = request.requires_fast
        return _fake_route_decision("demo/fast")

    monkeypatch.setattr("domain.ai_client.model_call.route_model_request", fake_route)
    monkeypatch.setattr("domain.ai_client.model_call.get_model_capabilities", lambda model: {"supports_fast": True})
    monkeypatch.setattr("domain.ai_client.model_call.LLMGateway.complete", lambda self, request: {"content": [{"type": "text", "text": "ok"}]})

    result = call_model({"question": "hello", "required_capabilities": ["model.fast"]})

    assert result["status"] == "ok"
    assert seen["requires_fast"] is True


def test_model_call_errors_when_required_capability_unavailable(monkeypatch):
    def fake_route(request, profiles=None):
        del request, profiles
        return _fake_route_decision("demo/text")

    def fail_complete(self, request):
        raise AssertionError("LLM should not be called when capabilities are unsatisfied")

    monkeypatch.setattr("domain.ai_client.model_call.route_model_request", fake_route)
    monkeypatch.setattr("domain.ai_client.model_call.get_model_capabilities", lambda model: {"supports_vision": False, "supports_image_input": False})
    monkeypatch.setattr("domain.ai_client.model_call.LLMGateway.complete", fail_complete)

    result = call_model({"question": "hello", "required_capabilities": ["model.image_input"]})

    assert result["status"] == "error"
    assert result["code"] == "MODEL_CAPABILITY_UNSATISFIED"
    assert result["missing_capabilities"] == ["model.image_input"]


def test_model_call_does_not_forward_secrets(monkeypatch):
    seen: dict[str, object] = {}

    def fake_complete(self, request):
        seen["messages"] = request["messages"]
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr("domain.ai_client.model_call.LLMGateway.complete", fake_complete)

    result = call_model(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "hello",
                    "metadata": {"api_key": "secret", "safe": "ok"},
                }
            ]
        }
    )

    assert result["status"] == "ok"
    assert "api_key" not in json.dumps(seen["messages"], ensure_ascii=False)


def test_model_switch_updates_conversation_default(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation(tmp_path)

    result = dispatch_input(
        {
            "delivery": {"action_id": "model.switch"},
            "target": {"conversation_id": conversation["id"]},
            "params": {"model": "demo/next"},
        },
        {},
    )

    assert result["status"] == "ok"
    assert ChatStore().get_conversation(conversation["id"])["model"] == "demo/next"


def test_model_route_is_turn_scoped(monkeypatch, tmp_path):
    _configure_paths(monkeypatch, tmp_path)
    conversation = _conversation(tmp_path)
    dispatch_input(
        {
            "delivery": {"action_id": "model.route"},
            "target": {"conversation_id": conversation["id"]},
            "params": {"model": "demo/route-once"},
        },
        {},
    )

    monkeypatch.setattr("domain.chat.run_request.route_model_request", lambda request: _fake_route_decision(request.preferred_model))
    monkeypatch.setattr("domain.chat.run_request.get_model_capabilities", lambda model: {"supports_thinking": True, "supports_tool_calling": False})

    prepared = prepare_chat_run(
        {"conversation_id": conversation["id"], "message": {"role": "user", "content": "hello"}},
        {},
    )

    assert prepared.model == "demo/route-once"
    assert "turn_model_route_override" not in (ChatStore().get_conversation(conversation["id"]).get("metadata") or {})


def test_composite_models_compat_with_model_pack():
    store = ModelPackStore({"composite_models": [{"id": "legacy-pack", "members": [{"model": "demo/text"}]}]})

    pack = store.get("modelpack/legacy-pack")

    assert pack is not None
    assert pack.source == "composite_compat"
