"""Deterministic AI gateway over manifest-selected global providers."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
    GlobalContractUnavailable,
)
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

CATALOG_CONTRACT = "tobkiri.resource.ai.model.catalog.v1"
CATALOG_GENERATE_OPERATION = (
    "rumi_model_catalog_pack.bundled-model-catalog.generate"
)
CATALOG_STREAM_OPERATION = "rumi_model_catalog_pack.bundled-model-catalog.stream"
CATALOG_OPERATION = CATALOG_GENERATE_OPERATION
GENERATE_PROVIDER_CONTRACT = "tobkiri.service.ai.provider.generate.v1"
GENERATE_PROVIDER_OPERATION = "rumi_provider_adapters_pack.provider-generate"
STREAM_PROVIDER_CONTRACT = "tobkiri.service.ai.provider.stream.v1"
STREAM_PROVIDER_OPERATION = "rumi_provider_adapters_pack.provider-stream"
HEALTH_CONTRACT = "tobkiri.resource.ai.provider.health.v1"
HEALTH_GENERATE_OPERATION = (
    "rumi_provider_registry_pack.provider-registry-health.generate"
)
HEALTH_STREAM_OPERATION = (
    "rumi_provider_registry_pack.provider-registry-health.stream"
)
HEALTH_OPERATION = HEALTH_GENERATE_OPERATION
USAGE_CONTRACT = "tobkiri.service.ai.usage.cost.v1"
USAGE_GENERATE_OPERATION = "rumi_ai_usage_pack.ai-usage-cost.generate"
USAGE_STREAM_OPERATION = "rumi_ai_usage_pack.ai-usage-cost.stream"
USAGE_OPERATION = USAGE_GENERATE_OPERATION
ROUTING_CONTRACT = "tobkiri.service.ai.route.v1"
ROUTING_GENERATE_OPERATION = "rumi_ai_routing_pack.ai-route.generate"
ROUTING_STREAM_OPERATION = "rumi_ai_routing_pack.ai-route.stream"
ROUTING_OPERATION = ROUTING_GENERATE_OPERATION
STREAM_NORMALIZE_CONTRACT = "tobkiri.service.ai.stream.normalize.v1"
STREAM_NORMALIZE_OPERATION = "rumi_ai_stream_pack.ai-stream-normalize"
TOOL_BRIDGE_CONTRACT = "tobkiri.service.ai.tool_intent.normalize.v1"
TOOL_BRIDGE_GENERATE_OPERATION = (
    "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.generate"
)
TOOL_BRIDGE_STREAM_OPERATION = (
    "rumi_ai_tool_bridge_pack.ai-tool-intent-normalize.stream"
)
TOOL_BRIDGE_OPERATION = TOOL_BRIDGE_GENERATE_OPERATION
REQUEST_PREPARE_CONTRACT = "tobkiri.service.ai.request.prepare.v1"
REQUEST_PREPARE_GENERATE_OPERATION = (
    "rumi_ai_pipeline_pack.ai-request-prepare.generate"
)
REQUEST_PREPARE_STREAM_OPERATION = (
    "rumi_ai_pipeline_pack.ai-request-prepare.stream"
)
REQUEST_PREPARE_OPERATION = REQUEST_PREPARE_GENERATE_OPERATION
FAILOVER_CONTRACT = "tobkiri.service.ai.failover.decide.v1"
FAILOVER_GENERATE_OPERATION = "rumi_ai_pipeline_pack.ai-failover-decide.generate"
FAILOVER_STREAM_OPERATION = "rumi_ai_pipeline_pack.ai-failover-decide.stream"
FAILOVER_OPERATION = FAILOVER_GENERATE_OPERATION
MODEL_PROFILE_CONTRACT = "tobkiri.resource.ai.model.profile.v1"
MODEL_PROFILE_GENERATE_OPERATION = (
    "rumi_model_registry_pack.model-profile-resource.generate"
)
MODEL_PROFILE_STREAM_OPERATION = (
    "rumi_model_registry_pack.model-profile-resource.stream"
)
MODEL_PROFILE_OPERATION = MODEL_PROFILE_GENERATE_OPERATION

_DIAGNOSTIC_LIMIT = 256
_DIAGNOSTICS: list[dict[str, Any]] = []
_DIAGNOSTIC_LOCK = threading.Lock()

_GENERATE_FUNCTION_ID = "rumi_ai_gateway_pack.ai-gateway.generate"
_STREAM_FUNCTION_ID = "rumi_ai_gateway_pack.ai-gateway.stream"
_ROUTING_DIAGNOSTICS_FUNCTION_ID = (
    "rumi_ai_gateway_pack.ai-gateway.routing-diagnostics"
)

_GENERATE_ALLOWED_CONTRACTS = frozenset(
    {
        CATALOG_CONTRACT,
        GENERATE_PROVIDER_CONTRACT,
        HEALTH_CONTRACT,
        USAGE_CONTRACT,
        ROUTING_CONTRACT,
        TOOL_BRIDGE_CONTRACT,
        REQUEST_PREPARE_CONTRACT,
        FAILOVER_CONTRACT,
        MODEL_PROFILE_CONTRACT,
    }
)
_STREAM_ALLOWED_CONTRACTS = frozenset(
    {
        CATALOG_CONTRACT,
        STREAM_PROVIDER_CONTRACT,
        HEALTH_CONTRACT,
        USAGE_CONTRACT,
        ROUTING_CONTRACT,
        STREAM_NORMALIZE_CONTRACT,
        TOOL_BRIDGE_CONTRACT,
        REQUEST_PREPARE_CONTRACT,
        FAILOVER_CONTRACT,
        MODEL_PROFILE_CONTRACT,
    }
)


@dataclass(frozen=True)
class RouteRequirement:
    """Provider-neutral request requirements bound to one route decision."""

    modalities: frozenset[str]
    capabilities: frozenset[str]
    tool_calling: bool
    thinking: bool
    minimum_context: int
    request_surface: str
    data_residency: str | None
    maximum_cost: float | None
    preferred_model_id: str | None
    preferred_provider_id: str | None
    preferred_provider_instance_id: str | None
    health_max_age: float


@dataclass(frozen=True)
class Candidate:
    """One catalog model joined to an executable selected provider handle."""

    model_id: str
    provider_instance_id: str
    catalog_provider_instance_id: str
    catalog_revision: str
    capabilities: frozenset[str]
    modalities: frozenset[str]
    context_length: int
    input_cost: float | None
    output_cost: float | None
    priority: int
    available: bool
    health: str
    health_observed_at: float | None
    raw: Mapping[str, Any]


def create_generate_operation(client: GlobalContractClient):
    """Create the global non-streaming gateway operation."""

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {
            "generate",
            "invoke",
            "resolve",
            "rumi_ai_gateway_pack.ai-gateway.generate",
        }:
            raise ValueError(f"unknown generate operation: {name}")
        return _invoke(
            client,
            {**dict(payload), "resolve_only": name == "resolve"},
            streaming=False,
        )

    return operation


def create_stream_operation(client: GlobalContractClient):
    """Create the global streaming gateway operation."""

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {
            "stream",
            "invoke",
            "rumi_ai_gateway_pack.ai-gateway.stream",
        }:
            raise ValueError(f"unknown stream operation: {name}")
        return _invoke(client, payload, streaming=True)

    return operation


def create_routing_diagnostics_operation(client: GlobalContractClient):
    """Create a redacted read-only routing diagnostic resource."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {"list", "get"}:
            raise ValueError(f"unknown diagnostic operation: {name}")
        request_id = str(payload.get("request_id") or "").strip()
        with _DIAGNOSTIC_LOCK:
            values = [dict(item) for item in _DIAGNOSTICS]
        if request_id:
            values = [item for item in values if item["request_id"] == request_id]
        return {"diagnostics": values, "count": len(values)}

    return operation


class AIGatewayHostFactoryV4:
    """Capture one exact AI Gateway Function behind the Host Broker."""

    def __init__(
        self,
        function_id: str,
        *,
        contract_id: str,
        operation_id: str,
        operation_name: str,
        allowed_contract_ids: frozenset[str],
        operation_factory: Any,
    ) -> None:
        self.function_id = function_id
        self._contract_id = contract_id
        self._operation_id = operation_id
        self._operation_name = operation_name
        self._allowed_contract_ids = allowed_contract_ids
        self._operation_factory = operation_factory

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind only one Plan-pinned Gateway Function and its dependencies."""

        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != self._contract_id
            or binding.operation.operation_id != self._operation_id
            for binding in context.provider_bindings
        ):
            raise PermissionError("AI Gateway provider bindings are incomplete")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if operation_id != self._operation_id:
                raise PermissionError("AI Gateway operation identity is invalid")
            client = invocation.contract_client(
                allowed_contract_ids=self._allowed_contract_ids,
                consumer_pack_id="rumi_ai_gateway_pack",
            )
            return self._operation_factory(client)(self._operation_name, payload)

        contributions: list[HostProviderContributionV4] = []
        for binding in context.provider_bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                raise PermissionError("AI Gateway domain binding is unavailable")
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
        return CapturedHostProviderV4(tuple(contributions), lambda: None)


HOST_PROVIDER_FACTORY = {
    _GENERATE_FUNCTION_ID: AIGatewayHostFactoryV4(
        _GENERATE_FUNCTION_ID,
        contract_id="tobkiri.service.ai.generate.v1",
        operation_id=_GENERATE_FUNCTION_ID,
        operation_name=_GENERATE_FUNCTION_ID,
        allowed_contract_ids=_GENERATE_ALLOWED_CONTRACTS,
        operation_factory=create_generate_operation,
    ),
    _STREAM_FUNCTION_ID: AIGatewayHostFactoryV4(
        _STREAM_FUNCTION_ID,
        contract_id="tobkiri.service.ai.stream.v1",
        operation_id=_STREAM_FUNCTION_ID,
        operation_name=_STREAM_FUNCTION_ID,
        allowed_contract_ids=_STREAM_ALLOWED_CONTRACTS,
        operation_factory=create_stream_operation,
    ),
    _ROUTING_DIAGNOSTICS_FUNCTION_ID: AIGatewayHostFactoryV4(
        _ROUTING_DIAGNOSTICS_FUNCTION_ID,
        contract_id="tobkiri.resource.ai.routing.diagnostics.v1",
        operation_id="rumi_ai_gateway_pack.ai-gateway-routing-diagnostics",
        operation_name="get",
        allowed_contract_ids=frozenset(),
        operation_factory=create_routing_diagnostics_operation,
    ),
}


def _invoke(
    client: GlobalContractClient,
    payload: Mapping[str, Any],
    *,
    streaming: bool,
) -> dict[str, Any]:
    resolve_only = bool(payload.get("resolve_only"))
    prepared = client.invoke(
        REQUEST_PREPARE_CONTRACT,
        (
            REQUEST_PREPARE_STREAM_OPERATION
            if streaming
            else REQUEST_PREPARE_GENERATE_OPERATION
        ),
        {
            **dict(payload),
            "request_id": str(payload.get("request_id") or uuid.uuid4()),
            "decision_time": time.time(),
        },
    )
    if not isinstance(prepared, Mapping):
        raise GlobalContractInvocationError(
            "invalid_response", "AI pipeline returned an invalid request"
        )
    request = dict(prepared)
    _resolve_model_reference(client, request, streaming=streaming)
    request_id = str(request["request_id"])
    deadline = float(request["deadline"])
    requirement = _requirement(request)
    provider_contract = (
        STREAM_PROVIDER_CONTRACT if streaming else GENERATE_PROVIDER_CONTRACT
    )
    provider_metadata = {
        str(item.get("provider_instance_id") or ""): item
        for item in client.providers(provider_contract)
    }
    if not provider_metadata:
        raise GlobalContractInvocationError(
            "missing_provider",
            f"no selected provider for {provider_contract}",
        )
    health = _health(client, streaming=streaming)
    candidates, excluded = _catalog_candidates(
        client,
        provider_metadata,
        requirement,
        health,
        streaming=streaming,
        explicit_pricing=request.get("_resolved_model_pricing"),
    )
    exact_binding = bool(
        requirement.preferred_model_id
        and requirement.preferred_provider_instance_id
        and not request.get("allow_failover")
    )
    if exact_binding and requirement.preferred_model_id:
        candidates = [
            item
            for item in candidates
            if item.model_id == requirement.preferred_model_id
        ]
    if exact_binding and requirement.preferred_provider_instance_id:
        candidates = [
            item
            for item in candidates
            if item.provider_instance_id
            == requirement.preferred_provider_instance_id
        ]
    if not candidates:
        _record_diagnostic(
            request_id,
            requirement,
            (),
            excluded,
            selected=None,
            policy_revision=str(request.get("policy_revision") or ""),
        )
        if request.get("model_profile_id") or request.get("model_reference"):
            raise GlobalContractInvocationError(
                "unresolved_profile",
                "saved model reference has no selected executable provider",
            )
        raise GlobalContractInvocationError(
            "capability_mismatch",
            "no selected model satisfies the request requirements",
        )
    ordered = candidates
    selected = ordered[0]
    _record_diagnostic(
        request_id,
        requirement,
        candidates,
        excluded,
        selected=selected,
        policy_revision=str(request.get("policy_revision") or ""),
    )
    if resolve_only:
        return {
            "status": "ok",
            "model_id": selected.model_id,
            "provider_instance_id": selected.provider_instance_id,
            "catalog_provider_instance_id": (
                selected.catalog_provider_instance_id
            ),
            "catalog_revision": selected.catalog_revision,
            "pricing_revision": selected.catalog_revision,
            "pricing": {
                "input": selected.input_cost,
                "output": selected.output_cost,
                "currency": str(selected.raw.get("currency") or "USD"),
            },
        }
    invocation = {
        "request_id": request_id,
        "model_id": str(
            selected.raw.get("provider_model_id") or selected.model_id
        ),
        "provider_id": str(selected.raw.get("provider_id") or ""),
        "messages": request.get("messages") or [],
        "input": request.get("input"),
        "parameters": dict(request.get("parameters") or {}),
        "tools": list(request.get("tools") or []),
        "required_capabilities": sorted(requirement.capabilities),
        "required_modalities": sorted(requirement.modalities),
        "request_surface": requirement.request_surface,
        "profile_id": request.get("profile_id"),
        "deadline": deadline,
        "credential_handle": request.get("credential_handle"),
        "idempotency_key": request.get("idempotency_key"),
    }
    attempts: list[dict[str, Any]] = []
    for attempt_number, attempt_candidate in enumerate(ordered, 1):
        invocation["attempt"] = attempt_number
        invocation["model_id"] = str(
            attempt_candidate.raw.get("provider_model_id")
            or attempt_candidate.model_id
        )
        invocation["provider_id"] = str(
            attempt_candidate.raw.get("provider_id") or ""
        )
        try:
            value = client.invoke(
                provider_contract,
                STREAM_PROVIDER_OPERATION if streaming else GENERATE_PROVIDER_OPERATION,
                invocation,
                provider_instance_id=attempt_candidate.provider_instance_id,
            )
            if streaming:
                normalized = client.invoke(
                    STREAM_NORMALIZE_CONTRACT,
                    STREAM_NORMALIZE_OPERATION,
                    {
                        "request_id": request_id,
                        "provider_attempt": attempt_number,
                        "value": value,
                    },
                )
                events = (
                    normalized.get("events")
                    if isinstance(normalized, Mapping)
                    else None
                )
                if not isinstance(events, list):
                    raise GlobalContractInvocationError(
                        "invalid_response",
                        "stream normalizer returned an invalid result",
                    )
                _attach_stream_usage_cost(client, events, attempt_candidate)
                _attach_stream_tool_intents(client, events, request_id)
                return {
                    "request_id": request_id,
                    "model_id": attempt_candidate.model_id,
                    "provider_instance_id": attempt_candidate.provider_instance_id,
                    "events": events,
                    "attempts": attempts,
                }
            result = _normalize_result(value, request_id, attempt_candidate)
            result["tool_intents"] = _tool_intents(
                client,
                result["tool_intents"],
                request_id,
                streaming=streaming,
            )
            result["usage_cost"] = _usage_cost(
                client,
                result["usage"],
                attempt_candidate,
                result["usage_provenance"],
                streaming=streaming,
            )
            result["attempts"] = attempts
            return result
        except GlobalContractUnavailable as exc:
            failure = GlobalContractInvocationError(
                "provider_unavailable",
                str(exc),
            )
        except GlobalContractInvocationError as exc:
            failure = exc
        attempts.append(
            {
                "attempt": attempt_number,
                "model_id": attempt_candidate.model_id,
                "provider_instance_id": attempt_candidate.provider_instance_id,
                "error_code": failure.code,
            }
        )
        failover = client.invoke(
            FAILOVER_CONTRACT,
            FAILOVER_STREAM_OPERATION if streaming else FAILOVER_GENERATE_OPERATION,
            {
                "allow_failover": request.get("allow_failover"),
                "idempotency_key": request.get("idempotency_key"),
                "tools": invocation["tools"],
                "error_code": failure.code,
                "attempt": attempt_number,
                "candidate_count": len(ordered),
                "deadline": deadline,
                "decision_time": time.time(),
            },
        )
        if not isinstance(failover, Mapping) or not failover.get("allowed"):
            raise failure
    raise GlobalContractInvocationError(
        "provider_unavailable",
        "all selected providers failed",
    )


def _requirement(request: Mapping[str, Any]) -> RouteRequirement:
    requirement = request.get("requirements")
    requirement = requirement if isinstance(requirement, Mapping) else {}
    modalities = _strings(requirement.get("modalities")) or frozenset({"text"})
    maximum_cost = _optional_float(requirement.get("maximum_cost"))
    return RouteRequirement(
        modalities=modalities,
        capabilities=_strings(requirement.get("capabilities")),
        tool_calling=bool(requirement.get("tool_calling", False)),
        thinking=bool(requirement.get("thinking", False)),
        minimum_context=max(0, int(requirement.get("minimum_context") or 0)),
        request_surface=str(requirement.get("request_surface") or "chat"),
        data_residency=(
            str(requirement.get("data_residency"))
            if requirement.get("data_residency")
            else None
        ),
        maximum_cost=maximum_cost,
        preferred_model_id=(
            str(requirement.get("preferred_model_id"))
            if requirement.get("preferred_model_id")
            else None
        ),
        preferred_provider_id=(
            str(requirement.get("preferred_provider_id"))
            if requirement.get("preferred_provider_id")
            else None
        ),
        preferred_provider_instance_id=(
            str(requirement.get("preferred_provider_instance_id"))
            if requirement.get("preferred_provider_instance_id")
            else None
        ),
        health_max_age=max(
            0.0,
            _optional_float(requirement.get("health_max_age")) or 60.0,
        ),
    )


def _resolve_model_reference(
    client: GlobalContractClient,
    request: dict[str, Any],
    *,
    streaming: bool,
) -> None:
    explicit = str(request.get("model_profile_id") or "").strip()
    legacy = str(request.get("model_reference") or "").strip()
    identifier = explicit or legacy
    if not identifier:
        return
    try:
        resolved = client.invoke(
            MODEL_PROFILE_CONTRACT,
            (
                MODEL_PROFILE_STREAM_OPERATION
                if streaming
                else MODEL_PROFILE_GENERATE_OPERATION
            ),
            {"identifier": identifier},
        )
    except GlobalContractInvocationError:
        if explicit:
            raise
        requirements = dict(request.get("requirements") or {})
        requirements.setdefault("preferred_model_id", legacy)
        request["requirements"] = requirements
        return
    profile = resolved.get("profile") if isinstance(resolved, Mapping) else None
    if not isinstance(profile, Mapping):
        raise GlobalContractInvocationError(
            "unresolved_profile",
            "model profile owner returned an invalid record",
        )
    request["model_profile_id"] = str(
        resolved.get("resolved_profile_id") or identifier
    )
    request["requirements"] = _merge_requirements(
        profile.get("requirements"),
        request.get("requirements"),
        model_id=str(profile.get("model_id") or ""),
    )
    parameters = dict(profile.get("parameters") or {})
    parameters.update(dict(request.get("parameters") or {}))
    request["parameters"] = parameters
    if request.get("credential_handle") is None:
        request["credential_handle"] = profile.get("credential_handle")
    metadata = profile.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    pricing = metadata.get("pricing")
    if isinstance(pricing, Mapping):
        request["_resolved_model_pricing"] = {
            "input": _optional_float(pricing.get("input")),
            "output": _optional_float(pricing.get("output")),
            "currency": str(pricing.get("currency") or "USD"),
            "revision": str(
                metadata.get("pricing_revision")
                or resolved.get("store_revision")
                or "model-profile"
            ),
        }


def _merge_requirements(
    profile_value: Any,
    request_value: Any,
    *,
    model_id: str,
) -> dict[str, Any]:
    profile = dict(profile_value) if isinstance(profile_value, Mapping) else {}
    request = dict(request_value) if isinstance(request_value, Mapping) else {}
    result = {**profile, **request}
    for key in ("modalities", "capabilities"):
        values = _strings(profile.get(key)) | _strings(request.get(key))
        if values:
            result[key] = sorted(values)
    for key in ("tool_calling", "thinking"):
        result[key] = bool(profile.get(key)) or bool(request.get(key))
    result["minimum_context"] = max(
        int(profile.get("minimum_context") or 0),
        int(request.get("minimum_context") or 0),
    )
    costs = [
        value
        for value in (
            _optional_float(profile.get("maximum_cost")),
            _optional_float(request.get("maximum_cost")),
        )
        if value is not None
    ]
    if costs:
        result["maximum_cost"] = min(costs)
    result.setdefault("preferred_model_id", model_id)
    return result


def _health(
    client: GlobalContractClient,
    *,
    streaming: bool,
) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for provider in client.providers(HEALTH_CONTRACT):
        provider_id = str(provider.get("provider_instance_id") or "")
        try:
            result = client.invoke(
                HEALTH_CONTRACT,
                HEALTH_STREAM_OPERATION if streaming else HEALTH_GENERATE_OPERATION,
                {},
                provider_instance_id=provider_id,
            )
        except Exception:
            continue
        items = result.get("providers") if isinstance(result, Mapping) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, Mapping) and item.get("provider_instance_id"):
                values[str(item["provider_instance_id"])] = dict(item)
    return values


def _catalog_candidates(
    client: GlobalContractClient,
    providers: Mapping[str, Mapping[str, Any]],
    requirement: RouteRequirement,
    health: Mapping[str, Mapping[str, Any]],
    *,
    streaming: bool,
    explicit_pricing: Any = None,
) -> tuple[list[Candidate], list[dict[str, str]]]:
    catalog_models: list[dict[str, Any]] = []
    for catalog_provider in client.providers(CATALOG_CONTRACT):
        catalog_provider_id = str(
            catalog_provider.get("provider_instance_id") or ""
        )
        result = client.invoke(
            CATALOG_CONTRACT,
            CATALOG_STREAM_OPERATION if streaming else CATALOG_GENERATE_OPERATION,
            {},
            provider_instance_id=catalog_provider_id,
        )
        raw_models = result.get("models") if isinstance(result, Mapping) else None
        for raw in raw_models if isinstance(raw_models, list) else []:
            if not isinstance(raw, Mapping):
                continue
            descriptor = dict(raw)
            descriptor["catalog_provider_instance_id"] = catalog_provider_id
            catalog_models.append(descriptor)
    _append_explicit_live_model(
        catalog_models,
        requirement,
        explicit_pricing=explicit_pricing,
    )
    routed = client.invoke(
        ROUTING_CONTRACT,
        ROUTING_STREAM_OPERATION if streaming else ROUTING_GENERATE_OPERATION,
        {
            "models": catalog_models,
            "execution_providers": list(providers.values()),
            "health": dict(health),
            "requirements": _requirement_payload(requirement),
            "decision_time": time.time(),
        },
    )
    values = routed.get("candidates") if isinstance(routed, Mapping) else None
    excluded = routed.get("excluded") if isinstance(routed, Mapping) else None
    values = values if isinstance(values, list) else []
    excluded = excluded if isinstance(excluded, list) else []
    return (
        [
            _candidate_from_route(item)
            for item in values
            if isinstance(item, Mapping)
        ],
        [
            dict(item)
            for item in excluded
            if isinstance(item, Mapping)
        ],
    )


def _append_explicit_live_model(
    catalog_models: list[dict[str, Any]],
    requirement: RouteRequirement,
    *,
    explicit_pricing: Any = None,
) -> None:
    """Bridge a provider-verified live model into deterministic routing.

    The model picker can contain account-scoped models returned by a provider's
    live ``/models`` endpoint before the bundled catalog is refreshed. An
    explicit saved reference must remain routable through the selected generic
    provider adapter instead of failing merely because that static snapshot is
    older than the provider inventory.
    """
    model_id = str(requirement.preferred_model_id or "").strip()
    provider_id, separator, provider_model_id = model_id.partition("/")
    if (
        not separator
        or not provider_id
        or not provider_model_id
        or any(character.isspace() for character in provider_id)
        or any(not character.isprintable() for character in model_id)
    ):
        return
    if any(str(item.get("model_id") or "") == model_id for item in catalog_models):
        return

    capabilities = set(requirement.capabilities)
    if requirement.tool_calling:
        capabilities.add("tool_calling")
    if requirement.thinking:
        capabilities.add("thinking")
    pricing = (
        dict(explicit_pricing)
        if isinstance(explicit_pricing, Mapping)
        else {}
    )
    catalog_models.append(
        {
            "model_id": model_id,
            "provider_model_id": provider_model_id,
            "provider_id": provider_id,
            "execution_provider_instance_id": "provider.compatibility.generate",
            "health_provider_instance_id": f"provider.{provider_id}",
            "catalog_revision": str(
                pricing.get("revision") or "explicit-live-model:v1"
            ),
            "input_cost": _optional_float(pricing.get("input")),
            "output_cost": _optional_float(pricing.get("output")),
            "currency": str(pricing.get("currency") or "USD"),
            "capabilities": sorted(capabilities),
            "modalities": sorted(requirement.modalities or {"text"}),
            "context_length": requirement.minimum_context,
            "priority": 0,
            "available": True,
            "request_surfaces": [requirement.request_surface],
            "data_residency": requirement.data_residency or "unknown",
        }
    )


def _requirement_payload(requirement: RouteRequirement) -> dict[str, Any]:
    return {
        "modalities": sorted(requirement.modalities),
        "capabilities": sorted(requirement.capabilities),
        "tool_calling": requirement.tool_calling,
        "thinking": requirement.thinking,
        "minimum_context": requirement.minimum_context,
        "request_surface": requirement.request_surface,
        "data_residency": requirement.data_residency,
        "maximum_cost": requirement.maximum_cost,
        "preferred_model_id": requirement.preferred_model_id,
        "preferred_provider_id": requirement.preferred_provider_id,
        "preferred_provider_instance_id": (
            requirement.preferred_provider_instance_id
        ),
        "health_max_age": requirement.health_max_age,
    }


def _candidate_from_route(value: Mapping[str, Any]) -> Candidate:
    descriptor = value.get("descriptor")
    descriptor = descriptor if isinstance(descriptor, Mapping) else {}
    return Candidate(
        model_id=str(value.get("model_id") or ""),
        provider_instance_id=str(
            value.get("execution_provider_instance_id") or ""
        ),
        catalog_provider_instance_id=str(
            value.get("catalog_provider_instance_id") or ""
        ),
        catalog_revision=str(value.get("catalog_revision") or "unknown"),
        capabilities=frozenset(_strings(value.get("capabilities"))),
        modalities=frozenset(_strings(value.get("modalities"))),
        context_length=int(value.get("context_length") or 0),
        input_cost=_optional_float(value.get("input_cost")),
        output_cost=_optional_float(value.get("output_cost")),
        priority=int(value.get("priority") or 0),
        available=True,
        health=str(value.get("health") or "unknown"),
        health_observed_at=_optional_float(value.get("health_observed_at")),
        raw=dict(descriptor),
    )


def _normalize_result(
    value: Any,
    request_id: str,
    selected: Candidate,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GlobalContractInvocationError(
            "invalid_response",
            "provider result must be an object",
        )
    if value.get("status") not in {None, "ok"}:
        raise GlobalContractInvocationError(
            str(value.get("error_code") or "provider_unavailable"),
            str(value.get("message") or "provider failed"),
        )
    return {
        "status": "ok",
        "request_id": request_id,
        "model_id": selected.model_id,
        "provider_instance_id": selected.provider_instance_id,
        "catalog_provider_instance_id": (
            selected.catalog_provider_instance_id
        ),
        "catalog_revision": selected.catalog_revision,
        "pricing_revision": selected.catalog_revision,
        "output": value.get("output"),
        "tool_intents": list(value.get("tool_intents") or []),
        "finish_reason": str(value.get("finish_reason") or "stop"),
        "usage": dict(value.get("usage") or {}),
        "usage_provenance": str(
            value.get("usage_provenance") or "provider_reported"
        ),
    }


def _attach_stream_usage_cost(
    client: GlobalContractClient,
    events: list[dict[str, Any]],
    candidate: Candidate,
) -> None:
    for event in events:
        usage = event.get("usage")
        if event.get("type") == "usage" and isinstance(usage, Mapping):
            event["usage_cost"] = _usage_cost(
                client,
                usage,
                candidate,
                "provider_reported",
                streaming=True,
            )


def _attach_stream_tool_intents(
    client: GlobalContractClient,
    events: list[dict[str, Any]],
    request_id: str,
) -> None:
    for event in events:
        intent = event.get("tool_intent")
        if event.get("type") == "tool_intent_delta" and isinstance(
            intent, Mapping
        ):
            normalized = _tool_intents(
                client,
                [intent],
                request_id,
                streaming=True,
            )
            event["tool_intent"] = normalized[0]


def _tool_intents(
    client: GlobalContractClient,
    intents: list[Any],
    request_id: str,
    *,
    streaming: bool,
) -> list[dict[str, Any]]:
    result = client.invoke(
        TOOL_BRIDGE_CONTRACT,
        TOOL_BRIDGE_STREAM_OPERATION if streaming else TOOL_BRIDGE_GENERATE_OPERATION,
        {"request_id": request_id, "intents": intents},
    )
    values = result.get("intents") if isinstance(result, Mapping) else None
    if not isinstance(values, list):
        raise GlobalContractInvocationError(
            "invalid_response", "AI tool bridge returned an invalid result"
        )
    return [dict(item) for item in values if isinstance(item, Mapping)]


def _usage_cost(
    client: GlobalContractClient,
    usage: Mapping[str, Any],
    candidate: Candidate,
    provenance: str,
    *,
    streaming: bool,
) -> dict[str, Any]:
    return client.invoke(
        USAGE_CONTRACT,
        USAGE_STREAM_OPERATION if streaming else USAGE_GENERATE_OPERATION,
        {
            "usage": dict(usage),
            "usage_provenance": provenance,
            "pricing": {
                "input": candidate.input_cost,
                "output": candidate.output_cost,
                "currency": str(candidate.raw.get("currency") or "USD"),
            },
            "pricing_revision": candidate.catalog_revision,
        },
    )


def _record_diagnostic(
    request_id: str,
    requirement: RouteRequirement,
    candidates: Iterable[Candidate],
    excluded: list[dict[str, str]],
    *,
    selected: Candidate | None,
    policy_revision: str,
) -> None:
    record = {
        "request_id": request_id,
        "created_at": time.time(),
        "requirements": {
            "modalities": sorted(requirement.modalities),
            "capabilities": sorted(requirement.capabilities),
            "tool_calling": requirement.tool_calling,
            "thinking": requirement.thinking,
            "minimum_context": requirement.minimum_context,
            "request_surface": requirement.request_surface,
            "data_residency": requirement.data_residency,
            "maximum_cost": requirement.maximum_cost,
            "health_max_age": requirement.health_max_age,
        },
        "candidates": [
            {
                "model_id": item.model_id,
                "provider_instance_id": item.provider_instance_id,
                "catalog_revision": item.catalog_revision,
                "health": item.health,
                "health_observed_at": item.health_observed_at,
            }
            for item in candidates
        ],
        "excluded": list(excluded),
        "selected": (
            {
                "model_id": selected.model_id,
                "provider_instance_id": selected.provider_instance_id,
                "catalog_revision": selected.catalog_revision,
            }
            if selected is not None
            else None
        ),
        "policy_revision": policy_revision,
    }
    with _DIAGNOSTIC_LOCK:
        _DIAGNOSTICS.append(record)
        del _DIAGNOSTICS[:-_DIAGNOSTIC_LIMIT]


def _strings(value: Any) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(item).strip() for item in value if str(item).strip())


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
