"""Typed text-in/text-out Gateway node coverage for Flow composition."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from jsonschema import Draft202012Validator
import pytest

from core_runtime.authority.v4 import authority_digest
from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)
from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import (
    GENERATE_PROVIDER_CONTRACT,
    MODEL_PROFILE_CONTRACT,
    MODEL_PROFILE_GENERATE_OPERATION,
    REQUEST_PREPARE_CONTRACT,
    ROUTING_CONTRACT,
    TOOL_BRIDGE_CONTRACT,
    USAGE_CONTRACT,
    _GENERATE_ALLOWED_CONTRACTS,
)
from ecosystem.rumi_ai_gateway_pack.runtime.text_generate import (
    CONTRACT_ID,
    FUNCTION_ID,
    HOST_PROVIDER_FACTORY,
)
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_protocol.canonical import canonical_json

_FUNCTION_ID = FUNCTION_ID
_CONTRACT_ID = CONTRACT_ID
_DOMAIN_ID = "domain.provider.gateway-text-generate"


def _text_input_schema() -> Mapping[str, Any]:
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads(
        (root / "schemas" / "pack_v4_catalog.v1.json").read_text()
    )
    pack = next(
        item
        for item in catalog["packs"]
        if item["pack_id"] == "rumi_ai_gateway_pack"
    )
    contract = next(
        item
        for item in pack["provided_contracts"]
        if item["contract_id"] == _CONTRACT_ID
    )
    return contract["schemas"]["input"]


class _CapturedDispatch:
    """Deterministic V4 dispatch double with no provider fallback."""

    profile_id = "defaults"
    plan_digest = authority_digest({"plan": "gateway-text-generate"})
    profile: Mapping[str, Any] = {
        "model_id": "fixture/model",
        "enabled": True,
        "credential_handle": "private-handle",
    }
    resolved_profile_id = "profile.fixture"

    def __init__(self, *, configured_provider: bool) -> None:
        self._configured_provider = configured_provider
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def provider_metadata(
        self, contract_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        if contract_id == GENERATE_PROVIDER_CONTRACT and self._configured_provider:
            return (
                {
                    "provider_instance_id": "provider.fixture",
                    "operation_id": (
                        "rumi_provider_adapters_pack.provider-generate"
                    ),
                },
            )
        return ()

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, Any]:
        assert version_range is None
        self.calls.append((contract_id, operation_id, dict(payload)))
        if contract_id == MODEL_PROFILE_CONTRACT:
            return {
                "profile": dict(self.profile),
                "resolved_profile_id": self.resolved_profile_id,
            }
        if contract_id == REQUEST_PREPARE_CONTRACT:
            return {
                "request_id": "request.gateway-text-generate",
                "deadline": time.time() + 30,
                "messages": list(payload.get("messages") or ()),
                "model_profile_id": payload.get("model_profile_id"),
                "requirements": dict(payload.get("requirements") or {}),
                "allow_failover": bool(payload.get("allow_failover", False)),
            }
        if contract_id == ROUTING_CONTRACT:
            return {
                "candidates": [
                    {
                        "model_id": "fixture/model",
                        "execution_provider_instance_id": "provider.fixture",
                        "catalog_provider_instance_id": "catalog.fixture",
                        "catalog_revision": "catalog.fixture.v1",
                        "capabilities": [],
                        "modalities": ["text"],
                        "context_length": 1,
                        "descriptor": {
                            "provider_id": "fixture",
                            "provider_model_id": "model",
                            "currency": "USD",
                        },
                    }
                ],
                "excluded": [],
            }
        if contract_id == GENERATE_PROVIDER_CONTRACT:
            return {
                "status": "ok",
                "output": "canonical provider result",
                "finish_reason": "stop",
                "usage": {},
            }
        if contract_id == TOOL_BRIDGE_CONTRACT:
            return {"intents": []}
        if contract_id == USAGE_CONTRACT:
            return {"cost": 0, "currency": "USD"}
        raise AssertionError(f"undeclared Gateway dispatch: {contract_id}")


class _DisabledProfileDispatch(_CapturedDispatch):
    """The registry resolves the profile but marks it disabled."""

    profile = {
        "model_id": "fixture/model",
        "enabled": False,
        "credential_handle": "private-handle",
    }


class _UnknownProfileDispatch(_CapturedDispatch):
    """The registry cannot resolve the requested profile identifier."""

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, Any]:
        if contract_id == MODEL_PROFILE_CONTRACT:
            self.calls.append((contract_id, operation_id, dict(payload)))
            raise GlobalContractInvocationError(
                "unresolved_profile", "no such model profile"
            )
        return super().invoke(
            contract_id,
            operation_id,
            payload,
            version_range=version_range,
        )


class _MalformedResultDispatch(_CapturedDispatch):
    """The provider returns a result without a text output string."""

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, Any]:
        result = super().invoke(
            contract_id,
            operation_id,
            payload,
            version_range=version_range,
        )
        if contract_id == GENERATE_PROVIDER_CONTRACT:
            return {**result, "output": {"unexpected": "object"}}
        return result


class _OversizedResultDispatch(_CapturedDispatch):
    """The provider returns text beyond the typed output bound."""

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, Any]:
        result = super().invoke(
            contract_id,
            operation_id,
            payload,
            version_range=version_range,
        )
        if contract_id == GENERATE_PROVIDER_CONTRACT:
            return {**result, "output": "x" * (262_144 + 1)}
        return result


class _Invocation:
    """Records the restricted client requested by the captured Provider."""

    def __init__(self, dispatch: _CapturedDispatch) -> None:
        self._dispatch = dispatch
        self.requests: list[tuple[frozenset[str], str]] = []

    def contract_client(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
    ) -> GlobalContractClient:
        self.requests.append((allowed_contract_ids, consumer_pack_id))
        return GlobalContractClient(
            session=self._dispatch,
            allowed_contract_ids=allowed_contract_ids,
            consumer_pack_id=consumer_pack_id,
        )


def _binding(
    *,
    function_id: str = _FUNCTION_ID,
    contract_id: str = _CONTRACT_ID,
    operation_id: str = _FUNCTION_ID,
) -> Any:
    principal_id = authority_digest({"principal": function_id})
    return SimpleNamespace(
        function=SimpleNamespace(
            function_id=function_id,
            implementation_digest=authority_digest(
                {"implementation": function_id}
            ),
        ),
        operation=SimpleNamespace(
            contract_id=contract_id,
            contract_version="1.0.0",
            operation_id=operation_id,
        ),
        principal_ref=OpaqueAuthorityRef(principal_id),
        artifact=SimpleNamespace(
            digest=authority_digest({"artifact": function_id})
        ),
    )


def _captured_provider(binding: Any | None = None) -> tuple[Any, Any]:
    binding = _binding() if binding is None else binding
    captured = HOST_PROVIDER_FACTORY[_FUNCTION_ID].capture(
        HostProviderCaptureContextV4(
            profile_id="defaults",
            plan_digest=authority_digest({"plan": "gateway-text-generate"}),
            security_epoch=1,
            activation={"activation_id": "activation.gateway-text-generate"},
            state_root=Path("/tmp/gateway-text-generate-state"),
            provider_bindings=(binding,),
            catalog_bindings=(),
            domain_ids={
                (
                    binding.operation.contract_id,
                    binding.operation.operation_id,
                    binding.principal_ref.value,
                ): _DOMAIN_ID
            },
        )
    )
    assert len(captured.contributions) == 1
    return captured.contributions[0], binding


def test_text_generate_returns_typed_text_over_the_real_gateway_path() -> None:
    """The node delegates through the captured prepare/route/usage chain."""
    contribution, _ = _captured_provider()
    dispatch = _CapturedDispatch(configured_provider=True)
    invocation = _Invocation(dispatch)
    result = contribution.invoke(
        _FUNCTION_ID,
        {
            "text": "Summarize the meeting.",
            "model_profile_id": "profile.alias",
            "system_prompt": "Reply in one sentence.",
        },
        invocation,
    )

    assert result == {
        "status": "ok",
        "request_id": "request.gateway-text-generate",
        "model_id": "fixture/model",
        "model_profile_id": "profile.fixture",
        "provider_instance_id": "provider.fixture",
        "text": "canonical provider result",
    }
    # The credential handle resolved by the registry stays inside the
    # Gateway boundary and never appears in the typed projection.
    assert "private-handle" not in repr(result)
    canonical_json(result)

    assert invocation.requests == [
        (_GENERATE_ALLOWED_CONTRACTS, "rumi_ai_gateway_pack")
    ]
    assert [item[:2] for item in dispatch.calls] == [
        (MODEL_PROFILE_CONTRACT, MODEL_PROFILE_GENERATE_OPERATION),
        (
            REQUEST_PREPARE_CONTRACT,
            "rumi_ai_pipeline_pack.ai-request-prepare.generate",
        ),
        (MODEL_PROFILE_CONTRACT, MODEL_PROFILE_GENERATE_OPERATION),
        (ROUTING_CONTRACT, "rumi_ai_routing_pack.ai-route.generate"),
        (
            GENERATE_PROVIDER_CONTRACT,
            "rumi_provider_adapters_pack.provider-generate",
        ),
        (
            TOOL_BRIDGE_CONTRACT,
            "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.generate",
        ),
        (USAGE_CONTRACT, "rumi_ai_usage_pack.ai-usage-cost.generate"),
    ]

    prepare = next(
        payload
        for contract, _, payload in dispatch.calls
        if contract == REQUEST_PREPARE_CONTRACT
    )
    assert prepare["messages"] == [
        {"role": "system", "content": "Reply in one sentence."},
        {"role": "user", "content": "Summarize the meeting."},
    ]
    assert prepare["model_profile_id"] == "profile.fixture"
    # The typed node pins modalities and the ordinary chat surface; the
    # profile-owned preferred model is merged in by profile resolution.
    assert prepare["requirements"] == {
        "modalities": ["text"],
        "request_surface": "chat",
    }

    routing = next(
        payload
        for contract, _, payload in dispatch.calls
        if contract == ROUTING_CONTRACT
    )
    assert routing["requirements"]["preferred_model_id"] == "fixture/model"

    provider = next(
        payload
        for contract, _, payload in dispatch.calls
        if contract == GENERATE_PROVIDER_CONTRACT
    )
    assert provider["credential_handle"] == "private-handle"
    assert provider["request_surface"] == "chat"
    assert provider["required_modalities"] == ["text"]


def test_text_generate_omits_system_message_without_system_prompt() -> None:
    contribution, _ = _captured_provider()
    dispatch = _CapturedDispatch(configured_provider=True)
    result = contribution.invoke(
        _FUNCTION_ID,
        {"text": "hello", "model_profile_id": "profile.fixture"},
        _Invocation(dispatch),
    )
    assert result["text"] == "canonical provider result"
    prepare = next(
        payload
        for contract, _, payload in dispatch.calls
        if contract == REQUEST_PREPARE_CONTRACT
    )
    assert prepare["messages"] == [{"role": "user", "content": "hello"}]


def test_text_generate_contract_schema_types_the_node_ports() -> None:
    """The captured input schema is the Flow-facing port contract."""
    schema = _text_input_schema()
    validator = Draft202012Validator(schema)

    assert schema["properties"]["model_profile_id"][
        "x-tobkiri-selector"
    ] == "model-profile"
    validator.validate(
        {"text": "hi", "model_profile_id": "profile.fixture"}
    )
    validator.validate(
        {
            "text": "hi",
            "model_profile_id": "profile.fixture",
            "system_prompt": "be brief",
        }
    )
    for invalid in (
        {},
        {"text": "hi"},
        {"model_profile_id": "profile.fixture"},
        {"text": "", "model_profile_id": "profile.fixture"},
        {"text": "hi", "model_profile_id": ""},
        {"text": "hi", "model_profile_id": "p", "credential_handle": "x"},
        {"text": "hi", "model_profile_id": "p", "messages": []},
        {"text": "x" * 262_145, "model_profile_id": "p"},
        {
            "text": "hi",
            "model_profile_id": "p",
            "system_prompt": "x" * 16_385,
        },
    ):
        assert not validator.is_valid(invalid), invalid


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "x" * 262_145, "model_profile_id": "profile.fixture"},
        {"text": "hi", "model_profile_id": "profile.fixture", "extra": 1},
        {"text": "hi", "model_profile_id": "profile.fixture",
         "credential_handle": "injected"},
        {"text": "hi", "model_profile_id": "profile.fixture",
         "provider_instance_id": "provider.evil"},
        {"text": "hi", "model_profile_id": "profile.fixture",
         "route_binding": {}},
        {"text": 5, "model_profile_id": "profile.fixture"},
        {"text": "hi", "model_profile_id": " " * 4},
    ],
)
def test_text_generate_rejects_unbounded_or_foreign_input_before_dispatch(
    payload: Mapping[str, Any],
) -> None:
    """Oversized text and execution fields never reach a dependency."""
    contribution, _ = _captured_provider()
    dispatch = _CapturedDispatch(configured_provider=True)
    with pytest.raises(ValueError, match="AI text generate"):
        contribution.invoke(
            _FUNCTION_ID, payload, _Invocation(dispatch)
        )
    assert not dispatch.calls


def test_text_generate_rejects_an_unknown_model_profile() -> None:
    contribution, _ = _captured_provider()
    dispatch = _UnknownProfileDispatch(configured_provider=True)
    with pytest.raises(GlobalContractInvocationError) as captured:
        contribution.invoke(
            _FUNCTION_ID,
            {"text": "hi", "model_profile_id": "profile.missing"},
            _Invocation(dispatch),
        )
    assert captured.value.code == "unresolved_profile"
    assert [contract for contract, _, _ in dispatch.calls] == [
        MODEL_PROFILE_CONTRACT
    ]


def test_text_generate_rejects_a_disabled_model_profile() -> None:
    """A registered but disabled profile never reaches the provider."""
    contribution, _ = _captured_provider()
    dispatch = _DisabledProfileDispatch(configured_provider=True)
    with pytest.raises(GlobalContractInvocationError) as captured:
        contribution.invoke(
            _FUNCTION_ID,
            {"text": "hi", "model_profile_id": "profile.fixture"},
            _Invocation(dispatch),
        )
    assert captured.value.code == "disabled_profile"
    assert [contract for contract, _, _ in dispatch.calls] == [
        MODEL_PROFILE_CONTRACT
    ]


def test_text_generate_rejects_a_malformed_provider_result() -> None:
    contribution, _ = _captured_provider()
    dispatch = _MalformedResultDispatch(configured_provider=True)
    with pytest.raises(GlobalContractInvocationError) as captured:
        contribution.invoke(
            _FUNCTION_ID,
            {"text": "hi", "model_profile_id": "profile.fixture"},
            _Invocation(dispatch),
        )
    assert captured.value.code == "invalid_response"


def test_text_generate_rejects_an_oversized_provider_result() -> None:
    contribution, _ = _captured_provider()
    dispatch = _OversizedResultDispatch(configured_provider=True)
    with pytest.raises(GlobalContractInvocationError) as captured:
        contribution.invoke(
            _FUNCTION_ID,
            {"text": "hi", "model_profile_id": "profile.fixture"},
            _Invocation(dispatch),
        )
    assert captured.value.code == "invalid_response"


def test_text_generate_capture_rejects_foreign_authority() -> None:
    """A binding pinned to another Function or Contract cannot capture."""
    factory = HOST_PROVIDER_FACTORY[_FUNCTION_ID]
    foreign_function = _binding(function_id="rumi_evil_pack.text-generate")
    with pytest.raises(PermissionError, match="bindings are incomplete"):
        factory.capture(
            HostProviderCaptureContextV4(
                profile_id="defaults",
                plan_digest=authority_digest({"plan": "g"}),
                security_epoch=1,
                activation={},
                state_root=Path("/tmp/gateway-text-generate-state"),
                provider_bindings=(foreign_function,),
                catalog_bindings=(),
                domain_ids={},
            )
        )
    foreign_contract = _binding(contract_id="tobkiri.service.ai.generate.v1")
    with pytest.raises(PermissionError, match="bindings are incomplete"):
        factory.capture(
            HostProviderCaptureContextV4(
                profile_id="defaults",
                plan_digest=authority_digest({"plan": "g"}),
                security_epoch=1,
                activation={},
                state_root=Path("/tmp/gateway-text-generate-state"),
                provider_bindings=(foreign_contract,),
                catalog_bindings=(),
                domain_ids={},
            )
        )


def test_text_generate_rejects_a_stale_operation_identity() -> None:
    """The captured contribution only dispatches its pinned operation."""
    contribution, _ = _captured_provider()
    dispatch = _CapturedDispatch(configured_provider=True)
    with pytest.raises(PermissionError, match="operation identity"):
        contribution.invoke(
            "rumi_ai_gateway_pack.ai-gateway.generate",
            {"text": "hi", "model_profile_id": "profile.fixture"},
            _Invocation(dispatch),
        )
    assert not dispatch.calls


def test_text_generate_client_cannot_reach_undeclared_contracts() -> None:
    """The captured allowlist is the same authority boundary as generate."""
    contribution, _ = _captured_provider()
    dispatch = _CapturedDispatch(configured_provider=True)
    invocation = _Invocation(dispatch)
    contribution.invoke(
        _FUNCTION_ID,
        {"text": "hi", "model_profile_id": "profile.fixture"},
        invocation,
    )
    allowed, consumer = invocation.requests[0]
    client = GlobalContractClient(
        session=dispatch,
        allowed_contract_ids=allowed,
        consumer_pack_id=consumer,
    )
    with pytest.raises(PermissionError, match="not declared"):
        client.invoke("tobkiri.foreign.contract.v1", "op", {})
