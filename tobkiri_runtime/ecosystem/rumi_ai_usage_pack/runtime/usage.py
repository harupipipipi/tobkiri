"""Explicit estimated token counts and provider-reported usage cost."""

from __future__ import annotations

import json
import math
from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)


def create_tokenize_operation(client: Any):
    """Create a deterministic tokenizer-estimate contract."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"estimate", "count"}:
            raise ValueError(f"unknown tokenizer operation: {name}")
        value = payload.get("input", payload.get("messages", ""))
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "tokens": max(1, math.ceil(len(encoded) / 4)),
            "provenance": "deterministic_estimate",
            "exact": False,
        }

    return operation


def create_cost_operation(client: Any):
    """Create a cost calculator that preserves unknown values."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {
            "calculate",
            "normalize",
            "rumi_ai_usage_pack.ai-usage-cost.generate",
            "rumi_ai_usage_pack.ai-usage-cost.stream",
        }:
            raise ValueError(f"unknown usage cost operation: {name}")
        usage = payload.get("usage")
        pricing = payload.get("pricing")
        usage = usage if isinstance(usage, Mapping) else {}
        pricing = pricing if isinstance(pricing, Mapping) else {}
        input_tokens = _number(
            usage.get("input_tokens", usage.get("prompt_tokens"))
        )
        output_tokens = _number(
            usage.get("output_tokens", usage.get("completion_tokens"))
        )
        input_rate = _number(pricing.get("input"))
        output_rate = _number(pricing.get("output"))
        known = None not in {
            input_tokens,
            output_tokens,
            input_rate,
            output_rate,
        }
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": (
                input_tokens + output_tokens
                if input_tokens is not None and output_tokens is not None
                else None
            ),
            "cost": (
                input_tokens * input_rate + output_tokens * output_rate
                if known else None
            ),
            "currency": str(pricing.get("currency") or "USD") if known else None,
            "known": known,
            "usage_provenance": str(
                payload.get("usage_provenance") or "provider_reported"
            ),
            "pricing_revision": payload.get("pricing_revision"),
        }

    return operation


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


_PACK_ID = "rumi_ai_usage_pack"
_USAGE_OPERATIONS: dict[
    str,
    tuple[str, Callable[[Any], Callable[[str, Mapping[str, Any]], dict[str, Any]]]],
] = {
    "rumi_ai_usage_pack.ai-usage.cost": ("calculate", create_cost_operation),
    "rumi_ai_usage_pack.ai-usage.tokenize": ("estimate", create_tokenize_operation),
}


class AIUsageHostFactoryV4:
    """Capture exact usage/tokenization Host Provider contributions."""

    def __init__(self, function_id: str) -> None:
        if function_id not in _USAGE_OPERATIONS:
            raise ValueError("AI usage function is not registered")
        self.function_id = function_id

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind only the exact resolved usage Function identity."""

        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            for binding in context.provider_bindings
        ):
            raise PermissionError("AI usage bindings are incomplete")
        operation_name, operation_factory = _USAGE_OPERATIONS[self.function_id]

        def invoke(
            _operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(),
                consumer_pack_id=_PACK_ID,
            )
            return operation_factory(client)(operation_name, payload)

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
    """Construct immutable exact-principal usage contributions."""

    contributions: list[HostProviderContributionV4] = []
    for binding in context.provider_bindings:
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("AI usage domain binding is unavailable")
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


HOST_PROVIDER_FACTORY = {
    function_id: AIUsageHostFactoryV4(function_id)
    for function_id in _USAGE_OPERATIONS
}
