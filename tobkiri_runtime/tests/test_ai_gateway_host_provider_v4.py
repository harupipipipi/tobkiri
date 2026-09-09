"""Production Host Provider coverage for the Pack v4 AI Gateway."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime.authority.v4 import authority_digest
from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)
from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from ecosystem.rumi_ai_gateway_pack.runtime.gateway import (
    CATALOG_CONTRACT,
    FAILOVER_CONTRACT,
    GENERATE_PROVIDER_CONTRACT,
    HEALTH_CONTRACT,
    HOST_PROVIDER_FACTORY,
    MODEL_PROFILE_CONTRACT,
    REQUEST_PREPARE_CONTRACT,
    ROUTING_CONTRACT,
    TOOL_BRIDGE_CONTRACT,
    USAGE_CONTRACT,
)
from tobkiri_host.models import OpaqueAuthorityRef


_FUNCTION_ID = "rumi_ai_gateway_pack.ai-gateway.generate"
_OPERATION_ID = _FUNCTION_ID
_CONTRACT_ID = "tobkiri.service.ai.generate.v1"


class _CapturedDispatch:
    """Deterministic V4 dispatch double with no provider fallback."""

    profile_id = "defaults"
    plan_digest = authority_digest({"plan": "gateway-host-provider"})

    def __init__(self, *, configured_provider: bool) -> None:
        self._configured_provider = configured_provider
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
        if contract_id == GENERATE_PROVIDER_CONTRACT and self._configured_provider:
            return ({"provider_instance_id": "provider.fixture",
                     "operation_id": "rumi_provider_adapters_pack.provider-generate"},)
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
        if contract_id == REQUEST_PREPARE_CONTRACT:
            return {
                "request_id": "request.gateway-host-provider",
                "deadline": time.time() + 30,
                "messages": list(payload.get("messages") or ()),
                "requirements": dict(payload.get("requirements") or {}),
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
        if contract_id in {
            CATALOG_CONTRACT,
            HEALTH_CONTRACT,
            MODEL_PROFILE_CONTRACT,
            FAILOVER_CONTRACT,
        }:
            raise AssertionError(f"unexpected Gateway dependency: {contract_id}")
        raise AssertionError(f"undeclared Gateway dispatch: {contract_id}")


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


def _binding() -> Any:
    principal_id = authority_digest({"principal": _FUNCTION_ID})
    return SimpleNamespace(
        function=SimpleNamespace(
            function_id=_FUNCTION_ID,
            implementation_digest=authority_digest({"implementation": _FUNCTION_ID}),
        ),
        operation=SimpleNamespace(
            contract_id=_CONTRACT_ID,
            contract_version="1.0.0",
            operation_id=_OPERATION_ID,
        ),
        principal_ref=OpaqueAuthorityRef(principal_id),
        artifact=SimpleNamespace(digest=authority_digest({"artifact": _FUNCTION_ID})),
    )


def _captured_provider(*, preflight: bool = False) -> tuple[Any, Any]:
    binding = _binding()
    domain_id = "domain.provider.gateway-host-provider"
    contract_id, operation_id = _CONTRACT_ID, _OPERATION_ID
    factory = HOST_PROVIDER_FACTORY[_FUNCTION_ID]
    if preflight:
        from ecosystem.rumi_ai_gateway_pack.runtime.preflight import (
            CONTRACT_ID, FUNCTION_ID, HOST_PROVIDER_FACTORY as PREFLIGHT_FACTORIES,
        )
        contract_id, operation_id = CONTRACT_ID, FUNCTION_ID
        binding.function.function_id = FUNCTION_ID
        binding.operation.contract_id = CONTRACT_ID
        binding.operation.operation_id = FUNCTION_ID
        factory = PREFLIGHT_FACTORIES[FUNCTION_ID]
    captured = factory.capture(
        HostProviderCaptureContextV4(
            profile_id="defaults",
            plan_digest=authority_digest({"plan": "gateway-host-provider"}),
            security_epoch=1,
            activation={"activation_id": "activation.gateway-host-provider"},
            state_root=Path("/tmp/gateway-host-provider-state"),
            provider_bindings=(binding,),
            catalog_bindings=(),
            domain_ids={
                (
                    contract_id,
                    operation_id,
                    binding.principal_ref.value,
                ): domain_id
            },
        )
    )
    assert len(captured.contributions) == 1
    return captured.contributions[0], binding


class _PreflightDispatch(_CapturedDispatch):
    def invoke(self, contract_id: str, operation_id: str, payload: Mapping[str, Any], *,
               version_range: str | None = None) -> Mapping[str, Any]:
        if contract_id == MODEL_PROFILE_CONTRACT:
            self.calls.append((contract_id, operation_id, dict(payload)))
            return {"profile": {"model_id": "fixture/model", "credential_handle": "private-handle"}}
        result = super().invoke(contract_id, operation_id, payload, version_range=version_range)
        if contract_id == REQUEST_PREPARE_CONTRACT:
            return {**result, "model_profile_id": payload.get("model_profile_id")}
        return result


def test_preflight_captured_operation_resolves_without_generation_or_billing() -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.preflight import FUNCTION_ID
    contribution, _ = _captured_provider(preflight=True)
    dispatch = _PreflightDispatch(configured_provider=True)
    result = contribution.invoke(FUNCTION_ID, {
        "model_profile_id": "model-profile", "messages": [{"role": "user", "content": "Hello"}],
    }, _Invocation(dispatch))
    assert result == {"ready": True, "model_profile_id": "model-profile",
                      "model_id": "fixture/model", "provider_instance_id": "provider.fixture",
                      "catalog_revision": "catalog.fixture.v1"}
    assert [contract for contract, _, _ in dispatch.calls] == [
        REQUEST_PREPARE_CONTRACT, MODEL_PROFILE_CONTRACT, ROUTING_CONTRACT,
    ]
    assert "private-handle" not in repr(result)


@pytest.mark.parametrize("extra", ["resolve_only", "credential_handle", "provider_instance_id", "_session_id"])
def test_preflight_rejects_extra_input_before_reading_dependencies(extra: str) -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.preflight import FUNCTION_ID
    contribution, _ = _captured_provider(preflight=True)
    dispatch = _PreflightDispatch(configured_provider=True)
    with pytest.raises(ValueError, match="fields"):
        contribution.invoke(FUNCTION_ID, {
            "model_profile_id": "model-profile", "messages": [{"role": "user", "content": "Hi"}],
            extra: "injected",
        }, _Invocation(dispatch))
    assert not dispatch.calls


def test_preflight_missing_provider_is_not_reported_ready() -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.preflight import FUNCTION_ID
    contribution, _ = _captured_provider(preflight=True)
    with pytest.raises(GlobalContractInvocationError, match="no selected provider"):
        contribution.invoke(FUNCTION_ID, {
            "model_profile_id": "model-profile", "messages": [{"role": "user", "content": "Hi"}],
        }, _Invocation(_PreflightDispatch(configured_provider=False)))


def test_preflight_missing_exact_provider_operation_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from core_runtime.global_contract_dispatch import GlobalContractUnavailable
    from ecosystem.rumi_ai_gateway_pack.runtime.preflight import FUNCTION_ID
    contribution, _ = _captured_provider(preflight=True)
    dispatch = _PreflightDispatch(configured_provider=True)
    monkeypatch.setattr(dispatch, "provider_metadata", lambda contract: (
        ({"provider_instance_id": "provider.fixture"},) if contract == GENERATE_PROVIDER_CONTRACT else ()
    ))
    with pytest.raises(GlobalContractUnavailable, match="selected provider operation"):
        contribution.invoke(FUNCTION_ID, {
            "model_profile_id": "model-profile", "messages": [{"role": "user", "content": "Hi"}],
        }, _Invocation(dispatch))
    assert not any(contract == GENERATE_PROVIDER_CONTRACT for contract, _, _ in dispatch.calls)


@pytest.mark.parametrize("contract", [GENERATE_PROVIDER_CONTRACT, USAGE_CONTRACT, FAILOVER_CONTRACT])
def test_preflight_dispatch_guard_denies_effectful_dependency_calls(contract: str) -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.preflight import _ReadOnlyDispatch
    dispatch = _PreflightDispatch(configured_provider=True)
    readonly = _ReadOnlyDispatch(dispatch)
    with pytest.raises(PermissionError, match="cannot execute"):
        readonly.invoke(contract, "injected", {})
    assert not dispatch.calls


def test_gateway_host_factory_dispatches_only_through_the_captured_client() -> None:
    """A configured test Provider reaches Gateway through exact V4 dispatch."""

    contribution, _binding_value = _captured_provider()
    dispatch = _CapturedDispatch(configured_provider=True)
    invocation = _Invocation(dispatch)
    result = contribution.invoke(
        _OPERATION_ID,
        {
            "messages": [{"role": "user", "content": "hello"}],
            "requirements": {"request_surface": "defaultspack.conversation"},
        },
        invocation,
    )

    assert result["status"] == "ok"
    assert result["output"] == "canonical provider result"
    assert invocation.requests == [
        (
            frozenset(
                {
                    CATALOG_CONTRACT,
                    GENERATE_PROVIDER_CONTRACT,
                    HEALTH_CONTRACT,
                    USAGE_CONTRACT,
                    ROUTING_CONTRACT,
                    TOOL_BRIDGE_CONTRACT,
                    REQUEST_PREPARE_CONTRACT,
                    FAILOVER_CONTRACT,
                    MODEL_PROFILE_CONTRACT,
                }
            ),
            "rumi_ai_gateway_pack",
        )
    ]
    assert [item[:2] for item in dispatch.calls] == [
        (REQUEST_PREPARE_CONTRACT, "rumi_ai_pipeline_pack.ai-request-prepare.generate"),
        (ROUTING_CONTRACT, "rumi_ai_routing_pack.ai-route.generate"),
        (GENERATE_PROVIDER_CONTRACT, "rumi_provider_adapters_pack.provider-generate"),
        (TOOL_BRIDGE_CONTRACT, "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.generate"),
        (USAGE_CONTRACT, "rumi_ai_usage_pack.ai-usage-cost.generate"),
    ]


def test_gateway_host_factory_leaves_an_unconfigured_provider_unavailable() -> None:
    """Missing selected provider is not replaced by a direct or legacy fallback."""

    contribution, _binding_value = _captured_provider()
    invocation = _Invocation(_CapturedDispatch(configured_provider=False))
    with pytest.raises(GlobalContractInvocationError) as captured:
        contribution.invoke(
            _OPERATION_ID,
            {"messages": [{"role": "user", "content": "hello"}]},
            invocation,
        )
    assert captured.value.code == "missing_provider"
