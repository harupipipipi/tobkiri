"""Deterministic model routing over explicit descriptors and health evidence."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)


def create_route_operation(client: Any):
    """Create a pure router with no catalog discovery or provider branches."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {
            "route",
            "match",
            "rumi_ai_routing_pack.ai-route.generate",
            "rumi_ai_routing_pack.ai-route.stream",
        }:
            raise ValueError(f"unknown AI routing operation: {name}")
        models = payload.get("models")
        providers = payload.get("execution_providers")
        health = payload.get("health")
        requirements = payload.get("requirements")
        decision_time = _number(payload.get("decision_time"))
        if decision_time is None:
            raise ValueError("routing decision_time is required")
        models = models if isinstance(models, list) else []
        providers = providers if isinstance(providers, list) else []
        health = health if isinstance(health, Mapping) else {}
        requirements = requirements if isinstance(requirements, Mapping) else {}
        provider_descriptors = [
            dict(item)
            for item in providers
            if isinstance(item, Mapping)
        ]
        candidates: list[dict[str, Any]] = []
        excluded: list[dict[str, str]] = []
        for model in models:
            if not isinstance(model, Mapping):
                continue
            candidate, reason = _candidate(
                model,
                provider_descriptors,
                health,
                requirements,
                decision_time,
            )
            if candidate is None:
                excluded.append(
                    {
                        "model_id": str(model.get("model_id") or "unknown"),
                        "provider_id": str(model.get("provider_id") or ""),
                        "reason": reason,
                    }
                )
            else:
                candidates.append(candidate)
        candidates.sort(key=lambda item: _sort_key(item, requirements))
        return {
            "selected": candidates[0] if candidates else None,
            "candidates": candidates,
            "excluded": excluded,
            "deterministic": True,
        }

    return operation


def _candidate(
    model: Mapping[str, Any],
    execution_providers: list[Mapping[str, Any]],
    health: Mapping[str, Any],
    requirements: Mapping[str, Any],
    decision_time: float,
) -> tuple[dict[str, Any] | None, str]:
    model_id = str(model.get("model_id") or "").strip()
    provider_id = str(model.get("provider_id") or "").strip()
    execution_id, execution_error = _execution_provider(
        provider_id,
        str(model.get("execution_provider_instance_id") or "").strip(),
        execution_providers,
    )
    if not model_id or not execution_id:
        return None, execution_error or "execution_provider_unresolved"
    if model.get("available", True) is False:
        return None, "model_unavailable"
    modalities = _strings(model.get("modalities"))
    capabilities = _strings(model.get("capabilities"))
    required_modalities = _strings(requirements.get("modalities")) or {"text"}
    required_capabilities = _strings(requirements.get("capabilities"))
    if not required_modalities.issubset(modalities):
        return None, "modality_mismatch"
    if bool(requirements.get("tool_calling")) and "tool_calling" not in capabilities:
        return None, "tool_calling_mismatch"
    if bool(requirements.get("thinking")) and "thinking" not in capabilities:
        return None, "thinking_mismatch"
    if not required_capabilities.issubset(capabilities):
        return None, "capability_mismatch"
    if _integer(model.get("context_length")) < _integer(
        requirements.get("minimum_context")
    ):
        return None, "context_length_mismatch"
    surfaces = _strings(model.get("request_surfaces"))
    request_surface = str(requirements.get("request_surface") or "chat")
    if surfaces and request_surface not in surfaces:
        return None, "request_surface_mismatch"
    required_residency = str(requirements.get("data_residency") or "")
    model_residencies = _strings(model.get("data_residency"))
    if required_residency and required_residency not in model_residencies:
        return None, "data_residency_mismatch"
    input_cost = _number(model.get("input_cost"))
    output_cost = _number(model.get("output_cost"))
    maximum_cost = _number(requirements.get("maximum_cost"))
    if maximum_cost is not None:
        if input_cost is None or output_cost is None:
            return None, "cost_unknown"
        if input_cost + output_cost > maximum_cost:
            return None, "cost_policy_mismatch"
    health_id = str(
        model.get("health_provider_instance_id") or execution_id
    )
    evidence = health.get(health_id)
    evidence = evidence if isinstance(evidence, Mapping) else {}
    health_status = str(evidence.get("status") or "unknown")
    observed_at = _number(evidence.get("observed_at"))
    max_age = _number(requirements.get("health_max_age")) or 60.0
    if observed_at is not None and decision_time - observed_at > max_age:
        health_status = "unknown"
    if health_status in {"unavailable", "denied", "invalid"}:
        return None, f"health_{health_status}"
    return {
        "model_id": model_id,
        "provider_model_id": str(model.get("provider_model_id") or model_id),
        "provider_id": provider_id,
        "execution_provider_instance_id": execution_id,
        "health_provider_instance_id": health_id,
        "catalog_provider_instance_id": str(
            model.get("catalog_provider_instance_id") or ""
        ),
        "catalog_revision": str(model.get("catalog_revision") or "unknown"),
        "capabilities": sorted(capabilities),
        "modalities": sorted(modalities),
        "context_length": _integer(model.get("context_length")),
        "input_cost": input_cost,
        "output_cost": output_cost,
        "priority": _integer(model.get("priority"), default=100),
        "health": health_status,
        "health_observed_at": observed_at,
        "descriptor": dict(model),
    }, ""


def _sort_key(
    candidate: Mapping[str, Any],
    requirements: Mapping[str, Any],
) -> tuple[Any, ...]:
    preferred_execution = str(
        requirements.get("preferred_provider_instance_id") or ""
    )
    preferred_provider = str(requirements.get("preferred_provider_id") or "")
    preferred_model = str(requirements.get("preferred_model_id") or "")
    health_order = {"healthy": 0, "available": 0, "degraded": 1, "unknown": 2}
    return (
        0 if preferred_execution and preferred_execution == candidate.get(
            "execution_provider_instance_id"
        ) else 1,
        0 if preferred_provider and preferred_provider == candidate.get(
            "provider_id"
        ) else 1,
        0 if preferred_model and preferred_model == candidate.get("model_id") else 1,
        health_order.get(str(candidate.get("health") or "unknown"), 3),
        _integer(candidate.get("priority"), default=100),
        candidate.get("input_cost")
        if candidate.get("input_cost") is not None else float("inf"),
        candidate.get("output_cost")
        if candidate.get("output_cost") is not None else float("inf"),
        str(candidate.get("model_id") or ""),
        str(candidate.get("provider_id") or ""),
        str(candidate.get("catalog_revision") or ""),
    )


def _execution_provider(
    provider_id: str,
    hinted_provider_id: str,
    providers: list[Mapping[str, Any]],
) -> tuple[str, str]:
    exact = []
    wildcard = []
    by_id = {}
    for provider in providers:
        instance_id = str(provider.get("provider_instance_id") or "")
        if not instance_id:
            continue
        by_id[instance_id] = provider
        keys = _strings(provider.get("routing_keys"))
        if provider_id and provider_id in keys:
            exact.append(instance_id)
        if "*" in keys:
            wildcard.append(instance_id)
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return "", "execution_provider_ambiguous"
    if hinted_provider_id in by_id:
        return hinted_provider_id, ""
    if len(wildcard) == 1:
        return wildcard[0], ""
    if len(wildcard) > 1:
        return "", "execution_provider_ambiguous"
    return "", "execution_provider_unresolved"


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item) for item in value if str(item).strip()}


def _integer(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_PACK_ID = "rumi_ai_routing_pack"
_FUNCTION_ID = "rumi_ai_routing_pack.ai-routing.default"


class AIRoutingHostFactoryV4:
    """Bind the manifest-selected pure router to exact Host dispatch edges."""

    function_id = _FUNCTION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Capture all and only operations resolved to the routing function."""

        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            for binding in context.provider_bindings
        ):
            raise PermissionError("AI routing bindings are incomplete")

        def invoke(
            _operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(),
                consumer_pack_id=_PACK_ID,
            )
            return create_route_operation(client)("route", payload)

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
    """Return contributions guarded by exact resolved principal/domain bindings."""

    contributions: list[HostProviderContributionV4] = []
    for binding in context.provider_bindings:
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("AI routing domain binding is unavailable")
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


HOST_PROVIDER_FACTORY = AIRoutingHostFactoryV4()
