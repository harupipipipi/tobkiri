"""
unit_registry.py - ストア内ユニットの登録・列挙・メタ読み取り・アーティファクト管理

Store 配下のユニット（data / python / binary）を管理する。
公式は意味を解釈しない（No Favoritism）。

ユニット格納構造:
  <store_root>/<unit_namespace>/<unit_name>/<unit_version>/
    unit.json（必須）
    + 実体ファイル群

F-1 追加:
  - UnitMeta.artifacts フィールド
  - list_artifacts() メソッド（SHA256 ハッシュ付き、パストラバーサル防止）
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import is_path_within


VALID_KINDS = frozenset({"data", "python", "binary"})
VALID_EXEC_MODES = frozenset({"pack_container", "host_capability", "sandbox"})


@dataclass
class UnitMeta:
    unit_id: str
    version: str
    kind: str
    entrypoint: Optional[str] = None
    declared_by_pack_id: str = ""
    declared_at: str = ""
    requires_individual_approval: bool = True
    exec_modes_allowed: List[str] = field(default_factory=list)
    permission_id: str = ""
    unit_dir: Optional[Path] = None
    store_id: str = ""
    namespace: str = ""
    name: str = ""
    artifacts: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "version": self.version,
            "kind": self.kind,
            "entrypoint": self.entrypoint,
            "declared_by_pack_id": self.declared_by_pack_id,
            "declared_at": self.declared_at,
            "requires_individual_approval": self.requires_individual_approval,
            "exec_modes_allowed": self.exec_modes_allowed,
            "permission_id": self.permission_id,
            "store_id": self.store_id,
            "namespace": self.namespace,
            "name": self.name,
            "unit_dir": str(self.unit_dir) if self.unit_dir else None,
            "artifacts": self.artifacts,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UnitMeta":
        ud = data.get("unit_dir")
        return cls(
            unit_id=data.get("unit_id", ""),
            version=data.get("version", ""),
            kind=data.get("kind", "data"),
            entrypoint=data.get("entrypoint"),
            declared_by_pack_id=data.get("declared_by_pack_id", ""),
            declared_at=data.get("declared_at", ""),
            requires_individual_approval=data.get("requires_individual_approval", True),
            exec_modes_allowed=data.get("exec_modes_allowed", []),
            permission_id=data.get("permission_id", ""),
            unit_dir=Path(ud) if ud else None,
            store_id=data.get("store_id", ""),
            namespace=data.get("namespace", ""),
            name=data.get("name", ""),
            artifacts=data.get("artifacts", []),
        )


@dataclass
class UnitRef:
    store_id: str
    unit_id: str
    version: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "store_id": self.store_id,
            "unit_id": self.unit_id,
            "version": self.version,
        }


@dataclass
class PublishResult:
    success: bool
    unit_id: str = ""
    version: str = ""
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "unit_id": self.unit_id,
            "version": self.version,
            "error": self.error,
        }


class UnitRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        # A-6: O(1) index map — (unit_id, version) -> ver_dir Path
        self._index: Dict[Tuple[str, str], Path] = {}
        self._index_root: Optional[Path] = None

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # ------------------------------------------------------------------
    # A-6: Index management
    # ------------------------------------------------------------------

    def build_index(self, store_root: Path) -> None:
        """
        Build an O(1) lookup index for (unit_id, version) -> ver_dir.

        Scans the store directory tree once and populates self._index.
        Thread-safe via self._lock.
        """
        new_index: Dict[Tuple[str, str], Path] = {}
        resolved_root = store_root.resolve()
        if resolved_root.is_dir():
            for ns_dir in sorted(resolved_root.iterdir()):
                if not ns_dir.is_dir() or ns_dir.name.startswith("."):
                    continue
                for name_dir in sorted(ns_dir.iterdir()):
                    if not name_dir.is_dir() or name_dir.name.startswith("."):
                        continue
                    for ver_dir in sorted(name_dir.iterdir()):
                        if not ver_dir.is_dir() or ver_dir.name.startswith("."):
                            continue
                        unit_json = ver_dir / "unit.json"
                        if unit_json.exists():
                            meta = self._load_unit_json(
                                unit_json, ver_dir, ns_dir.name, name_dir.name,
                            )
                            if meta and meta.unit_id and meta.version:
                                key = (meta.unit_id, meta.version)
                                if key not in new_index:
                                    new_index[key] = ver_dir
        with self._lock:
            self._index = new_index
            self._index_root = resolved_root

    def invalidate_index(self) -> None:
        """Clear the O(1) lookup index. Thread-safe."""
        with self._lock:
            self._index.clear()
            self._index_root = None

    def list_units(self, store_root: Path) -> List[UnitMeta]:
        results: List[UnitMeta] = []
        if not store_root.is_dir():
            return results
        for ns_dir in sorted(store_root.iterdir()):
            if not ns_dir.is_dir() or ns_dir.name.startswith("."):
                continue
            for name_dir in sorted(ns_dir.iterdir()):
                if not name_dir.is_dir() or name_dir.name.startswith("."):
                    continue
                for ver_dir in sorted(name_dir.iterdir()):
                    if not ver_dir.is_dir() or ver_dir.name.startswith("."):
                        continue
                    unit_json = ver_dir / "unit.json"
                    if unit_json.exists():
                        meta = self._load_unit_json(
                            unit_json, ver_dir, ns_dir.name, name_dir.name,
                        )
                        if meta:
                            results.append(meta)
        # A-6: Build index as side-effect of list_units
        self.build_index(store_root)
        return results

    def get_unit(
        self,
        store_root: Path,
        namespace: str,
        name: str,
        version: str,
    ) -> Optional[UnitMeta]:
        unit_dir = store_root / namespace / name / version
        if not unit_dir.is_dir():
            return None
        if not is_path_within(unit_dir, store_root):
            return None
        unit_json = unit_dir / "unit.json"
        if not unit_json.exists():
            return None
        return self._load_unit_json(unit_json, unit_dir, namespace, name)

    def get_unit_by_ref(
        self,
        store_root: Path,
        unit_ref: "UnitRef",
    ) -> Optional[UnitMeta]:
        if not store_root.is_dir():
            return None

        # A-6: Try O(1) index lookup first
        resolved_root = store_root.resolve()
        with self._lock:
            index_valid = (
                bool(self._index)
                and self._index_root is not None
                and self._index_root == resolved_root
            )
            if index_valid:
                ver_dir = self._index.get(
                    (unit_ref.unit_id, unit_ref.version)
                )

        if index_valid:
            if ver_dir is not None and ver_dir.is_dir():
                unit_json = ver_dir / "unit.json"
                if unit_json.exists():
                    # Derive namespace and name from path structure:
                    # ver_dir = store_root / namespace / name / version
                    try:
                        rel = ver_dir.relative_to(resolved_root)
                        parts = rel.parts  # (namespace, name, version)
                        if len(parts) >= 2:
                            ns_name = parts[0]
                            unit_name = parts[1]
                        else:
                            ns_name = ""
                            unit_name = ""
                    except (ValueError, IndexError):
                        ns_name = ""
                        unit_name = ""
                    meta = self._load_unit_json(
                        unit_json, ver_dir, ns_name, unit_name,
                    )
                    if (
                        meta
                        and meta.unit_id == unit_ref.unit_id
                        and meta.version == unit_ref.version
                    ):
                        meta.store_id = unit_ref.store_id
                        return meta
            # Index hit but data mismatch or dir gone — fall through to scan
            # (Do NOT return None immediately; the index may be stale)

        # Fallback: O(n) full scan (backward compatible)
        for ns_dir in sorted(store_root.iterdir()):
            if not ns_dir.is_dir() or ns_dir.name.startswith("."):
                continue
            for name_dir in sorted(ns_dir.iterdir()):
                if not name_dir.is_dir() or name_dir.name.startswith("."):
                    continue
                for ver_dir in sorted(name_dir.iterdir()):
                    if not ver_dir.is_dir() or ver_dir.name.startswith("."):
                        continue
                    unit_json = ver_dir / "unit.json"
                    if unit_json.exists():
                        meta = self._load_unit_json(
                            unit_json, ver_dir, ns_dir.name, name_dir.name,
                        )
                        if (
                            meta
                            and meta.unit_id == unit_ref.unit_id
                            and meta.version == unit_ref.version
                        ):
                            meta.store_id = unit_ref.store_id
                            return meta
        return None

    def publish_unit(
        self,
        store_root: Path,
        source_dir: Path,
        namespace: str,
        name: str,
        version: str,
        store_id: str = "",
    ) -> PublishResult:
        src_unit_json = source_dir / "unit.json"
        if not src_unit_json.exists():
            return PublishResult(success=False, error="unit.json not found in source")

        meta = self._load_unit_json(src_unit_json, source_dir, namespace, name)
        if meta is None:
            return PublishResult(success=False, error="Failed to parse unit.json")

        if meta.kind not in VALID_KINDS:
            return PublishResult(
                success=False, unit_id=meta.unit_id,
                error=f"Invalid kind: {meta.kind}",
            )
        if meta.kind in ("python", "binary") and not meta.entrypoint:
            return PublishResult(
                success=False, unit_id=meta.unit_id,
                error=f"entrypoint is required for kind={meta.kind}",
            )
        for mode in meta.exec_modes_allowed:
            if mode not in VALID_EXEC_MODES:
                return PublishResult(
                    success=False, unit_id=meta.unit_id,
                    error=f"Invalid exec_mode: {mode}",
                )

        dest_dir = store_root / namespace / name / version
        if not is_path_within(dest_dir, store_root):
            return PublishResult(
                success=False, unit_id=meta.unit_id,
                error="Path traversal detected in publish destination",
            )
        if dest_dir.exists():
            return PublishResult(
                success=False, unit_id=meta.unit_id, version=version,
                error=f"Unit already exists at {dest_dir}",
            )

        try:
            shutil.copytree(str(source_dir), str(dest_dir), symlinks=False)
        except Exception as e:
            return PublishResult(
                success=False, unit_id=meta.unit_id, error=f"Failed to copy: {e}",
            )

        # A-6: Invalidate index after publish (new unit added)
        self.invalidate_index()

        self._audit("unit_published", True, {
            "store_id": store_id,
            "unit_id": meta.unit_id,
            "version": version,
            "kind": meta.kind,
            "namespace": namespace,
            "name": name,
        })
        return PublishResult(success=True, unit_id=meta.unit_id, version=version)

    @staticmethod
    def compute_entrypoint_sha256(unit_dir: Path, entrypoint: str) -> Optional[str]:
        ep_path = unit_dir / entrypoint
        if not ep_path.exists():
            return None
        if not is_path_within(ep_path, unit_dir):
            return None
        h = hashlib.sha256()
        with open(ep_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    # ------------------------------------------------------------------
    # F-1: Artifacts handling
    # ------------------------------------------------------------------

    def list_artifacts(
        self,
        store_root: Path,
        unit_ref: "UnitRef",
    ) -> Dict[str, Any]:
        """
        指定ユニットのアーティファクトファイル一覧と SHA256 ハッシュを返す。

        unit.json の "artifacts" に記載されたファイルを対象にする。
        パストラバーサルが検出された場合はエラーを返す。

        Args:
            store_root: ストアのルートディレクトリ
            unit_ref: 対象ユニットの参照情報

        Returns:
            {
                "success": True,
                "unit_id": str,
                "version": str,
                "artifacts": [
                    {"path": str, "sha256": str, "size_bytes": int, "exists": bool},
                    ...
                ]
            }
            失敗時は {"success": False, "error": str}
        """
        meta = self.get_unit_by_ref(store_root, unit_ref)
        if meta is None:
            return {
                "success": False,
                "error": f"Unit not found: {unit_ref.unit_id}@{unit_ref.version}",
            }

        unit_dir = meta.unit_dir
        if unit_dir is None or not unit_dir.is_dir():
            return {
                "success": False,
                "error": "Unit directory not found",
            }

        if not meta.artifacts:
            return {
                "success": True,
                "unit_id": meta.unit_id,
                "version": meta.version,
                "artifacts": [],
            }

        result_artifacts: List[Dict[str, Any]] = []
        for artifact_path_str in meta.artifacts:
            if not isinstance(artifact_path_str, str) or not artifact_path_str:
                continue

            artifact_path = unit_dir / artifact_path_str

            # パストラバーサル防止
            if not is_path_within(artifact_path, unit_dir):
                return {
                    "success": False,
                    "error": f"Path traversal detected: {artifact_path_str}",
                }

            if not artifact_path.is_file():
                result_artifacts.append({
                    "path": artifact_path_str,
                    "sha256": None,
                    "size_bytes": 0,
                    "exists": False,
                })
                continue

            # SHA256 ハッシュ計算
            h = hashlib.sha256()
            size = 0
            with open(artifact_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
                    size += len(chunk)

            result_artifacts.append({
                "path": artifact_path_str,
                "sha256": h.hexdigest(),
                "size_bytes": size,
                "exists": True,
            })

        return {
            "success": True,
            "unit_id": meta.unit_id,
            "version": meta.version,
            "artifacts": result_artifacts,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_unit_json(
        self,
        unit_json_path: Path,
        unit_dir: Path,
        namespace: str,
        name: str,
    ) -> Optional[UnitMeta]:
        try:
            with open(unit_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            return UnitMeta(
                unit_id=data.get("unit_id", ""),
                version=data.get("version", ""),
                kind=data.get("kind", "data"),
                entrypoint=data.get("entrypoint"),
                declared_by_pack_id=data.get("declared_by_pack_id", ""),
                declared_at=data.get("declared_at", ""),
                requires_individual_approval=data.get("requires_individual_approval", True),
                exec_modes_allowed=data.get("exec_modes_allowed", []),
                permission_id=data.get("permission_id", ""),
                unit_dir=unit_dir,
                namespace=namespace,
                name=name,
                artifacts=data.get("artifacts", []),
            )
        except Exception:
            return None

    @staticmethod
    def _audit(event_type: str, success: bool, details: Dict[str, Any]) -> None:
        try:
            from .audit_logger import get_audit_logger
            get_audit_logger().log_system_event(
                event_type=event_type, success=success, details=details,
            )
        except Exception:
            pass


_global_unit_registry: Optional[UnitRegistry] = None
_unit_lock = threading.Lock()


def get_unit_registry() -> UnitRegistry:
    global _global_unit_registry
    if _global_unit_registry is None:
        with _unit_lock:
            if _global_unit_registry is None:
                _global_unit_registry = UnitRegistry()
    return _global_unit_registry
