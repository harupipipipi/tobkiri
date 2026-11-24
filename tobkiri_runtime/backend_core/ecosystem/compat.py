# backend_core/ecosystem/compat.py
"""
後方互換性レイヤー（最小版）

公式は具体的なコンポーネント型を定義しない。
エコシステム初期化状態の管理のみを提供する。
"""

from pathlib import Path
from typing import Optional
import threading

# エコシステムが初期化されているかどうか
_ecosystem_initialized = False
_init_lock = threading.Lock()


def is_ecosystem_initialized() -> bool:
    """エコシステムが初期化されているか確認"""
    global _ecosystem_initialized
    return _ecosystem_initialized


def mark_ecosystem_initialized():
    """エコシステムを初期化済みとしてマーク"""
    global _ecosystem_initialized
    with _init_lock:
        _ecosystem_initialized = True


def get_user_data_dir() -> Path:
    """ユーザーデータディレクトリを取得（唯一の公式パス）"""
    return Path('user_data')


def get_chats_dir() -> Path:
    """Legacy chats directory shim."""
    return get_mount_path_safe("data.chats", "chats")


def get_settings_dir() -> Path:
    """Settings directory shim."""
    return get_mount_path_safe("data.settings", "user_data/settings")


def get_ai_clients_dir() -> Path:
    """Legacy AI client directory shim."""
    return get_legacy_component_dir("ai_client")


def get_legacy_component_dir(component_type: str) -> Path:
    """Resolve deprecated legacy component directories.

    New runtime code should use defaultspack registries and manifests rather
    than these directories.
    """
    mapping = {
        "ai_client": Path("ai_client"),
    }
    return mapping[component_type]


def get_component_path(component_type: str) -> Optional[Path]:
    """Resolve legacy component directory names used by older loaders."""
    mapping = {
        "chats": get_chats_dir(),
    }
    if component_type in mapping:
        return mapping[component_type]
    try:
        return get_legacy_component_dir(component_type)
    except KeyError:
        return None


def get_mount_path_safe(mount_key: str, fallback: str) -> Path:
    """
    マウントパスを安全に取得（汎用）
    
    公式は具体的なマウントキーを定義しない。
    コンポーネントが自身で登録したマウントを取得する際に使用。
    
    Args:
        mount_key: コンポーネントが登録したマウントキー
        fallback: マウントが見つからない場合のフォールバックパス
    
    Returns:
        解決されたパス
    """
    if _ecosystem_initialized:
        try:
            from .mounts import get_mount_path
            return get_mount_path(mount_key)
        except (KeyError, Exception):
            pass
    return Path(fallback)


def register_mount_from_component(mount_key: str, path: str) -> bool:
    """
    コンポーネントからマウントを登録（汎用API）
    
    公式はマウントキーを定義しない。
    各コンポーネントが自身で必要なマウントを登録する。
    
    Args:
        mount_key: 登録するマウントキー（コンポーネントが決める）
        path: マウント先パス
    
    Returns:
        登録成功の可否
    """
    if not _ecosystem_initialized:
        return False
    
    try:
        from .mounts import get_mount_manager
        mm = get_mount_manager()
        mm.set_mount(mount_key, path)
        return True
    except Exception:
        return False


def add_to_sys_path(path: str) -> bool:
    """
    sys.pathにパスを追加（汎用API）
    
    コンポーネントが自身のランタイムディレクトリを追加する際に使用。
    
    Args:
        path: 追加するパス
    
    Returns:
        追加成功の可否
    """
    import sys
    try:
        if path and path not in sys.path:
            # Security: 末尾に追加し、標準ライブラリ・サードパーティの
            # importハイジャックを防止する（Pack由来のパスで上書きさせない）
            sys.path.append(path)
            return True
        return False
    except Exception:
        return False
