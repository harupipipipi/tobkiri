from __future__ import annotations

import json
import base64
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _assert_v4_ui_boundary() -> None:
    """Require UI dispatch to use verified Pack v4 ownership and authority."""
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )
    from tests.v4_batch_support import assert_legacy_registry_fails_closed

    assert_retired_module_absent("domain.function_runtime.bridge")
    assert_retired_module_absent("core_runtime.interface_registry")
    assert_legacy_registry_fails_closed()
    assert_profile_resolver_requires_authority_snapshot()


class TestDefaultspackUiRegistry(unittest.TestCase):
    @staticmethod
    def _jwt(payload):
        def encode(obj):
            raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        return f"{encode({'alg': 'none'})}.{encode(payload)}."

    def test_frontend_registry_accepts_lightweight_route_keyword(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = FrontendRegistry(pack_root=Path(tmpdir))
            catalog = registry.build_catalog(lightweight=True)
            settings = registry.get_settings(lightweight=True)

        self.assertIn("sidebar", catalog)
        self.assertIn("sections", settings)
        self.assertIn("values", settings)

    def test_lightweight_catalog_can_include_skills_without_full_hydration(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = FrontendRegistry(pack_root=Path(tmpdir))
            expected = [{"id": "settings_assistant", "label": "Settings"}]
            with patch.object(registry, "_skill_items", return_value=expected):
                catalog = registry.build_catalog(lightweight=True, include_skills=True)

        self.assertEqual(catalog["skills"], expected)

    def test_catalog_merges_tool_registry_and_extension_manifest(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir) / "ecosystem" / "defaultspack"
            (pack_root / "pack.v4.json").parent.mkdir(parents=True, exist_ok=True)
            (pack_root / "pack.v4.json").write_text(json.dumps({"pack": {"id": "defaultspack"}}), encoding="utf-8")
            ext_dir = pack_root / "frontend_extensions"
            ext_dir.mkdir(parents=True, exist_ok=True)
            (ext_dir / "extra.ui.json").write_text(
                json.dumps(
                    {
                        "sidebar_items": [
                            {
                                "id": "custom-widget",
                                "label": "Custom Widget",
                                "category": "widget",
                            }
                        ],
                        "settings_sections": [
                            {
                                "id": "custom",
                                "label": "Custom",
                                "fields": [
                                    {
                                        "id": "enabled",
                                        "label": "Enabled",
                                        "type": "toggle",
                                        "default": True,
                                    }
                                ],
                            }
                        ],
                        "chat_renderers": [
                            {"id": "text", "component": "CustomText", "block_types": ["text"]},
                            {
                                "id": "custom-renderer",
                                "component": "Custom",
                                "block_types": ["custom"],
                            },
                        ],
                        "shell_renderers": [
                            {
                                "id": "composer",
                                "component": "CustomComposer",
                                "regions": ["composer"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch("domain.frontend.registry.AIClient") as mock_client, patch(
                "domain.frontend.registry.selected_extension_pack_ids",
                return_value={"defaultspack"},
            ), patch(
                "domain.tool.catalog_contract_client._invoke",
                return_value={
                    "definitions": [
                        {
                            "tool_id": "contract-tool",
                            "display_name": "Contract Tool",
                            "description": "A v4 contract tool.",
                            "input_schema": {"type": "object", "properties": {}},
                            "execution": {},
                            "risk": "low",
                            "policy_tags": [],
                            "aliases": [],
                            "widget": {},
                            "authority": "service.invoke",
                        }
                    ]
                },
            ):
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                registry = FrontendRegistry(pack_root=pack_root)
                catalog = registry.build_catalog()

        sidebar_ids = {item["id"] for item in catalog["sidebar"]["items"]}
        section_ids = {section["id"] for section in catalog["settings"]["sections"]}
        renderers = {
            renderer["id"]: renderer for renderer in catalog["chat_rendering"]["renderers"]
        }
        shell_renderers = {renderer["id"]: renderer for renderer in catalog["shell"]["renderers"]}
        part_ids = {part["id"] for part in catalog["parts"]}
        parts = {part["id"]: part for part in catalog["parts"]}
        binding_part_ids = {binding["part_id"] for binding in catalog["component_bindings"]}

        self.assertIn("contract-tool", sidebar_ids)
        self.assertIn("custom-widget", sidebar_ids)
        self.assertIn("custom", section_ids)
        self.assertIn("system_info", section_ids)
        system_info_section = next(
            section for section in catalog["settings"]["sections"] if section["id"] == "system_info"
        )
        self.assertEqual(system_info_section["label"], "System Info")
        tools_section = next(
            section for section in catalog["settings"]["sections"] if section["id"] == "tools"
        )
        tools_field_ids = {field["id"] for field in tools_section["fields"]}
        self.assertIn("default_mode", tools_field_ids)
        self.assertIn("selection_strategy", tools_field_ids)
        default_mode_field = next(field for field in tools_section["fields"] if field["id"] == "default_mode")
        strategy_field = next(field for field in tools_section["fields"] if field["id"] == "selection_strategy")
        self.assertEqual(catalog["settings"]["values"]["tools"]["settings_version"], 3)
        self.assertEqual(catalog["settings"]["values"]["tools"]["default_mode"], "auto")
        self.assertEqual(catalog["settings"]["values"]["tools"]["selection_strategy"], "hybrid")
        default_mode_options = {option["value"] for option in default_mode_field["options"]}
        strategy_options = {option["value"] for option in strategy_field["options"]}
        self.assertIn("auto", default_mode_options)
        self.assertIn("manual", default_mode_options)
        self.assertIn("all_schemas", strategy_options)
        general_section = next(section for section in catalog["settings"]["sections"] if section["id"] == "general")
        general_field_ids = {field["id"] for field in general_section["fields"]}
        self.assertIn("language", general_field_ids)
        self.assertEqual(catalog["settings"]["values"]["general"]["language"], "ja")
        models_section = next(
            section for section in catalog["settings"]["sections"] if section["id"] == "models"
        )
        models_field_ids = {field["id"] for field in models_section["fields"]}
        self.assertEqual(len(models_field_ids), len(models_section["fields"]))
        self.assertNotIn("research", section_ids)
        self.assertNotIn("browser_computer", section_ids)
        self.assertNotIn("collaboration", section_ids)
        self.assertNotIn("share", section_ids)
        self.assertIn("custom-renderer", renderers)
        self.assertEqual(renderers["text"]["component"], "CustomText")
        self.assertEqual(shell_renderers["composer"]["component"], "CustomComposer")
        self.assertEqual(catalog["shell"]["layout"]["id"], "default_chat_shell")
        self.assertIn("ai_chat", part_ids)
        self.assertIn("conversation_history", part_ids)
        self.assertIn("extension_sidebar", part_ids)
        self.assertIn("tool_timeline", parts["activity_preview"]["schema"]["properties"])
        self.assertIn("approvals", parts["activity_preview"]["schema"]["properties"])
        self.assertIn("audio", parts["activity_preview"]["schema"]["properties"])
        self.assertIn("messages", parts["ai_chat"]["schema"]["properties"])
        self.assertIn("ai_chat", binding_part_ids)
        self.assertEqual(catalog["app"]["icon"], "/static/assets/icons/defaultspack-icon.png")
        self.assertEqual(catalog["diagnostics"], [])

    def test_default_template_settings_fields_replace_legacy_base_fields(self):
        from domain.frontend.registry import FrontendRegistry

        with patch("domain.frontend.registry.AIClient") as mock_client:
            mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
            catalog = FrontendRegistry(pack_root=DEFAULTSPACK_ROOT).build_catalog()

        section_ids = {section["id"] for section in catalog["settings"]["sections"]}
        self.assertIn("calendar", section_ids)

        tools_section = next(
            section for section in catalog["settings"]["sections"] if section["id"] == "tools"
        )
        tools_field_ids = {field["id"] for field in tools_section["fields"]}
        self.assertEqual(len(tools_field_ids), len(tools_section["fields"]))
        tool_assist_field = next(
            field for field in tools_section["fields"] if field["id"] == "tool_assist_mode"
        )
        self.assertEqual(tool_assist_field["template_id"], "rumi.composer.default")
        self.assertIn("vector", {option["value"] for option in tool_assist_field["options"]})

        models_section = next(
            section for section in catalog["settings"]["sections"] if section["id"] == "models"
        )
        models_field_ids = {field["id"] for field in models_section["fields"]}
        self.assertEqual(len(models_field_ids), len(models_section["fields"]))
        preferred_model_field = next(
            field for field in models_section["fields"] if field["id"] == "preferred_model"
        )
        self.assertEqual(preferred_model_field["type"], "model_select")
        self.assertEqual(preferred_model_field["template_id"], "rumi.model_selector.default")
        self.assertIn("model_api_routes", models_field_ids)
        route_field = next(
            field for field in models_section["fields"] if field["id"] == "model_api_routes"
        )
        self.assertEqual(route_field["type"], "model_api_routes")
        self.assertEqual(route_field["template_id"], "rumi.backend.model_routing.default")
        self.assertIsInstance(route_field.get("options"), list)
        self.assertTrue(route_field["options"])
        self.assertIn("api_keys", route_field)

        apis_section = next(
            section for section in catalog["settings"]["sections"] if section["id"] == "apis"
        )
        apis_field_ids = {field["id"] for field in apis_section["fields"]}
        self.assertEqual(len(apis_field_ids), len(apis_section["fields"]))
        self.assertIn("api_keys", apis_field_ids)
        api_keys_field = next(
            field for field in apis_section["fields"] if field["id"] == "api_keys"
        )
        self.assertEqual(api_keys_field["type"], "api_key_setup")
        self.assertEqual(api_keys_field["template_id"], "rumi.api_keys.default")
        self.assertNotIn("model_api_routes", apis_field_ids)

        calendar_section = next(
            section for section in catalog["settings"]["sections"] if section["id"] == "calendar"
        )
        self.assertTrue(calendar_section["fields"])
        self.assertTrue(
            all(
                field.get("template_id") == "rumi.calendar.default"
                for field in calendar_section["fields"]
            )
        )
        commands_section = next(
            section for section in catalog["settings"]["sections"] if section["id"] == "commands"
        )
        command_field = next(
            field for field in commands_section["fields"] if field["id"] == "show_advanced_commands"
        )
        self.assertEqual(command_field["template_id"], "rumi.composer.default")

    def test_catalog_merges_template_shell_projection_and_catalog_buckets(self):
        from domain.frontend.registry import FrontendRegistry

        template_meta = {
            "template_id": "template.shell",
            "trust_level": "builtin",
            "origin": {"kind": "template", "template_id": "template.shell"},
            "_source": "templates/shell/template.json",
        }
        template_catalog = FrontendRegistry._empty_template_catalog()
        template_catalog.update(
            {
                "commands": [
                    {
                        **template_meta,
                        "id": "context_txt",
                        "piece_id": "context_txt_command",
                        "projected_id": "template.shell:context_txt_command",
                    }
                ],
                "composer_inputs": [
                    {
                        **template_meta,
                        "id": "default_composer",
                        "piece_id": "default_composer_input",
                        "projected_id": "template.shell:default_composer_input",
                        "region_id": "composer",
                        "renderer": "composer",
                    }
                ],
                "context_policies": [
                    {
                        **template_meta,
                        "id": "materialize_txt",
                        "piece_id": "materialize_txt_policy",
                        "projected_id": "template.shell:materialize_txt_policy",
                    }
                ],
                "shell_regions": [
                    {
                        **template_meta,
                        "id": "template_sidecar",
                        "piece_id": "template_sidecar_region",
                        "projected_id": "template.shell:template_sidecar_region",
                        "part_id": "ai_chat",
                        "renderer": "template_sidecar",
                        "slot": "main",
                        "order": 45,
                        "enabled": True,
                    },
                    {
                        **template_meta,
                        "id": "composer",
                        "piece_id": "composer_shell_region",
                        "projected_id": "template.shell:composer_shell_region",
                        "part_id": "ai_chat",
                        "renderer": "composer",
                        "slot": "bottom",
                        "order": 1,
                        "enabled": False,
                    },
                ],
                "shell_renderers": [
                    {
                        **template_meta,
                        "id": "template_sidecar",
                        "piece_id": "template_sidecar_renderer",
                        "projected_id": "template.shell:template_sidecar_renderer",
                        "component": "TemplateSidecar",
                        "regions": ["template_sidecar"],
                        "fallback": "hidden",
                    },
                    {
                        **template_meta,
                        "id": "composer",
                        "piece_id": "composer_shell_renderer",
                        "projected_id": "template.shell:composer_shell_renderer",
                        "component": "TemplateComposer",
                        "regions": ["composer"],
                        "fallback": "plain_text",
                    },
                ],
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir) / "ecosystem" / "defaultspack"
            with (
                patch("domain.frontend.registry.AIClient") as mock_client,
                patch.object(
                    FrontendRegistry, "_template_catalog_metadata", return_value=template_catalog
                ),
            ):
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                catalog = FrontendRegistry(pack_root=pack_root).build_catalog()

        regions = {region["id"]: region for region in catalog["shell"]["layout"]["regions"]}
        renderers = {renderer["id"]: renderer for renderer in catalog["shell"]["renderers"]}

        self.assertEqual({item["id"] for item in catalog["commands"]}, {"context_txt"})
        self.assertEqual({item["id"] for item in catalog["composer_inputs"]}, {"default_composer"})
        self.assertEqual({item["id"] for item in catalog["context_policies"]}, {"materialize_txt"})
        self.assertEqual(
            {item["id"] for item in catalog["shell_regions"]}, {"composer", "template_sidecar"}
        )
        self.assertEqual(
            {item["id"] for item in catalog["shell_renderers"]}, {"composer", "template_sidecar"}
        )
        self.assertEqual(regions["template_sidecar"]["renderer"], "template_sidecar")
        self.assertEqual(regions["template_sidecar"]["template_id"], "template.shell")
        self.assertEqual(renderers["template_sidecar"]["component"], "TemplateSidecar")
        self.assertEqual(renderers["template_sidecar"]["trust_level"], "builtin")
        self.assertEqual(regions["composer"]["order"], 50)
        self.assertTrue(regions["composer"]["enabled"])
        self.assertEqual(regions["composer"]["template_id"], "template.shell")
        self.assertEqual(renderers["composer"]["component"], "Composer")
        self.assertEqual(renderers["composer"]["fallback"], "hidden")
        self.assertEqual(
            renderers["composer"]["projected_id"], "template.shell:composer_shell_renderer"
        )

    def test_catalog_filters_profile_visibility_for_selected_frontend_ids(self):
        from core_runtime.profile_workspace import ProfileWorkspaceManager
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir) / "ecosystem" / "defaultspack"
            (pack_root / "pack.v4.json").parent.mkdir(parents=True, exist_ok=True)
            (pack_root / "pack.v4.json").write_text(json.dumps({"pack": {"id": "defaultspack"}}), encoding="utf-8")
            ext_dir = pack_root / "frontend_extensions"
            ext_dir.mkdir(parents=True, exist_ok=True)
            (ext_dir / "profile.ui.json").write_text(
                json.dumps(
                    {
                        "sidebar_items": [
                            {
                                "id": "research_sidebar",
                                "label": "Research Sidebar",
                                "category": "widget",
                                "profile_visibility": {
                                    "selected_frontend_ids": ["research_sidebar"]
                                },
                            },
                            {
                                "id": "coding_sidebar",
                                "label": "Coding Sidebar",
                                "category": "widget",
                                "profile_visibility": {"selected_frontend_ids": ["coding_sidebar"]},
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            user_data_root = Path(tmpdir) / "rumi_user_data"
            profile_manager = ProfileWorkspaceManager(user_data_root)
            profile_manager.initialize_profile_workspace(
                {
                    "profile_id": "research-profile",
                    "name": "Research Profile",
                    "metadata": {"selected": {"frontend": ["research_sidebar"]}},
                    "policy": {},
                }
            )

            with patch.dict(os.environ, {"RUMI_USER_DATA": str(user_data_root)}), patch(
                "domain.frontend.registry.selected_extension_pack_ids",
                return_value={"defaultspack"},
            ):
                registry = FrontendRegistry(pack_root=pack_root)
                catalog = registry.build_catalog(profile_id="research-profile")

        sidebar_ids = {item["id"] for item in catalog["sidebar"]["items"]}
        self.assertIn("research_sidebar", sidebar_ids)
        self.assertIn("coding_sidebar", sidebar_ids)

    def test_frontend_extensions_filter_to_selected_setup_targets(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            rumi_root = Path(tmpdir) / "tobkiri_runtime"
            ecosystem_root = rumi_root / "ecosystem"

            def make_pack(pack_id: str) -> Path:
                pack_root = ecosystem_root / pack_id
                pack_root.mkdir(parents=True, exist_ok=True)
                (pack_root / "pack.v4.json").write_text(
                    json.dumps({"pack": {"id": pack_id}}),
                    encoding="utf-8",
                )
                ext_dir = pack_root / "frontend_extensions"
                ext_dir.mkdir(parents=True, exist_ok=True)
                (ext_dir / f"{pack_id}.ui.json").write_text(
                    json.dumps(
                        {
                            "sidebar_items": [
                                {
                                    "id": f"{pack_id}-item",
                                    "label": pack_id,
                                    "category": "widget",
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return pack_root

            pack_root = make_pack("defaultspack")
            make_pack("pack_a")
            make_pack("pack_b")
            selection_path = rumi_root / "user_data" / "settings" / "setup_pack_selection.json"
            selection_path.parent.mkdir(parents=True, exist_ok=True)
            selection_path.write_text(
                json.dumps(
                    {
                        "target_pack_ids": ["pack_a"],
                        "active_target_pack_id": "pack_a",
                    }
                ),
                encoding="utf-8",
            )
            user_ext_dir = pack_root / "user_data" / "shared" / "frontend_extensions"
            user_ext_dir.mkdir(parents=True, exist_ok=True)
            (user_ext_dir / "overlay.ui.json").write_text(
                json.dumps(
                    {
                        "sidebar_items": [
                            {
                                "id": "user-overlay-item",
                                "label": "User Overlay",
                                "category": "widget",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch("domain.frontend.registry.AIClient") as mock_client, patch(
                "domain.frontend.registry.selected_extension_pack_ids",
                return_value={"defaultspack", "pack_a"},
            ):
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                catalog = FrontendRegistry(pack_root=pack_root).build_catalog()

        sidebar_ids = {item["id"] for item in catalog["sidebar"]["items"]}
        self.assertIn("defaultspack-item", sidebar_ids)
        self.assertIn("pack_a-item", sidebar_ids)
        self.assertNotIn("user-overlay-item", sidebar_ids)
        self.assertNotIn("pack_b-item", sidebar_ids)

    def test_chat_send_builds_multimodal_attachment_blocks(self):
        from blocks.chat.send import (
            _attachment_image_blocks,
            _sanitize_attachment_metadata,
        )
        from domain.chat.message_converter import convert_to_standard

        attachments = [
            {
                "id": "image-1",
                "name": "sample.png",
                "size": 128,
                "type": "image/png",
                "dataUrl": "data:image/png;base64,iVBORw0KGgo=",
            }
        ]

        content = [{"type": "text", "text": "画像を見て"}]
        content.extend(_attachment_image_blocks(attachments))
        standard = convert_to_standard([{"role": "user", "content": content}])

        self.assertEqual(standard[0]["content"][0]["text"], "画像を見て")
        self.assertEqual(
            standard[0]["content"][1]["image_url"]["url"],
            "data:image/png;base64,iVBORw0KGgo=",
        )
        self.assertNotIn("dataUrl", _sanitize_attachment_metadata(attachments)[0])

    def test_fallback_http_routes_do_not_repeat_method_pattern_pairs(self):
        from transport.registry import _FALLBACK_HTTP_ROUTE_SPECS

        seen = set()
        duplicates = []
        for spec in _FALLBACK_HTTP_ROUTE_SPECS:
            key = (spec.method, spec.pattern)
            if key in seen:
                duplicates.append(key)
            seen.add(key)

        self.assertEqual(duplicates, [])

    def test_catalog_syncs_rumi_account_from_oauth_payload(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            rumi_root = Path(tmpdir) / "tobkiri_runtime"
            pack_root = rumi_root / "ecosystem" / "defaultspack"
            token_path = rumi_root / "user_data" / "settings" / "oauth_tokens.json"
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(
                json.dumps(
                    {
                        "access_token": self._jwt(
                            {
                                "email": "user@example.test",
                                "user_metadata": {
                                    "full_name": "Rumi User",
                                    "avatar_url": "https://example.test/avatar.png",
                                },
                                "app_metadata": {"plan": "Pro Plan"},
                            }
                        )
                    }
                ),
                encoding="utf-8",
            )

            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                catalog = FrontendRegistry(pack_root=pack_root).build_catalog()

        account = catalog["app"]["account"]
        self.assertEqual(account["display_name"], "Rumi User")
        self.assertEqual(account["email"], "user@example.test")
        self.assertEqual(account["plan_label"], "Pro Plan")
        self.assertEqual(account["avatar_url"], "https://example.test/avatar.png")
        self.assertEqual(account["source"], "rumi_oauth")

    def test_catalog_prefers_rumi_profile_for_account(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            rumi_root = Path(tmpdir) / "tobkiri_runtime"
            pack_root = rumi_root / "ecosystem" / "defaultspack"
            profile_path = rumi_root / "user_data" / "settings" / "profile.json"
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(
                json.dumps(
                    {
                        "username": "Profile Name",
                        "language": "ja",
                        "icon": "https://example.test/profile.png",
                        "plan": "Team Plan",
                    }
                ),
                encoding="utf-8",
            )

            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                catalog = FrontendRegistry(pack_root=pack_root).build_catalog()

        account = catalog["app"]["account"]
        self.assertEqual(account["display_name"], "Profile Name")
        self.assertEqual(account["plan_label"], "Team Plan")
        self.assertEqual(account["avatar_url"], "https://example.test/profile.png")
        self.assertEqual(account["source"], "rumi_profile")

    def test_malformed_frontend_shell_config_falls_back(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir) / "ecosystem" / "defaultspack"
            shell_path = pack_root / "user_data" / "shared" / "frontend_shell.json"
            shell_path.parent.mkdir(parents=True, exist_ok=True)
            shell_path.write_text("{not json", encoding="utf-8")

            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                catalog = FrontendRegistry(pack_root=pack_root).build_catalog()

        self.assertEqual(catalog["shell"]["layout"]["id"], "default_chat_shell")
        self.assertTrue(
            any(region["id"] == "chat_messages" for region in catalog["shell"]["layout"]["regions"])
        )
        self.assertFalse(
            any(item["code"] == "frontend_shell_invalid_json" for item in catalog["diagnostics"])
        )

    def test_catalog_reports_frontend_contract_diagnostics(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir) / "ecosystem" / "defaultspack"
            (pack_root / "pack.v4.json").parent.mkdir(parents=True, exist_ok=True)
            (pack_root / "pack.v4.json").write_text(json.dumps({"pack": {"id": "defaultspack"}}), encoding="utf-8")
            ext_dir = pack_root / "frontend_extensions"
            ext_dir.mkdir(parents=True, exist_ok=True)
            (ext_dir / "bad.ui.json").write_text(
                json.dumps(
                    {
                        "parts": [
                            {"id": "bad_part", "kind": "", "schema": []},
                        ],
                        "component_bindings": [
                            {"part_id": "missing_part", "component": "", "requires": "ai_client"},
                        ],
                        "shell_renderers": [
                            {"id": "bad_renderer", "component": "", "regions": "composer"},
                            {
                                "id": "remote_renderer",
                                "component": "Remote",
                                "module": "https://example.com/remote.js",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch("domain.frontend.registry.AIClient") as mock_client, patch(
                "domain.frontend.registry.selected_extension_pack_ids",
                return_value={"defaultspack"},
            ):
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                catalog = FrontendRegistry(pack_root=pack_root).build_catalog()

        codes = {item["code"] for item in catalog["diagnostics"]}
        self.assertIn("part_missing_kind", codes)
        self.assertIn("part_invalid_schema", codes)
        self.assertIn("binding_unknown_part", codes)
        self.assertIn("binding_missing_component", codes)
        self.assertIn("binding_invalid_requires", codes)
        self.assertIn("shell_renderer_missing_component", codes)
        self.assertIn("shell_renderer_invalid_regions", codes)
        self.assertIn("shell_renderer_untrusted_module", codes)
        self.assertIn("shell_renderer_missing_local_trust", codes)

    def test_update_settings_persists_values(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir) / "ecosystem" / "defaultspack"
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                registry = FrontendRegistry(pack_root=pack_root)
                values = registry.update_settings({"preview": {"auto_open": True, "max_items": 5}})
                reloaded = registry.get_settings()["values"]

        self.assertTrue(values["preview"]["auto_open"])
        self.assertEqual(values["preview"]["max_items"], 5)
        self.assertTrue(reloaded["preview"]["auto_open"])
        self.assertEqual(reloaded["preview"]["max_items"], 5)

    def test_lightweight_settings_do_not_hydrate_model_catalog(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            registry = FrontendRegistry(pack_root=pack_root)
            with patch.object(
                FrontendRegistry,
                "_selectable_model_profiles",
                side_effect=AssertionError("bootstrap settings must not build model profiles"),
            ):
                settings = registry.get_settings(lightweight=True)

        self.assertIn("sections", settings)
        self.assertIn("values", settings)
        self.assertIn("models", settings["values"])

    def test_lightweight_catalog_does_not_hydrate_model_catalog(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir) / "ecosystem" / "defaultspack"
            (pack_root / "pack.v4.json").parent.mkdir(parents=True, exist_ok=True)
            (pack_root / "pack.v4.json").write_text(json.dumps({"pack": {"id": "defaultspack"}}), encoding="utf-8")
            ext_dir = pack_root / "frontend_extensions"
            ext_dir.mkdir(parents=True, exist_ok=True)
            (ext_dir / "models.ui.json").write_text(
                json.dumps(
                    {
                        "sidebar_items": [
                            {
                                "id": "extension-models",
                                "label": "Extension Models",
                                "category": "system",
                                "panel": {"kind": "models", "title": "Extension Models"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = FrontendRegistry(pack_root=pack_root)
            with patch("domain.frontend.registry.selected_extension_pack_ids", return_value={"defaultspack"}), patch.object(
                FrontendRegistry,
                "_selectable_model_profiles",
                side_effect=AssertionError("bootstrap catalog must not build model profiles"),
            ), patch.object(
                FrontendRegistry,
                "_list_provider_models",
                side_effect=AssertionError("bootstrap catalog must not list provider models"),
            ):
                catalog = registry.build_catalog(lightweight=True)

        self.assertIn("sidebar", catalog)
        self.assertIn("items", catalog["sidebar"])
        self.assertEqual(catalog["skills"], [])
        item = next(
            candidate
            for candidate in catalog["sidebar"]["items"]
            if candidate["id"] == "extension-models"
        )
        self.assertEqual(item["panel"]["kind"], "models")
        self.assertNotIn("models", item["panel"])

    def test_selectable_model_profiles_are_cached_across_bootstrap_instances(self):
        from domain.frontend.registry import FrontendRegistry
        from ecosystem.defaultspack.backend.ai_client import provider_catalog

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            FrontendRegistry._selectable_model_profiles_cache.clear()
            profiles = [
                {
                    "profile_id": "stub/default",
                    "display_name": "Stub Default",
                    "provider_id": "stub",
                    "model_id": "default",
                    "type": "chat",
                    "availability": {"configured": True, "local": True},
                }
            ]
            with patch.object(provider_catalog, "list_profile_catalog", return_value=profiles) as mocked:
                first = FrontendRegistry(pack_root=pack_root)._selectable_model_profiles()
                second = FrontendRegistry(pack_root=pack_root)._selectable_model_profiles()

        self.assertEqual(first, second)
        self.assertEqual(mocked.call_count, 1)

    def test_all_invokable_non_catalog_profiles_are_user_selectable(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = FrontendRegistry(pack_root=Path(tmpdir))

            self.assertTrue(
                registry._is_user_selectable_profile(
                    {
                        "profile_id": "opencode-zen/minimax-m3-free",
                        "provider_id": "opencode-zen",
                        "model_id": "minimax-m3-free",
                        "type": "chat",
                        "availability": {
                            "configured": False,
                            "catalog_only": False,
                            "supports_invoke": True,
                        },
                    }
                )
            )
            self.assertFalse(
                registry._is_user_selectable_profile(
                    {
                        "profile_id": "catalog/example",
                        "provider_id": "catalog",
                        "model_id": "example",
                        "type": "chat",
                        "availability": {
                            "catalog_only": True,
                            "supports_invoke": True,
                        },
                    }
                )
            )

    def test_model_route_options_include_search_and_capability_metadata(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = FrontendRegistry(pack_root=Path(tmpdir))
            with patch.object(
                registry,
                "_selectable_model_profiles",
                return_value=[
                    {
                        "profile_id": "opencode-zen/minimax-m3-free",
                        "display_name": "MiniMax M3 Free",
                        "provider_id": "opencode-zen",
                        "provider_display_name": "OpenCode Zen",
                        "model_id": "minimax-m3-free",
                        "supports_tool_calling": True,
                        "capability_tags": ["tools"],
                        "availability": {
                            "configured": False,
                            "supports_invoke": True,
                        },
                    }
                ],
            ):
                options = registry._model_route_options()

        self.assertEqual(options[0]["provider_display_name"], "OpenCode Zen")
        self.assertTrue(options[0]["requires_api_key"])
        self.assertTrue(options[0]["supports_tool_calling"])
        self.assertEqual(options[0]["capability_tags"], ["tools"])

    def test_computer_use_haze_settings_are_exposed_and_sanitized(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                registry = FrontendRegistry(pack_root=pack_root)
                settings = registry.get_settings()
                values = registry.update_settings(
                    {
                        "computer_use_haze": {
                            "enabled": True,
                            "preset": "unknown",
                            "start_color": "bad",
                            "end_color": "#112233",
                            "accent_color": "#aabbcc",
                            "opacity": 42,
                            "edge_width": 1,
                            "animation_speed": "fast",
                        }
                    }
                )

        haze_section = next(
            section for section in settings["sections"] if section["id"] == "computer_use_haze"
        )
        field_types = {field["id"]: field["type"] for field in haze_section["fields"]}
        self.assertEqual(field_types["start_color"], "color")
        self.assertEqual(field_types["end_color"], "color")
        self.assertEqual(field_types["accent_color"], "color")
        self.assertEqual(settings["values"]["computer_use_haze"]["preset"], "aurora")
        self.assertEqual(values["computer_use_haze"]["preset"], "aurora")
        self.assertEqual(values["computer_use_haze"]["start_color"], "#6EE7F9")
        self.assertEqual(values["computer_use_haze"]["end_color"], "#112233")
        self.assertEqual(values["computer_use_haze"]["accent_color"], "#AABBCC")
        self.assertEqual(values["computer_use_haze"]["opacity"], 0.9)
        self.assertEqual(values["computer_use_haze"]["edge_width"], 40)
        self.assertEqual(values["computer_use_haze"]["animation_speed"], 1)

    def test_keyboard_button_navigation_defaults_on(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                settings = FrontendRegistry(pack_root=pack_root).get_settings()

        general = settings["values"]["general"]
        field_ids = {
            field["id"]
            for section in settings["sections"]
            if section["id"] == "general"
            for field in section["fields"]
        }
        self.assertTrue(general["keyboard_button_navigation"])
        self.assertIn("keyboard_button_navigation", field_ids)
        self.assertFalse(general["manual_runtime_mode_selection"])
        self.assertIn("manual_runtime_mode_selection", field_ids)

    def test_manual_runtime_mode_selection_requires_explicit_boolean_true(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            registry = FrontendRegistry(pack_root=pack_root)
            enabled = registry.update_settings(
                {"general": {"manual_runtime_mode_selection": True}}
            )
            malformed = registry.update_settings(
                {"general": {"manual_runtime_mode_selection": "true"}}
            )

        self.assertTrue(enabled["general"]["manual_runtime_mode_selection"])
        self.assertFalse(malformed["general"]["manual_runtime_mode_selection"])

    def test_keyboard_navigation_migrates_legacy_default_once(self):
        from domain.frontend.registry import FrontendRegistry

        fixture_path = Path(__file__).parent / "fixtures" / (
            "frontend_settings_keyboard_navigation_legacy.json"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = (
                pack_root / "user_data" / "shared" / "frontend_settings.json"
            )
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fixture_path, settings_path)
            registry = FrontendRegistry(pack_root=pack_root)

            first = registry.get_settings(lightweight=True)["values"]
            persisted_after_first_read = settings_path.read_text(encoding="utf-8")
            second = registry.get_settings(lightweight=True)["values"]
            persisted_after_second_read = settings_path.read_text(encoding="utf-8")

        self.assertTrue(first["general"]["keyboard_button_navigation"])
        self.assertEqual(first["general"]["settings_version"], 2)
        self.assertEqual(
            first["general"]["keyboard_button_navigation_source"],
            "legacy_default_migrated",
        )
        self.assertEqual(first["general"]["composer_placeholder"], "既存のプレースホルダー")
        self.assertEqual(first["general"]["language"], "en")
        self.assertEqual(first["general"]["legacy_custom_flag"], "keep-me")
        self.assertTrue(first["preview"]["auto_open"])
        self.assertEqual(first["preview"]["max_items"], 7)
        self.assertEqual(second, first)
        self.assertEqual(persisted_after_second_read, persisted_after_first_read)

    def test_settings_migration_is_atomic_and_preserves_permissions(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = (
                pack_root / "user_data" / "shared" / "frontend_settings.json"
            )
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "general": {
                            "keyboard_button_navigation": False,
                            "language": "en",
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings_path.chmod(0o640)

            values = FrontendRegistry(pack_root=pack_root).get_settings(
                lightweight=True
            )["values"]

            persisted = json.loads(settings_path.read_text(encoding="utf-8"))
            persisted_mode = settings_path.stat().st_mode & 0o777
            temporary_files = list(settings_path.parent.glob("*.tmp"))

        self.assertEqual(values["general"]["settings_version"], 2)
        self.assertEqual(persisted["general"]["settings_version"], 2)
        if os.name != "nt":
            self.assertEqual(persisted_mode, 0o640)
        self.assertEqual(temporary_files, [])

    def test_settings_atomic_replace_failure_keeps_original(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = (
                pack_root / "user_data" / "shared" / "frontend_settings.json"
            )
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            original = json.dumps(
                {
                    "general": {
                        "settings_version": 2,
                        "keyboard_button_navigation": True,
                    }
                }
            ).encode("utf-8")
            settings_path.write_bytes(original)
            registry = FrontendRegistry(pack_root=pack_root)

            with patch(
                "domain.frontend.registry.os.replace",
                side_effect=OSError("replace failed"),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    registry.update_settings({"general": {"language": "en"}})

            temporary_files = list(settings_path.parent.glob("*.tmp"))
            persisted_after_failure = settings_path.read_bytes()

        self.assertEqual(persisted_after_failure, original)
        self.assertEqual(temporary_files, [])

    def test_invalid_settings_are_backed_up_without_overwriting_original(self):
        from domain.frontend.registry import FrontendRegistry

        for original in (b"", b'{"general":'):
            with self.subTest(original=original):
                with tempfile.TemporaryDirectory() as tmpdir:
                    pack_root = Path(tmpdir)
                    settings_path = (
                        pack_root
                        / "user_data"
                        / "shared"
                        / "frontend_settings.json"
                    )
                    settings_path.parent.mkdir(parents=True, exist_ok=True)
                    settings_path.write_bytes(original)
                    settings_path.chmod(0o640)
                    registry = FrontendRegistry(pack_root=pack_root)

                    first = registry.get_settings(lightweight=True)["values"]
                    second = registry.get_settings(lightweight=True)["values"]
                    backups = list(
                        settings_path.parent.glob(
                            "frontend_settings.json.corrupt-*.bak"
                        )
                    )

                    self.assertEqual(second, first)
                    self.assertEqual(settings_path.read_bytes(), original)
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(backups[0].read_bytes(), original)
                    if os.name != "nt":
                        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o640)

    def test_keyboard_navigation_explicit_false_is_preserved_and_marked(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            registry = FrontendRegistry(pack_root=pack_root)
            migrated = registry.update_settings(
                {"general": {"keyboard_button_navigation": False}}
            )
            reloaded = registry.get_settings(lightweight=True)["values"]
            settings_mode = (
                pack_root
                / "user_data"
                / "shared"
                / "frontend_settings.json"
            ).stat().st_mode & 0o777

        self.assertFalse(migrated["general"]["keyboard_button_navigation"])
        self.assertEqual(
            migrated["general"]["keyboard_button_navigation_source"],
            "user",
        )
        self.assertFalse(reloaded["general"]["keyboard_button_navigation"])
        self.assertEqual(
            reloaded["general"]["keyboard_button_navigation_source"],
            "user",
        )
        if os.name != "nt":
            self.assertEqual(settings_mode, 0o600)

    def test_keyboard_navigation_future_version_false_is_not_migrated(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = (
                pack_root / "user_data" / "shared" / "frontend_settings.json"
            )
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "general": {
                            "settings_version": 99,
                            "keyboard_button_navigation": False,
                            "keyboard_button_navigation_source": "user",
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = FrontendRegistry(pack_root=pack_root)
            values = registry.get_settings(lightweight=True)["values"]
            updated = registry.update_settings(
                {
                    "general": {
                        "keyboard_button_navigation": False,
                        "language": "en",
                    }
                }
            )

        self.assertEqual(values["general"]["settings_version"], 99)
        self.assertFalse(values["general"]["keyboard_button_navigation"])
        self.assertEqual(
            values["general"]["keyboard_button_navigation_source"],
            "user",
        )
        self.assertEqual(updated["general"]["settings_version"], 99)
        self.assertFalse(updated["general"]["keyboard_button_navigation"])
        self.assertEqual(
            updated["general"]["keyboard_button_navigation_source"],
            "user",
        )

    def test_settings_api_keys_expose_google_browser_oauth_status(self):
        from domain.ai_client.oauth_store import (
            save_provider_oauth_client_config,
            save_provider_oauth_connection,
        )
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            secrets_dir = pack_root / "user_data" / "secrets"
            env = {"RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir)}
            with patch.dict(os.environ, env, clear=True):
                save_provider_oauth_client_config(
                    "google",
                    """
                    {
                      "installed": {
                        "client_id": "test-client.apps.googleusercontent.com",
                        "client_secret": "test-secret"
                      }
                    }
                    """,
                    pack_root=pack_root,
                )
                save_provider_oauth_connection(
                    "google",
                    {
                        "access_token": "oauth-access-token",
                        "refresh_token": "oauth-refresh-token",
                        "expires_in": 3600,
                        "scope": "openid email profile https://www.googleapis.com/auth/generative-language",
                    },
                    userinfo={"email": "user@example.test", "name": "OAuth User"},
                    pack_root=pack_root,
                )
                with patch("domain.frontend.registry.AIClient") as mock_client:
                    mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                    settings = FrontendRegistry(pack_root=pack_root).get_settings()

        api_rows = settings["values"]["apis"]["api_keys"]
        google = next(item for item in api_rows if item["provider_id"] == "google")
        self.assertIn("oauth", google)
        self.assertTrue(google["oauth"]["supported"])
        self.assertTrue(google["oauth"]["connected"])
        self.assertEqual(google["oauth"]["email"], "user@example.test")

    def test_external_settings_are_split_into_input_output_and_custom_sections(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            shutil.copytree(
                DEFAULTSPACK_ROOT / "external_io_templates", pack_root / "external_io_templates"
            )
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                settings = FrontendRegistry(pack_root=pack_root).get_settings()

        sections = {section["id"]: section for section in settings["sections"]}
        values = settings["values"]

        self.assertIn("external_input", sections)
        self.assertIn("external_output", sections)
        self.assertIn("external_custom", sections)
        self.assertNotIn("external_inputs", sections)
        input_fields = {field["id"]: field for field in sections["external_input"]["fields"]}
        output_fields = {field["id"]: field for field in sections["external_output"]["fields"]}
        self.assertEqual(input_fields["input_setup_guide"]["type"], "readonly")
        self.assertEqual(input_fields["input_provider"]["type"], "select")
        self.assertEqual(input_fields["input_template_id"]["type"], "select")
        self.assertEqual(input_fields["input_profile_id"]["type"], "select")
        self.assertEqual(input_fields["public_url_launcher"]["type"], "public_url")
        self.assertEqual(input_fields["provider_route_copy"]["type"], "readonly")
        self.assertEqual(output_fields["output_setup_guide"]["type"], "readonly")
        self.assertEqual(output_fields["output_provider"]["type"], "select")
        self.assertEqual(output_fields["output_template_id"]["type"], "select")
        self.assertEqual(output_fields["output_profile_id"]["type"], "select")
        self.assertEqual(output_fields["output_send_mode"]["type"], "select")
        self.assertEqual(output_fields["output_target_id"]["type"], "text")
        self.assertNotIn(
            "custom", {option["value"] for option in input_fields["input_provider"]["options"]}
        )
        self.assertNotIn(
            "custom.input",
            {option["value"] for option in input_fields["input_template_id"]["options"]},
        )
        self.assertNotIn("textarea", {field["type"] for field in input_fields.values()})
        self.assertNotIn("textarea", {field["type"] for field in output_fields.values()})
        self.assertEqual(sections["external_custom"]["fields"][-1]["type"], "textarea")
        self.assertTrue(values["external_input"]["include_source_context"])
        self.assertEqual(values["external_input"]["default_response_mode"], "same_response")
        self.assertEqual(values["external_input"]["input_provider"], "line")
        self.assertEqual(values["external_input"]["input_template_id"], "line.input.default")
        self.assertEqual(
            values["external_input"]["public_url_launcher"]["provider_id"],
            "cloudflare_quick_tunnel",
        )
        self.assertEqual(
            values["external_input"]["public_url_launcher"]["route_path"],
            "/api/integrations/line/webhook",
        )
        self.assertIn(
            "/api/integrations/line/webhook", values["external_input"]["provider_route_copy"]
        )
        self.assertEqual(values["external_output"]["output_send_mode"], "reply_to_origin")
        self.assertEqual(values["external_output"]["output_callback_token_id"], "main")
        self.assertIn("push fallback", values["external_output"]["output_setup_guide"])
        self.assertIn("line", values["external_input"]["input_template_summary"])
        self.assertIn("discord", values["external_output"]["output_template_summary"])
        self.assertIn("slack", values["external_output"]["output_template_summary"])
        self.assertIn("external_io_templates", values["external_custom"]["custom_template_path"])

        updated = FrontendRegistry(pack_root=DEFAULTSPACK_ROOT)._refresh_derived_settings(
            {
                **values,
                "external_input": {
                    **values["external_input"],
                    "input_provider": "slack",
                    "input_endpoint_id": "slack-main",
                },
                "external_output": {**values["external_output"], "output_provider": "discord"},
            }
        )

        self.assertEqual(updated["external_input"]["input_template_id"], "slack.input.default")
        self.assertEqual(
            updated["external_input"]["public_url_launcher"]["route_path"],
            "/api/integrations/slack/events",
        )
        self.assertEqual(
            updated["external_output"]["output_template_id"], "discord.output.bot_channel"
        )
        self.assertEqual(updated["external_output"]["output_profile_id"], "discord.bot_channel")

    def test_external_token_status_keeps_custom_provider_rows(self):
        from domain.external.token_store import external_token_status, set_external_token

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            set_external_token(
                "xiaomi-token-plan-sgp",
                "test-token",
                token_id="main",
                name="main",
                kind="token",
                pack_root=pack_root,
            )

            rows = external_token_status(pack_root=pack_root)

        provider_ids = {row["provider_id"] for row in rows}
        self.assertIn("line", provider_ids)
        self.assertIn("xiaomi-token-plan-sgp", provider_ids)
        custom = next(row for row in rows if row["provider_id"] == "xiaomi-token-plan-sgp")
        self.assertEqual(custom["tokens"][0]["provider_id"], "xiaomi-token-plan-sgp")
        self.assertEqual(custom["tokens"][0]["token_id"], "main")

    def test_update_settings_persists_sidebar_user_data(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                registry = FrontendRegistry(pack_root=pack_root)
                values = registry.update_settings(
                    {
                        "sidebar": {
                            "pinned_item_ids": ["browser_use"],
                            "starred_item_ids": ["web_search"],
                            "custom_tool_tags": {"browser_use": ["coding"]},
                        }
                    }
                )
                reloaded = registry.get_settings()["values"]

        self.assertEqual(values["sidebar"]["pinned_item_ids"], ["browser_use"])
        self.assertEqual(values["sidebar"]["starred_item_ids"], ["web_search"])
        self.assertEqual(values["sidebar"]["custom_tool_tags"], {"browser_use": ["coding"]})
        self.assertEqual(reloaded["sidebar"]["pinned_item_ids"], ["browser_use"])

    def test_external_input_sync_prefers_line_computer_use_endpoint_template(self):
        from domain.frontend.registry import FrontendRegistry
        from domain.webhook.endpoint_store import WebhookEndpointStore

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            shutil.copytree(
                DEFAULTSPACK_ROOT / "external_io_templates", pack_root / "external_io_templates"
            )
            endpoint_store = WebhookEndpointStore(
                pack_root / "user_data" / "shared" / "webhooks" / "endpoints.json"
            )
            endpoint_store.upsert(
                {
                    "id": "line-main",
                    "kind": "line",
                    "input_profile_id": "line.computer_use",
                    "response_profile_id": "line.default",
                    "conversation": {"strategy": "external_key", "model": "google/gemma-4-31b-it"},
                    "response": {"mode": "computer_use_line_biz"},
                    "enabled": True,
                }
            )
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                values = FrontendRegistry(pack_root=pack_root).get_settings()["values"]

        self.assertEqual(values["external_input"]["input_provider"], "line")
        self.assertEqual(values["external_input"]["input_template_id"], "line.input.computer_use")
        self.assertEqual(values["external_input"]["input_profile_id"], "line.computer_use")
        self.assertEqual(values["external_input"]["input_endpoint_id"], "line-main")

    def test_settings_migrates_default_target_without_losing_debug_values(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = pack_root / "user_data" / "shared" / "frontend_settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "debug": {
                            "ai_request_logging": True,
                            "default_target": "current_browser",
                            "custom_debug_flag": "keep-me",
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                values = FrontendRegistry(pack_root=pack_root).get_settings()["values"]

        self.assertTrue(values["debug"]["ai_request_logging"])
        self.assertEqual(values["debug"]["custom_debug_flag"], "keep-me")
        self.assertEqual(values["tools"]["default_target"], "current_browser")
        self.assertFalse(values["tools"]["keep_selected_tools_after_send"])

    def test_settings_migrates_legacy_tool_assist_all_to_safe_auto(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = pack_root / "user_data" / "shared" / "frontend_settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps({"tools": {"tool_assist_mode": "all"}}),
                encoding="utf-8",
            )

            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                values = FrontendRegistry(pack_root=pack_root).get_settings()["values"]

        self.assertEqual(values["tools"]["settings_version"], 3)
        self.assertEqual(values["tools"]["default_mode"], "auto")
        self.assertEqual(values["tools"]["selection_strategy"], "hybrid")
        self.assertEqual(values["tools"]["tool_assist_mode"], "auto")

    def test_settings_migrates_legacy_keep_selected_tools_after_send_true_to_false(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = pack_root / "user_data" / "shared" / "frontend_settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps({"tools": {"keep_selected_tools_after_send": True}}),
                encoding="utf-8",
            )

            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                values = FrontendRegistry(pack_root=pack_root).get_settings()["values"]

        self.assertEqual(values["tools"]["settings_version"], 3)
        self.assertFalse(values["tools"]["keep_selected_tools_after_send"])

    def test_settings_migrates_legacy_selector_model_without_rewriting_tools_key(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = pack_root / "user_data" / "shared" / "frontend_settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps({"tools": {"selector_model": "custom/tool-helper"}}),
                encoding="utf-8",
            )

            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                values = FrontendRegistry(pack_root=pack_root).get_settings()["values"]

        self.assertEqual(values["models"]["utility_models"]["tool_selector"], "custom/tool-helper")
        self.assertNotIn("selector_model", values["tools"])

    def test_settings_migrates_model_api_routes_from_apis_to_models(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = pack_root / "user_data" / "shared" / "frontend_settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "apis": {
                            "model_api_routes": "google/gemini-2.5-pro: google/main, google/backup"
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                values = FrontendRegistry(pack_root=pack_root).get_settings()["values"]

        self.assertEqual(
            values["models"]["model_api_routes"],
            "google/gemini-2.5-pro: google/main, google/backup\n",
        )
        self.assertNotIn("model_api_routes", values["apis"])

    def test_update_settings_does_not_store_openrouter_key_as_secret(self):
        from core_runtime.secrets_store import SecretsStore
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [
                    {"id": "openrouter/tencent/hy3:free"}
                ]
                registry = FrontendRegistry(pack_root=pack_root)
                values = registry.update_settings({"models": {"openrouter_api_key": "or-secret"}})
                reloaded = registry.get_settings()["values"]

            settings_path = pack_root / "user_data" / "shared" / "frontend_settings.json"
            settings_text = settings_path.read_text(encoding="utf-8")
            store = SecretsStore(str(pack_root / "user_data" / "secrets"))
            has_secret = store.has_secret("OPENROUTER_API_KEY")

        self.assertFalse(values["models"]["openrouter_api_key_configured"])
        self.assertEqual(values["models"]["openrouter_api_key"], "")
        self.assertEqual(reloaded["models"]["openrouter_api_key"], "")
        self.assertNotIn("or-secret", settings_text)
        self.assertFalse(has_secret)

    def test_update_settings_does_not_store_google_key_as_secret(self):
        from core_runtime.secrets_store import SecretsStore
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [
                    {"id": "google/gemini-2.5-flash"}
                ]
                registry = FrontendRegistry(pack_root=pack_root)
                values = registry.update_settings({"models": {"google_api_key": "google-secret"}})
                reloaded = registry.get_settings()["values"]

            settings_path = pack_root / "user_data" / "shared" / "frontend_settings.json"
            settings_text = settings_path.read_text(encoding="utf-8")
            store = SecretsStore(str(pack_root / "user_data" / "secrets"))
            has_secret = store.has_secret("GOOGLE_API_KEY")

        self.assertFalse(values["models"]["google_api_key_configured"])
        self.assertEqual(values["models"]["google_api_key"], "")
        self.assertEqual(reloaded["models"]["google_api_key"], "")
        self.assertNotIn("google-secret", settings_text)
        self.assertFalse(has_secret)

    def test_update_settings_external_tokens_are_not_secret_write_sink(self):
        from domain.external.token_store import read_external_token
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                registry = FrontendRegistry(pack_root=pack_root)
                for section_id in ("external_output", "external_inputs"):
                    values = registry.update_settings(
                        {
                            section_id: {
                                "external_tokens": {
                                    "action": "upsert",
                                    "provider_id": "line",
                                    "token_id": "main",
                                    "kind": "channel_secret",
                                    "value": f"attacker-secret-{section_id}",
                                }
                            }
                        }
                    )

            token_value = read_external_token("line", token_id="main", pack_root=pack_root)
            settings_text = (
                pack_root / "user_data" / "shared" / "frontend_settings.json"
            ).read_text(encoding="utf-8")

        self.assertIsInstance(values["external_output"]["external_tokens"], list)
        self.assertEqual(token_value, "")
        self.assertNotIn("attacker-secret", settings_text)

    def test_update_settings_api_keys_do_not_mirror_external_tokens(self):
        from domain.external.token_store import read_external_token
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                FrontendRegistry(pack_root=pack_root).update_settings(
                    {
                        "apis": {
                            "api_keys": {
                                "action": "upsert",
                                "provider_id": "line",
                                "name": "main",
                                "kind": "channel_secret",
                                "value": "line-provider-secret",
                            }
                        }
                    }
                )

            token_value = read_external_token("line", token_id="main", pack_root=pack_root)

        self.assertEqual(token_value, "")

    def test_openrouter_key_status_is_derived_from_secret_store(self):
        from core_runtime.secrets_store import SecretsStore
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = pack_root / "user_data" / "shared" / "frontend_settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "openrouter_api_key": "must-not-persist",
                            "openrouter_api_key_configured": False,
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = SecretsStore(str(pack_root / "user_data" / "secrets"))
            store.set_secret("OPENROUTER_API_KEY", "or-secret", actor="test")

            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [
                    {"id": "openrouter/tencent/hy3:free"}
                ]
                values = FrontendRegistry(pack_root=pack_root).get_settings()["values"]

        self.assertEqual(values["models"]["openrouter_api_key"], "")
        self.assertTrue(values["models"]["openrouter_api_key_configured"])

    def test_google_key_status_is_derived_from_secret_store(self):
        from core_runtime.secrets_store import SecretsStore
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            settings_path = pack_root / "user_data" / "shared" / "frontend_settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "google_api_key": "must-not-persist",
                            "google_api_key_configured": False,
                        }
                    }
                ),
                encoding="utf-8",
            )
            store = SecretsStore(str(pack_root / "user_data" / "secrets"))
            store.set_secret("GOOGLE_API_KEY", "google-secret", actor="test")

            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [
                    {"id": "google/gemini-2.5-flash"}
                ]
                values = FrontendRegistry(pack_root=pack_root).get_settings()["values"]

        self.assertEqual(values["models"]["google_api_key"], "")
        self.assertTrue(values["models"]["google_api_key_configured"])

    def test_fallback_http_routes_reject_unallowlisted_legacy_block_handlers(self):
        from transport.registry import HttpRouteSpec, build_http_routes_from_specs

        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            build_http_routes_from_specs(
                object(),
                [
                    HttpRouteSpec(
                        "POST",
                        "/api/ai/provider-key",
                        block_module="blocks.ai.provider_key",
                    )
                ],
            )

    def test_fallback_http_routes_include_tools_list(self):
        from transport.registry import build_fallback_http_routes

        class FakeServer:
            def __getattr__(self, name):
                if str(name).startswith("_handle_authority_"):
                    return lambda *_args, **_kwargs: {"status": "ok"}
                raise AttributeError(name)

            def _invoke_fallback_block(self, block_module, request_data, path_params, inject=None):
                return {"block_module": block_module, "request_data": request_data}

            def _handle_health(self, request_data, path_params):
                return {}

            def _handle_context_info(self, request_data, path_params):
                return {}

            def _handle_desktop_system_info(self, request_data, path_params):
                return {}

            def _handle_chat_redirect(self, request_data, path_params):
                return {}

            def _handle_static(self, request_data, path_params):
                return {}

            def _handle_static_file(self, request_data, path_params):
                return {}

        routes = build_fallback_http_routes(FakeServer())
        self.assertFalse(
            any(
                method == "GET" and pattern.match("/api/tools")
                for method, pattern, _handler, _source, _inject in routes
            )
        )
        self.assertTrue(
            any(
                method == "GET" and pattern.match("/api/health")
                for method, pattern, _handler, _source, _inject in routes
            )
        )

    def test_fallback_http_uses_block_when_function_bridge_rejects_unapproved_pack(self):
        _assert_v4_ui_boundary()

    def test_fallback_http_does_not_use_pack_not_approved_block_fallback_for_spoofed_post(self):
        _assert_v4_ui_boundary()

    def test_fallback_http_invokes_blocks_directly_without_facade(self):
        _assert_v4_ui_boundary()

    def test_prepare_chat_run_uses_catalog_default_when_model_is_empty(self):
        from domain.chat.run_request import prepare_chat_run
        from domain.chat.store import ChatStore

        store = ChatStore()
        conversation = store.create_conversation(model="")
        prepared = prepare_chat_run(
            {
                "conversation_id": conversation["id"],
                "message": {"role": "user", "content": "hello"},
            },
            {},
        )

        self.assertEqual(prepared.model, "rumi/rumi")

    def test_model_settings_are_editable_contracts(self):
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            with patch("domain.frontend.registry.AIClient") as mock_client:
                mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                registry = FrontendRegistry(pack_root=pack_root)
                settings = registry.get_settings()
                values = registry.update_settings(
                    {
                        "models": {
                            "preferred_model": "openrouter/tencent/hy3:free",
                        }
                    }
                )

        model_fields = {
            field["id"]: field
            for section in settings["sections"]
            if section["id"] == "models"
            for field in section["fields"]
        }
        self.assertEqual(model_fields["preferred_model"]["type"], "select")
        self.assertEqual(model_fields["main_model"]["type"], "model_select")
        self.assertEqual(model_fields["lightweight_model"]["type"], "model_select")
        self.assertFalse(model_fields["main_model"].get("advanced", False))
        self.assertFalse(model_fields["lightweight_model"].get("advanced", False))
        self.assertTrue(model_fields["preferred_model"]["advanced"])
        self.assertTrue(model_fields["utility_models"]["advanced"])
        self.assertGreaterEqual(len(model_fields["preferred_model"]["options"]), 1)
        model_option_values = {
            option["value"] for option in model_fields["preferred_model"]["options"]
        }
        self.assertIn("stub/default", model_option_values)
        self.assertNotIn("openrouter/openai/gpt-4o", model_option_values)
        self.assertEqual(len(model_option_values), len(model_fields["preferred_model"]["options"]))
        self.assertEqual(model_fields["thinking_level"]["type"], "select")
        self.assertIn(
            "xhigh",
            {option["value"] for option in model_fields["thinking_level"]["options"]},
        )
        self.assertNotIn("model_profile", model_fields)
        self.assertNotIn("detected_provider_count", model_fields)
        self.assertEqual(values["models"]["preferred_model"], "stub/default")

    def test_conversation_preview_uses_inspector_and_message_widgets(self):
        from domain.chat.store import ChatStore
        from domain.dev.inspector import Inspector
        from domain.frontend.registry import FrontendRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            chat_path = Path(tmpdir) / "chat" / "conversations.json"
            with patch.dict("os.environ", {"RUMI_DEFAULTSPACK_CHAT_STORE_PATH": str(chat_path)}):
                ChatStore._instance = None
                store = ChatStore()
                inspector = Inspector()
                inspector.clear()

                conversation = store.create_conversation()
                store.add_message(
                    conversation["id"],
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "code",
                                "filename": "demo.py",
                                "language": "python",
                                "text": "print('hi')",
                            }
                        ],
                        "widget": {"type": "indicator", "label": "Running"},
                        "tool_logs": [
                            {
                                "tool_name": "calculator",
                                "arguments": {"expression": "13829+12312"},
                                "result": {
                                    "summary": "26141",
                                    "visual_feedback": {
                                        "data_url": "data:image/png;base64,iVBORw0KGgo=",
                                        "model_image_path": "/tmp/result.png",
                                    },
                                },
                            },
                            {
                                "tool_name": "writer",
                                "arguments": {"path": "/tmp/report.md"},
                                "result": {"data": {"path": "/tmp/report.md"}},
                            },
                            {
                                "tool_name": "reader",
                                "arguments": {"path": "/tmp/readme.md"},
                                "result": {
                                    "data": {
                                        "widget": {
                                            "path": "/tmp/readme.md",
                                            "content": "# Readme",
                                        }
                                    }
                                },
                            },
                            {
                                "tool_name": "coding_file_create",
                                "arguments": {"path": "/tmp/pending.html"},
                                "result": {
                                    "status": "ok",
                                    "data": {
                                        "path": "/tmp/pending.html",
                                        "content": "<h1>Pending</h1>",
                                        "widget": {
                                            "type": "approval_request",
                                            "approval_required": True,
                                            "requires_approval": True,
                                        },
                                    },
                                },
                            },
                            {
                                "tool_name": "coding_file_create",
                                "arguments": {"path": "/tmp/failed.html"},
                                "result": {
                                    "status": "error",
                                    "data": {
                                        "path": "/tmp/failed.html",
                                        "content": "<h1>Failed</h1>",
                                    },
                                },
                            },
                        ],
                    },
                )
                inspector.log_request(
                    request_id="req-1",
                    conversation_id=conversation["id"],
                    tools_called=["web_search"],
                    context_info={
                        "knowledge_results": [
                            {"content": "Knowledge body", "metadata": {"title": "Knowledge"}}
                        ],
                        "memory_results": [{"content": "Memory body", "score": 0.8}],
                    },
                )

                with patch("domain.frontend.registry.AIClient") as mock_client:
                    mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
                    registry = FrontendRegistry(pack_root=Path(tmpdir))
                    preview = registry.build_conversation_preview(conversation["id"])
                ChatStore._instance = None

        ChatStore._instance = None

        preview_ids = {item["id"] for item in preview["previews"]}
        self.assertFalse(any(item.startswith("tool-web_search") for item in preview_ids))
        self.assertTrue(any(item.startswith("knowledge-") for item in preview_ids))
        self.assertTrue(any(item.startswith("memory-") for item in preview_ids))
        self.assertTrue(any(item.startswith("tool-log-artifact-") for item in preview_ids))
        self.assertTrue(any(item.startswith("tool-log-inline-") for item in preview_ids))
        self.assertFalse(
            any(
                (item.get("data") or {}).get("path") == "/tmp/report.md"
                for item in preview["previews"]
            )
        )
        self.assertFalse(
            any(
                (item.get("data") or {}).get("path") in {"/tmp/pending.html", "/tmp/failed.html"}
                for item in preview["previews"]
            )
        )
        content_preview = next(
            item
            for item in preview["previews"]
            if (item.get("data") or {}).get("path") == "/tmp/readme.md"
        )
        self.assertEqual(content_preview["data"]["content"], "# Readme")
        self.assertFalse(
            any(
                (item.get("data") or {}).get("filename", "").endswith(".tool")
                for item in preview["previews"]
            )
        )
        self.assertTrue(any(item.startswith("widget-") for item in preview_ids))
        self.assertTrue(any(item.startswith("code-") for item in preview_ids))

    def test_ui_routes_use_v4_qualified_operations_from_captured_host(self):
        from urllib.parse import quote

        from core_runtime.global_contracts.http_contract_dispatch import (
            HTTPContractBinding,
            HTTPContractTarget,
            resolve_contract_route,
        )

        _assert_v4_ui_boundary()

        route_operations = (
            ("GET", "/api/ui/catalog"),
            ("GET", "/api/ui/settings"),
            ("PUT", "/api/ui/settings"),
            ("GET", "/api/ui/commands"),
            ("POST", "/api/ui/commands/execute"),
            ("POST", "/api/ui/client-events"),
            ("GET", "/api/ui/conversations/{id}/preview"),
        )

        def binding(method: str, path: str) -> HTTPContractBinding:
            return HTTPContractBinding(
                method=method,
                path=path,
                presentation="defaultspack_ui",
                targets=(
                    HTTPContractTarget(
                        contribution_id="defaultspack.ui",
                        contract_id="defaultspack.ui.v4",
                        operation_id=path.removeprefix("/api/").replace("/", "."),
                        provider_id="defaultspack.desktop",
                        function_id="defaultspack.desktop",
                    ),
                ),
                application_id="defaultspack",
                route_namespace="defaultspack",
            )

        class CapturedHost:
            _contract_routes = {
                (method, path): binding(method, path)
                for method, path in route_operations
            }
        for method, route in route_operations:
            qualified_operation = (
                "/api/contracts/defaultspack/"
                + quote(f"{method} {route}", safe="")
            )
            resolved = resolve_contract_route(
                CapturedHost(),
                method,
                qualified_operation,
                namespace="defaultspack",
            )
            self.assertEqual(resolved.method, method)
            self.assertEqual(resolved.path, route)

    def test_slash_command_registry_lists_defaults_and_executes_thinking(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        registry = SlashCommandRegistry(DEFAULTSPACK_ROOT)
        commands = registry.list_commands()
        ids = {command["id"] for command in commands}

        self.assertIn("model", ids)
        self.assertIn("think", ids)
        self.assertIn("deepthink", ids)
        self.assertIn("compact", ids)
        self.assertIn("commit", ids)
        self.assertEqual(
            next(command for command in commands if command["id"] == "commit")["risk"], "high"
        )

        with patch(
            "domain.ai_client.model_runtime_settings.ModelRuntimeSettingsService"
        ) as service_cls:
            service = service_cls.return_value
            service.set_thinking_level.return_value = {"level": "high", "scope": "profile"}
            result = registry.execute(
                {
                    "command": "thinking",
                    "mode": "chat",
                    "args": {
                        "level": "high",
                        "scope": "profile",
                        "profile_id": "google/gemma-4-31b-it",
                    },
                    "conversation_id": "conv-1",
                },
                {},
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["data"]["executed"])
        service.set_thinking_level.assert_called_once_with(
            "high",
            "profile",
            "google/gemma-4-31b-it",
            "conv-1",
        )

    def test_slash_command_registry_executes_deepthink_toggle_with_warning(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        registry = SlashCommandRegistry(DEFAULTSPACK_ROOT)
        with patch(
            "domain.ai_client.model_runtime_settings.ModelRuntimeSettingsService"
        ) as service_cls:
            service = service_cls.return_value
            service.set_deepthink_enabled.return_value = {
                "enabled": True,
                "message": "DeepThinkをONにしました。タスクには数時間かかる可能性があります。",
            }
            result = registry.execute(
                {"command": "deepthink", "mode": "chat", "args": {"enabled": "on"}},
                {},
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["data"]["executed"])
        self.assertIn("数時間", result["data"]["message"])
        service.set_deepthink_enabled.assert_called_once_with(True)

    def test_slash_command_registry_returns_authoritative_deepthink_state(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        registry = SlashCommandRegistry(DEFAULTSPACK_ROOT)
        snapshot = {
            "state_ref": "defaultspack:models.deepthink_enabled",
            "value": True,
            "revision": 8,
            "freshness": "authoritative",
        }
        with patch(
            "domain.ai_client.model_runtime_settings.ModelRuntimeSettingsService"
        ) as service_cls:
            service = service_cls.return_value
            service.set_deepthink_enabled.return_value = {
                "enabled": True,
                "message": "DeepThinkをONにしました。",
                "state_snapshot": snapshot,
            }
            result = registry.execute(
                {
                    "command": "deepthink",
                    "mode": "chat",
                    "args": {"enabled": True},
                    "invocation_id": "deepthink-operation-8",
                    "idempotency_key": "deepthink-operation-8",
                    "client_sequence": 8,
                    "expected_revision": 7,
                },
                {},
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["operation_id"], "deepthink-operation-8")
        self.assertEqual(result["data"]["client_sequence"], 8)
        self.assertEqual(result["data"]["state_changes"], [snapshot])
        service.set_deepthink_enabled.assert_called_once_with(
            True,
            expected_revision=7,
            idempotency_key="deepthink-operation-8",
        )

    def test_slash_command_registry_model_command_opens_picker_without_query(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        with patch(
            "domain.ai_client.model_runtime_settings.ModelRuntimeSettingsService"
        ) as service_cls:
            result = SlashCommandRegistry(DEFAULTSPACK_ROOT).execute(
                {"command": "model", "mode": "chat", "args": {}},
                {},
            )

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["executed"])
        self.assertEqual(result["data"]["action"], "open_model_picker")
        self.assertEqual(result["data"]["candidates"], [])
        self.assertEqual(result["data"]["args"], {})
        service_cls.assert_not_called()

    def test_slash_command_registry_model_command_sets_exact_match(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        candidate = {
            "profile_id": "stub/default",
            "qualified_model_id": "stub/default",
            "provider_id": "stub",
            "model_id": "default",
            "display_name": "Stub Default",
            "configured": True,
            "local": True,
            "score": 1036,
            "label": "Stub / Stub Default",
        }
        with patch(
            "domain.ai_client.model_runtime_settings.ModelRuntimeSettingsService"
        ) as service_cls:
            service = service_cls.return_value
            service.resolve_model_candidates.return_value = {
                "query": "stub/default",
                "exact": candidate,
                "candidates": [candidate],
            }
            service.set_preferred_model.return_value = {
                "profile_id": "stub/default",
                "settings": {},
            }
            result = SlashCommandRegistry(DEFAULTSPACK_ROOT).execute(
                {"command": "model", "mode": "chat", "args": {"query": "stub/default"}},
                {},
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["data"]["executed"])
        self.assertEqual(result["data"]["selected_model"], candidate)
        self.assertEqual(result["data"]["result"]["profile_id"], "stub/default")
        service.resolve_model_candidates.assert_called_once_with("stub/default")
        service.set_preferred_model.assert_called_once_with("stub/default")

    def test_slash_command_registry_model_command_shows_ambiguous_candidates(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        candidates = [
            {"profile_id": "openai/gpt-4o", "display_name": "GPT-4o", "score": 950},
            {"profile_id": "openrouter/openai/gpt-4o", "display_name": "GPT-4o", "score": 950},
        ]
        with patch(
            "domain.ai_client.model_runtime_settings.ModelRuntimeSettingsService"
        ) as service_cls:
            service = service_cls.return_value
            service.resolve_model_candidates.return_value = {
                "query": "GPT-4o",
                "exact": None,
                "candidates": candidates,
            }
            result = SlashCommandRegistry(DEFAULTSPACK_ROOT).execute(
                {"command": "model", "mode": "chat", "args": {"query": "GPT-4o"}},
                {},
            )

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["executed"])
        self.assertEqual(result["data"]["action"], "show_model_candidates")
        self.assertEqual(result["data"]["candidates"], candidates)
        service.set_preferred_model.assert_not_called()

    def test_slash_command_registry_model_command_handles_unknown_query(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        with patch(
            "domain.ai_client.model_runtime_settings.ModelRuntimeSettingsService"
        ) as service_cls:
            service = service_cls.return_value
            service.resolve_model_candidates.return_value = {
                "query": "missing-model",
                "exact": None,
                "candidates": [],
            }
            result = SlashCommandRegistry(DEFAULTSPACK_ROOT).execute(
                {"command": "models", "mode": "chat", "args": {"query": "missing-model"}},
                {},
            )

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["executed"])
        self.assertEqual(result["data"]["action"], "show_model_candidates")
        self.assertEqual(result["data"]["candidates"], [])
        service.set_preferred_model.assert_not_called()

    def test_slash_command_registry_rejects_invalid_enum_args(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        result = SlashCommandRegistry(DEFAULTSPACK_ROOT).execute(
            {"command": "think", "mode": "chat", "args": {"level": "warp"}},
            {},
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(result["error"]["details"]["argument"], "level")

    def test_slash_command_registry_validates_required_args_and_boolean_false(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            commands_path = pack_root / "commands" / "default_commands.json"
            commands_path.parent.mkdir(parents=True, exist_ok=True)
            commands_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "flag",
                            "name": "flag",
                            "modes": ["chat"],
                            "args": [{"name": "enabled", "type": "boolean", "required": True}],
                            "execution": {"type": "frontend", "action": "toggle_flag"},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            registry = SlashCommandRegistry(pack_root)

            off_result = registry.execute(
                {"command": "flag", "mode": "chat", "args": {"enabled": "off"}}, {}
            )
            false_result = registry.execute(
                {"command": "flag", "mode": "chat", "args": {"enabled": False}}, {}
            )
            missing_result = registry.execute({"command": "flag", "mode": "chat", "args": {}}, {})

        self.assertEqual(off_result["status"], "ok")
        self.assertFalse(off_result["data"]["args"]["enabled"])
        self.assertEqual(false_result["status"], "ok")
        self.assertFalse(false_result["data"]["args"]["enabled"])
        self.assertEqual(missing_result["status"], "error")
        self.assertEqual(missing_result["error"]["code"], "MISSING_ARGUMENT")

    def test_slash_command_registry_rejects_user_manifest_rumi_function_and_non_allowlisted_default(
        self,
    ):
        from domain.frontend.command_registry import SlashCommandRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            defaults_path = pack_root / "commands" / "default_commands.json"
            user_path = pack_root / "user_data" / "shared" / "commands" / "user.json"
            defaults_path.parent.mkdir(parents=True, exist_ok=True)
            user_path.parent.mkdir(parents=True, exist_ok=True)
            defaults_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "baddefault",
                            "name": "baddefault",
                            "modes": ["chat"],
                            "execution": {
                                "type": "rumi_function",
                                "qualified_name": "defaultspack:not_allowlisted",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            user_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "userthink",
                            "name": "userthink",
                            "modes": ["chat"],
                            "execution": {
                                "type": "rumi_function",
                                "qualified_name": "defaultspack:ai_set_thinking_level",
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            registry = SlashCommandRegistry(pack_root)
            user_result = registry.execute(
                {"command": "userthink", "mode": "chat", "args": {"level": "low"}}, {}
            )
            default_result = registry.execute(
                {"command": "baddefault", "mode": "chat", "args": {}}, {}
            )

        self.assertEqual(user_result["status"], "error")
        self.assertEqual(user_result["error"]["code"], "INVALID_COMMAND")
        self.assertEqual(default_result["status"], "error")
        self.assertEqual(default_result["error"]["code"], "INVALID_COMMAND")

    def test_slash_command_registry_loads_builtin_template_slash_command(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            defaults_path = pack_root / "commands" / "default_commands.json"
            template_path = pack_root / "templates" / "context_txt" / "default" / "template.json"
            defaults_path.parent.mkdir(parents=True, exist_ok=True)
            template_path.parent.mkdir(parents=True, exist_ok=True)
            defaults_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "context_txt",
                            "name": "context_txt",
                            "label": "Old Context",
                            "modes": ["chat"],
                            "execution": {"type": "frontend", "action": "old_context"},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            template_path.write_text(
                json.dumps(
                    {
                        "id": "rumi.test.context_txt",
                        "kind": "frontend",
                        "version": "1.0.0",
                        "status": "active",
                        "pieces": [
                            {
                                "id": "context_txt_action",
                                "kind": "function",
                                "role": "action",
                                "action_id": "context_txt",
                                "override": {
                                    "mode": "replace",
                                    "target_public_id": "context_txt",
                                },
                                "slash_command": {
                                    "id": "context_txt",
                                    "name": "context_txt",
                                    "label": "Context TXT",
                                    "category": "chat",
                                    "modes": ["chat"],
                                    "execution": {
                                        "type": "pack_block",
                                        "qualified_name": "defaultspack:chat.materialize_context",
                                    },
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            registry = SlashCommandRegistry(pack_root)
            command = next(item for item in registry.list_commands() if item["id"] == "context_txt")
            duplicate = next(
                item
                for item in registry.manifest_errors()
                if item["code"] == "command_explicit_override"
            )

        self.assertEqual(command["label"], "Context TXT")
        self.assertEqual(command["execution"]["type"], "pack_block")
        self.assertIn("explicitly replaces", duplicate["message"])
        self.assertIn("template_id=rumi.test.context_txt", duplicate["source"])
        self.assertIn("piece_id=context_txt_action", duplicate["source"])

    def test_slash_command_registry_rejects_user_template_privileged_execution(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            template_path = (
                pack_root / "user_data" / "shared" / "templates" / "user_commands" / "template.json"
            )
            template_path.parent.mkdir(parents=True, exist_ok=True)
            template_path.write_text(
                json.dumps(
                    {
                        "id": "rumi.test.user_commands",
                        "kind": "frontend",
                        "version": "1.0.0",
                        "status": "active",
                        "pieces": [
                            {
                                "id": "user_pack_action",
                                "kind": "function",
                                "role": "action",
                                "action_id": "user_context_txt",
                                "slash_command": {
                                    "id": "user_context_txt",
                                    "name": "user_context_txt",
                                    "template_id": "rumi.composer.default",
                                    "piece_id": "context_txt_command",
                                    "source_path": "templates/composer/default/template.json",
                                    "trust_level": "builtin",
                                    "modes": ["chat"],
                                    "execution": {
                                        "type": "pack_block",
                                        "qualified_name": "defaultspack:chat.materialize_context",
                                    },
                                },
                            },
                            {
                                "id": "user_function_action",
                                "kind": "function",
                                "role": "action",
                                "action_id": "user_think",
                                "slash_command": {
                                    "id": "user_think",
                                    "name": "user_think",
                                    "modes": ["chat"],
                                    "execution": {
                                        "type": "rumi_function",
                                        "qualified_name": "defaultspack:ai_set_thinking_level",
                                    },
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            registry = SlashCommandRegistry(pack_root)
            ids = {command["id"] for command in registry.list_commands()}
            public_pack_command = next(
                command
                for command in registry.list_commands()
                if command["id"] == "user_context_txt"
            )
            internal_pack_command = registry.find_command("user_context_txt")
            pack_result = registry.execute({"command": "user_context_txt", "mode": "chat"}, {})
            function_result = registry.execute(
                {"command": "user_think", "mode": "chat", "args": {"level": "low"}},
                {},
            )

        self.assertIn("user_context_txt", ids)
        self.assertIn("user_think", ids)
        self.assertIsNotNone(internal_pack_command)
        self.assertEqual(internal_pack_command["_manifest_origin"], "user")
        self.assertEqual(internal_pack_command["_template_id"], "rumi.test.user_commands")
        self.assertEqual(internal_pack_command["_template_piece_id"], "user_pack_action")
        self.assertEqual(internal_pack_command["_template_trust_level"], "user")
        self.assertEqual(public_pack_command["template_id"], "rumi.test.user_commands")
        self.assertEqual(public_pack_command["piece_id"], "user_pack_action")
        self.assertEqual(public_pack_command["trust_level"], "user")
        self.assertTrue(
            public_pack_command["source_path"].endswith(
                "user_data/shared/templates/user_commands/template.json"
            )
        )
        self.assertEqual(pack_result["status"], "error")
        self.assertEqual(pack_result["error"]["code"], "INVALID_COMMAND")
        self.assertEqual(function_result["status"], "error")
        self.assertEqual(function_result["error"]["code"], "INVALID_COMMAND")

    def test_ui_commands_get_returns_duplicate_manifest_warnings(self):
        _assert_v4_ui_boundary()

    def test_slash_command_registry_blocks_high_risk_without_approval(self):
        from domain.frontend.command_registry import SlashCommandRegistry

        result = SlashCommandRegistry(DEFAULTSPACK_ROOT).execute(
            {"command": "commit", "mode": "coding", "args": {"message": "test"}},
            {},
        )

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["executed"])
        self.assertTrue(result["data"]["requires_approval"])

    def test_static_asset_serving_is_binary_and_assets_scoped(self):
        from transport.http import DefaultsHttpServer

        with tempfile.TemporaryDirectory() as tmpdir:
            pack_root = Path(tmpdir)
            (pack_root / "assets" / "icons").mkdir(parents=True)
            (pack_root / "ui").mkdir()
            (pack_root / "assets" / "icons" / "icon.png").write_bytes(b"\x89PNG\r\n")
            (pack_root / "ecosystem.json").write_text("{}", encoding="utf-8")

            server = DefaultsHttpServer(facade=None)
            original_file = __import__("transport.http", fromlist=["__file__"]).__file__
            try:
                import transport.http as http_module

                http_module.__file__ = str(pack_root / "transport" / "http.py")
                asset = server._handle_static_file({}, {"path": "assets/icons/icon.png"})
                hidden = server._handle_static_file({}, {"path": "ecosystem.json"})
            finally:
                http_module.__file__ = original_file

        self.assertEqual(asset["content_type"], "image/png")
        self.assertEqual(asset["body"], b"\x89PNG\r\n")
        self.assertEqual(hidden["status"], "error")

    def test_tool_manifest_ui_metadata_survives_tool_registry_normalization(self):
        from domain.tool.registry import ToolRegistry

        tool = ToolRegistry._tool_from_manifest(
            {
                "id": "oddly_named_manifest",
                "category": "tool",
                "description": "declared UI metadata",
                "config": {
                    "name": "oddly_named_tool",
                    "summary": "No legacy grouping keywords",
                    "ui": {
                        "group_id": "declared_group",
                        "group_label": "Declared Group",
                        "group_icon": "terminal",
                        "drop_capabilities": ["composer.toggle_chip"],
                        "widget_kind": "tool_toggle",
                    },
                },
            }
        )

        self.assertIsNotNone(tool)
        self.assertEqual(
            tool["ui"],
            {
                "group_id": "declared_group",
                "group_label": "Declared Group",
                "group_icon": "terminal",
                "drop_capabilities": ["composer.toggle_chip"],
                "widget_kind": "tool_toggle",
            },
        )

    def test_frontend_sidebar_items_include_tool_ui_declaration(self):
        import domain.frontend.registry as frontend_registry

        class FakeToolRegistry:
            def list_tools(self):
                return [
                    {
                        "tool_id": "oddly_named_tool",
                        "name": "Oddly Named",
                        "summary": "No legacy grouping keywords",
                        "tags": [],
                        "schema": {
                            "parameters": {"type": "object", "properties": {}, "required": []}
                        },
                        "execution": {"type": "local"},
                        "ui": {
                            "group_id": "declared_group",
                            "group_label": "Declared Group",
                            "group_icon": "terminal",
                            "drop_capabilities": ["composer.toggle_chip"],
                            "widget_kind": "tool_toggle",
                        },
                    }
                ]

        with patch("domain.frontend.registry.ToolRegistry", FakeToolRegistry):
            registry = frontend_registry.FrontendRegistry(DEFAULTSPACK_ROOT)
            items = registry._sidebar_items([], [])

        item = next(candidate for candidate in items if candidate["id"] == "oddly_named_tool")

        self.assertEqual(item["ui"]["group_id"], "declared_group")
        self.assertEqual(item["ui"]["group_label"], "Declared Group")
        self.assertEqual(item["ui"]["drop_capabilities"], ["composer.toggle_chip"])
        self.assertEqual(item["ui"]["widget_kind"], "tool_toggle")


if __name__ == "__main__":
    unittest.main()
