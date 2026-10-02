"""Browser execution stays inside captured Host authority and owner state."""

from __future__ import annotations

import json
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_browser_host_service_pack.runtime import browser_tool, host_provider, service
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_protocol.canonical import canonical_digest


class _Invocation:
    def __init__(self, envelope: SimpleNamespace) -> None:
        self.envelope = envelope
        self.current = True

    def assert_current(self) -> None:
        if not self.current:
            raise PermissionError("capture was revoked")


def _capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    access: str = "control",
) -> tuple[Any, _Invocation, list[dict[str, Any]]]:
    factory = host_provider.BROWSER_HOST_PROVIDER_FACTORIES[
        f"{host_provider.PACK_ID}.browser-host.{access}"
    ]
    principal = OpaqueAuthorityRef("browser-principal")
    domain = OpaqueAuthorityRef("browser-domain")
    activation = {"activation_id": "activation-1"}
    key = (factory.contract_id, factory.operation_id, principal.value)
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=factory.function_id,
            implementation_digest="sha256:" + "1" * 64,
        ),
        artifact=SimpleNamespace(digest="sha256:" + "2" * 64),
        operation=SimpleNamespace(
            contract_id=factory.contract_id,
            operation_id=factory.operation_id,
            contract_version="1.0.0",
        ),
        principal_ref=principal,
    )
    context = SimpleNamespace(
        provider_bindings=(binding,),
        domain_ids={key: domain.value},
        profile_id="defaults",
        plan_digest="sha256:" + "3" * 64,
        security_epoch=7,
        activation=activation,
        user_data_root=tmp_path / "user-data",
        state_root=tmp_path / "host-state",
    )
    calls: list[dict[str, Any]] = []

    class RecordingRunner:
        def __init__(self, *, user_data_root: Path) -> None:
            self.user_data_root = user_data_root

        def run(self, action: str, arguments: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            calls.append({"action": action, "arguments": arguments, **kwargs})
            return {"action": action, "executed": True}

    monkeypatch.setattr(host_provider, "BrowserHostRunner", RecordingRunner)
    captured = factory.capture(context)
    envelope = SimpleNamespace(
        contract_id=factory.contract_id,
        contract_version="1.0.0",
        operation_id=factory.operation_id,
        target_principal=principal,
        target_domain=domain,
        payload={},
        context=SimpleNamespace(
            profile_id=context.profile_id,
            plan_digest=context.plan_digest,
            security_epoch=context.security_epoch,
            activation_id=activation["activation_id"],
            activation_digest=canonical_digest(activation),
        ),
        deadline_monotonic=time.monotonic() + 30,
    )
    return captured, _Invocation(envelope), calls


def _invoke(captured: Any, invocation: _Invocation, payload: dict[str, Any]) -> Any:
    invocation.envelope.payload = payload
    contribution = captured.contributions[0]
    return contribution.invoke(contribution.operation_id, payload, invocation)


@pytest.mark.parametrize(
    "operation",
    ["browser.profile.create", "browser.extensions.install", "browser.devtools.evaluate",
     "browser.network.capture"],
)
def test_observe_contract_never_reaches_mutating_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    captured, invocation, calls = _capture(tmp_path, monkeypatch, "observe")
    with pytest.raises(PermissionError, match="outside the authorized contract"):
        _invoke(captured, invocation, {"operation": operation, "arguments": {}})
    assert calls == []


@pytest.mark.parametrize("field", sorted(host_provider._FORBIDDEN_ARGUMENTS))
def test_client_cannot_supply_host_authority_or_output_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    captured, invocation, calls = _capture(tmp_path, monkeypatch)
    with pytest.raises(PermissionError, match="authority material"):
        _invoke(captured, invocation, {
            "operation": "browser.profile.create",
            "arguments": {"profile_id": "model", field: "forged"},
        })
    assert calls == []


@pytest.mark.parametrize(
    "field", ["target_principal", "target_domain", "profile_id", "plan_digest",
              "security_epoch", "activation_id", "activation_digest"],
)
def test_changed_authenticated_binding_cannot_execute_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    captured, invocation, calls = _capture(tmp_path, monkeypatch)
    if field in {"target_principal", "target_domain"}:
        setattr(invocation.envelope, field, OpaqueAuthorityRef("foreign"))
    else:
        setattr(invocation.envelope.context, field, "foreign")
    with pytest.raises(PermissionError, match="binding changed"):
        _invoke(captured, invocation, {
            "operation": "browser.profile.create", "arguments": {"profile_id": "model"},
        })
    assert calls == []


def test_owned_browser_runner_executes_once_without_recursive_host_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, invocation, calls = _capture(tmp_path, monkeypatch)
    result = _invoke(captured, invocation, {
        "operation": "browser.profile.create", "arguments": {"profile_id": "model"},
    })
    assert result == {"action": "browser.profile.create", "executed": True}
    assert len(calls) == 1
    assert calls[0]["viewer_host_approved"] is True
    assert calls[0]["arguments"] == {
        "profile_id": "model", "_rumi_contract_operation": "browser.profile.create",
    }
    assert calls[0]["artifact_root"].is_relative_to(tmp_path / "host-state")
    captured.close()
    with pytest.raises(PermissionError, match="binding changed"):
        _invoke(captured, invocation, {
            "operation": "browser.profile.create", "arguments": {"profile_id": "another"},
        })
    assert len(calls) == 1


@pytest.mark.parametrize("arguments,budget", [
    ({"duration_ms": 1000}, 0.1),
    ({}, 1.5),
])
def test_network_capture_cannot_outlive_authorized_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any], budget: float,
) -> None:
    monkeypatch.setattr(service, "CONTROL_OPERATIONS", service.CONTROL_OPERATIONS | {
        "browser.network.capture",
    })
    monkeypatch.setitem(service._HOST_FUNCTIONS, "browser.network.capture", "browser.network.capture")
    captured, invocation, calls = _capture(tmp_path, monkeypatch)
    invocation.envelope.deadline_monotonic = time.monotonic() + budget
    with pytest.raises(ValueError, match="request budget"):
        _invoke(captured, invocation, {
            "operation": "browser.network.capture", "arguments": arguments,
        })
    assert calls == []


def test_revoked_capture_cannot_reach_browser_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, invocation, calls = _capture(tmp_path, monkeypatch)
    invocation.current = False
    with pytest.raises(PermissionError, match="revoked"):
        _invoke(captured, invocation, {
            "operation": "browser.profile.create", "arguments": {"profile_id": "model"},
        })
    assert calls == []


def test_browser_capture_requires_host_owned_user_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _captured, _invocation, calls = _capture(tmp_path, monkeypatch)
    with pytest.raises(PermissionError, match="owner storage"):
        host_provider._bind_browser(SimpleNamespace(user_data_root=None), access="observe")
    assert calls == []


class _ToolClient:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.calls: list[dict[str, Any]] = []

    def providers(self, contract: str) -> tuple[dict[str, str], ...]:
        access = "observe" if contract == browser_tool._OBSERVE else "control"
        return ({
            "function_id": f"{browser_tool.PACK_ID}.browser-host.{access}",
            "operation_id": f"{browser_tool.PACK_ID}.browser-host-{access}",
            "provider_instance_id": f"browser.{access}", "backend_id": "captured-browser",
        },)

    def invoke(
        self, contract: str, operation: str, payload: dict[str, Any], **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append({"contract": contract, "operation": operation, "payload": payload, **kwargs})
        if self.deny:
            raise PermissionError("nested browser authority denied")
        return {
            "action": payload["operation"],
            "cookies": [{"name": "session", "value": "COOKIE_SECRET_CANARY"}],
            "evaluation": {"value": 42},
            "headers": {"authorization": "BEARER_SECRET_CANARY"},
        }


class _ToolInvocation:
    def __init__(self, client: _ToolClient) -> None:
        self.client = client

    def assert_current(self) -> None:
        pass

    def contract_client(self, **kwargs: Any) -> _ToolClient:
        assert kwargs == {
            "allowed_contract_ids": frozenset({browser_tool._OBSERVE, browser_tool._CONTROL}),
            "consumer_pack_id": browser_tool.PACK_ID, "include_credentials": False,
        }
        return self.client


@pytest.mark.parametrize("action,access", [
    ("browser.devtools.inspect", "observe"),
    ("browser.extensions.install", "control"),
    ("browser.network.capture", "control"),
])
def test_model_tool_selects_exact_nested_authority_and_redacts_cookie_values(
    action: str, access: str,
) -> None:
    client = _ToolClient()
    invoke = browser_tool._bind(None)
    result = invoke({
        "tool_id": "browser_managed", "tool_call_id": "call-browser",
        "arguments": {"action": action, "payload": {"profile_id": "model"}},
    }, _ToolInvocation(client))
    assert client.calls == [{
        "contract": browser_tool._OBSERVE if access == "observe" else browser_tool._CONTROL,
        "operation": f"{browser_tool.PACK_ID}.browser-host-{access}",
        "payload": {"operation": action, "arguments": {"profile_id": "model"}},
        "provider_instance_id": f"browser.{access}",
    }]
    value = json.loads(result["result"])
    assert value["evaluation"]["value"] == 42
    assert value["cookies"][0]["value"] == "[REDACTED]"
    assert value["headers"]["authorization"] == "[REDACTED]"
    assert "COOKIE_SECRET_CANARY" not in json.dumps(result)
    assert "BEARER_SECRET_CANARY" not in json.dumps(result)


def test_model_tool_propagates_nested_denial_without_retry_or_legacy_execution() -> None:
    client = _ToolClient(deny=True)
    invoke = browser_tool._bind(None)
    with pytest.raises(PermissionError, match="nested browser authority denied"):
        invoke({
            "tool_id": "browser_managed", "tool_call_id": "call-browser",
            "arguments": {"action": "browser.runtime.start"},
        }, _ToolInvocation(client))
    assert len(client.calls) == 1


def test_model_screenshot_exposes_transport_limit_without_forwarding_base64() -> None:
    class ScreenshotClient(_ToolClient):
        def invoke(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            result = super().invoke(*args, **kwargs)
            result["screenshot"] = {
                "data_url": "data:image/png;base64," + "SCREENSHOT_CANARY" * 100_000,
                "method": "browser_cdp", "coordinate_system": "viewport_pixels",
            }
            return result

    client = ScreenshotClient()
    result = browser_tool._bind(None)({
        "tool_id": "browser_managed", "tool_call_id": "model-screenshot",
        "arguments": {"action": "browser.capture.page"},
    }, _ToolInvocation(client))
    value = json.loads(result["result"])
    assert value["image_forwarding_supported"] is False
    assert "cannot forward images" in value["message"]
    assert value["screenshot"]["method"] == "browser_cdp"
    assert "data_url" not in value["screenshot"]
    assert "SCREENSHOT_CANARY" not in json.dumps(result)
    assert len(json.dumps(result)) < 2000


@pytest.mark.parametrize(
    "value", ["表" * 100_000, "\\" * 100_000], ids=["unicode", "escaped-json"],
)
def test_large_model_inspection_keeps_useful_preview_within_transport_budget(value: str) -> None:
    class LargeInspectionClient(_ToolClient):
        def invoke(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            result = super().invoke(*args, **kwargs)
            result["dom"] = {"nodeName": "BODY", "children": [{"nodeValue": value}]}
            return result

    result = browser_tool._bind(None)({
        "tool_id": "browser_managed", "tool_call_id": "model-inspection",
        "arguments": {"action": "browser.devtools.inspect"},
    }, _ToolInvocation(LargeInspectionClient()))
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) < 48 * 1024
    assert result["widget"] == {
        "type": "browser", "action": "browser.devtools.inspect", "truncated": True,
    }
    summary = json.loads(result["result"])
    assert summary["truncated"] is True
    assert '"nodeName":"BODY"' in summary["result_preview"]
    assert "COOKIE_SECRET_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("arguments", [
    {"action": "browser.devtools.send_command"},
    {"action": "computer.screenshot"},
    {"action": "browser.runtime.start", "approved": True},
    {"action": "browser.runtime.start", "payload": "not an object"},
])
def test_model_cannot_choose_unbounded_host_operation(arguments: dict[str, Any]) -> None:
    client = _ToolClient()
    invoke = browser_tool._bind(None)
    with pytest.raises(ValueError, match="tool action"):
        invoke({
            "tool_id": "browser_managed", "tool_call_id": "call-browser",
            "arguments": arguments,
        }, _ToolInvocation(client))
    assert client.calls == []


def test_production_browser_capture_executes_owned_state_and_fences_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_runtime.authority.v4 import AuthorityDenied
    from tests.conformance_support.host_profile import captured_host_profile
    from tobkiri_host.errors import AuthorizationError, ProviderExecutionError

    factories = host_provider.BROWSER_HOST_PROVIDER_FACTORIES
    os_browser_calls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *args, **kwargs: os_browser_calls.append(url))
    with captured_host_profile(
        tmp_path, monkeypatch, packs=(), edges=(), backends=(),
    ) as (session, authority):
        control = factories[f"{host_provider.PACK_ID}.browser-host.control"]
        observe = factories[f"{host_provider.PACK_ID}.browser-host.observe"]

        def invoke(factory: Any, operation: str, arguments: dict[str, Any]) -> Any:
            return session.invoke(factory.contract_id, factory.operation_id, {
                "operation": operation, "arguments": arguments,
                "_session_id": "browser-owner-test",
            })

        session.assert_operation_ready(control.contract_id, control.operation_id)
        stopped_navigation = invoke(control, "browser.navigate", {"url": "https://example.com"})
        assert stopped_navigation["is_error"] is True
        assert os_browser_calls == []
        result = invoke(control, "browser.profile.create", {
            "profile_id": "model", "label": "PROFILE_LABEL_PRIVATE_CANARY",
        })
        assert result["profile"]["profile_id"] == "model"
        assert result.get("type") != "host_intent"
        state = tmp_path / "user-data/browser_host/state.json"
        assert "model" in json.loads(state.read_text())["profiles"]
        listed = invoke(observe, "browser.profiles.list", {})
        assert "model" in {profile["profile_id"] for profile in listed["profiles"]}
        imported = invoke(control, "browser.cookies.import", {
            "profile_id": "model", "format": "json",
            "content": json.dumps([{
                "name": "session", "value": "COOKIE_IMPORT_SECRET_CANARY",
                "domain": "example.com",
            }]),
        })
        assert imported["imported"] == 1
        before = state.read_bytes()
        with pytest.raises(ProviderExecutionError):
            invoke(observe, "browser.profile.delete", {"profile_id": "model"})
        assert state.read_bytes() == before
        audit = json.dumps(authority.audit_events())
        assert "PROFILE_LABEL_PRIVATE_CANARY" not in audit
        assert "COOKIE_IMPORT_SECRET_CANARY" not in audit
        extension_ids = {
            record.host_extension_id for record in authority.list_provider_authorities()
            if record.provider.function_id == control.function_id
        }
        assert len(extension_ids) == 1
        assert session.authority_control is not None
        session.authority_control.revoke(
            target_kind="host_extension", target_id=extension_ids.pop(),
            reason="browser owner test revocation",
        )
        with pytest.raises(AuthorizationError, match="^static authorization failed$") as failure:
            invoke(control, "browser.profile.delete", {"profile_id": "model"})
        assert isinstance(failure.value.__cause__, AuthorityDenied)
        assert str(failure.value.__cause__) == "Host Extension trust is unavailable"
        assert state.read_bytes() == before


@pytest.mark.parametrize("browser_edge_selected", [True, False])
def test_production_model_tool_requires_browser_edge_through_real_registry_and_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    browser_edge_selected: bool,
) -> None:
    from tests.conformance_support.host_profile import captured_host_profile
    from tobkiri_host.errors import ProviderExecutionError

    tool_broker = "rumi_tool_broker_pack.tool-broker.invoke"
    os_browser_calls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url, *args, **kwargs: os_browser_calls.append(url))
    contract = "tobkiri.service.tool.invoke.v1"
    operation = "rumi_tool_broker_pack.tool-invoke"
    edge = {
        "caller_function_id": "shell.tauri.default",
        "target_provider_id": tool_broker,
        "contract_id": contract, "operation_id": operation,
        "authority_mode": "profile_grant",
        "requested_scope_template": {
            "capability": "operation.invoke",
            "dimensions": {"contract": [contract], "operation": [operation]},
            "quotas": {}, "exact_request_digest": None, "opaque": False,
        },
    }
    with captured_host_profile(
        tmp_path, monkeypatch, packs=(), edges=(edge,), backends=(),
        exclude_callers=() if browser_edge_selected else (browser_tool.FUNCTION,),
    ) as (session, _authority):
        request = {
            "tool_id": "browser_managed", "tool_call_id": "model-browser-call",
            "arguments": {
                "action": "browser.profile.create", "payload": {"profile_id": "model-tool"},
            },
            "_session_id": "model-browser-session",
        }
        state = tmp_path / "user-data/browser_host/state.json"
        if not browser_edge_selected:
            with pytest.raises(ProviderExecutionError):
                session.invoke(contract, operation, request)
            assert not state.exists()
            return
        result = session.invoke(contract, operation, request)
        assert result["tool_id"] == "browser_managed"
        assert result["is_error"] is False
        model_value = json.loads(result["result"])
        assert model_value["profile"]["profile_id"] == "model-tool"
        stopped_navigation = session.invoke(contract, operation, {
            **request,
            "arguments": {
                "action": "browser.open_url", "payload": {"url": "https://example.com"},
            },
        })
        assert stopped_navigation["is_error"] is True
        assert os_browser_calls == []
        assert "model-tool" in json.loads(state.read_bytes())["profiles"]
        session.invoke(contract, operation, {
            **request,
            "arguments": {
                "action": "browser.cookies.import",
                "payload": {
                    "profile_id": "model-tool",
                    "cookies": [{
                        "name": "session", "value": "COOKIE_MODEL_SECRET_CANARY",
                        "domain": "example.com",
                    }],
                },
            },
        })
        cookies = session.invoke(contract, operation, {
            **request,
            "arguments": {
                "action": "browser.cookies.list",
                "payload": {"profile_id": "model-tool"},
            },
        })
        assert "COOKIE_MODEL_SECRET_CANARY" not in json.dumps(cookies)
        assert "value" not in json.loads(cookies["result"])["cookies"][0]
        before = state.read_bytes()
        with pytest.raises(ProviderExecutionError):
            session.invoke(contract, operation, {
                **request,
                "arguments": {"action": "browser.devtools.send_command"},
            })
        assert state.read_bytes() == before
