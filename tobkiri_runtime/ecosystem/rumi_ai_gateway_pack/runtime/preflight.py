"""Captured model readiness without generation, billing or credential transport."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import (
    GlobalContractClient, GlobalContractUnavailable, V4ContractDispatch,
)
from tobkiri_protocol.canonical import canonical_json

from ecosystem.rumi_ai_gateway_pack.runtime import gateway

CONTRACT_ID = "tobkiri.resource.ai.readiness.v1"
FUNCTION_ID = "rumi_ai_gateway_pack.ai-gateway.preflight"
_READ_OPERATIONS = frozenset({
    (gateway.CATALOG_CONTRACT, gateway.CATALOG_GENERATE_OPERATION),
    (gateway.HEALTH_CONTRACT, gateway.HEALTH_GENERATE_OPERATION),
    (gateway.MODEL_PROFILE_CONTRACT, gateway.MODEL_PROFILE_GENERATE_OPERATION),
    (gateway.REQUEST_PREPARE_CONTRACT, gateway.REQUEST_PREPARE_GENERATE_OPERATION),
    (gateway.ROUTING_CONTRACT, gateway.ROUTING_GENERATE_OPERATION),
})
_CONTRACTS = frozenset(contract for contract, _ in _READ_OPERATIONS) | {
    gateway.GENERATE_PROVIDER_CONTRACT,  # Selected metadata only, never invocation.
}


class _ReadOnlyDispatch:
    def __init__(self, captured: V4ContractDispatch) -> None:
        self._captured = captured
        self.profile_id = captured.profile_id
        self.plan_digest = captured.plan_digest

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, Any], ...]:
        if contract_id not in _CONTRACTS:
            raise PermissionError("AI preflight metadata target is not allowed")
        return self._captured.provider_metadata(contract_id)

    def invoke(
        self, contract_id: str, operation_id: str, payload: Mapping[str, Any], *,
        version_range: str | None = None,
    ) -> Mapping[str, Any]:
        if (contract_id, operation_id) not in _READ_OPERATIONS:
            raise PermissionError("AI preflight cannot execute this target")
        return self._captured.invoke(contract_id, operation_id, payload, version_range=version_range)


def create_preflight_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Reuse gateway resolution behind an exact read-only dispatch allowlist."""
    readonly = GlobalContractClient(
        session=_ReadOnlyDispatch(client.session),
        allowed_contract_ids=client.allowed_contract_ids & _CONTRACTS,
        consumer_pack_id=client.consumer_pack_id,
        # Deliberately no credential transport capability.
    )
    resolve = gateway.create_generate_operation(readonly)

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != FUNCTION_ID:
            raise ValueError("AI preflight operation is invalid")
        if set(payload) - {"tool_calling"} != {"model_profile_id", "messages"}:
            raise ValueError("AI preflight input fields are invalid")
        if type(payload.get("tool_calling", False)) is not bool:
            raise ValueError("AI preflight tool requirement is invalid")
        identifier = payload["model_profile_id"]
        messages = payload["messages"]
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 256
                or not isinstance(messages, list) or not messages or len(messages) > 2048
                or any(not isinstance(message, dict) or set(message) != {"role", "content"}
                       or message["role"] not in ("system", "user", "assistant")
                       or not isinstance(message["content"], str) for message in messages)
                or len(canonical_json(dict(payload))) > 60 * 1024):
            raise ValueError("AI preflight input is invalid")
        resolved = resolve("resolve", {
            "model_profile_id": identifier, "messages": messages,
            **({"requirements": {"tool_calling": True}} if payload.get("tool_calling") else {}),
        })
        selected = [item for item in readonly.providers(gateway.GENERATE_PROVIDER_CONTRACT)
                    if item.get("provider_instance_id") == resolved.get("provider_instance_id")
                    and item.get("operation_id") == gateway.GENERATE_PROVIDER_OPERATION]
        if len(selected) != 1:
            raise GlobalContractUnavailable("AI preflight selected provider operation is unavailable")
        # Expose neither credential material nor execution handles/pricing internals.
        result = {key: resolved[key] for key in ("model_id", "provider_instance_id", "catalog_revision")}
        if any(not isinstance(value, str) or not value or len(value) > 512 for value in result.values()):
            raise ValueError("AI preflight resolution is invalid")
        return {"ready": True, "model_profile_id": identifier, **result}

    return operation


HOST_PROVIDER_FACTORY = {
    FUNCTION_ID: gateway.AIGatewayHostFactoryV4(
        FUNCTION_ID, contract_id=CONTRACT_ID, operation_id=FUNCTION_ID,
        operation_name=FUNCTION_ID, allowed_contract_ids=_CONTRACTS,
        operation_factory=create_preflight_operation,
    ),
}
