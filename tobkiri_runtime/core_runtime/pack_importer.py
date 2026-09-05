"""
pack_importer.py - Pack Import (folder / .zip / .rumipack -> staging)

フォルダ / .zip / .rumipack（=zip互換）から Pack を
user_data/pack_staging/<staging_id>/payload/ に安全に展開する。

セキュリティ:
- Zip Slip 対策（../ 絶対パス シンボリックリンク拒否）
- zip 爆弾対策（ファイル数上限 / 総サイズ上限）
- 単一トップレベルディレクトリ強制
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .validation import check_path_within, is_safe_staging_id
from .pack_boundary import finite_children


STAGING_ROOT = "user_data/pack_staging"
MAX_FILES = 2000
MAX_TOTAL_SIZE = 500 * 1024 * 1024
ARCHIVE_EXTENSIONS = frozenset({".zip", ".rumipack"})


@dataclass
class ImportResult:
    success: bool
    staging_id: str = ""
    staging_dir: str = ""
    input_type: str = ""
    source_path: str = ""
    detected_pack_ids: List[str] = field(default_factory=list)
    is_multi_pack: bool = False
    error: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    build_commands: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "staging_id": self.staging_id,
            "staging_dir": self.staging_dir,
            "input_type": self.input_type,
            "source_path": self.source_path,
            "detected_pack_ids": self.detected_pack_ids,
            "is_multi_pack": self.is_multi_pack,
            "error": self.error,
            "meta": self.meta,
            "warnings": self.warnings,
            "build_commands": self.build_commands,
        }


class PackImporter:
    def __init__(self, staging_root: Optional[str] = None):
        self._staging_root = Path(staging_root or STAGING_ROOT)
        self._lock = threading.RLock()

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _staging_dir_for(self, staging_id: str) -> Optional[Path]:
        if not is_safe_staging_id(staging_id):
            return None
        staging_dir = self._staging_root / str(staging_id)
        path_ok, _ = check_path_within(staging_dir, self._staging_root)
        if not path_ok:
            return None
        return staging_dir

    def import_pack(
        self,
        source_path: str,
        notes: str = "",
        actor: str = "api_user",
    ) -> ImportResult:
        src = Path(source_path)
        if not src.exists():
            return ImportResult(
                success=False, source_path=source_path,
                error=f"Source not found: {source_path}",
            )

        if src.is_dir():
            input_type = "folder"
        elif src.is_file() and src.suffix.lower() in ARCHIVE_EXTENSIONS:
            input_type = "rumipack" if src.suffix.lower() == ".rumipack" else "zip"
        else:
            return ImportResult(
                success=False, source_path=source_path,
                error=f"Unsupported input: {source_path}",
            )

        staging_id = uuid.uuid4().hex[:16]
        staging_dir = self._staging_root / staging_id
        payload_dir = staging_dir / "payload"

        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
            payload_dir.mkdir(parents=True, exist_ok=True)

            if input_type == "folder":
                self._import_folder(src, payload_dir)
            else:
                self._import_archive(src, payload_dir)

            detected, is_multi, det_warnings, det_build_cmds = \
                self._detect_packs(payload_dir)

            meta = {
                "staging_id": staging_id,
                "input_type": input_type,
                "source_path": str(src.resolve()),
                "detected_pack_ids": detected,
                "is_multi_pack": is_multi,
                "imported_at": self._now_ts(),
                "actor": actor,
                "notes": notes,
            }
            with open(staging_dir / "meta.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            self._audit("pack_import_completed", True, {
                "staging_id": staging_id,
                "input_type": input_type,
                "detected_pack_ids": detected,
                "is_multi_pack": is_multi,
                "actor": actor,
            })

            return ImportResult(
                success=True,
                staging_id=staging_id,
                staging_dir=str(staging_dir),
                input_type=input_type,
                source_path=source_path,
                detected_pack_ids=detected,
                is_multi_pack=is_multi,
                meta=meta,
                warnings=det_warnings,
                build_commands=det_build_cmds,
            )
        except Exception as e:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            self._audit("pack_import_failed", False, {
                "staging_id": staging_id,
                "error": str(e),
                "actor": actor,
            })
            return ImportResult(
                success=False,
                staging_id=staging_id,
                source_path=source_path,
                input_type=input_type,
                error=str(e),
            )

    def _import_folder(self, src: Path, payload_dir: Path) -> None:
        dest = payload_dir / src.name
        if dest.exists():
            shutil.rmtree(dest)
        self._check_symlinks_in_dir(src)
        shutil.copytree(str(src), str(dest), symlinks=False)

    def _check_symlinks_in_dir(self, directory: Path) -> None:
        for root, dirs, files in os.walk(directory, followlinks=False):
            for name in dirs + files:
                p = Path(root) / name
                if p.is_symlink():
                    raise ValueError(f"Symbolic link detected and rejected: {p}")

    def _import_archive(self, src: Path, payload_dir: Path) -> None:
        if not zipfile.is_zipfile(str(src)):
            raise ValueError(f"Not a valid zip archive: {src}")
        with zipfile.ZipFile(str(src), "r") as zf:
            self._validate_zip_entries(zf)
            self._validate_single_top_directory(zf)
            self._check_zip_bomb(zf)
            self._safe_extract(zf, payload_dir)

    def _validate_zip_entries(self, zf: zipfile.ZipFile) -> None:
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or name.startswith("\\"):
                raise ValueError(f"Absolute path in archive: {name}")
            parts = name.replace("\\", "/").split("/")
            if ".." in parts:
                raise ValueError(f"Path traversal detected in archive: {name}")
            if "\x00" in name:
                raise ValueError(f"Null byte in archive entry name: {repr(name)}")

    def _validate_single_top_directory(self, zf: zipfile.ZipFile) -> str:
        top_names = set()
        for info in zf.infolist():
            parts = info.filename.replace("\\", "/").split("/")
            if parts and parts[0]:
                top_names.add(parts[0])
        if len(top_names) == 0:
            raise ValueError("Archive is empty")
        if len(top_names) > 1:
            raise ValueError(
                f"Archive must have exactly one top-level directory, "
                f"found {len(top_names)}: {sorted(top_names)}"
            )
        return top_names.pop()

    def _check_zip_bomb(self, zf: zipfile.ZipFile) -> None:
        total_size = 0
        file_count = 0
        for info in zf.infolist():
            if not info.is_dir():
                file_count += 1
                total_size += info.file_size
        if file_count > MAX_FILES:
            raise ValueError(
                f"Archive contains too many files: {file_count} > {MAX_FILES}"
            )
        if total_size > MAX_TOTAL_SIZE:
            raise ValueError(
                f"Archive total size too large: {total_size} > {MAX_TOTAL_SIZE} bytes"
            )

    def _safe_extract(self, zf: zipfile.ZipFile, dest: Path) -> None:
        dest_resolved = dest.resolve()
        for info in zf.infolist():
            target = dest / info.filename
            target_resolved = target.resolve()
            try:
                target_resolved.relative_to(dest_resolved)
            except ValueError:
                raise ValueError(
                    f"Zip Slip detected: {info.filename} resolves outside destination"
                )
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(
                    f"Symbolic link in archive rejected: {info.filename}"
                )
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src_f, open(target, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)

    # ------------------------------------------------------------------
    # ecosystem.json validation (BUG-2-1)
    # ------------------------------------------------------------------
    _ECOSYSTEM_REQUIRED_FIELDS = ("pack_id", "version")

    def _validate_ecosystem_json(
        self, eco_path: Path,
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """Validate ecosystem.json schema.

        Required fields:
          - pack_id  (str)
          - version  (str)
          - metadata.name (str)

        Returns:
            (valid, error_message, parsed_data)
        """
        logger = logging.getLogger(__name__)
        try:
            with open(eco_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            msg = f"ecosystem.json is not valid JSON: {eco_path} ({e})"
            logger.warning(msg)
            return False, msg, None
        except OSError as e:
            msg = f"Failed to read ecosystem.json: {eco_path} ({e})"
            logger.warning(msg)
            return False, msg, None

        if not isinstance(data, dict):
            msg = f"ecosystem.json root must be an object: {eco_path}"
            logger.warning(msg)
            return False, msg, None

        # Check top-level required string fields
        for key in self._ECOSYSTEM_REQUIRED_FIELDS:
            if key not in data:
                msg = f"ecosystem.json missing required field '{key}': {eco_path}"
                logger.warning(msg)
                return False, msg, None
            if not isinstance(data[key], str):
                msg = (
                    f"ecosystem.json field '{key}' must be a string, "
                    f"got {type(data[key]).__name__}: {eco_path}"
                )
                logger.warning(msg)
                return False, msg, None

        # Check metadata.name
        metadata = data.get("metadata")
        if not isinstance(metadata, dict) or "name" not in metadata:
            msg = f"ecosystem.json missing required field 'metadata.name': {eco_path}"
            logger.warning(msg)
            return False, msg, None
        if not isinstance(metadata["name"], str):
            msg = (
                f"ecosystem.json field 'metadata.name' must be a string, "
                f"got {type(metadata['name']).__name__}: {eco_path}"
            )
            logger.warning(msg)
            return False, msg, None

        # --- desktop_app section (optional) ---
        pack_id = str(data.get("pack_id", "<unknown-pack>"))
        desktop_app = data.get("desktop_app")
        if desktop_app is not None:
            if not isinstance(desktop_app, dict):
                msg = f"[{pack_id}] 'desktop_app' must be a dict"
                return False, msg, None
            da_command = desktop_app.get("command")
            if da_command is None or not isinstance(da_command, str):
                msg = f"[{pack_id}] 'desktop_app.command' is required and must be a string"
                return False, msg, None
            if not da_command.strip():
                msg = f"[{pack_id}] 'desktop_app.command' must not be empty"
                return False, msg, None
            for opt_key, opt_type, opt_label in [
                ("working_dir", str, "string"),
                ("env", dict, "dict"),
                ("capabilities", list, "list"),
                ("window", dict, "dict"),
                ("platforms", list, "list"),
            ]:
                opt_val = desktop_app.get(opt_key)
                if opt_val is not None and not isinstance(opt_val, opt_type):
                    msg = f"[{pack_id}] 'desktop_app.{opt_key}' must be a {opt_label}"
                    return False, msg, None

        return True, None, data

    def _detect_packs(
        self, payload_dir: Path,
    ) -> Tuple[List[str], bool, List[str], List[Dict[str, Any]]]:
        """Detect packs in payload directory.

        Returns:
            (pack_ids, is_multi_pack, warnings, build_commands)
        """
        logger = logging.getLogger(__name__)
        warnings: List[str] = []
        build_commands: List[Dict[str, Any]] = []

        top_dirs = list(finite_children(payload_dir, directories_only=True))
        if len(top_dirs) != 1:
            raise ValueError(
                f"Expected exactly one top-level directory in payload, "
                f"found {len(top_dirs)}"
            )
        top_dir = top_dirs[0]

        # --- helper: validate + extract build command ---
        def _check_and_collect(eco_path: Path, pack_id: str) -> bool:
            """Validate ecosystem.json and collect build commands.

            Returns True if validation passed, False otherwise.
            """
            valid, err, data = self._validate_ecosystem_json(eco_path)
            if not valid or data is None:
                warnings.append(
                    f"Pack '{pack_id}' skipped: {err}"
                )
                return False
            # BUG-1-1: record build.command if present
            runtime = data.get("runtime")
            if isinstance(runtime, dict):
                build = runtime.get("build")
                if isinstance(build, dict):
                    command = build.get("command")
                    if isinstance(command, str) and command.strip():
                        build_commands.append({
                            "pack_id": pack_id,
                            "command": command,
                        })
                        logger.info(
                            "Build command found for pack '%s': %s. "
                            "Will be executed after approval.",
                            pack_id, command,
                        )
            return True

        packs_dir = top_dir / "packs"
        if packs_dir.is_dir():
            pack_ids = []
            for d in finite_children(packs_dir, directories_only=True):
                if d.is_dir() and (d / "ecosystem.json").exists():
                    if _check_and_collect(d / "ecosystem.json", d.name):
                        pack_ids.append(d.name)
            if pack_ids:
                return pack_ids, True, warnings, build_commands

        if (top_dir / "ecosystem.json").exists():
            if _check_and_collect(top_dir / "ecosystem.json", top_dir.name):
                return [top_dir.name], False, warnings, build_commands
            # validation failed -> warning already recorded, fall through

        for d in finite_children(top_dir, directories_only=True):
            if d.is_dir() and (d / "ecosystem.json").exists():
                if _check_and_collect(d / "ecosystem.json", top_dir.name):
                    return [top_dir.name], False, warnings, build_commands
                # validation failed -> warning already recorded, continue

        raise ValueError(
            "No ecosystem.json found in payload. "
            "Expected either <top>/ecosystem.json or "
            "<top>/packs/<pack_id>/ecosystem.json"
        )

    def get_staging_meta(self, staging_id: str) -> Optional[Dict[str, Any]]:
        staging_dir = self._staging_dir_for(staging_id)
        if staging_dir is None:
            return None
        meta_path = staging_dir / "meta.json"
        if not meta_path.exists():
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def list_stagings(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        if not self._staging_root.exists():
            return results
        for d in finite_children(self._staging_root, directories_only=True):
            if d.is_dir() and is_safe_staging_id(d.name):
                meta = self.get_staging_meta(d.name)
                if meta:
                    results.append(meta)
        return results

    def cleanup_staging(self, staging_id: str) -> bool:
        staging_dir = self._staging_dir_for(staging_id)
        if staging_dir is None:
            return False
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
            return True
        return False

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


_global_importer: Optional[PackImporter] = None
_importer_lock = threading.Lock()


def get_pack_importer() -> PackImporter:
    global _global_importer
    if _global_importer is None:
        with _importer_lock:
            if _global_importer is None:
                _global_importer = PackImporter()
    return _global_importer


def reset_pack_importer(staging_root: str | None = None) -> PackImporter:
    global _global_importer
    with _importer_lock:
        _global_importer = PackImporter(staging_root)
    return _global_importer
