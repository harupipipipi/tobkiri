"""
capability_installer.py - Capability Handler 候補導入フロー

Wave 13 T-048: データクラス・定数は capability_models.py に分離。
後方互換のため全シンボルを re-export する。

ecosystem に Pack が同梱した候補 capability handler を検出し、
承認ワークフロー（pending → approve/reject/block）を経て
user_data/capabilities/handlers/ へコピー（実働化）する。

設計原則:
- ecosystem は候補（配布物）、user_data は実働（承認済み）
- candidate_key = "{pack_id}:{slug}:{handler_id}:{sha256}" で同一性管理
- approve 時に Trust 登録 + コピー + Registry/Executor reload を同時実行
- reject 3回で blocked（サイレント抑制）
- cooldown 1時間で再通知抑制
- 全操作を requests.jsonl + AuditLogger に記録
- スレッドセーフ（RLock）

Wave 17-B 変更:
- index.json / blocked.json に HMAC 署名の生成・検証を追加
- 後方互換: 署名なし旧ファイルは WARNING のみ（RUMI_REQUIRE_HMAC=1 で拒否）
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from .hmac_key_manager import generate_or_load_signing_key, compute_data_hmac, verify_data_hmac

from .validation import (
    validate_slug as _v_validate_slug,
    validate_entrypoint as _v_validate_entrypoint,
    check_no_symlinks as _v_check_no_symlinks,
    check_path_within as _v_check_path_within,
)

# --- Wave 13 T-048: import from capability_models (+ re-export) ---
from .capability_models import (          # noqa: F401 — re-export
    CandidateStatus,
    CandidateInfo,
    IndexItem,
    ScanResult,
    ApproveResult,
    RejectResult,
    UnblockResult,
    DEFAULT_ECOSYSTEM_DIR,
    CANDIDATE_SUBPATH,
    REQUESTS_DIR,
    INDEX_FILE,
    BLOCKED_FILE,
    REQUESTS_LOG_FILE,
    HANDLERS_DEST_DIR,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_REJECT_THRESHOLD,
    _EXCLUDED_PACK_DIRS,
    SLUG_PATTERN as _SLUG_PATTERN,
)

logger = logging.getLogger(__name__)
_REQUIRE_HMAC_DEFAULT = "1"


# ======================================================================
# Lazy import helpers (テストで patch 可能にするためモジュールレベルに配置)
# ======================================================================

def _get_trust_store():
    """遅延 import: CapabilityTrustStore"""
    from .capability_trust_store import get_capability_trust_store
    return get_capability_trust_store()


def _get_executor():
    """遅延 import: CapabilityExecutor"""
    from .capability_executor import get_capability_executor
    return get_capability_executor()


# ======================================================================
# CapabilityInstaller
# ======================================================================

class CapabilityInstaller:
    """
    Capability Handler 候補導入フロー管理

    - 候補スキャン（ecosystem 走査）
    - 状態管理（index.json + blocked.json + requests.jsonl）
    - approve（Trust登録 + コピー + Registry reload）
    - reject / block / unblock
    """

    def __init__(
        self,
        requests_dir: Optional[str] = None,
        handlers_dest_dir: Optional[str] = None,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        reject_threshold: int = DEFAULT_REJECT_THRESHOLD,
    ):
        self._requests_dir = Path(requests_dir or REQUESTS_DIR)
        self._handlers_dest_dir = Path(handlers_dest_dir or HANDLERS_DEST_DIR)
        self._cooldown_seconds = cooldown_seconds
        self._reject_threshold = reject_threshold
        self._lock = threading.RLock()

        # In-memory state
        self._index_items: Dict[str, IndexItem] = {}
        self._blocked: Dict[str, Dict[str, Any]] = {}

        # HMAC 署名用の秘密鍵をロード
        self._secret_key = generate_or_load_signing_key(
            self._requests_dir / ".secret_key",
            env_var="RUMI_HMAC_SECRET",
        )

        # Load persisted state
        self._ensure_dirs()
        self._load_index()
        self._load_blocked()

    # ------------------------------------------------------------------
    # Timestamp helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _now_dt() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse_ts(ts_str: str) -> Optional[datetime]:
        """ISO 8601 タイムスタンプをパース"""
        if not ts_str:
            return None
        try:
            if ts_str.endswith("Z"):
                ts_str = ts_str[:-1] + "+00:00"
            return datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Directory / File helpers
    # ------------------------------------------------------------------

    def _ensure_dirs(self) -> None:
        self._requests_dir.mkdir(parents=True, exist_ok=True)
        self._handlers_dest_dir.mkdir(parents=True, exist_ok=True)

    def _index_path(self) -> Path:
        return self._requests_dir / INDEX_FILE

    def _blocked_path(self) -> Path:
        return self._requests_dir / BLOCKED_FILE

    def _log_path(self) -> Path:
        return self._requests_dir / REQUESTS_LOG_FILE

    # ------------------------------------------------------------------
    # Persistence: index.json
    # ------------------------------------------------------------------

    def _load_index(self) -> None:
        path = self._index_path()
        if not path.exists():
            self._index_items = {}
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # --- HMAC 署名検証 ---
            stored_sig = data.pop("_hmac_signature", None)
            if stored_sig:
                if not verify_data_hmac(self._secret_key, data, stored_sig):
                    logger.critical("Capability index HMAC verification failed — possible tampering")
                    self._index_items = {}
                    return
            else:
                require_hmac = os.environ.get("RUMI_REQUIRE_HMAC", _REQUIRE_HMAC_DEFAULT) == "1"
                if require_hmac:
                    logger.critical("Capability index has no HMAC signature and RUMI_REQUIRE_HMAC=1")
                    self._index_items = {}
                    return
                else:
                    logger.warning(
                        "Capability index has no HMAC signature (legacy file). "
                        "Signature will be added on next save."
                    )

            items_raw = data.get("items", {})
            self._index_items = {}
            for key, item_data in items_raw.items():
                if isinstance(item_data, dict):
                    self._index_items[key] = IndexItem.from_dict(item_data)
        except (json.JSONDecodeError, OSError):
            self._index_items = {}

    def _save_index(self) -> None:
        data = {
            "version": "1.0",
            "updated_at": self._now_ts(),
            "cooldown_seconds": self._cooldown_seconds,
            "reject_threshold": self._reject_threshold,
            "items": {
                key: item.to_dict()
                for key, item in self._index_items.items()
            },
        }

        # HMAC 署名を追加
        data["_hmac_signature"] = compute_data_hmac(self._secret_key, data)

        path = self._index_path()
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(path)

    # ------------------------------------------------------------------
    # Persistence: blocked.json
    # ------------------------------------------------------------------

    def _load_blocked(self) -> None:
        path = self._blocked_path()
        if not path.exists():
            self._blocked = {}
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # --- HMAC 署名検証 ---
            stored_sig = data.pop("_hmac_signature", None)
            if stored_sig:
                if not verify_data_hmac(self._secret_key, data, stored_sig):
                    logger.critical("Capability blocked list HMAC verification failed — possible tampering")
                    self._blocked = {}
                    return
            else:
                require_hmac = os.environ.get("RUMI_REQUIRE_HMAC", _REQUIRE_HMAC_DEFAULT) == "1"
                if require_hmac:
                    logger.critical("Capability blocked list has no HMAC signature and RUMI_REQUIRE_HMAC=1")
                    self._blocked = {}
                    return
                else:
                    logger.warning(
                        "Capability blocked list has no HMAC signature (legacy file). "
                        "Signature will be added on next save."
                    )

            self._blocked = data.get("blocked", {})
        except (json.JSONDecodeError, OSError):
            self._blocked = {}

    def _save_blocked(self) -> None:
        data = {
            "version": "1.0",
            "updated_at": self._now_ts(),
            "blocked": self._blocked,
        }

        # HMAC 署名を追加
        data["_hmac_signature"] = compute_data_hmac(self._secret_key, data)

        path = self._blocked_path()
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(path)

    def _hmac_file_status(self, path: Path) -> str:
        if not path.exists():
            return "absent"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stored_sig = data.pop("_hmac_signature", None)
            if not stored_sig:
                return "missing"
            return "signed" if verify_data_hmac(self._secret_key, data, stored_sig) else "invalid"
        except Exception:
            return "invalid"

    def migrate_hmac_signatures(self) -> Dict[str, str]:
        """署名なし index / blocked を署名付きで保存し直す。"""
        statuses = {
            "index": self._hmac_file_status(self._index_path()),
            "blocked": self._hmac_file_status(self._blocked_path()),
        }
        results: Dict[str, str] = {}

        if statuses["index"] == "signed":
            results["index"] = "already_signed"
        elif statuses["index"] == "missing":
            self._save_index()
            results["index"] = "migrated"
        elif statuses["index"] == "absent":
            results["index"] = "absent"
        else:
            results["index"] = "failed"

        if statuses["blocked"] == "signed":
            results["blocked"] = "already_signed"
        elif statuses["blocked"] == "missing":
            self._save_blocked()
            results["blocked"] = "migrated"
        elif statuses["blocked"] == "absent":
            results["blocked"] = "absent"
        else:
            results["blocked"] = "failed"

        return results

    # ------------------------------------------------------------------
    # Persistence: requests.jsonl
    # ------------------------------------------------------------------

    def _append_event(
        self,
        event: str,
        candidate_key: str,
        actor: str = "",
        reason: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        entry = {
            "ts": self._now_ts(),
            "event": event,
            "candidate_key": candidate_key,
            "actor": actor,
            "reason": reason,
            "details": details or {},
        }
        try:
            with open(self._log_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Audit helper
    # ------------------------------------------------------------------

    @staticmethod
    def _audit_event(
        event_type: str,
        severity: Literal["info", "warning", "error", "critical"],
        description: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_security_event(
                event_type=event_type,
                severity=severity,
                description=description,
                details=details or {},
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # candidate_key construction
    # ------------------------------------------------------------------

    @staticmethod
    def make_candidate_key(pack_id: str, slug: str, handler_id: str, sha256: str) -> str:
        return f"{pack_id}:{slug}:{handler_id}:{sha256}"

    # ------------------------------------------------------------------
    # Slug validation (security) — delegates to validation.py
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_slug(slug: str) -> Tuple[bool, Optional[str]]:
        return _v_validate_slug(slug)

    # ------------------------------------------------------------------
    # Symlink check (security) — delegates to validation.py
    # ------------------------------------------------------------------

    @staticmethod
    def _check_no_symlinks(*paths: Path) -> Tuple[bool, Optional[str]]:
        return _v_check_no_symlinks(*paths)

    # ------------------------------------------------------------------
    # Entrypoint validation (security) — delegates to validation.py
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_entrypoint(entrypoint: str, slug_dir: Path) -> Tuple[bool, Optional[str], Optional[Path]]:
        return _v_validate_entrypoint(entrypoint, slug_dir)

    # ------------------------------------------------------------------
    # SHA-256 computation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_sha256(file_path: Path) -> str:
        import hashlib
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    # ------------------------------------------------------------------
    # scan_candidates
    # ------------------------------------------------------------------

    def scan_candidates(self, ecosystem_dir: Optional[str] = None) -> ScanResult:
        """
        ecosystem を走査して候補を検出し、pending を作成する。
        """
        with self._lock:
            eco_root = Path(ecosystem_dir or DEFAULT_ECOSYSTEM_DIR)
            result = ScanResult()
            now = self._now_dt()

            if not eco_root.is_dir():
                return result

            try:
                pack_dirs = sorted(
                    (d for d in eco_root.iterdir()
                     if d.is_dir()
                     and d.name not in _EXCLUDED_PACK_DIRS
                     and not d.name.startswith(".")),
                    key=lambda d: d.name,
                )
            except OSError:
                return result

            legacy_packs_root = eco_root / "packs"
            if legacy_packs_root.is_dir():
                try:
                    legacy_dirs = sorted(
                        (d for d in legacy_packs_root.iterdir()
                         if d.is_dir()
                         and d.name not in _EXCLUDED_PACK_DIRS
                         and not d.name.startswith(".")),
                        key=lambda d: d.name,
                    )
                    seen_pack_ids = {d.name for d in pack_dirs}
                    for ld in legacy_dirs:
                        if ld.name not in seen_pack_ids:
                            pack_dirs.append(ld)
                            seen_pack_ids.add(ld.name)
                except OSError:
                    pass

            for pack_dir in pack_dirs:
                pack_id = pack_dir.name
                candidates_root = pack_dir / CANDIDATE_SUBPATH

                if not candidates_root.is_dir():
                    continue

                try:
                    slug_dirs = sorted(
                        (d for d in candidates_root.iterdir()
                         if d.is_dir() and not d.name.startswith(".")),
                        key=lambda d: d.name,
                    )
                except OSError:
                    continue

                for slug_dir in slug_dirs:
                    result.scanned_count += 1
                    slug = slug_dir.name

                    slug_valid, slug_error = self._validate_slug(slug)
                    if not slug_valid:
                        result.errors.append({
                            "pack_id": pack_id,
                            "slug": slug,
                            "error": slug_error,
                        })
                        continue

                    handler_json_path = slug_dir / "handler.json"
                    if not handler_json_path.exists():
                        result.errors.append({
                            "pack_id": pack_id,
                            "slug": slug,
                            "error": "handler.json not found",
                        })
                        continue

                    try:
                        with open(handler_json_path, "r", encoding="utf-8") as f:
                            handler_data = json.load(f)
                    except (json.JSONDecodeError, OSError) as e:
                        result.errors.append({
                            "pack_id": pack_id,
                            "slug": slug,
                            "error": f"Failed to parse handler.json: {e}",
                        })
                        continue

                    if not isinstance(handler_data, dict):
                        result.errors.append({
                            "pack_id": pack_id,
                            "slug": slug,
                            "error": "handler.json must be a JSON object",
                        })
                        continue

                    handler_id = handler_data.get("handler_id")
                    permission_id = handler_data.get("permission_id")
                    entrypoint = handler_data.get("entrypoint", "handler.py:execute")

                    if not handler_id or not isinstance(handler_id, str):
                        result.errors.append({
                            "pack_id": pack_id,
                            "slug": slug,
                            "error": "Missing or invalid handler_id",
                        })
                        continue

                    if not permission_id or not isinstance(permission_id, str):
                        result.errors.append({
                            "pack_id": pack_id,
                            "slug": slug,
                            "error": "Missing or invalid permission_id",
                        })
                        continue

                    valid, ep_error, handler_py_path = self._validate_entrypoint(entrypoint, slug_dir)
                    if not valid or handler_py_path is None:
                        result.errors.append({
                            "pack_id": pack_id,
                            "slug": slug,
                            "error": ep_error,
                        })
                        continue

                    try:
                        sha256 = self._compute_sha256(handler_py_path)
                    except Exception as e:
                        result.errors.append({
                            "pack_id": pack_id,
                            "slug": slug,
                            "error": f"Failed to compute sha256: {e}",
                        })
                        continue

                    candidate_key = self.make_candidate_key(pack_id, slug, handler_id, sha256)

                    if candidate_key in self._blocked:
                        result.skipped_blocked += 1
                        continue

                    existing = self._index_items.get(candidate_key)
                    if existing is not None:
                        if existing.status == CandidateStatus.INSTALLED:
                            result.skipped_installed += 1
                            continue
                        elif existing.status == CandidateStatus.PENDING:
                            result.skipped_pending += 1
                            continue
                        elif existing.status == CandidateStatus.REJECTED:
                            if existing.cooldown_until:
                                cooldown_dt = self._parse_ts(existing.cooldown_until)
                                if cooldown_dt and cooldown_dt > now:
                                    result.skipped_cooldown += 1
                                    continue
                            existing.status = CandidateStatus.PENDING
                            existing.last_event_ts = self._now_ts()
                            existing.cooldown_until = None
                            self._save_index()
                            self._append_event(
                                event="capability_handler.requested",
                                candidate_key=candidate_key,
                                actor="system",
                                reason="Cooldown expired, re-pending",
                            )
                            result.pending_created += 1
                            continue
                        elif existing.status == CandidateStatus.FAILED:
                            result.skipped_failed += 1
                            continue
                        elif existing.status == CandidateStatus.BLOCKED:
                            result.skipped_blocked += 1
                            continue

                    candidate_info = CandidateInfo(
                        pack_id=pack_id,
                        slug=slug,
                        handler_id=handler_id,
                        permission_id=permission_id,
                        entrypoint=entrypoint,
                        source_dir=str(slug_dir),
                        handler_py_sha256=sha256,
                    )

                    item = IndexItem(
                        candidate_key=candidate_key,
                        status=CandidateStatus.PENDING,
                        reject_count=0,
                        cooldown_until=None,
                        last_event_ts=self._now_ts(),
                        candidate=candidate_info,
                        installed_to=None,
                        last_error=None,
                    )
                    self._index_items[candidate_key] = item
                    result.pending_created += 1

                    self._append_event(
                        event="capability_handler.requested",
                        candidate_key=candidate_key,
                        actor="system",
                        details=candidate_info.to_dict(),
                    )

            if result.pending_created > 0:
                self._save_index()

            return result

    # ------------------------------------------------------------------
    # approve_and_install
    # ------------------------------------------------------------------

    def approve_and_install(
        self,
        candidate_key: str,
        actor: str = "user",
        notes: str = "",
    ) -> ApproveResult:
        """
        候補を承認し、同時に install する。
        """
        with self._lock:
            item = self._index_items.get(candidate_key)
            if item is None:
                return ApproveResult(success=False, error="Candidate not found")

            if item.status == CandidateStatus.INSTALLED:
                return ApproveResult(
                    success=True,
                    status="installed",
                    installed_to=item.installed_to or "",
                    handler_id=item.candidate.handler_id if item.candidate else "",
                    permission_id=item.candidate.permission_id if item.candidate else "",
                    sha256=item.candidate.handler_py_sha256 if item.candidate else "",
                )

            if item.status == CandidateStatus.BLOCKED:
                return ApproveResult(success=False, error="Candidate is blocked. Unblock first.")

            if item.candidate is None:
                return ApproveResult(success=False, error="Candidate info missing")

            candidate = item.candidate
            source_dir = Path(candidate.source_dir)

            slug_valid, slug_error = self._validate_slug(candidate.slug)
            if not slug_valid:
                self._mark_failed(item, f"Invalid slug: {slug_error}")
                return ApproveResult(success=False, error=f"Invalid slug: {slug_error}")

            handler_json_path = source_dir / "handler.json"
            if not handler_json_path.exists():
                self._mark_failed(item, "Source handler.json not found during approve")
                return ApproveResult(success=False, error="Source handler.json not found")

            try:
                with open(handler_json_path, "r", encoding="utf-8") as f:
                    handler_data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                self._mark_failed(item, f"Failed to re-read handler.json: {e}")
                return ApproveResult(success=False, error=f"Failed to re-read handler.json: {e}")

            if handler_data.get("handler_id") != candidate.handler_id:
                self._mark_failed(item, "handler_id changed since scan")
                return ApproveResult(success=False, error="handler_id changed since scan (TOCTOU)")
            if handler_data.get("permission_id") != candidate.permission_id:
                self._mark_failed(item, "permission_id changed since scan")
                return ApproveResult(success=False, error="permission_id changed since scan (TOCTOU)")

            entrypoint = handler_data.get("entrypoint", "handler.py:execute")
            valid, ep_error, handler_py_path = self._validate_entrypoint(entrypoint, source_dir)
            if not valid or handler_py_path is None:
                self._mark_failed(item, f"Entrypoint validation failed: {ep_error}")
                return ApproveResult(success=False, error=f"Entrypoint validation failed: {ep_error}")

            try:
                actual_sha256 = self._compute_sha256(handler_py_path)
            except Exception as e:
                self._mark_failed(item, f"Failed to compute sha256: {e}")
                return ApproveResult(success=False, error=f"Failed to compute sha256: {e}")

            if actual_sha256 != candidate.handler_py_sha256:
                self._mark_failed(item, "SHA-256 mismatch (content changed since scan)")
                return ApproveResult(
                    success=False,
                    error="SHA-256 mismatch: handler.py content changed since scan (TOCTOU)",
                )

            try:
                trust_store = _get_trust_store()
                if not trust_store.is_loaded():
                    trust_store.load()
                trust_ok = trust_store.add_trust(
                    handler_id=candidate.handler_id,
                    sha256=actual_sha256,
                    note=f"Approved by {actor}. pack={candidate.pack_id}, slug={candidate.slug}. {notes}".strip(),
                )
                if not trust_ok:
                    self._mark_failed(item, "Failed to register trust")
                    return ApproveResult(success=False, error="Failed to register trust")
            except Exception as e:
                self._mark_failed(item, f"Trust registration error: {e}")
                return ApproveResult(success=False, error=f"Trust registration error: {e}")

            dest_dir = self._handlers_dest_dir / candidate.slug

            dest_ok, dest_error = _v_check_path_within(dest_dir, self._handlers_dest_dir)
            if not dest_ok:
                self._mark_failed(item, "Path traversal detected in destination path")
                return ApproveResult(
                    success=False,
                    error="Path traversal detected in destination path",
                )

            ep_file_for_check = entrypoint.rsplit(":", 1)[0] if ":" in entrypoint else "handler.py"
            source_json_path = source_dir / "handler.json"
            source_py_path = source_dir / ep_file_for_check

            symlink_ok, symlink_error = self._check_no_symlinks(
                source_json_path, source_py_path,
            )
            if not symlink_ok:
                failure_reason = symlink_error or "symlink validation failed"
                self._mark_failed(item, failure_reason)
                return ApproveResult(success=False, error=failure_reason)

            try:
                final_sha256 = self._compute_sha256(source_py_path)
            except Exception as e:
                self._mark_failed(item, f"Failed to compute sha256 (pre-copy): {e}")
                return ApproveResult(success=False, error=f"Failed to compute sha256 (pre-copy): {e}")

            if final_sha256 != candidate.handler_py_sha256:
                self._mark_failed(item, "SHA-256 mismatch at copy time (TOCTOU race detected)")
                return ApproveResult(
                    success=False,
                    error="SHA-256 mismatch at copy time: handler.py content changed between approval and copy (TOCTOU)",
                )

            try:
                copy_result = self._copy_handler(source_dir, dest_dir, candidate)
                if not copy_result[0]:
                    self._mark_failed(item, copy_result[1])
                    return ApproveResult(success=False, error=copy_result[1])
            except Exception as e:
                self._mark_failed(item, f"Copy error: {e}")
                return ApproveResult(success=False, error=f"Copy error: {e}")

            # Phase D: CapabilityHandlerRegistry removed. FunctionRegistry handles registration.

            try:
                executor = _get_executor()
                executor._initialized = False
                executor.initialize()
            except Exception:
                pass

            item.status = CandidateStatus.INSTALLED
            item.installed_to = str(dest_dir)
            item.cooldown_until = None
            item.last_event_ts = self._now_ts()
            item.last_error = None
            self._save_index()

            self._append_event(
                event="capability_handler.approved_and_installed",
                candidate_key=candidate_key,
                actor=actor,
                reason=notes,
                details={
                    "installed_to": str(dest_dir),
                    "handler_id": candidate.handler_id,
                    "permission_id": candidate.permission_id,
                    "sha256": actual_sha256,
                },
            )

            self._audit_event(
                event_type="capability_handler_installed",
                severity="info",
                description=f"Capability handler '{candidate.handler_id}' approved and installed",
                details={
                    "pack_id": candidate.pack_id,
                    "slug": candidate.slug,
                    "handler_id": candidate.handler_id,
                    "permission_id": candidate.permission_id,
                    "sha256": actual_sha256,
                    "source_dir": candidate.source_dir,
                    "installed_to": str(dest_dir),
                    "actor": actor,
                    "notes": notes,
                },
            )

            return ApproveResult(
                success=True,
                status="installed",
                installed_to=str(dest_dir),
                handler_id=candidate.handler_id,
                permission_id=candidate.permission_id,
                sha256=actual_sha256,
            )

    def _copy_handler(
        self,
        source_dir: Path,
        dest_dir: Path,
        candidate: CandidateInfo,
    ) -> Tuple[bool, str]:
        ep_file = candidate.entrypoint.rsplit(":", 1)[0] if ":" in candidate.entrypoint else "handler.py"

        source_json = source_dir / "handler.json"
        source_py = source_dir / ep_file

        if not source_json.exists():
            return False, "Source handler.json not found"
        if not source_py.exists():
            return False, f"Source {ep_file} not found"

        if os.path.islink(source_json) or os.path.islink(source_py):
            return False, "Symbolic link detected in source files (security risk)"

        if dest_dir.exists():
            existing_json_path = dest_dir / "handler.json"
            existing_py_path = dest_dir / ep_file

            if existing_json_path.exists() and existing_py_path.exists():
                try:
                    with open(existing_json_path, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                    existing_handler_id = existing_data.get("handler_id", "")
                    existing_sha256 = self._compute_sha256(existing_py_path)

                    if existing_handler_id == candidate.handler_id and existing_sha256 == candidate.handler_py_sha256:
                        return True, ""

                    return False, (
                        f"Destination already exists with different content. "
                        f"existing handler_id={existing_handler_id}, sha256={existing_sha256[:16]}... "
                        f"vs candidate handler_id={candidate.handler_id}, sha256={candidate.handler_py_sha256[:16]}..."
                    )
                except Exception as e:
                    return False, f"Failed to check existing destination: {e}"
            elif existing_json_path.exists() or existing_py_path.exists():
                return False, "Destination directory exists in inconsistent state"

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_py = dest_dir / ep_file
        dest_py.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source_json), str(dest_dir / "handler.json"))
        shutil.copy2(str(source_py), str(dest_py))

        return True, ""

    def _mark_failed(self, item: IndexItem, error: str) -> None:
        item.status = CandidateStatus.FAILED
        item.last_error = error
        item.last_event_ts = self._now_ts()
        self._save_index()

        self._append_event(
            event="capability_handler.install_failed",
            candidate_key=item.candidate_key,
            actor="system",
            reason=error,
        )

        self._audit_event(
            event_type="capability_handler_install_failed",
            severity="error",
            description=f"Capability handler install failed: {error}",
            details={
                "candidate_key": item.candidate_key,
                "error": error,
                "candidate": item.candidate.to_dict() if item.candidate else None,
            },
        )

    # ------------------------------------------------------------------
    # reject
    # ------------------------------------------------------------------

    def reject(
        self,
        candidate_key: str,
        actor: str = "user",
        reason: str = "",
    ) -> RejectResult:
        with self._lock:
            item = self._index_items.get(candidate_key)
            if item is None:
                return RejectResult(success=False, error="Candidate not found")

            if item.status == CandidateStatus.INSTALLED:
                return RejectResult(success=False, error="Cannot reject installed candidate")

            if item.status == CandidateStatus.BLOCKED:
                return RejectResult(success=False, error="Candidate is already blocked")

            now = self._now_dt()
            item.reject_count += 1
            item.last_event_ts = self._now_ts()

            cooldown_until_dt = now + timedelta(seconds=self._cooldown_seconds)
            cooldown_until_str = cooldown_until_dt.isoformat().replace("+00:00", "Z")

            if item.reject_count >= self._reject_threshold:
                item.status = CandidateStatus.BLOCKED
                item.cooldown_until = None

                self._blocked[candidate_key] = {
                    "candidate_key": candidate_key,
                    "blocked_at": self._now_ts(),
                    "reason": f"Rejected {item.reject_count} times",
                    "reject_count": item.reject_count,
                }
                self._save_blocked()

                self._append_event(
                    event="capability_handler.blocked",
                    candidate_key=candidate_key,
                    actor=actor,
                    reason=f"Rejected {item.reject_count} times (threshold={self._reject_threshold})",
                    details={"reject_count": item.reject_count},
                )

                self._audit_event(
                    event_type="capability_handler_blocked",
                    severity="warning",
                    description=f"Capability handler blocked after {item.reject_count} rejections",
                    details={
                        "candidate_key": candidate_key,
                        "reject_count": item.reject_count,
                        "candidate": item.candidate.to_dict() if item.candidate else None,
                    },
                )
            else:
                item.status = CandidateStatus.REJECTED
                item.cooldown_until = cooldown_until_str

                self._append_event(
                    event="capability_handler.rejected",
                    candidate_key=candidate_key,
                    actor=actor,
                    reason=reason,
                    details={
                        "reject_count": item.reject_count,
                        "cooldown_until": cooldown_until_str,
                    },
                )

                self._audit_event(
                    event_type="capability_handler_rejected",
                    severity="warning",
                    description=f"Capability handler rejected ({item.reject_count}/{self._reject_threshold})",
                    details={
                        "candidate_key": candidate_key,
                        "reject_count": item.reject_count,
                        "reason": reason,
                        "actor": actor,
                        "candidate": item.candidate.to_dict() if item.candidate else None,
                    },
                )

            self._save_index()

            return RejectResult(
                success=True,
                status=item.status.value,
                reject_count=item.reject_count,
                cooldown_until=item.cooldown_until,
            )

    # ------------------------------------------------------------------
    # unblock
    # ------------------------------------------------------------------

    def unblock(
        self,
        candidate_key: str,
        actor: str = "user",
        reason: str = "user_unblocked",
    ) -> UnblockResult:
        with self._lock:
            if candidate_key not in self._blocked:
                item = self._index_items.get(candidate_key)
                if item is None or item.status != CandidateStatus.BLOCKED:
                    return UnblockResult(success=False, error="Candidate not found in blocked list")
            else:
                del self._blocked[candidate_key]
                self._save_blocked()

            item = self._index_items.get(candidate_key)
            if item is None:
                return UnblockResult(success=False, error="Candidate not found in index")

            now = self._now_dt()
            cooldown_until_dt = now + timedelta(seconds=self._cooldown_seconds)
            cooldown_until_str = cooldown_until_dt.isoformat().replace("+00:00", "Z")

            item.status = CandidateStatus.REJECTED
            item.cooldown_until = cooldown_until_str
            item.last_event_ts = self._now_ts()
            self._save_index()

            self._append_event(
                event="capability_handler.unblocked",
                candidate_key=candidate_key,
                actor=actor,
                reason=reason,
                details={"cooldown_until": cooldown_until_str},
            )

            self._audit_event(
                event_type="capability_handler_unblocked",
                severity="warning",
                description="Capability handler unblocked",
                details={
                    "candidate_key": candidate_key,
                    "actor": actor,
                    "reason": reason,
                },
            )

            return UnblockResult(
                success=True,
                status_after="rejected",
            )

    # ------------------------------------------------------------------
    # Query helpers (for API)
    # ------------------------------------------------------------------

    def list_items(self, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = []
            for item in self._index_items.values():
                if status_filter and status_filter != "all":
                    if item.status.value != status_filter:
                        continue
                items.append(item.to_dict())
            return items

    def list_blocked(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._blocked)

    def get_item(self, candidate_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._index_items.get(candidate_key)
            if item is None:
                return None
            return item.to_dict()


# ======================================================================
# Global instance
# ======================================================================

_global_installer: Optional[CapabilityInstaller] = None
_installer_lock = threading.Lock()


def get_capability_installer() -> CapabilityInstaller:
    global _global_installer
    if _global_installer is None:
        with _installer_lock:
            if _global_installer is None:
                _global_installer = CapabilityInstaller()
    return _global_installer


def reset_capability_installer(
    requests_dir: Optional[str] = None,
    handlers_dest_dir: Optional[str] = None,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    reject_threshold: int = DEFAULT_REJECT_THRESHOLD,
) -> CapabilityInstaller:
    global _global_installer
    with _installer_lock:
        _global_installer = CapabilityInstaller(
            requests_dir=requests_dir,
            handlers_dest_dir=handlers_dest_dir,
            cooldown_seconds=cooldown_seconds,
            reject_threshold=reject_threshold,
        )
    return _global_installer
