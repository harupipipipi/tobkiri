# tests/test_ecosystem_phase4.py
"""
Phase 4 user_data構造のテスト
"""

import json
import shutil
import tempfile
from pathlib import Path
import pytest
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend_core.ecosystem.mounts import MountManager
from backend_core.ecosystem.active_ecosystem import (
    ActiveEcosystemManager,
    ActiveEcosystemConfig,
    DEFAULT_CONFIG
)
from backend_core.ecosystem.initializer import EcosystemInitializer


class TestMountsIntegration:
    """マウント統合テスト"""
    
    @pytest.fixture
    def temp_user_data(self):
        """一時的なuser_dataディレクトリ"""
        temp_dir = tempfile.mkdtemp()
        yield Path(temp_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    def test_mounts_file_creation(self, temp_user_data):
        """mounts.jsonが正しく作成される"""
        config_path = temp_user_data / "mounts.json"
        manager = MountManager(
            config_path=str(config_path),
            base_dir=str(temp_user_data)
        )
        
        # デフォルトマウントが設定される
        assert manager.has_mount("data.chats")
        assert manager.has_mount("data.settings")
        assert manager.has_mount("data.cache")
    
    def test_mount_path_resolution(self, temp_user_data):
        """マウントパスが正しく解決される"""
        config_path = temp_user_data / "mounts.json"
        manager = MountManager(
            config_path=str(config_path),
            base_dir=str(temp_user_data)
        )
        
        chats_path = manager.get_path("data.chats")
        assert chats_path.exists()
        assert chats_path.is_dir()
    
    def test_custom_mount_path(self, temp_user_data):
        """カスタムマウントパスが設定できる"""
        config_path = temp_user_data / "mounts.json"
        manager = MountManager(
            config_path=str(config_path),
            base_dir=str(temp_user_data)
        )
        
        custom_path = temp_user_data / "custom_chats"
        manager.set_mount("data.chats", str(custom_path))
        
        resolved = manager.get_path("data.chats")
        assert str(resolved) == str(custom_path.resolve())


class TestActiveEcosystem:
    """アクティブエコシステムテスト"""
    
    @pytest.fixture
    def temp_config(self):
        """一時的な設定ファイル"""
        temp_dir = tempfile.mkdtemp()
        config_path = Path(temp_dir) / "active_ecosystem.json"
        yield config_path
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    def test_default_config_creation(self, temp_config):
        """デフォルト設定が作成される"""
        manager = ActiveEcosystemManager(config_path=str(temp_config))
        
        assert manager.active_pack_identity == DEFAULT_CONFIG.active_pack_identity
        assert temp_config.exists()
    
    def test_override_management(self, temp_config):
        """オーバーライド管理"""
        manager = ActiveEcosystemManager(config_path=str(temp_config))
        
        manager.set_override("chats", "chats_v2")
        assert manager.get_override("chats") == "chats_v2"
        
        manager.remove_override("chats")
        assert manager.get_override("chats") is None
    
    def test_component_disable(self, temp_config):
        """コンポーネント無効化"""
        manager = ActiveEcosystemManager(config_path=str(temp_config))
        
        full_id = "default:chats:chats_v1"
        
        assert not manager.is_component_disabled(full_id)
        
        manager.disable_component(full_id)
        assert manager.is_component_disabled(full_id)
        
        manager.enable_component(full_id)
        assert not manager.is_component_disabled(full_id)
    
    def test_addon_disable(self, temp_config):
        """アドオン無効化"""
        manager = ActiveEcosystemManager(config_path=str(temp_config))
        
        addon_id = "test_addon"
        
        assert not manager.is_addon_disabled(addon_id)
        
        manager.disable_addon(addon_id)
        assert manager.is_addon_disabled(addon_id)
        
        manager.enable_addon(addon_id)
        assert not manager.is_addon_disabled(addon_id)
    
    def test_persistence(self, temp_config):
        """設定の永続化"""
        # 設定を変更
        manager1 = ActiveEcosystemManager(config_path=str(temp_config))
        manager1.set_override("test_type", "test_id")
        manager1.set_metadata("custom_key", "custom_value")
        
        # 新しいインスタンスで読み込み
        manager2 = ActiveEcosystemManager(config_path=str(temp_config))
        
        assert manager2.get_override("test_type") == "test_id"
        assert manager2.get_metadata("custom_key") == "custom_value"
    
    def test_reset_to_defaults(self, temp_config):
        """デフォルトにリセット"""
        manager = ActiveEcosystemManager(config_path=str(temp_config))
        
        manager.set_override("custom", "value")
        manager.disable_component("test:comp:id")
        
        manager.reset_to_defaults()
        
        assert manager.get_override("custom") is None
        assert not manager.is_component_disabled("test:comp:id")
    
    def test_default_config_not_shared(self, temp_config):
        """DEFAULT_CONFIGが共有されていないことを確認"""
        manager1 = ActiveEcosystemManager(config_path=str(temp_config))
        
        # manager1で変更
        manager1.set_override("new_type", "new_id")
        
        # DEFAULT_CONFIGは変更されていない
        assert "new_type" not in DEFAULT_CONFIG.overrides


class TestEcosystemInitializer:
    """エコシステム初期化テスト"""
    
    @pytest.fixture
    def temp_dirs(self):
        """一時ディレクトリ"""
        temp_dir = tempfile.mkdtemp()
        user_data = Path(temp_dir) / "user_data"
        ecosystem = Path(temp_dir) / "ecosystem"
        
        # 最小限のエコシステム構造を作成
        pack_dir = ecosystem / "test_pack" / "backend"
        pack_dir.mkdir(parents=True)
        
        ecosystem_data = {
            "pack_id": "test_pack",
            "pack_identity": "local:test-pack",
            "version": "1.0.0",
            "vocabulary": {"types": ["test_type"]}
        }
        with open(pack_dir / "ecosystem.json", 'w', encoding='utf-8') as f:
            json.dump(ecosystem_data, f)
        
        yield {"user_data": user_data, "ecosystem": ecosystem, "temp": temp_dir}
        
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    def test_initialization(self, temp_dirs):
        """Legacy initialization fails closed before runtime activation."""
        initializer = EcosystemInitializer(
            user_data_dir=str(temp_dirs["user_data"]),
            ecosystem_dir=str(temp_dirs["ecosystem"])
        )
        
        result = initializer.initialize()
        
        assert not result["success"]
        assert result["v4_dispatch_required"]
        assert not result["mounts_initialized"]
        assert not result["registry_loaded"]
        assert not result["active_ecosystem_loaded"]
        assert "Authority-resolved Profile" in result["errors"][0]
    
    def test_directories_created(self, temp_dirs):
        """Legacy initialization creates no implicit runtime directories."""
        initializer = EcosystemInitializer(
            user_data_dir=str(temp_dirs["user_data"]),
            ecosystem_dir=str(temp_dirs["ecosystem"])
        )
        
        initializer.initialize()
        
        user_data = temp_dirs["user_data"]
        assert not user_data.exists()
    
    def test_config_files_created(self, temp_dirs):
        """Legacy initialization writes no unbound compatibility config."""
        initializer = EcosystemInitializer(
            user_data_dir=str(temp_dirs["user_data"]),
            ecosystem_dir=str(temp_dirs["ecosystem"])
        )
        
        initializer.initialize()
        
        user_data = temp_dirs["user_data"]
        assert not (user_data / "mounts.json").exists()
        assert not (user_data / "active_ecosystem.json").exists()
    
    def test_validation(self, temp_dirs):
        """Legacy initializer validation cannot activate a runtime Registry."""
        del temp_dirs
        from tests.v4_batch_support import assert_legacy_registry_fails_closed
        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

        assert_legacy_registry_fails_closed()
        catalog = BundledCatalog.load(
            Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack" / "v4"
        )
        assert catalog.profiles["defaults"]["state"] == "needs_resolution"


class TestActiveEcosystemConfig:
    """ActiveEcosystemConfigのテスト"""
    
    def test_to_dict(self):
        """辞書変換"""
        config = ActiveEcosystemConfig(
            active_pack_identity="test:pack",
            overrides={"type1": "id1"},
            disabled_components=["comp1"],
            metadata={"key": "value"}
        )
        
        data = config.to_dict()
        
        assert data["active_pack_identity"] == "test:pack"
        assert data["overrides"] == {"type1": "id1"}
        assert data["disabled_components"] == ["comp1"]
        assert data["metadata"] == {"key": "value"}
    
    def test_from_dict(self):
        """辞書からの復元"""
        data = {
            "active_pack_identity": "test:pack",
            "overrides": {"type1": "id1"},
            "disabled_components": ["comp1"],
            "metadata": {"key": "value"}
        }
        
        config = ActiveEcosystemConfig.from_dict(data)
        
        assert config.active_pack_identity == "test:pack"
        assert config.overrides == {"type1": "id1"}
        assert config.disabled_components == ["comp1"]
        assert config.metadata == {"key": "value"}
    
    def test_from_dict_defaults(self):
        """辞書からの復元（デフォルト値）"""
        data = {}

        config = ActiveEcosystemConfig.from_dict(data)

        assert config.active_pack_identity is None
        assert config.overrides == {}
        assert config.disabled_components == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
