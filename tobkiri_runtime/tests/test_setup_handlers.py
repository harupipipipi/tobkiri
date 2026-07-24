from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestSetupHandlers(unittest.TestCase):
    @staticmethod
    def _reviewed_payload(handler, pack_ids):
        packs = [{"pack_id": pack_id, "risk_level": "low", "supports_all_ok": False} for pack_id in pack_ids]
        return packs, {
            "setup_pack_ids": pack_ids,
            "reviewed_pack_ids": pack_ids,
            "review_revision": handler._setup_pack_review_revision(packs),
            "confirmed_privileged_pack_ids": [],
        }
    class _FakeFunctionRegistry:
        def __init__(self, registered=None):
            self._registered = set(registered or [])

        def get(self, qualified_name):
            return object() if qualified_name in self._registered else None

    class _FakeContainer:
        def __init__(self, function_registry=None):
            self._function_registry = function_registry

        def get_or_none(self, name):
            if name == "function_registry":
                return self._function_registry
            return None

    def test_setup_handler_lists_packs(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked, patch.object(
            SetupHandlersMixin,
            "_recommended_default_profile_preview",
            return_value={
                "name": "Defaults Profile",
                "pack_ids": ["defaultspack", "rumi"],
            },
        ):
            mocked.return_value.list_packs.return_value = {"packs": []}
            result = handler._setup_list_packs()
        self.assertEqual(result["packs"], [])
        self.assertRegex(result["review_revision"], r"^setup-review-v1:[0-9a-f]{64}$")
        self.assertEqual(result["recommended_default_profile"]["pack_ids"], ["defaultspack", "rumi"])

    def test_defaults_profile_preview_lists_official_bundled_setup_packs(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked:
            mocked.return_value.list_packs.return_value = {
                "packs": [
                    {
                        "pack_id": "tools-setup",
                        "target_pack_id": "tools",
                        "display_name": "Tools",
                        "marketplace": {
                            "registry": "bundled",
                            "publisher": "rumi-ai",
                            "status": "verified",
                        },
                        "signing": {
                            "mode": "repository_reviewed",
                            "verified": True,
                        },
                        "schema_issues": [],
                    },
                    {
                        "pack_id": "defaultspack",
                        "target_pack_id": "defaultspack",
                        "display_name": "Tobkiri",
                        "marketplace": {
                            "registry": "bundled",
                            "publisher": "rumi-ai",
                            "status": "verified",
                        },
                        "signing": {
                            "mode": "repository_reviewed",
                            "verified": True,
                        },
                        "schema_issues": [],
                    },
                    {
                        "pack_id": "third-party",
                        "target_pack_id": "third-party",
                        "display_name": "Third party",
                        "marketplace": {
                            "registry": "external",
                            "publisher": "someone-else",
                            "status": "verified",
                        },
                        "signing": {"mode": "repository_reviewed"},
                        "schema_issues": [],
                    },
                    {
                        "pack_id": "broken",
                        "target_pack_id": "broken",
                        "display_name": "Broken",
                        "marketplace": {"publisher": "rumi-ai"},
                        "signing": {"mode": "repository_reviewed"},
                        "schema_issues": [{"code": "invalid"}],
                    },
                ]
            }
            preview = SetupHandlersMixin._recommended_default_profile_preview()

        self.assertEqual(preview["base_pack"], "defaultspack")
        self.assertEqual(preview["pack_ids"], ["defaultspack", "tools"])
        self.assertEqual(
            preview["packs"],
            [
                {"pack_id": "defaultspack", "display_name": "Tobkiri"},
                {"pack_id": "tools", "display_name": "Tools"},
            ],
        )

    def test_official_bundled_setup_pack_accepts_legacy_first_party_metadata(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        self.assertTrue(
            SetupHandlersMixin._is_official_bundled_setup_pack(
                {
                    "marketplace": {"id": "rumi.legacy"},
                    "signing": {"mode": "repository_reviewed"},
                }
            )
        )
        self.assertFalse(
            SetupHandlersMixin._is_official_bundled_setup_pack(
                {
                    "marketplace": {
                        "publisher": "third-party",
                        "registry": "bundled",
                        "status": "verified",
                    },
                    "signing": {"mode": "repository_reviewed"},
                }
            )
        )

    def test_setup_install_rejects_stale_tampered_and_unconfirmed_privileged_reviews(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        packs = [{"pack_id": "danger", "version": "2", "risk_level": "high", "supports_all_ok": True}]
        revision = handler._setup_pack_review_revision(packs)
        with patch("core_runtime.api.setup_handlers.get_setup_pack_manager") as mocked:
            mocked.return_value.list_packs.return_value = {"packs": packs}
            stale = handler._setup_install_pack({
                "setup_pack_ids": ["danger"], "reviewed_pack_ids": ["danger"],
                "review_revision": "setup-review-v1:stale", "confirmed_privileged_pack_ids": ["danger"],
            })
            tampered = handler._setup_install_pack({
                "setup_pack_ids": ["danger"], "reviewed_pack_ids": [],
                "review_revision": revision, "confirmed_privileged_pack_ids": ["danger"],
            })
            unconfirmed = handler._setup_install_pack({
                "setup_pack_ids": ["danger"], "reviewed_pack_ids": ["danger"],
                "review_revision": revision, "confirmed_privileged_pack_ids": [],
            })

        self.assertEqual(stale["status_code"], 409)
        self.assertEqual(tampered["status_code"], 409)
        self.assertEqual(unconfirmed["status_code"], 400)
        mocked.return_value.install.assert_not_called()

    def test_all_ok_support_alone_does_not_require_setup_confirmation(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        self.assertFalse(
            SetupHandlersMixin._setup_pack_requires_confirmation(
                {
                    "risk_level": "low",
                    "supports_all_ok": True,
                    "required_permissions": [],
                }
            )
        )
        self.assertTrue(
            SetupHandlersMixin._setup_pack_requires_confirmation(
                {"risk_level": "high", "required_permissions": []}
            )
        )
        self.assertTrue(
            SetupHandlersMixin._setup_pack_requires_confirmation(
                {"risk_level": "low", "required_permissions": ["host.execute"]}
            )
        )

    def test_setup_handler_filters_stale_selected_setup_packs(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked:
            mocked.return_value.list_packs.return_value = {
                "packs": [
                    {"pack_id": "defaultspack", "target_pack_id": "defaultspack"},
                ],
                "selected_setup_pack_id": "ghost_pack",
                "selected_setup_pack_ids": ["ghost_pack", "defaultspack"],
                "active_setup_pack_id": "ghost_pack",
                "active_target_pack_id": "ghost_target",
            }
            result = handler._setup_list_packs()

        self.assertEqual(result["selected_setup_pack_ids"], ["defaultspack"])
        self.assertEqual(result["selected_setup_pack_id"], "defaultspack")
        self.assertIsNone(result["active_setup_pack_id"])
        self.assertIsNone(result["active_target_pack_id"])
        self.assertTrue(result["packs"][0]["selected"])

    def test_setup_handler_derives_active_target_from_selected_pack_definition(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked:
            mocked.return_value.list_packs.return_value = {
                "packs": [
                    {"pack_id": "alpha", "target_pack_id": "alpha_target"},
                    {"pack_id": "beta", "target_pack_id": "beta_target"},
                ],
                "selected_setup_pack_id": "alpha",
                "selected_setup_pack_ids": ["alpha"],
                "active_setup_pack_id": "alpha",
                "active_target_pack_id": "stale_target",
            }
            result = handler._setup_list_packs()

        self.assertEqual(result["selected_setup_pack_ids"], ["alpha"])
        self.assertEqual(result["selected_setup_pack_id"], "alpha")
        self.assertEqual(result["active_setup_pack_id"], "alpha")
        self.assertEqual(result["active_target_pack_id"], "alpha_target")
        self.assertTrue(result["packs"][0]["selected"])
        self.assertFalse(result["packs"][1]["selected"])

    def test_setup_handler_accepts_multiple_setup_pack_ids(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        install_result = {
            "success": True,
            "active_target_pack_id": "otherpack",
            "installed_setup_pack_ids": ["alpha", "beta"],
            "installed_target_pack_ids": ["alpha", "beta"],
        }
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked, patch(
            "core_runtime.api.setup_handlers.invoke_pack_function"
        ) as invoke, patch(
            "core_runtime.api.setup_handlers.get_container",
            return_value=self._FakeContainer(),
        ):
            mocked.return_value.install.return_value = install_result
            packs, payload = self._reviewed_payload(handler, ["alpha", "beta"])
            mocked.return_value.list_packs.return_value = {"packs": packs}
            result = handler._setup_install_pack(payload)

        mocked.return_value.install.assert_called_once_with(["alpha", "beta"])
        invoke.assert_not_called()
        self.assertEqual(
            result["migration_statuses"],
            {
                "alpha": {
                    "pack_id": "alpha",
                    "available": False,
                    "needs_user_migration": False,
                    "registry_available": False,
                    "reason": "function_registry_unavailable",
                },
                "beta": {
                    "pack_id": "beta",
                    "available": False,
                    "needs_user_migration": False,
                    "registry_available": False,
                    "reason": "function_registry_unavailable",
                },
            },
        )

    def test_setup_install_refreshes_running_kernel_approval_cache(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _ApprovalManager:
            def __init__(self):
                self.initialize_count = 0

            def initialize(self):
                self.initialize_count += 1

        class _Handler(SetupHandlersMixin):
            def __init__(self):
                self.approval_manager = _ApprovalManager()

        handler = _Handler()
        install_result = {
            "success": True,
            "active_target_pack_id": "defaultspack",
            "installed_setup_pack_ids": ["defaultspack"],
            "installed_target_pack_ids": ["defaultspack"],
        }
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked, patch(
            "core_runtime.api.setup_handlers.get_container",
            return_value=self._FakeContainer(),
        ):
            mocked.return_value.install.return_value = install_result
            packs, payload = self._reviewed_payload(handler, ["defaultspack"])
            mocked.return_value.list_packs.return_value = {"packs": packs}
            handler._setup_install_pack(payload)

        self.assertEqual(handler.approval_manager.initialize_count, 1)

    def test_defaults_profile_install_approves_the_reviewed_profile_pack_set(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        packs, payload = self._reviewed_payload(handler, ["defaultspack"])
        payload.update(
            {
                "install_defaults_profile": True,
                "confirmed_defaults_profile": True,
                "reviewed_default_profile_pack_ids": ["defaultspack", "tools"],
            }
        )
        install_result = {
            "success": True,
            "active_target_pack_id": "defaultspack",
            "installed_target_pack_ids": [],
        }
        approval_result = {
            "requested_pack_ids": ["defaultspack", "tools"],
            "approved_pack_ids": ["tools"],
            "already_approved_pack_ids": ["defaultspack"],
            "failed": [],
        }
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked, patch.object(
            SetupHandlersMixin,
            "_recommended_default_profile_preview",
            return_value={
                "pack_ids": ["defaultspack", "tools"],
                "setup_pack_ids": ["defaultspack"],
            },
        ), patch.object(
            SetupHandlersMixin,
            "_approve_defaults_profile_packs",
            return_value=approval_result,
        ) as approve, patch(
            "core_runtime.api.setup_handlers.get_container",
            return_value=self._FakeContainer(),
        ):
            mocked.return_value.list_packs.return_value = {"packs": packs}
            mocked.return_value.install.return_value = install_result
            result = handler._setup_install_pack(payload)

        approve.assert_called_once_with(["defaultspack", "tools"])
        self.assertEqual(result["default_profile_approval"], approval_result)

    def test_defaults_profile_install_rejects_an_unreviewed_profile_plan(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        packs, payload = self._reviewed_payload(handler, ["defaultspack"])
        payload.update(
            {
                "install_defaults_profile": True,
                "confirmed_defaults_profile": True,
                "reviewed_default_profile_pack_ids": ["defaultspack"],
            }
        )
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked, patch.object(
            SetupHandlersMixin,
            "_recommended_default_profile_preview",
            return_value={
                "pack_ids": ["defaultspack", "tools"],
                "setup_pack_ids": ["defaultspack"],
            },
        ):
            mocked.return_value.list_packs.return_value = {"packs": packs}
            result = handler._setup_install_pack(payload)

        self.assertEqual(result["status_code"], 409)
        mocked.return_value.install.assert_not_called()

    def test_setup_handler_runs_migration_for_active_setup_target_when_supported(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        install_result = {
            "success": True,
            "active_target_pack_id": "alpha",
            "installed_setup_pack_ids": ["alpha"],
            "installed_target_pack_ids": ["alpha"],
        }
        registry = self._FakeFunctionRegistry(
            {"alpha:get_migration_status", "alpha:run_migration"}
        )
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked, patch(
            "core_runtime.api.setup_handlers.invoke_pack_function",
            side_effect=[
                {"needs_user_migration": True},
                {"migrated": True},
                {"needs_user_migration": False},
            ],
        ) as invoke:
            mocked.return_value.install.return_value = install_result
            packs, payload = self._reviewed_payload(handler, ["alpha"])
            mocked.return_value.list_packs.return_value = {"packs": packs}
            with patch(
                "core_runtime.api.setup_handlers.get_container",
                return_value=self._FakeContainer(registry),
            ):
                result = handler._setup_install_pack(payload)

        mocked.return_value.install.assert_called_once_with(["alpha"])
        self.assertEqual(invoke.call_count, 3)
        self.assertEqual(result["migrations"], {"alpha": {"migrated": True}})
        self.assertEqual(
            result["migration_statuses"]["alpha"],
            {
                "pack_id": "alpha",
                "available": True,
                "needs_user_migration": False,
                "registry_available": True,
                "reason": None,
            },
        )

    def test_setup_handler_multi_pack_migration_handles_mixed_capabilities(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        install_result = {
            "success": True,
            "active_target_pack_id": "beta",
            "installed_setup_pack_ids": ["beta", "gamma"],
            "installed_target_pack_ids": ["beta", "gamma"],
        }
        registry = self._FakeFunctionRegistry(
            {"beta:get_migration_status", "beta:run_migration"}
        )

        def _invoke(pack_id, function_id):
            if (pack_id, function_id) == ("beta", "get_migration_status"):
                if not hasattr(_invoke, "seen"):
                    _invoke.seen = True
                    return {"needs_user_migration": True}
                return {"needs_user_migration": False}
            if (pack_id, function_id) == ("beta", "run_migration"):
                return {"migrated": True}
            raise AssertionError(f"unexpected invoke: {(pack_id, function_id)}")

        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked, patch(
            "core_runtime.api.setup_handlers.invoke_pack_function",
            side_effect=_invoke,
        ) as invoke:
            mocked.return_value.install.return_value = install_result
            packs, payload = self._reviewed_payload(handler, ["beta", "gamma"])
            mocked.return_value.list_packs.return_value = {"packs": packs}
            with patch(
                "core_runtime.api.setup_handlers.get_container",
                return_value=self._FakeContainer(registry),
            ):
                result = handler._setup_install_pack(payload)

        mocked.return_value.install.assert_called_once_with(["beta", "gamma"])
        self.assertEqual(invoke.call_count, 3)
        self.assertEqual(result["migrations"], {"beta": {"migrated": True}})
        self.assertEqual(result["migration_statuses"]["beta"]["available"], True)
        self.assertEqual(result["migration_statuses"]["beta"]["needs_user_migration"], False)
        self.assertEqual(
            result["migration_statuses"]["gamma"],
            {
                "pack_id": "gamma",
                "available": False,
                "needs_user_migration": False,
                "registry_available": True,
                "reason": "function_not_registered",
            },
        )

    def test_setup_get_migration_status_uses_active_setup_target(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        registry = self._FakeFunctionRegistry({"alpha:get_migration_status"})
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked, patch(
            "core_runtime.api.setup_handlers.get_container",
            return_value=self._FakeContainer(registry),
        ), patch(
            "core_runtime.api.setup_handlers.invoke_pack_function",
            return_value={"needs_user_migration": False},
        ) as invoke:
            mocked.return_value.get_selection.return_value = {"active_target_pack_id": "alpha"}
            result = handler._setup_get_migration_status()

        invoke.assert_called_once_with("alpha", "get_migration_status")
        self.assertEqual(
            result,
            {
                "pack_id": "alpha",
                "available": True,
                "needs_user_migration": False,
                "registry_available": True,
                "reason": None,
            },
        )

    def test_setup_get_migration_status_returns_unavailable_without_active_target(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked:
            mocked.return_value.get_selection.return_value = {}
            result = handler._setup_get_migration_status()

        self.assertEqual(
            result,
            {
                "pack_id": None,
                "available": False,
                "needs_user_migration": False,
                "registry_available": False,
                "reason": "active_target_not_selected",
            },
        )

    def test_setup_get_migration_status_distinguishes_registry_unavailable(self):
        from core_runtime.api.setup_handlers import SetupHandlersMixin

        class _Handler(SetupHandlersMixin):
            pass

        handler = _Handler()
        with patch(
            "core_runtime.api.setup_handlers.get_setup_pack_manager"
        ) as mocked, patch(
            "core_runtime.api.setup_handlers.get_container",
            return_value=self._FakeContainer(None),
        ):
            mocked.return_value.get_selection.return_value = {"active_target_pack_id": "alpha"}
            result = handler._setup_get_migration_status()

        self.assertEqual(
            result,
            {
                "pack_id": "alpha",
                "available": False,
                "needs_user_migration": False,
                "registry_available": False,
                "reason": "function_registry_unavailable",
            },
        )

    def test_core_setup_routes_are_declared(self):
        ecosystem_path = (
            Path(__file__).resolve().parent.parent
            / "core_runtime"
            / "core_pack"
            / "core_setup"
            / "ecosystem.json"
        )
        import json

        data = json.loads(ecosystem_path.read_text(encoding="utf-8"))
        routes = data.get("api_routes", [])
        self.assertEqual(len(routes), 5)
        self.assertTrue(any(route.get("path") == "/api/setup/packs" for route in routes))
        self.assertTrue(
            any(route.get("path_pattern") == "/api/setup/packs/{id}/grant-all-ok" for route in routes)
        )

    def test_mutation_routes_are_not_pre_auth(self):
        import json
        from core_runtime.pack_api_server import PackAPIHandler

        setup_ecosystem_path = (
            Path(__file__).resolve().parent.parent
            / "core_runtime"
            / "core_pack"
            / "core_setup"
            / "ecosystem.json"
        )
        defaultspack_ecosystem_path = (
            Path(__file__).resolve().parent.parent
            / "ecosystem"
            / "defaultspack"
            / "ecosystem.json"
        )
        setup_data = json.loads(setup_ecosystem_path.read_text(encoding="utf-8"))
        defaultspack_data = json.loads(defaultspack_ecosystem_path.read_text(encoding="utf-8"))

        class _PackInfo:
            def __init__(self, ecosystem):
                self.ecosystem = ecosystem

        class _Registry:
            packs = {
                "core_setup": _PackInfo(setup_data),
                "defaultspack": _PackInfo(defaultspack_data),
            }

        PackAPIHandler.load_pre_auth_routes(_Registry())
        handler = PackAPIHandler.__new__(PackAPIHandler)
        self.assertFalse(handler._is_pre_auth_route("POST", "/api/setup/packs/install"))
        self.assertFalse(handler._is_pre_auth_route("POST", "/api/defaultspack/pack-requests/request-extension"))
        self.assertTrue(handler._is_pre_auth_route("GET", "/api/setup/status"))

    def test_control_panel_requires_session_except_bootstrap_exchange(self):
        import json
        from core_runtime.pack_api_server import PackAPIHandler

        control_panel_ecosystem_path = (
            Path(__file__).resolve().parent.parent
            / "core_runtime"
            / "core_pack"
            / "core_control_panel"
            / "ecosystem.json"
        )
        control_panel_data = json.loads(control_panel_ecosystem_path.read_text(encoding="utf-8"))

        self.assertTrue(control_panel_data["web_mount"]["auth_required"])
        self.assertEqual(
            control_panel_data["pre_auth_routes"],
            [
                {"method": "POST", "path": "/api/panel/auth/bootstrap"},
                {"method": "POST", "path": "/api/panel/auth/exchange"},
            ],
        )

        class _PackInfo:
            def __init__(self, ecosystem):
                self.ecosystem = ecosystem

        class _Registry:
            packs = {
                "core_control_panel": _PackInfo(control_panel_data),
            }

        PackAPIHandler.load_pre_auth_routes(_Registry())
        handler = PackAPIHandler.__new__(PackAPIHandler)
        self.assertTrue(handler._is_pre_auth_route("POST", "/api/panel/auth/bootstrap"))
        self.assertTrue(handler._is_pre_auth_route("POST", "/api/panel/auth/exchange"))
        self.assertFalse(handler._is_pre_auth_route("GET", "/api/panel/dashboard"))
        self.assertFalse(handler._is_pre_auth_route("POST", "/api/panel/flows"))

    def test_core_setup_web_uses_moved_setup_routes_only(self):
        web_path = (
            Path(__file__).resolve().parent.parent
            / "core_runtime"
            / "core_pack"
            / "core_setup"
            / "web"
            / "index.html"
        )
        source = web_path.read_text(encoding="utf-8")
        self.assertIn("/api/setup/packs/install", source)
        self.assertIn("/api/setup/migration/status", source)
        self.assertNotIn("/api/defaultspack/setup", source)
        self.assertNotIn(
            "Checked setup packs are installed together and receive all OK permissions.",
            source,
        )
        self.assertIn(
            "All OK を明示的にサポートする setup pack にだけ、個別の承認後に限定的な許可を付与します。",
            source,
        )
        self.assertIn("All OK 許可なしでインストール", source)
        self.assertIn("return_to", source)
        self.assertIn("active_target_not_selected", source)
        self.assertIn("有効な setup pack が選択されていません", source)
        self.assertIn("payloadError", source)
        self.assertIn("payloadErrorItem", source)
        self.assertIn("PANEL_CSRF_STORAGE_KEY", source)
        self.assertIn('"X-Rumi-CSRF"', source)
        self.assertIn('credentials: "same-origin"', source)
        self.assertIn("name.textContent = setupPackName(pack)", source)
        self.assertIn("description.textContent = pack.description", source)
        self.assertIn("input.dataset.selectPack = pack.pack_id", source)
        self.assertIn("document.createTextNode", source)
        self.assertIn('url.pathname === "/panel"', source)
        self.assertIn('url.pathname.startsWith("/panel/")', source)
        self.assertIn("!packs.active_target_pack_id", source)
        self.assertIn("payload.success === false", source)
        self.assertNotIn("card.innerHTML", source)
        self.assertNotIn("${pack.display_name}", source)
        self.assertNotIn("${pack.description", source)
        self.assertNotIn('data-select-pack="${pack.pack_id}"', source)
        self.assertNotIn('url.pathname.startsWith("/panel"))', source)
        self.assertNotIn("payload.errors.map(String)", source)
        self.assertNotIn(
            'migrationEl.textContent = migration.needs_user_migration ? "user.csv migration pending" : "ready"',
            source,
        )
        install_error_pattern = (
            r'async function installSelectedPacks\(\)[\s\S]*'
            r'getJson\("/api/setup/packs/install"[\s\S]*'
            r'catch \(error\) \{[\s\S]*'
            r'setStatus\("setup pack のインストールに失敗しました"'
        )
        self.assertRegex(source, install_error_pattern)


if __name__ == "__main__":
    unittest.main()
