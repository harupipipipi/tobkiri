"""
アクティブエコシステム管理

現在使用中のPackとコンポーネントのオーバーライド設定を管理する。
"""

import json
import copy
import os
import threading
from pathlib import Path
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, field, asdict

from .mounts import get_mount_path
import logging
from core_runtime.hmac_key_manager import (
    generate_or_load_signing_key,
    compute_data_hmac,
    verify_data_hmac,
)

logger = logging.getLogger(__name__)
_REQUIRE_HMAC_DEFAULT = "1"


@dataclass
class ActiveEcosystemConfig:
    """アクティブエコシステム設定"""
    active_pack_identity: Optional[str]
    overrides: Dict[str, str] = field(default_factory=dict)
    disabled_components: List[str] = field(default_factory=list)
    disabled_addons: List[str] = field(default_factory=list)
    interface_overrides: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ActiveEcosystemConfig':
        return cls(
            active_pack_identity=data.get('active_pack_identity'),
            overrides=data.get('overrides', {}),
            disabled_components=data.get('disabled_components', []),
            disabled_addons=data.get('disabled_addons', []),
            interface_overrides=data.get('interface_overrides', {}),
            metadata=data.get('metadata', {})
        )


# デフォルト設定
DEFAULT_CONFIG = ActiveEcosystemConfig(
    active_pack_identity=None,  # 公式は特定のPackを指定しない
    overrides={}                # 公式はオーバーライドを定義しない
)


class ActiveEcosystemManager:
    """
    アクティブエコシステム管理クラス
    
    user_data/active_ecosystem.json を読み書きし、
    現在使用中のPack/Componentを管理する。
    """
    
    def __init__(
        self,
        config_path: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        """
        Args:
            config_path: 設定ファイルのパス（省略時はuser_data/active_ecosystem.json）
            secret_key: HMAC署名鍵（省略時はファイルから読み込み）
        """
        if config_path:
            self.config_path = Path(config_path)
        else:
            settings_dir = get_mount_path("data.settings", ensure_exists=True)
            self.config_path = settings_dir.parent / "active_ecosystem.json"
        
        self._config: Optional[ActiveEcosystemConfig] = None
        self._lock = threading.Lock()
        
        # HMAC 署名鍵の読み込み
        if secret_key:
            self._secret_key: bytes = secret_key.encode("utf-8")
        else:
            self._secret_key = generate_or_load_signing_key(
                self._default_secret_key_path(),
            )
        
        # 設定を読み込み
        self._load_config()

    def _default_secret_key_path(self) -> Path:
        """Keep the signing key alongside active_ecosystem.json in the same user_data root."""
        return self.config_path.parent / "permissions" / ".secret_key"
    
    def _load_config(self):
        """設定ファイルを読み込む（HMAC 検証付き）"""
        with self._lock:
            if self.config_path.exists():
                try:
                    with open(self.config_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    stored_sig = data.pop("_hmac_signature", None)
                    if not stored_sig:
                        require_hmac = os.environ.get("RUMI_REQUIRE_HMAC", _REQUIRE_HMAC_DEFAULT) == "1"
                        if require_hmac:
                            logger.warning(
                                "Unsigned active_ecosystem config detected at %s, "
                                "falling back to defaults because HMAC is required",
                                self.config_path,
                            )
                            self._config = self._create_default_config()
                        else:
                            logger.warning(
                                "Unsigned active_ecosystem config detected at %s, "
                                "re-signing on next save",
                                self.config_path,
                            )
                            self._config = ActiveEcosystemConfig.from_dict(data)
                    elif not verify_data_hmac(self._secret_key, data, stored_sig):
                        logger.warning(
                            "HMAC verification failed for active_ecosystem "
                            "config at %s, falling back to defaults",
                            self.config_path,
                        )
                        self._config = self._create_default_config()
                    else:
                        self._config = ActiveEcosystemConfig.from_dict(data)
                except (json.JSONDecodeError, IOError) as e:
                    logger.warning("[ActiveEcosystem] 設定読み込みエラー: %s", e)
                    self._config = self._create_default_config()
            else:
                self._config = self._create_default_config()
                self._save_config_internal()
    
    def _create_default_config(self) -> ActiveEcosystemConfig:
        """デフォルト設定の新しいインスタンスを作成（公式は内容を定義しない）"""
        return ActiveEcosystemConfig(
            active_pack_identity=None,
            overrides={},
            disabled_components=[],
            disabled_addons=[],
            metadata={}
        )
    
    def _save_config_internal(self):
        """設定を保存（ロック内で呼び出す、HMAC 署名付き、アトミック書き込み）"""
        import tempfile
        import os as _os
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            data = self._require_config().to_dict()
            data["_hmac_signature"] = compute_data_hmac(self._secret_key, data)
            # PC-2 fix: アトミック書き込み — tmp ファイルに書いてから rename
            try:
                fd, tmp_path_str = tempfile.mkstemp(
                    dir=str(self.config_path.parent), suffix=".tmp", prefix=".active_eco_"
                )
                try:
                    with _os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                        f.flush()
                        _os.fsync(f.fileno())
                    Path(tmp_path_str).replace(self.config_path)
                except BaseException:
                    try:
                        _os.unlink(tmp_path_str)
                    except OSError:
                        pass
                    raise
            except Exception as _fallback_exc:
                # フォールバック: 直接書き込み
                logger.error(
                    "[ActiveEcosystem] Atomic write failed, falling back to direct write: %s",
                    _fallback_exc,
                )
                with open(self.config_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error("[ActiveEcosystem] 設定保存エラー: %s", e)

    def _require_config(self) -> ActiveEcosystemConfig:
        """Return the loaded configuration or fail closed if unavailable."""
        if self._config is None:
            raise RuntimeError("active ecosystem configuration is unavailable")
        return self._config
    
    def _save_config(self):
        """設定を保存"""
        with self._lock:
            self._save_config_internal()

    def hmac_status(self) -> str:
        if not self.config_path.exists():
            return "absent"
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stored_sig = data.pop("_hmac_signature", None)
            if not stored_sig:
                return "missing"
            return "signed" if verify_data_hmac(self._secret_key, data, stored_sig) else "invalid"
        except Exception:
            return "invalid"

    def migrate_hmac_signature(self) -> str:
        """署名なし active_ecosystem.json を署名付きで保存し直す。"""
        status = self.hmac_status()
        if status == "absent":
            return "absent"
        if status == "signed":
            return "already_signed"
        if status == "invalid":
            return "failed"
        with self._lock:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.pop("_hmac_signature", None)
            self._config = ActiveEcosystemConfig.from_dict(data)
            self._save_config_internal()
        return "migrated"
    
    @property
    def config(self) -> ActiveEcosystemConfig:
        """現在の設定を取得"""
        with self._lock:
            return copy.deepcopy(self._require_config())
    
    @property
    def active_pack_identity(self) -> Optional[str]:
        """アクティブなPack Identityを取得"""
        with self._lock:
            return self._require_config().active_pack_identity
    
    @active_pack_identity.setter
    def active_pack_identity(self, value: Optional[str]):
        """アクティブなPack Identityを設定"""
        with self._lock:
            self._require_config().active_pack_identity = value
            self._save_config_internal()


    def get_interface_override(self, interface_key: str) -> Optional[str]:
        """インターフェースキーのオーバーライドを取得"""
        with self._lock:
            return self._require_config().interface_overrides.get(interface_key)

    def set_interface_override(self, interface_key: str, pack_id: str):
        """インターフェースキーのオーバーライドを設定"""
        with self._lock:
            self._require_config().interface_overrides[interface_key] = pack_id
            self._save_config_internal()

    def remove_interface_override(self, interface_key: str) -> bool:
        """インターフェースオーバーライドを削除"""
        with self._lock:
            config = self._require_config()
            if interface_key in config.interface_overrides:
                del config.interface_overrides[interface_key]
                self._save_config_internal()
                return True
            return False

    def get_all_interface_overrides(self) -> Dict[str, str]:
        """すべてのインターフェースオーバーライドを取得"""
        with self._lock:
            return dict(self._require_config().interface_overrides)
    
    def get_override(self, component_type: str) -> Optional[str]:
        """
        コンポーネントタイプのオーバーライドを取得
        
        Args:
            component_type: コンポーネントタイプ
        
        Returns:
            オーバーライドされたコンポーネントID、または None
        """
        with self._lock:
            return self._require_config().overrides.get(component_type)
    
    def set_override(self, component_type: str, component_id: str):
        """
        コンポーネントタイプのオーバーライドを設定
        
        Args:
            component_type: コンポーネントタイプ
            component_id: 使用するコンポーネントID
        """
        with self._lock:
            self._require_config().overrides[component_type] = component_id
            self._save_config_internal()
    
    def remove_override(self, component_type: str) -> bool:
        """
        オーバーライドを削除
        
        Args:
            component_type: コンポーネントタイプ
        
        Returns:
            削除成功の可否
        """
        with self._lock:
            config = self._require_config()
            if component_type in config.overrides:
                del config.overrides[component_type]
                self._save_config_internal()
                return True
            return False
    
    def get_all_overrides(self) -> Dict[str, str]:
        """すべてのオーバーライドを取得"""
        with self._lock:
            return dict(self._require_config().overrides)
    
    def is_component_disabled(self, component_full_id: str) -> bool:
        """
        コンポーネントが無効化されているか確認
        
        Args:
            component_full_id: "pack_id:type:id" 形式
        
        Returns:
            無効化されている場合 True
        """
        with self._lock:
            return component_full_id in self._require_config().disabled_components
    
    def disable_component(self, component_full_id: str):
        """コンポーネントを無効化"""
        with self._lock:
            config = self._require_config()
            if component_full_id not in config.disabled_components:
                config.disabled_components.append(component_full_id)
                self._save_config_internal()
    
    def enable_component(self, component_full_id: str):
        """コンポーネントを有効化"""
        with self._lock:
            config = self._require_config()
            if component_full_id in config.disabled_components:
                config.disabled_components.remove(component_full_id)
                self._save_config_internal()
    
    def is_addon_disabled(self, addon_id: str) -> bool:
        """アドオンが無効化されているか確認"""
        with self._lock:
            return addon_id in self._require_config().disabled_addons
    
    def disable_addon(self, addon_id: str):
        """アドオンを無効化"""
        with self._lock:
            config = self._require_config()
            if addon_id not in config.disabled_addons:
                config.disabled_addons.append(addon_id)
                self._save_config_internal()
    
    def enable_addon(self, addon_id: str):
        """アドオンを有効化"""
        with self._lock:
            config = self._require_config()
            if addon_id in config.disabled_addons:
                config.disabled_addons.remove(addon_id)
                self._save_config_internal()
    
    def set_metadata(self, key: str, value: Any):
        """メタデータを設定"""
        with self._lock:
            self._require_config().metadata[key] = value
            self._save_config_internal()
    
    def get_metadata(self, key: str, default: Any = None) -> Any:
        """メタデータを取得"""
        with self._lock:
            return self._require_config().metadata.get(key, default)
    
    def reset_to_defaults(self):
        """デフォルト設定にリセット"""
        with self._lock:
            self._config = self._create_default_config()
            self._save_config_internal()
    
    def reload(self):
        """設定を再読み込み"""
        self._load_config()


# グローバルインスタンス
_global_manager: Optional[ActiveEcosystemManager] = None
_init_lock = threading.Lock()


def get_active_ecosystem_manager() -> ActiveEcosystemManager:
    """グローバルなActiveEcosystemManagerを取得"""
    global _global_manager
    
    if _global_manager is None:
        with _init_lock:
            if _global_manager is None:
                _global_manager = ActiveEcosystemManager()
    
    return _global_manager


def get_active_pack_identity() -> Optional[str]:
    """アクティブなPack Identityを取得（ショートカット）"""
    return get_active_ecosystem_manager().active_pack_identity


def get_component_override(component_type: str) -> Optional[str]:
    """コンポーネントオーバーライドを取得（ショートカット）"""
    return get_active_ecosystem_manager().get_override(component_type)
