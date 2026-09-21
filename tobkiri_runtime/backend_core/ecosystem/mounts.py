"""
マウント管理システム

データ保存先の抽象化レイヤーを提供する。
user_data/mounts.json でマウントポイントを設定可能。
"""

import os
import json
import logging
import threading
from pathlib import Path
from typing import Dict, Optional, Any

# W-2 fix: print() を logger に統一
logger = logging.getLogger(__name__)


# モジュール基準ベースディレクトリ
_BASE_DIR: Optional[Path]
try:
    from core_runtime.paths import BASE_DIR as _BASE_DIR
except ImportError:
    _BASE_DIR = None

# デフォルトのマウント設定
DEFAULT_MOUNTS = {
    # 汎用マウントのみ（具体的な用途名を定義しない）
    "data.user": "./user_data",
    "data.settings": "./user_data/settings",
    "data.cache": "./user_data/cache",
    # Legacy compatibility mounts kept for migration-safe upgrades.
    "data.chats": "./user_data/chats",
    "data.shared": "./user_data/shared",
}

# グローバルインスタンス（遅延初期化）
_global_mount_manager: Optional['MountManager'] = None
_init_lock = threading.Lock()


class MountManager:
    """
    マウントポイント管理クラス
    
    マウントポイントとは、論理的なデータ保存先（例: "data.chats"）を
    実際のファイルシステムパスにマッピングする仕組み。
    
    Example:
        manager = MountManager()
        chats_path = manager.get_path("data.chats")
        # -> Path("./user_data/chats")
        
        # カスタムパスに変更
        manager.set_mount("data.chats", "/mnt/nas/chats")
    """
    
    def __init__(
        self,
        config_path: Optional[str] = None,
        base_dir: Optional[str] = None
    ):
        """
        Args:
            config_path: マウント設定ファイルのパス
            base_dir: 相対パスの基準ディレクトリ（省略時はカレントディレクトリ）
        """
        configured_user_data = (
            os.environ.get("TOBKIRI_USER_DATA")
            or os.environ.get("RUMI_USER_DATA")
        )
        if base_dir is not None:
            resolved_base_dir = Path(base_dir)
        elif configured_user_data:
            # The default relative mounts are ``./user_data/...``.  Resolve
            # them next to the configured user-data root so desktop workers
            # never fall back to the read-only bundled app directory.
            resolved_base_dir = Path(configured_user_data).parent
        else:
            resolved_base_dir = _BASE_DIR if _BASE_DIR is not None else Path.cwd()

        if config_path is None:
            mounts_root = (
                Path(configured_user_data)
                if configured_user_data
                else resolved_base_dir / "user_data"
            )
            config_path = str(mounts_root / "mounts.json")
        self.config_path = Path(config_path)
        self.base_dir = resolved_base_dir
        if configured_user_data:
            user_data_root = Path(configured_user_data)
            self._default_mounts = {
                "data.user": str(user_data_root),
                "data.settings": str(user_data_root / "settings"),
                "data.cache": str(user_data_root / "cache"),
                "data.chats": str(user_data_root / "chats"),
                "data.shared": str(user_data_root / "shared"),
            }
        else:
            self._default_mounts = dict(DEFAULT_MOUNTS)
        self._mounts: Dict[str, str] = {}
        self._lock = threading.Lock()
        
        # 設定を読み込み
        self._load_config()
    
    def _load_config(self):
        """設定ファイルを読み込む"""
        with self._lock:
            if self.config_path.exists():
                try:
                    with open(self.config_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    self._mounts = data.get("mounts", {})
                except (json.JSONDecodeError, IOError) as e:
                    logger.warning("[MountManager] 設定ファイル読み込みエラー: %s", e)
                    self._mounts = {}
            
            # デフォルト値で補完
            for key, default_path in self._default_mounts.items():
                if key not in self._mounts:
                    self._mounts[key] = default_path
    
    def _save_config_internal(self):
        """設定ファイルを保存（ロック保持状態で呼び出す内部用）"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "version": "1.0",
            "mounts": self._mounts
        }
        
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error("[MountManager] 設定ファイル保存エラー: %s", e)
    
    def _save_config(self):
        """設定ファイルを保存"""
        with self._lock:
            self._save_config_internal()
    
    def get_path(self, mount_point: str, ensure_exists: bool = True) -> Path:
        """
        マウントポイントの実際のパスを取得
        
        Args:
            mount_point: マウントポイント名（例: "data.chats"）
            ensure_exists: Trueの場合、ディレクトリが存在しなければ作成
        
        Returns:
            実際のファイルシステムパス
        
        Raises:
            KeyError: 未定義のマウントポイントの場合
            ValueError: パストラバーサルが検出された場合
        """
        with self._lock:
            if mount_point not in self._mounts:
                raise KeyError(f"未定義のマウントポイント: {mount_point}")
            
            raw_path = self._mounts[mount_point]
        
        # 相対パスの場合はbase_dirを基準に解決
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.base_dir / path
        
        path = path.resolve()
        
        # PC-3 fix: パストラバーサル防御 — base_dir 配下であることを検証
        base_resolved = self.base_dir.resolve()
        try:
            path.relative_to(base_resolved)
        except ValueError:
            raise ValueError(
                f"パストラバーサルが検出されました: マウントポイント '{mount_point}' の "
                f"パス '{raw_path}' が base_dir '{base_resolved}' の外を参照しています"
            )
        
        # ディレクトリを作成
        if ensure_exists:
            path.mkdir(parents=True, exist_ok=True)
        
        return path
    
    def set_mount(self, mount_point: str, path: str, save: bool = True):
        """
        マウントポイントを設定
        
        Args:
            mount_point: マウントポイント名
            path: 実際のパス
            save: 設定ファイルに保存するかどうか
        """
        with self._lock:
            self._mounts[mount_point] = path
            if save:
                self._save_config_internal()
    
    def get_all_mounts(self) -> Dict[str, str]:
        """すべてのマウント設定を取得"""
        with self._lock:
            return dict(self._mounts)
    
    def has_mount(self, mount_point: str) -> bool:
        """マウントポイントが定義されているか確認"""
        with self._lock:
            return mount_point in self._mounts
    
    def remove_mount(self, mount_point: str, save: bool = True) -> bool:
        """
        マウントポイントを削除
        
        Args:
            mount_point: マウントポイント名
            save: 設定ファイルに保存するかどうか
        
        Returns:
            削除成功の可否
        """
        with self._lock:
            if mount_point in self._mounts:
                del self._mounts[mount_point]
                if save:
                    self._save_config_internal()
                return True
        return False
    
    def reset_to_defaults(self, save: bool = True):
        """デフォルト設定にリセット"""
        with self._lock:
            self._mounts = dict(DEFAULT_MOUNTS)
            if save:
                self._save_config_internal()
    
    def validate_paths(self) -> Dict[str, Dict[str, Any]]:
        """
        すべてのマウントパスを検証
        
        Returns:
            検証結果の辞書
        """
        results = {}
        
        with self._lock:
            mounts = dict(self._mounts)
        
        for mount_point, raw_path in mounts.items():
            path = Path(raw_path)
            if not path.is_absolute():
                path = self.base_dir / path
            
            results[mount_point] = {
                "raw_path": raw_path,
                "resolved_path": str(path.resolve()),
                "exists": path.exists(),
                "is_directory": path.is_dir() if path.exists() else None,
                "writable": os.access(path, os.W_OK) if path.exists() else None
            }
        
        return results


def get_mount_manager() -> MountManager:
    """
    グローバルなMountManagerインスタンスを取得
    
    Returns:
        MountManagerインスタンス
    """
    global _global_mount_manager
    
    if _global_mount_manager is None:
        with _init_lock:
            if _global_mount_manager is None:
                _global_mount_manager = MountManager()
    
    return _global_mount_manager


def get_mount_path(mount_point: str, ensure_exists: bool = True) -> Path:
    """
    マウントポイントの実際のパスを取得（ショートカット関数）
    
    Args:
        mount_point: マウントポイント名
        ensure_exists: ディレクトリが存在しなければ作成
    
    Returns:
        実際のファイルシステムパス
    """
    return get_mount_manager().get_path(mount_point, ensure_exists)


def initialize_mounts(
    config_path: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> None:
    """
    マウントシステムを初期化
    
    アプリケーション起動時に一度だけ呼び出す。
    
    Args:
        config_path: マウント設定ファイルのパス
        base_dir: 相対パスの基準ディレクトリ
    """
    global _global_mount_manager
    
    with _init_lock:
        kwargs = {}
        if config_path:
            kwargs['config_path'] = config_path
        if base_dir:
            kwargs['base_dir'] = base_dir
        
        _global_mount_manager = MountManager(**kwargs)
