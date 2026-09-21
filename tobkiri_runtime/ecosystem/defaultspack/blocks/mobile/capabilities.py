"""blocks.mobile.capabilities — プロバイダー/モデル/プロファイル/テンプレート一括取得。

PCへ接続したスマホがプロバイダーカタログを自動取得するためのエンドポイント。
既存の provider_catalog / template gallery を集約して返す。

入力:
  {
    "include_templates": bool (optional, default true),
    "provider": str (optional — モデルを特定プロバイダーに絞る)
  }

出力:
  {
    "status": "ok",
    "data": {
      "providers": [...],
      "models": [...],
      "profiles": [...],
      "templates": [...]
    }
  }
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import ok
from ecosystem.defaultspack.backend.ai_client.provider_catalog import (
    list_model_catalog,
    list_profile_catalog,
    list_provider_catalog,
)
from ecosystem.defaultspack.domain.template.gallery import get_gallery
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
from domain.frontend.command_registry import SlashCommandRegistry
from domain.mobile.tools import mobile_agent_template, mobile_tool_records, mobile_tool_summary
from domain.tool.registry import ToolRegistry


def _provider_summary(provider: dict) -> dict:
    """モバイルへ送る最小限のプロバイダー情報。シークレットは含めない。"""
    return {
        "provider_id": str(provider.get("provider_id") or provider.get("id") or ""),
        "display_name": str(provider.get("display_name") or provider.get("provider_metadata", {}).get("display_name") or ""),
        "kind": str(provider.get("kind") or provider.get("category") or ""),
        "configured": bool(provider.get("configured")),
        "openai_compatible": bool(provider.get("openai_compatible")),
        "local": bool(provider.get("local")),
        "catalog_only": bool(provider.get("catalog_only")),
        "default_model": str(provider.get("default_model") or provider.get("provider_metadata", {}).get("default_model") or ""),
        "default_base_url": str(
            provider.get("default_base_url")
            or provider.get("metadata", {}).get("default_base_url")
            or provider.get("availability", {}).get("base_url_hint")
            or provider.get("provider_metadata", {}).get("default_base_url")
            or ""
        ),
        "default_model_for": dict(
            provider.get("default_model_for")
            if isinstance(provider.get("default_model_for"), dict)
            else provider.get("metadata", {}).get("default_model_for")
            if isinstance(provider.get("metadata", {}).get("default_model_for"), dict)
            else {}
        ),
        "capabilities": list(provider.get("capabilities") or []),
        "env_vars": list(provider.get("env_vars") or provider.get("provider_metadata", {}).get("env_vars") or []),
        "base_url_envs": list(provider.get("base_url_envs") or provider.get("provider_metadata", {}).get("base_url_envs") or []),
        "configured_api_count": int(provider.get("configured_api_count") or 0),
    }


def _model_summary(model: dict) -> dict:
    return {
        "id": str(model.get("id") or ""),
        "provider_id": str(model.get("provider_id") or ""),
        "model_id": str(model.get("model_id") or ""),
        "display_name": str(model.get("display_name") or ""),
        "type": str(model.get("type") or "chat"),
        "enabled": bool(model.get("enabled")),
        "max_context": int(model.get("max_context") or model.get("max_context_tokens") or -1),
        "supports_thinking": bool(model.get("supports_thinking")),
        "supports_vision": bool(model.get("supports_vision")),
        "supports_tool_calling": bool(model.get("supports_tool_calling")),
        "thinking_levels": list(model.get("thinking_levels") or []),
        "default_thinking_level": model.get("default_thinking_level"),
        "speed_tier": str(model.get("speed_tier") or "balanced"),
        "cost_tier": str(model.get("cost_tier") or "unknown"),
        "capability_tags": list(model.get("capability_tags") or []),
    }


def _profile_summary(profile: dict, settings: dict | None = None) -> dict:
    settings = settings if isinstance(settings, dict) else _runtime_settings()
    favorite_profiles = {
        str(item or "").strip()
        for item in settings.get("favorite_profiles", [])
        if str(item or "").strip()
    }
    profile_id = str(profile.get("profile_id") or profile.get("id") or "")
    qualified_model_id = str(profile.get("qualified_model_id") or profile_id)
    provider_id = str(profile.get("provider_id") or profile.get("provider") or "")
    model_id = str(profile.get("model_id") or profile.get("model") or "")
    availability = profile.get("availability") if isinstance(profile.get("availability"), dict) else {}
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
    local = bool(
        profile.get("local")
        or availability.get("local")
        or availability.get("offline")
        or provider_id in {"stub", "ollama", "lmstudio", "vllm"}
    )
    configured = bool(
        profile.get("configured")
        or availability.get("configured")
        or availability.get("active")
        or str(availability.get("status", "")).lower() in {"configured", "active"}
        or provider_id == "stub"
    )
    return {
        "profile_id": profile_id,
        "provider_id": provider_id,
        "model_id": model_id,
        "display_name": str(profile.get("display_name") or ""),
        "qualified_model_id": qualified_model_id,
        "provider_display_name": str(
            profile.get("provider_display_name")
            or profile.get("provider_name")
            or metadata.get("provider_display_name")
            or provider_id
            or ""
        ),
        "label": str(
            profile.get("disambiguated_name")
            or metadata.get("disambiguated_name")
            or profile.get("display_name")
            or model_id
            or profile_id
        ),
        "type": str(profile.get("type") or "chat"),
        "configured": configured,
        "local": local,
        "requires_api_key": bool(provider_id and provider_id not in {"stub", "rumi"} and not local and not configured),
        "favorite": profile_id in favorite_profiles or qualified_model_id in favorite_profiles,
        "max_context": int(profile.get("max_context") or profile.get("max_context_tokens") or -1),
        "supports_thinking": bool(profile.get("supports_thinking")),
        "supports_vision": bool(profile.get("supports_vision")),
        "supports_tool_calling": bool(profile.get("supports_tool_calling")),
        "thinking_levels": list(profile.get("thinking_levels") or []),
        "default_thinking_level": profile.get("default_thinking_level"),
        "speed_tier": str(profile.get("speed_tier") or "balanced"),
        "cost_tier": str(profile.get("cost_tier") or "unknown"),
        "capability_tags": list(profile.get("capability_tags") or []),
    }


def _template_summary(entry: dict) -> dict:
    return {
        "entry_id": str(entry.get("entry_id") or ""),
        "name": str(entry.get("name") or ""),
        "description": str(entry.get("description") or ""),
        "source_type": str(entry.get("source_type") or ""),
        "tags": list(entry.get("tags") or []),
        "updated_at": str(entry.get("updated_at") or ""),
    }


def _runtime_settings() -> dict:
    try:
        return ModelRuntimeSettingsService().get_settings()
    except Exception:
        return {}


def _runtime_summary(settings: dict | None = None) -> dict:
    settings = settings if isinstance(settings, dict) else _runtime_settings()
    return {
        "preferred_model": str(settings.get("preferred_model") or ""),
        "preferred_model_group": str(settings.get("preferred_model_group") or "default"),
        "thinking_level": str(settings.get("thinking_level") or "medium"),
        "deepthink_enabled": bool(settings.get("deepthink_enabled", False)),
        "favorite_profiles": [
            str(item)
            for item in settings.get("favorite_profiles", [])
            if str(item or "").strip()
        ],
        "auto_route_within_group": bool(settings.get("auto_route_within_group", True)),
    }


def _command_summary(command: dict) -> dict:
    return {
        "id": str(command.get("id") or ""),
        "name": str(command.get("name") or ""),
        "aliases": [str(item) for item in command.get("aliases", [])],
        "label": str(command.get("label") or command.get("name") or ""),
        "description": str(command.get("description") or ""),
        "category": str(command.get("category") or "chat"),
        "visibility": str(command.get("visibility") or "default"),
        "risk": str(command.get("risk") or "low"),
        "modes": [str(item) for item in command.get("modes", [])],
        "enabled": command.get("enabled", True) is not False,
        "active": bool(command.get("active", False)),
        "args": [
            {
                "name": str(arg.get("name") or ""),
                "type": str(arg.get("type") or "string"),
                "required": bool(arg.get("required", False)),
                "values": [str(value) for value in arg.get("values", [])],
            }
            for arg in command.get("args", [])
            if isinstance(arg, dict)
        ],
        "execution": dict(command.get("execution") if isinstance(command.get("execution"), dict) else {}),
    }


def _commands_payload() -> tuple[list[dict], list[dict]]:
    try:
        registry = SlashCommandRegistry()
        return [_command_summary(command) for command in registry.list_commands()], registry.manifest_errors()
    except Exception as exc:
        return [], [{"level": "error", "code": "command_catalog_failed", "message": str(exc)}]


def _merged_input(input_data: dict) -> dict:
    if not isinstance(input_data, dict):
        return {}
    merged: dict = {}
    for container_key in ("query_params", "params", "body", "query"):
        value = input_data.get(container_key)
        if isinstance(value, dict):
            merged.update(value)
    for key, value in input_data.items():
        if key in {"query_params", "params", "body", "query"}:
            continue
        merged[key] = value
    return merged


def _optional_bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def run(input_data, context):
    args = _merged_input(input_data)

    include_templates = _optional_bool(args.get("include_templates"), True)
    provider_filter = str(args.get("provider") or "")

    settings = _runtime_settings()
    providers = [_provider_summary(p) for p in list_provider_catalog()]
    models = [_model_summary(m) for m in list_model_catalog(provider=provider_filter)]
    profiles = [_profile_summary(p, settings) for p in list_profile_catalog()]
    commands, command_errors = _commands_payload()
    try:
        tools = ToolRegistry().list_tools()
        tool_records = mobile_tool_records(tools, context=context if isinstance(context, dict) else {})
    except Exception:
        tool_records = []

    templates: list[dict] = []
    if include_templates:
        try:
            gallery = get_gallery()
            templates = [_template_summary(e) for e in gallery.list_entries()]
        except Exception:
            templates = []

    return ok(
        {
            "providers": providers,
            "models": models,
            "profiles": profiles,
            "templates": templates,
            "runtime": _runtime_summary(settings),
            "commands": commands,
            "command_manifest_errors": command_errors,
            "agent_template": mobile_agent_template(),
            "tools": tool_records,
            "tool_summary": mobile_tool_summary(tool_records),
        }
    )
