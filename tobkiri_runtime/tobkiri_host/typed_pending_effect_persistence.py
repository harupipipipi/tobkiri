"""Keep typed Host receipt rows separate from genuine pending-effect recovery."""

from __future__ import annotations

import re
from typing import Any, Mapping

_NAMESPACES = {
    "saved_tool_admission_v1": re.compile(r"saved-tool-admission-v1-[0-9a-f]{64}"),
    "action_approval_policy_v1": re.compile(r"action-approval-policy-v1-[0-9a-f]{64}"),
    "action_approval_policy_selection_v1": re.compile(
        r"action-approval-policy-selection-v1-[0-9a-f]{64}"
    ),
}


class TypedPendingEffectPersistence:
    """Delegate encrypted effect CAS while excluding only recognized receipts.

    Unknown kinds, malformed namespace IDs and untagged genuine effects remain
    visible to the strict PendingEffect parser and therefore fail recovery if
    corrupt. Receipt rows are still encrypted/authenticated by the same store.
    """

    def __init__(self, persistence: Any) -> None:
        self._persistence = persistence

    @property
    def supports_fused_lease_pending_effect_cas(self) -> bool:
        """Preserve the underlying authoritative effect/lease transaction support."""
        return self._persistence.supports_fused_lease_pending_effect_cas

    def create_host_pending_effect(self, effect_id: str, payload: Mapping[str, Any]) -> int:
        """Create a genuine pending effect through the existing encrypted store."""
        return self._persistence.create_host_pending_effect(effect_id, payload)

    def get_host_pending_effect(self, effect_id: str) -> Any:
        """Read a genuine effect snapshot without changing its validation."""
        return self._persistence.get_host_pending_effect(effect_id)

    def compare_and_swap_host_pending_effect(
        self, effect_id: str, *, expected_revision: int, payload: Mapping[str, Any]
    ) -> int:
        """Preserve the existing revision CAS and authoritative audit."""
        return self._persistence.compare_and_swap_host_pending_effect(
            effect_id, expected_revision=expected_revision, payload=payload
        )

    def list_host_pending_effects(self) -> list[Any]:
        """List every effect except exact known typed Host receipt namespaces."""
        result = []
        for revision, payload in self._persistence.list_host_pending_effects():
            pattern = _NAMESPACES.get(payload.get("record_kind"))
            identifier = payload.get("effect_id")
            if (
                pattern is not None
                and isinstance(identifier, str)
                and pattern.fullmatch(identifier)
            ):
                continue
            result.append((revision, payload))
        return result
