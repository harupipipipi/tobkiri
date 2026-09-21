# tests/test_ecosystem_phase1.py
"""
Phase 1 基盤レイヤーのテスト
"""

import uuid
import json
import tempfile
import shutil
from pathlib import Path
import pytest
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend_core.ecosystem.uuid_namespace import PACK_NAMESPACE_UUID
from backend_core.ecosystem.uuid_utils import (
    generate_pack_uuid,
    generate_component_uuid,
    generate_addon_uuid,
    validate_uuid,
    parse_uuid
)
from backend_core.ecosystem.mounts import MountManager, get_mount_path, DEFAULT_MOUNTS
from backend_core.ecosystem.json_patch import (
    apply_patch,
    validate_patch,
    create_patch_operation,
    JsonPatchError,
    JsonPatchTestError,
    JsonPatchForbiddenError
)


class TestUuidUtils:
    """UUID生成ユーティリティのテスト"""
    
    def test_pack_uuid_deterministic(self):
        """同じpack_identityから常に同じUUIDが生成される"""
        identity = "github:haru/default-pack"
        uuid1 = generate_pack_uuid(identity)
        uuid2 = generate_pack_uuid(identity)
        assert uuid1 == uuid2
    
    def test_pack_uuid_different_identities(self):
        """異なるpack_identityからは異なるUUIDが生成される"""
        uuid1 = generate_pack_uuid("github:haru/default-pack")
        uuid2 = generate_pack_uuid("github:haru/pro-pack")
        assert uuid1 != uuid2
    
    def test_pack_uuid_is_uuid5(self):
        """生成されるUUIDはversion 5"""
        pack_uuid = generate_pack_uuid("github:haru/default-pack")
        assert pack_uuid.version == 5
    
    def test_component_uuid_deterministic(self):
        """同じ入力から常に同じComponent UUIDが生成される"""
        pack_uuid = generate_pack_uuid("github:haru/default-pack")
        comp_uuid1 = generate_component_uuid(pack_uuid, "chats", "chats_v1")
        comp_uuid2 = generate_component_uuid(pack_uuid, "chats", "chats_v1")
        assert comp_uuid1 == comp_uuid2
    
    def test_component_uuid_different_types(self):
        """異なるtypeからは異なるUUIDが生成される"""
        pack_uuid = generate_pack_uuid("github:haru/default-pack")
        comp_uuid1 = generate_component_uuid(pack_uuid, "chats", "v1")
        comp_uuid2 = generate_component_uuid(pack_uuid, "tool_pack", "v1")
        assert comp_uuid1 != comp_uuid2
    
    def test_component_uuid_accepts_string(self):
        """pack_uuidは文字列でも受け付ける"""
        pack_uuid = generate_pack_uuid("github:haru/default-pack")
        comp_uuid1 = generate_component_uuid(pack_uuid, "chats", "v1")
        comp_uuid2 = generate_component_uuid(str(pack_uuid), "chats", "v1")
        assert comp_uuid1 == comp_uuid2
    
    def test_addon_uuid_deterministic(self):
        """同じ入力から常に同じAddon UUIDが生成される"""
        pack_uuid = generate_pack_uuid("github:haru/default-pack")
        addon_uuid1 = generate_addon_uuid(pack_uuid, "my_addon")
        addon_uuid2 = generate_addon_uuid(pack_uuid, "my_addon")
        assert addon_uuid1 == addon_uuid2
    
    def test_addon_uuid_different_ids(self):
        """異なるaddon_idからは異なるUUIDが生成される"""
        pack_uuid = generate_pack_uuid("github:haru/default-pack")
        addon_uuid1 = generate_addon_uuid(pack_uuid, "addon_a")
        addon_uuid2 = generate_addon_uuid(pack_uuid, "addon_b")
        assert addon_uuid1 != addon_uuid2
    
    def test_validate_uuid(self):
        """UUID検証が正しく動作する"""
        assert validate_uuid("a3e9f8c2-7b4d-5e1a-9c6f-2d8b4a7e3f1c")
        assert not validate_uuid("invalid-uuid")
        assert not validate_uuid("")
        assert not validate_uuid(None)
    
    def test_parse_uuid(self):
        """UUID解析が正しく動作する"""
        uuid_str = "a3e9f8c2-7b4d-5e1a-9c6f-2d8b4a7e3f1c"
        parsed = parse_uuid(uuid_str)
        assert isinstance(parsed, uuid.UUID)
        assert str(parsed) == uuid_str
    
    def test_parse_uuid_from_uuid_object(self):
        """UUIDオブジェクトからの解析"""
        original = uuid.UUID("a3e9f8c2-7b4d-5e1a-9c6f-2d8b4a7e3f1c")
        parsed = parse_uuid(original)
        assert parsed == original
    
    def test_parse_uuid_invalid(self):
        """無効なUUIDで例外が発生"""
        with pytest.raises(ValueError):
            parse_uuid("invalid")
    
    def test_pack_uuid_empty_identity_raises(self):
        """空のpack_identityで例外が発生"""
        with pytest.raises(ValueError):
            generate_pack_uuid("")
    
    def test_component_uuid_empty_type_raises(self):
        """空のcomponent_typeで例外が発生"""
        pack_uuid = generate_pack_uuid("github:haru/default-pack")
        with pytest.raises(ValueError):
            generate_component_uuid(pack_uuid, "", "id")
    
    def test_component_uuid_empty_id_raises(self):
        """空のcomponent_idで例外が発生"""
        pack_uuid = generate_pack_uuid("github:haru/default-pack")
        with pytest.raises(ValueError):
            generate_component_uuid(pack_uuid, "type", "")


class TestMountManager:

    def test_uses_configured_user_data_root_by_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        temp_dir: Path,
    ) -> None:
        """Desktop runtime mounts must honor the configured user-data root."""
        user_data = temp_dir / "launcher-user-data"
        monkeypatch.delenv("TOBKIRI_USER_DATA", raising=False)
        monkeypatch.setenv("RUMI_USER_DATA", str(user_data))

        manager = MountManager()

        assert manager.config_path == user_data / "mounts.json"
        assert manager.get_path("data.settings").resolve() == (
            user_data / "settings"
        ).resolve()
    """マウント管理のテスト"""
    
    @pytest.fixture
    def temp_dir(self):
        """一時ディレクトリを作成"""
        temp = tempfile.mkdtemp()
        yield Path(temp)
        shutil.rmtree(temp, ignore_errors=True)
    
    def test_default_mounts(self, temp_dir):
        """デフォルトマウントが設定される"""
        config_path = temp_dir / "mounts.json"
        manager = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        
        for mount_point in DEFAULT_MOUNTS:
            assert manager.has_mount(mount_point)
    
    def test_get_path(self, temp_dir):
        """パス取得が正しく動作する"""
        config_path = temp_dir / "mounts.json"
        manager = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        
        chats_path = manager.get_path("data.chats")
        assert chats_path.exists()
        assert chats_path.is_dir()
    
    def test_get_path_without_ensure_exists(self, temp_dir):
        """ensure_exists=Falseでディレクトリを作成しない"""
        config_path = temp_dir / "mounts.json"
        manager = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        
        # カスタムマウントを追加（存在しないパス）
        custom_path = temp_dir / "nonexistent" / "path"
        manager.set_mount("data.custom", str(custom_path), save=False)
        
        # ensure_exists=Falseで取得
        result = manager.get_path("data.custom", ensure_exists=False)
        assert not result.exists()
    
    def test_set_mount(self, temp_dir):
        """マウントポイントの設定が正しく動作する"""
        config_path = temp_dir / "mounts.json"
        manager = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        
        custom_path = str(temp_dir / "custom_chats")
        manager.set_mount("data.chats", custom_path)
        
        retrieved_path = manager.get_path("data.chats")
        assert str(retrieved_path) == str(Path(custom_path).resolve())
    
    def test_config_persistence(self, temp_dir):
        """設定がファイルに永続化される"""
        config_path = temp_dir / "mounts.json"
        
        # 設定を変更
        manager1 = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        manager1.set_mount("data.custom", "./custom")
        
        # 新しいインスタンスで読み込み
        manager2 = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        assert manager2.has_mount("data.custom")
    
    def test_unknown_mount_raises(self, temp_dir):
        """未定義のマウントポイントでエラー"""
        config_path = temp_dir / "mounts.json"
        manager = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        
        with pytest.raises(KeyError):
            manager.get_path("data.unknown")
    
    def test_remove_mount(self, temp_dir):
        """マウントポイントの削除"""
        config_path = temp_dir / "mounts.json"
        manager = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        
        manager.set_mount("data.temp", "./temp")
        assert manager.has_mount("data.temp")
        
        result = manager.remove_mount("data.temp")
        assert result is True
        assert not manager.has_mount("data.temp")
    
    def test_remove_nonexistent_mount(self, temp_dir):
        """存在しないマウントポイントの削除"""
        config_path = temp_dir / "mounts.json"
        manager = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        
        result = manager.remove_mount("data.nonexistent")
        assert result is False
    
    def test_get_all_mounts(self, temp_dir):
        """すべてのマウント設定を取得"""
        config_path = temp_dir / "mounts.json"
        manager = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        
        all_mounts = manager.get_all_mounts()
        assert isinstance(all_mounts, dict)
        assert "data.chats" in all_mounts
    
    def test_reset_to_defaults(self, temp_dir):
        """デフォルト設定にリセット"""
        config_path = temp_dir / "mounts.json"
        manager = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        
        # カスタム設定
        manager.set_mount("data.chats", "/custom/path")
        manager.set_mount("data.extra", "./extra")
        
        # リセット
        manager.reset_to_defaults()
        
        all_mounts = manager.get_all_mounts()
        assert all_mounts["data.chats"] == DEFAULT_MOUNTS["data.chats"]
        assert "data.extra" not in all_mounts
    
    def test_validate_paths(self, temp_dir):
        """パス検証"""
        config_path = temp_dir / "mounts.json"
        manager = MountManager(config_path=str(config_path), base_dir=str(temp_dir))
        
        # パスを作成
        manager.get_path("data.chats", ensure_exists=True)
        
        results = manager.validate_paths()
        assert "data.chats" in results
        assert results["data.chats"]["exists"] is True


class TestJsonPatch:
    """JSON Patchのテスト"""
    
    def test_add_operation(self):
        """add操作が正しく動作する"""
        doc = {"foo": "bar"}
        patch = [{"op": "add", "path": "/baz", "value": "qux"}]
        result = apply_patch(doc, patch)
        assert result == {"foo": "bar", "baz": "qux"}
    
    def test_add_nested(self):
        """ネストされたadd操作"""
        doc = {"foo": {"bar": "baz"}}
        patch = [{"op": "add", "path": "/foo/qux", "value": 123}]
        result = apply_patch(doc, patch)
        assert result == {"foo": {"bar": "baz", "qux": 123}}
    
    def test_add_to_root(self):
        """ルートへのadd操作"""
        doc = {"foo": "bar"}
        patch = [{"op": "add", "path": "", "value": {"new": "doc"}}]
        result = apply_patch(doc, patch)
        assert result == {"new": "doc"}
    
    def test_remove_operation(self):
        """remove操作が正しく動作する"""
        doc = {"foo": "bar", "baz": "qux"}
        patch = [{"op": "remove", "path": "/baz"}]
        result = apply_patch(doc, patch)
        assert result == {"foo": "bar"}
    
    def test_remove_nested(self):
        """ネストされたremove操作"""
        doc = {"foo": {"bar": "baz", "qux": 123}}
        patch = [{"op": "remove", "path": "/foo/qux"}]
        result = apply_patch(doc, patch)
        assert result == {"foo": {"bar": "baz"}}
    
    def test_remove_nonexistent_raises(self):
        """存在しないパスのremoveでエラー"""
        doc = {"foo": "bar"}
        patch = [{"op": "remove", "path": "/nonexistent"}]
        with pytest.raises(JsonPatchError):
            apply_patch(doc, patch)
    
    def test_replace_operation(self):
        """replace操作が正しく動作する"""
        doc = {"foo": "bar"}
        patch = [{"op": "replace", "path": "/foo", "value": "baz"}]
        result = apply_patch(doc, patch)
        assert result == {"foo": "baz"}
    
    def test_replace_nested(self):
        """ネストされたreplace操作"""
        doc = {"foo": {"bar": "old"}}
        patch = [{"op": "replace", "path": "/foo/bar", "value": "new"}]
        result = apply_patch(doc, patch)
        assert result == {"foo": {"bar": "new"}}
    
    def test_replace_nonexistent_raises(self):
        """存在しないパスのreplaceでエラー"""
        doc = {"foo": "bar"}
        patch = [{"op": "replace", "path": "/nonexistent", "value": "x"}]
        with pytest.raises(JsonPatchError):
            apply_patch(doc, patch)
    
    def test_test_operation_success(self):
        """test操作が成功する場合"""
        doc = {"foo": "bar"}
        patch = [{"op": "test", "path": "/foo", "value": "bar"}]
        result = apply_patch(doc, patch)
        assert result == {"foo": "bar"}
    
    def test_test_operation_failure(self):
        """test操作が失敗する場合"""
        doc = {"foo": "bar"}
        patch = [{"op": "test", "path": "/foo", "value": "baz"}]
        with pytest.raises(JsonPatchTestError):
            apply_patch(doc, patch)
    
    def test_test_nonexistent_path(self):
        """存在しないパスのtestでエラー"""
        doc = {"foo": "bar"}
        patch = [{"op": "test", "path": "/nonexistent", "value": "x"}]
        with pytest.raises(JsonPatchTestError):
            apply_patch(doc, patch)
    
    def test_move_forbidden(self):
        """move操作は禁止"""
        doc = {"foo": "bar"}
        patch = [{"op": "move", "from": "/foo", "path": "/baz"}]
        with pytest.raises(JsonPatchForbiddenError):
            apply_patch(doc, patch)
    
    def test_copy_forbidden(self):
        """copy操作は禁止"""
        doc = {"foo": "bar"}
        patch = [{"op": "copy", "from": "/foo", "path": "/baz"}]
        with pytest.raises(JsonPatchForbiddenError):
            apply_patch(doc, patch)
    
    def test_array_add_end(self):
        """配列末尾へのadd操作"""
        doc = {"foo": [1, 2, 3]}
        patch = [{"op": "add", "path": "/foo/-", "value": 4}]
        result = apply_patch(doc, patch)
        assert result == {"foo": [1, 2, 3, 4]}
    
    def test_array_add_index(self):
        """配列への挿入"""
        doc = {"foo": [1, 2, 3]}
        patch = [{"op": "add", "path": "/foo/1", "value": 99}]
        result = apply_patch(doc, patch)
        assert result == {"foo": [1, 99, 2, 3]}
    
    def test_array_add_at_end_index(self):
        """配列末尾へのインデックス指定追加"""
        doc = {"foo": [1, 2, 3]}
        patch = [{"op": "add", "path": "/foo/3", "value": 4}]
        result = apply_patch(doc, patch)
        assert result == {"foo": [1, 2, 3, 4]}
    
    def test_array_remove(self):
        """配列からのremove"""
        doc = {"foo": [1, 2, 3]}
        patch = [{"op": "remove", "path": "/foo/1"}]
        result = apply_patch(doc, patch)
        assert result == {"foo": [1, 3]}
    
    def test_array_replace(self):
        """配列要素のreplace"""
        doc = {"foo": [1, 2, 3]}
        patch = [{"op": "replace", "path": "/foo/1", "value": 99}]
        result = apply_patch(doc, patch)
        assert result == {"foo": [1, 99, 3]}
    
    def test_array_index_out_of_bounds(self):
        """配列インデックスが範囲外"""
        doc = {"foo": [1, 2, 3]}
        patch = [{"op": "add", "path": "/foo/10", "value": 99}]
        with pytest.raises(JsonPatchError):
            apply_patch(doc, patch)
    
    def test_validate_patch_valid(self):
        """有効なパッチの検証"""
        valid_patch = [
            {"op": "add", "path": "/foo", "value": "bar"},
            {"op": "remove", "path": "/baz"},
            {"op": "replace", "path": "/qux", "value": 123},
            {"op": "test", "path": "/check", "value": True}
        ]
        errors = validate_patch(valid_patch)
        assert errors == []
    
    def test_validate_patch_missing_op(self):
        """opがないパッチ"""
        invalid_patch = [{"path": "/foo", "value": "bar"}]
        errors = validate_patch(invalid_patch)
        assert len(errors) > 0
    
    def test_validate_patch_missing_path(self):
        """pathがないパッチ"""
        invalid_patch = [{"op": "add", "value": "bar"}]
        errors = validate_patch(invalid_patch)
        assert len(errors) > 0
    
    def test_validate_patch_missing_value(self):
        """valueがないadd/replace/testパッチ"""
        invalid_patch = [
            {"op": "add", "path": "/foo"},
            {"op": "replace", "path": "/bar"},
            {"op": "test", "path": "/baz"}
        ]
        errors = validate_patch(invalid_patch)
        assert len(errors) == 3
    
    def test_validate_patch_forbidden_ops(self):
        """禁止操作"""
        invalid_patch = [
            {"op": "move", "from": "/a", "path": "/b"},
            {"op": "copy", "from": "/a", "path": "/b"}
        ]
        errors = validate_patch(invalid_patch)
        assert len(errors) == 2
    
    def test_validate_patch_invalid_path_format(self):
        """パス形式が不正"""
        invalid_patch = [{"op": "add", "path": "no_leading_slash", "value": 1}]
        errors = validate_patch(invalid_patch)
        assert len(errors) > 0
    
    def test_in_place_modification(self):
        """in_place=Trueで元のドキュメントが変更される"""
        doc = {"foo": "bar"}
        patch = [{"op": "add", "path": "/baz", "value": "qux"}]
        
        result = apply_patch(doc, patch, in_place=True)
        assert doc == {"foo": "bar", "baz": "qux"}
        assert result is doc
    
    def test_not_in_place(self):
        """in_place=Falseで元のドキュメントは変更されない"""
        doc = {"foo": "bar"}
        patch = [{"op": "add", "path": "/baz", "value": "qux"}]
        
        result = apply_patch(doc, patch, in_place=False)
        assert doc == {"foo": "bar"}
        assert result == {"foo": "bar", "baz": "qux"}
    
    def test_create_patch_operation_add(self):
        """create_patch_operationでadd操作を作成"""
        op = create_patch_operation("add", "/foo", "bar")
        assert op == {"op": "add", "path": "/foo", "value": "bar"}
    
    def test_create_patch_operation_remove(self):
        """create_patch_operationでremove操作を作成"""
        op = create_patch_operation("remove", "/foo")
        assert op == {"op": "remove", "path": "/foo"}
    
    def test_create_patch_operation_forbidden(self):
        """create_patch_operationで禁止操作"""
        with pytest.raises(JsonPatchForbiddenError):
            create_patch_operation("move", "/foo")
    
    def test_multiple_operations(self):
        """複数の操作を連続で適用"""
        doc = {"a": 1, "b": 2}
        patch = [
            {"op": "add", "path": "/c", "value": 3},
            {"op": "replace", "path": "/a", "value": 10},
            {"op": "remove", "path": "/b"}
        ]
        result = apply_patch(doc, patch)
        assert result == {"a": 10, "c": 3}
    
    def test_escaped_characters_in_path(self):
        """パス内のエスケープ文字"""
        doc = {"foo/bar": "value", "tilde~field": "other"}
        
        # ~1 は / にデコード、~0 は ~ にデコード
        patch = [{"op": "replace", "path": "/foo~1bar", "value": "new"}]
        result = apply_patch(doc, patch)
        assert result["foo/bar"] == "new"
    
    def test_empty_patch_list(self):
        """空のパッチリスト"""
        doc = {"foo": "bar"}
        patch = []
        result = apply_patch(doc, patch)
        assert result == {"foo": "bar"}
    
    def test_deeply_nested_path(self):
        """深くネストされたパス"""
        doc = {"a": {"b": {"c": {"d": "value"}}}}
        patch = [{"op": "replace", "path": "/a/b/c/d", "value": "new"}]
        result = apply_patch(doc, patch)
        assert result["a"]["b"]["c"]["d"] == "new"


class TestNamespaceUuid:
    """名前空間UUIDのテスト"""
    
    def test_namespace_uuid_is_valid(self):
        """PACK_NAMESPACE_UUIDが有効なUUID"""
        assert isinstance(PACK_NAMESPACE_UUID, uuid.UUID)
    
    def test_namespace_uuid_is_constant(self):
        """PACK_NAMESPACE_UUIDは固定値"""
        expected = uuid.UUID("a3e9f8c2-7b4d-5e1a-9c6f-2d8b4a7e3f1c")
        assert PACK_NAMESPACE_UUID == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
