from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.frontend.registry import FrontendRegistry  # noqa: E402
from domain.templates import (  # noqa: E402
    ResolvedTemplate,
    build_template_catalog,
    parse_template,
    project_resolved_templates,
)
from domain.templates.projectors import empty_template_catalog  # noqa: E402


def _field_types(sections: list[dict]) -> set[str]:
    return {
        str(field.get("type"))
        for section in sections
        for field in section.get("fields", [])
        if isinstance(field, dict) and field.get("type")
    }


def test_template_projector_builds_stable_catalog_metadata():
    catalog = build_template_catalog(defaultspack_root=DEFAULTSPACK_ROOT)

    template_ids = {template["id"] for template in catalog["templates"]}
    assert "rumi.model_selector.default" in template_ids
    assert "rumi.api_keys.default" in template_ids
    assert "rumi.backend.model_routing.default" in template_ids
    assert "rumi.composer.default" in template_ids
    assert "rumi.external_io.default" in template_ids
    assert "rumi.backend.prompt_compaction.default" in template_ids
    # Prompt Workspace is owned by the optional Prompt Studio pack. The
    # defaultspack catalog must not synthesize that surface when its owner is
    # not selected.
    assert "rumi.prompt_workspace.default" not in template_ids
    assert "rumi.ambient_trigger.default" in template_ids

    assert _field_types(catalog["settings_sections"]) >= {
        "model_select",
        "provider_select",
        "api_key_setup",
    }
    assert any(
        "model_select" in renderer.get("field_types", []) for renderer in catalog["field_renderers"]
    )
    assert any(
        "model_select" in binding.get("field_types", [])
        for binding in catalog["component_bindings"]
    )
    assert any(item.get("action_id") == "ai_set_preferred_model" for item in catalog["actions"])
    assert any(item.get("data_source") == "provider_key_status" for item in catalog["data_sources"])
    assert any(item.get("service_id") == "model_router" for item in catalog["backend_services"])
    assert any(
        item.get("service_id") == "external_io_template_registry"
        for item in catalog["backend_services"]
    )
    assert any(item.get("service_id") == "prompt_compactor" for item in catalog["backend_services"])
    assert any(
        item.get("action_id") == "compact_prompt"
        and item.get("route_path") == "/api/prompts/compact"
        for item in catalog["actions"]
    )
    assert any(
        item.get("service_id") == "ambient_trigger_router"
        for item in catalog["backend_services"]
    )
    assert any(
        item.get("route_metadata", {}).get("path") == "/api/ai/models/route"
        for item in catalog["test_contracts"]
    )
    assert any(item.get("permission_id") == "api_key.use" for item in catalog["permissions"])
    assert any(
        item.get("permission_id") == "external_io.configure" for item in catalog["permissions"]
    )
    assert any(item.get("permission_id") == "context.compact" for item in catalog["permissions"])
    assert any(
        item.get("permission_id") == "ambient.trigger.dispatch"
        for item in catalog["permissions"]
    )
    assert not catalog["template_diagnostics"]

    projected_ids = {
        item.get("projected_id")
        for key in ("actions", "data_sources", "backend_services", "permissions", "test_contracts")
        for item in catalog[key]
    }
    assert "rumi.backend.model_routing.default:model_router_service" in projected_ids

    model_section = next(
        section for section in catalog["settings_sections"] if section["id"] == "models"
    )
    model_field = next(
        field for field in model_section["fields"] if field["id"] == "preferred_model"
    )
    assert model_field["piece_id"] == "model_select"
    assert model_field["template_id"] == "rumi.model_selector.default"
    assert model_field["projected_id"] == "rumi.model_selector.default:model_select"


def test_model_selector_schema_is_projected_to_every_model_and_provider_field():
    catalog = build_template_catalog(defaultspack_root=DEFAULTSPACK_ROOT)
    selector_fields = [
        field
        for section in catalog["settings_sections"]
        for field in section.get("fields", [])
        if field.get("type") in {"model_select", "provider_select", "model_api_routes"}
    ]

    assert selector_fields
    selector_schema = selector_fields[0]["selector_schema"]
    assert selector_schema["version"] == 1
    assert selector_schema["layout"]["provider_confirm_key"] == "Tab"
    assert selector_schema["layout"]["show_search"] is True
    assert selector_schema["layout"]["trigger_height_px"] == 44
    assert selector_schema["filters"]["exclude_model_ids"] == []
    assert all(field["selector_schema"] == selector_schema for field in selector_fields)


def test_template_catalog_projects_composer_surface_pieces():
    catalog = build_template_catalog(defaultspack_root=DEFAULTSPACK_ROOT)

    for key in (
        "commands",
        "composer_inputs",
        "shell_regions",
        "shell_renderers",
        "context_policies",
    ):
        assert key in catalog

    command = next(item for item in catalog["commands"] if item.get("id") == "context_txt")
    composer_input = next(
        item for item in catalog["composer_inputs"] if item.get("id") == "default_composer"
    )
    shell_region = next(item for item in catalog["shell_regions"] if item.get("id") == "composer")
    shell_renderer = next(
        item for item in catalog["shell_renderers"] if item.get("id") == "composer"
    )
    context_policy = next(
        item for item in catalog["context_policies"] if item.get("id") == "materialize_txt"
    )
    general_section = next(
        section for section in catalog["settings_sections"] if section["id"] == "general"
    )
    manual_mode_field = next(
        field
        for field in general_section["fields"]
        if field.get("id") == "manual_runtime_mode_selection"
    )

    assert command["execution"] == {
        "type": "pack_block",
        "qualified_name": "defaultspack:chat.materialize_context",
        "mode": "materialize_txt",
    }
    assert composer_input["region_id"] == "composer"
    assert composer_input["renderer"] == "composer"
    assert composer_input["layout"] == {
        "home": {"position": "center"},
        "conversation": {"position": "bottom"},
    }
    assert shell_region["renderer"] == "composer"
    assert shell_renderer["component"] == "Composer"
    assert shell_renderer["regions"] == ["composer"]
    assert context_policy["mode"] == "materialize_txt"
    assert manual_mode_field["default"] is False
    assert manual_mode_field["advanced"] is True
    assert manual_mode_field["control_center_section"] == "advanced"

    projected = [
        command,
        composer_input,
        shell_region,
        shell_renderer,
        context_policy,
        manual_mode_field,
    ]
    assert {item["template_id"] for item in projected} == {"rumi.composer.default"}
    assert {item["trust_level"] for item in projected} == {"builtin"}
    assert all(item["projected_id"].startswith("rumi.composer.default:") for item in projected)
    assert all(item["origin"]["template_id"] == "rumi.composer.default" for item in projected)
    assert all(
        item["_source"].endswith("templates/composer/default/template.json") for item in projected
    )


def test_empty_template_catalog_exposes_ai_input_and_tool_policy_buckets():
    catalog = empty_template_catalog()
    other = empty_template_catalog()

    assert "ai_inputs" in catalog
    assert "tool_policies" in catalog
    assert "external_io_templates" in catalog
    catalog["ai_inputs"].append({"id": "mutated"})
    assert other["ai_inputs"] == []
    assert other["external_io_templates"] == []


def test_template_catalog_projects_external_io_and_prompt_compaction_bundles():
    catalog = build_template_catalog(defaultspack_root=DEFAULTSPACK_ROOT)

    external_template = next(
        item for item in catalog["external_io_templates"] if item["id"] == "line.input.default"
    )
    ambient_template = next(
        item
        for item in catalog["external_io_templates"]
        if item["id"] == "ambient.input.webhook"
    )
    compact_command = next(
        item for item in catalog["commands"] if item.get("id") == "compact_context"
    )
    compact_policy = next(
        item for item in catalog["context_policies"] if item.get("id") == "long_context_txt"
    )
    token_source = next(
        item
        for item in catalog["data_sources"]
        if item.get("data_source") == "context_token_estimate"
    )
    compact_action = next(
        item for item in catalog["actions"] if item.get("action_id") == "compact_context"
    )

    assert external_template["origin"] == "template"
    assert external_template["template_id"] == "rumi.external_io.default"
    assert external_template["provider"] == "line"
    assert external_template["endpoint"]["route"] == "/api/integrations/line/webhook"
    assert (
        external_template["projected_id"] == "rumi.external_io.default:line_input_default_template"
    )
    assert ambient_template["template_id"] == "rumi.ambient_trigger.default"
    assert ambient_template["provider"] == "web"
    assert ambient_template["endpoint"]["route"] == "/api/ambient/events"
    assert compact_command["execution"]["qualified_name"] == "defaultspack:context.compact"
    assert compact_policy["format"] == "text/plain"
    assert token_source["route_path"] == "/api/context/token-estimate"
    assert compact_action["route_path"] == "/api/context/compact"

def test_template_catalog_does_not_synthesize_unselected_prompt_workspace_bundle():
    catalog = build_template_catalog(defaultspack_root=DEFAULTSPACK_ROOT)

    assert "rumi.prompt_workspace.default" not in {
        template["id"] for template in catalog["templates"]
    }
    assert not any(
        item.get("part_id") in {"prompt_studio", "prompt_command_center"}
        for item in catalog["component_bindings"]
    )
    assert not any(
        item.get("id") == "prompt_workspace_sidebar"
        for item in catalog["sidebar_items"]
    )
    assert not any(
        item.get("data_source") in {"prompt_editor_load", "prompt_workspace_index"}
        for item in catalog["data_sources"]
    )
    assert not any(
        item.get("action_id") == "prompt_editor_save"
        for item in catalog["actions"]
    )


def test_template_catalog_projects_ambient_ai_input_and_tool_policy():
    catalog = build_template_catalog(defaultspack_root=DEFAULTSPACK_ROOT)

    ai_input = next(item for item in catalog["ai_inputs"] if item["id"] == "ambient_finger_recording")
    tool_policy = next(
        item for item in catalog["tool_policies"] if item["id"] == "ambient_finger_recording_tools"
    )
    context_policy = next(
        item for item in catalog["context_policies"] if item["id"] == "ambient_audio_transcript"
    )

    assert ai_input["template_id"] == "rumi.ambient_trigger.default"
    assert ai_input["context_policy"] == "ambient_audio_transcript"
    assert ai_input["tool_policy"] == "ambient_finger_recording_tools"
    assert tool_policy["toggleable"] is True
    assert tool_policy["params"]["audio_source"] == "finger_recording"
    assert context_policy["mode"] == "ambient_audio_transcript"


def test_template_catalog_projects_ai_input_and_tool_policy_metadata():
    parsed = parse_template(
        {
            "id": "template.ai.input",
            "kind": "frontend",
            "version": "1.0.0",
            "status": "active",
            "pieces": [
                {
                    "id": "chat_composer_piece",
                    "kind": "composer_input",
                    "input": {
                        "id": "chat_composer",
                        "region_id": "composer",
                        "renderer": "composer",
                    },
                },
                {
                    "id": "chat_context_piece",
                    "kind": "context_policy",
                    "policy": {"id": "chat_context", "mode": "materialize_txt"},
                },
                {
                    "id": "chat_tools_piece",
                    "kind": "tool_policy",
                    "policy": {
                        "id": "chat_tools",
                        "toggleable": True,
                        "default_enabled_tools": ["web_search"],
                        "default_disabled_tools": ["terminal_exec"],
                        "tool_choice": "auto",
                        "parallel_tool_calls": True,
                        "params": {"max_tool_count": 4},
                    },
                },
                {
                    "id": "chat_ai_input_piece",
                    "kind": "ai_input",
                    "input": {
                        "id": "chat_ai_input",
                        "composer_input": "chat_composer",
                        "context_policy": "chat_context",
                        "tool_policy": "chat_tools",
                        "params": {"thinking_level": "medium"},
                    },
                },
            ],
        }
    )
    assert parsed.ok
    assert parsed.template is not None

    catalog = project_resolved_templates([ResolvedTemplate(template=parsed.template)])

    ai_input = next(item for item in catalog["ai_inputs"] if item["id"] == "chat_ai_input")
    tool_policy = next(item for item in catalog["tool_policies"] if item["id"] == "chat_tools")
    assert ai_input["composer_input"] == "chat_composer"
    assert ai_input["context_policy"] == "chat_context"
    assert ai_input["tool_policy"] == "chat_tools"
    assert ai_input["projected_id"] == "template.ai.input:chat_ai_input_piece"
    assert tool_policy["default_enabled_tools"] == ["web_search"]
    assert tool_policy["default_disabled_tools"] == ["terminal_exec"]
    assert tool_policy["parallel_tool_calls"] is True
    assert tool_policy["origin"]["template_id"] == "template.ai.input"
    assert not catalog["template_diagnostics"]


def test_only_active_templates_project_runtime_buckets_while_inactive_summaries_remain():
    resolved = []
    for status in ("active", "draft", "deprecated", "disabled"):
        template_id = f"template.status.{status}"
        parsed = parse_template(
            {
                "id": template_id,
                "kind": "pack",
                "version": "1.0.0",
                "status": status,
                "pieces": [
                    {
                        "id": "field",
                        "kind": "settings_field",
                        "section_id": "models",
                        "type": "text",
                    },
                    {
                        "id": "command",
                        "kind": "composer_command",
                        "command": {
                            "id": f"{status}_command",
                            "execution": {"type": "frontend", "action": "noop"},
                        },
                    },
                    {
                        "id": "action",
                        "kind": "function",
                        "role": "action",
                        "action_id": f"{status}_action",
                    },
                    {"id": "service", "kind": "backend_service", "service_id": f"{status}_service"},
                    {
                        "id": "route",
                        "kind": "api_route",
                        "method": "GET",
                        "route_path": f"/api/{status}",
                    },
                    {"id": "permission", "kind": "permission", "permission_id": f"{status}.use"},
                ],
            },
            trust_level="builtin",
        )
        assert parsed.template is not None
        resolved.append(ResolvedTemplate(template=parsed.template, diagnostics=parsed.diagnostics))

    catalog = project_resolved_templates(resolved)

    summaries = {template["id"]: template for template in catalog["templates"]}
    assert set(summaries) == {
        "template.status.active",
        "template.status.draft",
        "template.status.deprecated",
        "template.status.disabled",
    }
    assert summaries["template.status.active"]["projectable"] is True
    assert summaries["template.status.draft"]["projectable"] is False
    assert summaries["template.status.deprecated"]["projectable"] is False
    assert summaries["template.status.disabled"]["projectable"] is False

    runtime_projected_ids = {
        item.get("projected_id")
        for key in ("commands", "actions", "backend_services", "api_routes", "permissions")
        for item in catalog[key]
    }
    model_section = next(
        section for section in catalog["settings_sections"] if section["id"] == "models"
    )
    runtime_projected_ids.update(field.get("projected_id") for field in model_section["fields"])

    assert "template.status.active:command" in runtime_projected_ids
    assert "template.status.active:route" in runtime_projected_ids
    assert "template.status.active:field" in runtime_projected_ids
    assert not any(
        str(item or "").startswith("template.status.draft:") for item in runtime_projected_ids
    )
    assert not any(
        str(item or "").startswith("template.status.deprecated:") for item in runtime_projected_ids
    )
    assert not any(
        str(item or "").startswith("template.status.disabled:") for item in runtime_projected_ids
    )


def test_projector_overwrites_piece_level_trust_spoofing():
    parsed = parse_template(
        {
            "id": "template.user.spoofed_piece",
            "kind": "pack",
            "version": "1.0.0",
            "status": "active",
            "pieces": [
                {
                    "id": "spoofed_action",
                    "kind": "function",
                    "role": "action",
                    "action_id": "spoofed_action",
                    "trust_level": "builtin",
                    "template_id": "rumi.composer.default",
                    "projected_id": "rumi.composer.default:spoofed_action",
                }
            ],
        },
        trust_level="user",
    )
    assert parsed.template is not None

    catalog = project_resolved_templates([ResolvedTemplate(template=parsed.template)])
    action = next(item for item in catalog["actions"] if item.get("id") == "spoofed_action")

    assert action["trust_level"] == "user"
    assert action["template_id"] == "template.user.spoofed_piece"
    assert action["projected_id"] == "template.user.spoofed_piece:spoofed_action"


def test_resolved_template_id_collision_is_error_and_not_projected():
    resolved = []
    for command_id, template_id in (
        ("one", "template.duplicate.resolved"),
        ("two", " template.duplicate.resolved "),
    ):
        parsed = parse_template(
            {
                "id": template_id,
                "kind": "pack",
                "version": "1.0.0",
                "status": "active",
                "pieces": [
                    {
                        "id": command_id,
                        "kind": "composer_command",
                        "command": {
                            "id": command_id,
                            "execution": {"type": "frontend", "action": "noop"},
                        },
                    }
                ],
            },
            trust_level="builtin",
        )
        assert parsed.template is not None
        resolved.append(ResolvedTemplate(template=parsed.template, diagnostics=parsed.diagnostics))

    catalog = project_resolved_templates(resolved)

    assert any(
        diagnostic["code"] == "template.registry.resolved_duplicate_template"
        for diagnostic in catalog["template_diagnostics"]
    )
    assert any(
        diagnostic["code"] == "template.invalid_id"
        for diagnostic in catalog["template_diagnostics"]
    )
    assert catalog["commands"] == []


def test_patched_noncanonical_template_id_does_not_project_runtime_buckets(tmp_path):
    template_path = tmp_path / "user_data" / "shared" / "templates" / "patched" / "template.json"
    template_path.parent.mkdir(parents=True)
    template_path.write_text(
        json.dumps(
            {
                "id": " user.template ",
                "kind": "frontend",
                "version": "1.0.0",
                "status": "active",
                "metadata": {"declared_id": " user.template "},
                "pieces": [
                    {
                        "id": "command",
                        "kind": "composer_command",
                        "command": {
                            "id": "user_command",
                            "execution": {"type": "frontend", "action": "noop"},
                        },
                    },
                    {
                        "id": "action",
                        "kind": "function",
                        "role": "action",
                        "action_id": "user_action",
                    },
                    {
                        "id": "route",
                        "kind": "api_route",
                        "method": "GET",
                        "route_path": "/api/user-template",
                    },
                ],
                "patches": [{"op": "replace", "path": "/metadata", "value": {}}],
            }
        ),
        encoding="utf-8",
    )

    catalog = build_template_catalog(defaultspack_root=tmp_path)

    assert any(
        diagnostic["code"] == "template.invalid_id"
        for diagnostic in catalog["template_diagnostics"]
    )
    assert catalog["commands"] == []
    assert catalog["actions"] == []
    assert catalog["api_routes"] == []


def test_frontend_catalog_merges_template_metadata_without_dropping_existing_keys():
    with patch("domain.frontend.registry.AIClient") as mock_client:
        mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
        registry = FrontendRegistry(pack_root=DEFAULTSPACK_ROOT)
        catalog = registry.build_catalog()
        settings = registry.get_settings()

    for key in (
        "app",
        "agent_service",
        "shell",
        "parts",
        "component_bindings",
        "sidebar",
        "settings",
        "chat_rendering",
        "skills",
        "routes",
        "extension_points",
        "diagnostics",
    ):
        assert key in catalog

    assert "templates" in catalog
    assert "field_renderers" in catalog
    assert "data_sources" in catalog
    assert "actions" in catalog
    assert "backend_services" in catalog
    assert "api_routes" in catalog
    assert "permissions" in catalog
    assert "template_diagnostics" in catalog
    assert "commands" in catalog
    assert "composer_inputs" in catalog
    assert "context_policies" in catalog
    assert "external_io_templates" in catalog
    assert "shell_regions" in catalog
    assert "shell_renderers" in catalog

    assert any(template["id"] == "rumi.model_selector.default" for template in catalog["templates"])
    composer_input = next(
        item for item in catalog["composer_inputs"] if item["id"] == "default_composer"
    )
    assert composer_input["template_id"] == "rumi.composer.default"
    assert composer_input["region_id"] == "composer"
    assert any(item["id"] == "context_txt" for item in catalog["commands"])
    assert any(item["id"] == "compact_context" for item in catalog["commands"])
    assert any(item["id"] == "materialize_txt" for item in catalog["context_policies"])
    assert any(item["id"] == "line.input.default" for item in catalog["external_io_templates"])
    assert any(
        item["path"] == "/api/context/token-estimate"
        and item["function_name"] == "defaultspack:context_token_estimate"
        for item in catalog["routes"]["template_backed"]
    )
    composer_region = next(
        item for item in catalog["shell"]["layout"]["regions"] if item["id"] == "composer"
    )
    composer_renderer = next(
        item for item in catalog["shell"]["renderers"] if item["id"] == "composer"
    )
    assert composer_region["template_id"] == "rumi.composer.default"
    assert composer_renderer["template_id"] == "rumi.composer.default"
    assert "model_select" in _field_types(catalog["settings"]["sections"])
    assert "api_key_setup" in _field_types(settings["sections"])
    assert any(
        "model_select" in renderer.get("field_types", []) for renderer in catalog["field_renderers"]
    )


def test_projected_settings_fields_namespace_piece_ids_and_diagnose_setting_key_collisions():
    parsed_one = parse_template(
        {
            "id": "template.one",
            "kind": "pack",
            "version": "1.0.0",
            "status": "active",
            "pieces": [
                {
                    "id": "shared",
                    "kind": "settings_field",
                    "section_id": "models",
                    "type": "text",
                }
            ],
        }
    )
    parsed_two = parse_template(
        {
            "id": "template.two",
            "kind": "pack",
            "version": "1.0.0",
            "status": "active",
            "pieces": [
                {
                    "id": "shared",
                    "kind": "settings_field",
                    "section_id": "models",
                    "type": "text",
                }
            ],
        }
    )
    assert parsed_one.template is not None
    assert parsed_two.template is not None

    catalog = project_resolved_templates(
        [
            ResolvedTemplate(template=parsed_one.template),
            ResolvedTemplate(template=parsed_two.template),
        ]
    )

    models = next(section for section in catalog["settings_sections"] if section["id"] == "models")
    assert models["fields"] == []
    assert any(
        diagnostic["code"] == "template.catalog.settings_field_id_collision"
        for diagnostic in catalog["template_diagnostics"]
    )


def test_malformed_template_does_not_break_frontend_catalog(tmp_path):
    bad_template = tmp_path / "templates" / "broken" / "template.json"
    bad_template.parent.mkdir(parents=True)
    bad_template.write_text("{ broken", encoding="utf-8")

    with patch("domain.frontend.registry.AIClient") as mock_client:
        mock_client.return_value.list_models.return_value = [{"id": "stub/default"}]
        catalog = FrontendRegistry(pack_root=tmp_path).build_catalog()

    assert "app" in catalog
    assert "settings" in catalog
    assert catalog["templates"] == []
    assert any(
        item["code"] == "template.discovery.json_parse_error" for item in catalog["diagnostics"]
    )
    assert any(
        item["code"] == "template.discovery.json_parse_error"
        for item in catalog["template_diagnostics"]
    )
