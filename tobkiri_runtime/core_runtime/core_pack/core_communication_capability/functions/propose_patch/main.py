"""
pack.update.propose_patch - Built-in Capability Handler

Pack A が Pack B のファイル修正案を staging として生成する。
自動 apply は絶対に行わない。ユーザーが /api/packs/apply で適用する前提。

安全要件:
- allowed_target_packs / allowed_path_globs で制限
- sha256_before 検証
- 自動 apply 禁止 (staging 生成のみ)
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional

_SAFE_ID_RE = re.compile("^[a-zA-Z0-9_.-]+$")


def execute(context: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    principal_id = context.get("principal_id", "")
    grant_config = context.get("grant_config", {})

    target_pack_id = args.get("target_pack_id", "")
    changes = args.get("changes", [])
    notes = args.get("notes", "")
    request_id = args.get("request_id", uuid.uuid4().hex[:12])

    if not target_pack_id or not isinstance(target_pack_id, str):
        return _error("Missing or invalid target_pack_id", "validation_error")
    if not changes or not isinstance(changes, list):
        return _error("Missing or empty changes array", "validation_error")
    if not _safe_pack_id(target_pack_id):
        return _error("Invalid target_pack_id (path traversal)", "validation_error")

    allowed_targets = grant_config.get("allowed_target_packs", [])
    if allowed_targets and target_pack_id not in allowed_targets:
        return _error("Target pack not in allowed_target_packs", "grant_denied")

    allowed_path_globs = grant_config.get("allowed_path_globs", [])

    pack_dir = _find_pack_dir(target_pack_id)
    if pack_dir is None:
        return _error("Pack '" + target_pack_id + "' not found", "not_found")

    max_proposals = grant_config.get("max_proposals_per_target", 0)
    expires_at = grant_config.get("expires_at_epoch", 0)

    if max_proposals > 0 or expires_at > 0:
        try:
            from core_runtime.capability_usage_store import get_capability_usage_store
            store = get_capability_usage_store()
            result = store.check_and_consume(
                principal_id=principal_id,
                permission_id="pack.update.propose_patch",
                scope_key=target_pack_id,
                max_count=max_proposals,
                expires_at_epoch=expires_at,
            )
            if not result.allowed:
                return _error("Usage limit: " + str(result.reason), "usage_limit_exceeded")
        except Exception as e:
            return _error("Usage store error: " + str(e), "internal_error")

    validated_changes: List[Dict[str, Any]] = []
    for i, change in enumerate(changes):
        file_path = change.get("file_path", "")
        content = change.get("content")
        sha256_before = change.get("sha256_before")

        validated_path_error = _validate_relative_file_path(file_path)
        if validated_path_error:
            return _error("changes[" + str(i) + "]: " + validated_path_error, "validation_error")

        if allowed_path_globs:
            if not any(fnmatch.fnmatch(file_path, g) for g in allowed_path_globs):
                return _error(
                    "changes[" + str(i) + "]: file_path not in allowed_path_globs",
                    "grant_denied",
                )
        source_file = pack_dir / file_path
        if sha256_before:
            if source_file.exists():
                actual_hash = _compute_sha256(source_file)
                if actual_hash != sha256_before:
                    return _error(
                        "changes[" + str(i) + "]: sha256_before mismatch", "hash_mismatch"
                    )
            else:
                return _error(
                    "changes[" + str(i) + "]: file not found for sha256 check", "not_found"
                )

        validated_changes.append({
            "file_path": file_path,
            "content": content,
            "sha256_before": sha256_before,
        })

    staging_id = "propose_" + uuid.uuid4().hex[:12]
    staging_dir = Path("user_data") / "pack_staging" / staging_id

    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        payload_dir = staging_dir / "payload" / target_pack_id
        shutil.copytree(str(pack_dir), str(payload_dir))

        changed_paths: List[str] = []
        total_bytes = 0
        resolved_payload_dir = payload_dir.resolve()
        for change in validated_changes:
            fp = change["file_path"]
            target_file = _safe_payload_target(resolved_payload_dir, fp)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            content = change["content"]
            if isinstance(content, (dict, list)):
                content_str = json.dumps(content, ensure_ascii=False, indent=2)
            else:
                content_str = str(content)
            target_file.write_text(content_str, encoding="utf-8")
            changed_paths.append(fp)
            total_bytes += len(content_str.encode("utf-8"))

        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        meta = {
            "staging_id": staging_id,
            "input_type": "propose_patch",
            "source_path": "capability:" + principal_id,
            "imported_at": ts,
            "actor": principal_id,
            "notes": notes or ("Proposed by " + principal_id),
            "detected_pack_ids": [target_pack_id],
            "is_multi_pack": False,
            "proposal_info": {
                "from_pack_id": principal_id,
                "target_pack_id": target_pack_id,
                "changed_paths": changed_paths,
                "request_id": request_id,
            },
        }
        meta_file = staging_dir / "meta.json"
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    except Exception as e:
        if staging_dir.exists():
            shutil.rmtree(str(staging_dir), ignore_errors=True)
        return _error("Staging generation failed: " + str(e), "staging_error")

    _audit_propose_patch(
        principal_id=principal_id,
        target_pack_id=target_pack_id,
        staging_id=staging_id,
        changed_paths=changed_paths,
        total_bytes=total_bytes,
        request_id=request_id,
    )

    return {
        "success": True,
        "staging_id": staging_id,
        "changed_paths": changed_paths,
        "total_bytes": total_bytes,
    }


def _error(message: str, error_type: str) -> Dict[str, Any]:
    return {"success": False, "error": message, "error_type": error_type}


def _validate_relative_file_path(file_path: Any) -> Optional[str]:
    if not file_path or not isinstance(file_path, str):
        return "missing file_path"
    if Path(file_path).is_absolute() or PureWindowsPath(file_path).is_absolute():
        return "absolute file_path is not allowed"
    if ".." in file_path:
        return "path traversal in file_path"
    return None


def _safe_payload_target(payload_dir: Path, file_path: str) -> Path:
    resolved_payload_dir = payload_dir.resolve(strict=False)
    target_file = (resolved_payload_dir / file_path).resolve(strict=False)
    try:
        target_file.relative_to(resolved_payload_dir)
    except ValueError as exc:
        raise ValueError("file_path escapes staging payload directory") from exc
    return target_file


def _safe_pack_id(pack_id: str) -> bool:
    if ".." in pack_id or "/" in pack_id:
        return False
    if pack_id.startswith("."):
        return False
    return bool(_SAFE_ID_RE.match(pack_id))


def _find_pack_dir(pack_id: str) -> Optional[Path]:
    try:
        from core_runtime.paths import ECOSYSTEM_DIR, discover_pack_locations
        locations = discover_pack_locations(str(ECOSYSTEM_DIR))
        for loc in locations:
            if loc.pack_id == pack_id:
                return loc.pack_subdir
    except Exception:
        pass
    for candidate in [
        Path("ecosystem") / pack_id,
        Path("ecosystem") / "packs" / pack_id,
    ]:
        if candidate.is_dir():
            return candidate
    return None


def _compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _audit_propose_patch(**kwargs: Any) -> None:
    try:
        from core_runtime.audit_logger import get_audit_logger
        audit = get_audit_logger()
        audit.log_permission_event(
            pack_id=kwargs.get("principal_id", ""),
            permission_type="capability",
            action="propose_patch",
            success=True,
            details={
                "target_pack_id": kwargs.get("target_pack_id"),
                "staging_id": kwargs.get("staging_id"),
                "changed_paths": kwargs.get("changed_paths"),
                "total_bytes": kwargs.get("total_bytes"),
                "request_id": kwargs.get("request_id"),
            },
        )
    except Exception:
        pass
