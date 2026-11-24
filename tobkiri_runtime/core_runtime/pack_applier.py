"""
pack_applier.py - Pack Apply (staging -> ecosystem)

staging に展開された Pack を ecosystem/ にコピー（apply）する。
apply 前にバックアップを作成し、pack_identity の不一致を検出して拒否する。
"""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import ECOSYSTEM_DIR, find_ecosystem_json
from .pack_boundary import finite_children
from .validation import check_path_within, is_safe_staging_id, validate_pack_id

BACKUP_ROOT = "user_data/pack_backups"
STAGING_ROOT = "user_data/pack_staging"


@dataclass
class ApplyResult:
    success: bool
    applied_pack_ids: List[str] = field(default_factory=list)
    backup_paths: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None
    errors: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "applied_pack_ids": self.applied_pack_ids,
            "backup_paths": self.backup_paths,
            "error": self.error,
            "errors": self.errors,
        }


class PackApplier:
    def __init__(
        self,
        ecosystem_dir: Optional[str] = None,
        backup_root: Optional[str] = None,
        staging_root: Optional[str] = None,
    ):
        self._ecosystem_dir = Path(ecosystem_dir or ECOSYSTEM_DIR)
        self._backup_root = Path(backup_root or BACKUP_ROOT)
        self._staging_root = Path(staging_root or STAGING_ROOT)
        self._lock = threading.RLock()

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _now_ts_safe() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _staging_dir_for(self, staging_id: str) -> Optional[Path]:
        if not is_safe_staging_id(staging_id):
            return None
        staging_dir = self._staging_root / str(staging_id)
        path_ok, _ = check_path_within(staging_dir, self._staging_root)
        if not path_ok:
            return None
        return staging_dir

    def apply(
        self,
        staging_id: str,
        mode: str = "replace",
        actor: str = "api_user",
    ) -> ApplyResult:
        if mode != "replace":
            return ApplyResult(success=False, error=f"Unsupported mode: {mode}")

        staging_dir = self._staging_dir_for(staging_id)
        if staging_dir is None:
            return ApplyResult(success=False, error=f"Invalid staging_id: {staging_id}")
        if not staging_dir.exists():
            return ApplyResult(success=False, error=f"Staging not found: {staging_id}")

        meta_path = staging_dir / "meta.json"
        if not meta_path.exists():
            return ApplyResult(success=False, error="meta.json not found in staging")

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:
            return ApplyResult(success=False, error=f"Failed to read meta.json: {e}")

        detected_pack_ids = meta.get("detected_pack_ids", [])
        is_multi_pack = meta.get("is_multi_pack", False)
        payload_dir = staging_dir / "payload"

        if not payload_dir.exists():
            return ApplyResult(success=False, error="payload directory not found")

        top_dirs = list(finite_children(payload_dir, directories_only=True))
        if len(top_dirs) != 1:
            return ApplyResult(
                success=False,
                error=f"Expected 1 top-level dir in payload, found {len(top_dirs)}",
            )
        top_dir = top_dirs[0]

        self._audit("pack_apply_started", True, {
            "staging_id": staging_id,
            "detected_pack_ids": detected_pack_ids,
            "is_multi_pack": is_multi_pack,
            "mode": mode,
            "actor": actor,
        })

        result = ApplyResult(success=True)

        try:
            if is_multi_pack:
                packs_dir = top_dir / "packs"
                if not packs_dir.is_dir():
                    return ApplyResult(
                        success=False,
                        error="Multi-pack but no packs/ directory",
                    )
                for pack_id in detected_pack_ids:
                    pack_src = packs_dir / pack_id
                    if not pack_src.is_dir():
                        result.errors.append({
                            "pack_id": pack_id,
                            "error": f"Pack directory not found: {pack_id}",
                        })
                        continue
                    ok, err, backup_path = self._apply_single_pack(pack_id, pack_src)
                    if ok:
                        result.applied_pack_ids.append(pack_id)
                        if backup_path:
                            result.backup_paths[pack_id] = str(backup_path)
                    else:
                        result.errors.append({"pack_id": pack_id, "error": err})
            else:
                pack_id = detected_pack_ids[0] if detected_pack_ids else top_dir.name
                pack_src = top_dir
                ok, err, backup_path = self._apply_single_pack(pack_id, pack_src)
                if ok:
                    result.applied_pack_ids.append(pack_id)
                    if backup_path:
                        result.backup_paths[pack_id] = str(backup_path)
                else:
                    result.success = False
                    result.error = err
                    result.errors.append({"pack_id": pack_id, "error": err})

            if result.errors and not result.applied_pack_ids:
                result.success = False
                if not result.error:
                    result.error = "All packs failed to apply"
        except Exception as e:
            result.success = False
            result.error = str(e)

        self._audit(
            "pack_apply_completed" if result.success else "pack_apply_failed",
            result.success,
            {
                "staging_id": staging_id,
                "applied_pack_ids": result.applied_pack_ids,
                "errors": result.errors,
                "actor": actor,
            },
        )
        return result

    def apply_staging(
        self,
        staging_id: str,
        mode: str = "replace",
        actor: str = "api_user",
    ) -> ApplyResult:
        """Compatibility wrapper for callers that still use the old name."""
        return self.apply(staging_id, mode=mode, actor=actor)

    def _apply_single_pack(
        self,
        pack_id: str,
        pack_src: Path,
    ) -> Tuple[bool, Optional[str], Optional[Path]]:
        if not isinstance(pack_id, str) or not validate_pack_id(pack_id):
            return False, f"Invalid pack_id: {pack_id}", None
        src_ok, src_error = check_path_within(pack_src, self._staging_root)
        if not src_ok:
            return False, src_error, None
        dest = self._ecosystem_dir / pack_id
        dest_ok, dest_error = check_path_within(dest, self._ecosystem_dir)
        if not dest_ok:
            return False, dest_error, None
        backup_path = None

        if dest.exists() and dest.is_dir():
            ok, err = self._check_pack_identity(pack_src, dest)
            if not ok:
                return False, err, None
            backup_path = self._create_backup(pack_id, dest)
            shutil.rmtree(dest)

        self._ecosystem_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(pack_src), str(dest), symlinks=False)

        try:
            from .approval_manager import get_approval_manager
            am = get_approval_manager()
            am.mark_modified(pack_id)
        except Exception:
            pass

        return True, None, backup_path

    def _check_pack_identity(
        self,
        new_pack_dir: Path,
        existing_pack_dir: Path,
    ) -> Tuple[bool, Optional[str]]:
        new_identity = self._read_pack_identity(new_pack_dir)
        existing_identity = self._read_pack_identity(existing_pack_dir)

        if new_identity is None:
            return False, "New pack has no ecosystem.json or unreadable"
        if existing_identity is None:
            return True, None

        new_pid = new_identity.get("pack_id") or ""
        existing_pid = existing_identity.get("pack_id") or ""

        if new_pid and existing_pid and new_pid != existing_pid:
            return False, (
                f"pack_id mismatch: existing='{existing_pid}', "
                f"new='{new_pid}'"
            )

        # Check pack_identity (e.g. "github:author/repo")
        new_pi = new_identity.get("pack_identity") or ""
        existing_pi = existing_identity.get("pack_identity") or ""

        if new_pi and existing_pi and new_pi != existing_pi:
            return False, (
                f"pack_identity mismatch: existing pack_identity='{existing_pi}', "
                f"new pack_identity='{new_pi}' "
                f"(pack_id='{new_pid}'). "
                f"To replace, manually remove the existing pack first."
            )

        return True, None

    def _read_pack_identity(self, pack_dir: Path) -> Optional[Dict[str, Any]]:
        eco_json, _ = find_ecosystem_json(pack_dir)
        if eco_json is None:
            return None
        try:
            with open(eco_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "pack_id": data.get("pack_id"),
                "pack_identity": data.get("pack_identity"),
            }
        except Exception:
            return None

    def _create_backup(self, pack_id: str, pack_dir: Path) -> Path:
        ts = self._now_ts_safe()
        backup_dir = self._backup_root / pack_id / ts
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(pack_dir), str(backup_dir), symlinks=False)
        return backup_dir

    @staticmethod
    def _audit(event_type: str, success: bool, details: Dict[str, Any]) -> None:
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type=event_type,
                success=success,
                details=details,
                error=str(details.get("error") or ""),
            )
        except Exception:
            pass


_global_applier: Optional[PackApplier] = None
_applier_lock = threading.Lock()


def get_pack_applier() -> PackApplier:
    global _global_applier
    if _global_applier is None:
        with _applier_lock:
            if _global_applier is None:
                _global_applier = PackApplier()
    return _global_applier


def reset_pack_applier(**kwargs) -> PackApplier:
    global _global_applier
    with _applier_lock:
        _global_applier = PackApplier(**kwargs)
    return _global_applier
