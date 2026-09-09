"""Exercise the Defaults settings record through the actual AI routing code."""

from typing import Any

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.defaultspack.defaultspack.model_profile_presentation import (
    normalize_model_profile_save,
)
from ecosystem.rumi_ai_gateway_pack.runtime import gateway
from ecosystem.rumi_model_catalog_pack.runtime.catalog import (
    create_model_catalog_operation,
)
from ecosystem.rumi_provider_adapters_pack.runtime.adapter import _connection
from tests.test_ai_gateway_pack import FakeContractClient


class SettingsRouteClient(FakeContractClient):
    """Use real settings/catalog/router; replace only owner and Provider I/O."""

    def __init__(self, model_id: str) -> None:
        super().__init__()
        self.record = normalize_model_profile_save({
            "model_profile_id": "daily", "model_id": model_id,
            "provider_instance_id": "provider.deepseek.main",
            "display_name": "Daily", "expected_revision": 0,
        })["record"]
        self.provider_requests: list[dict[str, Any]] = []

    def providers(self, contract_id: str) -> tuple[dict[str, Any], ...]:
        if contract_id in {
            gateway.GENERATE_PROVIDER_CONTRACT, gateway.STREAM_PROVIDER_CONTRACT,
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
        return super().providers(contract_id)

    def invoke(
        self, contract_id: str, operation: str, payload: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if contract_id == gateway.MODEL_PROFILE_CONTRACT:
            return {"profile": self.record, "resolved_profile_id": "daily"}
        if contract_id == gateway.CATALOG_CONTRACT:
            return create_model_catalog_operation(None)(operation, payload)
        if contract_id in {
            gateway.GENERATE_PROVIDER_CONTRACT, gateway.STREAM_PROVIDER_CONTRACT,
        }:
            self.provider_requests.append(dict(payload))
            # Match the real adapter's registry lookup, not a fabricated route.
            owner = type("Registry", (), {"invoke": lambda *args: {
                "providers": [{"provider_instance_id": "provider.deepseek.main",
                               "enabled": True}]
            }})()
            assert _connection(owner, payload)["provider_instance_id"] == (
                "provider.deepseek.main"
            )
        return super().invoke(contract_id, operation, payload, **kwargs)


@pytest.mark.parametrize("model_id", ["deepseek-chat", "organization/model"])
@pytest.mark.parametrize("streaming", [False, True])
def test_settings_model_routes_to_its_connection(
    model_id: str, streaming: bool,
) -> None:
    """Raw Provider model IDs must survive without catalog brand prefixes."""
    client = SettingsRouteClient(model_id)
    create = gateway.create_stream_operation if streaming else gateway.create_generate_operation
    create(client)("stream" if streaming else "generate", {
        "model_reference": "daily", "messages": [],
        "requirements": {"request_surface": "conversation.saved"},
    })
    assert len(client.provider_requests) == 1
    assert client.provider_requests[0]["model_id"] == model_id
    assert client.provider_requests[0]["provider_id"] == "deepseek.main"


def test_settings_route_does_not_invent_tool_capability() -> None:
    """A manually configured connection is not model capability evidence."""
    client = SettingsRouteClient("deepseek-chat")
    with pytest.raises(GlobalContractInvocationError):
        gateway.create_generate_operation(client)("generate", {
            "model_reference": "daily", "messages": [],
            "requirements": {"tool_calling": True},
        })
    assert not client.provider_requests
