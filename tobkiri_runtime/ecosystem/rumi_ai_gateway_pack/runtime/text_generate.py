"""Typed text-in/text-out generation for user Flow composition.

This module adapts a minimal TEXT node contract onto the ordinary
non-streaming AI Gateway pipeline.  The caller supplies only bounded text
and a registered model profile identifier; the Gateway keeps request
preparation, model-profile resolution, routing, usage accounting,
deadlines, cancellation, and credential boundaries unchanged.  The node
verifies the resolved profile is enabled before delegating, so a disabled
profile can never reach a provider through the typed surface, and it
projects the provider result down to text plus safe identity only.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)

from ecosystem.rumi_ai_gateway_pack.runtime import gateway

CONTRACT_ID = "tobkiri.service.ai.text.generate.v1"
FUNCTION_ID = "rumi_ai_gateway_pack.ai-gateway.text-generate"

_TEXT_MAX_LENGTH = 262_144
_SYSTEM_PROMPT_MAX_LENGTH = 16_384
_IDENTIFIER_MAX_LENGTH = 256


def create_text_generate_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the bounded text-in/text-out generation operation."""
    generate = gateway.create_generate_operation(client)

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != FUNCTION_ID:
            raise ValueError("AI text generate operation is invalid")
        text, model_profile_id, system_prompt = _validated_input(payload)
        resolved = client.invoke(
            gateway.MODEL_PROFILE_CONTRACT,
            gateway.MODEL_PROFILE_GENERATE_OPERATION,
            {"identifier": model_profile_id},
        )
        profile = (
            resolved.get("profile") if isinstance(resolved, Mapping) else None
        )
        if not isinstance(profile, Mapping):
            raise GlobalContractInvocationError(
                "unresolved_profile",
                "model profile owner returned an invalid record",
            )
        if profile.get("enabled") is not True:
            raise GlobalContractInvocationError(
                "disabled_profile", "model profile is disabled"
            )
        resolved_profile_id = str(
            resolved.get("resolved_profile_id") or model_profile_id
        )
        messages = (
            [{"role": "system", "content": system_prompt}]
            if system_prompt
            else []
        ) + [{"role": "user", "content": text}]
        result = generate(
            "generate",
            {
                "messages": messages,
                "model_profile_id": resolved_profile_id,
                "requirements": {
                    "modalities": ["text"],
                    "request_surface": "chat",
                },
            },
        )
        output = result.get("output") if isinstance(result, Mapping) else None
        if not isinstance(output, str) or len(output) > _TEXT_MAX_LENGTH:
            raise GlobalContractInvocationError(
                "invalid_response",
                "provider result has no bounded text output",
            )
        return {
            "status": "ok",
            "request_id": str(result.get("request_id") or ""),
            "model_id": str(result.get("model_id") or ""),
            "model_profile_id": resolved_profile_id,
            "provider_instance_id": str(
                result.get("provider_instance_id") or ""
            ),
            "text": output,
        }

    return operation


def _validated_input(
    payload: Mapping[str, Any],
) -> tuple[str, str, str | None]:
    """Return bounded text fields or reject before any dependency call."""
    permitted = {"text", "model_profile_id", "system_prompt"}
    if not isinstance(payload, Mapping) or set(payload) - permitted:
        raise ValueError("AI text generate input fields are invalid")
    text = payload.get("text")
    model_profile_id = _identifier(payload.get("model_profile_id"))
    system_prompt = payload.get("system_prompt")
    if (
        not isinstance(text, str)
        or not 1 <= len(text) <= _TEXT_MAX_LENGTH
        or model_profile_id is None
        or (
            system_prompt is not None
            and (
                not isinstance(system_prompt, str)
                or len(system_prompt) > _SYSTEM_PROMPT_MAX_LENGTH
            )
        )
    ):
        raise ValueError("AI text generate input is invalid")
    return text, model_profile_id, system_prompt


def _identifier(value: Any) -> str | None:
    """Return one bounded identifier or ``None`` for an invalid value."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > _IDENTIFIER_MAX_LENGTH:
        return None
    return normalized


HOST_PROVIDER_FACTORY = {
    FUNCTION_ID: gateway.AIGatewayHostFactoryV4(
        FUNCTION_ID,
        contract_id=CONTRACT_ID,
        operation_id=FUNCTION_ID,
        operation_name=FUNCTION_ID,
        allowed_contract_ids=gateway._GENERATE_ALLOWED_CONTRACTS,
        operation_factory=create_text_generate_operation,
    ),
}
