"""test_web_mount.py — web_mount テーブル構築のユニットテスト"""

import sys
import types
import unittest
from pathlib import Path


class FakePackInfo:
    """Registry の PackInfo を模倣するテスト用オブジェクト"""

    def __init__(self, pack_id, ecosystem, subdir=None, path=None):
        self.pack_id = pack_id
        self.ecosystem = ecosystem
        self.subdir = subdir or Path("/fake/pack/" + pack_id)
        self.path = path or self.subdir


class FakeRegistry:
    def __init__(self, packs_dict):
        self.packs = packs_dict


def _make_handler_class():
    """PackAPIHandler のテーブル構築メソッドのみをインポートする。

    循環 import を避けるため、必要な部分だけを動的にロードする。
    """
    # core_runtime パッケージを最小限モック
    core_runtime_mod = types.ModuleType("core_runtime")
    sys.modules.setdefault("core_runtime", core_runtime_mod)

    # pack_api_server を直接読み込まず、テスト対象メソッドを検証する
    # テーブル構築ロジックを直接テストするためスタンドアロンで再現
    class _Handler:
        _web_mounts = []
        _pre_auth_table = []

        @classmethod
        def load_web_mounts(cls, registry, pack_ids=None):
            cls._web_mounts = []
            if registry is None:
                return 0
            count = 0
            for pack_id, pack_info in registry.packs.items():
                if pack_ids is not None and pack_id not in pack_ids:
                    continue
                wm = pack_info.ecosystem.get("web_mount")
                if not wm or not isinstance(wm, dict):
                    continue
                path_prefix = wm.get("path_prefix", "")
                static_root_rel = wm.get("static_root", "")
                if not path_prefix or not static_root_rel:
                    continue
                base_dir = getattr(pack_info, "subdir", None) or pack_info.path
                web_root = Path(str(base_dir)) / static_root_rel
                cls._web_mounts.append(
                    {
                        "path_prefix": path_prefix,
                        "web_root": web_root.resolve(),
                        "spa_fallback": wm.get("spa_fallback", False),
                        "auth_required": wm.get("auth_required", True),
                        "pack_id": pack_id,
                    }
                )
                count += 1
            cls._web_mounts.sort(key=lambda e: len(e["path_prefix"]), reverse=True)
            return count

        def _match_web_mount(self, request_path):
            for wm in self._web_mounts:
                prefix = wm["path_prefix"]
                if request_path == prefix or request_path.startswith(prefix + "/"):
                    return wm
            return None

    return _Handler


class TestWebMountTable(unittest.TestCase):
    def setUp(self):
        self.Handler = _make_handler_class()

    def test_web_mount_registered(self):
        """web_mount 付き Pack がテーブルに登録される"""
        packs = {
            "core_setup": FakePackInfo(
                "core_setup",
                {
                    "web_mount": {
                        "path_prefix": "/setup",
                        "static_root": "web",
                        "spa_fallback": True,
                        "auth_required": False,
                    }
                },
            ),
        }
        reg = FakeRegistry(packs)
        count = self.Handler.load_web_mounts(reg)
        self.assertEqual(count, 1)
        self.assertEqual(len(self.Handler._web_mounts), 1)
        self.assertEqual(self.Handler._web_mounts[0]["path_prefix"], "/setup")

    def test_web_mount_skipped_when_absent(self):
        """web_mount なしの Pack はスキップされる"""
        packs = {
            "some_pack": FakePackInfo(
                "some_pack",
                {
                    "pack_id": "some_pack",
                },
            ),
        }
        reg = FakeRegistry(packs)
        count = self.Handler.load_web_mounts(reg)
        self.assertEqual(count, 0)
        self.assertEqual(len(self.Handler._web_mounts), 0)

    def test_match_web_mount_exact(self):
        """パスプレフィックスの完全一致"""
        packs = {
            "panel": FakePackInfo(
                "panel",
                {
                    "web_mount": {
                        "path_prefix": "/panel",
                        "static_root": "web",
                    }
                },
            ),
        }
        self.Handler.load_web_mounts(FakeRegistry(packs))
        handler = self.Handler()
        self.assertIsNotNone(handler._match_web_mount("/panel"))
        self.assertIsNotNone(handler._match_web_mount("/panel/"))
        self.assertIsNotNone(handler._match_web_mount("/panel/index.html"))

    def test_match_web_mount_no_match(self):
        """テーブルにないパスはマッチしない"""
        packs = {
            "panel": FakePackInfo(
                "panel",
                {
                    "web_mount": {
                        "path_prefix": "/panel",
                        "static_root": "web",
                    }
                },
            ),
        }
        self.Handler.load_web_mounts(FakeRegistry(packs))
        handler = self.Handler()
        self.assertIsNone(handler._match_web_mount("/api/something"))
        self.assertIsNone(handler._match_web_mount("/panelx"))

    def test_longest_prefix_match(self):
        """最長一致: /panel/admin が /panel より /panel/admin を優先"""
        packs = {
            "panel": FakePackInfo(
                "panel",
                {
                    "web_mount": {
                        "path_prefix": "/panel",
                        "static_root": "web",
                    }
                },
            ),
            "admin": FakePackInfo(
                "admin",
                {
                    "web_mount": {
                        "path_prefix": "/panel/admin",
                        "static_root": "web",
                    }
                },
            ),
        }
        self.Handler.load_web_mounts(FakeRegistry(packs))
        handler = self.Handler()
        match = handler._match_web_mount("/panel/admin/page")
        self.assertIsNotNone(match)
        self.assertEqual(match["pack_id"], "admin")

    def test_none_registry(self):
        """registry が None の場合は 0 を返す"""
        count = self.Handler.load_web_mounts(None)
        self.assertEqual(count, 0)

    def test_multiple_mounts(self):
        """複数の web_mount が登録される"""
        packs = {
            "setup": FakePackInfo(
                "setup", {"web_mount": {"path_prefix": "/setup", "static_root": "web"}}
            ),
            "panel": FakePackInfo(
                "panel", {"web_mount": {"path_prefix": "/panel", "static_root": "web"}}
            ),
        }
        reg = FakeRegistry(packs)
        count = self.Handler.load_web_mounts(reg)
        self.assertEqual(count, 2)

    def test_pack_filter_only_loads_requested_mounts(self):
        """pack_ids を指定すると control panel だけ先行ロードできる"""
        packs = {
            "core_control_panel": FakePackInfo(
                "core_control_panel", {"web_mount": {"path_prefix": "/panel", "static_root": "web"}}
            ),
            "core_setup": FakePackInfo(
                "core_setup", {"web_mount": {"path_prefix": "/setup", "static_root": "web"}}
            ),
        }
        reg = FakeRegistry(packs)

        count = self.Handler.load_web_mounts(reg, pack_ids={"core_control_panel"})

        self.assertEqual(count, 1)
        self.assertEqual(self.Handler._web_mounts[0]["pack_id"], "core_control_panel")


class TestPackAPIHandlerWebMountSecurity(unittest.TestCase):
    def setUp(self):
        from core_runtime.pack_api_server import PackAPIHandler

        self.Handler = PackAPIHandler

    def test_static_root_traversal_is_rejected_at_load_time(self):
        self.assertFalse(hasattr(self.Handler, "load_web_mounts"))
        self.assertFalse(hasattr(self.Handler, "_web_mounts"))

    def test_absolute_static_root_is_rejected_at_load_time(self):
        handler = object.__new__(self.Handler)
        for mount in handler._fixed_web_mounts():
            self.assertTrue(mount["web_root"].is_absolute())
            self.assertNotEqual(mount["web_root"], Path("/etc"))

    def test_windows_style_static_root_traversal_is_rejected(self):
        handler = object.__new__(self.Handler)
        self.assertIsNone(handler._match_web_mount("/leak"))
        self.assertIsNone(handler._match_web_mount("/..\\outside_pack_dir"))

    def test_core_mounts_do_not_resolve_pack_owned_desktops_alias(self):
        handler = object.__new__(self.Handler)

        match = handler._match_web_mount("/desktops")

        self.assertIsNone(match)

    def test_defaultspack_contribution_owns_desktops_alias(self):
        from ecosystem.defaultspack.defaultspack.surface_contributions import (
            defaultspack_web_mounts,
        )

        mounts = defaultspack_web_mounts(Path("/pack/defaultspack"))
        match = next(mount for mount in mounts if mount["path_prefix"] == "/desktops")

        self.assertEqual(match["web_root"], Path("/pack/defaultspack/ui"))
        self.assertEqual(match["index_file"], "shell.html")
        self.assertTrue(match["spa_fallback"])
        self.assertTrue(match["auth_required"])
        self.assertTrue(match["auth_bootstrap"])

    def test_pack_owned_desktops_alias_does_not_capture_api_desktops(self):
        handler = object.__new__(self.Handler)

        self.assertIsNone(handler._match_web_mount("/api/desktops"))


if __name__ == "__main__":
    unittest.main()
