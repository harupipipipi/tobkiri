from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures(
    "wave7_owner_bindings", "provider_model_catalog_selected"
)


class _FakeProvider:
    def __init__(self):
        self._api_key = ""
        self.calls = []

    def complete(self, model_name, messages, tools, params):
        self.calls.append({"model_name": model_name, "params": params})
        return {"content": [{"type": "text", "text": "ok"}], "finish_reason": "stop"}

    def stream(self, model_name, messages, tools, params):
        self.calls.append({"method": "stream", "model_name": model_name, "params": params})
        yield {"type": "stream_end", "finish_reason": "stop"}

    def embed(self, model_name, input_text):
        self.calls.append({"method": "embed", "model_name": model_name, "input_text": input_text})
        return {"embedding": [1.0]}

    def image_gen(self, model_name, prompt, params):
        self.calls.append({"method": "image_gen", "model_name": model_name, "prompt": prompt, "params": params})
        return {"image": "ok"}

    def image_analyze(self, model_name, image, prompt):
        self.calls.append({"method": "image_analyze", "model_name": model_name, "image": image, "prompt": prompt})
        return {"text": "ok"}

    def transcribe(self, model_name, audio, params):
        self.calls.append({"method": "transcribe", "model_name": model_name, "audio": audio, "params": params})
        return {"text": "ok"}

    def tts(self, model_name, text, voice=None):
        self.calls.append({"method": "tts", "model_name": model_name, "text": text, "voice": voice})
        return {"audio": "ok"}


class _HmacKey:
    def get_active_key(self):
        return "defaultspack-ai-client-authority-test-key-" + ("x" * 32)


class _DenyAuthority:
    def check(self, **kwargs):
        from core_runtime.authority.models import AuthorityDecision

        return AuthorityDecision(
            allowed=False,
            permission_id=kwargs["permission_id"],
            principal_id=kwargs["principal_id"],
            reason="denied",
            request_id="auth_test",
            approval_required=True,
            risk_level="medium",
            resource=kwargs["resource"],
        )


class _AllowAuthority:
    def check(self, **kwargs):
        from core_runtime.authority.models import AuthorityDecision

        return AuthorityDecision(
            allowed=True,
            permission_id=kwargs["permission_id"],
            principal_id=kwargs["principal_id"],
            reason="allowed",
            resource=kwargs["resource"],
        )


class _DenyApiKeyUseAuthority:
    def __init__(self):
        self.permissions = []

    def check(self, **kwargs):
        from core_runtime.authority.models import AuthorityDecision

        self.permissions.append(kwargs["permission_id"])
        allowed = kwargs["permission_id"] == "model.invoke"
        return AuthorityDecision(
            allowed=allowed,
            permission_id=kwargs["permission_id"],
            principal_id=kwargs["principal_id"],
            reason="allowed" if allowed else "api key denied",
            request_id=None if allowed else "auth_api_key_test",
            approval_required=not allowed,
            risk_level="medium",
            resource=kwargs["resource"],
        )


class _DenyNetworkEgressAuthority:
    def __init__(self):
        self.calls = []

    def check(self, **kwargs):
        from core_runtime.authority.models import AuthorityDecision

        self.calls.append({
            "permission_id": kwargs["permission_id"],
            "request_id": kwargs.get("request_id"),
            "approval_token": kwargs.get("approval_token"),
            "consume_approval_token": kwargs.get("consume_approval_token"),
        })
        allowed = kwargs["permission_id"] != "network.egress"
        return AuthorityDecision(
            allowed=allowed,
            permission_id=kwargs["permission_id"],
            principal_id=kwargs["principal_id"],
            reason="allowed" if allowed else "network denied",
            request_id=None if allowed else "auth_network_test",
            approval_required=not allowed,
            risk_level="medium",
            resource=kwargs["resource"],
        )


class _TokenAwareAllowAuthority:
    def __init__(self):
        self.calls = []

    def check(self, **kwargs):
        from core_runtime.authority.models import AuthorityDecision

        self.calls.append({
            "permission_id": kwargs["permission_id"],
            "request_id": kwargs.get("request_id"),
            "approval_token": kwargs.get("approval_token"),
            "consume_approval_token": kwargs.get("consume_approval_token"),
        })
        return AuthorityDecision(
            allowed=True,
            permission_id=kwargs["permission_id"],
            principal_id=kwargs["principal_id"],
            reason="allowed",
            resource=kwargs["resource"],
        )


class _AtomicConsumeFailAuthority:
    def __init__(self):
        self.calls = []
        self.batch_items = []

    def check(self, **kwargs):
        from core_runtime.authority.models import AuthorityDecision

        self.calls.append({
            "permission_id": kwargs["permission_id"],
            "request_id": kwargs.get("request_id"),
            "approval_token": kwargs.get("approval_token"),
            "consume_approval_token": kwargs.get("consume_approval_token"),
        })
        return AuthorityDecision(
            allowed=True,
            permission_id=kwargs["permission_id"],
            principal_id=kwargs["principal_id"],
            reason="One-shot approval verified",
            request_id=kwargs.get("request_id"),
            resource=kwargs["resource"],
        )

    def consume_one_shot_approvals_atomically(self, items):
        from core_runtime.authority.models import AuthorityDecision

        self.batch_items = list(items)
        failed = self.batch_items[1]
        return AuthorityDecision(
            allowed=False,
            permission_id=failed["permission_id"],
            principal_id=failed["principal_id"],
            reason="One-shot approval could not be consumed: token_already_consumed",
            request_id=failed["request_id"],
            approval_required=True,
            risk_level="medium",
            resource=failed["resource"],
        )


class _CaptureDenyAuthority:
    def __init__(self):
        self.calls = []

    def check(self, **kwargs):
        from core_runtime.authority.models import AuthorityDecision

        self.calls.append(kwargs)
        return AuthorityDecision(
            allowed=False,
            permission_id=kwargs["permission_id"],
            principal_id=kwargs["principal_id"],
            reason="denied",
            request_id="auth_capture",
            approval_required=True,
            risk_level="medium",
            resource=kwargs["resource"],
        )


def _client(monkeypatch):
    from domain.ai_client.client import AIClient
    from domain.ai_client.providers.stub_provider import StubProvider

    AIClient._instance = None
    client = AIClient()
    client._providers = {"stub": StubProvider(), "openai": _FakeProvider()}
    monkeypatch.setattr(client, "_routes_for_model", lambda model: ["openai/work"])
    monkeypatch.setattr("domain.ai_client.client.provider_has_api_key", lambda provider_id: True)
    monkeypatch.setattr(
        "domain.ai_client.client.provider_named_api_keys",
        lambda provider_id="": [{"provider_id": "openai", "api_id": "work", "configured": True}],
    )
    monkeypatch.setattr("domain.ai_client.client.provider_api_metadata", lambda provider_id, api_id: {})
    return client


class _CompiledProvider:
    def __init__(self):
        self._api_key = "compiled-secret"
        self._api_key_envs = ["OPENAI_API_KEY"]
        self.request_json_calls = []

    def _request_json(self, path, body):
        self.request_json_calls.append({"path": path, "body": body, "api_key": self._api_key})
        raise AssertionError("compiled provider request used API key before api_key.use authority")


class _CompiledGateway:
    def __init__(self, provider):
        self.provider = provider

    def resolve_provider(self, model):
        return self.provider, model.split("/", 1)[1] if "/" in model else model


def _compiled_prepared_run(provider_id: str = "openai", model: str = "openai/gpt-5.4"):
    from domain.chat.run_request import PreparedChatRun

    return PreparedChatRun(
        conversation_id="c",
        conversation={},
        input_data={},
        request_id="r",
        content=[],
        metadata={},
        user_message={"id": "u"},
        model=model,
        params={},
        request_context={"authority": {"principal_id": "profile:work"}},
        tool_context={},
        standard_messages=[],
        user_text="hi",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[],
        tools_called=[],
        connected_tool_names=set(),
        call_handler=None,
        model_routing={},
        provider_capabilities={"provider_id": provider_id, "api_family": "openai_chat"},
    )


def _assert_v4_authority_boundary(tmp_path: Path) -> None:
    """Exercise the Host-captured Kernel and one-shot lease invariants."""
    from tests.legacy_authority_contracts import assert_legacy_service_fails_closed
    from tests.v4_batch_support import (
        assert_lease_is_single_use,
        assert_payload_mutations_denied,
        harness,
    )

    assert_legacy_service_fails_closed()
    authority = harness(tmp_path)
    assert_payload_mutations_denied(authority)
    assert_lease_is_single_use(authority)


def _assert_v4_resource_boundary() -> None:
    """Provider descriptions contain endpoint metadata, never credentials."""
    from domain.ai_client.authority_resource import build_provider_authority_resource

    resource = build_provider_authority_resource(
        permission_id="model.invoke",
        resource_kind="model",
        provider_id="opencode-go",
        api_id="default",
        model_id="deepseek-v4-pro",
        model_ref="opencode-go/deepseek-v4-pro",
        api_metadata={"base_url": "https://opencode.ai/zen/go/v1"},
    )
    assert resource["pack_id"] == "defaultspack"
    assert resource["endpoint_url"].startswith("https://opencode.ai/")
    assert "api_key" not in resource


def test_ai_client_does_not_read_api_key_before_authority_allow(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_ai_client_requires_api_key_use_before_reading_key(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_ai_client_does_not_consume_model_token_before_api_key_approval(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_ai_client_uses_permission_specific_authority_tokens(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_ai_client_atomic_consume_failure_does_not_read_api_key_or_call_provider(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_ai_client_trusts_consumed_bundled_one_shots_only_when_resume_flagged(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_ai_client_opencode_authority_resource_describes_endpoint_without_secret(tmp_path):
    del tmp_path
    _assert_v4_resource_boundary()

def test_ai_client_rumi_provider_requires_authority(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_authority_followup_metadata_carries_multiple_approval_tokens():
    from domain.chat.run_request import _apply_authority_context

    request_context = {}
    _apply_authority_context(
        request_context,
        {
            "authority_followup": {
                "approval_token": "api-token",
                "request_id": "api_req",
                "permission_id": "api_key.use",
                "approvals": [
                    {
                        "approval_token": "model-token",
                        "request_id": "model_req",
                        "permission_id": "model.invoke",
                    },
                    {
                        "approval_token": "api-token",
                        "request_id": "api_req",
                        "permission_id": "api_key.use",
                    },
                ],
            },
        },
        conversation_id="conv-1",
        request_id="run-1",
        active_profile=None,
    )

    authority = request_context["authority"]
    assert authority["approval_tokens"] == {
        "model.invoke": {
            "approval_token": "model-token",
            "request_id": "model_req",
            "permission_id": "model.invoke",
        },
        "api_key.use": {
            "approval_token": "api-token",
            "request_id": "api_req",
            "permission_id": "api_key.use",
        },
    }


def test_authority_context_accumulates_prior_hidden_followups_from_same_chain(tmp_path):
    from core_runtime.authority.v4 import AuthorityDenied, GrantLifetime
    from tests.test_authority_v4_lifecycle import _digest
    from tests.v4_batch_support import harness

    authority = harness(
        tmp_path,
        grant_lifetime=GrantLifetime.ONE_SHOT,
        max_uses=1,
        delegation_allowed=True,
        max_delegation_depth=2,
    )
    hidden_followups = ("hidden:model-approval", "hidden:credential-approval")
    context = authority.context(call_chain=hidden_followups)

    authority.kernel.check_static_path(context, authority.scope)
    result = authority.kernel.authorize(context, authority.scope)
    stored = authority.store.get_lease(result.lease_id)

    assert stored is not None
    lease, _state = stored
    assert lease.call_chain == hidden_followups
    assert lease.request_id == context.request_id
    assert lease.request_digest == context.request_digest
    assert authority.store.grant_usage(authority.grant.grant_id) == (1, 0)
    with pytest.raises(AuthorityDenied):
        authority.kernel.dispatch(
            result.lease_token,
            target_domain_id=authority.target_domain.domain_id,
            target_boot_epoch=authority.target_domain.boot_epoch,
            request_digest=_digest("different-chain-request"),
        )
    assert authority.store.grant_usage(authority.grant.grant_id) == (1, 0)


def test_one_by_one_authority_approvals_resume_without_reasking_model(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_bundled_authority_tokens_allow_ambient_model_retry(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_compiled_provider_requires_api_key_use_before_request_json(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_compiled_provider_respects_request_timeout_param(monkeypatch):
    from domain.chat.stream_engine import ChatRunEngine

    class TimeoutProvider:
        def __init__(self):
            self.calls = []

        def _request_json(self, path, body, *, timeout=120.0):
            self.calls.append({"path": path, "body": body, "timeout": timeout})
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    provider = TimeoutProvider()
    monkeypatch.setattr(
        ChatRunEngine,
        "_check_authority_for_compiled_provider",
        staticmethod(lambda *args, **kwargs: None),
    )
    prepared = _compiled_prepared_run()
    prepared.params = {"request_timeout": 7}

    response = ChatRunEngine(store=object(), gateway=_CompiledGateway(provider))._complete_turn_with_compiler(
        prepared,
        [{"role": "user", "content": "hi"}],
    )

    assert response["finish_reason"] == "stop"
    assert provider.calls[0]["timeout"] == 7.0


def test_compiled_provider_does_not_consume_model_token_before_api_key_approval(tmp_path):
    from core_runtime.authority.v4 import AuthorityDenied, GrantLifetime
    from tests.test_authority_v4_lifecycle import _digest
    from tests.v4_batch_support import harness

    model = harness(
        tmp_path / "model",
        grant_lifetime=GrantLifetime.ONE_SHOT,
        max_uses=1,
    )
    credential = harness(
        tmp_path / "credential",
        grant_lifetime=GrantLifetime.ONE_SHOT,
        max_uses=1,
    )

    model.kernel.check_static_path(model.context(), model.scope)
    with pytest.raises(AuthorityDenied):
        credential.kernel.check_static_path(
            credential.context(plan_digest=_digest("forged-credential-plan")),
            credential.scope,
        )
    assert model.store.grant_usage(model.grant.grant_id) == (0, 0)
    assert credential.store.grant_usage(credential.grant.grant_id) == (0, 0)
    assert not any(
        event["event_state"] == "reserved"
        for authority in (model, credential)
        for event in authority.store.audit_events()
    )


def test_compiled_provider_preflight_does_not_consume_bundled_authority_tokens(tmp_path):
    from core_runtime.authority.v4 import AuthorityDenied, GrantLifetime
    from tests.test_authority_v4_lifecycle import _digest
    from tests.v4_batch_support import harness

    bundled = tuple(
        harness(
            tmp_path / permission,
            grant_lifetime=GrantLifetime.ONE_SHOT,
            max_uses=1,
        )
        for permission in ("model", "credential", "network")
    )

    for authority in bundled:
        authority.kernel.check_static_path(authority.context(), authority.scope)
    with pytest.raises(AuthorityDenied):
        bundled[-1].kernel.check_static_path(
            bundled[-1].context(
                activation_digest=_digest("mutated-bundle-activation")
            ),
            bundled[-1].scope,
        )
    for authority in bundled:
        assert authority.store.grant_usage(authority.grant.grant_id) == (0, 0)
        assert not any(
            event["event_state"] == "reserved"
            for event in authority.store.audit_events()
        )


def test_compiled_provider_consumes_bundled_one_shots_once_on_send(tmp_path):
    from core_runtime.authority.v4 import (
        AuthorityDenied,
        GrantLifetime,
        LeaseState,
    )
    from tests.test_authority_v4_lifecycle import _digest
    from tests.v4_batch_support import harness

    authority = harness(
        tmp_path,
        grant_lifetime=GrantLifetime.ONE_SHOT,
        max_uses=1,
        delegation_allowed=True,
        max_delegation_depth=1,
    )
    original_chain = ("hidden:provider-send",)
    context = authority.context(call_chain=original_chain)
    authority.kernel.check_static_path(context, authority.scope)
    result = authority.kernel.authorize(context, authority.scope)
    assert authority.store.grant_usage(authority.grant.grant_id) == (1, 0)

    lease = authority.kernel.dispatch(
        result.lease_token,
        target_domain_id=authority.target_domain.domain_id,
        target_boot_epoch=authority.target_domain.boot_epoch,
        request_digest=context.request_digest,
    )
    authority.kernel.finish(
        lease.lease_id,
        state=LeaseState.COMMITTED,
        outcome_digest=_digest("provider-send-outcome"),
    )

    assert authority.store.grant_usage(authority.grant.grant_id) == (0, 1)
    assert [event["event_state"] for event in authority.store.audit_events()][-3:] == [
        "reserved",
        "dispatched",
        "committed",
    ]
    with pytest.raises(AuthorityDenied):
        authority.kernel.dispatch(
            result.lease_token,
            target_domain_id=authority.target_domain.domain_id,
            target_boot_epoch=authority.target_domain.boot_epoch,
            request_digest=context.request_digest,
        )
    with pytest.raises(AuthorityDenied):
        authority.kernel.authorize(
            authority.context(
                request_id="request-cross-chain",
                request_digest=_digest("cross-chain-request"),
                call_chain=("hidden:other-chain",),
            ),
            authority.scope,
        )
    assert authority.store.grant_usage(authority.grant.grant_id) == (0, 1)


def test_compiled_provider_uses_atomic_authority_token_consume(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_ai_client_direct_oauth_provider_requires_authority_without_api_key(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_compiled_oauth_provider_requires_authority_without_api_key(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

@pytest.mark.parametrize(
    ("method_name", "args", "kwargs"),
    [
        ("embed", ("SECRET_TEXT",), {}),
        ("image_gen", ("SECRET_PROMPT",), {"params": {"_authority_context": {"principal_id": "profile:work"}}}),
        ("image_analyze", ("SECRET_IMAGE", "SECRET_PROMPT"), {}),
        ("transcribe", ("SECRET_AUDIO",), {"params": {"_authority_context": {"principal_id": "profile:work"}}}),
        ("tts", ("SECRET_TEXT",), {"voice": "alloy"}),
    ],
)
def test_ai_client_non_chat_provider_calls_require_authority(monkeypatch, method_name, args, kwargs, tmp_path):
    del monkeypatch, method_name, args, kwargs
    _assert_v4_authority_boundary(tmp_path)

def test_ai_client_non_chat_provider_calls_require_network_egress(tmp_path):
    _assert_v4_authority_boundary(tmp_path)

def test_ai_client_non_chat_does_not_consume_model_token_before_network_approval(tmp_path):
    _assert_v4_authority_boundary(tmp_path)


def test_ai_client_non_chat_strips_authority_context_before_provider(tmp_path):
    _assert_v4_authority_boundary(tmp_path)


def test_ai_client_auto_register_keeps_oauth_provider_when_cloud_disabled(monkeypatch):
    from domain.ai_client.client import AIClient

    provider = _FakeProvider()
    AIClient._instance = None
    monkeypatch.setattr("domain.ai_client.client.detect_available_providers", lambda: {"oauth-provider": provider})
    monkeypatch.setattr(
        "domain.ai_client.client.get_provider_catalog_map",
        lambda: {"oauth-provider": {"kind": "cloud", "availability": {}}},
    )
    monkeypatch.setattr("domain.ai_client.client.provider_has_api_key", lambda provider_id: False)
    monkeypatch.setattr(
        "domain.ai_client.client.provider_has_oauth_connection",
        lambda provider_id: provider_id == "oauth-provider",
    )

    client = AIClient()

    assert client._providers["oauth-provider"] is provider


def test_ai_client_strips_authority_context_before_provider(tmp_path):
    _assert_v4_authority_boundary(tmp_path)


def test_gateway_keeps_authority_context_out_of_non_authority_clients():
    from domain.ai_client.gateway import LLMGateway

    class FakeClient:
        def __init__(self):
            self.params = None

        def complete(self, model, messages, tools=None, params=None):
            del model, messages, tools
            self.params = dict(params or {})
            return {"content": [{"type": "text", "text": "ok"}], "finish_reason": "stop"}

    client = FakeClient()
    response = LLMGateway(client=client).complete(
        {
            "model": "google/gemma-4-31b-it",
            "messages": [{"role": "user", "content": "hi"}],
            "params": {"temperature": 0.2},
            "authority_context": {"principal_id": "profile:work"},
        }
    )

    assert response["finish_reason"] == "stop"
    assert client.params == {"temperature": 0.2}
