"""Manifest-authoritative component entrypoint resolution.

The resolver intentionally returns a verified path only.  It never imports or
executes an entrypoint and therefore cannot become an alternate authority or a
host-execution fallback around PackVM.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

from .registry import DomainComponentRegistry


_RESOLVER_MODULE = "ecosystem.defaultspack.domain.components.entrypoints"


class ComponentEntrypointResolutionError(RuntimeError):
    """Report a safe, stable component-resolution failure."""

    def __init__(
        self,
        *,
        category: str,
        component_id: str,
        contract_id: str,
        revision: str,
        reason: str,
    ) -> None:
        self.category = category
        self.component_id = component_id
        self.contract_id = contract_id
        self.revision = revision
        self.reason = reason
        super().__init__(self.safe_diagnostic())

    def safe_diagnostic(self) -> str:
        """Return diagnostics without filesystem paths or component payloads."""

        component = f"{self.category}/{self.component_id}"
        revision = self.revision or "unknown"
        return (
            "component resolution failed: "
            f"module={_RESOLVER_MODULE}, component={component}, "
            f"contract={self.contract_id}, revision={revision}, "
            f"reason={self.reason}"
        )


@dataclass(frozen=True)
class ResolvedComponentEntrypoint:
    """A manifest-selected, boundary-checked component entrypoint."""

    category: str
    component_id: str
    contract_id: str
    revision: str
    manifest_path: Path
    path: Path


def _raise_resolution_error(
    *,
    category: str,
    component_id: str,
    contract_id: str,
    revision: str,
    reason: str,
) -> NoReturn:
    raise ComponentEntrypointResolutionError(
        category=category,
        component_id=component_id,
        contract_id=contract_id,
        revision=revision,
        reason=reason,
    )


def _safe_entrypoint_path(
    *,
    manifest_path: Path,
    relative_path: str,
    category: str,
    component_id: str,
    contract_id: str,
    revision: str,
) -> Path:
    normalized = relative_path.replace("\\", "/").strip()
    pure_path = PurePosixPath(normalized)
    if not normalized or pure_path.is_absolute() or ".." in pure_path.parts or "." == normalized:
        _raise_resolution_error(
            category=category,
            component_id=component_id,
            contract_id=contract_id,
            revision=revision,
            reason="invalid_relative_path",
        )

    component_root = Path(os.path.normpath(manifest_path.parent.absolute()))
    candidate = Path(os.path.normpath((component_root / normalized).absolute()))
    try:
        candidate.relative_to(component_root)
        cursor = candidate
        while cursor != component_root:
            if cursor.is_symlink():
                raise OSError("symlinked component entrypoint")
            cursor = cursor.parent
        if component_root.is_symlink() or not candidate.is_file():
            raise OSError("component entrypoint is not a regular file")
    except (OSError, RuntimeError, ValueError):
        _raise_resolution_error(
            category=category,
            component_id=component_id,
            contract_id=contract_id,
            revision=revision,
            reason="entrypoint_outside_component_or_unavailable",
        )
    return candidate


def resolve_component_entrypoint(
    registry: DomainComponentRegistry,
    *,
    category: str,
    component_id: str,
    contract_id: str,
    required: bool = True,
) -> ResolvedComponentEntrypoint | None:
    """Resolve a canonical component ID and manifest entrypoint contract.

    Aliases are resolved by the supplied registry, after which the canonical
    component ID from the selected manifest is retained in the result.  No
    filesystem scanning or import-name inference participates in selection.
    """

    requested_category = str(category or "").strip()
    requested_component = str(component_id or "").strip()
    requested_contract = str(contract_id or "").strip()
    component = registry.get(requested_category, requested_component)
    if component is None:
        _raise_resolution_error(
            category=requested_category or "unknown",
            component_id=requested_component or "unknown",
            contract_id=requested_contract or "unknown",
            revision="",
            reason="component_not_selected",
        )

    manifest = component.manifest
    revision = str(manifest.get("version") or "").strip()
    entrypoints = manifest.get("entrypoints")
    relative_path = (
        entrypoints.get(requested_contract)
        if isinstance(entrypoints, dict) and requested_contract
        else None
    )
    if not isinstance(relative_path, str) or not relative_path.strip():
        if not required:
            return None
        _raise_resolution_error(
            category=component.category,
            component_id=component.id,
            contract_id=requested_contract or "unknown",
            revision=revision,
            reason="entrypoint_contract_not_declared",
        )

    manifest_path = Path(component.manifest_path)
    path = _safe_entrypoint_path(
        manifest_path=manifest_path,
        relative_path=relative_path,
        category=component.category,
        component_id=component.id,
        contract_id=requested_contract,
        revision=revision,
    )
    return ResolvedComponentEntrypoint(
        category=component.category,
        component_id=component.id,
        contract_id=requested_contract,
        revision=revision,
        manifest_path=manifest_path,
        path=path,
    )
