"""Production Host Provider coverage for the Pack v4 AI Gateway."""

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
    CATALOG_CONTRACT,
    FAILOVER_CONTRACT,
    GENERATE_PROVIDER_CONTRACT,
    HEALTH_CONTRACT,
    HOST_PROVIDER_FACTORY,
    MODEL_PROFILE_CONTRACT,
    PROVIDER_REGISTRY_CONTRACT,
    PROVIDER_REGISTRY_GENERATE_OPERATION,
    REQUEST_PREPARE_CONTRACT,
    ROUTING_CONTRACT,
    TOOL_BRIDGE_CONTRACT,
    USAGE_CONTRACT,
)
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_protocol.canonical import canonical_json


_FUNCTION_ID = "rumi_ai_gateway_pack.ai-gateway.generate"
_OPERATION_ID = _FUNCTION_ID
_CONTRACT_ID = "tobkiri.service.ai.generate.v1"


def _route_quote_input_schema() -> Mapping[str, Any]:
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
        if item["contract_id"] == "tobkiri.resource.ai.route.quote.v1"
    )
    return contract["schemas"]["input"]


class _CapturedDispatch:
    """Deterministic V4 dispatch double with no provider fallback."""

    profile_id = "defaults"
    plan_digest = authority_digest({"plan": "gateway-host-provider"})

    def __init__(self, *, configured_provider: bool) -> None:
        self._configured_provider = configured_provider
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
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
        if contract_id == REQUEST_PREPARE_CONTRACT:
            return {
                "request_id": "request.gateway-host-provider",
                "deadline": time.time() + 30,
                "messages": list(payload.get("messages") or ()),
                "model_profile_id": payload.get("model_profile_id"),
                "model_reference": payload.get("model_reference"),
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
            implementation_digest=authority_digest(
                {"implementation": _FUNCTION_ID}
            ),
        ),
        operation=SimpleNamespace(
            contract_id=_CONTRACT_ID,
            contract_version="1.0.0",
            operation_id=_OPERATION_ID,
        ),
        principal_ref=OpaqueAuthorityRef(principal_id),
        artifact=SimpleNamespace(
            digest=authority_digest({"artifact": _FUNCTION_ID})
        ),
    )


def _captured_provider(*, quote: bool = False) -> tuple[Any, Any]:
    binding = _binding()
    domain_id = "domain.provider.gateway-host-provider"
    contract_id, operation_id = _CONTRACT_ID, _OPERATION_ID
    factory = HOST_PROVIDER_FACTORY[_FUNCTION_ID]
    if quote:
        from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import (
            CONTRACT_ID,
            FUNCTION_ID,
            HOST_PROVIDER_FACTORY as QUOTE_FACTORIES,
        )

        contract_id, operation_id = CONTRACT_ID, FUNCTION_ID
        binding.function.function_id = FUNCTION_ID
        binding.operation.contract_id = CONTRACT_ID
        binding.operation.operation_id = FUNCTION_ID
        factory = QUOTE_FACTORIES[FUNCTION_ID]
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


class _RouteQuoteDispatch(_CapturedDispatch):
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
            return {
                "profile": {
                    "model_id": "fixture/model",
                    "credential_handle": "private-handle",
                }
            }
        result = super().invoke(
            contract_id,
            operation_id,
            payload,
            version_range=version_range,
        )
        return result


class _SavedConnectionRouteQuoteDispatch(_RouteQuoteDispatch):
    """Quote fixture for one registry-owned opaque saved connection."""

    connection_id = "connection/openai:main"

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
        if contract_id == PROVIDER_REGISTRY_CONTRACT:
            return (
                {
                    "provider_instance_id": "registry.resource.generate",
                    "operation_id": PROVIDER_REGISTRY_GENERATE_OPERATION,
                },
            )
        return super().provider_metadata(contract_id)

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
            return {
                "profile": {
                    "model_id": "fixture/model",
                    "metadata": {
                        "provider_connection_id": self.connection_id,
                    },
                },
            }
        if contract_id == PROVIDER_REGISTRY_CONTRACT:
            self.calls.append((contract_id, operation_id, dict(payload)))
            assert operation_id == PROVIDER_REGISTRY_GENERATE_OPERATION
            return {
                "revision": 7,
                "providers": [
                    {
                        "provider_instance_id": self.connection_id,
                        "display_name": "OpenAI main",
                        "enabled": True,
                    }
                ],
            }
        return super().invoke(
            contract_id,
            operation_id,
            payload,
            version_range=version_range,
        )


class _UncataloguedSavedConnectionDispatch(_SavedConnectionRouteQuoteDispatch):
    """Represent a saved connection whose route has no catalog owner id."""

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
        if contract_id != ROUTING_CONTRACT:
            return result
        return {
            **result,
            "candidates": [
                {
                    **dict(result["candidates"][0]),
                    "catalog_provider_instance_id": "",
                }
            ],
        }


class _PricedRouteDispatch(_RouteQuoteDispatch):
    """Fixture with float owner rates and a float usage-cost response."""

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
        if contract_id == ROUTING_CONTRACT:
            return {
                **result,
                "candidates": [
                    {
                        **dict(result["candidates"][0]),
                        "input_cost": 0.000001,
                        "output_cost": 0.0000025,
                    }
                ],
            }
        if contract_id == USAGE_CONTRACT:
            return {
                "input_tokens": 1.0,
                "output_tokens": 2.0,
                "total_tokens": 3.0,
                "cost": 0.000006,
                "currency": "USD",
                "known": True,
            }
        return result


def test_route_quote_resolves_without_generation_or_billing() -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import FUNCTION_ID

    contribution, _ = _captured_provider(quote=True)
    dispatch = _RouteQuoteDispatch(configured_provider=True)
    result = contribution.invoke(
        FUNCTION_ID,
        {
            "model_profile_id": "model-profile",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        _Invocation(dispatch),
    )

    assert result == {
        "ready": True,
        "model_id": "fixture/model",
        "provider_instance_id": "provider.fixture",
        "catalog_provider_instance_id": "catalog.fixture",
        "catalog_revision": "catalog.fixture.v1",
        "pricing_revision": "catalog.fixture.v1",
        "pricing": {
            "input": None,
            "output": None,
            "currency": "USD",
            "unit": "usd_per_token",
        },
        "route_binding": {
            "model_id": "fixture/model",
            "provider_instance_id": "provider.fixture",
            "catalog_provider_instance_id": "catalog.fixture",
            "catalog_revision": "catalog.fixture.v1",
            "pricing_revision": "catalog.fixture.v1",
            "pricing": {
                "input": None,
                "output": None,
                "currency": "USD",
                "unit": "usd_per_token",
            },
        },
    }
    assert [contract for contract, _, _ in dispatch.calls] == [
        REQUEST_PREPARE_CONTRACT,
        MODEL_PROFILE_CONTRACT,
        ROUTING_CONTRACT,
    ]
    assert "private-handle" not in repr(result)


def test_route_quote_contract_accepts_generic_requirements() -> None:
    """Quote requirements remain Pack-neutral route constraints."""
    validator = Draft202012Validator(_route_quote_input_schema())
    payload = {
        "model_reference": "fixture/model",
        "messages": [{"role": "user", "content": "Inspect an image"}],
        "requirements": {
            "modalities": ["text", "image"],
            "tool_calling": True,
            "request_surface": "strategy.fixture",
        },
    }
    validator.validate(payload)
    assert not validator.is_valid({**payload, "credential_handle": "injected"})


def test_route_quote_accepts_profile_reference_and_multimodal_messages() -> None:
    """Quote accepts the same image-bearing request shape as generation."""
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import FUNCTION_ID

    contribution, _ = _captured_provider(quote=True)
    dispatch = _RouteQuoteDispatch(configured_provider=True)
    result = contribution.invoke(
        FUNCTION_ID,
        {
            "model_reference": {"profile_id": "model-profile"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Inspect this image."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,aGVsbG8="
                            },
                        },
                    ],
                }
            ],
        },
        _Invocation(dispatch),
    )

    assert result["ready"] is True
    prepared = next(
        payload
        for contract, _, payload in dispatch.calls
        if contract == REQUEST_PREPARE_CONTRACT
    )
    assert prepared["model_profile_id"] == "model-profile"
    assert prepared["messages"][0]["content"][1]["type"] == "image_url"


def test_gateway_resolves_a_structured_profile_reference() -> None:
    """Ordinary generation accepts the generic strategy profile wrapper."""
    contribution, _ = _captured_provider()
    dispatch = _RouteQuoteDispatch(configured_provider=True)

    result = contribution.invoke(
        _OPERATION_ID,
        {
            "model_reference": {"profile_id": "model-profile"},
            "messages": [{"role": "user", "content": "hello"}],
        },
        _Invocation(dispatch),
    )

    assert result["status"] == "ok"
    model_profile_payload = next(
        payload
        for contract, _, payload in dispatch.calls
        if contract == MODEL_PROFILE_CONTRACT
    )
    assert model_profile_payload == {"identifier": "model-profile"}


def test_route_quote_forwards_generic_requirements_to_routing() -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import FUNCTION_ID

    contribution, _ = _captured_provider(quote=True)
    dispatch = _RouteQuoteDispatch(configured_provider=True)
    result = contribution.invoke(
        FUNCTION_ID,
        {
            "model_profile_id": "model-profile",
            "messages": [{"role": "user", "content": "Inspect the image"}],
            "requirements": {"modalities": ["text", "image"]},
        },
        _Invocation(dispatch),
    )

    assert result["ready"] is True
    prepared = next(
        payload
        for contract, _, payload in dispatch.calls
        if contract == REQUEST_PREPARE_CONTRACT
    )
    assert prepared["requirements"] == {"modalities": ["text", "image"]}


def test_route_quote_rejects_oversized_input_before_dependency_reads() -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import FUNCTION_ID

    contribution, _ = _captured_provider(quote=True)
    dispatch = _RouteQuoteDispatch(configured_provider=True)
    with pytest.raises(ValueError, match="route quote input is invalid"):
        contribution.invoke(
            FUNCTION_ID,
            {
                "model_profile_id": "model-profile",
                "messages": [
                    {"role": "user", "content": "x" * (3 * 1024 * 1024)}
                ],
            },
            _Invocation(dispatch),
        )
    assert not dispatch.calls


def test_route_quote_rechecks_an_opaque_saved_connection() -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import FUNCTION_ID

    contribution, _ = _captured_provider(quote=True)
    dispatch = _SavedConnectionRouteQuoteDispatch(configured_provider=True)
    result = contribution.invoke(
        FUNCTION_ID,
        {
            "model_profile_id": "model-profile",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        _Invocation(dispatch),
    )

    assert result["ready"] is True
    assert result["provider_instance_id"] == "provider.fixture"
    assert [contract for contract, _, _ in dispatch.calls] == [
        REQUEST_PREPARE_CONTRACT,
        MODEL_PROFILE_CONTRACT,
        PROVIDER_REGISTRY_CONTRACT,
        ROUTING_CONTRACT,
    ]
    assert "connection/openai:main" not in repr(result)


def test_route_quote_preserves_an_uncatalogued_saved_connection() -> None:
    """A saved connection remains owner-bound without leaking its registry id."""
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import FUNCTION_ID

    contribution, _ = _captured_provider(quote=True)
    result = contribution.invoke(
        FUNCTION_ID,
        {
            "model_profile_id": "model-profile",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        _Invocation(_UncataloguedSavedConnectionDispatch(configured_provider=True)),
    )

    assert result["catalog_provider_instance_id"] == ""
    assert result["route_binding"]["catalog_provider_instance_id"] == ""
    assert "connection/openai:main" not in repr(result)


def test_route_quote_and_gateway_response_use_packvm_safe_decimal_strings() -> None:
    """Quoted rates and generated usage never introduce float bridge frames."""
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import FUNCTION_ID

    quote_contribution, _ = _captured_provider(quote=True)
    quote = quote_contribution.invoke(
        FUNCTION_ID,
        {
            "model_profile_id": "model-profile",
            "messages": [{"role": "user", "content": "Hello"}],
        },
        _Invocation(_PricedRouteDispatch(configured_provider=True)),
    )

    assert quote["pricing"] == {
        "input": "0.000001",
        "output": "0.0000025",
        "currency": "USD",
        "unit": "usd_per_token",
    }
    canonical_json(quote)

    contribution, _ = _captured_provider()
    response = contribution.invoke(
        _OPERATION_ID,
        {
            "messages": [{"role": "user", "content": "Hello"}],
            "_session_id": "session.packvm-bridge.gateway-float-safe",
            "requirements": {
                "preferred_model_id": "fixture/model",
                "preferred_provider_instance_id": "provider.fixture",
            },
            "allow_failover": False,
            # A pre-existing direct caller can still submit numeric rates.
            "route_binding": {
                **quote["route_binding"],
                "pricing": {
                    "input": 0.000001,
                    "output": 0.0000025,
                    "currency": "USD",
                },
            },
        },
        _Invocation(_PricedRouteDispatch(configured_provider=True)),
    )

    assert response["usage_cost"]["cost"] == "0.000006"
    assert response["usage_cost"]["input_tokens"] == "1"
    assert response["usage_cost"]["known"] is True
    canonical_json(response)


@pytest.mark.parametrize(
    "extra",
    ["resolve_only", "credential_handle", "provider_instance_id", "_session_id"],
)
def test_route_quote_rejects_execution_fields_before_dependency_reads(
    extra: str,
) -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import FUNCTION_ID

    contribution, _ = _captured_provider(quote=True)
    dispatch = _RouteQuoteDispatch(configured_provider=True)
    with pytest.raises(ValueError, match="fields"):
        contribution.invoke(
            FUNCTION_ID,
            {
                "model_profile_id": "model-profile",
                "messages": [{"role": "user", "content": "Hi"}],
                extra: "injected",
            },
            _Invocation(dispatch),
        )
    assert not dispatch.calls


def test_route_quote_missing_provider_is_not_reported_ready() -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import FUNCTION_ID

    contribution, _ = _captured_provider(quote=True)
    with pytest.raises(GlobalContractInvocationError, match="no selected provider"):
        contribution.invoke(
            FUNCTION_ID,
            {
                "model_profile_id": "model-profile",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            _Invocation(_RouteQuoteDispatch(configured_provider=False)),
        )


def test_route_quote_requires_the_exact_selected_provider_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_runtime.global_contract_dispatch import GlobalContractUnavailable
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import FUNCTION_ID

    contribution, _ = _captured_provider(quote=True)
    dispatch = _RouteQuoteDispatch(configured_provider=True)
    monkeypatch.setattr(
        dispatch,
        "provider_metadata",
        lambda contract: (
            ({"provider_instance_id": "provider.fixture"},)
            if contract == GENERATE_PROVIDER_CONTRACT
            else ()
        ),
    )
    with pytest.raises(GlobalContractUnavailable, match="selected provider operation"):
        contribution.invoke(
            FUNCTION_ID,
            {
                "model_profile_id": "model-profile",
                "messages": [{"role": "user", "content": "Hi"}],
            },
            _Invocation(dispatch),
        )
    assert not any(
        contract == GENERATE_PROVIDER_CONTRACT
        for contract, _, _ in dispatch.calls
    )


@pytest.mark.parametrize(
    "contract",
    [GENERATE_PROVIDER_CONTRACT, USAGE_CONTRACT, FAILOVER_CONTRACT],
)
def test_route_quote_dispatch_guard_denies_effectful_dependency_calls(
    contract: str,
) -> None:
    from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import _ReadOnlyDispatch

    dispatch = _RouteQuoteDispatch(configured_provider=True)
    readonly = _ReadOnlyDispatch(dispatch)
    with pytest.raises(PermissionError, match="cannot execute"):
        readonly.invoke(contract, "injected", {})
    assert not dispatch.calls


def test_gateway_host_factory_dispatches_only_through_the_captured_client() -> None:
    """A configured test Provider reaches Gateway through exact V4 dispatch."""
    contribution, _ = _captured_provider()
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
                    PROVIDER_REGISTRY_CONTRACT,
                }
            ),
            "rumi_ai_gateway_pack",
        )
    ]
    assert [item[:2] for item in dispatch.calls] == [
        (
            REQUEST_PREPARE_CONTRACT,
            "rumi_ai_pipeline_pack.ai-request-prepare.generate",
        ),
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


def test_gateway_uses_one_generic_provider_call_for_unknown_route_metadata() -> None:
    """Strategy-local metadata cannot turn Gateway into a special workflow."""
    contribution, _ = _captured_provider()
    dispatch = _CapturedDispatch(configured_provider=True)
    result = contribution.invoke(
        _OPERATION_ID,
        {
            "messages": [{"role": "user", "content": "hello"}],
            "requirements": {
                "request_surface": "strategy.fixture",
                "strategy_hint": "multi-pass",
            },
        },
        _Invocation(dispatch),
    )

    assert result["output"] == "canonical provider result"
    provider_calls = [
        item
        for item in dispatch.calls
        if item[0] == GENERATE_PROVIDER_CONTRACT
    ]
    assert len(provider_calls) == 1


def test_gateway_enforces_a_generic_route_binding() -> None:
    """A quoted route cannot silently drift to another provider or price."""
    contribution, _ = _captured_provider()
    binding = {
        "model_id": "fixture/model",
        "provider_instance_id": "provider.fixture",
        "catalog_provider_instance_id": "catalog.fixture",
        "catalog_revision": "catalog.fixture.v1",
        "pricing_revision": "catalog.fixture.v1",
        "pricing": {"input": None, "output": None, "currency": "USD"},
    }
    result = contribution.invoke(
        _OPERATION_ID,
        {
            "messages": [{"role": "user", "content": "hello"}],
            "requirements": {
                "preferred_model_id": "fixture/model",
                "preferred_provider_instance_id": "provider.fixture",
            },
            "allow_failover": False,
            "route_binding": binding,
        },
        _Invocation(_CapturedDispatch(configured_provider=True)),
    )
    assert result["output"] == "canonical provider result"

    stale = {**binding, "pricing": {**binding["pricing"], "currency": "JPY"}}
    with pytest.raises(GlobalContractInvocationError) as captured:
        contribution.invoke(
            _OPERATION_ID,
            {
                "messages": [{"role": "user", "content": "hello"}],
                "requirements": {
                    "preferred_model_id": "fixture/model",
                    "preferred_provider_instance_id": "provider.fixture",
                },
                "allow_failover": False,
                "route_binding": stale,
            },
            _Invocation(_CapturedDispatch(configured_provider=True)),
        )
    assert captured.value.code == "route_binding_stale"


def test_gateway_rejects_route_binding_with_failover() -> None:
    contribution, _ = _captured_provider()
    binding = {
        "model_id": "fixture/model",
        "provider_instance_id": "provider.fixture",
        "catalog_provider_instance_id": "catalog.fixture",
        "catalog_revision": "catalog.fixture.v1",
        "pricing_revision": "catalog.fixture.v1",
        "pricing": {"input": None, "output": None, "currency": "USD"},
    }
    with pytest.raises(GlobalContractInvocationError) as captured:
        contribution.invoke(
            _OPERATION_ID,
            {
                "messages": [{"role": "user", "content": "hello"}],
                "allow_failover": True,
                "route_binding": binding,
            },
            _Invocation(_CapturedDispatch(configured_provider=True)),
        )
    assert captured.value.code == "route_binding_conflict"


def test_gateway_host_factory_leaves_an_unconfigured_provider_unavailable() -> None:
    """Missing selected provider is not replaced by a direct or legacy fallback."""
    contribution, _ = _captured_provider()
    invocation = _Invocation(_CapturedDispatch(configured_provider=False))
    with pytest.raises(GlobalContractInvocationError) as captured:
        contribution.invoke(
            _OPERATION_ID,
            {"messages": [{"role": "user", "content": "hello"}]},
            invocation,
        )
    assert captured.value.code == "missing_provider"
