"""
network_grant_manager.py - ネットワーク権限管理 (DI Container 対応)

Pack単位でのネットワークアクセス許可を管理する。
allowed_domains / allowed_ports による制御と、
Modified検出時の自動無効化を実装。

設計原則:
- Pack単位でのGrant(運用を簡単に)
- ModifiedなPackは自動的にネットワーク権限を失う
- HMAC署名で改ざん検知
- 監査ログに全ての判定を記録
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .hmac_key_manager import (
    generate_or_load_signing_key,
    compute_data_hmac,
    verify_data_hmac,
)


logger = logging.getLogger(__name__)


@dataclass
class NetworkGrant:
    """ネットワーク権限Grant"""
    pack_id: str
    enabled: bool
    allowed_domains: List[str]
    allowed_ports: List[int]
    granted_at: str
    updated_at: str
    granted_by: str = "system"
    notes: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "enabled": self.enabled,
            "allowed_domains": self.allowed_domains,
            "allowed_ports": self.allowed_ports,
            "granted_at": self.granted_at,
            "updated_at": self.updated_at,
            "granted_by": self.granted_by,
            "notes": self.notes,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'NetworkGrant':
        return cls(
            pack_id=data.get("pack_id", ""),
            enabled=data.get("enabled", False),
            allowed_domains=data.get("allowed_domains", []),
            allowed_ports=data.get("allowed_ports", []),
            granted_at=data.get("granted_at", ""),
            updated_at=data.get("updated_at", ""),
            granted_by=data.get("granted_by", "system"),
            notes=data.get("notes", ""),
        )


@dataclass
class NetworkCheckResult:
    """ネットワークアクセスチェック結果"""
    allowed: bool
    reason: str
    pack_id: str
    domain: Optional[str] = None
    port: Optional[int] = None
    grant: Optional[NetworkGrant] = None


class NetworkGrantManager:
    """
    ネットワーク権限Grant管理
    
    user_data/permissions/network/{pack_id}.json でGrant情報を永続化。
    """
    
    GRANTS_DIR = "user_data/permissions/network"
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
            self._grants_dir = USER_DATA_DIR / "permissions" / "network"
        if secret_key:
            self._secret_key: bytes = secret_key.encode("utf-8")
        else:
            from .paths import USER_DATA_DIR as _UDD
            self._secret_key = generate_or_load_signing_key(
                _UDD / "permissions" / ".secret_key",
            )
        self._grants: Dict[str, NetworkGrant] = {}
        self._disabled_packs: Set[str] = set()  # ModifiedでDisabledになったPack
        self._lock = threading.RLock()
        
        self._ensure_dir()
        self._load_all_grants()
    
    def _now_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    def _ensure_dir(self) -> None:
        """ディレクトリを作成"""
        self._grants_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_grant_file(self, pack_id: str) -> Path:
        """Pack IDからGrantファイルパスを取得"""
        safe_id = pack_id.replace("/", "_").replace(":", "_")
        return self._grants_dir / f"{safe_id}.json"
    
    def _load_all_grants(self) -> None:
        """全Grantをロード"""
        with self._lock:
            self._grants.clear()
            
            if not self._grants_dir.exists():
                return
            
            for grant_file in self._grants_dir.glob("*.json"):
                try:
                    self._load_grant_file(grant_file)
                except Exception as e:
                    print(f"[NetworkGrantManager] Failed to load {grant_file}: {e}")
    
    def _load_grant_file(self, file_path: Path) -> Optional[NetworkGrant]:
        """単一のGrantファイルをロード"""
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # HMAC検証（署名なしファイルも改ざん扱いで拒否）
        stored_sig = data.pop("_hmac_signature", None)
        if not stored_sig:
            pack_id = data.get("pack_id", file_path.stem)
            print(f"[NetworkGrantManager] Missing HMAC signature for {file_path}")
            self._disabled_packs.add(pack_id)
            return None

        if not verify_data_hmac(self._secret_key, data, stored_sig):
            print(f"[NetworkGrantManager] HMAC verification failed for {file_path}")
            pack_id = data.get("pack_id", file_path.stem)
            self._disabled_packs.add(pack_id)
            return None
        
        grant = NetworkGrant.from_dict(data)
        self._grants[grant.pack_id] = grant
        return grant
    
    def _save_grant(self, grant: NetworkGrant) -> bool:
        """Grantを保存"""
        try:
            data = grant.to_dict()
            data["_hmac_signature"] = compute_data_hmac(self._secret_key, data)
            
            file_path = self._get_grant_file(grant.pack_id)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            return True
        except Exception as e:
            print(f"[NetworkGrantManager] Failed to save grant for {grant.pack_id}: {e}")
            return False
    
    def grant_network_access(
        self,
        pack_id: str,
        allowed_domains: List[str],
        allowed_ports: List[int],
        granted_by: str = "user",
        notes: str = ""
    ) -> NetworkGrant:
        """ネットワークアクセスを許可"""
        with self._lock:
            # #31: ワイルドカードドメイン警告
            if "*" in allowed_domains:
                logger.warning(
                    "Wildcard domain '*' granted for pack '%s' by '%s'. "
                    "This allows unrestricted domain access.",
                    pack_id, granted_by,
                )
                self._log_grant_event(pack_id, "wildcard_domain_warning", True, {
                    "allowed_domains": allowed_domains,
                    "granted_by": granted_by,
                    "severity": "warning",
                    "message": "Wildcard domain '*' grants unrestricted domain access",
                })

            now = self._now_ts()
            
            existing = self._grants.get(pack_id)
            if existing:
                grant = NetworkGrant(
                    pack_id=pack_id,
                    enabled=True,
                    allowed_domains=allowed_domains,
                    allowed_ports=allowed_ports,
                    granted_at=existing.granted_at,
                    updated_at=now,
                    granted_by=granted_by,
                    notes=notes,
                )
            else:
                grant = NetworkGrant(
                    pack_id=pack_id,
                    enabled=True,
                    allowed_domains=allowed_domains,
                    allowed_ports=allowed_ports,
                    granted_at=now,
                    updated_at=now,
                    granted_by=granted_by,
                    notes=notes,
                )
            
            self._grants[pack_id] = grant
            self._disabled_packs.discard(pack_id)
            self._save_grant(grant)
            
            self._log_grant_event(pack_id, "grant", True, {
                "allowed_domains": allowed_domains,
                "allowed_ports": allowed_ports,
                "granted_by": granted_by,
            })
            
            return grant
    
    def revoke_network_access(self, pack_id: str, reason: str = "") -> bool:
        """ネットワークアクセスを取り消し"""
        with self._lock:
            grant = self._grants.get(pack_id)
            if not grant:
                return False
            
            grant.enabled = False
            grant.updated_at = self._now_ts()
            grant.notes = reason or grant.notes
            
            self._save_grant(grant)
            self._log_grant_event(pack_id, "revoke", True, {"reason": reason})
            
            return True
    
    def disable_for_modified(self, pack_id: str) -> None:
        """ModifiedなPackのネットワークアクセスを無効化"""
        with self._lock:
            self._disabled_packs.add(pack_id)
            self._log_grant_event(pack_id, "disable_modified", True, {
                "reason": "Pack has been modified since approval"
            })
    
    def enable_after_reapproval(self, pack_id: str) -> None:
        """再承認後にネットワークアクセスを再有効化"""
        with self._lock:
            self._disabled_packs.discard(pack_id)
            self._log_grant_event(pack_id, "enable_reapproval", True, {
                "reason": "Pack re-approved"
            })
    
    def check_access(
        self,
        pack_id: str,
        domain: str,
        port: int
    ) -> NetworkCheckResult:
        """ネットワークアクセスをチェック"""
        with self._lock:
            if pack_id in self._disabled_packs:
                result = NetworkCheckResult(
                    allowed=False,
                    reason="Pack is disabled due to modification",
                    pack_id=pack_id,
                    domain=domain,
                    port=port,
                )
                self._log_access_check(result)
                return result
            
            grant = self._grants.get(pack_id)
            if not grant:
                result = NetworkCheckResult(
                    allowed=False,
                    reason="No network grant for this pack",
                    pack_id=pack_id,
                    domain=domain,
                    port=port,
                )
                self._log_access_check(result)
                return result
            
            if not grant.enabled:
                result = NetworkCheckResult(
                    allowed=False,
                    reason="Network grant is disabled",
                    pack_id=pack_id,
                    domain=domain,
                    port=port,
                    grant=grant,
                )
                self._log_access_check(result)
                return result
            
            domain_allowed = self._check_domain(domain, grant.allowed_domains)
            if not domain_allowed:
                result = NetworkCheckResult(
                    allowed=False,
                    reason=f"Domain '{domain}' not in allowed list",
                    pack_id=pack_id,
                    domain=domain,
                    port=port,
                    grant=grant,
                )
                self._log_access_check(result)
                return result
            
            port_allowed = self._check_port(port, grant.allowed_ports)
            if not port_allowed:
                result = NetworkCheckResult(
                    allowed=False,
                    reason=f"Port {port} not in allowed list",
                    pack_id=pack_id,
                    domain=domain,
                    port=port,
                    grant=grant,
                )
                self._log_access_check(result)
                return result
            
            result = NetworkCheckResult(
                allowed=True,
                reason="Access granted",
                pack_id=pack_id,
                domain=domain,
                port=port,
                grant=grant,
            )
            self._log_access_check(result)
            return result
    
    def _check_domain(self, domain: str, allowed: List[str]) -> bool:
        """ドメインが許可リストに含まれるかチェック"""
        if not allowed:
            # Security: 空リスト = 全拒否（何も許可しない）
            # 全ドメイン許可が必要な場合は ["*"] を明示的に指定すること
            return False
        
        domain_lower = domain.lower()
        
        for pattern in allowed:
            pattern_lower = pattern.lower()
            
            # 完全一致
            if domain_lower == pattern_lower:
                return True

            # ワイルドカード("*") = 全ドメイン許可（明示的な全許可）
            if pattern_lower == "*":
                return True
            
            # ワイルドカード(*.example.com)
            if pattern_lower.startswith("*."):
                base_domain = pattern_lower[2:]  # example.com
                # *.example.com は example.com 自体も許可
                if domain_lower == base_domain:
                    return True
                # サブドメインも許可
                if domain_lower.endswith("." + base_domain):
                    return True
        
        return False
    
    def _check_port(self, port: int, allowed: List[int]) -> bool:
        """ポートが許可リストに含まれるかチェック"""
        if not allowed:
            # Security: 空リスト = 全拒否（何も許可しない）
            # 全ポート許可が必要な場合は [0] を明示的に指定すること
            return False
        if 0 in allowed:
            return True
        return port in allowed
    
    def _log_grant_event(self, pack_id: str, action: str, success: bool, details: Dict[str, Any]) -> None:
        """Grant操作を監査ログに記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            audit.log_permission_event(
                pack_id=pack_id,
                permission_type="network",
                action=action,
                success=success,
                details=details
            )
        except Exception:
            pass
    
    def _log_access_check(self, result: NetworkCheckResult) -> None:
        """アクセスチェックを監査ログに記録"""
        try:
            from .audit_logger import get_audit_logger
            audit = get_audit_logger()
            
            # 許可/拒否を明確に記録
            audit.log_network_event(
                pack_id=result.pack_id,
                domain=result.domain or "",
                port=result.port or 0,
                allowed=result.allowed,
                reason=result.reason if not result.allowed else None,
                request_details={
                    "check_type": "access_check",
                    "grant_enabled": result.grant.enabled if result.grant else None,
                    "grant_domains": result.grant.allowed_domains if result.grant else None,
                    "grant_ports": result.grant.allowed_ports if result.grant else None,
                }
            )
        except Exception:
            pass
    
    def get_grant(self, pack_id: str) -> Optional[NetworkGrant]:
        """Grantを取得"""
        with self._lock:
            return self._grants.get(pack_id)
    
    def get_all_grants(self) -> Dict[str, NetworkGrant]:
        """全Grantを取得"""
        with self._lock:
            return dict(self._grants)
    
    def get_disabled_packs(self) -> Set[str]:
        """無効化されたPackを取得"""
        with self._lock:
            return set(self._disabled_packs)
    
    def is_pack_network_enabled(self, pack_id: str) -> bool:
        """Packのネットワークが有効かチェック"""
        with self._lock:
            if pack_id in self._disabled_packs:
                return False
            grant = self._grants.get(pack_id)
            return grant is not None and grant.enabled
    
    def delete_grant(self, pack_id: str) -> bool:
        """Grantを削除"""
        with self._lock:
            if pack_id not in self._grants:
                return False
            
            del self._grants[pack_id]
            
            file_path = self._get_grant_file(pack_id)
            if file_path.exists():
                file_path.unlink()
            
            self._log_grant_event(pack_id, "delete", True, {})
            return True


def get_network_grant_manager() -> NetworkGrantManager:
    """
    グローバルな NetworkGrantManager を取得する。

    DI コンテナ経由で遅延初期化・キャッシュされる。

    Returns:
        NetworkGrantManager インスタンス
    """
    from .di_container import get_container
    return get_container().get("network_grant_manager")


def reset_network_grant_manager(
    grants_dir: Optional[str] = None,
) -> NetworkGrantManager:
    """
    NetworkGrantManager をリセットする（テスト用）。

    新しいインスタンスを生成し、DI コンテナのキャッシュを置き換える。

    Args:
        grants_dir: Grant ファイルの保存ディレクトリ（省略時はデフォルト）

    Returns:
        新しい NetworkGrantManager インスタンス
    """
    from .di_container import get_container
    new_instance = NetworkGrantManager(grants_dir)
    container = get_container()
    container.set_instance("network_grant_manager", new_instance)
    return new_instance
