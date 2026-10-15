"""Deterministic request normalization and replay-safe failover decisions."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

_RETRYABLE = {"provider_unavailable", "quota", "invalid_response"}
_REQUIREMENT_KEYS = {
    "modalities",
    "capabilities",
    "tool_calling",
    "thinking",
    "minimum_context",
    "request_surface",
    "data_residency",
    "maximum_cost",
    "preferred_model_id",
    "preferred_provider_id",
    "preferred_provider_instance_id",
    "health_max_age",
}


def create_prepare_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create deterministic provider-neutral request preparation."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {
            "prepare",
            "normalize",
            "rumi_ai_pipeline_pack.ai-request-prepare.generate",
            "rumi_ai_pipeline_pack.ai-request-prepare.stream",
        }:
            raise ValueError(f"unknown AI pipeline operation: {name}")
        decision_time = _number(payload.get("decision_time"))
        if decision_time is None:
            raise ValueError("AI request decision_time is required")
        deadline = _number(payload.get("deadline"))
        deadline = deadline if deadline is not None else decision_time + 60.0
        if deadline <= decision_time:
            raise GlobalContractInvocationError(
                "deadline", "request deadline elapsed"
            )
        credential_handle = payload.get("credential_handle")
        if credential_handle is not None and not str(
            credential_handle
        ).startswith(("credential:", "opaque:")):
            raise GlobalContractInvocationError(
                "denied", "AI pipeline accepts only opaque credential handles"
            )
        requirements = payload.get("requirements")
        requirements = requirements if isinstance(requirements, Mapping) else {}
        request_id = str(payload.get("request_id") or "").strip()
        if not request_id:
            raise ValueError("AI request_id is required")
        return {
            "request_id": request_id,
            "decision_time": decision_time,
            "deadline": deadline,
            "messages": list(payload.get("messages") or []),
            "input": payload.get("input"),
            "parameters": dict(payload.get("parameters") or {}),
            "tools": list(payload.get("tools") or []),
            "requirements": {
                str(key): value
                for key, value in requirements.items()
                if key in _REQUIREMENT_KEYS
            },
            "credential_handle": credential_handle,
            "idempotency_key": payload.get("idempotency_key"),
            "allow_failover": bool(payload.get("allow_failover", False)),
            "policy_revision": str(payload.get("policy_revision") or ""),
            "conversation_id": payload.get("conversation_id"),
            "profile_id": payload.get("profile_id"),
            "model_profile_id": payload.get("model_profile_id"),
            "model_reference": payload.get("model_reference"),
        }

    return operation


def create_failover_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create replay-safe retry/failover policy decisions."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {
            "decide",
            "allow",
            "rumi_ai_pipeline_pack.ai-failover-decide.generate",
            "rumi_ai_pipeline_pack.ai-failover-decide.stream",
        }:
            raise ValueError(f"unknown AI failover operation: {name}")
        error_code = str(payload.get("error_code") or "")
        attempt = max(1, int(payload.get("attempt") or 1))
        candidate_count = max(0, int(payload.get("candidate_count") or 0))
        deadline = _number(payload.get("deadline"))
        decision_time = _number(payload.get("decision_time"))
        checks = {
            "explicitly_allowed": bool(payload.get("allow_failover", False)),
            "idempotency_bound": bool(payload.get("idempotency_key")),
            "tool_free": not bool(payload.get("tools")),
            "retryable_error": error_code in _RETRYABLE,
            "candidate_available": attempt < candidate_count,
            "deadline_remaining": (
                deadline is not None
                and decision_time is not None
                and decision_time < deadline
            ),
        }
        allowed = all(checks.values())
        return {
            "allowed": allowed,
            "checks": checks,
            "reason": "replay_safe_failover"
            if allowed else next(key for key, value in checks.items() if not value),
        }

    return operation


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_PACK_ID = "rumi_ai_pipeline_pack"
_PIPELINE_OPERATIONS: dict[
    str,
    tuple[str, Callable[[Any], Callable[[str, Mapping[str, Any]], dict[str, Any]]]],
] = {
    "rumi_ai_pipeline_pack.ai-pipeline.prepare": (
        "prepare",
        create_prepare_operation,
    ),
    "rumi_ai_pipeline_pack.ai-pipeline.failover": (
        "decide",
        create_failover_operation,
    ),
}


class AIPipelineHostFactoryV4:
    """Bind one manifest-selected pipeline function to Host broker dispatch."""

    def __init__(self, function_id: str) -> None:
        if function_id not in _PIPELINE_OPERATIONS:
            raise ValueError("AI pipeline function is not registered")
        self.function_id = function_id

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Capture only bindings that resolve to this exact pipeline function."""

        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            for binding in context.provider_bindings
        ):
            raise PermissionError("AI pipeline bindings are incomplete")
        operation_name, operation_factory = _PIPELINE_OPERATIONS[self.function_id]

        def invoke(
            _operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            # This function has no manifest requested contract edges. Still
            # obtain the invocation-bound client so it cannot acquire ambient
            # Host capabilities if the pure operation grows in the future.
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(),
                consumer_pack_id=_PACK_ID,
            )
            return operation_factory(client)(operation_name, payload)

        contributions = _contributions(context, invoke)
        return CapturedHostProviderV4(tuple(contributions), lambda: None)


def _contributions(
    context: HostProviderCaptureContextV4,
    invoke: Callable[
        [str, Mapping[str, Any], HostProviderInvocationContextV4], Mapping[str, Any]
    ],
) -> list[HostProviderContributionV4]:
    """Project verified bindings into exact Host Provider contributions."""

    contributions: list[HostProviderContributionV4] = []
    for binding in context.provider_bindings:
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("AI pipeline domain binding is unavailable")
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
    function_id: AIPipelineHostFactoryV4(function_id)
    for function_id in _PIPELINE_OPERATIONS
}
