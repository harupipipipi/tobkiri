"""
capability_grant_manager.py - Capability 権限 Grant 管理

principal_id × permission_id の Grant を管理する。
NetworkGrantManager と同じ HMAC 署名方式を採用。

設計原則:
- 1 principal 1 ファイル
- HMAC-SHA256 署名で改ざん検知
- principal_id のサニタイズ（パストラバーサル防止）
- 公式は permission_id の意味を解釈しない
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .compat import safe_chmod
from .authority.config_lattice import meet_authority_configs
from .hierarchical_grant import parse_principal_chain, intersect_config

_UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|.\x00-\x1f]')

BATCH_GRANT_MAX_ITEMS = 50


@dataclass
class BatchGrantResult:
    """batch_grant の結果"""
    success: bool
    results: List[Dict[str, Any]] = field(default_factory=list)
    granted_count: int = 0
    failed_count: int = 0



def sanitize_principal_id(principal_id: str) -> str:
    """principal_id をファイルシステム安全な文字列に変換"""
    return _UNSAFE_CHARS.sub("_", principal_id)


@dataclass
class CapabilityPermissionGrant:
    """単一 permission の grant 情報"""
    enabled: bool
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilityGrant:
    """principal 単位の grant"""
    principal_id: str
    enabled: bool
    granted_at: str
    updated_at: str
    permissions: Dict[str, CapabilityPermissionGrant] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": "1.0",
            "principal_id": self.principal_id,
            "enabled": self.enabled,
            "granted_at": self.granted_at,
            "updated_at": self.updated_at,
            "permissions": {
                pid: {"enabled": p.enabled, "config": p.config}
                for pid, p in self.permissions.items()
            },
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CapabilityGrant':
        permissions = {}
        for pid, pdata in data.get("permissions", {}).items():
            if isinstance(pdata, dict):
                permissions[pid] = CapabilityPermissionGrant(
                    enabled=pdata.get("enabled", False),
                    config=pdata.get("config", {}),
                )
        return cls(
            principal_id=data.get("principal_id", ""),
            enabled=data.get("enabled", False),
            granted_at=data.get("granted_at", ""),
            updated_at=data.get("updated_at", ""),
            permissions=permissions,
        )


@dataclass
class GrantCheckResult:
    """Grant チェック結果"""
    allowed: bool
    reason: str
    principal_id: str
    permission_id: str
    config: Dict[str, Any] = field(default_factory=dict)


class CapabilityGrantManager:
    """
    Capability Grant 管理
    
    user_data/permissions/capabilities/<safe_principal_id>.json で
    principal 単位の Grant を永続化する。
    """
    
    DEFAULT_GRANTS_DIR = "user_data/permissions/capabilities"
    SECRET_KEY_FILE = "user_data/permissions/.secret_key"
    
    def __init__(
        self,
        grants_dir: Optional[str] = None,
        secret_key: Optional[str] = None,
    ) -> None:
        if grants_dir:
            self._grants_dir = Path(grants_dir)
        else:
            from .paths import USER_DATA_DIR
            self._grants_dir = USER_DATA_DIR / "permissions" / "capabilities"
        self._secret_key = secret_key or self._load_or_create_secret_key()
        self._grants: Dict[str, CapabilityGrant] = {}
        self._tampered_principals: Set[str] = set()
        self._lock = threading.RLock()
        
        self._ensure_dir()
        self._load_all_grants()
    
    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    def _ensure_dir(self) -> None:
        """ディレクトリを作成"""
        self._grants_dir.mkdir(parents=True, exist_ok=True)
    
    def _load_or_create_secret_key(self) -> str:
        """NetworkGrantManager と同じ secret_key を流用"""
        from .paths import USER_DATA_DIR
        key_file = USER_DATA_DIR / "permissions" / ".secret_key"
        
        if key_file.exists():
            try:
                return key_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        
        key = hashlib.sha256(os.urandom(32)).hexdigest()
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key, encoding="utf-8")
        
        try:
            safe_chmod(key_file, 0o600)
        except (OSError, AttributeError):
            pass
        
        return key
    
    def _compute_hmac(self, data: Dict[str, Any]) -> str:
        """HMAC 署名を計算"""
        data_copy = {k: v for k, v in data.items() if not k.startswith("_hmac")}
        payload = json.dumps(data_copy, sort_keys=True, ensure_ascii=False)
        return hmac.new(
            self._secret_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    
    def _get_grant_file(self, principal_id: str) -> Path:
        """principal_id から Grant ファイルパスを取得"""
        return self._grants_dir / f"{sanitize_principal_id(principal_id)}.json"
    
    def _load_all_grants(self) -> None:
        """全 Grant をロード"""
        with self._lock:
            self._grants.clear()
            self._tampered_principals.clear()
            
            if not self._grants_dir.exists():
                return
            
            for grant_file in self._grants_dir.glob("*.json"):
                try:
                    self._load_grant_file(grant_file)
                except Exception:
                    pass
    
    def _load_grant_file(self, file_path: Path) -> Optional[CapabilityGrant]:
        """単一 Grant ファイルをロード"""
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # HMAC 検証（署名なしファイルも改ざん扱いで拒否）
        stored_sig = data.pop("_hmac_signature", None)
        if not stored_sig:
            principal_id = data.get("principal_id", file_path.stem)
            self._tampered_principals.add(principal_id)
            self._tampered_principals.add(sanitize_principal_id(principal_id))
            self._tampered_principals.add(file_path.stem)
            self._audit_tamper(principal_id, file_path)
            return None

        computed_sig = self._compute_hmac(data)
        if not hmac.compare_digest(stored_sig, computed_sig):
            principal_id = data.get("principal_id", file_path.stem)
            self._tampered_principals.add(principal_id)
            self._tampered_principals.add(sanitize_principal_id(principal_id))
            self._tampered_principals.add(file_path.stem)
            self._audit_tamper(principal_id, file_path)
            return None
        
        grant = CapabilityGrant.from_dict(data)
        if grant.principal_id:
            self._grants[grant.principal_id] = grant
        return grant
    
    def _audit_tamper(self, principal_id: str, file_path: Path) -> None:
        """改ざん検出を監査ログに記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_security_event(
                event_type="capability_grant_tampered",
                severity="critical",
                description=f"HMAC verification failed for capability grant: {principal_id}",
                details={
                    "principal_id": principal_id,
                    "file": str(file_path),
                },
            )
        except Exception:
            pass
    
    def _save_grant(self, grant: CapabilityGrant) -> bool:
        """Grant を保存"""
        try:
            data = grant.to_dict()
            data["_hmac_signature"] = self._compute_hmac(data)
            
            file_path = self._get_grant_file(grant.principal_id)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            return True
        except Exception:
            return False
    
    def check(self, principal_id: str, permission_id: str) -> GrantCheckResult:
        """
        Grant をチェック
        
        Args:
            principal_id: 主体ID（UDS由来）
            permission_id: 要求する権限ID
        
        Returns:
            GrantCheckResult
        """
        with self._lock:
            # 改ざん検出済みの principal は拒否（raw と sanitize 両方で判定）
            if (principal_id in self._tampered_principals
                    or sanitize_principal_id(principal_id) in self._tampered_principals):
                return GrantCheckResult(
                    allowed=False,
                    reason=f"Grant file for '{principal_id}' has been tampered with",
                    principal_id=principal_id,
                    permission_id=permission_id,
                )

            # 階層 principal チェーン（parent__child 形式に対応）
            chain = parse_principal_chain(principal_id)
            configs = []

            for ancestor_id in chain:
                # 改ざんチェック（各階層）
                if (ancestor_id in self._tampered_principals
                        or sanitize_principal_id(ancestor_id) in self._tampered_principals):
                    label = 'ancestor' if ancestor_id != principal_id else 'principal'
                    return GrantCheckResult(
                        allowed=False,
                        reason=f"Grant file for {label} '{ancestor_id}' has been tampered with",
                        principal_id=principal_id,
                        permission_id=permission_id,
                    )

                grant = self._grants.get(ancestor_id)
                label = 'ancestor' if ancestor_id != principal_id else 'principal'

                if grant is None:
                    return GrantCheckResult(
                        allowed=False,
                        reason=f"No capability grant for {label} '{ancestor_id}'",
                        principal_id=principal_id,
                        permission_id=permission_id,
                    )

                if not grant.enabled:
                    return GrantCheckResult(
                        allowed=False,
                        reason=f"Capability grant for {label} '{ancestor_id}' is disabled",
                        principal_id=principal_id,
                        permission_id=permission_id,
                    )

                perm = grant.permissions.get(permission_id)
                if perm is None:
                    return GrantCheckResult(
                        allowed=False,
                        reason=f"Permission '{permission_id}' not granted to {label} '{ancestor_id}'",
                        principal_id=principal_id,
                        permission_id=permission_id,
                    )

                if not perm.enabled:
                    return GrantCheckResult(
                        allowed=False,
                        reason=f"Permission '{permission_id}' is disabled for {label} '{ancestor_id}'",
                        principal_id=principal_id,
                        permission_id=permission_id,
                    )

                configs.append(dict(perm.config))

            # 全階層 OK → config は intersection
            final_config = intersect_config(configs) if len(configs) > 1 else (configs[0] if configs else {})

            return GrantCheckResult(
                allowed=True,
                reason="Granted",
                principal_id=principal_id,
                permission_id=permission_id,
                config=final_config,
            )

    def check_authority(self, principal_id: str, permission_id: str) -> GrantCheckResult:
        """Check a grant using Authority v2 config lattice semantics.

        Unlike the legacy ``check`` path, missing config keys in a child grant do
        not erase constraints inherited from a parent grant.
        """
        with self._lock:
            if (principal_id in self._tampered_principals
                    or sanitize_principal_id(principal_id) in self._tampered_principals):
                return GrantCheckResult(
                    allowed=False,
                    reason=f"Grant file for '{principal_id}' has been tampered with",
                    principal_id=principal_id,
                    permission_id=permission_id,
                )

            configs = []
            for ancestor_id in parse_principal_chain(principal_id):
                if (ancestor_id in self._tampered_principals
                        or sanitize_principal_id(ancestor_id) in self._tampered_principals):
                    label = "ancestor" if ancestor_id != principal_id else "principal"
                    return GrantCheckResult(
                        allowed=False,
                        reason=f"Grant file for {label} '{ancestor_id}' has been tampered with",
                        principal_id=principal_id,
                        permission_id=permission_id,
                    )

                grant = self._grants.get(ancestor_id)
                label = "ancestor" if ancestor_id != principal_id else "principal"
                if grant is None:
                    return GrantCheckResult(
                        allowed=False,
                        reason=f"No capability grant for {label} '{ancestor_id}'",
                        principal_id=principal_id,
                        permission_id=permission_id,
                    )
                if not grant.enabled:
                    return GrantCheckResult(
                        allowed=False,
                        reason=f"Capability grant for {label} '{ancestor_id}' is disabled",
                        principal_id=principal_id,
                        permission_id=permission_id,
                    )
                perm = grant.permissions.get(permission_id)
                if perm is None:
                    return GrantCheckResult(
                        allowed=False,
                        reason=f"Permission '{permission_id}' not granted to {label} '{ancestor_id}'",
                        principal_id=principal_id,
                        permission_id=permission_id,
                    )
                if not perm.enabled:
                    return GrantCheckResult(
                        allowed=False,
                        reason=f"Permission '{permission_id}' is disabled for {label} '{ancestor_id}'",
                        principal_id=principal_id,
                        permission_id=permission_id,
                    )
                configs.append(dict(perm.config))

            try:
                final_config = meet_authority_configs(*configs)
            except ValueError as exc:
                return GrantCheckResult(
                    allowed=False,
                    reason=str(exc),
                    principal_id=principal_id,
                    permission_id=permission_id,
                )

            return GrantCheckResult(
                allowed=True,
                reason="Granted",
                principal_id=principal_id,
                permission_id=permission_id,
                config=final_config,
            )

    
    def grant_permission(
        self,
        principal_id: str,
        permission_id: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> CapabilityGrant:
        """permission を付与"""
        with self._lock:
            now = self._now_ts()
            grant = self._grants.get(principal_id)
            
            if grant is None:
                grant = CapabilityGrant(
                    principal_id=principal_id,
                    enabled=True,
                    granted_at=now,
                    updated_at=now,
                )
                self._grants[principal_id] = grant
            
            grant.updated_at = now
            grant.enabled = True
            grant.permissions[permission_id] = CapabilityPermissionGrant(
                enabled=True,
                config=config or {},
            )
            
            self._tampered_principals.discard(principal_id)  # raw
            self._tampered_principals.discard(sanitize_principal_id(principal_id))  # sanitized
            self._save_grant(grant)
            
            self._audit_grant_event(principal_id, permission_id, "grant", True)
            
            return grant
    
    def revoke_permission(
        self,
        principal_id: str,
        permission_id: str,
    ) -> bool:
        """permission を取り消し"""
        with self._lock:
            grant = self._grants.get(principal_id)
            if grant is None:
                return False
            
            perm = grant.permissions.get(permission_id)
            if perm is None:
                return False
            
            perm.enabled = False
            grant.updated_at = self._now_ts()
            self._save_grant(grant)
            
            self._audit_grant_event(principal_id, permission_id, "revoke", True)
            return True
    
    def revoke_all(self, principal_id: str) -> bool:
        """principal の全 permission を取り消し"""
        with self._lock:
            grant = self._grants.get(principal_id)
            if grant is None:
                return False
            
            grant.enabled = False
            grant.updated_at = self._now_ts()
            self._save_grant(grant)
            
            self._audit_grant_event(principal_id, "*", "revoke_all", True)
            return True
    

    def batch_grant(
        self,
        grants: List[Dict[str, Any]],
    ) -> BatchGrantResult:
        """
        複数の Grant 操作を一括で実行する (best-effort)。

        各要素は {"principal_id": str, "permission_id": str, "config": dict|None}。
        最大 BATCH_GRANT_MAX_ITEMS 件。個別失敗は結果に含め、他は続行する。

        Args:
            grants: Grant 操作のリスト

        Returns:
            BatchGrantResult
        """
        if not isinstance(grants, list):
            return BatchGrantResult(
                success=False,
                results=[],
                granted_count=0,
                failed_count=0,
            )

        if len(grants) > BATCH_GRANT_MAX_ITEMS:
            return BatchGrantResult(
                success=False,
                results=[{
                    "principal_id": "",
                    "permission_id": "",
                    "granted": False,
                    "error": f"Too many grants: {len(grants)} exceeds max {BATCH_GRANT_MAX_ITEMS}",
                }],
                granted_count=0,
                failed_count=1,
            )

        results: List[Dict[str, Any]] = []
        granted_count = 0
        failed_count = 0

        for item in grants:
            principal_id = item.get("principal_id", "")
            permission_id = item.get("permission_id", "")
            raw_config = item.get("config")
            config = raw_config if isinstance(raw_config, dict) else {}

            if not principal_id or not permission_id:
                results.append({
                    "principal_id": principal_id,
                    "permission_id": permission_id,
                    "granted": False,
                    "error": "Missing principal_id or permission_id",
                })
                failed_count += 1
                continue

            try:
                self.grant_permission(principal_id, permission_id, config)
                results.append({
                    "principal_id": principal_id,
                    "permission_id": permission_id,
                    "granted": True,
                    "error": None,
                })
                granted_count += 1
            except Exception as e:
                results.append({
                    "principal_id": principal_id,
                    "permission_id": permission_id,
                    "granted": False,
                    "error": str(e),
                })
                failed_count += 1

        return BatchGrantResult(
            success=True,
            results=results,
            granted_count=granted_count,
            failed_count=failed_count,
        )

    def get_grant(self, principal_id: str) -> Optional[CapabilityGrant]:
        """Grant を取得"""
        with self._lock:
            return self._grants.get(principal_id)
    
    def get_all_grants(self) -> Dict[str, CapabilityGrant]:
        """全 Grant を取得"""
        with self._lock:
            return dict(self._grants)
    
    def delete_grant(self, principal_id: str) -> bool:
        """Grant を削除"""
        with self._lock:
            if principal_id not in self._grants:
                return False
            
            del self._grants[principal_id]
            
            file_path = self._get_grant_file(principal_id)
            if file_path.exists():
                file_path.unlink()
            
            self._audit_grant_event(principal_id, "*", "delete", True)
            return True
    
    def _audit_grant_event(
        self, principal_id: str, permission_id: str, action: str, success: bool
    ) -> None:
        """Grant 操作を監査ログに記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_permission_event(
                pack_id=principal_id,
                permission_type="capability",
                action=action,
                success=success,
                details={
                    "principal_id": principal_id,
                    "permission_id": permission_id,
                },
            )
        except Exception:
            pass


# グローバルインスタンス
_global_grant_manager: Optional[CapabilityGrantManager] = None
_grant_lock = threading.Lock()


def get_capability_grant_manager() -> CapabilityGrantManager:
    """グローバルなCapabilityGrantManagerを取得"""
    global _global_grant_manager
    if _global_grant_manager is None:
        with _grant_lock:
            if _global_grant_manager is None:
                _global_grant_manager = CapabilityGrantManager()
    return _global_grant_manager


def reset_capability_grant_manager(
    grants_dir: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> CapabilityGrantManager:
    """リセット（テスト用）"""
    global _global_grant_manager
    with _grant_lock:
        _global_grant_manager = CapabilityGrantManager(grants_dir, secret_key)
    return _global_grant_manager
