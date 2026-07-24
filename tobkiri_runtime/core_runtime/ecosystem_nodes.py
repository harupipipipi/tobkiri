"""
Discovery and registry for Capability Graph node definitions.

Pack-provided files are loaded only after the existing pack approval/hash
verification gate passes. User-shared graph/profile loading is intentionally
out of scope for PR1.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .interface_registry import InterfaceRegistry
from .node_models import (
    CORE_START_NODE_ID,
    NodeDefinition,
    NodeValidationError,
    load_node_document,
    make_core_start_node,
)

logger = logging.getLogger(__name__)


class NodeDiscoveryError(RuntimeError):
    """Raised when node discovery finds invalid or conflicting definitions."""


class EcosystemNodeRegistry:
    """Load, validate, and register node definitions from approved packs."""

    def __init__(
        self,
        *,
        registry: Any = None,
        interface_registry: Optional[InterfaceRegistry] = None,
        approval_manager: Any = None,
        ecosystem_dir: Optional[str] = None,
        effective_pack_ids: Optional[Iterable[str]] = None,
    ) -> None:
        self.registry = registry
        self.interface_registry = interface_registry
        self.approval_manager = approval_manager
        self.ecosystem_dir = ecosystem_dir
        self.effective_pack_ids = (
            frozenset(str(item) for item in effective_pack_ids)
            if effective_pack_ids is not None
            else None
        )
        self.nodes: Dict[str, NodeDefinition] = {}
        self.diagnostics: List[Dict[str, Any]] = []

    def load_all_nodes(self, *, register: bool = True) -> Dict[str, NodeDefinition]:
        self.nodes = {}
        self.diagnostics = []

        self._register_node(make_core_start_node(), source="core", register=register)

        for pack_id, pack_info in self._iter_packs():
            ok, reason = self._is_pack_approved(pack_id)
            if not ok:
                self._diagnose(
                    "warning",
                    "pack_skipped_unapproved",
                    f"Pack '{pack_id}' is not approved or hash-verified: {reason}",
                    pack_id=pack_id,
                    reason=reason,
                )
                continue
            for node_file in self._discover_node_files(pack_info):
                try:
                    self._load_node_file(pack_id, node_file, register=register)
                except NodeValidationError as exc:
                    self._diagnose(
                        "error",
                        "invalid_node_file",
                        str(exc),
                        pack_id=pack_id,
                        path=str(node_file),
                    )
                    raise NodeDiscoveryError(f"{node_file}: {exc}") from exc

        return dict(self.nodes)

    def list_nodes(self) -> List[NodeDefinition]:
        if not self.nodes:
            self.load_all_nodes()
        return [self.nodes[node_id] for node_id in sorted(self.nodes)]

    def get_node(self, node_id: str) -> Optional[NodeDefinition]:
        if not self.nodes:
            self.load_all_nodes()
        return self.nodes.get(node_id)

    def to_public_list(self) -> List[Dict[str, Any]]:
        return [node.to_dict() for node in self.list_nodes()]

    def _load_node_file(self, pack_id: str, node_file: Path, *, register: bool) -> None:
        try:
            with node_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise NodeValidationError(f"invalid JSON: {exc}") from exc

        definitions = load_node_document(
            data,
            source_path=str(node_file),
            pack_id=pack_id,
        )
        for node in definitions:
            self._register_node(node, source="ecosystem", register=register)

    def _register_node(
        self,
        node: NodeDefinition,
        *,
        source: str,
        register: bool,
    ) -> None:
        if node.node_id in self.nodes:
            existing = self.nodes[node.node_id]
            if (
                existing.metadata.get("pack_id") == node.metadata.get("pack_id")
                and _is_nodes_components_duplicate(existing, node)
            ):
                self._diagnose(
                    "warning",
                    "duplicate_node_id_same_pack_skipped",
                    f"Duplicate node_id '{node.node_id}' in pack '{node.metadata.get('pack_id')}' was skipped",
                    node_id=node.node_id,
                    path=str(node.metadata.get("source_path") or ""),
                    existing_path=str(existing.metadata.get("source_path") or ""),
                )
                return
            raise NodeDiscoveryError(
                "duplicate node_id '{}': {} conflicts with {}".format(
                    node.node_id,
                    node.metadata.get("source_path") or source,
                    existing.metadata.get("source_path") or existing.metadata.get("owner") or "existing",
                )
            )
        if node.node_id.startswith("rumi.") and node.node_id != CORE_START_NODE_ID:
            raise NodeDiscoveryError(f"ecosystem packs cannot register core-owned node id '{node.node_id}'")

        self.nodes[node.node_id] = node
        if register and self.interface_registry is not None:
            self.interface_registry.register(
                f"node.{node.node_id}",
                node,
                meta={
                    "source": source,
                    "pack_id": node.metadata.get("pack_id"),
                    "_system": source == "core",
                },
            )

    def _iter_packs(self) -> Iterable[Tuple[str, Any]]:
        registry = self.registry or self._load_registry()
        packs = getattr(registry, "packs", None)
        if isinstance(packs, dict):
            return [
                (pack_id, pack_info)
                for pack_id, pack_info in packs.items()
                if self.effective_pack_ids is None
                or pack_id in self.effective_pack_ids
            ]
        return []

    def _load_registry(self) -> Any:
        from .paths import discover_pack_locations, resolve_pack_locations

        locations = (
            resolve_pack_locations(self.effective_pack_ids, self.ecosystem_dir)
            if self.effective_pack_ids is not None
            else discover_pack_locations(self.ecosystem_dir)
        )
        registry = type(
            "DiscoveredPackRegistry",
            (),
            {"packs": {loc.pack_id: loc for loc in locations}},
        )()
        self.registry = registry
        return registry

    def _approval_manager(self) -> Any:
        if self.approval_manager is not None:
            return self.approval_manager
        try:
            from .approval_manager import get_approval_manager

            self.approval_manager = get_approval_manager()
        except Exception:
            self.approval_manager = None
        return self.approval_manager

    def _is_pack_approved(self, pack_id: str) -> Tuple[bool, Optional[str]]:
        approval_manager = self._approval_manager()
        if approval_manager is None:
            return False, "approval_manager_unavailable"
        checker = getattr(approval_manager, "is_pack_approved_and_verified", None)
        if not callable(checker):
            return False, "approval_checker_unavailable"
        try:
            result = checker(pack_id)
        except Exception as exc:
            return False, f"approval_check_error:{exc}"
        if isinstance(result, tuple):
            ok = bool(result[0])
            reason = result[1] if len(result) > 1 else None
            return ok, reason
        return bool(result), None

    def _discover_node_files(self, pack_info: Any) -> List[Path]:
        pack_subdir = (
            getattr(pack_info, "pack_subdir", None)
            or getattr(pack_info, "subdir", None)
            or getattr(pack_info, "path", None)
        )
        if pack_subdir is None:
            return []
        base = Path(pack_subdir)
        candidates: List[Path] = []

        nodes_dir = base / "nodes"
        if nodes_dir.is_dir():
            candidates.extend(sorted(nodes_dir.glob("*.node.json")))

        components_dir = base / "components"
        if components_dir.is_dir():
            candidates.extend(
                component_dir / "node.json"
                for component_dir in sorted(components_dir.iterdir())
                if component_dir.is_dir() and (component_dir / "node.json").is_file()
            )

        return candidates

    def _diagnose(self, level: str, code: str, message: str, **meta: Any) -> None:
        self.diagnostics.append(
            {
                "level": level,
                "code": code,
                "message": message,
                **meta,
            }
        )
        if level == "error":
            logger.error("%s: %s", code, message)
        else:
            logger.info("%s: %s", code, message)


def _is_nodes_components_duplicate(existing: NodeDefinition, candidate: NodeDefinition) -> bool:
    existing_path = str(existing.metadata.get("source_path") or "").replace("\\", "/")
    candidate_path = str(candidate.metadata.get("source_path") or "").replace("\\", "/")
    return (
        "/nodes/" in existing_path
        and "/components/" in candidate_path
    ) or (
        "/components/" in existing_path
        and "/nodes/" in candidate_path
    )
