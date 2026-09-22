from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Optional

from core_runtime.validation import check_path_within, validate_pack_id

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[4]
PACK_REQUESTS_ROOT = BASE_DIR / "user_data" / "packs" / "defaultspack" / "pack_requests"
PACK_BACKUP_ROOT = BASE_DIR / "user_data" / "packs" / "defaultspack" / "pack_backups"
PACK_STAGING_ROOT = BASE_DIR / "user_data" / "pack_staging"
ECOSYSTEM_DIR = BASE_DIR / "ecosystem"


class PatchMode(str, Enum):
    REQUEST_EXTENSION = "request_extension"
    FORCED_PATCH = "forced_patch"


PACK_REQUEST_MODES = frozenset(mode.value for mode in PatchMode)
_STAGING_ID_RE = re.compile(r"^[a-fA-F0-9]{16}$")
_REQUEST_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def _is_safe_staging_id(value: str) -> bool:
    try:
        from core_runtime.validation import is_safe_staging_id

        return is_safe_staging_id(value)
    except Exception:
        return bool(value and _STAGING_ID_RE.fullmatch(str(value)))


def _is_safe_request_id(value: str) -> bool:
    return bool(isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value))


@dataclass
class ExtensionRequest:
    request_id: str
    mode: PatchMode | str
    pack_id: str
    target_pack_id: str
    summary: str
    status: str = "pending"
    created_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    staging_id: str = ""
    slot: str = "default"
    fullscreen: bool = False
    exclusive: bool = False
    changed_paths: List[str] = field(default_factory=list)
    detected_pack_ids: List[str] = field(default_factory=list)
    staging_meta_sha256: str = ""
    selection_required: bool = False
    selection_candidates: List[Dict[str, Any]] = field(default_factory=list)
    applied_pack_ids: List[str] = field(default_factory=list)
    backup_paths: Dict[str, str] = field(default_factory=dict)
    decision_notes: str = ""
    applied_at: Optional[str] = None
    reviewed_at: Optional[str] = None
    rejected_at: Optional[str] = None
    rolled_back_at: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        mode = data.get("mode")
        data["mode"] = mode.value if isinstance(mode, PatchMode) else mode
        data["actor"] = data.pop("pack_id")
        data["notes"] = data.pop("summary")
        return data


class ExtensionManager:
    def __init__(
        self,
        requests_root: Path | None = None,
        ecosystem_dir: Path | None = None,
        backup_root: Path | None = None,
        staging_root: Path | None = None,
    ) -> None:
        self.requests_root = Path(requests_root or PACK_REQUESTS_ROOT)
        self.ecosystem_dir = Path(ecosystem_dir or ECOSYSTEM_DIR)
        self.backup_root = Path(backup_root or PACK_BACKUP_ROOT)
        self.staging_root = Path(staging_root or PACK_STAGING_ROOT)
        self._requests: Dict[str, ExtensionRequest] = {}
        self._audit: List[Dict[str, Any]] = []

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _request_path(self, request_id: str) -> Path:
        if not _is_safe_request_id(request_id):
            raise ValueError("invalid request_id")
        path = self.requests_root / f"{request_id}.json"
        path_ok, error = check_path_within(path, self.requests_root)
        if not path_ok:
            raise ValueError(error or "invalid request path")
        return path

    def _write_request(self, request: ExtensionRequest) -> None:
        self.requests_root.mkdir(parents=True, exist_ok=True)
        path = self._request_path(request.request_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(request.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for attempt in range(5):
            try:
                tmp.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    break
                time.sleep(0.05)
        self._requests[request.request_id] = request

    @staticmethod
    def _from_dict(data: Dict[str, Any]) -> ExtensionRequest:
        data = dict(data)
        data["pack_id"] = data.pop("actor", data.get("pack_id", "defaultspack"))
        data["summary"] = data.pop("notes", data.get("summary", ""))
        mode = data.get("mode", PatchMode.REQUEST_EXTENSION.value)
        data["mode"] = PatchMode(mode) if mode in PACK_REQUEST_MODES else mode
        allowed = {item.name for item in fields(ExtensionRequest)}
        data = {key: value for key, value in data.items() if key in allowed}
        return ExtensionRequest(**data)

    def _read_request(self, request_id: str) -> Optional[ExtensionRequest]:
        if not _is_safe_request_id(request_id):
            return None
        if request_id in self._requests:
            return self._requests[request_id]
        try:
            path = self._request_path(request_id)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse pack request %s: %s", path, exc)
            return None
        if not isinstance(data, dict):
            return None
        try:
            request = self._from_dict(data)
        except (TypeError, ValueError) as exc:
            logger.warning("Invalid pack request %s: %s", path, exc)
            return None
        if request.request_id != request_id or not _is_safe_request_id(request.request_id):
            logger.warning("Pack request id mismatch in %s", path)
            return None
        self._requests[request.request_id] = request
        return request

    def _load_all(self) -> List[ExtensionRequest]:
        items = list(self._requests.values())
        seen = {item.request_id for item in items}
        if self.requests_root.is_dir():
            for path in sorted(self.requests_root.glob("*.json")):
                if path.stem in seen:
                    continue
                req = self._read_request(path.stem)
                if req is not None:
                    items.append(req)
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items

    @staticmethod
    def _is_active_request(request: ExtensionRequest) -> bool:
        return request.status in {"pending", "applied"}

    def _evaluate_request_policy(
        self,
        *,
        target_pack_id: str,
        slot: str,
        fullscreen: bool,
        exclusive: bool,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        conflicts: List[Dict[str, Any]] = []
        selection_candidates: List[Dict[str, Any]] = []
        normalized_slot = slot or "default"
        for existing in self._load_all():
            if not self._is_active_request(existing):
                continue
            same_slot = existing.slot == normalized_slot
            fullscreen_conflict = (
                fullscreen or existing.fullscreen or (normalized_slot == "fullscreen" and same_slot)
            )
            if fullscreen_conflict:
                conflicts.append({
                    "request_id": existing.request_id,
                    "target_pack_id": existing.target_pack_id,
                    "slot": existing.slot,
                    "status": existing.status,
                    "reason": "fullscreen_exclusive",
                })
                continue
            if same_slot and (exclusive or existing.exclusive):
                conflicts.append({
                    "request_id": existing.request_id,
                    "target_pack_id": existing.target_pack_id,
                    "slot": existing.slot,
                    "status": existing.status,
                    "reason": "slot_exclusive",
                })
                continue
            if same_slot and existing.target_pack_id != target_pack_id:
                selection_candidates.append({
                    "request_id": existing.request_id,
                    "target_pack_id": existing.target_pack_id,
                    "slot": existing.slot,
                    "status": existing.status,
                })
        return conflicts, selection_candidates

    def create_request(
        self,
        mode: PatchMode | str,
        pack_id: str = "defaultspack",
        target_pack_id: str = "",
        summary: str = "",
        staging_id: str = "",
        slot: str = "default",
        fullscreen: bool = False,
        exclusive: bool = False,
        detected_pack_ids: Optional[List[str]] = None,
        changed_paths: Optional[List[str]] = None,
        staging_meta_sha256: str = "",
    ) -> ExtensionRequest:
        mode_value = mode.value if isinstance(mode, PatchMode) else str(mode)
        if mode_value not in PACK_REQUEST_MODES:
            raise ValueError(f"Unsupported mode: {mode_value}")
        normalized_slot = slot or "default"
        normalized_fullscreen = bool(fullscreen or normalized_slot == "fullscreen")
        normalized_exclusive = bool(exclusive or normalized_fullscreen)
        conflicts, selection_candidates = self._evaluate_request_policy(
            target_pack_id=target_pack_id,
            slot=normalized_slot,
            fullscreen=normalized_fullscreen,
            exclusive=normalized_exclusive,
        )
        if conflicts:
            raise RuntimeError("pack modification request conflicts with active request(s)")
        request_id = f"{mode_value[:3]}_{staging_id}" if staging_id else f"{mode_value[:3]}_{self._now_ts().replace(':', '').replace('.', '')}"
        request = ExtensionRequest(
            request_id=request_id,
            mode=PatchMode(mode_value),
            pack_id=pack_id,
            target_pack_id=target_pack_id,
            summary=summary,
            staging_id=staging_id,
            slot=normalized_slot,
            fullscreen=normalized_fullscreen,
            exclusive=normalized_exclusive,
            changed_paths=list(changed_paths or []),
            detected_pack_ids=list(detected_pack_ids or []),
            staging_meta_sha256=staging_meta_sha256,
            selection_required=bool(selection_candidates),
            selection_candidates=selection_candidates,
        )
        self._audit.append({"action": "create", "request_id": request.request_id, "mode": mode_value})
        self._write_request(request)
        return request

    def _staging_dir_for(self, staging_id: str) -> Optional[Path]:
        if not _is_safe_staging_id(staging_id):
            return None
        staging_dir = self.staging_root / str(staging_id)
        path_ok, _ = check_path_within(staging_dir, self.staging_root)
        if not path_ok:
            return None
        return staging_dir

    def _load_staging_meta(self, staging_id: str) -> tuple[Dict[str, Any], str]:
        staging_dir = self._staging_dir_for(staging_id)
        if staging_dir is None:
            raise ValueError("invalid staging_id")
        meta_path = staging_dir / "meta.json"
        path_ok, error = check_path_within(meta_path, staging_dir)
        if not path_ok:
            raise ValueError(error or "invalid staging metadata path")
        if not meta_path.is_file():
            raise FileNotFoundError("staging meta not found")
        raw = meta_path.read_bytes()
        try:
            meta = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid staging meta: {exc}") from exc
        if not isinstance(meta, dict):
            raise ValueError("invalid staging meta")
        return meta, sha256(raw).hexdigest()

    @staticmethod
    def _normalize_detected_pack_ids(meta: Dict[str, Any]) -> List[str]:
        detected = meta.get("detected_pack_ids")
        if not isinstance(detected, list) or not detected:
            raise ValueError("staging meta missing detected_pack_ids")
        normalized: List[str] = []
        for pack_id in detected:
            if not isinstance(pack_id, str) or not validate_pack_id(pack_id):
                raise ValueError(f"invalid detected pack_id: {pack_id}")
            normalized.append(pack_id)
        return normalized

    @staticmethod
    def _normalize_changed_paths(meta: Dict[str, Any]) -> List[str]:
        changed = meta.get("changed_paths")
        proposal_info = meta.get("proposal_info")
        if changed is None and isinstance(proposal_info, dict):
            changed = proposal_info.get("changed_paths")
        if changed is None:
            return []
        if not isinstance(changed, list):
            raise ValueError("invalid changed_paths in staging meta")
        normalized: List[str] = []
        for item in changed:
            if not isinstance(item, str):
                raise ValueError("invalid changed path in staging meta")
            path = item.replace("\\", "/").strip()
            parts = [part for part in path.split("/") if part]
            if (
                not path
                or len(path) > 512
                or path.startswith("/")
                or "\x00" in path
                or any(part in {".", ".."} for part in parts)
            ):
                raise ValueError(f"invalid changed path in staging meta: {item}")
            normalized.append(path)
        return normalized

    def _snapshot_staging_meta(self, staging_id: str) -> Dict[str, Any]:
        meta, digest = self._load_staging_meta(staging_id)
        return {
            "detected_pack_ids": self._normalize_detected_pack_ids(meta),
            "changed_paths": self._normalize_changed_paths(meta),
            "staging_meta_sha256": digest,
        }

    @staticmethod
    def _metadata_mismatch_response() -> Dict[str, Any]:
        return {"error": "staging metadata changed since request creation", "status_code": 409}

    def create_pack_request(
        self,
        *,
        mode: str,
        staging_id: str,
        actor: str = "defaultspack",
        notes: str = "",
        target_pack_id: str = "",
        slot: str = "default",
        fullscreen: bool = False,
        exclusive: bool = False,
    ) -> Dict[str, Any]:
        if not staging_id:
            return {"error": "staging_id is required", "status_code": 400}
        if not _is_safe_staging_id(staging_id):
            return {"error": "invalid staging_id", "status_code": 400}
        try:
            staging_snapshot = self._snapshot_staging_meta(staging_id)
        except FileNotFoundError:
            return {"error": "staging meta not found", "status_code": 404}
        except ValueError as exc:
            return {"error": str(exc), "status_code": 400}
        detected_pack_ids = staging_snapshot["detected_pack_ids"]
        if target_pack_id:
            if not validate_pack_id(target_pack_id):
                return {"error": "invalid target_pack_id", "status_code": 400}
            if target_pack_id not in detected_pack_ids:
                return {"error": "target_pack_id does not match staging metadata", "status_code": 400}
        else:
            target_pack_id = detected_pack_ids[0]
        try:
            request = self.create_request(
                mode,
                pack_id=actor,
                target_pack_id=target_pack_id,
                summary=notes,
                staging_id=staging_id,
                slot=slot,
                fullscreen=fullscreen,
                exclusive=exclusive,
                detected_pack_ids=detected_pack_ids,
                changed_paths=staging_snapshot["changed_paths"],
                staging_meta_sha256=staging_snapshot["staging_meta_sha256"],
            )
        except ValueError as exc:
            return {"error": str(exc), "status_code": 400}
        except RuntimeError as exc:
            return {"error": str(exc), "status_code": 409}
        result = request.to_dict()
        try:
            from .authority_bridge import ensure_authority_request_for_pack_request

            authority = ensure_authority_request_for_pack_request(request)
            if authority.get("request_id"):
                result["authority_request_id"] = authority.get("request_id")
            result["authority_request"] = authority
        except Exception as exc:
            result["authority_request"] = {"success": False, "error": str(exc)}
        return result

    def list_pending(self) -> List[ExtensionRequest]:
        return [request for request in self._load_all() if request.status == "pending"]

    def list_requests(self, status_filter: str = "all") -> Dict[str, Any]:
        items = self._load_all()
        if status_filter != "all":
            items = [item for item in items if item.status == status_filter]
        return {
            "requests": [item.to_dict() for item in items],
            "count": len(items),
            "status_filter": status_filter,
        }

    def get_request(self, request_id: str) -> Dict[str, Any]:
        if not _is_safe_request_id(request_id):
            return {"error": "invalid request_id", "status_code": 400}
        request = self._read_request(request_id)
        if request is None:
            return {"error": f"Unknown request: {request_id}", "status_code": 404}
        return request.to_dict()

    def approve(self, request_id: str) -> Optional[ExtensionRequest]:
        request = self._read_request(request_id)
        if request is None:
            return None
        request.status = "approved"
        self._audit.append({"action": "approve", "request_id": request_id})
        self._write_request(request)
        return request

    def reject(self, request_id: str) -> Optional[ExtensionRequest]:
        request = self._read_request(request_id)
        if request is None:
            return None
        request.status = "rejected"
        request.rejected_at = self._now_ts()
        self._audit.append({"action": "reject", "request_id": request_id})
        self._write_request(request)
        return request

    def approve_request(
        self,
        request_id: str,
        reviewer: str = "user",
        decision_notes: str = "",
    ) -> Dict[str, Any]:
        if not _is_safe_request_id(request_id):
            return {"error": "invalid request_id", "status_code": 400}
        request = self._read_request(request_id)
        if request is None:
            return {"error": f"Unknown request: {request_id}", "status_code": 404}
        if request.status != "pending":
            return {"error": f"Request is not pending: {request.status}", "status_code": 409}
        if not _is_safe_staging_id(request.staging_id):
            return {"error": "invalid staging_id", "status_code": 400}
        if not request.detected_pack_ids or not request.staging_meta_sha256:
            return {"error": "request is missing staging metadata snapshot", "status_code": 409}
        try:
            staging_snapshot = self._snapshot_staging_meta(request.staging_id)
        except FileNotFoundError:
            return {"error": "staging meta not found", "status_code": 404}
        except ValueError as exc:
            return {"error": str(exc), "status_code": 400}
        if (
            request.detected_pack_ids != staging_snapshot["detected_pack_ids"]
            or request.changed_paths != staging_snapshot["changed_paths"]
            or request.staging_meta_sha256 != staging_snapshot["staging_meta_sha256"]
        ):
            return self._metadata_mismatch_response()

        try:
            from core_runtime.pack_applier import PackApplier

            apply_result = PackApplier(
                ecosystem_dir=str(self.ecosystem_dir),
                backup_root=str(self.backup_root),
                staging_root=str(self.staging_root),
            ).apply(request.staging_id, mode="replace", actor=reviewer)
        except Exception as exc:
            request.error = str(exc)
            request.reviewed_at = self._now_ts()
            self._write_request(request)
            return {"error": "pack apply failed", "detail": str(exc), "status_code": 500}

        if not getattr(apply_result, "success", False):
            request.error = getattr(apply_result, "error", None) or "pack apply failed"
            request.reviewed_at = self._now_ts()
            self._write_request(request)
            payload = apply_result.to_dict() if hasattr(apply_result, "to_dict") else {"error": request.error}
            payload.setdefault("status_code", 500)
            return payload

        request.status = "applied"
        request.reviewed_at = self._now_ts()
        request.applied_at = request.reviewed_at
        request.decision_notes = decision_notes
        request.applied_pack_ids = list(getattr(apply_result, "applied_pack_ids", []) or [])
        request.backup_paths = dict(getattr(apply_result, "backup_paths", {}) or {})
        request.error = None
        self._write_request(request)
        self._audit.append({
            "action": "approve_request",
            "request_id": request_id,
            "reviewer": reviewer,
            "applied_pack_ids": request.applied_pack_ids,
        })
        return request.to_dict()

    def reject_request(
        self,
        request_id: str,
        reviewer: str = "user",
        reason: str = "",
    ) -> Dict[str, Any]:
        if not _is_safe_request_id(request_id):
            return {"error": "invalid request_id", "status_code": 400}
        request = self._read_request(request_id)
        if request is None:
            return {"error": f"Unknown request: {request_id}", "status_code": 404}
        if request.status != "pending":
            return {"error": f"Request is not pending: {request.status}", "status_code": 409}
        request.status = "rejected"
        request.reviewed_at = self._now_ts()
        request.rejected_at = request.reviewed_at
        request.decision_notes = reason
        self._write_request(request)
        self._audit.append({"action": "reject_request", "request_id": request_id, "reviewer": reviewer})
        return request.to_dict()

    def rollback_request(
        self,
        request_id: str,
        reviewer: str = "user",
        notes: str = "",
    ) -> Dict[str, Any]:
        if not _is_safe_request_id(request_id):
            return {"error": "invalid request_id", "status_code": 400}
        request = self._read_request(request_id)
        if request is None:
            return {"error": f"Unknown request: {request_id}", "status_code": 404}
        if request.status != "applied":
            return {"error": f"Request is not applied: {request.status}", "status_code": 409}
        if not isinstance(request.backup_paths, dict):
            return {"error": "invalid backup_paths", "status_code": 400}
        if not isinstance(request.applied_pack_ids, list):
            return {"error": "invalid applied_pack_ids", "status_code": 400}

        backup_root_resolved = self.backup_root.resolve()
        restored = []
        removed = []
        for pack_id, backup_path in request.backup_paths.items():
            if not isinstance(pack_id, str) or not validate_pack_id(pack_id):
                return {"error": f"Invalid pack_id for rollback: {pack_id}", "status_code": 400}
            source = Path(backup_path).resolve()
            try:
                source.relative_to(backup_root_resolved)
            except ValueError:
                return {"error": f"Invalid backup path for {pack_id}", "status_code": 400}
            if not source.is_dir():
                return {"error": f"Backup not found for {pack_id}", "status_code": 404}
            dest = self.ecosystem_dir / pack_id
            dest_ok, dest_error = check_path_within(dest, self.ecosystem_dir)
            if not dest_ok:
                return {"error": dest_error or f"Invalid destination for {pack_id}", "status_code": 400}
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(str(source), str(dest), symlinks=False)
            restored.append(pack_id)

        for pack_id in request.applied_pack_ids:
            if not isinstance(pack_id, str) or not validate_pack_id(pack_id):
                return {"error": f"Invalid pack_id for rollback: {pack_id}", "status_code": 400}
            if pack_id in request.backup_paths:
                continue
            dest = self.ecosystem_dir / pack_id
            dest_ok, dest_error = check_path_within(dest, self.ecosystem_dir)
            if not dest_ok:
                return {"error": dest_error or f"Invalid destination for {pack_id}", "status_code": 400}
            if dest.exists():
                shutil.rmtree(dest)
            removed.append(pack_id)

        request.status = "rolled_back"
        request.rolled_back_at = self._now_ts()
        request.reviewed_at = request.rolled_back_at
        request.decision_notes = notes or request.decision_notes
        self._write_request(request)
        self._audit.append({
            "action": "rollback_request",
            "request_id": request_id,
            "reviewer": reviewer,
            "restored": restored,
            "removed": removed,
        })
        return request.to_dict()

    def get_audit_log(self) -> List[Dict[str, Any]]:
        return list(self._audit)


_global_extension_manager: ExtensionManager | None = None


def get_extension_manager() -> ExtensionManager:
    global _global_extension_manager
    if _global_extension_manager is None:
        _global_extension_manager = ExtensionManager()
    return _global_extension_manager
