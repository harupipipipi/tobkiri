"""Bounded multi-message text generation for composable conversation graphs."""
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractClient, GlobalContractInvocationError
from ecosystem.rumi_ai_gateway_pack.runtime import gateway
from tobkiri_protocol.canonical import canonical_json

CONTRACT_ID = "tobkiri.service.ai.messages.generate.v1"
FUNCTION_ID = "rumi_ai_gateway_pack.ai-gateway.messages-generate"
_MAX_TEXT = 262_144
_MAX_INPUT_BYTES = 512 * 1024


def _input(payload: Mapping[str, Any]) -> tuple[list[dict[str, str]], str]:
    if not isinstance(payload, Mapping) or set(payload) != {"messages", "model_profile_id"}:
        raise ValueError("message generation fields are invalid")
    model = payload["model_profile_id"]
    messages = payload["messages"]
    if type(model) is not str or not model or model != model.strip() or len(model) > 256 or any(ord(c) < 32 for c in model):
        raise ValueError("registered model profile is required")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
        raise ValueError("bounded conversation messages are required")
    checked = []
    for item in messages:
        if not isinstance(item, Mapping) or set(item) != {"role", "content"}:
            raise ValueError("conversation message fields are invalid")
        role, content = item["role"], item["content"]
        if role not in ("system", "user", "assistant") or type(content) is not str or not 1 <= len(content) <= _MAX_TEXT:
            raise ValueError("conversation text message is invalid")
        checked.append({"role": role, "content": content})
    if len(canonical_json({"messages": checked, "model_profile_id": model})) > _MAX_INPUT_BYTES:
        raise ValueError("conversation input exceeds node budget")
    return checked, model


def create_messages_generate_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Keep message roles/history intact while retaining ordinary Gateway policy."""
    generate = gateway.create_generate_operation(client)

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != FUNCTION_ID:
            raise ValueError("message generation operation is invalid")
        messages, model = _input(payload)
        resolved = client.invoke(gateway.MODEL_PROFILE_CONTRACT, gateway.MODEL_PROFILE_GENERATE_OPERATION, {"identifier": model})
        profile = resolved.get("profile") if isinstance(resolved, Mapping) else None
        if not isinstance(profile, Mapping) or profile.get("enabled") is not True:
            raise GlobalContractInvocationError("unresolved_profile", "enabled model profile is required")
        resolved_id = resolved.get("resolved_profile_id") or model
        if type(resolved_id) is not str or not resolved_id or resolved_id != resolved_id.strip() or len(resolved_id) > 256:
            raise GlobalContractInvocationError("unresolved_profile", "resolved profile identity is invalid")
        result = generate("generate", {
            "messages": messages, "model_profile_id": resolved_id,
            "requirements": {"modalities": ["text"], "request_surface": "chat"},
        })
        text = result.get("output") if isinstance(result, Mapping) else None
        if not isinstance(result, Mapping) or result.get("status") != "ok" or result.get("tool_intents") or (
            type(text) is not str or not text.strip() or len(text) > _MAX_TEXT
        ):
            raise GlobalContractInvocationError("invalid_response", "bounded completed text response is required")
        # Full parameters, credential handles and floating telemetry stay inside
        # Gateway; Workflow journals only this finite canonical projection.
        return {"status": "ok", "text": text, "model_profile_id": resolved_id}

    return operation


HOST_PROVIDER_FACTORY = {
    FUNCTION_ID: gateway.AIGatewayHostFactoryV4(
        FUNCTION_ID, contract_id=CONTRACT_ID, operation_id=FUNCTION_ID,
        operation_name=FUNCTION_ID, allowed_contract_ids=gateway._GENERATE_ALLOWED_CONTRACTS,
        operation_factory=create_messages_generate_operation,
    ),
}
