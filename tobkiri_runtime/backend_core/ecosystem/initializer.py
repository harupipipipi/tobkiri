"""
エコシステム初期化

旧エコシステム初期化 API の互換境界を提供する。
Pack v4 の Authority-resolved Profile と dispatch session が実行時の所有者となる。
"""

from pathlib import Path
from typing import Any, Dict


class EcosystemInitializer:
    """
    エコシステム初期化クラス
    
    旧ランタイムの初期化要求を fail closed で拒否する。
    """
    
    def __init__(
        self,
        user_data_dir: str = "user_data",
        ecosystem_dir: str = "ecosystem"
    ):
        """
        Args:
            user_data_dir: ユーザーデータディレクトリ
            ecosystem_dir: エコシステムディレクトリ
        """
        self.user_data_dir = Path(user_data_dir)
        self.ecosystem_dir = Path(ecosystem_dir)
    
    def initialize(self) -> Dict[str, Any]:
        """Reject legacy Registry activation without creating compatibility state.

        Runtime activation is owned by the Authority Kernel and an immutable
        captured v4 dispatch session.  This compatibility entry point must not
        manufacture mounts, scan installed Packs, or create an implicit active
        Pack configuration.
        """
        return {
            "success": False,
            "mounts_initialized": False,
            "directories_created": [],
            "registry_loaded": False,
            "packs_loaded": 0,
            "components_loaded": 0,
            "active_ecosystem_loaded": False,
            "v4_dispatch_required": True,
            "errors": [
                "Legacy ecosystem initialization is disabled; "
                "use an Authority-resolved Profile and captured v4 dispatch session"
            ],
        }
    
    def validate(self) -> Dict[str, Any]:
        """Report that the removed runtime Registry cannot validate Packs."""
        return {
            "valid": False,
            "warnings": [],
            "errors": [
                "Legacy ecosystem validation is disabled; use the v4 catalog "
                "and Authority-resolved Profile"
            ],
        }


def initialize_ecosystem(
    user_data_dir: str = "user_data",
    ecosystem_dir: str = "ecosystem"
) -> Dict[str, Any]:
    """
    エコシステムを初期化（ショートカット関数）
    
    Args:
        user_data_dir: ユーザーデータディレクトリ
        ecosystem_dir: エコシステムディレクトリ
    
    Returns:
        初期化結果
    """
    initializer = EcosystemInitializer(user_data_dir, ecosystem_dir)
    return initializer.initialize()


def validate_ecosystem() -> Dict[str, Any]:
    """
    エコシステムを検証（ショートカット関数）
    
    Returns:
        検証結果
    """
    return EcosystemInitializer().validate()
