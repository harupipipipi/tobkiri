"""Pack-aggregated provider and model catalog loader.

defaultspack owns the loader only. Concrete provider/model entries live in
installed packs such as rumi_model_catalog_pack under extensions/llm/providers.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from ecosystem.defaultspack.domain.ai_client.providers import (
    get_all_known_models,
    get_provider_catalog,
    validate_provider_catalog_coverage,
)
from ecosystem.defaultspack.domain.ai_client.api_key_store import provider_named_api_keys
from ecosystem.defaultspack.domain.ai_client.model_capabilities import (
    flatten_capability_fields,
)
from ecosystem.defaultspack.domain.ai_client.model_capability_schema import (
    knowledge_band_for_level,
)
from ecosystem.defaultspack.domain.ai_client.model_metadata_schema import context_window_value


def _runtime_client():
    """Return the live client without making the static catalog depend on startup order."""
    from ecosystem.defaultspack.domain.ai_client.client import AIClient

    return AIClient()


def _active_provider_ids() -> set[str]:
    try:
        return {
            str(provider.get("provider_id") or provider.get("id") or "").strip()
            for provider in _runtime_client().list_providers()
            if isinstance(provider, dict)
            and str(provider.get("provider_id") or provider.get("id") or "").strip()
        }
    except Exception:
        return set()


def list_provider_catalog() -> List[Dict[str, Any]]:
    """List every installed provider, marking credential-backed ones as active.

    The provider registry is deliberately broader than the active runtime.  Passing
    the active ids through preserves that complete setup catalog while ensuring a
    just-configured provider is immediately shown as available.
    """
    active_provider_ids = _active_provider_ids()
    return [
        _with_legacy_provider_fields(provider)
        for provider in get_provider_catalog(active_provider_ids=active_provider_ids)
    ]


def list_model_catalog(provider: str = "") -> List[Dict[str, Any]]:
    """Return static metadata plus every model discoverable by active providers.

    Gateway providers (notably OpenRouter) expose their live inventory at runtime.
    Previously this endpoint discarded that inventory and returned only the small
    curated overlay, leaving valid account models absent from the desktop UI.
    """
    active_provider_ids = _active_provider_ids()
    models = get_all_known_models(
        provider_id=provider or None,
        active_provider_ids=active_provider_ids,
    )
    try:
        runtime_models = _runtime_client().list_models(provider=provider or None)
    except Exception:
        runtime_models = []

    # A successful live inventory is authoritative for that connection.  Do
    # not append an old bundled overlay merely because it contains different
    # ids: that makes removed/unavailable models reappear in the UI and defeats
    # account-scoped discovery.
    live_inventory_sources = {
        "remote_models_endpoint",
        "openrouter_models_api",
        "vercel_gateway_models_api",
        "vercel_ai_gateway_models_api",
        "native_models_endpoint",
        "native_server_api",
        "last_known_good_inventory",
    }
    providers_with_live_inventory = {
        str(model.get("provider_id") or model.get("provider") or "").strip()
        for model in runtime_models
        if isinstance(model, dict)
        and str((model.get("metadata") or {}).get("source") or "").strip().lower()
        in live_inventory_sources
    }

    merged: dict[str, Dict[str, Any]] = {}
    order: list[str] = []
    for source in (models, runtime_models):
        for raw in source:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            provider_id = str(item.get("provider_id") or item.get("provider") or "").strip()
            model_id = str(item.get("model_id") or item.get("model_name") or "").strip()
            qualified_id = str(item.get("qualified_model_id") or item.get("id") or "").strip()
            if not qualified_id and provider_id and model_id:
                qualified_id = f"{provider_id}/{model_id}"
            if not qualified_id:
                continue
            if provider and provider_id != provider:
                continue
            if source is models and provider_id in providers_with_live_inventory:
                continue
            if qualified_id not in merged:
                merged[qualified_id] = item
                order.append(qualified_id)
                continue

            # Runtime discovery is authoritative for capability/pricing metadata,
            # while curated metadata remains a fallback for omitted fields.
            existing = merged[qualified_id]
            metadata = dict(existing.get("metadata") or {})
            metadata.update(dict(item.get("metadata") or {}))
            existing.update({key: value for key, value in item.items() if value not in (None, "", [], {})})
            existing["metadata"] = metadata

    return [_with_legacy_model_fields(merged[qualified_id]) for qualified_id in order]


def list_profile_catalog() -> List[Dict[str, Any]]:
    # Build profiles from the same merged catalog used by /api/ai/models.  This
    # keeps the composer, Settings, and API routes in agreement after a provider
    # key is saved instead of exposing live models in only one endpoint.
    profiles: list[Dict[str, Any]] = []
    for model in list_model_catalog():
        metadata = dict(model.get("metadata") or {})
        metadata.update(
            {
                "profile_source": "catalog",
                "resolved_model_key": model.get("qualified_model_id") or model.get("id") or "",
            }
        )
        profiles.append(
            {
                "id": model.get("qualified_model_id") or model.get("id") or "",
                "profile_id": model.get("qualified_model_id") or model.get("id") or "",
                "name": model.get("display_name") or model.get("name") or "",
                "display_name": model.get("display_name") or model.get("name") or "",
                "provider": model.get("provider_id") or model.get("provider") or "",
                "provider_id": model.get("provider_id") or model.get("provider") or "",
                "provider_display_name": model.get("provider_display_name") or "",
                "model": model.get("model_id") or model.get("model_name") or "",
                "model_id": model.get("model_id") or model.get("model_name") or "",
                "model_name": model.get("model_name") or model.get("model_id") or "",
                "qualified_model_id": model.get("qualified_model_id") or model.get("id") or "",
                "availability": dict(model.get("availability") or {}),
                "name_collision": bool(model.get("name_collision")),
                "provider_count_for_model_name": int(model.get("provider_count_for_model_name") or 0),
                "disambiguated_name": model.get("disambiguated_name") or model.get("display_name") or "",
                "type": model.get("type") or "chat",
                "context_window": int(model.get("context_window") or 0),
                "capabilities": list(model.get("capabilities") or []),
                "request_features": dict(model.get("request_features") or {}),
                "routing": dict(model.get("routing") or {}),
                "thinking": dict(model.get("thinking") or {}),
                "defaults": dict(model.get("defaults") or {}),
                "pricing": dict(model.get("pricing") or {}),
                "metadata": metadata,
                "supports_thinking": bool(model.get("supports_thinking")),
                "thinking_levels": list(model.get("thinking_levels") or []),
                "default_thinking_level": model.get("default_thinking_level"),
            }
        )
    return [_with_legacy_profile_fields(profile) for profile in profiles]


def validate_catalog_coverage() -> List[Dict[str, Any]]:
    return validate_provider_catalog_coverage()


def _with_legacy_provider_fields(provider: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(provider)
    availability = dict(item.get("availability", {}))
    configuration_source = availability.get("configuration_source")
    env_source = next((env_name for env_name in item.get("env_vars", []) if os.environ.get(str(env_name))), None)
    if env_source:
        configuration_source = env_source
    configured_envs = [configuration_source] if configuration_source else []
    capabilities = set(item.get("capabilities", []))
    kind = str(item.get("kind") or "")
    metadata = dict(item.get("metadata", {}))
    default_model_for = item.get("default_model_for")
    if isinstance(default_model_for, dict):
        item["default_model_for"] = {str(key): str(value) for key, value in default_model_for.items()}
        metadata["default_model_for"] = dict(item["default_model_for"])
    item.setdefault("category", kind)
    item["configured"] = bool(availability.get("configured"))
    item["configured_envs"] = configured_envs
    item["local"] = kind == "local" or "local" in capabilities
    item["openai_compatible"] = "openai_compatible" in capabilities or bool(metadata.get("openai_compatible"))
    item["catalog_only"] = bool(metadata.get("catalog_only", availability.get("catalog_only", False)))
    item["supports_invoke"] = bool(metadata.get("supports_invoke", availability.get("supports_invoke", False)))
    named_apis = provider_named_api_keys(str(item.get("provider_id") or item.get("id") or ""))
    item["configured_api_count"] = len([api for api in named_apis if api.get("configured")])
    item["named_apis"] = named_apis
    return item


def _canonical_model_id(model: Dict[str, Any]) -> str:
    return str(model.get("canonical_model_id") or model.get("model_id") or model.get("model_name") or model.get("id") or "")


def _model_context(model: Dict[str, Any]) -> int:
    qualified = str(model.get("qualified_model_id") or model.get("id") or "")
    if qualified == "stub/default":
        return -1
    try:
        return context_window_value(model, default=-1)
    except Exception:
        return -1


def _supports_thinking(model: Dict[str, Any]) -> bool:
    model_type = str(model.get("type") or "chat").lower()
    model_id = str(model.get("model_id") or model.get("id") or "").lower()
    if model_type not in {"chat", "reasoning"}:
        return False
    if model.get("supports_thinking") is not None:
        return bool(model.get("supports_thinking"))
    if bool(model.get("supports_thinking")):
        return True
    return any(token in model_id for token in ("gpt-5", "claude", "gemini", "deepseek"))


def _capability_enrichment(model: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return flatten_capability_fields(model)
    except Exception:
        supports_thinking = _supports_thinking(model)
        return {
            "supports_vision": False,
            "supports_image_input": False,
            "supports_audio": False,
            "supports_audio_input": False,
            "supports_tool_calling": False,
            "supports_fast": False,
            "supports_thinking": supports_thinking,
            "thinking_levels": ["low", "medium", "high", "xhigh"] if supports_thinking else [],
            "default_thinking_level": "medium" if supports_thinking else None,
            "speed_tier": "balanced",
            "quality_tier": "unknown",
            "knowledge_level": 0,
            "knowledge_band": knowledge_band_for_level(0),
            "cost_tier": "unknown",
            "latency_tier": "medium",
            "capability_tags": ["thinking"] if supports_thinking else [],
            "allowed_roles": ["primary_chat"],
            "recommended_roles": ["primary_chat"],
            "model_capabilities": {},
        }


def _supports_vision(model: Dict[str, Any]) -> bool:
    return bool(_capability_enrichment(model).get("supports_vision"))


def _supports_tool_calling(model: Dict[str, Any]) -> bool:
    return bool(_capability_enrichment(model).get("supports_tool_calling"))


def _supports_fast_mode(model: Dict[str, Any]) -> bool:
    return bool(_capability_enrichment(model).get("supports_fast"))


def _knowledge_level(model: Dict[str, Any]) -> int:
    try:
        return int(_capability_enrichment(model).get("knowledge_level") or 0)
    except (TypeError, ValueError):
        return 0


def _speed_tier(model: Dict[str, Any]) -> str:
    return str(_capability_enrichment(model).get("speed_tier") or "balanced")


def _capability_tags(model: Dict[str, Any]) -> list[str]:
    tags = _capability_enrichment(model).get("capability_tags")
    return [str(tag) for tag in tags] if isinstance(tags, list) else []


def _with_legacy_model_fields(model: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(model)
    canonical = _canonical_model_id(item)
    max_context = _model_context(item)
    capability_fields = _capability_enrichment(item)
    supports_thinking = bool(capability_fields.get("supports_thinking", _supports_thinking(item)))
    defaults = dict(item.get("defaults", {})) if isinstance(item.get("defaults"), dict) else {}
    pricing = dict(item.get("pricing", {})) if isinstance(item.get("pricing"), dict) else {}
    thinking_levels = capability_fields.get("thinking_levels", item.get("thinking_levels"))
    if not isinstance(thinking_levels, list):
        thinking_levels = ["low", "medium", "high", "xhigh"] if supports_thinking else []
    item["canonical_model_id"] = canonical
    item["same_model_across_providers_key"] = str(item.get("same_model_across_providers_key") or canonical)
    item["max_context"] = max_context
    item["max_context_tokens"] = max_context
    item["supports_thinking"] = supports_thinking
    item["thinking_levels"] = thinking_levels
    item["default_thinking_level"] = capability_fields.get(
        "default_thinking_level",
        item.get("default_thinking_level", "medium" if supports_thinking else None),
    )
    item["supports_vision"] = bool(capability_fields.get("supports_vision"))
    item["supports_image_input"] = bool(capability_fields.get("supports_image_input"))
    item["supports_audio"] = bool(capability_fields.get("supports_audio"))
    item["supports_audio_input"] = bool(capability_fields.get("supports_audio_input") or capability_fields.get("supports_audio"))
    item["supports_tool_calling"] = bool(capability_fields.get("supports_tool_calling"))
    item["supports_fast"] = bool(capability_fields.get("supports_fast"))
    item["speed_tier"] = str(capability_fields.get("speed_tier") or "balanced")
    item["quality_tier"] = str(capability_fields.get("quality_tier") or "unknown")
    item["knowledge_level"] = int(capability_fields.get("knowledge_level") or 0)
    item["knowledge_band"] = str(capability_fields.get("knowledge_band") or knowledge_band_for_level(item["knowledge_level"]))
    item["cost_tier"] = str(capability_fields.get("cost_tier") or "unknown")
    item["latency_tier"] = str(capability_fields.get("latency_tier") or "medium")
    item["capability_tags"] = list(capability_fields.get("capability_tags") or [])
    item["allowed_roles"] = list(capability_fields.get("allowed_roles") or [])
    item["recommended_roles"] = list(capability_fields.get("recommended_roles") or [])
    item["model_capabilities"] = dict(capability_fields.get("model_capabilities") or {})
    item["defaults"] = defaults
    item["pricing"] = pricing
    metadata = dict(item.get("metadata", {}))
    metadata.update(
        {
            "max_context": max_context,
            "supports_thinking": supports_thinking,
            "thinking_levels": thinking_levels,
            "supports_vision": item["supports_vision"],
            "supports_image_input": item["supports_image_input"],
            "supports_audio": item["supports_audio"],
            "supports_audio_input": item["supports_audio_input"],
            "supports_tool_calling": item["supports_tool_calling"],
            "supports_fast": item["supports_fast"],
            "speed_tier": item["speed_tier"],
            "quality_tier": item["quality_tier"],
            "knowledge_level": item["knowledge_level"],
            "knowledge_band": item["knowledge_band"],
            "cost_tier": item["cost_tier"],
            "latency_tier": item["latency_tier"],
            "capability_tags": item["capability_tags"],
            "allowed_roles": item["allowed_roles"],
            "recommended_roles": item["recommended_roles"],
            "model_capabilities": item["model_capabilities"],
            "defaults": defaults,
            "pricing": pricing,
            "routing": dict(item.get("routing", {})) if isinstance(item.get("routing"), dict) else {},
            "request_features": dict(item.get("request_features", {})) if isinstance(item.get("request_features"), dict) else {},
            "thinking": dict(item.get("thinking", {})) if isinstance(item.get("thinking"), dict) else {},
        }
    )
    item["metadata"] = metadata
    return item


def _with_legacy_profile_fields(profile: Dict[str, Any]) -> Dict[str, Any]:
    item = dict(profile)
    model_like = {
        "id": item.get("qualified_model_id") or item.get("profile_id"),
        "model_id": item.get("model_id"),
        "type": item.get("type", "chat"),
        "context_window": item.get("context_window", item.get("max_context", 0)),
        "supports_thinking": item.get("supports_thinking"),
        "thinking_levels": item.get("thinking_levels"),
        "default_thinking_level": item.get("default_thinking_level"),
        "metadata": item.get("metadata", {}),
        "defaults": item.get("defaults", {}),
        "routing": item.get("routing", {}),
        "request_features": item.get("request_features", {}),
        "thinking": item.get("thinking", {}),
        "pricing": item.get("pricing", {}),
        "capabilities": item.get("capabilities", []),
        "supports_vision": item.get("supports_vision"),
        "supports_image_input": item.get("supports_image_input"),
        "supports_audio": item.get("supports_audio"),
        "supports_audio_input": item.get("supports_audio_input"),
        "supports_tool_calling": item.get("supports_tool_calling"),
        "supports_fast": item.get("supports_fast"),
        "capability_tags": item.get("capability_tags"),
        "tags": item.get("tags"),
        "traits": item.get("traits"),
        "input_modalities": item.get("input_modalities"),
        "modalities": item.get("modalities"),
        "speed_tier": item.get("speed_tier"),
        "quality_tier": item.get("quality_tier"),
        "knowledge_level": item.get("knowledge_level"),
        "knowledge_band": item.get("knowledge_band"),
        "cost_tier": item.get("cost_tier"),
        "model_roles": item.get("model_roles"),
    }
    enriched = _with_legacy_model_fields(model_like)
    item["max_context"] = enriched["max_context"]
    item["max_context_tokens"] = enriched["max_context_tokens"]
    item["supports_thinking"] = enriched["supports_thinking"]
    item["thinking_levels"] = enriched["thinking_levels"]
    item["default_thinking_level"] = enriched["default_thinking_level"]
    item["supports_vision"] = enriched["supports_vision"]
    item["supports_image_input"] = enriched["supports_image_input"]
    item["supports_audio"] = enriched["supports_audio"]
    item["supports_audio_input"] = enriched["supports_audio_input"]
    item["supports_tool_calling"] = enriched["supports_tool_calling"]
    item["supports_fast"] = enriched["supports_fast"]
    item["speed_tier"] = enriched["speed_tier"]
    item["quality_tier"] = enriched["quality_tier"]
    item["knowledge_level"] = enriched["knowledge_level"]
    item["knowledge_band"] = enriched["knowledge_band"]
    item["cost_tier"] = enriched["cost_tier"]
    item["latency_tier"] = enriched["latency_tier"]
    item["capability_tags"] = enriched["capability_tags"]
    item["allowed_roles"] = enriched["allowed_roles"]
    item["recommended_roles"] = enriched["recommended_roles"]
    item["model_capabilities"] = enriched["model_capabilities"]
    item["defaults"] = enriched.get("defaults", {})
    item["pricing"] = enriched.get("pricing", {})
    item["same_model_across_providers_key"] = str(
        item.get("same_model_across_providers_key")
        or item.get("canonical_model_id")
        or item.get("model_id")
        or item.get("model_name")
        or ""
    )
    metadata = dict(item.get("metadata", {}))
    metadata.update(enriched["metadata"])
    item["metadata"] = metadata
    return item
