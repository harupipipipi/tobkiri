"""Defaultspack composition hooks for the generic Host HTTP contract adapter."""

from __future__ import annotations

from typing import Mapping, cast

from core_runtime.global_contracts.capability_capture import (
    CapabilityDispatchSession,
    CapabilityBindingSnapshot,
    capture_capability_binding_snapshot,
)
from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
)
from ecosystem.defaultspack.defaultspack.http_dynamic_targets import (
    defaultspack_dynamic_capability_targets,
)


def defaultspack_capability_snapshot(
    binding: HTTPContractBinding,
    *,
    session: CapabilityDispatchSession,
    catalog: Mapping[str, object],
) -> CapabilityBindingSnapshot:
    """Capture Defaultspack candidates through core identity verification."""

    return capture_capability_binding_snapshot(
        binding,
        session=session,
        catalog=catalog,
        dynamic_target_factory=defaultspack_dynamic_capability_targets,
    )


def defaultspack_capability_snapshot_mapping(
    binding: object,
    *,
    session: object,
    catalog: Mapping[str, object],
) -> Mapping[str, object]:
    """Return the Defaultspack surface's serialized capture projection."""

    if not isinstance(binding, HTTPContractBinding):
        raise ValueError("HTTP capability binding is invalid")
    snapshot = defaultspack_capability_snapshot(
        binding,
        session=cast(CapabilityDispatchSession, session),
        catalog=catalog,
    )
    return snapshot.to_mapping(
        profile_id=str(getattr(session, "profile_id", "")),
        profile_revision=str(getattr(session, "profile_revision", "")),
        activation_id=str(getattr(session, "activation_id", "")),
        plan_digest=str(getattr(session, "plan_digest", "")),
    )


def defaultspack_capability_binding(
    bindings: tuple[object, ...],
) -> object | None:
    """Select Defaultspack's sole capability-invocation map entry."""

    candidates = tuple(
        binding
        for binding in bindings
        if isinstance(binding, HTTPContractBinding)
        and binding.method == "POST"
        and binding.path == "/api/ui/capability/invoke"
    )
    return candidates[0] if len(candidates) == 1 else None


__all__ = [
    "defaultspack_capability_snapshot",
    "defaultspack_capability_binding",
    "defaultspack_capability_snapshot_mapping",
]
