"""Discovery, registry, and validation for Capability Graph files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml  # type: ignore[import-untyped]

from .graph_models import GraphDefinition, GraphValidationError, load_graph_document
from .interface_registry import InterfaceRegistry
from .port_standards import GraphValidationResult, validate_graph_ports
from .profile_models import ProfileDefinition

logger = logging.getLogger(__name__)


class GraphDiscoveryError(RuntimeError):
    """Raised when graph discovery finds invalid or conflicting files."""


class CapabilityGraphLoader:
    """Load user-shared and approved pack-provided Capability Graphs."""

    def __init__(
        self,
        *,
        registry: Any = None,
        interface_registry: Optional[InterfaceRegistry] = None,
        approval_manager: Any = None,
        ecosystem_dir: Optional[str] = None,
        shared_graphs_dir: Optional[str | Path] = None,
        workspace_graphs_dir: Optional[str | Path] = None,
        effective_pack_ids: Optional[Iterable[str]] = None,
    ) -> None:
        base_dir = Path(__file__).resolve().parent.parent
        self.registry = registry
        self.interface_registry = interface_registry
        self.approval_manager = approval_manager
        self.ecosystem_dir = ecosystem_dir
        self.effective_pack_ids = (
            frozenset(str(item) for item in effective_pack_ids)
            if effective_pack_ids is not None
            else None
        )
        self.shared_graphs_dir = (
            Path(shared_graphs_dir)
            if shared_graphs_dir is not None
            else base_dir / "user_data" / "shared" / "graphs"
        )
        self.workspace_graphs_dir = (
            Path(workspace_graphs_dir)
            if workspace_graphs_dir is not None
            else base_dir / "graphs"
        )
        self.graphs: Dict[str, GraphDefinition] = {}
        self.diagnostics: List[Dict[str, Any]] = []

    def load_all_graphs(self, *, register: bool = True) -> Dict[str, GraphDefinition]:
        self.graphs = {}
        self.diagnostics = []

        for graph_file in self._discover_user_graph_files():
            self._load_graph_file(
                graph_file,
                pack_id=None,
                source_type="user",
                register=register,
            )

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
            for graph_file in self._discover_pack_graph_files(pack_info):
                self._load_graph_file(
                    graph_file,
                    pack_id=pack_id,
                    source_type="ecosystem",
                    register=register,
                )

        for graph_file in self._discover_workspace_graph_files():
            self._load_graph_file(
                graph_file,
                pack_id=None,
                source_type="workspace",
                register=register,
            )

        return dict(self.graphs)

    def list_graphs(self) -> List[GraphDefinition]:
        if not self.graphs:
            self.load_all_graphs()
        return [self.graphs[graph_id] for graph_id in sorted(self.graphs)]

    def get_graph(self, graph_id: str) -> Optional[GraphDefinition]:
        if not self.graphs:
            self.load_all_graphs()
        return self.graphs.get(graph_id)

    def to_public_list(self) -> List[Dict[str, Any]]:
        return [graph.to_dict() for graph in self.list_graphs()]

    def validate_graph(
        self,
        graph_id: str,
        *,
        node_registry: Any,
        profile: Optional[ProfileDefinition] = None,
    ) -> GraphValidationResult:
        graph = self.get_graph(graph_id)
        if graph is None:
            return GraphValidationResult(
                ok=False,
                diagnostics=[
                    {
                        "level": "error",
                        "code": "graph_not_found",
                        "message": f"Graph '{graph_id}' was not found",
                        "graph_id": graph_id,
                    }
                ],
            )
        nodes = getattr(node_registry, "nodes", None)
        if not nodes:
            nodes = node_registry.load_all_nodes(register=True)
        return validate_graph_ports(graph, nodes=dict(nodes), profile=profile)

    def _load_graph_file(
        self,
        graph_file: Path,
        *,
        pack_id: Optional[str],
        source_type: str,
        register: bool,
    ) -> None:
        try:
            with graph_file.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            graph = load_graph_document(
                data,
                source_path=str(graph_file),
                pack_id=pack_id,
                source_type=source_type,
            )
            self._register_graph(graph, register=register)
        except (OSError, yaml.YAMLError, GraphValidationError) as exc:
            self._diagnose(
                "error",
                "invalid_graph_file",
                str(exc),
                pack_id=pack_id,
                path=str(graph_file),
            )
            raise GraphDiscoveryError(f"{graph_file}: {exc}") from exc

    def _register_graph(self, graph: GraphDefinition, *, register: bool) -> None:
        if graph.graph_id in self.graphs:
            existing = self.graphs[graph.graph_id]
            raise GraphDiscoveryError(
                "duplicate graph_id '{}': {} conflicts with {}".format(
                    graph.graph_id,
                    graph.metadata.get("source_path") or "new graph",
                    existing.metadata.get("source_path") or "existing graph",
                )
            )
        self.graphs[graph.graph_id] = graph
        if register and self.interface_registry is not None:
            self.interface_registry.register(
                f"graph.{graph.graph_id}",
                graph,
                meta={
                    "source": graph.metadata.get("source_type"),
                    "pack_id": graph.metadata.get("pack_id"),
                    "_system": graph.metadata.get("source_type") == "workspace",
                },
            )

    def _discover_user_graph_files(self) -> List[Path]:
        if not self.shared_graphs_dir.is_dir():
            return []
        return sorted(self.shared_graphs_dir.glob("*.graph.yaml"))

    def _discover_pack_graph_files(self, pack_info: Any) -> List[Path]:
        pack_subdir = (
            getattr(pack_info, "pack_subdir", None)
            or getattr(pack_info, "subdir", None)
            or getattr(pack_info, "path", None)
        )
        if pack_subdir is None:
            return []
        graphs_dir = Path(pack_subdir) / "graphs"
        if not graphs_dir.is_dir():
            return []
        return sorted(graphs_dir.glob("*.graph.yaml"))

    def _discover_workspace_graph_files(self) -> List[Path]:
        if not self.workspace_graphs_dir.is_dir():
            return []
        return sorted(self.workspace_graphs_dir.glob("*.graph.yaml"))

    def _iter_packs(self) -> Iterable[Tuple[str, Any]]:
        registry = self.registry or self._load_registry()
        discovered: Dict[str, Any] = {}
        packs = getattr(registry, "packs", None)
        if isinstance(packs, dict):
            discovered.update(packs)
        for loc in self._discover_installed_packs():
            discovered.setdefault(loc.pack_id, loc)
        return [
            (pack_id, pack_info)
            for pack_id, pack_info in discovered.items()
            if self.effective_pack_ids is None
            or pack_id in self.effective_pack_ids
        ]

    def _discover_installed_packs(self) -> List[Any]:
        from .paths import discover_pack_locations, resolve_pack_locations

        try:
            if self.effective_pack_ids is not None:
                return list(
                    resolve_pack_locations(
                        self.effective_pack_ids,
                        self.ecosystem_dir,
                    )
                )
            return list(discover_pack_locations(self.ecosystem_dir))
        except Exception:
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
