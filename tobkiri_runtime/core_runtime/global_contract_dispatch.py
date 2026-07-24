"""Generic dispatch from legacy host adapters to activated global providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .interface_registry import InterfaceRegistry
from .resolved_profile_scope import persisted_resolved_profile


class GlobalContractUnavailable(RuntimeError):
    """Raised when no unique active provider exists in the resolved plan."""


class GlobalContractInvocationError(RuntimeError):
    """Preserve a provider-neutral operation failure code across isolation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def invoke_global_contract(
    interface_registry: InterfaceRegistry,
    contract_id: str,
    operation: str,
    payload: Mapping[str, Any],
) -> Any:
    """Invoke one activated provider without branching on an implementation pack."""
    eligible, denied = _eligible_providers(interface_registry, contract_id)
    if not eligible and denied:
        raise GlobalContractInvocationError(
            "denied",
            f"active profile does not grant {contract_id}",
        )
    if len(eligible) != 1:
        raise GlobalContractUnavailable(
            f"expected one active provider for {contract_id}; found {len(eligible)}"
        )
    return eligible[0]["operation"](operation, dict(payload))


def selected_global_providers(
    interface_registry: InterfaceRegistry,
    contract_id: str,
) -> tuple[dict[str, Any], ...]:
    """Return non-executable metadata for every selected provider."""
    eligible, denied = _eligible_providers(interface_registry, contract_id)
    if not eligible and denied:
        raise GlobalContractInvocationError(
            "denied",
            f"active profile does not grant {contract_id}",
        )
    return tuple(
        {
            key: value
            for key, value in item.items()
            if key != "operation"
        }
        for item in eligible
    )


def invoke_selected_global_provider(
    interface_registry: InterfaceRegistry,
    contract_id: str,
    provider_instance_id: str,
    operation: str,
    payload: Mapping[str, Any],
) -> Any:
    """Invoke one exact provider already selected by the active plan."""
    eligible, denied = _eligible_providers(interface_registry, contract_id)
    matches = [
        item
        for item in eligible
        if str(item.get("provider_instance_id") or "") == provider_instance_id
    ]
    if not matches and denied:
        raise GlobalContractInvocationError("denied", "provider capability denied")
    if len(matches) != 1:
        raise GlobalContractUnavailable(
            f"selected provider is unavailable: {contract_id}/{provider_instance_id}"
        )
    return matches[0]["operation"](operation, dict(payload))


@dataclass(frozen=True)
class GlobalContractClient:
    """Restricted typed consumer client for one activated provider pack."""

    interface_registry: InterfaceRegistry
    allowed_contract_ids: frozenset[str]
    consumer_pack_id: str

    def providers(self, contract_id: str) -> tuple[dict[str, Any], ...]:
        """List selected metadata only for a manifest-declared requirement."""
        self._require_declared(contract_id)
        return selected_global_providers(self.interface_registry, contract_id)

    def invoke(
        self,
        contract_id: str,
        operation: str,
        payload: Mapping[str, Any],
        *,
        provider_instance_id: str | None = None,
    ) -> Any:
        """Invoke one selected requirement without exposing registry authority."""
        self._require_declared(contract_id)
        bound_payload = dict(payload)
        bound_payload.pop("_contract_consumer_pack_id", None)
        bound_payload["_contract_consumer_pack_id"] = self.consumer_pack_id
        if provider_instance_id is None:
            return invoke_global_contract(
                self.interface_registry,
                contract_id,
                operation,
                bound_payload,
            )
        return invoke_selected_global_provider(
            self.interface_registry,
            contract_id,
            provider_instance_id,
            operation,
            bound_payload,
        )

    def _require_declared(self, contract_id: str) -> None:
        if contract_id not in self.allowed_contract_ids:
            raise PermissionError(
                f"contract was not declared by consumer: {contract_id}"
            )


def _eligible_providers(
    interface_registry: InterfaceRegistry,
    contract_id: str,
) -> tuple[list[dict[str, Any]], int]:
    plan = persisted_resolved_profile()
    if plan is None:
        raise GlobalContractUnavailable("resolved profile is not active")
    candidates = interface_registry.get(
        f"global_contract.provider.{contract_id}",
        strategy="all",
    )
    selected = {
        (item.source_pack_id, item.provider_instance_id, item.content_hash)
        for item in plan.providers
        if item.contract_id == contract_id
    }
    eligible: list[dict[str, Any]] = []
    denied = 0
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        source_pack_id = str(candidate.get("source_pack_id") or "").strip()
        identity = (
            source_pack_id,
            str(candidate.get("provider_instance_id") or ""),
            str(candidate.get("content_hash") or ""),
        )
        if (
            source_pack_id not in plan.effective_pack_set
            or str(candidate.get("contract_id") or "") != contract_id
            or identity not in selected
        ):
            continue
        required_capabilities = {
            str(value)
            for value in candidate.get("required_capabilities", [])
            if str(value)
        }
        if not required_capabilities.issubset(set(plan.effective_permissions)):
            denied += 1
            continue
        if callable(candidate.get("operation")):
            eligible.append(candidate)
    eligible.sort(
        key=lambda item: (
            str(item.get("provider_instance_id") or ""),
            str(item.get("content_hash") or ""),
        )
    )
    return eligible, denied
