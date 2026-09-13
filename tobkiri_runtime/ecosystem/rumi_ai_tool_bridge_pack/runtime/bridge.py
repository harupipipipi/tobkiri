"""Normalize provider tool calls into non-authoritative operation descriptors."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


def create_tool_intent_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create a pure provider-tool-call normalization operation."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {
            "normalize",
            "validate",
            "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.generate",
            "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.stream",
        }:
            raise ValueError(f"unknown AI tool bridge operation: {name}")
        values = payload.get("intents")
        values = values if isinstance(values, list) else []
        request_id = str(payload.get("request_id") or "").strip()
        normalized = [
            _normalize(item, request_id, index)
            for index, item in enumerate(values)
            if isinstance(item, Mapping)
        ]
        if len(normalized) != len(values):
            raise _invalid("tool intent must be an object")
        return {"intents": normalized}

    return operation


def _normalize(
    value: Mapping[str, Any],
    request_id: str,
    index: int,
) -> dict[str, Any]:
    function = value.get("function")
    function = function if isinstance(function, Mapping) else value
    name = str(function.get("name") or value.get("name") or "").strip()
    if _TOOL_NAME.fullmatch(name) is None:
        raise _invalid("tool intent name is invalid")
    arguments = function.get("arguments", value.get("arguments", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            raise _invalid("tool intent arguments are invalid JSON") from None
    if not isinstance(arguments, Mapping):
        raise _invalid("tool intent arguments must be an object")
    intent_id = str(value.get("id") or f"{request_id}:tool:{index}")
    return {
        "intent_id": intent_id,
        "request_id": request_id,
        "operation": name,
        "arguments": dict(arguments),
        "authority_granted": False,
        "approved": False,
        "approval_status": "unrequested",
        "executes": False,
    }


def _invalid(message: str) -> GlobalContractInvocationError:
    return GlobalContractInvocationError("invalid_response", message)


_PACK_ID = "rumi_ai_tool_bridge_pack"
_FUNCTION_ID = "rumi_ai_tool_bridge_pack.ai-tool-bridge.normalize"


class AIToolBridgeHostFactoryV4:
    """Bind tool-intent projection to the exact Host Provider principal."""

    function_id = _FUNCTION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Capture only manifest-resolved tool bridge operations."""

        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            for binding in context.provider_bindings
        ):
            raise PermissionError("AI tool bridge bindings are incomplete")

        def invoke(
            _operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(),
                consumer_pack_id=_PACK_ID,
            )
            return create_tool_intent_operation(client)("normalize", payload)

        return CapturedHostProviderV4(
            tuple(_contributions(context, invoke)),
            lambda: None,
        )


def _contributions(
    context: HostProviderCaptureContextV4,
    invoke: Callable[
        [str, Mapping[str, Any], HostProviderInvocationContextV4], Mapping[str, Any]
    ],
) -> list[HostProviderContributionV4]:
    """Map exact resolver bindings to non-authoritative Host contributions."""

    contributions: list[HostProviderContributionV4] = []
    for binding in context.provider_bindings:
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("AI tool bridge domain binding is unavailable")
        contributions.append(
            HostProviderContributionV4(
                contract_id=binding.operation.contract_id,
                contract_version=binding.operation.contract_version,
                operation_id=binding.operation.operation_id,
                principal_id=binding.principal_ref.value,
                artifact_digest=binding.artifact.digest,
                implementation_digest=binding.function.implementation_digest,
                domain_id=domain_id,
                invoke=invoke,
            )
        )
    return contributions


HOST_PROVIDER_FACTORY = AIToolBridgeHostFactoryV4()
