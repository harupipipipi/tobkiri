"""Read-only route quotes for strategy Packs.

This module resolves a model route through the same Pack-neutral policy used
by generation, while deliberately withholding credential and invocation
handles.  Strategy Packs can use the quote for their own admission policies
and then invoke the ordinary generation contract themselves.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractUnavailable,
    V4ContractDispatch,
)
from tobkiri_protocol.canonical import canonical_json

from ecosystem.rumi_ai_gateway_pack.runtime import gateway

CONTRACT_ID = "tobkiri.resource.ai.route.quote.v1"
FUNCTION_ID = "rumi_ai_gateway_pack.ai-gateway.route-quote"
_READ_OPERATIONS = frozenset({
    (gateway.CATALOG_CONTRACT, gateway.CATALOG_GENERATE_OPERATION),
    (gateway.HEALTH_CONTRACT, gateway.HEALTH_GENERATE_OPERATION),
    (gateway.MODEL_PROFILE_CONTRACT, gateway.MODEL_PROFILE_GENERATE_OPERATION),
    (
        gateway.PROVIDER_REGISTRY_CONTRACT,
        gateway.PROVIDER_REGISTRY_GENERATE_OPERATION,
    ),
    (gateway.REQUEST_PREPARE_CONTRACT, gateway.REQUEST_PREPARE_GENERATE_OPERATION),
    (gateway.ROUTING_CONTRACT, gateway.ROUTING_GENERATE_OPERATION),
})
_READ_OPERATIONS = _READ_OPERATIONS | frozenset({
    (gateway.CATALOG_CONTRACT, gateway.CATALOG_STREAM_OPERATION),
    (gateway.HEALTH_CONTRACT, gateway.HEALTH_STREAM_OPERATION),
    (gateway.MODEL_PROFILE_CONTRACT, gateway.MODEL_PROFILE_STREAM_OPERATION),
    (gateway.PROVIDER_REGISTRY_CONTRACT, gateway.PROVIDER_REGISTRY_STREAM_OPERATION),
    (gateway.REQUEST_PREPARE_CONTRACT, gateway.REQUEST_PREPARE_STREAM_OPERATION),
    (gateway.ROUTING_CONTRACT, gateway.ROUTING_STREAM_OPERATION),
})
_CONTRACTS = frozenset(contract for contract, _ in _READ_OPERATIONS) | {
    # Read provider metadata only to prove that the selected route is executable.
    gateway.GENERATE_PROVIDER_CONTRACT, gateway.STREAM_PROVIDER_CONTRACT,
}


class _ReadOnlyDispatch:
    """Limit a captured dispatch to metadata and route-resolution reads."""

    def __init__(self, captured: V4ContractDispatch) -> None:
        self._captured = captured
        self.profile_id = captured.profile_id
        self.plan_digest = captured.plan_digest

    def provider_metadata(
        self,
        contract_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        """Return only metadata for declared quote dependencies."""
        if contract_id not in _CONTRACTS:
            raise PermissionError("AI route quote metadata target is not allowed")
        return self._captured.provider_metadata(contract_id)

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, Any]:
        """Perform only a declared read operation."""
        if (contract_id, operation_id) not in _READ_OPERATIONS:
            raise PermissionError("AI route quote cannot execute this target")
        return self._captured.invoke(
            contract_id,
            operation_id,
            payload,
            version_range=version_range,
        )


def create_route_quote_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create an owner-bound, credential-free route quote operation."""
    readonly = GlobalContractClient(
        session=_ReadOnlyDispatch(client.session),
        allowed_contract_ids=client.allowed_contract_ids & _CONTRACTS,
        consumer_pack_id=client.consumer_pack_id,
    )

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != FUNCTION_ID:
            raise ValueError("AI route quote operation is invalid")
        _validate_quote_input(payload)
        model_profile_id, model_reference = _quote_model_reference(payload)
        if not (model_profile_id or model_reference):
            raise ValueError("AI route quote requires a model reference")
        streaming = payload.get("delivery_mode", "buffered") == "incremental"
        resolve = gateway.create_stream_operation(readonly) if streaming else gateway.create_generate_operation(readonly)
        provider_contract = gateway.STREAM_PROVIDER_CONTRACT if streaming else gateway.GENERATE_PROVIDER_CONTRACT
        provider_operation = gateway.STREAM_PROVIDER_OPERATION if streaming else gateway.GENERATE_PROVIDER_OPERATION
        resolved = resolve(
            "resolve",
            {
                "messages": [dict(message) for message in payload["messages"]],
                **(
                    {"model_profile_id": model_profile_id}
                    if model_profile_id
                    else {"model_reference": model_reference}
                ),
                **(
                    {"requirements": dict(payload["requirements"])}
                    if isinstance(payload.get("requirements"), Mapping)
                    else {}
                ),
            },
        )
        selected = [
            item
            for item in readonly.providers(provider_contract)
            if item.get("provider_instance_id")
            == resolved.get("provider_instance_id")
            and item.get("operation_id") == provider_operation
        ]
        if len(selected) != 1:
            raise GlobalContractUnavailable(
                "AI route quote selected provider operation is unavailable"
            )
        result = {
            key: resolved[key]
            for key in (
                "model_id",
                "provider_instance_id",
                "catalog_provider_instance_id",
                "catalog_revision",
                "pricing_revision",
            )
        }
        required_identifiers = {
            "model_id",
            "provider_instance_id",
            "catalog_revision",
            "pricing_revision",
        }
        if any(
            not isinstance(result[key], str)
            or not result[key]
            or len(result[key]) > 512
            for key in required_identifiers
        ) or (
            not isinstance(result["catalog_provider_instance_id"], str)
            or len(result["catalog_provider_instance_id"]) > 512
        ):
            raise ValueError("AI route quote resolution is invalid")
        pricing = resolved.get("pricing")
        if not isinstance(pricing, Mapping):
            raise ValueError("AI route quote pricing is invalid")
        result["pricing"] = {
            "input": _quote_rate(pricing.get("input")),
            "output": _quote_rate(pricing.get("output")),
            "currency": _quote_currency(pricing.get("currency")),
            # The Gateway usage contract multiplies these catalog rates by
            # token counts directly. Make that existing unit explicit for the
            # Host aggregate budget ledger instead of letting a Pack guess.
            "unit": "usd_per_token",
        }
        return {
            "ready": True,
            **result,
            "route_binding": {
                key: result[key]
                for key in (
                    "model_id",
                    "provider_instance_id",
                    "catalog_provider_instance_id",
                    "catalog_revision",
                    "pricing_revision",
                    "pricing",
                )
            },
        }

    return operation


def _validate_quote_input(payload: Mapping[str, Any]) -> None:
    """Reject credential or execution fields before any dependency lookup."""
    permitted = {
        "model_profile_id",
        "model_reference",
        "messages",
        "requirements", "delivery_mode",
    }
    if set(payload) - permitted:
        raise ValueError("AI route quote input fields are invalid")
    if payload.get("delivery_mode", "buffered") not in {"buffered", "incremental"}:
        raise ValueError("AI route quote delivery mode is invalid")
    messages = payload.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or len(messages) > 2048
        or any(not isinstance(message, Mapping) for message in messages)
        or not isinstance(payload.get("requirements", {}), Mapping)
    ):
        raise ValueError("AI route quote input is invalid")
    try:
        input_size = len(canonical_json(dict(payload)))
    except (TypeError, ValueError):
        raise ValueError("AI route quote input is invalid") from None
    if input_size > 3 * 1024 * 1024:
        raise ValueError("AI route quote input is invalid")
    try:
        _quote_model_reference(payload)
    except ValueError:
        raise ValueError("AI route quote model reference is invalid") from None


def _quote_model_reference(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Normalize the string and structured model-reference forms.

    A model reference is either a legacy model/profile identifier string or the
    generic profile wrapper ``{"profile_id": "..."}``.  The wrapper lets a
    strategy keep its request envelope opaque while still resolving through the
    same profile authority as ordinary Gateway generation.
    """
    model_profile_id = _optional_identifier(payload.get("model_profile_id"))
    if payload.get("model_profile_id") is not None and not model_profile_id:
        raise ValueError("model profile id is invalid")
    reference_value = payload.get("model_reference")
    if reference_value is None:
        return model_profile_id, None
    model_reference = _optional_identifier(reference_value)
    if model_reference:
        return model_profile_id, model_reference
    if not isinstance(reference_value, Mapping) or set(reference_value) != {
        "profile_id"
    }:
        raise ValueError("model reference is invalid")
    profile_id = _optional_identifier(reference_value.get("profile_id"))
    if not profile_id:
        raise ValueError("model reference profile id is invalid")
    return model_profile_id or profile_id, None


def _optional_identifier(value: Any) -> str | None:
    """Return one bounded identifier or ``None`` for an absent optional value."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        return None
    return normalized


def _quote_rate(value: Any) -> str | None:
    """Return Gateway's exact decimal representation for a published rate."""
    return gateway.canonical_pricing_rate(value)


def _quote_currency(value: Any) -> str:
    """Return a short owner-provided currency label without guessing rates."""
    currency = str(value or "USD")
    return currency if 0 < len(currency) <= 16 else "USD"


HOST_PROVIDER_FACTORY = {
    FUNCTION_ID: gateway.AIGatewayHostFactoryV4(
        FUNCTION_ID,
        contract_id=CONTRACT_ID,
        operation_id=FUNCTION_ID,
        operation_name=FUNCTION_ID,
        allowed_contract_ids=_CONTRACTS,
        operation_factory=create_route_quote_operation,
    ),
}
