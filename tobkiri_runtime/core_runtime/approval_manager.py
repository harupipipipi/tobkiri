"""
approval_manager.py - Pack承認管理

Packのインストール、承認、ハッシュ検証を管理する。
承認されていないPackのコードは実行されない。

Phase2追加: local_pack対応（ecosystem/flows/**の仮想pack）
パス刷新: pack供給元を ecosystem/ 直下に変更（ecosystem/packs/ 互換あり）

Agent 7-F 変更:
  S-9:  HMAC検証失敗時のaudit log記録
  M-12: scan_packsのI/Oをロック外に移動
  G-2:  apply_update capability
  G-3:  version history / rollback

Wave 17-B 変更:
  rollback_to_version: 現在のファイルハッシュと target_hashes の一致を検証
  verify_hash: use_cache パラメータ追加
  _compute_pack_hashes_nocache: キャッシュなしハッシュ計算
  is_pack_approved_and_verified: use_cache=False で検証
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import json
import os
import sys
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple


from .paths import (
    LOCAL_PACK_ID,
    LOCAL_PACK_DIR,
    ECOSYSTEM_DIR,
    GRANTS_DIR,
    CORE_PACK_DIR,
    CORE_PACK_ID_PREFIX,
    discover_pack_locations,
    check_pack_id_mismatch,
    PackLocation,
)
from .hmac_key_manager import (
    generate_or_load_signing_key,
    compute_data_hmac,
    verify_data_hmac,
)
from .host_contract import host_contract_value

logger = logging.getLogger(__name__)


SYSTEM_PACK_DESCRIPTOR_SCHEMA = "io.tobkiri.system-pack-trust.v1"


def _signed_system_pack_descriptors() -> list[Mapping[str, Any]]:
    """Read host-verified system-Pack descriptors without ambient fallback."""
    raw = host_contract_value("system_pack_descriptors")
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, Mapping)]


class PackStatus(Enum):
    """Pack状態"""
    INSTALLED = "installed"
    PENDING = "pending"
    APPROVED = "approved"
    RUNNING = "running"
    MODIFIED = "modified"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass
class PackApproval:
    """Pack承認情報"""
    pack_id: str
    status: PackStatus
    created_at: str
    approved_at: Optional[str] = None
    file_hashes: Dict[str, str] = field(default_factory=dict)
    permissions_requested: List[Dict[str, Any]] = field(default_factory=list)
    rejection_reason: Optional[str] = None
    version_history: List[Dict[str, Any]] = field(default_factory=list)  # G-3
    rule_approved: bool = False
    rule_approved_at: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PackApproval':
        data = dict(data)
        # 後方互換: version_history がなければ空リスト
        data.setdefault("version_history", [])
        data.setdefault("rule_approved", False)
        data.setdefault("rule_approved_at", None)
        if isinstance(data.get("status"), str):
            data["status"] = PackStatus(data["status"])
        return cls(**data)


@dataclass
class ApprovalResult:
    """承認操作結果"""
    success: bool
    pack_id: str = ""
    error: Optional[str] = None
    status: Optional[PackStatus] = None


class ApprovalManager:
    """Pack承認管理クラス"""
    
    def __init__(
        self,
        packs_dir: str = ECOSYSTEM_DIR,
        grants_dir: str = GRANTS_DIR,
        secret_key: Optional[str] = None,
        system_pack_descriptors: Iterable[Mapping[str, Any]] | None = None,
    ):
        self.packs_dir = Path(packs_dir)
        self.grants_dir = Path(grants_dir)
        if secret_key:
            self._secret_key: bytes = secret_key.encode("utf-8")
        else:
            self._secret_key = generate_or_load_signing_key(
                self.grants_dir / ".secret_key",
                env_var="RUMI_HMAC_SECRET",
            )
        self._approvals: Dict[str, PackApproval] = {}
        self._pack_locations: Dict[str, PackLocation] = {}
        self._lock = threading.RLock()  # RLockで再入可能
        self._initialized = False
        # #37: ハッシュキャッシュ (key=resolved_path, value=(hashes, monotonic_ts))
        self._hash_cache: Dict[str, Tuple[Dict[str, str], float]] = {}
        self._hash_cache_ttl: float = float(
            os.environ.get("RUMI_HASH_CACHE_TTL_SEC", "30")
        )
        descriptors = (
            list(system_pack_descriptors)
            if system_pack_descriptors is not None
            else _signed_system_pack_descriptors()
        )
        self._system_pack_descriptors = tuple(
            dict(item) for item in descriptors if isinstance(item, Mapping)
        )
    
    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _is_core_pack(self, pack_id: str) -> bool:
        """Return True only for shipped core packs in the trusted core_pack dir."""
        normalized_pack_id = str(pack_id or "").strip()
        if not normalized_pack_id.startswith(CORE_PACK_ID_PREFIX):
            return False

        trusted_core_dir = self._resolve_core_pack_dir(normalized_pack_id)
        if trusted_core_dir is None:
            return False

        # A pack with the same core-looking ID under the user/imported pack
        # search root must not inherit trust from the shipped core_pack copy.
        user_pack_dir = self._resolve_pack_dir(normalized_pack_id)
        if user_pack_dir is None:
            return True

        try:
            return user_pack_dir.resolve() == trusted_core_dir.resolve()
        except OSError:
            return False

    def _resolve_core_pack_dir(self, pack_id: str) -> Optional[Path]:
        """Resolve a core pack only if it is inside CORE_PACK_DIR."""
        core_root = Path(CORE_PACK_DIR)
        try:
            core_root_resolved = core_root.resolve()
            candidate = (core_root / pack_id).resolve()
            candidate.relative_to(core_root_resolved)
        except (OSError, ValueError):
            return None

        if not candidate.is_dir():
            return None
        if not (candidate / "ecosystem.json").is_file():
            return None
        return candidate

    def _is_system_pack(self, pack_id: str) -> bool:
        """Return true only for a host-verified system-Pack descriptor."""
        normalized_pack_id = str(pack_id or "").strip()
        if not normalized_pack_id:
            return False
        # A partially initialized manager has no trusted descriptor root.  It
        # must never promote a Pack to system trust merely because a caller
        # supplied a core-looking ID or location.
        if not isinstance(getattr(self, "packs_dir", None), Path):
            return False
        if not isinstance(getattr(self, "_system_pack_descriptors", None), tuple):
            return False
        pack_dir = self._resolve_pack_dir(normalized_pack_id)
        if pack_dir is None or not pack_dir.exists():
            return False

        return self._system_pack_descriptor(
            normalized_pack_id,
            pack_dir,
        ) is not None

    def _is_trusted_builtin_pack(self, pack_id: str) -> bool:
        """Compatibility name for generic, descriptor-backed system trust."""
        return self._is_system_pack(pack_id)

    def _system_pack_descriptor(
        self,
        pack_id: str,
        pack_dir: Path,
    ) -> Mapping[str, Any] | None:
        try:
            resolved_pack_dir = pack_dir.resolve()
            resolved_packs_root = self.packs_dir.resolve()
            resolved_pack_dir.relative_to(resolved_packs_root)
        except (OSError, ValueError):
            return None
        for descriptor in self._system_pack_descriptors:
            if descriptor.get("schema") != SYSTEM_PACK_DESCRIPTOR_SCHEMA:
                continue
            if descriptor.get("trust_class") != "system":
                continue
            if str(descriptor.get("pack_id") or "").strip() != pack_id:
                continue
            root = descriptor.get("root")
            if not isinstance(root, str) or not root:
                continue
            try:
                if Path(root).expanduser().resolve() != resolved_pack_dir:
                    continue
            except OSError:
                continue
            return descriptor
        return None

    def is_pack_in_process_allowed(
        self,
        pack_id: str,
        pack_root: Path,
    ) -> bool:
        """Check the explicit system-Pack permission for in-process execution."""
        descriptor = self._system_pack_descriptor(str(pack_id or "").strip(), pack_root)
        return bool(descriptor and descriptor.get("allow_in_process") is True)

    def _invalidate_hash_cache(self, pack_id: str) -> None:
        """指定 pack のハッシュキャッシュを無効化する"""
        if pack_id == LOCAL_PACK_ID:
            local_dir = self._get_local_pack_dir()
            if local_dir.exists():
                self._hash_cache.pop(str(local_dir.resolve()), None)
        else:
            pack_dir = self._resolve_pack_dir(pack_id)
            if pack_dir:
                self._hash_cache.pop(str(pack_dir.resolve()), None)
        try:
            from .resolved_profile_scope import (
                invalidate_persisted_resolved_profile,
            )

            invalidate_persisted_resolved_profile()
        except ImportError:
            pass
    
    def initialize(self) -> None:
        """初期化: grants.jsonを読み込み"""
        with self._lock:
            self.grants_dir.mkdir(parents=True, exist_ok=True)
            
            for grant_file in self.grants_dir.glob("*.grants.json"):
                try:
                    self._load_grant_file(grant_file)
                except Exception:
                    continue
            
            self._initialized = True
    
    def _load_grant_file(self, path: Path) -> None:
        """grants.jsonを読み込み、HMAC検証"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # HMAC検証（署名なしファイルも改ざん扱いで拒否）
        stored_sig = data.pop("_hmac_signature", None)
        if not stored_sig:
            pack_id = data.get("pack_id", path.stem.replace(".grants", ""))
            # S-9: HMAC検証失敗（署名なし）を監査ログに記録
            try:
                from .audit_logger import get_audit_logger
                get_audit_logger().log_security_event(
                    event_type="hmac_verification_failed",
                    severity="critical",
                    description="Grant file has no HMAC signature — possible tampering",
                    pack_id=pack_id,
                    details={"file_path": str(path), "reason": "missing_signature"},
                )
            except Exception:
                pass
            self._approvals[pack_id] = PackApproval(
                pack_id=pack_id,
                status=PackStatus.MODIFIED,
                created_at=data.get("created_at", self._now_ts())
            )
            return

        if not verify_data_hmac(self._secret_key, data, stored_sig):
            pack_id = data.get("pack_id", path.stem.replace(".grants", ""))
            # S-9: HMAC検証失敗（署名不一致）を監査ログに記録
            try:
                from .audit_logger import get_audit_logger
                get_audit_logger().log_security_event(
                    event_type="hmac_verification_failed",
                    severity="critical",
                    description="Grant file HMAC signature mismatch — possible tampering",
                    pack_id=pack_id,
                    details={"file_path": str(path), "reason": "signature_mismatch"},
                )
            except Exception:
                pass
            self._approvals[pack_id] = PackApproval(
                pack_id=pack_id,
                status=PackStatus.MODIFIED,
                created_at=data.get("created_at", self._now_ts())
            )
            return
        
        pack_id = data.get("pack_id")
        if pack_id:
            self._approvals[pack_id] = PackApproval.from_dict(data)
    
    def _save_grant(self, approval: PackApproval) -> None:
        """grants.jsonを保存（HMAC署名付き、アトミック書き込み）"""
        self.grants_dir.mkdir(parents=True, exist_ok=True)
        
        data = approval.to_dict()
        data["_hmac_signature"] = compute_data_hmac(self._secret_key, data)
        
        path = self.grants_dir / f"{approval.pack_id}.grants.json"
        # PC-9 fix: パストラバーサル防御 — grants_dir 配下であることを検証
        grants_dir_resolved = self.grants_dir.resolve()
        path_resolved = path.resolve()
        try:
            path_resolved.relative_to(grants_dir_resolved)
        except ValueError:
            logger.error(
                "Path traversal detected in pack_id '%s': resolved path '%s' "
                "is outside grants_dir '%s'",
                approval.pack_id, path_resolved, grants_dir_resolved,
            )
            return  # 書き込みを拒否
        # PC-2 fix: アトミック書き込み — tmp ファイルに書いてから rename
        import tempfile
        try:
            fd, tmp_path_str = tempfile.mkstemp(
                dir=str(self.grants_dir), suffix=".tmp", prefix=".grant_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                Path(tmp_path_str).replace(path)
            except BaseException:
                try:
                    os.unlink(tmp_path_str)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.error("Failed to save grant file atomically for '%s': %s", approval.pack_id, e)
            # フォールバック: 直接書き込み（データ消失よりはマシ）
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
    
    def _is_local_pack_mode_enabled(self) -> bool:
        """local_packモードが有効かチェック"""
        mode = os.environ.get("RUMI_LOCAL_PACK_MODE", "off").lower()
        return mode == "require_approval"
    
    def _get_local_pack_dir(self) -> Path:
        """local_pack用のディレクトリを取得"""
        return Path(LOCAL_PACK_DIR)
    
    def _compute_local_pack_hashes(self) -> Dict[str, str]:
        """
        local_pack用のハッシュを計算
        
        対象: ecosystem/flows/**/*.flow.yaml, ecosystem/flows/**/*.modifier.yaml
        """
        hashes: Dict[str, str] = {}
        local_dir = self._get_local_pack_dir()
        
        if not local_dir.exists():
            return hashes
        
        # .flow.yaml と .modifier.yaml を対象
        patterns = ["**/*.flow.yaml", "**/*.modifier.yaml"]
        
        for pattern in patterns:
            for file_path in local_dir.glob(pattern):
                if file_path.is_file():
                    # __pycache__ 等を除外
                    if self._should_skip_hash_file(file_path, local_dir):
                        continue
                    
                    relative_path = file_path.relative_to(local_dir).as_posix()
                    hash_value = self._compute_file_hash(file_path)
                    hashes[relative_path] = hash_value
        
        return hashes
    
    def scan_packs(self) -> List[str]:
        """
        インストール済みPackをスキャン
        
        discover_pack_locations() を使って ecosystem/* と ecosystem/packs/* を走査。
        canonical pack_id はディレクトリ名。
        ecosystem.json の pack_id が異なる場合は警告を記録。

        M-12: ロック内ではメモリ更新のみ行い、ファイルI/Oはロック外で実行。
        """
        packs = []
        
        locations = discover_pack_locations(str(self.packs_dir))

        pending_saves = []  # M-12: ロック外でバッチI/O
        for loc in locations:
            pack_id = loc.pack_id
            self._pack_locations[pack_id] = loc
            packs.append(pack_id)
            
            # ecosystem.json の pack_id とディレクトリ名の不一致を検出・記録
            mismatch_warning = check_pack_id_mismatch(loc)
            if mismatch_warning:
                self._record_pack_id_mismatch(pack_id, mismatch_warning)
            
            with self._lock:
                if pack_id not in self._approvals:
                    self._approvals[pack_id] = PackApproval(
                        pack_id=pack_id,
                        status=PackStatus.INSTALLED,
                        created_at=self._now_ts()
                    )
                    pending_saves.append(pack_id)
        
        # local_pack対応: RUMI_LOCAL_PACK_MODE=require_approval の場合のみ
        if self._is_local_pack_mode_enabled():
            local_dir = self._get_local_pack_dir()
            if local_dir.exists():
                packs.append(LOCAL_PACK_ID)
                with self._lock:
                    if LOCAL_PACK_ID not in self._approvals:
                        self._approvals[LOCAL_PACK_ID] = PackApproval(
                            pack_id=LOCAL_PACK_ID,
                            status=PackStatus.INSTALLED,
                            created_at=self._now_ts()
                        )
                        pending_saves.append(LOCAL_PACK_ID)

        # M-12: ロック外でバッチI/O
        for pid in pending_saves:
            with self._lock:
                approval = self._approvals.get(pid)
            if approval:
                self._save_grant(approval)
        
        return packs

    def get_resource_requirements(self, pack_id: str) -> Dict[str, Any]:
        """Return resource requirements declared in a pack ecosystem.json."""
        pack_dir = self._resolve_pack_dir(pack_id)
        if pack_dir is None:
            return {}
        ecosystem_file = pack_dir / "ecosystem.json"
        try:
            data = json.loads(ecosystem_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

        requirements: Dict[str, Any] = {}
        if data.get("required_secrets"):
            requirements["required_secrets"] = data["required_secrets"]
        if data.get("required_network"):
            requirements["required_network"] = data["required_network"]
        if data.get("host_execution"):
            requirements["host_execution"] = data["host_execution"]
        return requirements
    
    def _resolve_pack_dir(self, pack_id: str) -> Optional[Path]:
        """
        pack_id から pack_dir を解決する。
        キャッシュ → discover の順で探索。
        """
        if pack_id in self._pack_locations:
            return self._pack_locations[pack_id].pack_dir
        
        # キャッシュにない場合は再探索
        locations = discover_pack_locations(str(self.packs_dir))
        for loc in locations:
            self._pack_locations[loc.pack_id] = loc
        
        if pack_id in self._pack_locations:
            return self._pack_locations[pack_id].pack_dir
        
        # フォールバック: 旧構造
        legacy = self.packs_dir / pack_id
        if legacy.is_dir():
            return legacy
        return None
    
    def _record_pack_id_mismatch(self, pack_id: str, warning: str) -> None:
        """pack_id 不一致の警告を diagnostics/audit に記録"""
        print(
            f"[ApprovalManager] WARNING: {warning}",
            file=sys.stderr,
        )
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type="pack_id_mismatch",
                success=True,
                details={
                    "pack_id": pack_id,
                    "warning": warning,
                }
            )
        except Exception:
            pass
    
    def get_status(self, pack_id: str) -> Optional[PackStatus]:
        """Pack状態を取得"""
        # W22-A: core_pack は常時 APPROVED
        if self._is_core_pack(pack_id) or self._is_system_pack(pack_id):
            return PackStatus.APPROVED

        with self._lock:
            approval = self._approvals.get(pack_id)
            return approval.status if approval else None
    
    def get_approval(self, pack_id: str) -> Optional[PackApproval]:
        """承認情報を取得"""
        with self._lock:
            return self._approvals.get(pack_id)
    
    def get_pending_packs(self) -> List[str]:
        """承認待ちPackを取得"""
        with self._lock:
            return [
                pack_id for pack_id, approval in self._approvals.items()
                if approval.status in (PackStatus.INSTALLED, PackStatus.PENDING, PackStatus.MODIFIED)
            ]
    
    def is_pack_approved_and_verified(self, pack_id: str) -> tuple:
        """
        Packが承認済み+ハッシュ一致かチェック
        
        Returns:
            (is_valid: bool, reason: Optional[str])
            - is_valid: True = 承認済み+ハッシュ一致
            - reason: 不合格の場合の理由
        """
        # Core Packs and signed system-Pack descriptors bypass grants only after
        # their canonical roots have been verified.
        if self._is_core_pack(pack_id) or self._is_system_pack(pack_id):
            return True, None

        with self._lock:
            approval = self._approvals.get(pack_id)
            
            if approval is None:
                return False, "not_found"
            
            if approval.status == PackStatus.BLOCKED:
                return False, "blocked"
            
            if approval.status == PackStatus.MODIFIED:
                return False, "modified"
            
            if approval.status != PackStatus.APPROVED:
                return False, "not_approved"
        
        # ハッシュ検証（ロック外でファイルI/O）— キャッシュなしで検証（TOCTOU 緩和）
        if not self.verify_hash(pack_id, use_cache=False):
            return False, "hash_mismatch"
        
        return True, None

    def get_verified_pack_trust(
        self, pack_ids: Iterable[str]
    ) -> Dict[str, str]:
        """Return host-verified trust classes for authorized pack IDs.

        Only canonical shipped core and host-verified system Pack descriptors receive
        ``system`` trust. Other approved packs receive ``verified`` after the
        normal approval and hash-verification path succeeds.
        """
        verified: Dict[str, str] = {}
        for raw_pack_id in pack_ids:
            pack_id = str(raw_pack_id or "").strip()
            if not pack_id or pack_id in verified:
                continue
            if self._is_core_pack(pack_id) or self._is_system_pack(pack_id):
                verified[pack_id] = "system"
                continue
            if self.is_pack_approved_and_verified(pack_id)[0]:
                verified[pack_id] = "verified"
        return verified
    
    def approve(self, pack_id: str) -> ApprovalResult:
        """Packを承認"""
        with self._lock:
            if pack_id not in self._approvals:
                return ApprovalResult(success=False, pack_id=pack_id, error="Pack not found")
            
            # local_pack特殊処理
            if pack_id == LOCAL_PACK_ID:
                file_hashes = self._compute_local_pack_hashes()
            else:
                pack_dir = self._resolve_pack_dir(pack_id)
                if pack_dir is None or not pack_dir.exists():
                    return ApprovalResult(success=False, pack_id=pack_id, error="Pack directory not found")
                
                file_hashes = self._compute_pack_hashes(pack_dir)
            
            return self._persist_approved_snapshot(pack_id, file_hashes)

    def _persist_approved_snapshot(
        self, pack_id: str, file_hashes: Mapping[str, str]
    ) -> ApprovalResult:
        """Persist exactly the hashes already inspected by the caller.

        This method intentionally never scans the Pack.  In particular,
        delegated approval must not compare snapshot A and then accidentally
        approve a second scan B.
        """
        approval = self._approvals[pack_id]
        exact_hashes = {
            str(path): str(digest) for path, digest in sorted(file_hashes.items())
        }
        previous_hashes = dict(approval.file_hashes)
        approval.status = PackStatus.APPROVED
        approval.approved_at = self._now_ts()
        approval.file_hashes = exact_hashes
        approval.rejection_reason = None
        if approval.rule_approved and previous_hashes != exact_hashes:
            self._clear_rule_approval(approval)
        approval.version_history.append({
            "version": len(approval.version_history) + 1,
            "timestamp": approval.approved_at,
            "action": "approve",
            "file_hashes": dict(exact_hashes),
        })
        self._save_grant(approval)
        self._create_declared_stores(pack_id)
        try:
            eco_data = self._read_ecosystem_data(pack_id)
            if eco_data.get("host_execution", False) is True:
                logger.warning(
                    "SECURITY: Pack '%s' declares host_execution=true. "
                    "This pack will run directly on the host without Docker isolation.",
                    pack_id,
                )
                try:
                    from .audit_logger import get_audit_logger
                    get_audit_logger().log_security_event(
                        event_type="approve_host_execution_warning",
                        severity="warning",
                        description=f"Pack '{pack_id}' runs on host without Docker isolation",
                        pack_id=pack_id,
                    )
                except Exception:
                    pass
        except Exception:
            pass
        self._invalidate_hash_cache(pack_id)
        return ApprovalResult(
            success=True, pack_id=pack_id, status=PackStatus.APPROVED
        )

    @staticmethod
    def _pack_snapshot_digest(pack_id: str, file_hashes: Mapping[str, str]) -> str:
        """Return a path-free digest for one exact Pack content snapshot."""
        payload = {
            "pack_id": str(pack_id),
            "file_hashes": {
                str(path): str(digest)
                for path, digest in sorted(file_hashes.items())
            },
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get_pack_approval_snapshot(self, pack_id: str) -> dict[str, Any]:
        """Describe the exact Pack content a delegated approval would authorize."""
        with self._lock:
            file_hashes, error = self._exact_pack_hashes(pack_id)
            if error:
                return {"success": False, "error": error}
            status = self.get_status(pack_id)
            return {
                "success": True,
                "pack_id": pack_id,
                "status": status.value if status else "unknown",
                "snapshot_digest": self._pack_snapshot_digest(pack_id, file_hashes),
                "file_count": len(file_hashes),
            }

    def _exact_pack_hashes(
        self, pack_id: str
    ) -> tuple[Dict[str, str], Optional[str]]:
        if pack_id not in self._approvals:
            return {}, "Pack not found"
        if pack_id == LOCAL_PACK_ID:
            return self._compute_local_pack_hashes(), None
        pack_dir = self._resolve_pack_dir(pack_id)
        if pack_dir is None or not pack_dir.exists():
            return {}, "Pack directory not found"
        return self._compute_pack_hashes_nocache(pack_dir), None

    def approve_if_snapshot(self, pack_id: str, expected_digest: str) -> ApprovalResult:
        """Approve only if Pack contents still match the reviewed snapshot."""
        with self._lock:
            file_hashes, error = self._exact_pack_hashes(pack_id)
            if error:
                return ApprovalResult(
                    success=False,
                    pack_id=pack_id,
                    error=error,
                )
            snapshot_digest = self._pack_snapshot_digest(pack_id, file_hashes)
            if not hmac.compare_digest(
                snapshot_digest,
                str(expected_digest or ""),
            ):
                return ApprovalResult(
                    success=False,
                    pack_id=pack_id,
                    error="Pack contents changed after approval was requested",
                )
            self._invalidate_hash_cache(pack_id)
            result = self._persist_approved_snapshot(pack_id, file_hashes)
            if not result.success:
                return result

            # Detect a concurrent external writer in the compare→persist
            # interval.  The grant contains only the inspected hashes above;
            # it never adopts this verification scan.
            current_hashes, verify_error = self._exact_pack_hashes(pack_id)
            if verify_error or current_hashes != file_hashes:
                approval = self._approvals[pack_id]
                approval.status = PackStatus.MODIFIED
                approval.rejection_reason = (
                    "Pack contents changed while approval was being persisted"
                )
                self._save_grant(approval)
                self._invalidate_hash_cache(pack_id)
                return ApprovalResult(
                    success=False,
                    pack_id=pack_id,
                    error=approval.rejection_reason,
                    status=PackStatus.MODIFIED,
                )
            return result
    
    def approve_rule(self, pack_id: str) -> ApprovalResult:
        """rule Pack に対するルール拡張承認を実行する。

        通常の approve() が完了済みであること、かつ pack_type が "rule" で
        あることを前提とする。条件を満たさない場合はエラーを返す。

        Args:
            pack_id: Pack ID

        Returns:
            ApprovalResult
        """
        with self._lock:
            approval = self._approvals.get(pack_id)
            if not approval:
                return ApprovalResult(
                    success=False, pack_id=pack_id, error="Pack not found"
                )

            # 通常承認が完了していることを確認
            if approval.status != PackStatus.APPROVED:
                return ApprovalResult(
                    success=False,
                    pack_id=pack_id,
                    error=f"Pack must be approved first (current status: {approval.status.value})",
                )

            # pack_type が "rule" であることを確認
            eco_data = self._read_ecosystem_data(pack_id)
            pack_type = eco_data.get("pack_type", "application")
            if pack_type != "rule":
                return ApprovalResult(
                    success=False,
                    pack_id=pack_id,
                    error=f"approve_rule is only valid for pack_type 'rule', but pack_type is '{pack_type}'",
                )

            approval.rule_approved = True
            approval.rule_approved_at = self._now_ts()

            # バージョン履歴にルール拡張承認イベントを記録
            approval.version_history.append({
                "version": len(approval.version_history) + 1,
                "timestamp": approval.rule_approved_at,
                "action": "approve_rule",
                "file_hashes": dict(approval.file_hashes),
            })

            self._save_grant(approval)

        # audit log（ロック外）
        try:
            from .audit_logger import get_audit_logger
            get_audit_logger().log_security_event(
                event_type="pack_rule_approved",
                severity="warning",
                description=f"Rule pack '{pack_id}' rule-approved",
                pack_id=pack_id,
            )
        except Exception:
            pass

        return ApprovalResult(
            success=True, pack_id=pack_id, status=PackStatus.APPROVED
        )

    def is_rule_approved(self, pack_id: str) -> bool:
        """rule Pack がルール拡張承認済みかどうかを返す。

        pack_type が "rule" でない Pack に対しては常に True を返す
        （ルール拡張承認は不要なため）。

        Args:
            pack_id: Pack ID

        Returns:
            True ならルール拡張承認済み（または不要）、False なら未承認。
        """
        # core_pack は常に承認済み
        if self._is_core_pack(pack_id):
            return True

        eco_data = self._read_ecosystem_data(pack_id)
        pack_type = eco_data.get("pack_type", "application")
        if pack_type != "rule":
            return True  # rule でなければルール拡張承認は不要

        with self._lock:
            approval = self._approvals.get(pack_id)
            if not approval or not approval.rule_approved:
                return False
            return self._rule_approval_matches_current_hashes(approval)

    @staticmethod
    def _clear_rule_approval(approval: PackApproval) -> None:
        """Pack のルール拡張承認を取り消す。"""
        approval.rule_approved = False
        approval.rule_approved_at = None

    @staticmethod
    def _rule_approval_matches_current_hashes(approval: PackApproval) -> bool:
        """現在承認済みのハッシュに対して rule approval が付与済みか確認。"""
        for entry in reversed(approval.version_history):
            if entry.get("action") != "approve_rule":
                continue
            return entry.get("file_hashes") == approval.file_hashes
        return False

    def reject(self, pack_id: str, reason: str = "") -> ApprovalResult:
        """Packを拒否"""
        with self._lock:
            if pack_id not in self._approvals:
                return ApprovalResult(success=False, pack_id=pack_id, error="Pack not found")
            
            approval = self._approvals[pack_id]
            approval.status = PackStatus.BLOCKED
            approval.rejection_reason = reason
            self._clear_rule_approval(approval)
            
            self._save_grant(approval)
            
            # キャッシュ無効化
            self._invalidate_hash_cache(pack_id)
            
            return ApprovalResult(success=True, pack_id=pack_id, status=PackStatus.BLOCKED)
    
    def mark_modified(self, pack_id: str) -> None:
        """Packを変更済みとしてマーク（再承認必要）"""
        with self._lock:
            if pack_id in self._approvals:
                approval = self._approvals[pack_id]
                approval.status = PackStatus.MODIFIED
                self._clear_rule_approval(approval)
                self._save_grant(approval)
    
    # ------------------------------------------------------------------ #
    # Wave 1-1: ハッシュ粒度緩和 — ファイルごとのクリティカル判定
    # ------------------------------------------------------------------ #

    # セキュリティクリティカルファイル/ディレクトリ定義
    CRITICAL_FILES = frozenset({
        "ecosystem.json",
        "permissions.json",
        "routes.json",
        "backend/ecosystem.json",
        "backend/permissions.json",
        "backend/routes.json",
    })
    CRITICAL_DIRS = (
        "flows/",
        "lib/",
        "components/",
        "backend/flows/",
        "backend/lib/",
        "backend/components/",
    )

    def verify_hash_detailed(self, pack_id: str, use_cache: bool = True) -> Dict[str, Any]:
        """ファイルごとのハッシュ検証結果を返し、クリティカル/非クリティカルを判定する。

        Returns:
            {
                "valid": bool,            # 全ファイルが一致なら True
                "critical_changed": bool,  # クリティカルファイルに変更があるか
                "changed_files": list,     # 変更されたファイルのパスリスト
                "added_files": list,       # 追加されたファイルのパスリスト
                "removed_files": list,     # 削除されたファイルのパスリスト
            }
        """
        # core_pack はハッシュ検証不要
        if self._is_core_pack(pack_id) or self._is_system_pack(pack_id):
            return {
                "valid": True,
                "critical_changed": False,
                "changed_files": [],
                "added_files": [],
                "removed_files": [],
            }

        # ロック内で approval を取得
        with self._lock:
            approval = self._approvals.get(pack_id)
            if not approval or not approval.file_hashes:
                return {
                    "valid": False,
                    "critical_changed": True,
                    "changed_files": [],
                    "added_files": [],
                    "removed_files": [],
                }
            stored_hashes = self._normalize_hash_paths(approval.file_hashes)

        # ロック外でファイル I/O
        if pack_id == LOCAL_PACK_ID:
            current_hashes = self._normalize_hash_paths(self._compute_local_pack_hashes())
        else:
            pack_dir = self._resolve_pack_dir(pack_id)
            if pack_dir is None or not pack_dir.exists():
                return {
                    "valid": False,
                    "critical_changed": True,
                    "changed_files": [],
                    "added_files": [],
                    "removed_files": [],
                }
            if use_cache:
                current_hashes = self._normalize_hash_paths(self._compute_pack_hashes(pack_dir))
            else:
                current_hashes = self._normalize_hash_paths(self._compute_pack_hashes_nocache(pack_dir))

        stored_keys = set(stored_hashes.keys())
        current_keys = set(current_hashes.keys())

        removed_files = sorted(stored_keys - current_keys)
        added_files = sorted(current_keys - stored_keys)
        changed_files = sorted(
            p for p in (stored_keys & current_keys)
            if stored_hashes[p] != current_hashes[p]
        )

        valid = (not removed_files and not added_files and not changed_files)

        # --- クリティカル判定 ---
        critical_changed = False

        # ファイル削除は常にクリティカル
        if removed_files:
            critical_changed = True

        # クリティカルファイル/ディレクトリの変更チェック
        if not critical_changed:
            for fp in changed_files:
                if self._is_critical_path(fp):
                    critical_changed = True
                    break

        # ファイル追加: blocks/ 内のみ非クリティカル、それ以外はクリティカル
        if not critical_changed:
            for fp in added_files:
                if not fp.startswith("blocks/"):
                    critical_changed = True
                    break

        return {
            "valid": valid,
            "critical_changed": critical_changed,
            "changed_files": changed_files,
            "added_files": added_files,
            "removed_files": removed_files,
        }

    def _is_critical_path(self, file_path: str) -> bool:
        """ファイルパスがセキュリティクリティカルかどうかを判定する。"""
        file_path = self._normalize_hash_path(file_path)
        if file_path in self.CRITICAL_FILES:
            return True
        for critical_dir in self.CRITICAL_DIRS:
            if file_path.startswith(critical_dir):
                return True
        return False

    @staticmethod
    def _normalize_hash_path(file_path: str) -> str:
        return str(file_path or "").replace("\\", "/")

    @classmethod
    def _normalize_hash_paths(cls, file_hashes: Mapping[str, str]) -> Dict[str, str]:
        return {
            cls._normalize_hash_path(path): value
            for path, value in dict(file_hashes or {}).items()
        }

    def verify_hash(self, pack_id: str, use_cache: bool = True) -> bool:
        """Packのファイルハッシュを検証"""
        # W22-A: core_pack はハッシュ検証不要
        if self._is_core_pack(pack_id) or self._is_system_pack(pack_id):
            return True

        # ロック内でapprovalを取得
        with self._lock:
            approval = self._approvals.get(pack_id)
            if not approval or not approval.file_hashes:
                return False
            stored_hashes = dict(approval.file_hashes)  # コピー
        
        # ロック外でファイルI/O
        if pack_id == LOCAL_PACK_ID:
            current_hashes = self._compute_local_pack_hashes()
        else:
            pack_dir = self._resolve_pack_dir(pack_id)
            if pack_dir is None or not pack_dir.exists():
                return False
            if use_cache:
                current_hashes = self._compute_pack_hashes(pack_dir)
            else:
                current_hashes = self._compute_pack_hashes_nocache(pack_dir)
        
        # 比較
        if set(current_hashes.keys()) != set(stored_hashes.keys()):
            return False
        
        for path, hash_value in stored_hashes.items():
            if current_hashes.get(path) != hash_value:
                return False
        
        return True
    
    def _compute_pack_hashes(self, pack_dir: Path) -> Dict[str, str]:
        """Packの全ファイルのハッシュを計算（TTLキャッシュ付き）"""
        cache_key = str(pack_dir.resolve())
        now = time.monotonic()
        
        # キャッシュチェック
        cached = self._hash_cache.get(cache_key)
        if cached is not None:
            cached_result, cached_time = cached
            if now - cached_time < self._hash_cache_ttl:
                return dict(cached_result)
        
        # 計算
        hashes = {}
        
        for file_path in pack_dir.rglob("*"):
            if file_path.is_file():
                if self._should_skip_hash_file(file_path, pack_dir):
                    continue
                
                relative_path = file_path.relative_to(pack_dir).as_posix()
                hash_value = self._compute_file_hash(file_path)
                hashes[relative_path] = hash_value
        
        # キャッシュ保存
        self._hash_cache[cache_key] = (hashes, now)
        
        return hashes

    def _compute_pack_hashes_nocache(self, pack_dir: Path) -> Dict[str, str]:
        """Packの全ファイルのハッシュを計算（キャッシュを参照・更新しない）"""
        hashes = {}

        for file_path in pack_dir.rglob("*"):
            if file_path.is_file():
                if self._should_skip_hash_file(file_path, pack_dir):
                    continue

                relative_path = file_path.relative_to(pack_dir).as_posix()
                hash_value = self._compute_file_hash(file_path)
                hashes[relative_path] = hash_value

        return hashes

    @staticmethod
    def _should_skip_hash_file(file_path: Path, base_dir: Path | None = None) -> bool:
        try:
            path_parts = file_path.relative_to(base_dir).parts if base_dir is not None else file_path.parts
        except ValueError:
            path_parts = file_path.parts
        parts = set(path_parts)
        if parts & {"__pycache__", ".git", "user_data"}:
            return True
        name = file_path.name
        return name.endswith(".pyc") or name == ".DS_Store"
    
    def _compute_file_hash(self, path: Path) -> str:
        """ファイルのSHA-256ハッシュを計算"""
        sha256 = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return f"sha256:{sha256.hexdigest()}"
    
    def remove_approval(self, pack_id: str) -> bool:
        """承認情報を削除"""
        with self._lock:
            if pack_id in self._approvals:
                del self._approvals[pack_id]
                
                grant_file = self.grants_dir / f"{pack_id}.grants.json"
                grants_dir_resolved = self.grants_dir.resolve()
                grant_file_resolved = grant_file.resolve()
                try:
                    grant_file_resolved.relative_to(grants_dir_resolved)
                except ValueError:
                    logger.error(
                        "Path traversal detected in remove_approval for pack_id '%s'",
                        pack_id,
                    )
                    return False
                if grant_file.exists():
                    grant_file.unlink()
                
                return True
            return False
    
    def get_approved_pack_ids(self) -> Set[str]:
        """承認済み+ハッシュ一致のpack_idセットを取得"""
        approved_packs = set()
        with self._lock:
            for pack_id, approval in self._approvals.items():
                if approval.status == PackStatus.APPROVED:
                    approved_packs.add(pack_id)
            approved_packs.update(
                str(descriptor.get("pack_id") or "").strip()
                for descriptor in self._system_pack_descriptors
                if self._is_system_pack(str(descriptor.get("pack_id") or ""))
            )
        
        # ハッシュ検証（ロック外）
        verified_packs = set()
        for pack_id in approved_packs:
            if self.verify_hash(pack_id):
                verified_packs.add(pack_id)
        
        return verified_packs

    # ------------------------------------------------------------------ #
    # G-2: apply_update
    # ------------------------------------------------------------------ #

    def apply_update(self, pack_id: str, new_hashes: Dict[str, str]) -> ApprovalResult:
        """
        Pack更新を適用する。

        new_hashes が現在の承認済みハッシュと一致すれば APPROVED を維持。
        不一致の場合は MODIFIED に変更し再承認を要求。
        結果は audit log に記録される。

        Args:
            pack_id: Pack ID
            new_hashes: 更新後のファイルハッシュ

        Returns:
            ApprovalResult
        """
        with self._lock:
            approval = self._approvals.get(pack_id)
            if not approval:
                return ApprovalResult(success=False, pack_id=pack_id, error="Pack not found")

            old_hashes = dict(approval.file_hashes)
            hashes_match = (old_hashes == new_hashes)

            if hashes_match:
                # ハッシュ一致: APPROVED 維持
                result_status = approval.status
            else:
                # ハッシュ不一致: MODIFIED → 要再承認
                approval.status = PackStatus.MODIFIED
                approval.file_hashes = dict(new_hashes)
                result_status = PackStatus.MODIFIED

                # G-3: バージョン履歴に update イベントを記録
                approval.version_history.append({
                    "version": len(approval.version_history) + 1,
                    "timestamp": self._now_ts(),
                    "action": "update_modified",
                    "file_hashes": dict(new_hashes),
                })

                self._save_grant(approval)

            # キャッシュ無効化
            self._invalidate_hash_cache(pack_id)

        # audit log（ロック外）
        try:
            from .audit_logger import get_audit_logger
            get_audit_logger().log_security_event(
                event_type="pack_update_applied",
                severity="info" if hashes_match else "warning",
                description=(
                    f"Pack update applied: hashes {'match' if hashes_match else 'mismatch'}"
                ),
                pack_id=pack_id,
                details={
                    "hashes_match": hashes_match,
                    "result_status": result_status.value,
                },
            )
        except Exception:
            pass

        return ApprovalResult(
            success=True, pack_id=pack_id, status=result_status,
        )

    # ------------------------------------------------------------------ #
    # G-3: version history / rollback
    # ------------------------------------------------------------------ #

    def get_version_history(self, pack_id: str) -> List[Dict[str, Any]]:
        """Pack のバージョン履歴を取得する。"""
        with self._lock:
            approval = self._approvals.get(pack_id)
            if not approval:
                return []
            return list(approval.version_history)

    def rollback_to_version(self, pack_id: str, version_index: int) -> ApprovalResult:
        """
        指定バージョンのハッシュで再承認する。

        現在のファイルハッシュが target_hashes と一致する場合のみ APPROVED に変更。
        不一致の場合は失敗を返す（ファイルを先に復元する必要がある）。

        Args:
            pack_id: Pack ID
            version_index: version_history 内のインデックス（0始まり）

        Returns:
            ApprovalResult
        """
        # Phase 1: ロック内で approval と target_hashes を取得
        with self._lock:
            approval = self._approvals.get(pack_id)
            if not approval:
                return ApprovalResult(
                    success=False, pack_id=pack_id, error="Pack not found",
                )

            if version_index < 0 or version_index >= len(approval.version_history):
                return ApprovalResult(
                    success=False, pack_id=pack_id,
                    error=f"Invalid version index: {version_index}",
                )

            target_version = approval.version_history[version_index]
            target_hashes = target_version.get("file_hashes", {})

        # Phase 2: ロック外で現在のファイルハッシュを計算し検証
        if pack_id == LOCAL_PACK_ID:
            current_hashes = self._compute_local_pack_hashes()
        else:
            pack_dir = self._resolve_pack_dir(pack_id)
            if pack_dir is None or not pack_dir.exists():
                return ApprovalResult(
                    success=False, pack_id=pack_id, error="Pack directory not found",
                )
            current_hashes = self._compute_pack_hashes_nocache(pack_dir)

        if current_hashes != target_hashes:
            return ApprovalResult(
                success=False, pack_id=pack_id,
                error="Current files do not match target version hashes. Restore files first.",
            )

        # Phase 3: ロック再取得して APPROVED に更新
        with self._lock:
            # ロック再取得後に approval が存在するか再確認
            approval = self._approvals.get(pack_id)
            if not approval:
                return ApprovalResult(
                    success=False, pack_id=pack_id, error="Pack not found",
                )

            approval.status = PackStatus.APPROVED
            approval.approved_at = self._now_ts()
            approval.file_hashes = dict(target_hashes)
            approval.rejection_reason = None

            # バージョン履歴にロールバックイベントを記録
            approval.version_history.append({
                "version": len(approval.version_history) + 1,
                "timestamp": approval.approved_at,
                "action": "rollback",
                "rollback_to_version_index": version_index,
                "file_hashes": dict(target_hashes),
            })

            self._save_grant(approval)

            # キャッシュ無効化
            self._invalidate_hash_cache(pack_id)

        # audit log（ロック外）
        try:
            from .audit_logger import get_audit_logger
            get_audit_logger().log_security_event(
                event_type="pack_version_rollback",
                severity="warning",
                description=f"Pack rolled back to version index {version_index}",
                pack_id=pack_id,
                details={"version_index": version_index},
            )
        except Exception:
            pass

        return ApprovalResult(
            success=True, pack_id=pack_id, status=PackStatus.APPROVED,
        )

    # ------------------------------------------------------------------ #
    # #62  宣言的Store作成
    # ------------------------------------------------------------------ #

    def _create_declared_stores(self, pack_id: str) -> None:
        """
        Pack の ecosystem.json にある stores 宣言を読み取り、
        StoreRegistry に登録する。local_pack はスキップ。
        """
        if pack_id == LOCAL_PACK_ID:
            return

        stores_decl = self._read_stores_declaration(pack_id)
        if not stores_decl:
            return

        try:
            from .store_registry import get_store_registry
            registry = get_store_registry()
            results = registry.create_store_for_pack(pack_id, stores_decl)

            for r in results:
                if not r.success and r.error:
                    self._audit_store_creation(pack_id, r.store_id, False, r.error)
                elif r.success:
                    self._audit_store_creation(pack_id, r.store_id, True, None)
        except Exception:
            pass


    def _read_ecosystem_data(self, pack_id: str) -> Dict[str, Any]:
        """
        Pack の ecosystem.json を読み取って返す。

        W26-HOTFIX: approve() 内の host_execution 警告で使用。
        見つからない場合やパースエラー時は空 dict を返す。
        """
        loc = self._pack_locations.get(pack_id)
        if loc is None:
            return {}
        try:
            with open(loc.ecosystem_json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _read_stores_declaration(self, pack_id: str) -> List[Dict[str, Any]]:
        """ecosystem.json から stores フィールドを読み取る"""
        loc = self._pack_locations.get(pack_id)
        if loc is None:
            return []
        try:
            with open(loc.ecosystem_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("stores", [])
        except Exception:
            return []

    def _audit_store_creation(
        self, pack_id: str, store_id: str, success: bool, error: Optional[str]
    ) -> None:
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_system_event(
                event_type="declarative_store_creation",
                success=success,
                details={
                    "pack_id": pack_id,
                    "store_id": store_id or "",
                    "error": error,
                },
            )
        except Exception:
            pass



# グローバル変数（後方互換のため残存。DI コンテナ優先）
_global_approval_manager: Optional[ApprovalManager] = None
_am_lock = threading.Lock()


def get_approval_manager() -> ApprovalManager:
    """
    グローバルな ApprovalManager を取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。

    Returns:
        ApprovalManager インスタンス
    """
    from .di_container import get_container
    return get_container().get("approval_manager")


def initialize_approval_manager(
    packs_dir: str = ECOSYSTEM_DIR,
    grants_dir: str = GRANTS_DIR,
) -> ApprovalManager:
    """
    ApprovalManager を特定の引数で初期化する。

    新しいインスタンスを生成・初期化し、DI コンテナのキャッシュを置き換える。

    Args:
        packs_dir: Pack ディレクトリ（省略時はデフォルト）
        grants_dir: Grant ファイルの保存ディレクトリ（省略時はデフォルト）

    Returns:
        初期化済み ApprovalManager インスタンス
    """
    global _global_approval_manager
    with _am_lock:
        _global_approval_manager = ApprovalManager(packs_dir=packs_dir, grants_dir=grants_dir)
        _global_approval_manager.initialize()
    # DI コンテナのキャッシュも更新（_am_lock の外で実行してデッドロック回避）
    from .di_container import get_container
    get_container().set_instance("approval_manager", _global_approval_manager)
    return _global_approval_manager
