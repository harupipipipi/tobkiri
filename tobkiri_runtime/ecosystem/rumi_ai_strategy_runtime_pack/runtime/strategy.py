"""Plan-pinned dispatch to an explicitly selected AI strategy provider."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from core_runtime.global_contract_dispatch import (
    GlobalContractInvocationError,
    GlobalContractUnavailable,
)
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
)


DISPATCH_CONTRACT_ID = "tobkiri.service.ai.strategy.dispatch.v1"
DISPATCH_OPERATION_ID = (
    "rumi_ai_strategy_runtime_pack.ai-strategy.dispatch"
)
CATALOG_CONTRACT_ID = "tobkiri.resource.ai.strategy.catalog.v1"
CATALOG_OPERATION_ID = "rumi_ai_strategy_runtime_pack.ai-strategy.catalog"
EXECUTE_CONTRACT_ID = "tobkiri.service.ai.strategy.execute.v1"
_FUNCTION_ID = DISPATCH_OPERATION_ID
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "model_profile_id",
        "model_reference",
        "messages",
        "input",
        "parameters",
        "tools",
        "requirements",
        "profile_id",
        "deadline",
        "idempotency_key",
        "maximum_cost_microusd",
        "policy_revision",
        "allow_failover",
        "system_prompt_digest",
    }
)


def create_dispatch_operation(client: Any):
    """Create a dispatcher restricted to captured strategy providers."""

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != DISPATCH_OPERATION_ID:
            raise ValueError("AI strategy dispatch operation is invalid")
        if set(payload) != {"strategy_reference", "request"}:
            raise ValueError("AI strategy dispatch input fields are invalid")
        strategy_reference = str(payload.get("strategy_reference") or "").strip()
        if not _IDENTIFIER.fullmatch(strategy_reference):
            raise ValueError("AI strategy reference is invalid")
        request = payload.get("request")
        if not isinstance(request, Mapping):
            raise ValueError("AI strategy request must be an object")
        request_value = dict(request)
        if set(request_value) - _REQUEST_FIELDS:
            raise ValueError("AI strategy request fields are invalid")
        if not request_value.get("request_id"):
            raise ValueError("AI strategy request_id is required")
        if not isinstance(request_value.get("messages"), list):
            raise ValueError("AI strategy messages are required")
        if not request_value.get("idempotency_key"):
            raise ValueError("AI strategy idempotency_key is required")
        # The dispatcher seals a conservative Host-owned default before the
        # strategy Pack's immutable outer request is captured. Saved turns
        # always provide their stricter per-turn ceiling explicitly.
        request_value.setdefault("maximum_cost_microusd", 50_000)
        deadline = request_value.get("deadline")
        if (
            type(deadline) is not int
            or deadline <= 0
        ):
            raise ValueError("AI strategy deadline must be Unix milliseconds")
        maximum_cost = request_value.get("maximum_cost_microusd")
        if (
            type(maximum_cost) is not int
            or not 1 <= maximum_cost <= (2**53) - 1
        ):
            raise ValueError("AI strategy maximum cost must be integer micro-USD")
        # This trusted dispatcher owns the absolute ceiling. A caller may ask
        # for a lower budget, but cannot enlarge the PackVM Host ledger.
        request_value["maximum_cost_microusd"] = min(maximum_cost, 1_000_000)

        matches = [
            item
            for item in client.providers(EXECUTE_CONTRACT_ID)
            if item.get("provider_id") == strategy_reference
            and isinstance(item.get("provider_instance_id"), str)
            and item.get("provider_instance_id")
            and isinstance(item.get("operation_id"), str)
            and item.get("operation_id")
        ]
        if len(matches) != 1:
            raise GlobalContractUnavailable(
                "selected AI strategy provider is unavailable: "
                f"{strategy_reference}"
            )
        selected = matches[0]
        if selected.get("backend_unavailable_reason"):
            raise GlobalContractUnavailable(
                "selected AI strategy provider backend is unavailable: "
                f"{strategy_reference}"
            )
        result = client.invoke(
            EXECUTE_CONTRACT_ID,
            str(selected["operation_id"]),
            request_value,
            provider_instance_id=str(selected["provider_instance_id"]),
        )
        if not isinstance(result, Mapping):
            raise GlobalContractInvocationError(
                "strategy_invalid_response",
                "selected AI strategy returned an invalid response",
            )
        return dict(result)

    return operation


def create_catalog_operation(client: Any):
    """Create an authority-free projection of Plan-admitted strategy providers."""

    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name != CATALOG_OPERATION_ID or payload:
            raise ValueError("AI strategy catalog request is invalid")
        strategies = []
        for item in client.providers(EXECUTE_CONTRACT_ID):
            if item.get("backend_unavailable_reason"):
                continue
            identity = {
                key: item.get(key)
                for key in (
                    "provider_instance_id",
                    "provider_id",
                    "pack_id",
                    "operation_id",
                    "artifact_digest",
                    "implementation_digest",
                )
            }
            display_name = item.get("pack_display_name")
            description = item.get("pack_description", "")
            if (
                not all(isinstance(value, str) and value for value in identity.values())
                or not _IDENTIFIER.fullmatch(identity["provider_instance_id"])
                or not _IDENTIFIER.fullmatch(identity["provider_id"])
                or not isinstance(display_name, str)
                or not display_name.strip()
                or not isinstance(description, str)
            ):
                continue
            record = {
                **identity,
                "strategy_reference": identity["provider_id"],
                "label": display_name.strip(),
                "description": description.strip(),
            }
            strategies.append(record)
        strategies.sort(
            key=lambda item: (
                item["pack_id"],
                item["provider_instance_id"],
                item["operation_id"],
            )
        )
        return {"strategies": strategies, "count": len(strategies)}

    return operation


class AIStrategyDispatcherHostFactoryV4:
    """Capture the generic dispatcher behind one immutable Host plan."""

    function_id = _FUNCTION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind only the dispatcher Function and declared strategy providers."""

        if len(context.provider_bindings) != 1:
            raise PermissionError("AI strategy dispatcher binding is incomplete")
        binding = context.provider_bindings[0]
        if (
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != DISPATCH_CONTRACT_ID
            or binding.operation.operation_id != DISPATCH_OPERATION_ID
        ):
            raise PermissionError("AI strategy dispatcher binding is invalid")
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("AI strategy dispatcher domain is unavailable")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: Any,
        ) -> Mapping[str, Any]:
            if operation_id != DISPATCH_OPERATION_ID:
                raise PermissionError("AI strategy operation identity is invalid")
            invocation.assert_current()
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({EXECUTE_CONTRACT_ID}),
                consumer_pack_id="rumi_ai_strategy_runtime_pack",
                include_credentials=False,
            )
            result = create_dispatch_operation(client)(operation_id, payload)
            invocation.assert_current()
            return result

        contribution = HostProviderContributionV4(
            contract_id=binding.operation.contract_id,
            contract_version=binding.operation.contract_version,
            operation_id=binding.operation.operation_id,
            principal_id=binding.principal_ref.value,
            artifact_digest=binding.artifact.digest,
            implementation_digest=binding.function.implementation_digest,
            domain_id=domain_id,
            invoke=invoke,
        )
        return CapturedHostProviderV4((contribution,), lambda: None)


class AIStrategyCatalogHostFactoryV4(AIStrategyDispatcherHostFactoryV4):
    """Capture the read-only strategy catalog from the same immutable Plan."""

    function_id = CATALOG_OPERATION_ID

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind only the catalog Function and admitted provider metadata."""

        if len(context.provider_bindings) != 1:
            raise PermissionError("AI strategy catalog binding is incomplete")
        binding = context.provider_bindings[0]
        if (
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != CATALOG_CONTRACT_ID
            or binding.operation.operation_id != CATALOG_OPERATION_ID
        ):
            raise PermissionError("AI strategy catalog binding is invalid")
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("AI strategy catalog domain is unavailable")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: Any,
        ) -> Mapping[str, Any]:
            if operation_id != CATALOG_OPERATION_ID:
                raise PermissionError("AI strategy catalog identity is invalid")
            invocation.assert_current()
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({EXECUTE_CONTRACT_ID}),
                consumer_pack_id="rumi_ai_strategy_runtime_pack",
                include_credentials=False,
            )
            result = create_catalog_operation(client)(operation_id, payload)
            invocation.assert_current()
            return result

        contribution = HostProviderContributionV4(
            contract_id=binding.operation.contract_id,
            contract_version=binding.operation.contract_version,
            operation_id=binding.operation.operation_id,
            principal_id=binding.principal_ref.value,
            artifact_digest=binding.artifact.digest,
            implementation_digest=binding.function.implementation_digest,
            domain_id=domain_id,
            invoke=invoke,
        )
        return CapturedHostProviderV4((contribution,), lambda: None)


HOST_PROVIDER_FACTORY = {
    DISPATCH_OPERATION_ID: AIStrategyDispatcherHostFactoryV4(),
    CATALOG_OPERATION_ID: AIStrategyCatalogHostFactoryV4(),
}


__all__ = [
    "DISPATCH_CONTRACT_ID",
    "DISPATCH_OPERATION_ID",
    "CATALOG_CONTRACT_ID",
    "CATALOG_OPERATION_ID",
    "EXECUTE_CONTRACT_ID",
    "HOST_PROVIDER_FACTORY",
    "AIStrategyDispatcherHostFactoryV4",
    "AIStrategyCatalogHostFactoryV4",
    "create_catalog_operation",
    "create_dispatch_operation",
]
