from __future__ import annotations

import base64
import importlib
import json
import re
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from domain.ai_client.client import AIClient
from domain.frontend_settings_catalog import SettingsCatalogInputs, SettingsSections
from domain.frontend_builtin_catalog import builtin_ui_catalog
from domain.ai_client.api_key_store import provider_key_status
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
from domain.ai_client.oauth_store import provider_oauth_statuses
from domain.capability.catalog import CapabilityCatalog
from domain.chat.store import ChatStore
from domain.codex.app_server import codex_app_server_status
from domain.codex.connection_store import codex_connection_status
from domain.components.registry import DomainComponentRegistry, build_domain_component_roots
from domain.dev.inspector import Inspector
from domain.extensions.activation import selected_extension_pack_ids
from domain.extensions.runtime import get_extension_registry
from domain.external.input_profile_registry import InputProfileRegistry
from domain.external.io_templates import external_io_template_catalog
from domain.external.output_profile_registry import OutputProfileRegistry
from domain.external.source_store import ExternalSourceStore, external_source_key
from domain.external.token_store import external_token_status
from domain.frontend_settings_store import (
    FrontendSettingsCorruptError,
    FrontendSettingsStore,
    MUTATION_RECEIPTS_KEY,
    STATE_REVISIONS_KEY,
    defaultspack_frontend_settings_path,
)
from domain.tool.catalog_contract_client import ContractToolCatalog as ToolRegistry
from domain.webhook.endpoint_store import WebhookEndpointStore
from transport.registry import (
    component_http_route_specs,
    component_route_diagnostics,
    template_http_route_specs,
    template_route_diagnostics,
)


_GENERAL_SETTINGS_VERSION = 2
_KEYBOARD_NAVIGATION_SOURCE_DEFAULT = "default"
_KEYBOARD_NAVIGATION_SOURCE_LEGACY_MIGRATION = "legacy_default_migrated"
_KEYBOARD_NAVIGATION_SOURCE_USER = "user"


def _validated_dict(value: object) -> dict[str, object]:
    """Return a dictionary value after validating its runtime container type."""
    if isinstance(value, dict):
        return dict(value)
    return {}


def _validated_dict_list(value: object) -> list[dict[str, object]]:
    """Return only dictionary entries from a runtime list value."""
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


class FrontendRegistry:
    """Registry for frontend catalog, settings, and chat preview metadata."""

    _selectable_model_profiles_lock = threading.Lock()
    _selectable_model_profiles_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
    _selectable_model_profiles_cache_ttl_seconds = 30.0
    _load_diagnostics: list[dict[str, str]]

    def __init__(self, pack_root: Path | None = None) -> None:
        self._pack_root = pack_root or Path(__file__).resolve().parents[2]
        settings_owner = pack_root if pack_root is not None else None
        self._settings_path = defaultspack_frontend_settings_path(settings_owner)
        self._settings_store = FrontendSettingsStore(self._settings_path)

    def build_catalog(
        self,
        profile_id: str | None = None,
        *,
        lightweight: bool = False,
        include_skills: bool = False,
    ) -> dict[str, Any]:
        self._load_diagnostics = []
        template_catalog = self._template_catalog_metadata()
        extensions = self._load_extensions()
        ui_surfaces = self._load_ui_surfaces()
        selected_frontend_ids = self._profile_frontend_selection(profile_id)
        shell = self._merge_template_shell_catalog(
            self._shell(ui_surfaces, extensions),
            template_catalog,
        )
        shell = self._filter_shell(shell, selected_frontend_ids)
        parts = self._filter_frontend_items(self._parts(ui_surfaces, extensions), selected_frontend_ids)
        component_bindings = [
            *self._component_bindings(ui_surfaces, extensions),
            *template_catalog.get("component_bindings", []),
        ]
        component_bindings = self._filter_frontend_items(component_bindings, selected_frontend_ids)
        sidebar_items = [
            *self._sidebar_items(ui_surfaces, extensions, lightweight=lightweight),
            *template_catalog.get("sidebar_items", []),
        ]
        sidebar_items = self._filter_frontend_items(sidebar_items, selected_frontend_ids)
        settings_sections = self._merge_settings_sections(
            self._settings_sections(
                ui_surfaces,
                extensions,
                template_catalog=template_catalog,
                lightweight=lightweight,
            ),
            template_catalog.get("settings_sections", []),
            hydrate_dynamic=not lightweight,
        )
        settings_sections = self._filter_frontend_items(settings_sections, selected_frontend_ids)
        chat_renderers = [
            *self._chat_renderers(ui_surfaces, extensions),
            *template_catalog.get("chat_renderers", []),
        ]
        chat_renderers = self._filter_frontend_items(chat_renderers, selected_frontend_ids)
        return {
            "dynamic_host": self._dynamic_frontend_catalog(),
            "app": self._app_metadata(ui_surfaces),
            "agent_service": CapabilityCatalog(self._pack_root).manifest(),
            "shell": shell,
            "parts": parts,
            "component_bindings": component_bindings,
            "sidebar": {
                "filters": self._sidebar_filters(),
                "items": sidebar_items,
            },
            "settings": {
                "sections": settings_sections,
                "values": self._read_settings(),
            },
            "chat_rendering": {
                "renderers": chat_renderers,
            },
            "skills": self._skill_items() if include_skills or not lightweight else [],
            "routes": self._route_metadata(),
            "templates": template_catalog.get("templates", []),
            "field_renderers": template_catalog.get("field_renderers", []),
            "data_sources": template_catalog.get("data_sources", []),
            "actions": template_catalog.get("actions", []),
            "backend_services": template_catalog.get("backend_services", []),
            "api_routes": template_catalog.get("api_routes", []),
            "permissions": template_catalog.get("permissions", []),
            "template_diagnostics": template_catalog.get("template_diagnostics", []),
            "commands": template_catalog.get("commands", []),
            "composer_inputs": template_catalog.get("composer_inputs", []),
            "ai_inputs": template_catalog.get("ai_inputs", []),
            "tool_policies": template_catalog.get("tool_policies", []),
            "composer_widgets": template_catalog.get("composer_widgets", []),
            "context_policies": template_catalog.get("context_policies", []),
            "external_io_templates": template_catalog.get("external_io_templates", []),
            "shell_regions": template_catalog.get("shell_regions", []),
            "shell_renderers": template_catalog.get("shell_renderers", []),
            "extension_points": self._extension_points(),
            "diagnostics": self._diagnostics(shell, parts, component_bindings),
        }

    @staticmethod
    def _dynamic_frontend_catalog() -> dict[str, Any] | None:
        """Project the active core-owned frontend catalog into the legacy API."""
        try:
            from .host import build_frontend_catalog
            from core_runtime.resolved_profile_scope import persisted_resolved_profile

            plan = persisted_resolved_profile()
            if plan is None:
                return None
            return build_frontend_catalog(plan).to_dict()
        except Exception:
            return None

    def get_settings(self, *, lightweight: bool = False) -> dict[str, Any]:
        self._load_diagnostics: list[dict[str, Any]] = []
        template_catalog = self._template_catalog_metadata()
        ui_surfaces = self._load_ui_surfaces()
        return {
            "sections": self._merge_settings_sections(
                self._settings_sections(
                    ui_surfaces,
                    self._load_extensions(),
                    template_catalog=template_catalog,
                    lightweight=lightweight,
                ),
                template_catalog.get("settings_sections", []),
                hydrate_dynamic=not lightweight,
            ),
            "values": self._read_settings(),
        }

    def update_settings(self, patch: dict[str, Any] | None) -> dict[str, Any]:
        sanitized_patch = self._sanitize_settings_patch(patch or {})
        def merge(current: dict[str, Any]) -> dict[str, Any]:
            values = self._deep_merge(self._default_settings(), current)
            self._mark_explicit_keyboard_navigation_change(
                values, sanitized_patch
            )
            return self._refresh_derived_settings(
                self._deep_merge(values, sanitized_patch)
            )

        return self._settings_store.update(merge)

    def build_conversation_preview(self, conversation_id: str) -> dict[str, Any]:
        store = ChatStore()
        conversation = store.get_conversation(conversation_id)
        if conversation is None:
            raise KeyError(conversation_id)

        inspector = Inspector()
        preview_items: list[dict[str, Any]] = []
        latest_log = inspector.find_by_conversation(conversation_id, limit=1)
        if latest_log:
            preview_items.extend(self._preview_from_log(latest_log[0]))

        for message in conversation.get("messages", [])[-6:]:
            preview_items.extend(self._preview_from_message(message))

        preview_items.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
        return {
            "conversation_id": conversation_id,
            "previews": preview_items[:20],
            "summary": {
                "messages": len(conversation.get("messages", [])),
                "preview_count": len(preview_items[:20]),
            },
        }

    def _sidebar_filters(self) -> list[dict[str, str]]:
        return builtin_ui_catalog()["sidebar_filters"]

    def _app_metadata(self, ui_surfaces: list[dict[str, Any]]) -> dict[str, Any]:
        app: dict[str, Any] = {
            "id": "defaultspack",
            "name": "Tobkiri",
            "icon": "/static/assets/icons/defaultspack-icon.png",
            "account": self._rumi_account_metadata(),
        }
        for surface in ui_surfaces:
            config = surface.get("config", {})
            if isinstance(config, dict) and isinstance(config.get("app"), dict):
                app = self._deep_merge(app, config["app"])
        return app

    def _rumi_root(self) -> Path:
        return self._pack_root.parents[1]

    def _rumi_account_metadata(self) -> dict[str, Any]:
        account: dict[str, Any] = {
            "display_name": "Developer",
            "email": "",
            "plan_label": "Local Account",
            "avatar_url": "",
            "initial": "D",
            "source": "fallback",
        }
        token_payload = self._read_rumi_oauth_payload()
        profile = self._read_rumi_profile()
        user_metadata = token_payload.get("user_metadata", {}) if isinstance(token_payload, dict) else {}
        app_metadata = token_payload.get("app_metadata", {}) if isinstance(token_payload, dict) else {}
        email = str(token_payload.get("email") or user_metadata.get("email") or "").strip()
        display_name = str(
            profile.get("username")
            or user_metadata.get("full_name")
            or user_metadata.get("name")
            or token_payload.get("name")
            or ""
        ).strip()
        if not display_name and email:
            display_name = email.split("@", 1)[0]
        avatar_url = str(
            profile.get("icon")
            or user_metadata.get("avatar_url")
            or user_metadata.get("picture")
            or token_payload.get("picture")
            or ""
        ).strip()
        plan_label = str(
            profile.get("plan")
            or profile.get("subscription_plan")
            or token_payload.get("plan")
            or token_payload.get("subscription_plan")
            or app_metadata.get("plan")
            or app_metadata.get("subscription_plan")
            or "Local Account"
        ).strip()
        if display_name:
            account["display_name"] = display_name
        if email:
            account["email"] = email
        if avatar_url:
            account["avatar_url"] = avatar_url
        if plan_label:
            account["plan_label"] = plan_label
        account["initial"] = str(account["display_name"] or account["email"] or "R")[0].upper()
        account["source"] = "rumi_profile" if profile else ("rumi_oauth" if token_payload else "fallback")
        return account

    def _read_rumi_profile(self) -> dict[str, Any]:
        profile_path = self._rumi_root() / "user_data" / "settings" / "profile.json"
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            return profile if isinstance(profile, dict) else {}
        except Exception:
            return {}

    def _read_rumi_oauth_payload(self) -> dict[str, Any]:
        token_path = self._rumi_root() / "user_data" / "settings" / "oauth_tokens.json"
        try:
            token_data = json.loads(token_path.read_text(encoding="utf-8"))
            token = str(token_data.get("access_token", ""))
            payload_segment = token.split(".")[1]
            padding = "=" * (-len(payload_segment) % 4)
            decoded = base64.urlsafe_b64decode((payload_segment + padding).encode("ascii"))
            payload = json.loads(decoded.decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _shell(
        self,
        ui_surfaces: list[dict[str, Any]],
        extensions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        shell: dict[str, object] = builtin_ui_catalog()["shell"]
        user_shell = self._load_shell_config()
        for manifest in [*ui_surfaces, user_shell, *extensions]:
            config = manifest.get("config", manifest)
            if not isinstance(config, dict):
                continue
            if isinstance(config.get("shell_layout"), dict):
                current_layout = _validated_dict(shell.get("layout"))
                shell["layout"] = self._deep_merge(current_layout, config["shell_layout"])
            renderers = config.get("shell_renderers")
            if isinstance(renderers, list):
                current_renderers = _validated_dict_list(shell.get("renderers"))
                shell["renderers"] = self._dedupe_by_key(
                    [*current_renderers, *[dict(item) for item in renderers if isinstance(item, dict)]],
                    "id",
                )
        return shell

    def _merge_template_shell_catalog(
        self,
        shell: dict[str, Any],
        template_catalog: dict[str, Any],
    ) -> dict[str, Any]:
        merged = deepcopy(shell)
        layout = merged.get("layout")
        if not isinstance(layout, dict):
            layout = {}
            merged["layout"] = layout

        template_regions = template_catalog.get("shell_regions")
        if isinstance(template_regions, list):
            regions = _validated_dict_list(layout.get("regions"))
            layout["regions"] = self._merge_template_shell_items(
                regions,
                _validated_dict_list(template_regions),
            )

        template_renderers = template_catalog.get("shell_renderers")
        if isinstance(template_renderers, list):
            renderers = _validated_dict_list(merged.get("renderers"))
            merged["renderers"] = self._merge_template_shell_items(
                renderers,
                _validated_dict_list(template_renderers),
            )
        return merged

    def _merge_template_shell_items(
        self,
        base_items: list[dict[str, Any]],
        template_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        index_by_id: dict[str, int] = {}
        for item in base_items:
            current = deepcopy(item)
            item_id = str(current.get("id") or "").strip()
            if item_id and item_id in index_by_id:
                merged[index_by_id[item_id]] = current
                continue
            if item_id:
                index_by_id[item_id] = len(merged)
            merged.append(current)

        for item in template_items:
            current = deepcopy(item)
            item_id = str(current.get("id") or "").strip()
            if not item_id or item_id not in index_by_id:
                if item_id:
                    index_by_id[item_id] = len(merged)
                merged.append(current)
                continue
            self._copy_template_projection_metadata(merged[index_by_id[item_id]], current)
        return merged

    @staticmethod
    def _copy_template_projection_metadata(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key in ("kind", "template_id", "piece_id", "projected_id", "origin", "trust_level", "_source"):
            if key in source:
                target[key] = deepcopy(source[key])

    def _parts(
        self,
        ui_surfaces: list[dict[str, Any]],
        extensions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = builtin_ui_catalog()["parts"]
        parts.extend(self._config_list(ui_surfaces, "parts"))
        parts.extend(self._config_list(extensions, "parts"))
        return self._dedupe_by_key(parts, "id")

    def _component_bindings(
        self,
        ui_surfaces: list[dict[str, Any]],
        extensions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        bindings: list[dict[str, Any]] = builtin_ui_catalog()["component_bindings"]
        bindings.extend(self._config_list(ui_surfaces, "component_bindings"))
        bindings.extend(self._config_list(extensions, "component_bindings"))
        return self._dedupe_by_key(bindings, "part_id")

    def _profile_frontend_selection(self, profile_id: str | None) -> set[str]:
        # Frontend attachment is resolved by v4 Shell/contract bindings.  The
        # removed Profile YAML graph cannot filter the active Shell.
        del profile_id
        return set()

    def _filter_shell(self, shell: dict[str, Any], selected_frontend_ids: set[str]) -> dict[str, Any]:
        if not selected_frontend_ids:
            return shell
        filtered = deepcopy(shell)
        layout = filtered.get("layout")
        if isinstance(layout, dict) and isinstance(layout.get("regions"), list):
            layout["regions"] = self._filter_frontend_items(layout.get("regions") or [], selected_frontend_ids)
        if isinstance(filtered.get("renderers"), list):
            filtered["renderers"] = self._filter_frontend_items(filtered.get("renderers") or [], selected_frontend_ids)
        return filtered

    def _filter_frontend_items(self, items: list[dict[str, Any]], selected_frontend_ids: set[str]) -> list[dict[str, Any]]:
        if not selected_frontend_ids:
            return items
        filtered: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            visibility = _validated_dict(item.get("profile_visibility"))
            selected_ids = visibility.get("selected_frontend_ids")
            if isinstance(selected_ids, list):
                normalized = {
                    str(value).strip()
                    for value in selected_ids
                    if isinstance(value, str) and value.strip()
                }
                if normalized and not (normalized & selected_frontend_ids):
                    continue
            filtered.append(item)
        return filtered

    def _sidebar_items(
        self,
        ui_surfaces: list[dict[str, Any]],
        extensions: list[dict[str, Any]],
        *,
        lightweight: bool = False,
    ) -> list[dict[str, Any]]:
        registry = ToolRegistry()
        items: list[dict[str, Any]] = builtin_ui_catalog()["sidebar_primary_items"]

        try:
            activity_manifests = (
                get_extension_registry(force_reload=True)
                .activities()
                .list(enabled_only=True)
            )
        except Exception:
            activity_manifests = []
        for activity in activity_manifests:
            activity_id = str(activity.get("id") or "").strip()
            if not activity_id:
                continue
            label = self._localized_label(
                activity.get("display_name"), activity_id
            )
            items.append(
                {
                    "id": activity_id,
                    "label": label,
                    "category": "activity",
                    "description": self._localized_label(
                        activity.get("description"), ""
                    ),
                    "tags": [
                        "activity",
                        *[
                            str(alias)
                            for alias in activity.get("aliases", [])
                            if str(alias).strip()
                        ],
                    ],
                    "ui": (
                        dict(activity.get("ui"))
                        if isinstance(activity.get("ui"), dict)
                        else {}
                    ),
                    "origin": {
                        "kind": "activity_registry",
                        "path": str(activity.get("source_path") or ""),
                    },
                    "panel": {
                        "kind": "activity",
                        "title": label,
                        "notes": [
                            "このActivityのToolとSkillはCapability Planで動的に解決されます。",
                            "明示指定: @" + activity_id,
                        ],
                    },
                }
            )

        for tool in registry.list_tools():
            schema = tool.get("schema", {}).get("parameters", {})
            execution_type = tool.get("execution", {}).get("type", "local")
            ui = dict(tool.get("ui", {})) if isinstance(tool.get("ui"), dict) else {}
            ui["advanced_only"] = True
            label = self._tool_display_label(tool, ui)
            risk = str(tool.get("risk") or tool.get("metadata", {}).get("risk") or "low").strip().lower()
            tags = [str(tag) for tag in tool.get("tags", []) if str(tag)]
            if risk == "high" and "danger" not in tags:
                tags.append("danger")
            items.append(
                {
                    "id": tool.get("tool_id", tool.get("name", "tool")),
                    "label": label,
                    "category": "tool",
                    "description": tool.get("summary", ""),
                    "badge": "Dynamic" if execution_type == "dynamic" else None,
                    "tags": tags,
                    "risk": risk,
                    "ui": ui,
                    "tool_info": {
                        "requires_approval": bool(tool.get("requires_approval")),
                        "approval_policy": str(tool.get("approval_policy") or ""),
                        "attachment_policy": str(tool.get("attachment_policy") or ""),
                        "supports_attachments": tool.get("supports_attachments"),
                        "capability_requirements": (
                            dict(tool.get("capability_requirements"))
                            if isinstance(tool.get("capability_requirements"), dict)
                            else {}
                        ),
                        "requires_model_capabilities": [
                            str(item)
                            for item in (tool.get("requires_model_capabilities") or [])
                            if str(item or "").strip()
                        ],
                        "requires_input_modalities": [
                            str(item)
                            for item in (tool.get("requires_input_modalities") or [])
                            if str(item or "").strip()
                        ],
                        "requires_runtime_capabilities": [
                            str(item)
                            for item in (tool.get("requires_runtime_capabilities") or [])
                            if str(item or "").strip()
                        ],
                        "setup_state": {"status": "ok", "missing": []},
                        "trusted": bool(tool.get("trusted", False)),
                        "source_pack_id": str(tool.get("source_pack_id") or ""),
                    },
                    "origin": {"kind": "tool_registry", "path": "domain/tool/registry.py"},
                    "panel": {
                        "kind": "tool_settings",
                        "title": label,
                        "fields": self._tool_settings_fields(ui),
                        "actions": self._tool_panel_actions(ui),
                        "notes": [
                            "Tool call arguments stay in ToolRegistry schema and are not shown as settings.",
                            self._tool_schema_summary(schema),
                            self._tool_capability_summary(tool),
                        ],
                    },
                }
            )

        items.extend(
            builtin_ui_catalog()["sidebar_system_items"]
        )

        items.extend(self._config_list(ui_surfaces, "sidebar_items"))
        items.extend(
            self._hydrate_sidebar_items(
                self._config_list(extensions, "sidebar_items"),
                hydrate_models=not lightweight,
            )
        )

        return sorted(self._dedupe_by_key(items, "id"), key=self._sidebar_item_sort_key)

    @staticmethod
    def _localized_label(value: Any, fallback: str) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for locale in ("ja", "en"):
                text = str(value.get(locale) or "").strip()
                if text:
                    return text
            for candidate in value.values():
                text = str(candidate or "").strip()
                if text:
                    return text
        return fallback

    @staticmethod
    def _tool_display_label(tool: dict[str, Any], ui: dict[str, Any]) -> str:
        for value in (
            tool.get("display_name"),
            ui.get("composer_label"),
            tool.get("name"),
            tool.get("tool_id"),
        ):
            label = str(value or "").strip()
            if label:
                return label
        return "tool"

    def _skill_items(self) -> list[dict[str, Any]]:
        try:
            skills = get_extension_registry(force_reload=True).skills().list(enabled_only=True)
        except Exception:
            return []

        items: list[dict[str, Any]] = []
        for skill in skills:
            skill_id = str(skill.get("id") or "").strip()
            if not skill_id:
                continue
            display_name = str(skill.get("display_name") or skill.get("name") or skill_id.rsplit("/", 1)[-1]).strip()
            triggers = skill.get("triggers") if isinstance(skill.get("triggers"), list) else []
            applies_to = skill.get("applies_to_tools") if isinstance(skill.get("applies_to_tools"), list) else []
            metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
            aliases = skill.get("aliases") if isinstance(skill.get("aliases"), list) else metadata.get("aliases", [])
            items.append(
                {
                    "id": skill_id,
                    "label": display_name,
                    "description": str(skill.get("description") or metadata.get("feedback") or ""),
                    "triggers": [str(item) for item in triggers if str(item).strip()],
                    "applies_to_tools": [str(item) for item in applies_to if str(item).strip()],
                    "aliases": [str(item) for item in aliases if str(item).strip()] if isinstance(aliases, list) else [],
                    "metadata": {
                        "source": metadata.get("source", "skill"),
                        "source_path": skill.get("source_path", ""),
                    },
                }
            )
        return sorted(items, key=lambda item: (item["label"].casefold(), item["id"].casefold()))

    @staticmethod
    def _sidebar_item_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        category_order = {
            "widget": 0,
            "activity": 1,
            "capability": 2,
            "integration": 3,
            "system": 4,
            "tool": 5,
        }
        tool_group_order = {
            "browser": 0,
            "computer": 1,
            "coding/files/read": 10,
            "coding/files/write": 11,
            "coding/github/status": 20,
            "coding/github/commit": 21,
            "coding/terminal/exec": 30,
            "build": 40,
            "terminal": 50,
            "research": 60,
            "planning": 70,
            "agent": 80,
            "manage": 90,
            "operate": 100,
            "other": 999,
        }
        category = str(item.get("category", "system"))
        ui = item.get("ui")
        ui = ui if isinstance(ui, dict) else {}
        group_id = str(ui.get("group_id") or "")
        group_root = group_id.split("/", 1)[0] if group_id else ""
        group_rank = tool_group_order.get(group_id, tool_group_order.get(group_root, 500))
        label = str(item.get("label") or item.get("id") or "").casefold()
        item_id = str(item.get("id") or "").casefold()
        return (category_order.get(category, 99), group_rank, group_id.casefold(), label, item_id)

    def _settings_sections(
        self,
        ui_surfaces: list[dict[str, Any]],
        extensions: list[dict[str, Any]],
        *,
        template_catalog: dict[str, Any] | None = None,
        lightweight: bool = False,
        inputs: SettingsCatalogInputs | None = None,
    ) -> list[dict[str, Any]]:
        if inputs is None:
            templates = self._external_io_template_catalog(template_catalog)
            inputs = SettingsCatalogInputs(
                input_templates=_validated_dict_list(templates.get("input")),
                output_templates=_validated_dict_list(templates.get("output")),
                input_profile_options=self._input_profile_options(),
                output_profile_options=self._output_profile_options(),
                model_options=self._model_options(lightweight=lightweight),
                model_route_options=self._model_route_options(lightweight=lightweight),
                api_key_status=[] if lightweight else provider_key_status(pack_root=self._pack_root),
            )
        return SettingsSections().build(
            ui_surfaces, extensions, template_catalog=template_catalog, inputs=inputs
        )

    def _chat_renderers(
        self,
        ui_surfaces: list[dict[str, Any]],
        extensions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        renderers = builtin_ui_catalog()["chat_renderers"]

        renderers.extend(self._config_list(ui_surfaces, "chat_renderers"))
        renderers.extend(self._config_list(extensions, "chat_renderers"))

        return self._dedupe_by_key(renderers, "id")

    def _extension_points(self) -> list[dict[str, Any]]:
        return builtin_ui_catalog()["extension_points"]

    def _preview_from_log(self, log: dict[str, Any]) -> list[dict[str, Any]]:
        timestamp = self._iso_to_ms(log.get("timestamp"))
        items: list[dict[str, Any]] = []
        context_info = log.get("context_info", {})
        for index, item in enumerate(context_info.get("knowledge_results", []), start=1):
            items.append(
                {
                    "id": f"knowledge-{index}-{timestamp}",
                    "toolStepId": "knowledge",
                    "timestamp": timestamp - index,
                    "data": {
                        "type": "web",
                        "url": item.get("metadata", {}).get("source", ""),
                        "title": item.get("metadata", {}).get("title", f"Knowledge #{index}"),
                        "snippet": item.get("content", ""),
                    },
                }
            )

        for index, item in enumerate(context_info.get("memory_results", []), start=1):
            items.append(
                {
                    "id": f"memory-{index}-{timestamp}",
                    "toolStepId": "memory",
                    "timestamp": timestamp - 100 - index,
                    "data": {
                        "type": "file",
                        "filename": item.get("metadata", {}).get("source", f"memory-{index}.md"),
                        "size": f"score {item.get('score', 0):.2f}",
                        "content": item.get("content", ""),
                    },
                }
            )
        return items

    def _preview_from_message(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        timestamp = int(message.get("created_at", 0))
        previews: list[dict[str, Any]] = []
        widget = message.get("widget")
        if isinstance(widget, dict):
            previews.append(
                {
                    "id": f"widget-{message.get('id')}",
                    "toolStepId": "widget",
                    "timestamp": timestamp,
                    "data": {
                        "type": "file",
                        "filename": f"widget:{widget.get('type', 'custom')}",
                        "size": "inline widget",
                        "content": json.dumps(widget, ensure_ascii=False, indent=2),
                    },
                }
            )

        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "image":
                previews.append(
                    {
                        "id": f"image-{message.get('id')}-{index}",
                        "toolStepId": "image",
                        "timestamp": timestamp - index,
                        "data": {
                            "type": "image",
                            "url": block.get("url", ""),
                            "alt": block.get("alt", "image"),
                            "prompt": block.get("prompt"),
                        },
                    }
                )
            elif block_type == "code":
                previews.append(
                    {
                        "id": f"code-{message.get('id')}-{index}",
                        "toolStepId": "code",
                        "timestamp": timestamp - index,
                        "data": {
                            "type": "code",
                            "filename": block.get("filename", "snippet"),
                            "language": block.get("language", "text"),
                            "content": block.get("text", ""),
                        },
                    }
                )
        for index, log in enumerate(message.get("tool_logs") or []):
            if isinstance(log, dict):
                previews.extend(self._preview_from_tool_log(message, log, index))
        return previews

    def _preview_from_tool_log(self, message: dict[str, Any], log: dict[str, Any], index: int) -> list[dict[str, Any]]:
        timestamp = int(message.get("created_at", 0)) - 200 - index
        tool_name = str(log.get("tool_name") or "tool")
        result = log.get("result")
        if self._tool_result_failed(result) or self._tool_result_pending_approval(result):
            return []
        previews: list[dict[str, Any]] = []
        conversation_id = str(message.get("conversation_id") or "")
        for artifact_index, path in enumerate(self._artifact_paths_from_value(result)):
            name = Path(path).name or "artifact"
            url = "/api/chat/conversations/{}/artifact-file?path={}".format(
                quote(conversation_id, safe=""),
                quote(path, safe=""),
            )
            if self._is_image_path(path):
                previews.append(
                    {
                        "id": f"tool-log-artifact-{message.get('id')}-{index}-{artifact_index}",
                        "toolStepId": tool_name,
                        "timestamp": timestamp + artifact_index + 0.1,
                        "data": {
                            "type": "image",
                            "url": url,
                            "alt": name,
                            "path": path,
                        },
                    }
                )
            else:
                content = self._artifact_content_from_value(result, path)
                if content is None:
                    continue
                previews.append(
                    {
                        "id": f"tool-log-artifact-{message.get('id')}-{index}-{artifact_index}",
                        "toolStepId": tool_name,
                        "timestamp": timestamp + artifact_index + 0.1,
                        "data": {
                            "type": "file",
                            "filename": name,
                            "size": "tool artifact",
                            "path": path,
                            "url": url,
                            "downloadName": name,
                            "content": content,
                        },
                    }
                )
        for image_index, url in enumerate(self._inline_image_urls_from_value(result)):
            previews.append(
                {
                    "id": f"tool-log-inline-{message.get('id')}-{index}-{image_index}",
                    "toolStepId": tool_name,
                    "timestamp": timestamp + len(previews) + image_index + 0.1,
                    "data": {
                        "type": "image",
                        "url": url,
                        "alt": self._data_url_name(url),
                    },
                }
            )
        return previews

    def _tool_result_failed(self, value: Any) -> bool:
        if isinstance(value, list):
            return any(self._tool_result_failed(item) for item in value)
        if not isinstance(value, dict):
            return False
        if value.get("is_error") is True or value.get("ok") is False or value.get("success") is False:
            return True
        for key in ("status", "phase", "outcome"):
            status = str(value.get(key) or "").strip().lower()
            if status in {"error", "failed", "failure", "denied", "rejected", "cancelled", "canceled"}:
                return True
        return any(
            self._tool_result_failed(value.get(key))
            for key in ("data", "result", "output")
            if isinstance(value.get(key), dict)
        )

    def _tool_result_pending_approval(self, value: Any) -> bool:
        if isinstance(value, list):
            return any(self._tool_result_pending_approval(item) for item in value)
        if not isinstance(value, dict):
            return False
        if value.get("approval_required") is True or value.get("requires_approval") is True:
            return True
        status = str(value.get("status") or value.get("phase") or value.get("outcome") or "").strip().lower()
        if status in {"approval_required", "requires_approval", "pending_approval"}:
            return True
        return any(
            self._tool_result_pending_approval(value.get(key))
            for key in ("widget", "data", "result", "output")
            if isinstance(value.get(key), dict)
        )

    def _artifact_content_from_value(self, value: Any, path: str) -> str | None:
        target_path = str(path or "").strip()
        if not target_path:
            return None
        return self._artifact_content_from_node(value, target_path, set())

    def _artifact_content_from_node(self, value: Any, path: str, seen: set[int]) -> str | None:
        if isinstance(value, dict):
            value_id = id(value)
            if value_id in seen:
                return None
            seen.add(value_id)
            if self._mapping_references_artifact_path(value, path):
                content = self._content_from_artifact_mapping(value, path)
                if content is not None:
                    return content
            for key, item in value.items():
                if key in {"data_url", "dataUrl"}:
                    continue
                content = self._artifact_content_from_node(item, path, seen)
                if content is not None:
                    return content
        elif isinstance(value, list):
            value_id = id(value)
            if value_id in seen:
                return None
            seen.add(value_id)
            for item in value:
                content = self._artifact_content_from_node(item, path, seen)
                if content is not None:
                    return content
        return None

    @staticmethod
    def _mapping_references_artifact_path(value: dict[str, Any], path: str) -> bool:
        for key in ("model_image_path", "screenshot_path", "path"):
            item = value.get(key)
            if isinstance(item, str) and item.strip() == path:
                return True
        return False

    def _content_from_artifact_mapping(self, value: dict[str, Any], path: str) -> str | None:
        for key in ("content", "text", "markdown", "body", "html"):
            content = self._coerce_artifact_content(value.get(key), path)
            if content is not None:
                return content
        return None

    @staticmethod
    def _coerce_artifact_content(value: Any, path: str) -> str | None:
        if isinstance(value, str):
            content = value
        elif isinstance(value, (dict, list)):
            content = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            return None
        normalized = " ".join(content.split()).strip()
        if not normalized or normalized == path or normalized.lower().startswith("artifact:"):
            return None
        return content

    def _artifact_paths_from_value(self, value: Any, seen: set[str] | None = None) -> list[str]:
        seen = seen or set()
        paths: list[str] = []
        if isinstance(value, dict):
            preferred = ""
            for key in ("model_image_path", "screenshot_path", "workspace_path", "path"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    preferred = item.strip()
                    break
            if preferred and preferred not in seen:
                seen.add(preferred)
                paths.append(preferred)
            for key, item in value.items():
                if key in {"path", "workspace_path", "screenshot_path", "model_image_path", "data_url", "dataUrl"}:
                    continue
                paths.extend(self._artifact_paths_from_value(item, seen))
        elif isinstance(value, list):
            for item in value:
                paths.extend(self._artifact_paths_from_value(item, seen))
        return paths

    def _inline_image_urls_from_value(self, value: Any, seen: set[str] | None = None) -> list[str]:
        seen = seen or set()
        urls: list[str] = []
        if isinstance(value, dict):
            for key in ("data_url", "dataUrl"):
                item = value.get(key)
                if isinstance(item, str) and self._is_image_data_url(item) and item not in seen:
                    seen.add(item)
                    urls.append(item)
            for key, item in value.items():
                if key in {"data_url", "dataUrl"}:
                    continue
                urls.extend(self._inline_image_urls_from_value(item, seen))
        elif isinstance(value, list):
            for item in value:
                urls.extend(self._inline_image_urls_from_value(item, seen))
        return urls

    @staticmethod
    def _is_image_path(path: str) -> bool:
        return Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

    @staticmethod
    def _is_image_data_url(value: str) -> bool:
        return value.lower().startswith("data:image/") and ";base64," in value[:80].lower()

    @staticmethod
    def _data_url_name(value: str) -> str:
        prefix = value.split(";", 1)[0]
        extension = prefix.rsplit("/", 1)[-1].replace("jpeg", "jpg").split("+")[0] or "png"
        return f"screenshot.{extension}"

    def _preview_text(self, value: Any, limit: int) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        elif isinstance(value, (int, float, bool)):
            text = str(value)
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        text = " ".join(text.split())
        return text if len(text) <= limit else f"{text[:limit - 1]}…"

    def _load_ui_surfaces(self) -> list[dict[str, Any]]:
        try:
            surfaces = get_extension_registry().ui_surfaces().list(enabled_only=True)
        except Exception:
            surfaces = []
        return [
            *[surface for surface in surfaces if isinstance(surface, dict)],
            *self._load_component_ui_surfaces(),
        ]

    def _load_component_ui_surfaces(self) -> list[dict[str, Any]]:
        try:
            registry = DomainComponentRegistry(build_domain_component_roots(self._pack_root))
        except Exception:
            return []
        surfaces: list[dict[str, Any]] = []
        for component in registry.list("ui_surfaces"):
            manifest = component.as_dict()
            config = manifest.get("ui")
            if not isinstance(config, dict):
                config = {}
            surfaces.append(
                {
                    "id": component.id,
                    "category": "ui_surface",
                    "config": config,
                    "_source": manifest.get("source_path", ""),
                    "source_pack_id": component.source_pack_id,
                }
            )
        return surfaces

    def _route_metadata(self) -> dict[str, Any]:
        component_specs = component_http_route_specs()
        template_specs = template_http_route_specs(defaultspack_root=self._pack_root)
        return {
            "manifest_backed": [
                {
                    "method": spec.method,
                    "path": spec.pattern,
                    "block_module": spec.block_module,
                    "handler_name": spec.handler_name,
                }
                for spec in component_specs
            ],
            "template_backed": [
                {
                    "method": spec.method,
                    "path": spec.pattern,
                    "function_id": spec.function_id,
                    "function_name": spec.function_name,
                }
                for spec in template_specs
            ],
            "diagnostics": [
                *component_route_diagnostics(),
                *template_route_diagnostics(defaultspack_root=self._pack_root),
            ],
        }

    def _load_extensions(self) -> list[dict[str, Any]]:
        extensions = []
        for path in self._frontend_extension_paths():
            try:
                extension = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(extension, dict):
                    extension["_source"] = str(path)
                    projection_id = self._source_projection_id(path)
                    if projection_id:
                        extension["source_projection_id"] = projection_id
                        extension["source_authority_id"] = projection_id
                        extension["source_authority_kind"] = "ui_projection"
                    else:
                        pack_id = self._source_pack_id(path)
                        extension["source_pack_id"] = pack_id
                        extension["source_authority_id"] = pack_id
                        extension["source_authority_kind"] = "pack"
                    extensions.append(extension)
                else:
                    self._add_diagnostic("warning", "frontend_extension_not_object", f"{path} must contain a JSON object.", str(path))
            except (OSError, json.JSONDecodeError) as exc:
                self._add_diagnostic("warning", "frontend_extension_invalid_json", str(exc), str(path))
                continue
        return extensions

    def _frontend_extension_paths(self) -> list[Path]:
        paths: list[Path] = []
        seen: set[Path] = set()
        for directory in self._frontend_extension_dirs():
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.ui.json")):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                paths.append(path)
        return paths

    def _frontend_extension_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        ecosystem_root = self._ecosystem_root()
        selected_pack_ids = selected_extension_pack_ids(self._pack_root)
        if ecosystem_root.exists():
            for pack_id in sorted(selected_pack_ids):
                pack_root = ecosystem_root / pack_id
                if not pack_root.is_dir() or self._v4_pack_id(pack_root) != pack_id:
                    continue
                dirs.append(pack_root / "frontend_extensions")
        from core_runtime.profile_content_projection import selected_projection_roots
        from core_runtime.resolved_profile_scope import effective_profile_projections

        for _projection_id, root in selected_projection_roots(
            effective_profile_projections(), kind="ui_projection"
        ):
            dirs.append(root / "frontend_extensions")
        return dirs

    @staticmethod
    def _source_projection_id(path: Path) -> str:
        from core_runtime.profile_content_projection import selected_projection_roots
        from core_runtime.resolved_profile_scope import effective_profile_projections

        resolved_path = path.resolve()
        for projection_id, root in selected_projection_roots(
            effective_profile_projections(), kind="ui_projection"
        ):
            try:
                resolved_path.relative_to(root.resolve())
                return projection_id
            except ValueError:
                continue
        return ""

    def _ecosystem_root(self) -> Path:
        if self._v4_pack_id(self._pack_root) and self._pack_root.parent.name == "ecosystem":
            return self._pack_root.parent
        return Path(__file__).resolve().parents[3]

    def _source_pack_id(self, path: Path) -> str:
        for parent in path.parents:
            pack_id = self._v4_pack_id(parent)
            if pack_id:
                return pack_id
        return ""

    @staticmethod
    def _v4_pack_id(pack_root: Path) -> str:
        candidates = (
            pack_root / "pack.v4.json",
            pack_root / "v4" / "packs" / f"{pack_root.name}.pack.v4.json",
        )
        for manifest_path in candidates:
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                pack_id = str((raw.get("pack") or {}).get("id") or "").strip()
                if pack_id:
                    return pack_id
            except (OSError, UnicodeError, ValueError, TypeError):
                continue
        return ""

    def _load_shell_config(self) -> dict[str, Any]:
        # Shell selection is bound by the verified v4 Profile/ShellDefinition.
        # Mutable user_data shell JSON is not a layout authority.
        return {}

    def _template_catalog_metadata(self) -> dict[str, Any]:
        try:
            template_projectors = importlib.import_module("domain.templates.projectors")
            build_template_catalog = template_projectors.build_template_catalog
            catalog = build_template_catalog(defaultspack_root=self._pack_root)
        except Exception as exc:
            self._add_diagnostic(
                "warning",
                "template_catalog_build_failed",
                f"failed to build template catalog: {exc}",
                str(self._pack_root / "templates"),
            )
            return self._empty_template_catalog()
        if not isinstance(catalog, dict):
            self._add_diagnostic(
                "warning",
                "template_catalog_invalid",
                "template catalog projector returned a non-object result.",
                "domain/templates/projectors",
            )
            return self._empty_template_catalog()

        for diagnostic in catalog.get("template_diagnostics", []):
            if not isinstance(diagnostic, dict):
                continue
            self._add_diagnostic(
                str(diagnostic.get("level") or diagnostic.get("severity") or "warning"),
                str(diagnostic.get("code") or "template_catalog_diagnostic"),
                str(diagnostic.get("message") or ""),
                str(diagnostic.get("source") or diagnostic.get("source_path") or "template_catalog"),
            )
        return catalog

    @staticmethod
    def _empty_template_catalog() -> dict[str, Any]:
        try:
            return importlib.import_module("domain.templates.projectors").empty_template_catalog()
        except Exception:
            return {"template_diagnostics": []}

    def _external_io_template_catalog(self, template_catalog: dict[str, Any] | None = None) -> dict[str, Any]:
        template_items = None
        if isinstance(template_catalog, dict) and isinstance(template_catalog.get("external_io_templates"), list):
            template_items = [item for item in template_catalog["external_io_templates"] if isinstance(item, dict)]
        return external_io_template_catalog(self._pack_root, template_items=template_items)

    def _merge_settings_sections(
        self,
        base_sections: list[dict[str, Any]],
        extra_sections: list[dict[str, Any]],
        *,
        hydrate_dynamic: bool = True,
    ) -> list[dict[str, Any]]:
        try:
            merge_settings_sections = importlib.import_module("domain.templates.projectors").merge_settings_sections
            sections, diagnostics = merge_settings_sections([*base_sections, *extra_sections])
        except Exception as exc:
            self._add_diagnostic(
                "warning",
                "template_settings_merge_failed",
                f"failed to merge template settings sections: {exc}",
                "domain/templates/projectors",
            )
            return [section for section in [*base_sections, *extra_sections] if isinstance(section, dict)]
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                continue
            self._add_diagnostic(
                str(diagnostic.get("level") or diagnostic.get("severity") or "warning"),
                str(diagnostic.get("code") or "template_settings_diagnostic"),
                str(diagnostic.get("message") or ""),
                str(diagnostic.get("source") or diagnostic.get("source_path") or "template_catalog"),
            )
        if not hydrate_dynamic:
            return sections
        return self._hydrate_dynamic_settings_fields(sections)

    @staticmethod
    def _template_settings_field_ids(template_catalog: dict[str, Any] | None) -> set[tuple[str, str]]:
        if not isinstance(template_catalog, dict):
            return set()
        owned: set[tuple[str, str]] = set()
        sections = template_catalog.get("settings_sections")
        if not isinstance(sections, list):
            return owned
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("id") or "").strip()
            if not section_id:
                continue
            fields = section.get("fields")
            if not isinstance(fields, list):
                continue
            for field in fields:
                if not isinstance(field, dict):
                    continue
                field_id = str(field.get("id") or "").strip()
                if field_id:
                    owned.add((section_id, field_id))
        return owned

    def _suppress_template_owned_base_settings(
        self,
        sections: list[dict[str, Any]],
        template_catalog: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        template_owned = self._template_settings_field_ids(template_catalog)
        if not template_owned:
            return sections
        filtered_sections: list[dict[str, Any]] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("id") or "").strip()
            fields = section.get("fields")
            if not section_id or not isinstance(fields, list):
                filtered_sections.append(section)
                continue
            next_section = dict(section)
            next_section["fields"] = [
                field
                for field in fields
                if not (
                    isinstance(field, dict)
                    and (section_id, str(field.get("id") or "").strip()) in template_owned
                )
            ]
            filtered_sections.append(next_section)
        return filtered_sections

    def _hydrate_dynamic_settings_fields(self, sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        provider_rows: list[dict[str, Any]] | None = None
        model_options: list[dict[str, Any]] | None = None
        model_route_options: list[dict[str, Any]] | None = None
        hydrated_sections: list[dict[str, Any]] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("id") or "").strip()
            fields = section.get("fields")
            if not isinstance(fields, list):
                hydrated_sections.append(section)
                continue
            next_section = dict(section)
            next_fields: list[dict[str, Any]] = []
            for field in fields:
                if not isinstance(field, dict):
                    continue
                item = dict(field)
                field_id = str(item.get("id") or "").strip()
                field_type = str(item.get("type") or "").strip()
                if section_id == "models" and field_id in {"preferred_model", "main_model", "lightweight_model"}:
                    if model_options is None:
                        model_options = self._model_options()
                    item["options"] = model_options
                    if field_id != "preferred_model":
                        item.setdefault("type", "model_select")
                    item.setdefault("renderer", "model_select")
                elif str(item.get("type") or "").strip() == "model_select":
                    if model_options is None:
                        model_options = self._model_options()
                    item.setdefault("options", model_options)
                if section_id == "models" and field_id == "model_api_routes":
                    if model_route_options is None:
                        model_route_options = self._model_route_options()
                    if provider_rows is None:
                        provider_rows = provider_key_status(pack_root=self._pack_root)
                    item["options"] = model_route_options
                    item["api_keys"] = provider_rows
                    item.setdefault("type", "model_api_routes")
                    item.setdefault("renderer", "model_routing")
                if section_id == "apis" and (field_id == "api_keys" or field_type == "api_key_setup"):
                    if provider_rows is None:
                        provider_rows = provider_key_status(pack_root=self._pack_root)
                    item["api_keys"] = provider_rows
                    item.setdefault("type", "api_key_setup")
                    item.setdefault("renderer", "api_key_setup")
                next_fields.append(item)
            next_section["fields"] = next_fields
            hydrated_sections.append(next_section)
        return hydrated_sections

    def _diagnostics(
        self,
        shell: dict[str, Any],
        parts: list[dict[str, Any]],
        component_bindings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        diagnostics = list(getattr(self, "_load_diagnostics", []))

        part_ids = {str(part.get("id", "")).strip() for part in parts if str(part.get("id", "")).strip()}
        renderer_ids = {
            str(renderer.get("id", "")).strip()
            for renderer in shell.get("renderers", [])
            if isinstance(renderer, dict) and str(renderer.get("id", "")).strip()
        }

        seen_parts: set[str] = set()
        for index, part in enumerate(parts):
            part_id = str(part.get("id", "")).strip()
            source = str(part.get("_source", "catalog.parts"))
            if not part_id:
                diagnostics.append(self._diagnostic("warning", "part_missing_id", f"parts[{index}] is missing id.", source))
            elif part_id in seen_parts:
                diagnostics.append(self._diagnostic("warning", "part_duplicate_id", f"part id '{part_id}' is duplicated; the last definition wins.", source))
            seen_parts.add(part_id)
            if not isinstance(part.get("kind"), str) or not str(part.get("kind", "")).strip():
                diagnostics.append(self._diagnostic("warning", "part_missing_kind", f"part '{part_id or index}' is missing kind.", source))
            if "schema" in part and not isinstance(part.get("schema"), dict):
                diagnostics.append(self._diagnostic("warning", "part_invalid_schema", f"part '{part_id or index}' schema must be an object.", source))

        for index, binding in enumerate(component_bindings):
            source = str(binding.get("_source", "catalog.component_bindings"))
            part_id = str(binding.get("part_id", "")).strip()
            if not part_id:
                diagnostics.append(self._diagnostic("warning", "binding_missing_part_id", f"component_bindings[{index}] is missing part_id.", source))
            elif part_id not in part_ids:
                diagnostics.append(self._diagnostic("warning", "binding_unknown_part", f"component binding references unknown part '{part_id}'.", source))
            if not isinstance(binding.get("component"), str) or not str(binding.get("component", "")).strip():
                diagnostics.append(self._diagnostic("warning", "binding_missing_component", f"component binding for '{part_id or index}' is missing component.", source))
            for key in ("requires", "optional"):
                if key in binding and not isinstance(binding.get(key), list):
                    diagnostics.append(self._diagnostic("warning", f"binding_invalid_{key}", f"component binding '{part_id or index}' {key} must be a list.", source))

        layout = shell.get("layout", {})
        regions = layout.get("regions", []) if isinstance(layout, dict) else []
        if not isinstance(regions, list):
            diagnostics.append(self._diagnostic("warning", "shell_regions_not_list", "shell_layout.regions must be a list.", "catalog.shell.layout"))
            regions = []

        for index, region in enumerate(regions):
            if not isinstance(region, dict):
                diagnostics.append(self._diagnostic("warning", "shell_region_not_object", f"shell_layout.regions[{index}] must be an object.", "catalog.shell.layout"))
                continue
            region_id = str(region.get("id", "")).strip()
            part_id = str(region.get("part_id", "")).strip()
            renderer_id = str(region.get("renderer", "")).strip()
            source = str(region.get("_source", "catalog.shell.layout"))
            if not region_id:
                diagnostics.append(self._diagnostic("warning", "shell_region_missing_id", f"shell_layout.regions[{index}] is missing id.", source))
            if part_id and part_id not in part_ids:
                diagnostics.append(self._diagnostic("warning", "shell_region_unknown_part", f"region '{region_id or index}' references unknown part '{part_id}'.", source))
            if renderer_id and renderer_id not in renderer_ids:
                diagnostics.append(self._diagnostic("warning", "shell_region_unknown_renderer", f"region '{region_id or index}' references unknown renderer '{renderer_id}'.", source))
            if "order" in region and not isinstance(region.get("order"), (int, float)):
                diagnostics.append(self._diagnostic("warning", "shell_region_invalid_order", f"region '{region_id or index}' order must be numeric.", source))

        for index, renderer in enumerate(shell.get("renderers", [])):
            if not isinstance(renderer, dict):
                diagnostics.append(self._diagnostic("warning", "shell_renderer_not_object", f"shell.renderers[{index}] must be an object.", "catalog.shell.renderers"))
                continue
            renderer_id = str(renderer.get("id", "")).strip()
            source = str(renderer.get("_source", "catalog.shell.renderers"))
            if not renderer_id:
                diagnostics.append(self._diagnostic("warning", "shell_renderer_missing_id", f"shell.renderers[{index}] is missing id.", source))
            if not isinstance(renderer.get("component"), str) or not str(renderer.get("component", "")).strip():
                diagnostics.append(self._diagnostic("warning", "shell_renderer_missing_component", f"shell renderer '{renderer_id or index}' is missing component.", source))
            if "regions" in renderer and not isinstance(renderer.get("regions"), list):
                diagnostics.append(self._diagnostic("warning", "shell_renderer_invalid_regions", f"shell renderer '{renderer_id or index}' regions must be a list.", source))
            module = renderer.get("module")
            if module is not None and not self._is_trusted_renderer_module(module):
                diagnostics.append(self._diagnostic("warning", "shell_renderer_untrusted_module", f"shell renderer '{renderer_id or index}' module must be a trusted static renderer path.", source))
            if module is not None and renderer.get("trust") != "local":
                diagnostics.append(self._diagnostic("warning", "shell_renderer_missing_local_trust", f"shell renderer '{renderer_id or index}' module requires trust='local'.", source))

        return diagnostics

    def _is_trusted_renderer_module(self, module: Any) -> bool:
        if not isinstance(module, str):
            return False
        return module.startswith(("/static/renderers/", "/static/assets/renderers/", "/static/user_renderers/"))

    def _add_diagnostic(self, level: str, code: str, message: str, source: str) -> None:
        if not hasattr(self, "_load_diagnostics"):
            self._load_diagnostics = []
        self._load_diagnostics.append(self._diagnostic(level, code, message, source))

    def _diagnostic(self, level: str, code: str, message: str, source: str) -> dict[str, str]:
        return {"level": level, "code": code, "message": message, "source": source}

    def _config_list(self, manifests: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for manifest in manifests:
            config = manifest.get("config", manifest)
            if not isinstance(config, dict):
                continue
            items = config.get(key, [])
            if not isinstance(items, list):
                continue
            values.extend(item for item in items if isinstance(item, dict))
        return values

    def _hydrate_sidebar_items(
        self,
        items: list[dict[str, Any]],
        *,
        hydrate_models: bool = True,
    ) -> list[dict[str, Any]]:
        hydrated: list[dict[str, Any]] = []
        for item in items:
            item = deepcopy(item)
            panel = item.get("panel")
            if (
                hydrate_models
                and isinstance(panel, dict)
                and panel.get("kind") == "models"
                and "models" not in panel
            ):
                panel["models"] = self._list_provider_models()
            hydrated.append(item)
        return hydrated

    def _dedupe_by_key(self, items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for item in items:
            value = str(item.get(key, "")).strip()
            if not value:
                value = f"__index_{len(order)}"
            if value not in deduped:
                order.append(value)
            deduped[value] = item
        return [deduped[value] for value in order]

    def _read_settings(self) -> dict[str, Any]:
        values = self._default_settings()
        try:
            saved = self._settings_store.read(preserve_corrupt=True)
        except FrontendSettingsCorruptError:
            return self._refresh_derived_settings(values)
        saved, migrated = self._migrate_legacy_keyboard_navigation(saved)
        if migrated:
            saved = self._settings_store.update(
                lambda current: self._migrate_legacy_keyboard_navigation(current)[0]
            )
        if saved:
            saved = dict(saved)
            saved.pop(MUTATION_RECEIPTS_KEY, None)
            saved.pop(STATE_REVISIONS_KEY, None)
            saved = self._settings_with_legacy_tool_version(saved)
            values = self._deep_merge(values, saved)
        return self._refresh_derived_settings(values)

    def _migrate_legacy_keyboard_navigation(
        self,
        saved: Any,
    ) -> tuple[dict[str, Any], bool]:
        """Upgrade the old persisted default without rewriting later user choices."""

        if not isinstance(saved, dict):
            return {}, False
        normalized = deepcopy(saved)
        general = normalized.get("general")
        if not isinstance(general, dict):
            general = {}
            normalized["general"] = general
        try:
            settings_version = int(general.get("settings_version") or 1)
        except (TypeError, ValueError):
            settings_version = 1
        if settings_version >= _GENERAL_SETTINGS_VERSION:
            return normalized, False

        source = str(general.get("keyboard_button_navigation_source") or "").strip()
        legacy_value = general.get("keyboard_button_navigation")
        if (
            source != _KEYBOARD_NAVIGATION_SOURCE_USER
            and legacy_value is not None
            and not self._setting_bool(legacy_value, False)
        ):
            general["keyboard_button_navigation"] = True
            general["keyboard_button_navigation_source"] = (
                _KEYBOARD_NAVIGATION_SOURCE_LEGACY_MIGRATION
            )
        else:
            general.setdefault(
                "keyboard_button_navigation_source",
                _KEYBOARD_NAVIGATION_SOURCE_DEFAULT,
            )
        general["settings_version"] = _GENERAL_SETTINGS_VERSION
        return normalized, True

    def _settings_with_legacy_tool_version(self, saved: Any) -> dict[str, Any]:
        if not isinstance(saved, dict):
            return {}
        normalized = deepcopy(saved)
        tools = normalized.get("tools")
        if isinstance(tools, dict) and "settings_version" not in tools:
            tools["settings_version"] = 1
        return normalized

    def _default_settings(self) -> dict[str, Any]:
        return {
            "general": {
                "settings_version": _GENERAL_SETTINGS_VERSION,
                "composer_placeholder": "メッセージを入力...",
                "show_activity_in_messages": True,
                "keyboard_button_navigation": True,
                "keyboard_button_navigation_source": (
                    _KEYBOARD_NAVIGATION_SOURCE_DEFAULT
                ),
                "spotlight_shortcut_enabled": True,
                "spotlight_shortcut": "Ctrl+K",
                "spotlight_shortcut_text_input": True,
                "language": "ja",
                "manual_runtime_mode_selection": False,
            },
            "preview": {"auto_open": False, "default_mode": "auto", "max_items": 12},
            "calendar": {
                "agent_current_chat": False,
                "agent_model": "",
                "agent_task_default": False,
                "default_time": "09:00",
                "quick_add_enabled": True,
                "default_item_type": "task",
                "week_start": "sunday",
                "show_outside_days": True,
                "show_time_picker": True,
                "dim_weekends": True,
                "task_color": "blue",
                "time_slot_minutes": 15,
                "event_color": "green",
                "max_items_per_day": 3,
            },
            "chat_rendering": {"show_widgets": True, "unknown_block_strategy": "placeholder"},
            "models": {
                **ModelRuntimeSettingsService(self._pack_root).default_model_settings(),
            },
            "continuity": {
                "handoff": {
                    "sandbox_id": "logical-sandbox",
                    "mode": "move",
                    "destination_node_id": "",
                    "route_id": "",
                },
            },
            "commands": {
                "show_advanced_commands": False,
            },
            "tools": {
                "settings_version": 3,
                "default_target": "",
                "default_mode": "auto",
                "selection_strategy": "hybrid",
                "semantic_backend": "auto",
                "embedding_model": "",
                "semantic_candidate_limit": 32,
                "final_tool_limit": 8,
                "catalog_ai_direct_limit": 80,
                "selector_trace": "summary",
                "show_selection_summary": True,
                "show_selection_reasons": False,
                "show_selected_tools_in_answer": True,
                "expand_selection_reasoning": False,
                "standard_permissions": {
                    "read": "auto",
                    "search": "auto",
                    "create": "confirm",
                    "update": "confirm",
                    "send": "confirm",
                    "execute": "confirm",
                    "computer": "confirm",
                    "delete": "confirm",
                },
                "action_permissions": {
                    "read": "auto",
                    "search": "auto",
                    "create": "confirm",
                    "update": "confirm",
                    "send": "confirm",
                    "execute": "confirm",
                    "computer": "confirm",
                    "delete": "confirm",
                },
                "service_permission_overrides": {},
                "tool_permission_overrides": {},
                "pinned_service_ids": [],
                "keep_selected_tools_after_send": False,
                "tool_assist_mode": "auto",
                "tool_assist_limit": 8,
                "disabled_tool_ids": [],
                "hidden_tool_ids": [],
            },
            "computer_use_haze": {
                "enabled": True,
                "preset": "aurora",
                "start_color": "#6EE7F9",
                "end_color": "#A78BFA",
                "accent_color": "#F0ABFC",
                "opacity": 0.36,
                "edge_width": 150,
                "animation_speed": 1,
            },
            "debug": {
                "ai_request_logging": False,
            },
            "sidebar": {
                "pinned_item_ids": [],
                "starred_item_ids": [],
                "custom_tool_tags": {},
                "ui_placements": [],
            },
            "apis": {
                "api_keys": [],
            },
            "external_input": {
                "input_setup_guide": (
                    "1. Providerを選ぶ\n"
                    "2. Temporary Public URLでWebhook URLを発行する\n"
                    "3. ProviderのWebhook URL欄へコピーする\n"
                    "4. LINE Messaging API Channel Secret / Access Tokenを貼る\n"
                    "5. line-main endpointを有効化し、受信元ルールを確認する"
                ),
                "endpoint_summary": "",
                "input_provider": "line",
                "input_template_id": "line.input.default",
                "input_profile_id": "line.default",
                "input_endpoint_id": "line-main",
                "public_url_launcher": {
                    "provider_id": "cloudflare_quick_tunnel",
                    "local_url": "http://127.0.0.1:8766",
                    "route_path": "/api/integrations/line/webhook",
                },
                "provider_route_copy": (
                    "LINE: /api/integrations/line/webhook\n"
                    "Discord: /api/integrations/discord/interactions, /api/integrations/discord/events\n"
                    "Slack: /api/integrations/slack/events"
                ),
                "input_template_summary": "LINE / Discord / Slack / Generic / Custom",
                "input_profile_summary": "",
                "include_source_context": True,
                "default_response_mode": "same_response",
                "input_response_preset": "same_source_reply",
                "policy_summary": "line.production: verified text only, saved source allowed, unknown source denied.",
                "saved_sources_summary": "No saved sources",
            },
            "external_output": {
                "output_setup_guide": (
                    "LINE: Messaging API Channel Access Tokenで受信元へreply。push fallbackは既定OFF\n"
                    "Discord Bot + Channel: Bot Tokenを保存し、Channel IDをTarget IDへ貼る\n"
                    "Discord Webhook URL: Channel Webhook URLをExternal Tokensへ保存する\n"
                    "Slack: Bot Tokenを保存し、Channel ID / Thread TSをTarget IDへ貼る\n"
                    "Web/local: 外部投稿せず、chat historyやlocal保存に寄せる"
                ),
                "external_tokens": [],
                "output_provider": "line",
                "output_template_id": "line.output.default",
                "output_profile_id": "line.default",
                "output_send_mode": "reply_to_origin",
                "output_target_id": "",
                "output_callback_token_id": "main",
                "output_template_summary": "Discord bot/channel or webhook URL, LINE source reply or explicit push, Slack channel, Generic webhook, Web/local.",
                "output_profile_summary": "",
                "response_summary": "Prompt decisions create action plans; tools/adapters execute after policy checks.",
                "response_prompt_preset": "same_source_reply",
                "public_url_summary": "Providers: static, cloudflare_quick_tunnel",
            },
            "external_custom": {
                "custom_template_path": "user_data/shared/external_io_templates",
                "custom_profile_paths": "user_data/shared/input_profiles, user_data/shared/output_profiles",
                "custom_prompt_examples": (
                    "Google Chromeをcomputer_useで操作して起動し、"
                    "https://chat.line.biz/U938c119aee3803767d500905c221a1f4/chat/C7d9e77e21e38512175c081f235f0aec8 "
                    "にアクセスして返答して。"
                ),
            },
        }

    def _model_options(self, *, lightweight: bool = False) -> list[dict[str, str]]:
        if lightweight:
            return [{"value": "stub/default", "label": "Stub Default"}]
        profiles = self._selectable_model_profiles()
        return [
            {
                "value": profile["profile_id"],
                "label": self._model_option_label(profile),
            }
            for profile in profiles
        ] or [{"value": "stub/default", "label": "Stub Default"}]

    def _model_route_options(self, *, lightweight: bool = False) -> list[dict[str, Any]]:
        if lightweight:
            return [
                {
                    "value": "stub/default",
                    "label": "Stub Default",
                    "provider_id": "stub",
                    "model_id": "default",
                    "local": True,
                }
            ]
        profiles = self._selectable_model_profiles()
        options: list[dict[str, Any]] = []
        for profile in profiles:
            profile_id = str(
                profile.get("profile_id")
                or profile.get("qualified_model_id")
                or profile.get("id")
                or ""
            ).strip()
            if not profile_id:
                continue
            provider_id = str(profile.get("provider_id") or profile.get("provider") or "").strip()
            model_id = str(profile.get("model_id") or profile.get("model") or "").strip()
            if not provider_id and "/" in profile_id:
                provider_id, inferred_model = profile_id.split("/", 1)
                model_id = model_id or inferred_model
            availability = _validated_dict(profile.get("availability"))
            configured = bool(
                availability.get("configured")
                or availability.get("active")
                or str(availability.get("status", "")).lower() in {"configured", "active"}
            )
            local = bool(
                profile.get("local")
                or availability.get("local")
                or availability.get("offline")
                or provider_id in {"stub", "ollama", "lmstudio", "vllm"}
            )
            requires_api_key = bool(
                provider_id
                and provider_id not in {"stub", "rumi"}
                and not local
                and not configured
            )
            options.append(
                {
                    "value": profile_id,
                    "label": self._model_option_label(profile),
                    "provider_id": provider_id,
                    "provider_display_name": str(
                        profile.get("provider_display_name") or provider_id
                    ),
                    "model_id": model_id,
                    "qualified_model_id": str(profile.get("qualified_model_id") or profile_id),
                    "configured": configured,
                    "local": local,
                    "requires_api_key": requires_api_key,
                    "api_key_required": requires_api_key,
                    "api_key_configured": configured,
                    "supports_vision": bool(profile.get("supports_vision")),
                    "supports_image_input": bool(
                        profile.get("supports_image_input")
                        or profile.get("supports_vision")
                    ),
                    "supports_tool_calling": bool(profile.get("supports_tool_calling")),
                    "supports_thinking": bool(profile.get("supports_thinking")),
                    "supports_fast": bool(profile.get("supports_fast")),
                    "speed_tier": str(profile.get("speed_tier") or ""),
                    "quality_tier": str(profile.get("quality_tier") or ""),
                    "cost_tier": str(profile.get("cost_tier") or ""),
                    "knowledge_level": profile.get("knowledge_level"),
                    "capability_tags": list(profile.get("capability_tags") or []),
                    "recommended_roles": list(profile.get("recommended_roles") or []),
                    "notes": str(profile.get("notes") or ""),
                }
            )
        return options or [{"value": "stub/default", "label": "Stub Default", "provider_id": "stub", "model_id": "default", "local": True}]

    def _selectable_model_profiles(self) -> list[dict[str, Any]]:
        cache_key = str(self._pack_root.resolve())
        now = time.monotonic()
        with self._selectable_model_profiles_lock:
            cached = self._selectable_model_profiles_cache.get(cache_key)
            if cached is not None and now - cached[0] < self._selectable_model_profiles_cache_ttl_seconds:
                return deepcopy(cached[1])

        list_profile_catalog_fn: Callable[[], list[dict[str, object]]] | None = None
        try:
            from ecosystem.defaultspack.backend.ai_client.provider_catalog import (
                list_profile_catalog as primary_catalog_loader,
            )
            list_profile_catalog_fn = primary_catalog_loader
        except ModuleNotFoundError:
            try:
                from backend.ai_client.provider_catalog import (
                    list_profile_catalog as fallback_catalog_loader,
                )
                list_profile_catalog_fn = fallback_catalog_loader
            except ModuleNotFoundError:
                pass

        with self._selectable_model_profiles_lock:
            now = time.monotonic()
            cached = self._selectable_model_profiles_cache.get(cache_key)
            if cached is not None and now - cached[0] < self._selectable_model_profiles_cache_ttl_seconds:
                return deepcopy(cached[1])
            if list_profile_catalog_fn is not None:
                try:
                    profiles = list_profile_catalog_fn()
                except Exception:
                    profiles = self._fallback_selectable_model_profiles()
            else:
                profiles = self._fallback_selectable_model_profiles()

            filtered = [profile for profile in profiles if self._is_user_selectable_profile(profile)]
            filtered.sort(key=self._model_profile_sort_key)
            self._selectable_model_profiles_cache[cache_key] = (time.monotonic(), deepcopy(filtered))
            return deepcopy(filtered)

    def _fallback_selectable_model_profiles(self) -> list[dict[str, Any]]:
        return [
            {
                "profile_id": model["id"],
                "display_name": model.get("name") or model["id"],
                "provider_id": model.get("provider_id") or model.get("provider"),
                "model_id": model.get("model_id") or str(model.get("id", "")).split("/", 1)[-1],
                "type": model.get("type", "chat"),
                "availability": model.get("availability", {}),
            }
            for model in self._list_provider_models()
        ]

    def _is_user_selectable_profile(self, profile: dict[str, Any]) -> bool:
        provider_id = str(profile.get("provider_id") or profile.get("provider") or "").strip()
        model_id = str(profile.get("model_id") or "").strip()
        model_type = str(profile.get("type") or "chat").strip().lower()
        availability = _validated_dict(profile.get("availability"))

        if model_type and model_type != "chat":
            return False
        if provider_id == "rumi":
            return False
        if provider_id == "stub":
            return model_id == "default"
        if profile.get("local") or availability.get("local") or availability.get("offline"):
            return True
        if self._is_unconfigured_direct_cloud_profile(provider_id, availability):
            return True
        return bool(
            availability.get("configured")
            or availability.get("active")
            or availability.get("status") in {"configured", "active"}
        )

    def _is_unconfigured_direct_cloud_profile(
        self,
        provider_id: str,
        availability: dict[str, Any],
    ) -> bool:
        """Expose every invokable catalog model as a setup target without enabling calls."""
        if not provider_id:
            return False
        if availability.get("configured") or availability.get("active"):
            return False
        if availability.get("catalog_only"):
            return False
        return bool(availability.get("supports_invoke"))

    def _model_profile_sort_key(self, profile: dict[str, Any]) -> tuple[int, int, str]:
        model_id = str(profile.get("model_id") or "").strip()
        availability = _validated_dict(profile.get("availability"))
        is_default = str(profile.get("profile_id") or "") == "stub/default"
        is_local = bool(profile.get("local") or availability.get("local") or availability.get("offline"))
        is_configured = bool(
            availability.get("configured")
            or availability.get("active")
            or str(availability.get("status", "")).lower() in {"configured", "active"}
        )
        provider_order = 0 if is_default else (1 if is_local else (2 if is_configured else 9))
        model_order = 0 if model_id == "default" else 20
        return (provider_order, model_order, str(profile.get("display_name") or profile.get("profile_id") or ""))

    def _model_option_label(self, profile: dict[str, Any]) -> str:
        provider = str(
            profile.get("provider_display_name")
            or profile.get("provider_id")
            or profile.get("provider")
            or ""
        ).strip()
        name = str(profile.get("display_name") or profile.get("profile_id") or "").strip()
        availability = _validated_dict(profile.get("availability"))
        provider_id = str(profile.get("provider_id") or profile.get("provider") or "").strip()
        requires_key = (
            not (profile.get("local") or availability.get("local") or availability.get("offline"))
            and provider_id not in {"stub", "rumi"}
            and not availability.get("configured")
        )
        suffix = " - API key required" if requires_key else ""
        return f"{provider} / {name}{suffix}" if provider else f"{name}{suffix}"

    def _list_provider_models(self) -> list[dict[str, Any]]:
        try:
            client = AIClient()
            return client.list_models()
        except Exception:
            return [{"id": "stub/default", "name": "stub/default"}]

    def _tool_settings_fields(self, ui: dict[str, Any]) -> list[dict[str, Any]]:
        fields = ui.get("settings_fields", [])
        if not isinstance(fields, list):
            return []
        return [field for field in fields if isinstance(field, dict)]

    def _tool_panel_actions(self, ui: dict[str, Any]) -> list[dict[str, Any]]:
        actions = ui.get("panel_actions", [])
        if not isinstance(actions, list):
            return []
        normalized: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("id") or "").strip()
            label = str(action.get("label") or "").strip()
            if not action_id or not label:
                continue
            item: dict[str, object] = {
                "id": action_id,
                "label": label,
            }
            for key in ("icon", "method", "endpoint", "preview_type", "requires_approval"):
                if key in action:
                    item[key] = action[key]
            if isinstance(action.get("payload"), dict):
                item["payload"] = dict(action["payload"])
            normalized.append(item)
        return normalized

    def _tool_capability_summary(self, tool: dict[str, Any]) -> str:
        parts: list[str] = []
        if tool.get("requires_approval"):
            approval_policy = str(tool.get("approval_policy") or "").strip()
            parts.append(
                f"Requires approval ({approval_policy})."
                if approval_policy
                else "Requires approval."
            )
        requires_model = [
            str(item).strip()
            for item in (tool.get("requires_model_capabilities") or [])
            if str(item or "").strip()
        ]
        if requires_model:
            parts.append(f"Model capabilities: {', '.join(requires_model)}.")
        requires_input = [
            str(item).strip()
            for item in (tool.get("requires_input_modalities") or [])
            if str(item or "").strip()
        ]
        if requires_input:
            parts.append(f"Input modalities: {', '.join(requires_input)}.")
        requires_runtime = [
            str(item).strip()
            for item in (tool.get("requires_runtime_capabilities") or [])
            if str(item or "").strip()
        ]
        if requires_runtime:
            parts.append(f"Runtime capabilities: {', '.join(requires_runtime)}.")
        capability_requirements = tool.get("capability_requirements")
        if isinstance(capability_requirements, dict):
            requires_all = [
                str(item).strip()
                for item in (capability_requirements.get("requires_all") or [])
                if str(item or "").strip()
            ]
            requires_any = [
                str(item).strip()
                for item in (capability_requirements.get("requires_any") or [])
                if str(item or "").strip()
            ]
            forbids = [
                str(item).strip()
                for item in (capability_requirements.get("forbids") or [])
                if str(item or "").strip()
            ]
            if requires_all:
                parts.append(f"Requires all: {', '.join(requires_all)}.")
            if requires_any:
                parts.append(f"Requires any: {', '.join(requires_any)}.")
            if forbids:
                parts.append(f"Forbids: {', '.join(forbids)}.")
        attachment_policy = str(tool.get("attachment_policy") or "").strip()
        if attachment_policy:
            parts.append(f"Attachment policy: {attachment_policy}.")
        supports_attachments = tool.get("supports_attachments")
        if isinstance(supports_attachments, bool):
            parts.append("Supports attachments." if supports_attachments else "Attachments disabled.")
        if not parts:
            return "No additional capability requirements."
        return " ".join(parts)

    def _tool_schema_summary(self, schema: dict[str, Any]) -> str:
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if not isinstance(properties, dict) or not properties:
            return "This tool does not declare runtime arguments."
        names = ", ".join(str(name) for name in properties.keys())
        return f"Runtime arguments: {names}."

    def _iso_to_ms(self, value: Any) -> int:
        if not value or not isinstance(value, str):
            return 0
        from datetime import datetime

        normalized = value.replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(normalized).timestamp() * 1000)
        except ValueError:
            return 0

    def _deep_merge(self, base: dict[str, object], patch: dict[str, object]) -> dict[str, object]:
        result = deepcopy(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                existing = result[key]
                if isinstance(existing, dict):
                    result[key] = self._deep_merge(existing, value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _sanitize_hex_color(value: Any, default: str) -> str:
        candidate = str(value or "").strip()
        if re.match(r"^#[0-9a-fA-F]{6}$", candidate):
            return candidate.upper()
        return default

    @staticmethod
    def _setting_bool(value: Any, default: bool) -> bool:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if not normalized:
                return default
            if normalized in {"false", "0", "off", "no", "n", "disable", "disabled"}:
                return False
            if normalized in {"true", "1", "on", "yes", "y", "enable", "enabled"}:
                return True
        return bool(value)

    @staticmethod
    def _clamped_float(value: Any, default: float, minimum: float, maximum: float) -> float:
        try:
            return max(minimum, min(maximum, float(value)))
        except (TypeError, ValueError):
            return default

    def _sanitize_settings_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        sanitized = deepcopy(patch)
        general = sanitized.get("general")
        if isinstance(general, dict):
            general.pop("settings_version", None)
            general.pop("keyboard_button_navigation_source", None)
        apis = sanitized.get("apis")
        if isinstance(apis, dict):
            legacy_model_routes = apis.pop("model_api_routes", None)
            if legacy_model_routes:
                models_patch = sanitized.setdefault("models", {})
                if isinstance(models_patch, dict) and not models_patch.get("model_api_routes"):
                    models_patch["model_api_routes"] = legacy_model_routes
            # Credential mutations require the signed approval flow exposed by
            # /api/ai/provider-key. Settings patches have no trusted approval
            # context, so they may never become a second secret-write path.
            apis.pop("api_keys", None)
            apis["api_keys"] = []
        external_output = sanitized.get("external_output")
        legacy_external_inputs = sanitized.get("external_inputs")
        token_container = external_output if isinstance(external_output, dict) else legacy_external_inputs
        if isinstance(token_container, dict):
            # External tokens are secrets and must only be changed through the
            # dedicated /api/external/tokens route, where standalone HTTP applies
            # local-origin, bearer-token, and CSRF checks.  Keep the settings
            # response shape stable, but treat any submitted token patch as
            # display-only data so /api/ui/settings cannot become a secret-write
            # bypass.
            token_container.pop("external_tokens", None)
            token_container["external_tokens"] = []
        if isinstance(legacy_external_inputs, dict) and "external_inputs" in sanitized:
            sanitized.pop("external_inputs", None)
        accounts_connections = sanitized.get("accounts_connections")
        if isinstance(accounts_connections, dict):
            accounts_connections.pop("providers", None)
            accounts_connections["providers"] = {}
        tools_mcp = sanitized.get("tools_mcp")
        if isinstance(tools_mcp, dict):
            tools_mcp.pop("codex_app_server", None)
        models = sanitized.get("models")
        if isinstance(models, dict):
            sanitized["models"] = ModelRuntimeSettingsService(
                self._pack_root
            ).sanitize_models_patch(models)
        return sanitized

    def _mark_explicit_keyboard_navigation_change(
        self,
        current: dict[str, Any],
        patch: dict[str, Any],
    ) -> None:
        general_patch = patch.get("general")
        if not isinstance(general_patch, dict):
            return
        current_general = current.get("general")
        current_version_value: object = (
            current_general.get("settings_version")
            if isinstance(current_general, dict)
            else _GENERAL_SETTINGS_VERSION
        )
        try:
            if not isinstance(current_version_value, (bool, int, float, str)):
                raise TypeError("settings_version must be numeric")
            current_version = int(current_version_value)
        except (TypeError, ValueError):
            current_version = _GENERAL_SETTINGS_VERSION
        general_patch["settings_version"] = max(
            _GENERAL_SETTINGS_VERSION,
            current_version,
        )
        if "keyboard_button_navigation" not in general_patch:
            return
        current_value = (
            current_general.get("keyboard_button_navigation")
            if isinstance(current_general, dict)
            else True
        )
        next_value = self._setting_bool(
            general_patch.get("keyboard_button_navigation"),
            True,
        )
        if next_value != self._setting_bool(current_value, True):
            general_patch["keyboard_button_navigation_source"] = (
                _KEYBOARD_NAVIGATION_SOURCE_USER
            )

    def _refresh_derived_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        refreshed = deepcopy(values)
        debug = refreshed.setdefault("debug", {})
        if not isinstance(debug, dict):
            debug = {}
            refreshed["debug"] = debug
        debug.setdefault("ai_request_logging", False)

        general = refreshed.setdefault("general", {})
        if not isinstance(general, dict):
            general = {}
            refreshed["general"] = general
        language = str(general.get("language") or "ja").strip().lower()
        general["language"] = language if language in {"ja", "en", "auto"} else "ja"
        try:
            general_settings_version = int(
                general.get("settings_version") or _GENERAL_SETTINGS_VERSION
            )
        except (TypeError, ValueError):
            general_settings_version = _GENERAL_SETTINGS_VERSION
        general["settings_version"] = max(
            _GENERAL_SETTINGS_VERSION,
            general_settings_version,
        )
        general["keyboard_button_navigation"] = self._setting_bool(
            general.get("keyboard_button_navigation"),
            True,
        )
        keyboard_navigation_source = str(
            general.get("keyboard_button_navigation_source") or ""
        ).strip()
        general["keyboard_button_navigation_source"] = (
            keyboard_navigation_source
            if keyboard_navigation_source
            in {
                _KEYBOARD_NAVIGATION_SOURCE_DEFAULT,
                _KEYBOARD_NAVIGATION_SOURCE_LEGACY_MIGRATION,
                _KEYBOARD_NAVIGATION_SOURCE_USER,
            }
            else _KEYBOARD_NAVIGATION_SOURCE_DEFAULT
        )
        general["spotlight_shortcut_enabled"] = self._setting_bool(
            general.get("spotlight_shortcut_enabled"),
            True,
        )
        shortcut = str(general.get("spotlight_shortcut") or "Ctrl+K").strip()
        general["spotlight_shortcut"] = shortcut or "Ctrl+K"
        general["spotlight_shortcut_text_input"] = self._setting_bool(
            general.get("spotlight_shortcut_text_input"),
            True,
        )
        general["manual_runtime_mode_selection"] = (
            general.get("manual_runtime_mode_selection") is True
        )

        tools = refreshed.setdefault("tools", {})
        if not isinstance(tools, dict):
            tools = {}
            refreshed["tools"] = tools
        try:
            settings_version = int(tools.get("settings_version") or 1)
        except (TypeError, ValueError):
            settings_version = 1
        legacy_tool_assist_mode = str(tools.get("tool_assist_mode") or "auto").strip().lower()
        if settings_version < 3:
            if "default_mode" not in tools:
                if legacy_tool_assist_mode in {"off", "manual"}:
                    tools["default_mode"] = "manual"
                else:
                    tools["default_mode"] = "auto"
            if "selection_strategy" not in tools:
                if legacy_tool_assist_mode == "vector":
                    tools["selection_strategy"] = "lexical"
                elif legacy_tool_assist_mode == "all_schemas":
                    tools["selection_strategy"] = "all_schemas"
                else:
                    tools["selection_strategy"] = "hybrid"
        tools["settings_version"] = 3
        tools["keep_selected_tools_after_send"] = (
            False
            if settings_version < 3
            else self._setting_bool(tools.get("keep_selected_tools_after_send"), False)
        )
        disabled_tool_ids = tools.get("disabled_tool_ids")
        hidden_tool_ids = tools.get("hidden_tool_ids")
        normalized_disabled_tool_ids = list(
            dict.fromkeys(
                str(item).strip()
                for item in (disabled_tool_ids if isinstance(disabled_tool_ids, list) else [])
                if str(item or "").strip()
            )
        )
        tool_permission_overrides = tools.get("tool_permission_overrides")
        if not isinstance(tool_permission_overrides, dict):
            tool_permission_overrides = {}
        if settings_version < 3:
            for tool_id in normalized_disabled_tool_ids:
                tool_permission_overrides.setdefault(tool_id, "block")
        tools["tool_permission_overrides"] = tool_permission_overrides
        tools["disabled_tool_ids"] = normalized_disabled_tool_ids
        tools["hidden_tool_ids"] = list(
            dict.fromkeys(
                str(item).strip()
                for item in (hidden_tool_ids if isinstance(hidden_tool_ids, list) else [])
                if str(item or "").strip()
            )
        )
        default_mode = str(tools.get("default_mode") or "auto").strip().lower()
        tools["default_mode"] = default_mode if default_mode in {"auto", "review", "manual", "none"} else "auto"
        selection_strategy = str(tools.get("selection_strategy") or "hybrid").strip().lower()
        tools["selection_strategy"] = selection_strategy if selection_strategy in {"hybrid", "semantic", "catalog_ai", "all_with_hints", "all_schemas", "lexical"} else "hybrid"
        semantic_backend = str(tools.get("semantic_backend") or "auto").strip().lower()
        tools["semantic_backend"] = semantic_backend if semantic_backend in {"auto", "embedding", "lexical"} else "auto"
        selector_trace = str(tools.get("selector_trace") or "summary").strip().lower()
        tools["selector_trace"] = selector_trace if selector_trace in {"none", "summary", "full"} else "summary"
        tools["embedding_model"] = str(tools.get("embedding_model") or "").strip()
        models_settings = refreshed.setdefault("models", {})
        if not isinstance(models_settings, dict):
            models_settings = {}
            refreshed["models"] = models_settings
        utility_models = models_settings.setdefault("utility_models", {})
        if not isinstance(utility_models, dict):
            utility_models = {}
            models_settings["utility_models"] = utility_models
        selector_alias = str(tools.get("selector_model") or "").strip()
        if selector_alias and not str(utility_models.get("tool_selector") or "").strip():
            utility_models["tool_selector"] = selector_alias
        tools.pop("selector_model", None)
        show_summary = self._setting_bool(tools.get("show_selected_tools_in_answer", tools.get("show_selection_summary")), True)
        show_reasons = self._setting_bool(tools.get("expand_selection_reasoning", tools.get("show_selection_reasons")), False)
        tools["show_selection_summary"] = show_summary
        tools["show_selection_reasons"] = show_reasons
        tools["show_selected_tools_in_answer"] = show_summary
        tools["expand_selection_reasoning"] = show_reasons
        standard_permissions = tools.get("standard_permissions")
        if not isinstance(standard_permissions, dict):
            standard_permissions = {}
        action_permissions = tools.get("action_permissions")
        if not isinstance(action_permissions, dict):
            action_permissions = {}
        for action, default in {
            "read": "auto",
            "search": "auto",
            "create": "confirm",
            "update": "confirm",
            "send": "confirm",
            "execute": "confirm",
            "computer": "confirm",
            "delete": "confirm",
        }.items():
            value = str(standard_permissions.get(action) or action_permissions.get(action) or default).strip().lower()
            if action == "delete" and value == "auto":
                value = "confirm"
            value = value if value in {"auto", "confirm", "block"} else default
            standard_permissions[action] = value
            action_permissions[action] = value
        tools["standard_permissions"] = standard_permissions
        tools["action_permissions"] = action_permissions
        service_permission_overrides = tools.get("service_permission_overrides")
        if not isinstance(service_permission_overrides, dict):
            service_permission_overrides = {}
        sanitized_service_overrides: dict[str, dict[str, str] | str] = {}
        for service_id, raw_override in service_permission_overrides.items():
            clean_service_id = str(service_id).strip()
            if not clean_service_id:
                continue
            if isinstance(raw_override, str):
                value = raw_override.strip().lower()
                if value in {"auto", "confirm", "block"}:
                    sanitized_service_overrides[clean_service_id] = value
                continue
            if not isinstance(raw_override, dict):
                continue
            clean_override: dict[str, str] = {}
            for action, raw_value in raw_override.items():
                clean_action = str(action).strip()
                value = str(raw_value or "").strip().lower()
                if clean_action == "delete" and value == "auto":
                    value = "confirm"
                if clean_action and value in {"auto", "confirm", "block"}:
                    clean_override[clean_action] = value
            if clean_override:
                sanitized_service_overrides[clean_service_id] = clean_override
        tools["service_permission_overrides"] = sanitized_service_overrides
        pinned_service_ids = tools.get("pinned_service_ids")
        if not isinstance(pinned_service_ids, list):
            pinned_service_ids = []
        tools["pinned_service_ids"] = list(dict.fromkeys(str(item).strip() for item in pinned_service_ids if str(item or "").strip()))
        tool_assist_mode = legacy_tool_assist_mode
        if settings_version < 2 and tool_assist_mode in {"all", "auto", "vector"}:
            tool_assist_mode = "auto"
        elif tool_assist_mode == "manual":
            tool_assist_mode = "off"
        elif tool_assist_mode == "all":
            tool_assist_mode = "auto"
        tools["tool_assist_mode"] = (
            tool_assist_mode if tool_assist_mode in {"auto", "vector", "off", "all_schemas"} else "auto"
        )
        try:
            tools["tool_assist_limit"] = max(1, min(24, int(tools.get("tool_assist_limit", 8))))
        except (TypeError, ValueError):
            tools["tool_assist_limit"] = 8
        try:
            tools["semantic_candidate_limit"] = max(8, min(64, int(tools.get("semantic_candidate_limit", 32))))
        except (TypeError, ValueError):
            tools["semantic_candidate_limit"] = 32
        try:
            tools["final_tool_limit"] = max(1, min(24, int(tools.get("final_tool_limit", tools.get("tool_assist_limit", 8)))))
        except (TypeError, ValueError):
            tools["final_tool_limit"] = 8
        try:
            tools["catalog_ai_direct_limit"] = max(20, min(200, int(tools.get("catalog_ai_direct_limit", 80))))
        except (TypeError, ValueError):
            tools["catalog_ai_direct_limit"] = 80
        legacy_default_target = self._legacy_default_target(refreshed)
        if "default_target" not in tools or (not str(tools.get("default_target") or "").strip() and legacy_default_target):
            tools["default_target"] = legacy_default_target

        haze = refreshed.setdefault("computer_use_haze", {})
        if not isinstance(haze, dict):
            haze = {}
            refreshed["computer_use_haze"] = haze
        haze["enabled"] = bool(haze.get("enabled", True))
        preset = str(haze.get("preset") or "aurora").strip().lower()
        haze["preset"] = preset if preset in {"aurora", "ocean", "ember", "custom"} else "aurora"
        haze["start_color"] = self._sanitize_hex_color(haze.get("start_color"), "#6EE7F9")
        haze["end_color"] = self._sanitize_hex_color(haze.get("end_color"), "#A78BFA")
        haze["accent_color"] = self._sanitize_hex_color(haze.get("accent_color"), "#F0ABFC")
        haze["opacity"] = self._clamped_float(haze.get("opacity"), 0.36, 0.05, 0.9)
        haze["edge_width"] = int(self._clamped_float(haze.get("edge_width"), 150, 40, 420))
        haze["animation_speed"] = self._clamped_float(haze.get("animation_speed"), 1, 0.1, 4)

        triggers = refreshed.setdefault("triggers", {})
        if not isinstance(triggers, dict):
            triggers = {}
            refreshed["triggers"] = triggers
        trigger_mode = str(triggers.get("mode") or "vector").strip().lower()
        triggers["mode"] = trigger_mode if trigger_mode in {"vector", "llm"} else "vector"
        triggers["filter_unrelated"] = bool(triggers.get("filter_unrelated", False))
        triggers["model"] = str(triggers.get("model") or "").strip()
        try:
            triggers["vector_threshold"] = max(0.0, min(1.0, float(triggers.get("vector_threshold", 0.1))))
        except (TypeError, ValueError):
            triggers["vector_threshold"] = 0.1

        calendar = refreshed.setdefault("calendar", {})
        if not isinstance(calendar, dict):
            calendar = {}
            refreshed["calendar"] = calendar
        calendar["quick_add_enabled"] = bool(calendar.get("quick_add_enabled", True))
        calendar["agent_current_chat"] = bool(calendar.get("agent_current_chat", False))
        calendar["agent_model"] = str(calendar.get("agent_model") or "").strip()
        calendar["agent_task_default"] = bool(calendar.get("agent_task_default", False))
        default_time = str(calendar.get("default_time") or "09:00").strip()
        calendar["default_time"] = default_time if re.match(r"^\d{1,2}:\d{2}$", default_time) else "09:00"
        try:
            slot_minutes = int(calendar.get("time_slot_minutes", 15))
        except (TypeError, ValueError):
            slot_minutes = 15
        calendar["time_slot_minutes"] = slot_minutes if slot_minutes in {15, 30, 60} else 15
        item_type = str(calendar.get("default_item_type") or "task").strip().lower()
        calendar["default_item_type"] = item_type if item_type in {"task", "event", "reminder"} else "task"
        week_start = str(calendar.get("week_start") or "sunday").strip().lower()
        calendar["week_start"] = week_start if week_start in {"sunday", "monday"} else "sunday"
        calendar["show_outside_days"] = bool(calendar.get("show_outside_days", True))
        calendar["show_time_picker"] = bool(calendar.get("show_time_picker", True))
        calendar["dim_weekends"] = bool(calendar.get("dim_weekends", True))
        task_color = str(calendar.get("task_color") or "blue").strip().lower()
        calendar["task_color"] = task_color if task_color in {"blue", "cyan", "slate"} else "blue"
        event_color = str(calendar.get("event_color") or "green").strip().lower()
        calendar["event_color"] = event_color if event_color in {"green", "blue", "slate"} else "green"
        try:
            calendar["max_items_per_day"] = max(1, min(6, int(calendar.get("max_items_per_day", 3))))
        except (TypeError, ValueError):
            calendar["max_items_per_day"] = 3

        models = refreshed.setdefault("models", {})
        if not isinstance(models, dict):
            models = {}
            refreshed["models"] = models

        sidebar = refreshed.setdefault("sidebar", {})
        if not isinstance(sidebar, dict):
            sidebar = {}
            refreshed["sidebar"] = sidebar
        sidebar["ui_placements"] = sidebar.get("ui_placements") if isinstance(sidebar.get("ui_placements"), list) else []

        apis = refreshed.setdefault("apis", {})
        if isinstance(apis, dict):
            apis["api_keys"] = provider_key_status(pack_root=self._pack_root)
            legacy_routes = apis.pop("model_api_routes", None)
            if legacy_routes and not models.get("model_api_routes"):
                models["model_api_routes"] = legacy_routes
        accounts_connections = refreshed.setdefault("accounts_connections", {})
        if not isinstance(accounts_connections, dict):
            accounts_connections = {}
            refreshed["accounts_connections"] = accounts_connections
        connection_providers = accounts_connections.setdefault("providers", {})
        if not isinstance(connection_providers, dict):
            connection_providers = {}
            accounts_connections["providers"] = connection_providers
        connection_providers.update(provider_oauth_statuses(pack_root=self._pack_root))
        connection_providers["codex"] = codex_connection_status(pack_root=self._pack_root)
        tools_mcp = refreshed.setdefault("tools_mcp", {})
        if not isinstance(tools_mcp, dict):
            tools_mcp = {}
            refreshed["tools_mcp"] = tools_mcp
        tools_mcp["codex_app_server"] = codex_app_server_status(pack_root=self._pack_root)
        refreshed["models"] = ModelRuntimeSettingsService(self._pack_root).refresh_models_settings(models)
        line = refreshed.setdefault("line", {})
        if not isinstance(line, dict):
            line = {}
            refreshed["line"] = line
        mention_policy = line.setdefault("mention_policy", {"group_room_mention_required": True})
        if isinstance(mention_policy, str):
            try:
                parsed = json.loads(mention_policy)
                if isinstance(parsed, dict):
                    line["mention_policy"] = parsed
            except Exception:
                pass
        external_input = refreshed.setdefault("external_input", {})
        if not isinstance(external_input, dict):
            external_input = {}
            refreshed["external_input"] = external_input
        external_output = refreshed.setdefault("external_output", {})
        if not isinstance(external_output, dict):
            external_output = {}
            refreshed["external_output"] = external_output
        external_custom = refreshed.setdefault("external_custom", {})
        if not isinstance(external_custom, dict):
            external_custom = {}
            refreshed["external_custom"] = external_custom
        endpoints = WebhookEndpointStore(
            self._pack_root / "user_data" / "shared" / "webhooks" / "endpoints.json"
        ).list_endpoints()
        input_profiles = InputProfileRegistry(self._pack_root).list_profiles()
        output_profiles = OutputProfileRegistry(self._pack_root).list_profiles()
        template_catalog = self._template_catalog_metadata()
        external_template_catalog = self._external_io_template_catalog(template_catalog)
        input_templates = _validated_dict_list(external_template_catalog.get("input"))
        output_templates = _validated_dict_list(external_template_catalog.get("output"))
        enabled_count = sum(1 for endpoint in endpoints if endpoint.get("enabled"))
        self._sync_external_input_selection(external_input, input_templates, endpoints=endpoints)
        self._sync_external_output_selection(external_output, output_templates)
        external_input.setdefault(
            "input_setup_guide",
            (
                "1. Providerを選ぶ\n"
                "2. Temporary Public URLでWebhook URLを発行する\n"
                "3. ProviderのWebhook URL欄へコピーする\n"
                "4. External Outputで必要なtokenを貼る\n"
                "5. 送信元を伝える / default応答を選ぶ"
            ),
        )
        external_input["endpoint_summary"] = f"{len(endpoints)} endpoints ({enabled_count} enabled)"
        external_input.setdefault("input_provider", "line")
        external_input.setdefault("input_template_id", "line.input.default")
        external_input.setdefault("input_profile_id", "line.default")
        external_input.setdefault("input_endpoint_id", f"{external_input.get('input_provider') or 'line'}-main")
        external_input.setdefault(
            "public_url_launcher",
            {
                "provider_id": "cloudflare_quick_tunnel",
                "local_url": "http://127.0.0.1:8766",
                "route_path": self._route_for_input_provider(str(external_input.get("input_provider") or "line"), input_templates),
            },
        )
        if isinstance(external_input.get("public_url_launcher"), dict):
            public_url_launcher = external_input["public_url_launcher"]
            public_url_launcher.setdefault("provider_id", "cloudflare_quick_tunnel")
            public_url_launcher.setdefault("local_url", "http://127.0.0.1:8766")
            public_url_launcher["route_path"] = self._route_for_input_provider(str(external_input.get("input_provider") or "line"), input_templates)
        external_input["provider_route_copy"] = self._provider_route_copy(input_templates)
        external_input["input_template_summary"] = self._template_summary(input_templates)
        external_input["input_profile_summary"] = ", ".join(profile.id for profile in input_profiles) or "No profiles"
        external_input.setdefault("include_source_context", True)
        external_input.setdefault("default_response_mode", "same_response")
        external_input.setdefault("input_response_preset", "same_source_reply")
        external_input.setdefault("policy_summary", "line.production: verified text only, saved source allowed, unknown source denied.")
        external_input["saved_sources_summary"] = self._external_sources_summary()
        external_output.setdefault(
            "output_setup_guide",
            (
                "LINE: Messaging API Channel Access Tokenで受信元へreply。push fallbackは既定OFF\n"
                "Discord Bot + Channel: Bot Tokenを保存し、Channel IDをTarget IDへ貼る\n"
                "Discord Webhook URL: Channel Webhook URLをExternal Tokensへ保存する\n"
                "Slack: Bot Tokenを保存し、Channel ID / Thread TSをTarget IDへ貼る\n"
                "Web/local: 外部投稿せず、chat historyやlocal保存に寄せる"
            ),
        )
        external_output["external_tokens"] = external_token_status(pack_root=self._pack_root)
        external_output.setdefault("output_provider", "line")
        external_output.setdefault("output_template_id", "line.output.default")
        external_output.setdefault("output_profile_id", "line.default")
        external_output.setdefault("output_send_mode", "reply_to_origin")
        if (
            str(external_output.get("output_provider") or "line") == "line"
            and str(external_output.get("output_send_mode") or "") in {"same_source_reply", "line_reply_or_push", "reply_or_push"}
        ):
            external_output["output_send_mode"] = "reply_to_origin"
        external_output.setdefault("output_target_id", "")
        external_output.setdefault("output_callback_token_id", "main")
        external_output["output_template_summary"] = self._template_summary(output_templates)
        external_output["output_profile_summary"] = ", ".join(profile.id for profile in output_profiles) or "No output profiles"
        external_output.setdefault("response_summary", "Prompt decisions create action plans; tools/adapters execute after policy checks.")
        external_output.setdefault("response_prompt_preset", "same_source_reply")
        external_output.setdefault("public_url_summary", "Providers: static, cloudflare_quick_tunnel")
        extension_paths = _validated_dict(external_template_catalog.get("extension_paths"))
        external_custom["custom_template_path"] = str(extension_paths.get("templates") or external_custom.get("custom_template_path") or "")
        external_custom["custom_profile_paths"] = ", ".join(
            item
            for item in [
                str(extension_paths.get("input_profiles") or ""),
                str(extension_paths.get("output_profiles") or ""),
            ]
            if item
        )
        external_custom.setdefault(
            "custom_prompt_examples",
            (
                "Google Chromeをcomputer_useで操作して起動し、"
                "https://chat.line.biz/U938c119aee3803767d500905c221a1f4/chat/C7d9e77e21e38512175c081f235f0aec8 "
                "にアクセスして返答して。"
            ),
        )
        refreshed.pop("external_inputs", None)
        models = refreshed.setdefault("models", {})
        if isinstance(models, dict):
            refreshed["models"] = ModelRuntimeSettingsService(
                self._pack_root
            ).refresh_models_settings(models)
        return refreshed

    def _input_profile_options(self) -> list[dict[str, str]]:
        profiles = InputProfileRegistry(self._pack_root).list_profiles()
        return [
            {
                "value": profile.id,
                "label": f"{profile.provider} / {profile.display_name}",
            }
            for profile in sorted(profiles, key=lambda item: (item.provider, item.id))
            if profile.id
        ] or [{"value": "line.default", "label": "line / LINE Default"}]

    def _output_profile_options(self) -> list[dict[str, str]]:
        profiles = OutputProfileRegistry(self._pack_root).list_profiles()
        return [
            {
                "value": profile.id,
                "label": f"{profile.provider} / {profile.display_name}",
            }
            for profile in sorted(profiles, key=lambda item: (item.provider, item.id))
            if profile.id
        ] or [{"value": "line.default", "label": "line / LINE Default"}]

    @staticmethod
    def _provider_options(templates: list[Any], *, fallback: list[str]) -> list[dict[str, str]]:
        providers: list[str] = []
        for item in templates:
            if not isinstance(item, dict):
                continue
            if item.get("origin") == "custom" or item.get("provider") == "custom":
                continue
            provider = str(item.get("provider") or "").strip()
            if provider and provider not in providers:
                providers.append(provider)
        if not providers:
            providers = list(fallback)
        return [{"value": provider, "label": provider} for provider in providers]

    @staticmethod
    def _template_options(templates: list[Any], *, include_custom: bool) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        for item in templates:
            if not isinstance(item, dict):
                continue
            if not include_custom and (item.get("origin") == "custom" or item.get("provider") == "custom"):
                continue
            template_id = str(item.get("id") or "").strip()
            if not template_id:
                continue
            provider = str(item.get("provider") or "").strip()
            display_name = str(item.get("display_name") or template_id).strip()
            options.append({"value": template_id, "label": f"{provider} / {display_name}" if provider else display_name})
        return options or [{"value": "", "label": "No templates"}]

    @staticmethod
    def _provider_route_copy(templates: list[Any] | None = None) -> str:
        lines: list[str] = []
        for item in templates or []:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").strip()
            route = FrontendRegistry._template_route(item)
            if provider and route:
                label = str(item.get("display_name") or item.get("id") or provider).strip()
                lines.append(f"{provider.upper()} {label}: {route}")
        if lines:
            return "\n".join(dict.fromkeys(lines))
        return "\n".join(
            [
                "LINE: /api/integrations/line/webhook",
                "Discord interactions: /api/integrations/discord/interactions",
                "Discord events: /api/integrations/discord/events",
                "Slack events: /api/integrations/slack/events",
                "Generic inbound: /api/webhooks/inbound/{webhook_id}",
            ]
        )

    @staticmethod
    def _template_summary(templates: list[Any]) -> str:
        providers: list[str] = []
        for item in templates:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "").strip()
            if provider and provider not in providers:
                providers.append(provider)
        return ", ".join(providers) if providers else "No templates"

    @staticmethod
    def _template_map(templates: list[Any]) -> dict[str, dict[str, Any]]:
        return {
            str(item.get("id") or "").strip(): item
            for item in templates
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        }

    @staticmethod
    def _first_template_for_provider(templates: list[Any], provider: str) -> dict[str, Any] | None:
        for item in templates:
            if not isinstance(item, dict):
                continue
            if item.get("origin") == "custom" or item.get("provider") == "custom":
                continue
            if str(item.get("provider") or "").strip() == provider:
                return item
        return None

    def _sync_external_input_selection(
        self,
        values: dict[str, Any],
        templates: list[Any],
        *,
        endpoints: list[dict[str, Any]] | None = None,
    ) -> None:
        template_by_id = self._template_map(templates)
        provider = str(values.get("input_provider") or "line").strip() or "line"
        endpoint_id = str(values.get("input_endpoint_id") or f"{provider}-main").strip()
        endpoint = next(
            (
                item
                for item in list(endpoints or [])
                if isinstance(item, dict) and str(item.get("id") or "").strip() == endpoint_id
            ),
            None,
        )
        endpoint_template = self._input_template_for_endpoint(endpoint, templates) if endpoint else None
        if endpoint is not None:
            endpoint_provider = str(endpoint.get("kind") or provider).strip() or provider
            values["input_provider"] = endpoint_provider
            values["input_endpoint_id"] = str(endpoint.get("id") or endpoint_id).strip() or endpoint_id
            endpoint_profile_id = str(endpoint.get("input_profile_id") or "").strip()
            if endpoint_profile_id:
                values["input_profile_id"] = endpoint_profile_id
            if endpoint_template:
                values["input_template_id"] = str(endpoint_template.get("id") or values.get("input_template_id") or "").strip()
            provider = values["input_provider"]
        template = template_by_id.get(str(values.get("input_template_id") or "").strip())
        if template is None or str(template.get("provider") or "").strip() != provider:
            template = endpoint_template or self._first_template_for_provider(templates, provider) or template or self._first_template_for_provider(templates, "line")
        if not template:
            return
        values["input_provider"] = str(template.get("provider") or provider).strip()
        values["input_template_id"] = str(template.get("id") or values.get("input_template_id") or "").strip()
        if endpoint is None and template.get("input_profile_id"):
            values["input_profile_id"] = str(template.get("input_profile_id"))
        if endpoint is None:
            template_endpoint = _validated_dict(template.get("endpoint"))
            if template_endpoint.get("id"):
                values["input_endpoint_id"] = str(template_endpoint.get("id"))

    def _sync_external_output_selection(self, values: dict[str, Any], templates: list[Any]) -> None:
        template_by_id = self._template_map(templates)
        provider = str(values.get("output_provider") or "line").strip() or "line"
        template = template_by_id.get(str(values.get("output_template_id") or "").strip())
        if template is None or str(template.get("provider") or "").strip() != provider:
            template = self._first_template_for_provider(templates, provider) or template or self._first_template_for_provider(templates, "line")
        if not template:
            return
        template_id = str(template.get("id") or values.get("output_template_id") or "").strip()
        values["output_provider"] = str(template.get("provider") or provider).strip()
        values["output_template_id"] = template_id
        if template.get("output_profile_id"):
            values["output_profile_id"] = str(template.get("output_profile_id"))
        mode = self._output_mode_for_template(template)
        if mode:
            values.setdefault("output_send_mode", mode)

    def _external_sources_summary(self) -> str:
        try:
            sources = ExternalSourceStore().list_sources()
        except Exception:
            sources = []
        if not sources:
            return "No saved sources"
        lines = []
        for source in sorted(sources, key=lambda item: (str(item.get("provider") or ""), str(item.get("source_type") or ""), str(item.get("source_id") or "")))[:20]:
            key = external_source_key(
                str(source.get("provider") or ""),
                str(source.get("source_type") or ""),
                str(source.get("source_id") or ""),
            )
            state = "push:on" if source.get("allow_push") else "push:off"
            enabled = "enabled" if source.get("enabled") else "disabled"
            lines.append(f"{key} ({enabled}, reply:on, {state})")
        if len(sources) > 20:
            lines.append(f"... and {len(sources) - 20} more")
        return "\n".join(lines)

    def _input_template_for_endpoint(self, endpoint: dict[str, Any] | None, templates: list[Any]) -> dict[str, Any] | None:
        if not isinstance(endpoint, dict):
            return None
        provider = str(endpoint.get("kind") or "").strip()
        input_profile_id = str(endpoint.get("input_profile_id") or "").strip()
        response = _validated_dict(endpoint.get("response"))
        response_mode = str(response.get("mode") or "").strip()
        candidates = [
            item
            for item in templates
            if isinstance(item, dict) and str(item.get("provider") or "").strip() == provider
        ]
        if not candidates:
            return None
        for item in candidates:
            if (
                input_profile_id
                and str(item.get("input_profile_id") or "").strip() == input_profile_id
                and str(_validated_dict(item.get("response")).get("mode") or "").strip()
                == response_mode
            ):
                return item
        for item in candidates:
            if input_profile_id and str(item.get("input_profile_id") or "").strip() == input_profile_id:
                return item
        if provider == "line" and response_mode == "computer_use_line_biz":
            return next((item for item in candidates if str(item.get("id") or "").strip() == "line.input.computer_use"), None)
        return None

    @staticmethod
    def _template_route(template: dict[str, Any]) -> str:
        endpoint = _validated_dict(template.get("endpoint"))
        route = str(endpoint.get("route") or "").strip()
        if route:
            return route
        routes = endpoint.get("routes")
        if isinstance(routes, list):
            return str(next((item for item in routes if str(item or "").strip()), "")).strip()
        return ""

    @staticmethod
    def _output_mode_for_template(template: dict[str, Any]) -> str:
        response = _validated_dict(template.get("response"))
        default_response = _validated_dict(template.get("default_response"))
        return str(
            template.get("output_send_mode")
            or template.get("send_mode")
            or response.get("mode")
            or default_response.get("mode")
            or ""
        ).strip()

    @staticmethod
    def _route_for_input_provider(provider: str, templates: list[Any] | None = None) -> str:
        provider = provider.strip().lower()
        for item in templates or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("provider") or "").strip().lower() != provider:
                continue
            route = FrontendRegistry._template_route(item)
            if route:
                return route
        if provider == "discord":
            return "/api/integrations/discord/interactions"
        if provider == "slack":
            return "/api/integrations/slack/events"
        if provider == "generic":
            return "/api/webhooks/inbound/{webhook_id}"
        return "/api/integrations/line/webhook"

    @staticmethod
    def _legacy_default_target(values: dict[str, Any]) -> str:
        for container_key in ("debug", "browser", "browser_use"):
            container = values.get(container_key)
            if not isinstance(container, dict):
                continue
            value = container.get("default_target")
            if value is not None:
                return str(value)
        value = values.get("default_target")
        return str(value) if value is not None else ""
