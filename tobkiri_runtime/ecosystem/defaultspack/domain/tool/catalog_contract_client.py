"""Finite legacy catalog shape over the selected global tool registry."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    GlobalContractUnavailable,
    invoke_global_contract,
)
from core_runtime.resolved_profile_scope import active_resolved_profile

from .definition_projection import project_tool_definition as _legacy_shape

DEFINITION_CONTRACT = "rumi.resource.tool.definition.v1"


class ContractToolCatalog:
    """Expose legacy read methods without making domain services registry-aware."""

    def list_tools(self, filter_dict: Mapping[str, Any] | None = None) -> list[dict]:
        """List selected global definitions in the finite legacy shape."""
        try:
            snapshot = _invoke("list", {})
        except GlobalContractUnavailable:
            return []
        definitions = (
            snapshot.get("definitions") if isinstance(snapshot, Mapping) else []
        )
        tools = [
            _legacy_shape(item)
            for item in definitions or []
            if isinstance(item, Mapping)
        ]
        if filter_dict and isinstance(filter_dict.get("tags"), list):
            required = {str(item) for item in filter_dict["tags"]}
            tools = [
                item
                for item in tools
                if required.intersection(item.get("tags", []))
            ]
        return tools

    def get(self, tool_name: str) -> dict[str, Any] | None:
        """Resolve one exact tool ID or finite alias."""
        try:
            value = _invoke("resolve", {"tool_id": tool_name})
        except GlobalContractUnavailable:
            return None
        definition = value.get("definition") if isinstance(value, Mapping) else None
        return _legacy_shape(definition) if isinstance(definition, Mapping) else None

    def get_schema(self, tool_name: str) -> dict[str, Any]:
        """Return the legacy schema wrapper for one definition."""
        value = self.get(tool_name)
        return dict(value.get("schema") or {}) if isinstance(value, Mapping) else {}


def _invoke(operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
    plan = active_resolved_profile()
    if registry is None or plan is None:
        raise GlobalContractUnavailable("global tool registry is unavailable")
    return invoke_global_contract(
        registry,
        DEFINITION_CONTRACT,
        operation,
        {"profile_id": plan.profile_id, **dict(payload)},
    )
