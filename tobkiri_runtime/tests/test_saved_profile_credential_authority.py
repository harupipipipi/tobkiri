"""Saved model hints must not override the current Provider credential owner.

These tests use real temporary owner stores, Gateway routing and the Provider
adapter. Only the final Host credential transport is synthetic; no network or
credential material store is accessed. This is a routing regression test, not
native Launcher, PackVM, credential-transport or real-inference acceptance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_ai_gateway_pack.runtime import gateway
from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
    create_failover_operation,
    create_prepare_operation,
)
from ecosystem.rumi_ai_routing_pack.runtime.router import create_route_operation
from ecosystem.rumi_ai_stream_pack.runtime.normalizer import (
    create_stream_normalize_operation,
)
from ecosystem.rumi_ai_tool_bridge_pack.runtime.bridge import (
    create_tool_intent_operation,
)
from ecosystem.rumi_ai_usage_pack.runtime.usage import create_cost_operation
from ecosystem.rumi_model_registry_pack.runtime.registry import (
    ModelRegistry,
    _hash,
    _profile_record,
)
from ecosystem.rumi_provider_adapters_pack.runtime import adapter
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry


_CONNECTION_ID = "connection/openai:main"
_PROFILE_HANDLE = "credential:synthetic-legacy-profile"
_REGISTRY_HANDLE = "credential:synthetic-current-provider"
_MODEL_ID = "organization/account-visible-model"
_PROFILE_ID = "daily"
_ALIAS = "daily-alias"
_REPLY = "Synthetic adapter reply."


class OwnerRouteClient:
    """Join actual owner snapshots and pure routing to the real adapter."""

    def __init__(
        self, models: ModelRegistry, providers: ProviderRegistry,
    ) -> None:
        self.models = models
        self.provider_registry = providers
        self.provider_requests: list[dict[str, Any]] = []
        self.transport_requests: list[dict[str, Any]] = []

    def providers(self, contract_id: str) -> tuple[dict[str, Any], ...]:
        if contract_id in {
            gateway.GENERATE_PROVIDER_CONTRACT,
            gateway.STREAM_PROVIDER_CONTRACT,
        }:
            streaming = contract_id == gateway.STREAM_PROVIDER_CONTRACT
            return ({
                "provider_instance_id": (
                    "provider.compatibility.stream" if streaming
                    else "provider.compatibility.generate"
                ),
                "operation_id": (
                    gateway.STREAM_PROVIDER_OPERATION if streaming
                    else gateway.GENERATE_PROVIDER_OPERATION
                ),
            },)
        if contract_id == gateway.PROVIDER_REGISTRY_CONTRACT:
            return tuple({
                "provider_instance_id": f"registry.{mode}",
                "operation_id": operation,
            } for mode, operation in (
                ("generate", gateway.PROVIDER_REGISTRY_GENERATE_OPERATION),
                ("stream", gateway.PROVIDER_REGISTRY_STREAM_OPERATION),
            ))
        # An explicit saved connection needs no catalog or network inventory.
        return ()

    def invoke(
        self,
        contract_id: str,
        operation: str,
        payload: Mapping[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if contract_id == gateway.MODEL_PROFILE_CONTRACT:
            resolved = self.models.resolve(str(payload["identifier"]))
            assert resolved is not None
            return resolved
        if contract_id == gateway.PROVIDER_REGISTRY_CONTRACT:
            return self.provider_registry.snapshot()
        if contract_id == gateway.REQUEST_PREPARE_CONTRACT:
            return create_prepare_operation(None)(operation, payload)
        if contract_id == gateway.ROUTING_CONTRACT:
            return create_route_operation(None)(operation, payload)
        if contract_id == gateway.FAILOVER_CONTRACT:
            return create_failover_operation(None)(operation, payload)
        if contract_id == gateway.TOOL_BRIDGE_CONTRACT:
            return create_tool_intent_operation(None)(operation, payload)
        if contract_id == gateway.USAGE_CONTRACT:
            return create_cost_operation(None)(operation, payload)
        if contract_id == gateway.STREAM_NORMALIZE_CONTRACT:
            return create_stream_normalize_operation(None)(operation, payload)
        if contract_id in {
            gateway.GENERATE_PROVIDER_CONTRACT,
            gateway.STREAM_PROVIDER_CONTRACT,
        }:
            self.provider_requests.append(dict(payload))
            streaming = contract_id == gateway.STREAM_PROVIDER_CONTRACT
            create = (
                adapter.create_stream_operation if streaming
                else adapter.create_generate_operation
            )
            return create(self)("stream" if streaming else "generate", payload)
        raise AssertionError(f"Unexpected dependency: {contract_id}/{operation}")

    def post_json_with_credential(self, **kwargs: Any) -> dict[str, Any]:
        """Record the selected handle without resolving it or opening a socket."""
        self.transport_requests.append(dict(kwargs))
        return {
            "choices": [{
                "message": {"content": _REPLY},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }


def _owners(
    root: Path, source: str, profile_handle: str | None,
) -> tuple[ModelRegistry, ProviderRegistry]:
    """Persist the handle through each supported model-owner import path."""
    providers = ProviderRegistry("defaults", user_data_root=root)
    providers.save({
        "provider_instance_id": _CONNECTION_ID,
        "adapter_id": "openai-compatible",
        "endpoint": "https://example.invalid/v1",
        "credential_handle": _REGISTRY_HANDLE,
        "enabled": True,
    }, expected_revision=0)

    models = ModelRegistry("defaults", user_data_root=root)
    record = {
        "model_profile_id": _PROFILE_ID,
        "display_name": "Daily",
        "model_id": _MODEL_ID,
        "credential_handle": profile_handle,
        "metadata": {"provider_connection_id": _CONNECTION_ID},
    }
    if source == "save":
        saved = models.save(record, expected_revision=0)
        models.set_alias(
            _ALIAS, _PROFILE_ID, expected_revision=saved["store_revision"],
        )
    else:
        assert source == "migration"
        normalized = _profile_record(record)
        migration_source = {
            "profiles": [normalized], "aliases": {_ALIAS: _PROFILE_ID},
        }
        models.migrate(
            migration_source["profiles"],
            migration_source["aliases"],
            expected_source_hash=_hash(migration_source),
        )
    resolved = models.resolve(_ALIAS)
    assert resolved is not None
    assert resolved["profile"]["credential_handle"] == profile_handle
    return models, providers


def _invoke(
    client: OwnerRouteClient, streaming: bool, **extra: Any,
) -> dict[str, Any]:
    create = (
        gateway.create_stream_operation if streaming
        else gateway.create_generate_operation
    )
    return create(client)("stream" if streaming else "generate", {
        "model_reference": _ALIAS,
        "messages": [{"role": "user", "content": "Synthetic test prompt."}],
        "requirements": {"request_surface": "conversation.saved"},
        **extra,
    })


@pytest.mark.parametrize("source", ["save", "migration"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("profile_handle", [None, _PROFILE_HANDLE])
def test_saved_model_profile_uses_current_provider_credential(
    tmp_path: Path, source: str, streaming: bool, profile_handle: str | None,
) -> None:
    """An accepted legacy profile hint must not block its current connection."""
    models, providers = _owners(tmp_path, source, profile_handle)
    client = OwnerRouteClient(models, providers)
    result = _invoke(client, streaming)

    assert len(client.provider_requests) == 1
    assert client.provider_requests[0]["model_id"] == _MODEL_ID
    assert client.provider_requests[0]["provider_connection_id"] == _CONNECTION_ID
    assert client.provider_requests[0].get("credential_handle") is None
    assert len(client.transport_requests) == 1
    transport = client.transport_requests[0]
    assert transport["credential_handle"] == _REGISTRY_HANDLE
    assert transport["provider_instance_id"] == _CONNECTION_ID
    assert transport["endpoint"] == "https://example.invalid/v1/chat/completions"
    assert transport["body"]["model"] == _MODEL_ID
    assert transport["credential_scope"] == (
        "ai.stream" if streaming else "ai.generate"
    )
    if streaming:
        assert "".join(
            event["delta"] for event in result["events"]
            if event["type"] == "text_delta"
        ) == _REPLY
        assert result["events"][-1]["type"] == "finish"
    else:
        assert result["output"] == _REPLY
    assert _PROFILE_HANDLE not in str(result)
    assert _REGISTRY_HANDLE not in str(result)


@pytest.mark.parametrize("source", ["save", "migration"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "supplied_handle", ["credential:synthetic-caller-forgery", "raw-synthetic-value"],
)
def test_explicit_request_cannot_supply_provider_credential(
    tmp_path: Path, source: str, streaming: bool, supplied_handle: str,
) -> None:
    """Ignoring owner hints must not admit a caller-selected credential."""
    models, providers = _owners(tmp_path, source, _PROFILE_HANDLE)
    client = OwnerRouteClient(models, providers)
    with pytest.raises(GlobalContractInvocationError) as denied:
        _invoke(client, streaming, credential_handle=supplied_handle)

    assert denied.value.code == "denied"
    assert not client.transport_requests
    assert supplied_handle not in str(denied.value)
