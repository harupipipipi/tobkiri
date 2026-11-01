from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def reset_ecosystem_state():
    from backend_core.ecosystem import compat

    original_state = compat._ecosystem_initialized
    compat._ecosystem_initialized = False

    yield

    compat._ecosystem_initialized = original_state


class TestCompatibilityLayer:
    def test_ecosystem_not_initialized(self):
        from backend_core.ecosystem import compat

        compat._ecosystem_initialized = False

        assert compat.get_chats_dir() == Path("chats")
        assert compat.get_settings_dir() == Path("user_data/settings")
        assert compat.get_ai_clients_dir() == Path("ai_client")
        assert not hasattr(compat, "get_tools_dir")
        assert not hasattr(compat, "get_prompts_dir")
        assert not hasattr(compat, "get_supporters_dir")

    def test_mark_initialized(self):
        from backend_core.ecosystem import compat

        compat._ecosystem_initialized = False
        assert not compat.is_ecosystem_initialized()

        compat.mark_ecosystem_initialized()
        assert compat.is_ecosystem_initialized()

        compat._ecosystem_initialized = False

    def test_get_component_path_fallback(self):
        from backend_core.ecosystem import compat

        compat._ecosystem_initialized = False

        assert compat.get_component_path("chats") == Path("chats")
        assert compat.get_component_path("tool_pack") is None
        assert compat.get_component_path("prompt_pack") is None
        assert compat.get_component_path("unknown_type") is None


class TestChatManagerMigration:
    @pytest.fixture
    def temp_dir(self):
        temp = tempfile.mkdtemp()
        yield Path(temp)
        shutil.rmtree(temp, ignore_errors=True)

    def test_default_path_without_ecosystem(self, temp_dir):
        from backend_core.ecosystem import compat

        compat._ecosystem_initialized = False

        from chat_manager import ChatManager

        manager = ChatManager(chats_dir=str(temp_dir / "chats"))
        assert manager.chats_dir == temp_dir / "chats"

    def test_explicit_path_override(self, temp_dir):
        from chat_manager import ChatManager

        custom_path = temp_dir / "custom_chats"
        manager = ChatManager(chats_dir=str(custom_path))

        assert manager.chats_dir == custom_path


class TestSettingsManagerMigration:
    @pytest.fixture
    def temp_dir(self):
        temp = tempfile.mkdtemp()
        yield Path(temp)
        shutil.rmtree(temp, ignore_errors=True)

    def test_explicit_path_override(self, temp_dir):
        from settings_manager import SettingsManager

        custom_path = temp_dir / "custom_user_data"

        manager = SettingsManager(user_data_dir=str(custom_path))
        assert manager.user_data_dir == custom_path


class TestRelationshipManagerMigration:
    @pytest.fixture
    def temp_dir(self):
        temp = tempfile.mkdtemp()
        yield Path(temp)
        shutil.rmtree(temp, ignore_errors=True)

    def test_explicit_path_override(self, temp_dir):
        from relationship_manager import RelationshipManager

        custom_path = temp_dir / "custom_chats"
        custom_path.mkdir()

        manager = RelationshipManager(chats_dir=str(custom_path))
        assert manager.chats_dir == custom_path


class TestEcosystemIntegration:
    @pytest.fixture
    def full_ecosystem(self):
        temp_dir = tempfile.mkdtemp()

        user_data = Path(temp_dir) / "user_data"
        user_data.mkdir()
        (user_data / "chats").mkdir()
        (user_data / "settings").mkdir()
        (user_data / "cache").mkdir()
        (user_data / "shared").mkdir()

        mounts_data = {
            "version": "1.0",
            "mounts": {
                "data.chats": str(user_data / "chats"),
                "data.settings": str(user_data / "settings"),
                "data.cache": str(user_data / "cache"),
                "data.shared": str(user_data / "shared"),
            },
        }
        with open(user_data / "mounts.json", "w") as f:
            json.dump(mounts_data, f)

        ecosystem = Path(temp_dir) / "ecosystem"
        pack_dir = ecosystem / "default" / "backend"
        pack_dir.mkdir(parents=True)

        ecosystem_data = {
            "pack_id": "default",
            "pack_identity": "github:haru/default-pack",
            "version": "1.0.0",
            "vocabulary": {
                "types": ["chats", "tool_pack", "prompt_pack"],
            },
        }
        with open(pack_dir / "ecosystem.json", "w") as f:
            json.dump(ecosystem_data, f)

        for comp_type, comp_id in [("chats", "chats_v1"), ("tool", "tool_v1")]:
            comp_dir = pack_dir / "components" / comp_type
            comp_dir.mkdir(parents=True)

            type_name = "tool_pack" if comp_type == "tool" else comp_type
            manifest = {
                "type": type_name,
                "id": comp_id,
                "version": "1.0.0",
            }
            with open(comp_dir / "manifest.json", "w") as f:
                json.dump(manifest, f)

        active_data = {
            "active_pack_identity": "github:haru/default-pack",
            "overrides": {
                "chats": "chats_v1",
                "tool_pack": "tool_v1",
            },
        }
        with open(user_data / "active_ecosystem.json", "w") as f:
            json.dump(active_data, f)

        yield {
            "temp": temp_dir,
            "user_data": user_data,
            "ecosystem": ecosystem,
        }

        shutil.rmtree(temp_dir, ignore_errors=True)

    def test_full_initialization(self, full_ecosystem, monkeypatch):
        del full_ecosystem, monkeypatch
        from tests.v4_batch_support import assert_legacy_registry_fails_closed
        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

        assert_legacy_registry_fails_closed()
        catalog = BundledCatalog.load(
            Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack" / "v4"
        )
        assert catalog.profiles["defaults"]["state"] == "needs_resolution"
        assert len(catalog.packs) == len(set(catalog.packs))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
