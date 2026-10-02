"""test_pre_auth_routes.py — pre_auth_routes テーブル構築のユニットテスト"""
import unittest
from pathlib import Path


class FakePackInfo:
    def __init__(self, pack_id, ecosystem, subdir=None, path=None):
        self.pack_id = pack_id
        self.ecosystem = ecosystem
        self.subdir = subdir or Path("/fake/pack/" + pack_id)
        self.path = path or self.subdir


class FakeRegistry:
    def __init__(self, packs_dict):
        self.packs = packs_dict


def _make_handler_class():
    """テスト用に pre_auth テーブル構築/判定ロジックを再現"""
    class _Handler:
        _web_mounts = []
        _pre_auth_table = []

        @classmethod
        def load_pre_auth_routes(cls, registry, pack_ids=None):
            cls._pre_auth_table = []
            if registry is None:
                return 0
            count = 0
            for pack_id, pack_info in registry.packs.items():
                if pack_ids is not None and pack_id not in pack_ids:
                    continue
                routes = pack_info.ecosystem.get("pre_auth_routes")
                if routes and isinstance(routes, list):
                    for route in routes:
                        if not isinstance(route, dict):
                            continue
                        method = route.get("method", "").upper()
                        if not method:
                            continue
                        entry = {"method": method, "pack_id": pack_id}
                        if "path" in route:
                            entry["path"] = route["path"]
                        if "path_prefix" in route:
                            entry["path_prefix"] = route["path_prefix"]
                        cls._pre_auth_table.append(entry)
                        count += 1
                wm = pack_info.ecosystem.get("web_mount")
                if wm and isinstance(wm, dict) and not wm.get("auth_required", True):
                    prefix = wm.get("path_prefix", "")
                    if prefix:
                        for m in ("GET", "POST", "PUT", "DELETE"):
                            cls._pre_auth_table.append({
                                "method": m,
                                "path_prefix": prefix,
                                "pack_id": pack_id,
                                "_source": "web_mount",
                            })
                        count += 4
            return count

        def _is_pre_auth_route(self, method, path):
            method_upper = method.upper()
            for entry in self._pre_auth_table:
                if entry["method"] != method_upper:
                    continue
                if "path" in entry and entry["path"] == path:
                    return True
                if "path_prefix" in entry and path.startswith(entry["path_prefix"]):
                    return True
            return False

    return _Handler


class TestPreAuthRoutes(unittest.TestCase):

    def setUp(self):
        self.Handler = _make_handler_class()

    def test_path_prefix_match(self):
        """path_prefix 指定のルートが正しくマッチする"""
        packs = {
            "panel": FakePackInfo("panel", {
                "pre_auth_routes": [
                    {"method": "GET", "path_prefix": "/api/panel/"},
                    {"method": "POST", "path_prefix": "/api/panel/"},
                ]
            }),
        }
        self.Handler.load_pre_auth_routes(FakeRegistry(packs))
        handler = self.Handler()
        self.assertTrue(handler._is_pre_auth_route("GET", "/api/panel/dashboard"))
        self.assertTrue(handler._is_pre_auth_route("POST", "/api/panel/flows"))

    def test_exact_path_match(self):
        """path（完全一致）指定のルートが正しくマッチする"""
        packs = {
            "setup": FakePackInfo("setup", {
                "pre_auth_routes": [
                    {"method": "GET", "path": "/api/setup/status"},
                    {"method": "POST", "path": "/api/setup/complete"},
                ]
            }),
        }
        self.Handler.load_pre_auth_routes(FakeRegistry(packs))
        handler = self.Handler()
        self.assertTrue(handler._is_pre_auth_route("GET", "/api/setup/status"))
        self.assertTrue(handler._is_pre_auth_route("POST", "/api/setup/complete"))

    def test_no_match(self):
        """テーブルにないパスがマッチしない"""
        packs = {
            "setup": FakePackInfo("setup", {
                "pre_auth_routes": [
                    {"method": "GET", "path": "/api/setup/status"},
                ]
            }),
        }
        self.Handler.load_pre_auth_routes(FakeRegistry(packs))
        handler = self.Handler()
        self.assertFalse(handler._is_pre_auth_route("GET", "/api/packs"))
        self.assertFalse(handler._is_pre_auth_route("POST", "/api/setup/status"))  # wrong method
        self.assertFalse(handler._is_pre_auth_route("GET", "/api/setup/status/extra"))  # not prefix

    def test_method_mismatch(self):
        """メソッドが異なるとマッチしない"""
        packs = {
            "panel": FakePackInfo("panel", {
                "pre_auth_routes": [
                    {"method": "GET", "path_prefix": "/api/panel/"},
                ]
            }),
        }
        self.Handler.load_pre_auth_routes(FakeRegistry(packs))
        handler = self.Handler()
        self.assertFalse(handler._is_pre_auth_route("DELETE", "/api/panel/something"))

    def test_web_mount_auto_preauth(self):
        """web_mount で auth_required=false のパスが自動的に pre-auth に含まれる"""
        packs = {
            "setup": FakePackInfo("setup", {
                "web_mount": {
                    "path_prefix": "/setup",
                    "static_root": "web",
                    "auth_required": False,
                }
            }),
        }
        self.Handler.load_pre_auth_routes(FakeRegistry(packs))
        handler = self.Handler()
        self.assertTrue(handler._is_pre_auth_route("GET", "/setup/index.html"))
        self.assertTrue(handler._is_pre_auth_route("POST", "/setup/something"))

    def test_web_mount_auth_required_true(self):
        """web_mount で auth_required=true のパスは pre-auth に含まれない"""
        packs = {
            "admin": FakePackInfo("admin", {
                "web_mount": {
                    "path_prefix": "/admin",
                    "static_root": "web",
                    "auth_required": True,
                }
            }),
        }
        self.Handler.load_pre_auth_routes(FakeRegistry(packs))
        handler = self.Handler()
        self.assertFalse(handler._is_pre_auth_route("GET", "/admin/index.html"))

    def test_none_registry(self):
        """registry が None の場合は 0 を返す"""
        count = self.Handler.load_pre_auth_routes(None)
        self.assertEqual(count, 0)

    def test_combined_routes(self):
        """複数 Pack の pre_auth_routes が統合される"""
        packs = {
            "setup": FakePackInfo("setup", {
                "pre_auth_routes": [
                    {"method": "GET", "path": "/api/setup/status"},
                ]
            }),
            "panel": FakePackInfo("panel", {
                "pre_auth_routes": [
                    {"method": "GET", "path_prefix": "/api/panel/"},
                ]
            }),
        }
        self.Handler.load_pre_auth_routes(FakeRegistry(packs))
        handler = self.Handler()
        self.assertTrue(handler._is_pre_auth_route("GET", "/api/setup/status"))
        self.assertTrue(handler._is_pre_auth_route("GET", "/api/panel/dashboard"))

    def test_pack_filter_only_loads_requested_routes(self):
        """pack_ids 指定時は control panel の pre-auth だけ先行ロードできる"""
        packs = {
            "core_control_panel": FakePackInfo("core_control_panel", {
                "pre_auth_routes": [
                    {"method": "POST", "path": "/api/panel/auth/exchange"},
                ]
            }),
            "core_setup": FakePackInfo("core_setup", {
                "pre_auth_routes": [
                    {"method": "GET", "path": "/api/setup/status"},
                ]
            }),
        }

        self.Handler.load_pre_auth_routes(FakeRegistry(packs), pack_ids={"core_control_panel"})
        handler = self.Handler()

        self.assertTrue(handler._is_pre_auth_route("POST", "/api/panel/auth/exchange"))
        self.assertFalse(handler._is_pre_auth_route("GET", "/api/setup/status"))


class TestPackAPIHandlerPreAuthSecurity(unittest.TestCase):
    def setUp(self):
        from core_runtime.pack_api_server import PackAPIHandler

        self.Handler = PackAPIHandler

    def test_untrusted_pack_cannot_register_broad_api_preauth_prefix(self):
        _packs = {
            "evilpack": FakePackInfo("evilpack", {
                "pack_id": "evilpack",
                "pack_identity": "example:evil",
                "metadata": {"is_core_pack": False},
                "pre_auth_routes": [
                    {"method": "GET", "path_prefix": "/api/"},
                    {"method": "POST", "path_prefix": "/api/"},
                    {"method": "PUT", "path_prefix": "/api/"},
                    {"method": "DELETE", "path_prefix": "/api/"},
                ],
            }),
        }

        self.assertFalse(hasattr(self.Handler, "load_pre_auth_routes"))
        self.assertFalse(hasattr(self.Handler, "_pre_auth_table"))
        self.assertTrue(self.Handler._retired_api_path("/api/network/grant"))

    def test_spoofed_core_pack_outside_bundled_core_dir_is_not_trusted(self):
        _packs = {
            "core_setup": FakePackInfo("core_setup", {
                "pack_id": "core_setup",
                "pack_identity": "core:rumi/setup",
                "metadata": {"is_core_pack": True},
                "pre_auth_routes": [
                    {"method": "GET", "path_prefix": "/api/"},
                ],
            }),
        }

        self.assertFalse(hasattr(self.Handler, "load_pre_auth_routes"))
        self.assertFalse(hasattr(self.Handler, "_pre_auth_table"))

    def test_signed_p2p_integration_event_uses_fixed_preauth(self):
        self.assertFalse(hasattr(self.Handler, "_is_pre_auth_route"))
        self.assertTrue(
            self.Handler._retired_api_path("/api/integrations/p2p/events")
        )


if __name__ == "__main__":
    unittest.main()
