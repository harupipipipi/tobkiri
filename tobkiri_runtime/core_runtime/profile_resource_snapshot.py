from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from .pack_boundary import finite_files, resolve_pack_root
from .profile_workspace import ProfileWorkspaceManager
from .resolved_profile import (
    ResolvedProfile,
    create_lockfile,
    write_lockfile,
)


class ProfileResourceSnapshotManager:
    def __init__(
        self,
        user_data_root: Path | None = None,
        *,
        ecosystem_dir: str | None = None,
    ) -> None:
        self.workspace_manager = ProfileWorkspaceManager(user_data_root)
        self.ecosystem_dir = ecosystem_dir

    def snapshot_resolved_profile(
        self,
        plan: ResolvedProfile,
    ) -> dict[str, Any]:
        """Snapshot every selected pack/provider/resource hash for one plan."""
        paths = self.workspace_manager.paths_for_profile(plan.profile_id)
        snapshot_root = paths.snapshots_dir / "resolved-profile" / plan.plan_hash
        snapshot_root.mkdir(parents=True, exist_ok=True)
        lockfile = create_lockfile(plan)
        write_lockfile(snapshot_root / "profile.lock.json", lockfile)
        manifest = {
            "version": 2,
            "profile_id": plan.profile_id,
            "profile_revision": plan.profile_revision,
            "input_hash": plan.input_hash,
            "plan_hash": plan.plan_hash,
            "effective_pack_set": list(plan.effective_pack_set),
            "packs": [asdict(item) for item in plan.packs],
            "providers": [asdict(item) for item in plan.providers],
            "resources": [asdict(item) for item in plan.projections],
            "lock_hash": lockfile.lock_hash,
        }
        (snapshot_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest

    def snapshot_default_resources(
        self,
        profile_id: str,
        *,
        base_pack: str,
        graph_id: str | None = None,
        graph_ids: list[str] | None = None,
        flow_ids: list[str] | None = None,
        prompt_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        paths = self.workspace_manager.paths_for_profile(profile_id)
        pack_path = self._pack_path(base_pack)
        snapshot_root = paths.snapshots_dir / base_pack
        flows_root = snapshot_root / "flows"
        prompts_root = snapshot_root / "prompts"
        flows_root.mkdir(parents=True, exist_ok=True)
        prompts_root.mkdir(parents=True, exist_ok=True)

        items: list[dict[str, Any]] = []
        requested_flow_ids = self._unique_strings(flow_ids or ["chat_turn"])
        requested_prompt_ids = self._unique_strings(prompt_ids or [])
        requested_graph_ids = self._unique_strings(graph_ids or ([graph_id] if graph_id else []))

        for flow_id in requested_flow_ids:
            source = self._resolve_flow_path(pack_path, flow_id)
            if source is None:
                continue
            dest = flows_root / source.name
            self._copy_with_record(pack_path, source, snapshot_root, dest, "flow", items)

        for prompt_id in requested_prompt_ids:
            for source in self._resolve_prompt_paths(pack_path, prompt_id):
                dest = prompts_root / self._prompt_snapshot_relative_path(pack_path, source)
                self._copy_with_record(pack_path, source, snapshot_root, dest, "prompt", items)

        graph_refs = self._graph_refs_for_ids(pack_path, requested_graph_ids)
        nodes_payload = {
            "version": 1,
            "graph_id": graph_id,
            "graph_ids": requested_graph_ids,
            "nodes": graph_refs["nodes"],
        }
        blocks_payload = {
            "version": 1,
            "graph_id": graph_id,
            "graph_ids": requested_graph_ids,
            "blocks": graph_refs["blocks"],
        }
        (snapshot_root / "nodes.json").write_text(
            json.dumps(nodes_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (snapshot_root / "blocks.json").write_text(
            json.dumps(blocks_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        manifest = {
            "version": 1,
            "profile_id": paths.profile_id,
            "source_pack": base_pack,
            "source_pack_path": str(pack_path),
            "source_graph_id": graph_id,
            "graph_ids": requested_graph_ids,
            "graph_refs": graph_refs,
            "requested_flow_ids": requested_flow_ids,
            "requested_prompt_ids": requested_prompt_ids,
            "snapshot_at": int(time.time()),
            "items": items,
        }
        (snapshot_root / "manifest.lock.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest

    def _pack_path(self, base_pack: str) -> Path:
        return resolve_pack_root(base_pack, self.ecosystem_dir)

    def _resolve_flow_path(self, pack_path: Path, flow_id: str) -> Path | None:
        candidates = []
        raw = Path(flow_id)
        if raw.suffix in {".yaml", ".yml"}:
            candidates.append(pack_path / raw)
            candidates.append(pack_path / "flows" / raw.name)
        else:
            candidates.extend(
                [
                    pack_path / "flows" / f"{flow_id}.flow.yaml",
                    pack_path / "flows" / f"{flow_id}.yaml",
                    pack_path / "flows" / flow_id / "flow.yaml",
                    pack_path / "flows" / f"{flow_id.rsplit('.', 1)[-1]}.flow.yaml",
                    pack_path / "flows" / flow_id.rsplit(".", 1)[-1] / "flow.yaml",
                ]
            )
        source = next(
            (
                safe
                for path in candidates
                if (safe := self._safe_pack_file(pack_path, path)) is not None
            ),
            None,
        )
        if source is not None:
            return source
        flows_dir = pack_path / "flows"
        if not flows_dir.is_dir():
            return None
        for candidate in finite_files(flows_dir, (".yaml", ".yml"), recursive=True):
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(data, dict) and data.get("flow_id") == flow_id:
                return candidate
        return None

    def _resolve_prompt_path(self, pack_path: Path, prompt_id: str) -> Path | None:
        paths = self._resolve_prompt_paths(pack_path, prompt_id)
        return paths[0] if paths else None

    def _resolve_prompt_paths(self, pack_path: Path, prompt_id: str) -> list[Path]:
        raw = Path(prompt_id)
        candidates = []
        if raw.suffix in {".md", ".txt", ".json", ".yaml", ".yml"}:
            candidates.append(pack_path / raw)
            candidates.append(pack_path / "prompts" / raw.name)
        else:
            prompt_name = prompt_id.rsplit(".", 1)[-1]
            candidates.extend(
                [
                    pack_path / "prompts" / f"{prompt_id}.md",
                    pack_path / "prompts" / f"{prompt_id}.prompt.md",
                    pack_path / "prompts" / f"{prompt_id}.system.md",
                    pack_path / "prompts" / prompt_id / "manifest.json",
                    pack_path / "prompts" / prompt_id / "prompt.md",
                    pack_path / "extensions" / "prompts" / prompt_id / "manifest.json",
                ]
            )
            if prompt_name != prompt_id:
                candidates.extend(
                    [
                        pack_path / "prompts" / f"{prompt_name}.md",
                        pack_path / "prompts" / f"{prompt_name}.prompt.md",
                        pack_path / "prompts" / f"{prompt_name}.system.md",
                        pack_path / "prompts" / prompt_name / "manifest.json",
                        pack_path / "prompts" / prompt_name / "prompt.md",
                        pack_path / "extensions" / "prompts" / prompt_name / "manifest.json",
                    ]
                )
        source = next(
            (
                safe
                for path in candidates
                if (safe := self._safe_pack_file(pack_path, path)) is not None
            ),
            None,
        )
        if source is None:
            return []
        if source.name == "manifest.json" and source.parent.parent.name in {"prompts"}:
            return self._prompt_manifest_files(pack_path, source)
        return [source]

    def _prompt_manifest_files(
        self, pack_path: Path, manifest_path: Path
    ) -> list[Path]:
        files = [manifest_path]
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            manifest = {}
        if isinstance(manifest, dict):
            config = manifest.get("config")
            template_file = config.get("template_file") if isinstance(config, dict) else None
            if isinstance(template_file, str) and template_file:
                candidate = manifest_path.parent / template_file
                safe = self._safe_pack_file(pack_path, candidate)
                if safe is not None:
                    files.append(safe)
        fallback = manifest_path.parent / "prompt.md"
        safe_fallback = self._safe_pack_file(pack_path, fallback)
        if safe_fallback is not None and safe_fallback not in files:
            files.append(safe_fallback)
        return files

    def _prompt_snapshot_relative_path(self, pack_path: Path, source: Path) -> Path:
        for prompt_root in (pack_path / "extensions" / "prompts", pack_path / "prompts"):
            try:
                return source.relative_to(prompt_root)
            except ValueError:
                continue
        return Path(source.name)

    def _copy_with_record(
        self,
        pack_path: Path,
        source: Path,
        snapshot_root: Path,
        dest: Path,
        item_type: str,
        items: list[dict[str, Any]],
    ) -> None:
        safe_source = self._safe_pack_file(pack_path, source)
        if safe_source is None:
            raise PermissionError("snapshot source escapes the selected Pack")
        source = safe_source
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        items.append(
            {
                "type": item_type,
                "source": source.relative_to(pack_path).as_posix(),
                "dest": dest.relative_to(snapshot_root).as_posix(),
                "sha256": self._sha256(source),
            }
        )

    def _graph_refs_for_ids(self, pack_path: Path, graph_ids: list[str]) -> dict[str, Any]:
        graph_entries = []
        node_refs = []
        block_refs = []
        for graph_id in graph_ids:
            graph_path = self._resolve_graph_path(pack_path, graph_id)
            if graph_path is None:
                continue
            refs = self._graph_refs_from_path(graph_path)
            node_refs.extend(refs["nodes"])
            block_refs.extend(refs["blocks"])
            graph_entries.append(
                {
                    "graph_id": graph_id,
                    "source": graph_path.relative_to(pack_path).as_posix(),
                    "nodes": refs["nodes"],
                    "blocks": refs["blocks"],
                }
            )
        return {
            "graphs": graph_entries,
            "nodes": sorted(set(node_refs)),
            "blocks": sorted(set(block_refs)),
        }

    def _resolve_graph_path(self, pack_path: Path, graph_id: str | None) -> Path | None:
        if not graph_id:
            return None
        graph_name = graph_id.rsplit(".", 1)[-1]
        candidates = [
            pack_path / "graphs" / f"{graph_name}.graph.yaml",
            pack_path / "graphs" / f"{graph_name}.yaml",
            pack_path / "graphs" / f"{graph_id}.graph.yaml",
            pack_path / "graphs" / f"{graph_id.replace('.', '_')}.graph.yaml",
        ]
        graph_path = next(
            (
                safe
                for path in candidates
                if (safe := self._safe_pack_file(pack_path, path)) is not None
            ),
            None,
        )
        if graph_path is not None:
            return graph_path
        graphs_dir = pack_path / "graphs"
        if not graphs_dir.is_dir():
            return None
        for candidate in finite_files(graphs_dir, (".yaml", ".yml"), recursive=False):
            try:
                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(data, dict) and data.get("graph_id") == graph_id:
                return candidate
        return None

    def _graph_refs_from_path(self, graph_path: Path) -> dict[str, list[str]]:
        data = yaml.safe_load(graph_path.read_text(encoding="utf-8")) or {}
        nodes = data.get("nodes") if isinstance(data, dict) else []
        node_refs = []
        block_refs = []
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                ref = str(node.get("ref") or node.get("node_id") or node.get("id") or "")
                if ref:
                    node_refs.append(ref)
                block = node.get("block") or node.get("handler") or node.get("function")
                if block:
                    block_refs.append(str(block))
        blocks = data.get("blocks") if isinstance(data, dict) else []
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, str):
                    block_refs.append(block)
                elif isinstance(block, dict):
                    block_ref = (
                        block.get("ref")
                        or block.get("block")
                        or block.get("handler")
                        or block.get("function")
                        or block.get("id")
                    )
                    if block_ref:
                        block_refs.append(str(block_ref))
        return {"nodes": sorted(set(node_refs)), "blocks": sorted(set(block_refs))}

    def _unique_strings(self, values: Iterable[str | None]) -> list[str]:
        result = []
        seen = set()
        for value in values:
            if not isinstance(value, str):
                continue
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    @staticmethod
    def _safe_pack_file(pack_path: Path, candidate: Path) -> Path | None:
        """Return one regular in-Pack file without following symlink edges."""

        root = pack_path.resolve()
        try:
            relative = candidate.relative_to(pack_path)
        except ValueError:
            return None
        current = pack_path
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return None
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        return resolved if resolved.is_file() else None

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
