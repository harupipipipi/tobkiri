from __future__ import annotations

import sys
import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_component_catalog_selected")


@pytest.fixture(autouse=True)
def _prefer_defaultspack_domain_package():
    for path in (str(DEFAULTSPACK_ROOT), str(ROOT)):
        while path in sys.path:
            sys.path.remove(path)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))
    domain_module = sys.modules.get("domain")
    domain_file = str(getattr(domain_module, "__file__", "") or "") if domain_module is not None else ""
    if domain_module is not None and str(DEFAULTSPACK_ROOT) not in domain_file:
        for name in [module_name for module_name in sys.modules if module_name == "domain" or module_name.startswith("domain.")]:
            sys.modules.pop(name, None)


def _computer_control_tool_def(tool_name: str) -> dict[str, object]:
    return {
        "tool_id": tool_name,
        "name": tool_name,
        "risk": "high",
        "requires_approval": True,
        "capability_grants": ["browser.control", "computer.control"],
        "execution": {
            "type": "rumi_function",
            "qualified_name": f"rumi_default_tools_pack:{tool_name}",
        },
    }


def _attached_plan_context(tool_name: str, **context: object) -> dict[str, object]:
    from core_runtime.capability_plan import canonical_capability_plan_digest
    from domain.tool.registry import ToolRegistry

    tool = ToolRegistry().get(tool_name)
    assert isinstance(tool, dict), tool_name
    schema = tool.get("schema")
    if not isinstance(schema, dict):
        contract = tool.get("contract")
        schema = (
            contract.get("input_schema")
            if isinstance(contract, dict)
            and isinstance(contract.get("input_schema"), dict)
            else {}
        )
    plan = {
        "schema_version": "tobkiri.capability-plan/v1",
        "plan_id": f"plan_computer_router_{tool_name}",
        "registry_revision": "registry_test",
        "effective_capabilities": [],
        "provider_selections": {},
        "tools": {
            "attached": [tool_name],
            "schema_hashes": {
                tool_name: hashlib.sha256(
                    json.dumps(
                        schema,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
            },
        },
    }
    plan["digest"] = canonical_capability_plan_digest(plan)
    return {"principal_id": "defaultspack", "capability_plan": plan, **context}


def _computer_router_module():
    from ecosystem.defaultspack.domain.host_bridge import computer_router

    return computer_router


def test_run_computer_action_wraps_controller_approval_with_request_id(monkeypatch) -> None:
    from domain.safety import approval

    computer_router = _computer_router_module()
    approval.reset_approval_state_for_tests()
    monkeypatch.setenv("RUMI_COMPUTER_HOST_INTERNAL", "1")

    class _FakeController:
        def __init__(self, artifact_root=None):
            self.artifact_root = artifact_root

        def run(self, action, payload, *, yolo_mode=False):
            return {
                "action": action,
                "requires_approval": True,
                "approval_token": "legacy-token",
                "approval_expires_in_seconds": 300,
                "payload": dict(payload),
            }

    monkeypatch.setattr(computer_router, "BrowserComputerController", _FakeController)

    result = computer_router.run_computer_action(
        "computer.click",
        {"x": 10, "y": 20},
        {"conversation_id": "conv_1"},
        tool_name="computer_use",
        tool_arguments={"action": "computer.click", "x": 10, "y": 20},
    )

    assert result["approval_required"] is True
    assert result["tool_name"] == "computer_use"
    assert result["action"] == "computer.click"
    assert str(result["approval_request_id"]).startswith("apr_")
    assert "承認してください" in result["message"]
    assert result["user_prompt"] == "承認してください"
    assert "表/前面で作業しますか" in result["message"]
    assert result["payload"] == {"x": 10, "y": 20}
    stored = approval.get_approval_request(result["approval_request_id"])
    assert stored["details"]["arguments"] == {
        "action": "computer.click",
        "payload": {"x": 10, "y": 20},
    }
    assert stored["details"]["payload"] == {"x": 10, "y": 20}


def test_tool_executor_local_computer_use_uses_router(monkeypatch) -> None:
    from domain.tool.executor import ToolExecutor

    captured: dict[str, object] = {}

    def fake_router(action, payload, context=None, *, tool_name="computer_use", tool_arguments=None, artifact_root=None, yolo_mode=False):
        captured["action"] = action
        captured["payload"] = dict(payload)
        captured["tool_name"] = tool_name
        captured["tool_arguments"] = dict(tool_arguments or {})
        captured["artifact_root"] = artifact_root
        captured["yolo_mode"] = yolo_mode
        return {"action": action, "routed": True}

    monkeypatch.setattr(_computer_router_module(), "run_computer_action", fake_router)

    result = ToolExecutor()._execute_local(
        "computer_use",
        {"action": "apps"},
        {"conversation_id": "conv_1", "conversation_workspace_dir": "/tmp/conversation/workspace"},
        tool_def=_computer_control_tool_def("computer_use"),
    )

    assert result["is_error"] is False
    assert captured["action"] == "computer.apps"
    assert captured["tool_name"] == "computer_use"
    assert captured["tool_arguments"] == {"action": "apps"}


def test_tool_executor_local_computer_use_accepts_context_approval_token(monkeypatch) -> None:
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    arguments = {"action": "apps"}
    request = approval.create_approval_request(
        "computer.apps",
        "high",
        {"action": "computer.apps"},
        details={"pack_id": "defaultspack", "conversation_id": "conv_1"},
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    captured: dict[str, object] = {}

    def fake_router(action, payload, context=None, *, tool_name="computer_use", tool_arguments=None, artifact_root=None, yolo_mode=False):
        captured["payload"] = dict(payload)
        captured["context"] = dict(context or {})
        captured["tool_arguments"] = dict(tool_arguments or {})
        return {"action": action, "routed": True}

    monkeypatch.setattr(_computer_router_module(), "run_computer_action", fake_router)

    result = ToolExecutor()._execute_local(
        "computer_use",
        arguments,
        {
            "conversation_id": "conv_1",
            "tool_approval_tokens": {
                "computer_use": decision["token"],
                "computer.apps": decision["token"],
            },
        },
        tool_def=_computer_control_tool_def("computer_use"),
    )

    assert result["is_error"] is False
    assert captured["payload"]["approval_token"] == decision["token"]
    assert captured["context"]["_tool_server_approved"] is True


def test_tool_executor_local_computer_use_maps_browser_open_url_alias(monkeypatch) -> None:
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    arguments = {"action": "browser_open_url", "url": "https://www.youtube.com", "app": "Vivaldi"}
    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        {"url": "https://www.youtube.com", "app": "Vivaldi"},
        details={"pack_id": "defaultspack", "conversation_id": "conv_1"},
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    captured: dict[str, object] = {}

    def fake_router(action, payload, context=None, *, tool_name="computer_use", tool_arguments=None, artifact_root=None, yolo_mode=False):
        captured["action"] = action
        captured["payload"] = dict(payload)
        captured["tool_name"] = tool_name
        captured["tool_arguments"] = dict(tool_arguments or {})
        return {"action": action, "routed": True}

    monkeypatch.setattr(_computer_router_module(), "run_computer_action", fake_router)

    result = ToolExecutor()._execute_local(
        "computer_use",
        arguments,
        {
            "conversation_id": "conv_1",
            "computer_use_target_app": "Vivaldi",
            "tool_approval_tokens": {
                "browser.open_url": decision["token"],
            },
        },
        tool_def=_computer_control_tool_def("computer_use"),
    )

    assert result["is_error"] is False
    assert captured["action"] == "browser.open_url"
    assert captured["payload"]["url"] == "https://www.youtube.com"
    assert captured["payload"]["app"] == "Vivaldi"
    assert captured["payload"]["approval_token"] == decision["token"]
    assert captured["tool_name"] == "computer_use"


def test_browser_computer_open_url_approval_uses_controller_payload() -> None:
    from domain.safety import approval
    from domain.tool.executor import _approval_required_tool_response_for_context

    approval.reset_approval_state_for_tests()
    response = _approval_required_tool_response_for_context(
        _computer_control_tool_def("browser_computer"),
        {"action": "browser.open_url", "payload": {"url": "https://www.google.com"}},
        {
            "conversation_id": "conv_1",
            "computer_use_target_app": "ChatGPT Atlas",
            "user_requested_computer_use": True,
        },
    )

    request = approval.get_approval_request(response["widget"]["approval_request_id"])
    expected_args = {
        "action": "browser.open_url",
        "payload": {
            "url": "https://www.google.com",
            "profile_id": "default",
            "persistent": False,
            "target_app": "ChatGPT Atlas",
        },
    }
    assert "foreground/on-screen" in response["result"]
    assert "foreground/on-screen" in response["message"]
    assert "foreground/on-screen" in response["widget"]["user_prompt"]
    assert "承認してください" in response["message"]
    assert "表/前面で作業しますか" in response["widget"]["user_prompt"]
    assert response["widget"]["recovery"]["recommended_next_actions"] == [
        "approve_request",
        "choose_foreground_work",
    ]
    assert request["details"]["arguments"] == expected_args
    assert request["args_hash"] == approval.hash_arguments(expected_args)
    assert response["widget"]["payload"] == expected_args["payload"]


def test_browser_computer_open_url_raw_replay_accepts_controller_payload_token() -> None:
    from domain.safety import approval
    from domain.tool.executor import _context_with_tool_approval_token

    approval.reset_approval_state_for_tests()
    request_args = {
        "action": "browser.open_url",
        "payload": {
            "url": "https://www.google.com",
            "profile_id": "default",
            "persistent": False,
            "target_app": "ChatGPT Atlas",
        },
    }
    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        request_args,
        details={"pack_id": "defaultspack", "conversation_id": "conv_1"},
    )
    decision = approval.approve(request["request_id"])

    context, error = _context_with_tool_approval_token(
        {
            "conversation_id": "conv_1",
            "computer_use_target_app": "ChatGPT Atlas",
            "user_requested_computer_use": True,
            "tool_approval_tokens": {"browser.open_url": decision["token"]},
        },
        _computer_control_tool_def("browser_computer"),
        {"action": "browser.open_url", "payload": {"url": "https://www.google.com"}},
    )

    assert error is None
    assert context["_tool_server_approval_token_valid"] is True
    assert context["_tool_server_approval_args_hash"] == request["args_hash"]


def test_tool_executor_local_computer_use_accepts_controller_shaped_browser_open_url_token(monkeypatch) -> None:
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    payload = {
        "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "profile_id": "default",
        "persistent": False,
        "target_app": "",
    }
    arguments = {"action": "browser.open_url", "payload": payload}
    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        {"action": "browser.open_url", "payload": payload},
        details={
            "tool_name": "computer_use",
            "action": "browser.open_url",
            "function_id": "browser.open_url",
            "pack_id": "defaultspack",
            "conversation_id": "conv_1",
        },
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    captured: dict[str, object] = {}

    def fake_router(action, payload, context=None, *, tool_name="computer_use", tool_arguments=None, artifact_root=None, yolo_mode=False):
        captured["action"] = action
        captured["payload"] = dict(payload)
        captured["context"] = dict(context or {})
        return {"action": action, "routed": True}

    monkeypatch.setattr(_computer_router_module(), "run_computer_action", fake_router)

    result = ToolExecutor()._execute_local(
        "computer_use",
        arguments,
        {
            "conversation_id": "conv_1",
            "tool_approval_tokens": {"browser.open_url": decision["token"]},
        },
        tool_def=_computer_control_tool_def("computer_use"),
    )

    assert result["is_error"] is False
    assert captured["action"] == "browser.open_url"
    assert captured["payload"]["url"] == payload["url"]
    assert captured["payload"]["approval_token"] == decision["token"]
    assert captured["context"]["_tool_server_approved"] is True


def test_rumi_function_computer_use_does_not_double_consume_forwarded_approval_token() -> None:
    from types import SimpleNamespace

    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    payload = {
        "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "profile_id": "default",
        "persistent": False,
        "target_app": "Vivaldi",
    }
    arguments = {"action": "browser.open_url", "payload": payload}
    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        {"action": "browser.open_url", "payload": payload},
        details={
            "tool_name": "computer_use",
            "action": "browser.open_url",
            "function_id": "browser.open_url",
            "pack_id": "defaultspack",
            "conversation_id": "conv_1",
        },
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    class _FakeCapabilityExecutor:
        def execute(self, principal_id, request_payload):
            forwarded_context = request_payload.get("context") or {}
            token = forwarded_context["_tool_server_approval_token"]
            assert token == decision["token"]
            assert forwarded_context["_tool_server_approval_token_valid"] is True
            return SimpleNamespace(
                success=True,
                output={
                    "status": "ok",
                    "data": {
                        "result": "computer_use browser.open_url completed",
                        "is_error": False,
                        "widget": {"type": "computer_use", "action": "browser.open_url"},
                    },
                },
                error=None,
                error_type="",
            )

    result = ToolExecutor()._execute_rumi_function(
        _computer_control_tool_def("computer_use"),
        arguments,
        {
            "conversation_id": "conv_1",
            "tool_approval_tokens": {"browser.open_url": decision["token"]},
            "capability_executor": _FakeCapabilityExecutor(),
        },
    )

    assert result["is_error"] is False
    assert result["result"] == "computer_use browser.open_url completed"
    assert approval.get_approval_request(request["request_id"])["status"] == "consumed"


def test_computer_key_approval_response_uses_controller_arguments() -> None:
    from domain.safety import approval
    from domain.tool.executor import _approval_required_tool_response

    approval.reset_approval_state_for_tests()

    result = _approval_required_tool_response(
        _computer_control_tool_def("computer_use"),
        {"action": "key", "key": "k"},
        {"conversation_id": "conv_1"},
    )

    request_id = result["widget"]["approval_request_id"]
    stored = approval.get_approval_request(request_id)
    assert stored["operation"] == "computer.key"
    assert stored["args_hash"] == approval.hash_arguments(
        {"action": "computer.key", "payload": {"key": "k"}}
    )
    assert stored["details"]["arguments"] == {"action": "computer.key", "payload": {"key": "k"}}


def test_computer_approval_hash_ignores_haze_sequence_id() -> None:
    from domain.safety import approval

    approved = {"action": "computer.show_app", "payload": {"app": "Vivaldi"}}
    with_sequence = {
        "action": "computer.show_app",
        "payload": {
            "app": "Vivaldi",
            "computer_use_haze_sequence_id": "run_1",
        },
    }

    assert approval.hash_arguments(with_sequence) == approval.hash_arguments(approved)


def test_tool_executor_local_computer_use_promotes_text_url_for_browser_open_url(monkeypatch) -> None:
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    arguments = {"action": "browser_open_url", "app": "Vivaldi", "text": "https://www.youtube.com"}
    approval_args = {"app": "Vivaldi", "text": "https://www.youtube.com", "url": "https://www.youtube.com"}
    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        approval_args,
        details={"pack_id": "defaultspack", "conversation_id": "conv_1"},
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    captured: dict[str, object] = {}

    def fake_router(action, payload, context=None, *, tool_name="computer_use", tool_arguments=None, artifact_root=None, yolo_mode=False):
        captured["action"] = action
        captured["payload"] = dict(payload)
        return {"action": action, "routed": True}

    monkeypatch.setattr(_computer_router_module(), "run_computer_action", fake_router)

    result = ToolExecutor()._execute_local(
        "computer_use",
        arguments,
        {
            "conversation_id": "conv_1",
            "computer_use_target_app": "Vivaldi",
            "tool_approval_tokens": {"browser.open_url": decision["token"]},
        },
        tool_def=_computer_control_tool_def("computer_use"),
    )

    assert result["is_error"] is False
    assert captured["action"] == "browser.open_url"
    assert captured["payload"]["url"] == "https://www.youtube.com"
    assert captured["payload"]["text"] == "https://www.youtube.com"


def test_tool_executor_local_computer_use_promotes_value_url_for_browser_open_url(monkeypatch) -> None:
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()
    arguments = {"action": "browser_open_url", "app": "Vivaldi", "value": "https://www.youtube.com"}
    approval_args = {"app": "Vivaldi", "value": "https://www.youtube.com", "url": "https://www.youtube.com"}
    request = approval.create_approval_request(
        "browser.open_url",
        "high",
        approval_args,
        details={"pack_id": "defaultspack", "conversation_id": "conv_1"},
    )
    decision = approval.approve(request["request_id"])
    assert decision["approved"] is True

    captured: dict[str, object] = {}

    def fake_router(action, payload, context=None, *, tool_name="computer_use", tool_arguments=None, artifact_root=None, yolo_mode=False):
        captured["action"] = action
        captured["payload"] = dict(payload)
        return {"action": action, "routed": True}

    monkeypatch.setattr(_computer_router_module(), "run_computer_action", fake_router)

    result = ToolExecutor()._execute_local(
        "computer_use",
        arguments,
        {
            "conversation_id": "conv_1",
            "computer_use_target_app": "Vivaldi",
            "tool_approval_tokens": {"browser.open_url": decision["token"]},
        },
        tool_def=_computer_control_tool_def("computer_use"),
    )

    assert result["is_error"] is False
    assert captured["action"] == "browser.open_url"
    assert captured["payload"]["url"] == "https://www.youtube.com"
    assert captured["payload"]["value"] == "https://www.youtube.com"


def test_tool_executor_local_computer_use_promotes_single_user_text_url_for_browser_open_url(monkeypatch) -> None:
    from domain.tool.executor import ToolExecutor

    arguments = {"action": "browser_open_url", "app": "Vivaldi", "text": "", "value": ""}
    captured: dict[str, object] = {}

    def fake_router(action, payload, context=None, *, tool_name="computer_use", tool_arguments=None, artifact_root=None, yolo_mode=False):
        captured["action"] = action
        captured["payload"] = dict(payload)
        return {"action": action, "routed": True}

    monkeypatch.setattr(_computer_router_module(), "run_computer_action", fake_router)

    result = ToolExecutor()._execute_local(
        "computer_use",
        arguments,
        {
            "conversation_id": "conv_1",
            "computer_use_target_app": "Vivaldi",
            "user_requested_computer_use": True,
            "user_text": "Vivaldiで https://www.youtube.com/watch?v=jNQXAC9IVRw を開いて。",
        },
        tool_def=_computer_control_tool_def("computer_use"),
    )

    assert result["is_error"] is False
    assert captured["action"] == "browser.open_url"
    assert captured["payload"]["app"] == "Vivaldi"
    assert captured["payload"]["url"] == "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def test_user_requested_mouse_keyboard_click_preflight_stores_physical_true() -> None:
    from domain.safety import approval
    from domain.tool.executor import _preflight_user_requested_computer_approval

    approval.reset_approval_state_for_tests()

    result = _preflight_user_requested_computer_approval(
        "computer_use",
        _computer_control_tool_def("computer_use"),
        {"action": "click", "x": 100, "y": 200},
        {
            "conversation_id": "conv_1",
            "computer_use_target_app": "Vivaldi",
            "computer_use_target_title": "YouTube",
            "computer_use_physical_clicks": True,
            "user_requested_computer_use": True,
            "user_text": "Vivaldiをマウスで操作してYouTubeを再生して",
        },
    )

    assert result is not None
    request_id = result["widget"]["approval_request_id"]
    stored = approval.get_approval_request(request_id)
    assert stored["operation"] == "computer.click"
    assert stored["details"]["arguments"] == {
        "action": "computer.click",
        "payload": {
            "app": "Vivaldi",
            "title": "YouTube",
            "physical": True,
            "x": 100,
            "y": 200,
        },
    }


def test_computer_use_context_url_fallback_requires_single_url() -> None:
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "browser.open_url",
        {"app": "Vivaldi"},
        {"user_text": "Compare https://example.com and https://www.youtube.com"},
    )

    assert "url" not in payload


def test_computer_use_context_url_fallback_uses_conversation_user_text() -> None:
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "browser.open_url",
        {},
        {
            "user_text": "承認しました。続行してください。",
            "conversation_user_text": "Vivaldiで https://www.youtube.com/watch?v=jNQXAC9IVRw を開いて。",
        },
    )

    assert payload["url"] == "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def test_computer_use_target_app_overrides_browser_alias_for_open_url() -> None:
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "browser.open_url",
        {"url": "https://www.youtube.com", "app": "Google Chrome"},
        {"computer_use_target_app": "ChatGPT Atlas", "user_requested_computer_use": True},
    )

    assert payload["app"] == "ChatGPT Atlas"
    assert payload["url"] == "https://www.youtube.com"


def test_computer_use_payload_preserves_background_and_foreground_fallback() -> None:
    from domain.tool.executor import _browser_computer_action_payload

    action, payload = _browser_computer_action_payload(
        "computer_use",
        {
            "action": "key",
            "key_combo": "return",
            "background": True,
            "foreground": True,
            "fallback": "foreground",
        },
    )

    assert action == "computer.key"
    assert payload["background"] is True
    assert payload["foreground"] is True
    assert payload["fallback"] == "foreground"


def test_missing_computer_approval_message_mentions_foreground_choice() -> None:
    from domain.safety import approval
    from domain.tool.executor import _approval_required_tool_response

    approval.reset_approval_state_for_tests()

    result = _approval_required_tool_response(
        _computer_control_tool_def("computer_use"),
        {"action": "type", "text": "hello"},
        {"conversation_id": "conv_1", "user_requested_computer_use": True},
    )

    assert "foreground/on-screen" in result["message"]
    assert "承認してください" in result["widget"]["user_prompt"]
    assert "表/前面で作業しますか" in result["widget"]["user_prompt"]
    assert result["recovery"]["recommended_next_actions"] == [
        "approve_request",
        "choose_foreground_work",
    ]


def test_viewer_connection_required_message_mentions_approval_and_foreground(monkeypatch) -> None:
    computer_router = _computer_router_module()

    class MissingViewer:
        def available(self):
            return False

    monkeypatch.delenv("RUMI_COMPUTER_HOST_INTERNAL", raising=False)
    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(computer_router.ViewerBrokerClient, "from_environment", classmethod(lambda cls: MissingViewer()))

    result = computer_router.run_computer_action(
        "computer.observe",
        {},
        {"conversation_id": "conv_1"},
        tool_name="computer_use",
        yolo_mode=True,
    )

    assert result["is_error"] is True
    assert "foreground/on-screen" in result["message"]
    assert "承認してください" in result["message"]
    assert "表/前面で作業しますか" in result["user_prompt"]
    assert "Rumi Viewer" in result["user_prompt"]
    assert result["recovery"]["kind"] == "viewer_connection_required"
    assert result["recovery"]["requires_approval"] is True
    assert result["recovery"]["requires_viewer_connection"] is True


def test_viewer_broker_screenshot_result_recommends_type_and_key(monkeypatch) -> None:
    computer_router = _computer_router_module()

    class AvailableViewer:
        def available(self):
            return True

        def run_computer(self, function_id, args, context=None, artifact_root=None):
            return {
                "action": function_id,
                "ok": True,
                "target": {"app": "ChatGPT Atlas"},
                "image_size": {"width": 1200, "height": 800},
            }

    monkeypatch.delenv("RUMI_COMPUTER_HOST_INTERNAL", raising=False)
    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(computer_router.ViewerBrokerClient, "from_environment", classmethod(lambda cls: AvailableViewer()))

    result = computer_router.run_computer_action(
        "computer.screenshot",
        {"app": "ChatGPT Atlas"},
        {"conversation_id": "conv_1"},
        tool_name="browser_computer",
        yolo_mode=True,
    )

    assert result["action"] == "computer.screenshot"
    assert result["recommended_next_actions"][:2] == ["computer.type", "computer.key"]
    assert "normal approval gates still apply" in result["input_guidance"]


def test_viewer_router_preserves_posted_unverified_delivery_and_ax_readiness(monkeypatch) -> None:
    computer_router = _computer_router_module()

    class AvailableViewer:
        def available(self):
            return True

        def run_computer(self, function_id, args, context=None, artifact_root=None):
            return {
                "action": function_id,
                "is_error": True,
                "error_code": "TYPE_COMPLETION_NOT_VERIFIED",
                "executed": True,
                "delivered": True,
                "background": True,
                "completion_verified": False,
                "outcome": "posted_unverified",
                "verification_required": "screenshot",
                "ax_candidate": {
                    "driver_registered": True,
                    "driver_available": False,
                    "pyobjc_ax_import_available": False,
                    "ax_process_trusted": False,
                    "attempted": False,
                    "result_code": "AX_IMPORT_UNAVAILABLE",
                },
            }

    monkeypatch.delenv("RUMI_COMPUTER_HOST_INTERNAL", raising=False)
    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        computer_router.ViewerBrokerClient,
        "from_environment",
        classmethod(lambda cls: AvailableViewer()),
    )

    result = computer_router.run_computer_action(
        "computer.type",
        {"app": "ChatGPT Atlas", "fallback": "background", "text": "youtube"},
        {"conversation_id": "conv_1"},
        tool_name="browser_computer",
        yolo_mode=True,
    )

    assert result["executed"] is True
    assert result["delivered"] is True
    assert result["completion_verified"] is False
    assert result["outcome"] == "posted_unverified"
    assert result["verification_required"] == "screenshot"
    assert result["ax_candidate"]["result_code"] == "AX_IMPORT_UNAVAILABLE"


def test_computer_use_target_app_overrides_browser_alias_for_show_app() -> None:
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "computer.show_app",
        {"app": "Google Chrome"},
        {"computer_use_target_app": "ChatGPT Atlas", "user_requested_computer_use": True},
    )

    assert payload == {"app": "ChatGPT Atlas"}


def test_computer_use_target_app_keeps_non_browser_app() -> None:
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "computer.show_app",
        {"app": "LINE"},
        {"computer_use_target_app": "ChatGPT Atlas", "user_requested_computer_use": True},
    )

    assert payload == {"app": "LINE"}


def test_user_requested_computer_use_defaults_safe_input_to_background() -> None:
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "computer.type",
        {"text": "youtube"},
        {"computer_use_target_app": "Vivaldi", "user_requested_computer_use": True},
    )

    assert payload["app"] == "Vivaldi"
    assert payload["background"] is True


def test_atlas_user_requested_computer_use_prefers_visible_foreground_input() -> None:
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    payload = _computer_use_payload_with_context_defaults(
        "computer.type",
        {"text": "youtube", "background": True},
        {"computer_use_target_app": "ChatGPT Atlas", "user_requested_computer_use": True},
    )

    assert payload["app"] == "ChatGPT Atlas"
    assert payload["fallback"] == "foreground"
    assert "background" not in payload


def test_computer_use_debug_foreground_env_overrides_background_default(monkeypatch) -> None:
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    monkeypatch.setenv("RUMI_COMPUTER_USE_DEBUG_FOREGROUND", "1")

    payload = _computer_use_payload_with_context_defaults(
        "computer.key",
        {"key_combo": "return"},
        {"computer_use_target_app": "Vivaldi", "user_requested_computer_use": True},
    )

    assert payload["app"] == "Vivaldi"
    assert payload["fallback"] == "foreground"
    assert "background" not in payload


def test_user_requested_computer_use_background_default_respects_foreground_controls() -> None:
    from domain.tool.executor import _computer_use_payload_with_context_defaults

    foreground = _computer_use_payload_with_context_defaults(
        "computer.key",
        {"key_combo": "return", "fallback": "foreground"},
        {"user_requested_computer_use": True},
    )
    physical = _computer_use_payload_with_context_defaults(
        "computer.click",
        {"x": 10, "y": 20, "physical": True},
        {"user_requested_computer_use": True},
    )

    assert "background" not in foreground
    assert "background" not in physical


def test_user_requested_computer_preflight_rejects_empty_browser_open_url_without_approval() -> None:
    from domain.safety import approval
    from domain.tool.executor import _preflight_user_requested_computer_approval

    approval.reset_approval_state_for_tests()

    result = _preflight_user_requested_computer_approval(
        "computer_use",
        _computer_control_tool_def("computer_use"),
        {"action": "browser.open_url", "payload": {}},
        {
            "conversation_id": "conv_1",
            "computer_use_target_app": "Vivaldi",
            "user_requested_computer_use": True,
            "user_text": "Vivaldiでブラウザを操作して。",
        },
    )

    assert result is not None
    assert result["is_error"] is True
    assert result["widget"] is None
    assert result["rejected_by_tool_validation"] is True
    assert "non-empty url" in result["result"]
    assert approval.list_approval_requests() == []


def test_user_requested_computer_preflight_rejects_empty_show_app_without_approval() -> None:
    from domain.safety import approval
    from domain.tool.executor import _preflight_user_requested_computer_approval

    approval.reset_approval_state_for_tests()

    result = _preflight_user_requested_computer_approval(
        "computer_use",
        _computer_control_tool_def("computer_use"),
        {"action": "computer.show_app", "payload": {}},
        {
            "conversation_id": "conv_1",
            "user_requested_computer_use": True,
            "user_text": "アプリを前面に出して。",
        },
    )

    assert result is not None
    assert result["is_error"] is True
    assert result["widget"] is None
    assert result["rejected_by_tool_validation"] is True
    assert "app, application, or name" in result["result"]
    assert approval.list_approval_requests() == []


def test_user_requested_computer_preflight_stores_replayable_atlas_show_app() -> None:
    from domain.safety import approval
    from domain.tool.executor import _preflight_user_requested_computer_approval

    approval.reset_approval_state_for_tests()

    result = _preflight_user_requested_computer_approval(
        "computer_use",
        _computer_control_tool_def("computer_use"),
        {"action": "show_app"},
        {
            "conversation_id": "conv_1",
            "computer_use_target_app": "ChatGPT Atlas",
            "user_requested_computer_use": True,
            "user_text": "ChatGPT Atlasで操作して。",
        },
    )

    assert result is not None
    assert result["is_error"] is False
    request_id = result["widget"]["approval_request_id"]
    stored = approval.get_approval_request(request_id)
    assert stored["operation"] == "computer.show_app"
    replayable_arguments = {
        "action": "computer.show_app",
        "payload": {"app": "ChatGPT Atlas"},
    }
    assert stored["args_hash"] == approval.hash_arguments(replayable_arguments)
    assert stored["details"]["arguments"] == replayable_arguments


def test_user_requested_computer_preflight_approval_stores_replayable_context_url() -> None:
    from domain.safety import approval
    from domain.tool.executor import _preflight_user_requested_computer_approval

    approval.reset_approval_state_for_tests()

    result = _preflight_user_requested_computer_approval(
        "computer_use",
        _computer_control_tool_def("computer_use"),
        {"action": "browser_open_url YouTube", "open": True, "title": "YouTube"},
        {
            "conversation_id": "conv_1",
            "computer_use_target_app": "Vivaldi",
            "user_requested_computer_use": True,
            "user_text": "Vivaldiで https://www.youtube.com/watch?v=jNQXAC9IVRw を開いて。",
        },
    )

    assert result is not None
    request_id = result["widget"]["approval_request_id"]
    stored = approval.get_approval_request(request_id)
    assert stored["operation"] == "browser.open_url"
    assert stored["details"]["arguments"] == {
        "action": "browser.open_url",
        "payload": {
            "persistent": False,
            "profile_id": "default",
            "target_app": "Vivaldi",
            "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        },
    }


def test_tool_executor_permission_preflight_stores_replayable_browser_open_url() -> None:
    from domain.safety import approval
    from domain.tool.executor import ToolExecutor

    approval.reset_approval_state_for_tests()

    result = ToolExecutor().execute(
        "computer_use",
        {
            "action": "browser_open_url",
            "open": True,
            "target": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        },
        {
            "conversation_id": "conv_1",
            "profile_id": "defaultspack.operations_company",
            "user_requested_computer_use": True,
            "user_text": "Vivaldiで https://www.youtube.com/watch?v=jNQXAC9IVRw を開いて。",
            **_attached_plan_context("computer_use"),
        },
    )

    request_id = result["widget"]["approval_request_id"]
    stored = approval.get_approval_request(request_id)
    assert stored["operation"] == "browser.open_url"
    assert stored["details"]["arguments"] == {
        "action": "browser.open_url",
        "payload": {
            "persistent": False,
            "profile_id": "default",
            "target_app": "",
            "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        },
    }


def test_computer_use_pack_function_routes_original_arguments(monkeypatch) -> None:
    from ecosystem.rumi_default_tools_pack.functions.computer_use import main as computer_main
    from ecosystem.rumi_default_tools_pack.functions.computer_use.main import run

    captured: dict[str, object] = {}

    def fake_router(action, payload, context=None, **kwargs):
        captured["action"] = action
        captured["payload"] = dict(payload)
        captured["context"] = dict(context or {})
        captured.update(kwargs)
        return {"action": action, "tool_name": kwargs.get("tool_name", "computer_use"), "apps": [{"name": "Google Chrome"}]}

    def fake_browser(context, browser_args):
        routed = fake_router(
            browser_args["action"],
            browser_args["payload"],
            context,
            tool_name=browser_args["tool_name"],
            tool_arguments=browser_args["tool_arguments"],
        )
        return {
            "result": "computer_use computer.apps completed",
            "is_error": False,
            "widget": {"type": "computer_use", **routed},
        }

    monkeypatch.setattr(computer_main, "_run_browser_computer", fake_browser)

    result = run(
        {"conversation_workspace_dir": "/tmp/conversation/workspace"},
        {"action": "apps"},
    )

    assert result["is_error"] is False
    assert captured["action"] == "computer.apps"
    assert captured["tool_name"] == "computer_use"
    assert captured["tool_arguments"] == {"action": "apps"}
    assert result["widget"]["type"] == "computer_use"
