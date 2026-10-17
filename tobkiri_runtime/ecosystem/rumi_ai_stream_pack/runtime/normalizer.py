"""Normalize typed provider stream events without provider-specific logic."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

_ALLOWED_TYPES = {
    "text_delta",
    "thinking_delta",
    "tool_intent_delta",
    "usage",
    "finish",
    "error",
}


def create_stream_normalize_operation(client: Any):
    """Create a pure typed stream normalizer."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {
            "normalize",
            "validate",
            "rumi_ai_stream_pack.ai-stream-normalize",
        }:
            raise ValueError(f"unknown stream normalization operation: {name}")
        request_id = str(payload.get("request_id") or "").strip()
        provider_attempt = int(payload.get("provider_attempt") or 1)
        value = payload.get("value")
        events = value.get("events") if isinstance(value, Mapping) else value
        if (
            not request_id
            or not isinstance(events, Iterable)
            or isinstance(events, (str, bytes, Mapping))
        ):
            raise GlobalContractInvocationError(
                "invalid_response",
                "provider stream must contain iterable events and request_id",
            )
        normalized = []
        finished = False
        for sequence, event in enumerate(events):
            if not isinstance(event, Mapping):
                raise GlobalContractInvocationError(
                    "invalid_response", "stream event must be an object"
                )
            event_type = str(event.get("type") or "")
            if event_type not in _ALLOWED_TYPES:
                raise GlobalContractInvocationError(
                    "invalid_response", f"unknown stream event type: {event_type}"
                )
            if finished:
                raise GlobalContractInvocationError(
                    "invalid_response", "stream emitted an event after finish"
                )
            normalized.append(
                {
                    "request_id": request_id,
                    "sequence": sequence,
                    "type": event_type,
                    "delta": event.get("delta"),
                    "tool_intent": event.get("tool_intent"),
                    "usage": event.get("usage"),
                    "finish_reason": event.get("finish_reason"),
                    "error_code": event.get("error_code"),
                    "provider_attempt": provider_attempt,
                }
            )
            finished = event_type in {"finish", "error"}
        if not normalized or not finished:
            raise GlobalContractInvocationError(
                "invalid_response", "stream is missing a terminal event"
            )
        return {"events": normalized}

    return operation


_PACK_ID = "rumi_ai_stream_pack"
_FUNCTION_ID = "rumi_ai_stream_pack.ai-stream.normalize"


class AIStreamHostFactoryV4:
    """Capture the pure stream normalizer for exact Host Provider dispatch."""

    function_id = _FUNCTION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind only resolved normalizer Function principals and domains."""

        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            for binding in context.provider_bindings
        ):
            raise PermissionError("AI stream bindings are incomplete")

        def invoke(
            _operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(),
                consumer_pack_id=_PACK_ID,
            )
            return create_stream_normalize_operation(client)("normalize", payload)

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
    """Create immutable contributions from resolved Host Function bindings."""

    contributions: list[HostProviderContributionV4] = []
    for binding in context.provider_bindings:
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("AI stream domain binding is unavailable")
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


HOST_PROVIDER_FACTORY = AIStreamHostFactoryV4()
