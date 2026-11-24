"""Fail-closed tombstone for the removed runtime Ecosystem registry.

Pack discovery and executable registration are owned by the Pack v4
composition root.  This module retains data shapes for import compatibility;
it cannot discover manifests or create a runtime inventory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class LegacyRegistryUnavailable(RuntimeError):
    """Raised whenever removed runtime Registry behavior is requested."""


@dataclass
class ComponentInfo:
    """Read-only legacy component shape used by offline data converters."""

    type: str
    id: str
    version: str
    uuid: str
    manifest: dict[str, Any]
    path: Path
    pack_id: str
    permissions_required: dict[str, Any] = field(default_factory=dict)

    @property
    def full_id(self) -> str:
        """Return the historical qualified component identifier."""
        return f"{self.pack_id}:{self.type}:{self.id}"


@dataclass
class PackInfo:
    """Read-only legacy Pack shape used by offline data converters."""

    pack_id: str
    pack_identity: str
    version: str
    uuid: str
    ecosystem: dict[str, Any]
    path: Path
    subdir: Path | None = None
    components: dict[str, ComponentInfo] = field(default_factory=dict)
    addons: list[dict[str, Any]] = field(default_factory=list)
    routes: list[dict[str, Any]] = field(default_factory=list)


class Registry:
    """Removed runtime authority; no filesystem or manifest access is allowed."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.packs: dict[str, PackInfo] = {}

    def load_all_packs(self) -> dict[str, PackInfo]:
        """Reject legacy runtime discovery instead of silently scanning Packs."""
        raise LegacyRegistryUnavailable(
            "runtime Ecosystem Registry was removed; use a verified Pack v4 "
            "ResolvedPlan and V4DispatchSession"
        )


def get_registry() -> Registry:
    """Reject access to the removed process-global runtime inventory."""
    raise LegacyRegistryUnavailable(
        "process-global Ecosystem Registry is unavailable in Pack v4 runtime"
    )


def reload_registry() -> Registry:
    """Reject attempts to rebuild authority from installed filesystem state."""
    raise LegacyRegistryUnavailable(
        "runtime Registry reload is forbidden; activate a new verified v4 plan"
    )


def resolve_load_order(*_args: Any, **_kwargs: Any) -> list[str]:
    """Reject dependency resolution through legacy Pack metadata."""
    raise LegacyRegistryUnavailable(
        "legacy load order is unavailable; use ProfileLock effective_set"
    )


__all__ = [
    "ComponentInfo",
    "LegacyRegistryUnavailable",
    "PackInfo",
    "Registry",
    "get_registry",
    "reload_registry",
    "resolve_load_order",
]
