"""Finite legacy projection over selected AI catalog and registry owners.

defaultspack owns no catalog or connection state. The active resolved profile
selects the global owners; this module only preserves legacy response fields.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    GlobalContractInvocationError,
    GlobalContractUnavailable,
    invoke_global_contract,
)
from ecosystem.defaultspack.domain.ai_client.model_capabilities import (
    flatten_capability_fields,
)
from ecosystem.defaultspack.domain.ai_client.model_capability_schema import (
    knowledge_band_for_level,
)
from ecosystem.defaultspack.domain.ai_client.model_metadata_schema import context_window_value
from ecosystem.defaultspack.domain.ai_client.thinking_control import (
    normalize_thinking_control,
)
from ecosystem.defaultspack.domain.ai_client.providers import (
    get_all_known_models,
    get_provider_catalog,
)

_MODEL_CATALOG_CONTRACT = "rumi.resource.ai.model.catalog.v1"
_MODEL_PROFILE_CONTRACT = "rumi.resource.ai.model.profile.v1"
_PROVIDER_REGISTRY_CONTRACT = "rumi.resource.ai.provider.registry.v1"

_LIVE_INVENTORY_SOURCES = {
    "remote_models_endpoint",
    "openrouter_models_api",
    "vercel_gateway_models_api",
    "vercel_ai_gateway_models_api",
    "native_models_endpoint",
    "native_server_api",
    "last_known_good_inventory",
}

_RUNTIME_INVENTORY_CACHE_TTL_SECONDS = 5.0
_runtime_inventory_cache_lock = threading.RLock()
_runtime_inventory_cache: dict[
    str, tuple[float, Any, tuple[tuple[str, int], ...], list[Dict[str, Any]]]
] = {}
_runtime_inventory_state = threading.local()


def _runtime_provider_fingerprint(client: Any) -> tuple[tuple[str, int], ...]:
    providers = getattr(client, "_providers", None)
    if not isinstance(providers, dict):
        return ()
    return tuple(
        sorted(
            (str(provider_id), id(provider))
            for provider_id, provider in providers.items()
        )
    )


def _clear_runtime_inventory_cache() -> None:
    """Clear the short-lived runtime inventory cache after provider changes."""
    with _runtime_inventory_cache_lock:
        _runtime_inventory_cache.clear()


def _active_runtime_inventory_providers() -> set[str]:
    active = getattr(_runtime_inventory_state, "active_providers", None)
    if active is None:
        active = set()
        _runtime_inventory_state.active_providers = active
    return active


def _runtime_client():
    """Return the live provider client without coupling module import to startup."""
    from ecosystem.defaultspack.domain.ai_client.client import AIClient

    return AIClient()


def _invoke(contract_id: str, operation: str, payload: Dict[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise GlobalContractUnavailable("interface registry is unavailable")
    return invoke_global_contract(registry, contract_id, operation, payload)


def list_provider_catalog() -> List[Dict[str, Any]]:
    try:
        catalog = _invoke(_MODEL_CATALOG_CONTRACT, "list", {})
        connections = _invoke(_PROVIDER_REGISTRY_CONTRACT, "list", {})
    except (GlobalContractInvocationError, GlobalContractUnavailable):
        return _selected_provider_fallback()
    providers = catalog.get("providers") if isinstance(catalog, dict) else None
    providers = providers if isinstance(providers, list) else []
    connection_items = connections.get("providers") if isinstance(connections, dict) else None
    connection_items = connection_items if isinstance(connection_items, list) else []
    if not providers:
        return _selected_provider_fallback()
    configured = {
        str(item.get("provider_instance_id") or "")
        for item in connection_items
        if isinstance(item, dict) and item.get("enabled", True)
    }
    return [
        _with_legacy_provider_fields(
            provider,
            configured=f"provider.{provider.get('provider_id')}" in configured,
        )
        for provider in providers
        if isinstance(provider, dict)
    ]


def _selected_provider_fallback() -> List[Dict[str, Any]]:
    """Expose the selected bundled registry during contract restoration."""
    try:
        from core_runtime.resolved_profile_scope import effective_pack_ids

        selected_pack_ids = set(effective_pack_ids())
        if selected_pack_ids and "rumi_model_catalog_pack" not in selected_pack_ids:
            return []
        runtime_providers = _runtime_client().list_providers()
        active_provider_ids = {
            str(item.get("provider_id") or item.get("id") or "").strip()
            for item in runtime_providers
            if isinstance(item, dict)
            and str(item.get("provider_id") or item.get("id") or "").strip()
        }
        providers = get_provider_catalog(active_provider_ids=active_provider_ids)
    except Exception:
        return []
    return [
        _with_legacy_provider_fields(
            item,
            configured=str(item.get("provider_id") or "") in active_provider_ids,
        )
        for item in providers
        if isinstance(item, dict)
    ]


def list_model_catalog(provider: str = "") -> List[Dict[str, Any]]:
    try:
        result = _invoke(
            _MODEL_CATALOG_CONTRACT,
            "list",
            {"provider_id": provider} if provider else {},
        )
    except (GlobalContractInvocationError, GlobalContractUnavailable):
        return _selected_catalog_fallback(provider)
    models = result.get("models") if isinstance(result, dict) else None
    models = models if isinstance(models, list) else []
    if not models:
        return _selected_catalog_fallback(provider)
    return [
        _with_legacy_model_fields(model) for model in _merge_runtime_inventory(models, provider)
    ]


def _selected_catalog_fallback(provider: str) -> List[Dict[str, Any]]:
    """Project the bundled catalog only when its owning Pack is active.

    The launcher can serve its first request while global-contract handlers are
    still being restored after a profile restart.  Returning an empty picker
    in that short (or failed-restoration) window hides valid, approved models.
    This is deliberately limited to the selected model-catalog Pack and uses
    its declarative resource; it never enables an unselected provider.
    """
    runtime_only = _merge_runtime_inventory([], provider)
    try:
        from core_runtime.resolved_profile_scope import effective_pack_ids

        selected_pack_ids = set(effective_pack_ids())
        if selected_pack_ids and "rumi_model_catalog_pack" not in selected_pack_ids:
            return [
                _with_legacy_model_fields(model)
                for model in runtime_only
                if isinstance(model, dict)
            ]
        models = get_all_known_models(provider_id=provider or None)
        models = _merge_selected_openrouter_inventory(models, provider)
        models = _merge_runtime_inventory(models, provider)
    except Exception:
        models = runtime_only
    return [_with_legacy_model_fields(model) for model in models if isinstance(model, dict)]


def _merge_runtime_inventory(
    models: List[Dict[str, Any]],
    provider: str,
) -> List[Dict[str, Any]]:
    """Merge runtime discovery, treating a successful live inventory as final.

    A provider's live account inventory replaces its bundled overlay instead of
    being appended to it.  This prevents expired aliases (for example removed
    OpenRouter ``:free`` variants) from returning to the picker after a
    successful refresh.  Providers without live discovery retain their selected
    catalog entries, and runtime metadata wins for duplicate model ids.
    """
    normalized_provider = str(provider or "").strip()
    active_providers = _active_runtime_inventory_providers()
    if active_providers:
        # A runtime provider may consult the legacy catalog while discovering
        # its own (or another provider's) models.  Do not recurse into the
        # fallback construction from inside that discovery call.
        runtime_models = []
    else:
        active_providers.add(normalized_provider)
        try:
            client = _runtime_client()
            fingerprint = _runtime_provider_fingerprint(client)
            now = time.monotonic()
            with _runtime_inventory_cache_lock:
                cached = _runtime_inventory_cache.get(normalized_provider)
            if (
                cached is not None
                and cached[0] > now
                and cached[1] is client
                and cached[2] == fingerprint
            ):
                runtime_models = [dict(model) for model in cached[3]]
            else:
                runtime_models = client.list_models(
                    provider=normalized_provider or None
                )
                runtime_models = [
                    dict(model)
                    for model in runtime_models
                    if isinstance(model, dict)
                ]
                with _runtime_inventory_cache_lock:
                    _runtime_inventory_cache[normalized_provider] = (
                        now + _RUNTIME_INVENTORY_CACHE_TTL_SECONDS,
                        client,
                        fingerprint,
                        [dict(model) for model in runtime_models],
                    )
        except Exception:
            runtime_models = []
        finally:
            active_providers.discard(normalized_provider)
    runtime_models = [dict(model) for model in runtime_models if isinstance(model, dict)]

    providers_with_live_inventory = {
        str(model.get("provider_id") or model.get("provider") or "").strip()
        for model in runtime_models
        if str(
            (model.get("metadata") or {}).get("source")
            if isinstance(model.get("metadata"), dict)
            else ""
        )
        .strip()
        .lower()
        in _LIVE_INVENTORY_SOURCES
    }

    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for source, is_runtime in ((models, False), (runtime_models, True)):
        for raw in source:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            provider_id = str(item.get("provider_id") or item.get("provider") or "").strip()
            if normalized_provider and provider_id != normalized_provider:
                continue
            if not is_runtime and provider_id in providers_with_live_inventory:
                continue
            model_id = str(item.get("model_id") or item.get("model_name") or "").strip()
            qualified_id = str(item.get("qualified_model_id") or item.get("id") or "").strip()
            if not qualified_id and provider_id and model_id:
                qualified_id = f"{provider_id}/{model_id}"
            if not qualified_id:
                continue
            if qualified_id not in merged:
                merged[qualified_id] = item
                order.append(qualified_id)
                continue

            existing = merged[qualified_id]
            metadata = dict(existing.get("metadata") or {})
            metadata.update(dict(item.get("metadata") or {}))
            existing.update(
                {key: value for key, value in item.items() if value not in (None, "", [], {})}
            )
            existing["metadata"] = metadata

    return [merged[qualified_id] for qualified_id in order]


def _merge_selected_openrouter_inventory(
    models: List[Dict[str, Any]],
    provider: str,
) -> List[Dict[str, Any]]:
    """Use the selected catalog owner's bounded OpenRouter inventory fallback.

    Global contracts can be temporarily unavailable while the desktop surface
    restores its active profile.  The ordinary fallback already reads the
    selected model-catalog Pack directly; for OpenRouter it must use that
    Pack's live/LKG/static operation as well, otherwise the UI silently falls
    back to the small bundled allowlist for the lifetime of the process.

    This path remains fail-closed: the Pack must still be hash-verified and
    approved, and the catalog operation itself owns the fixed endpoint,
    timeout, response-size cap, cache, and static fallback.
    """
    normalized_provider = str(provider or "").strip()
    if normalized_provider and normalized_provider != "openrouter":
        return models
    try:
        from core_runtime.approval_manager import get_approval_manager

        approved, _reason = get_approval_manager().is_pack_approved_and_verified(
            "rumi_model_catalog_pack"
        )
        if not approved:
            return models

        result = _invoke(
            _MODEL_CATALOG_CONTRACT,
            "list",
            {"provider_id": "openrouter"},
        )
        openrouter_models = result.get("models") if isinstance(result, dict) else None
        if not isinstance(openrouter_models, list) or not openrouter_models:
            return models
        verified_openrouter = [
            dict(model)
            for model in openrouter_models
            if isinstance(model, dict) and model.get("provider_id") == "openrouter"
        ]
        if not verified_openrouter:
            return models
        if normalized_provider == "openrouter":
            return verified_openrouter
        return [
            model
            for model in models
            if isinstance(model, dict) and model.get("provider_id") != "openrouter"
        ] + verified_openrouter
    except Exception:
        return models


def list_profile_catalog() -> List[Dict[str, Any]]:
    try:
        result = _invoke(_MODEL_PROFILE_CONTRACT, "list", {})
    except (GlobalContractInvocationError, GlobalContractUnavailable):
        return _selected_profile_fallback()
    profiles = result.get("profiles") if isinstance(result, dict) else None
    profiles = profiles if isinstance(profiles, list) else []
    if not profiles:
        return _selected_profile_fallback()
    return _merge_model_profiles(profiles, list_model_catalog())


def _merge_model_profiles(
    profiles: List[Dict[str, Any]],
    models: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Add dynamically discovered models missing from the profile contract.

    Model search and the conversation picker consume profiles rather than the
    raw model catalog.  Keeping this projection in one place prevents a live
    provider from appearing in Settings while remaining absent in chat.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for raw in profiles:
        if not isinstance(raw, dict):
            continue
        profile = _with_legacy_profile_fields(raw)
        profile_id = str(profile.get("profile_id") or profile.get("qualified_model_id") or "")
        if not profile_id:
            continue
        merged[profile_id] = profile
        order.append(profile_id)

    for raw in models:
        if not isinstance(raw, dict):
            continue
        profile_id = str(raw.get("id") or raw.get("qualified_model_id") or "")
        provider_id = str(raw.get("provider_id") or raw.get("provider") or "")
        model_id = str(raw.get("model_id") or "")
        if not profile_id or not provider_id or not model_id:
            continue
        projected = _with_legacy_profile_fields(
            {
                **raw,
                "profile_id": profile_id,
                "qualified_model_id": profile_id,
                "provider_id": provider_id,
                "model_id": model_id,
                "display_name": str(raw.get("display_name") or raw.get("name") or model_id),
                "provider_display_name": str(raw.get("provider_display_name") or provider_id),
            }
        )
        if profile_id not in merged:
            merged[profile_id] = projected
            order.append(profile_id)
            continue
        existing = merged[profile_id]
        metadata = dict(existing.get("metadata") or {})
        metadata.update(dict(projected.get("metadata") or {}))
        existing.update(
            {key: value for key, value in projected.items() if value not in (None, "", [], {})}
        )
        existing["metadata"] = metadata
    return [merged[profile_id] for profile_id in order]


def _selected_profile_fallback() -> List[Dict[str, Any]]:
    """Expose one selectable profile per approved bundled catalog model."""
    profiles: List[Dict[str, Any]] = []
    for model in _selected_catalog_fallback(""):
        profile_id = str(model.get("id") or model.get("qualified_model_id") or "")
        provider_id = str(model.get("provider_id") or model.get("provider") or "")
        model_id = str(model.get("model_id") or "")
        if not profile_id or not provider_id or not model_id:
            continue
        profiles.append(
            _with_legacy_profile_fields(
                {
                    **model,
                    "profile_id": profile_id,
                    "qualified_model_id": profile_id,
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "display_name": str(model.get("display_name") or model.get("name") or model_id),
                    "provider_display_name": str(model.get("provider_display_name") or provider_id),
                }
            )
        )
    return profiles


def validate_catalog_coverage() -> List[Dict[str, Any]]:
    providers = {item.get("provider_id") for item in list_provider_catalog()}
    return [
        {
            "code": "catalog_provider_missing",
            "model_id": item.get("model_id"),
            "provider_id": item.get("provider_id"),
        }
        for item in list_model_catalog()
        if item.get("provider_id") not in providers
    ]


def _with_legacy_provider_fields(
    provider: Dict[str, Any],
    *,
    configured: bool,
) -> Dict[str, Any]:
    item = dict(provider)
    availability = dict(item.get("availability", {}))
    configured_envs: List[str] = []
    capabilities = set(item.get("capabilities", []))
    kind = str(item.get("kind") or "")
    metadata = dict(item.get("metadata", {}))
    default_model_for = item.get("default_model_for")
    if isinstance(default_model_for, dict):
        item["default_model_for"] = {
            str(key): str(value) for key, value in default_model_for.items()
        }
        metadata["default_model_for"] = dict(item["default_model_for"])
    item.setdefault("category", kind)
    item["configured"] = configured
    item["configured_envs"] = configured_envs
    item["local"] = kind == "local" or "local" in capabilities
    item["openai_compatible"] = "openai_compatible" in capabilities or bool(
        metadata.get("openai_compatible")
    )
    item["catalog_only"] = bool(
        metadata.get("catalog_only", availability.get("catalog_only", False))
    )
    item["supports_invoke"] = bool(
        metadata.get("supports_invoke", availability.get("supports_invoke", False))
    )
    item["configured_api_count"] = 1 if configured else 0
    item["named_apis"] = []
    return item


def _canonical_model_id(model: Dict[str, Any]) -> str:
    return str(
        model.get("canonical_model_id")
        or model.get("model_id")
        or model.get("model_name")
        or model.get("id")
        or ""
    )


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
    declared = model.get("capabilities")
    if isinstance(declared, list):
        values = {str(item) for item in declared}
        supports_thinking = "thinking" in values
        return {
            "supports_vision": "image_input" in values,
            "supports_image_input": "image_input" in values,
            "supports_audio": "audio_input" in values,
            "supports_audio_input": "audio_input" in values,
            "supports_tool_calling": "tool_calling" in values,
            "supports_fast": "fast" in values,
            "supports_thinking": supports_thinking,
            "thinking_levels": (["low", "medium", "high", "xhigh"] if supports_thinking else []),
            "default_thinking_level": ("medium" if supports_thinking else None),
            "speed_tier": "balanced",
            "quality_tier": "unknown",
            "knowledge_level": 0,
            "knowledge_band": knowledge_band_for_level(0),
            "cost_tier": "unknown",
            "latency_tier": "medium",
            "capability_tags": sorted(values),
            "allowed_roles": ["primary_chat"],
            "recommended_roles": ["primary_chat"],
            "model_capabilities": {key: True for key in sorted(values)},
        }
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
    item["same_model_across_providers_key"] = str(
        item.get("same_model_across_providers_key") or canonical
    )
    item["max_context"] = max_context
    item["max_context_tokens"] = max_context
    item["supports_thinking"] = supports_thinking
    item["thinking_levels"] = thinking_levels
    item["thinking_control"] = normalize_thinking_control(item)
    item["default_thinking_level"] = capability_fields.get(
        "default_thinking_level",
        item.get("default_thinking_level", "medium" if supports_thinking else None),
    )
    item["supports_vision"] = bool(capability_fields.get("supports_vision"))
    item["supports_image_input"] = bool(capability_fields.get("supports_image_input"))
    item["supports_audio"] = bool(capability_fields.get("supports_audio"))
    item["supports_audio_input"] = bool(
        capability_fields.get("supports_audio_input") or capability_fields.get("supports_audio")
    )
    item["supports_tool_calling"] = bool(capability_fields.get("supports_tool_calling"))
    item["supports_fast"] = bool(capability_fields.get("supports_fast"))
    item["speed_tier"] = str(capability_fields.get("speed_tier") or "balanced")
    item["quality_tier"] = str(capability_fields.get("quality_tier") or "unknown")
    item["knowledge_level"] = int(capability_fields.get("knowledge_level") or 0)
    item["knowledge_band"] = str(
        capability_fields.get("knowledge_band") or knowledge_band_for_level(item["knowledge_level"])
    )
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
            "thinking_control": item["thinking_control"],
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
            "routing": dict(item.get("routing", {}))
            if isinstance(item.get("routing"), dict)
            else {},
            "request_features": dict(item.get("request_features", {}))
            if isinstance(item.get("request_features"), dict)
            else {},
            "thinking": dict(item.get("thinking", {}))
            if isinstance(item.get("thinking"), dict)
            else {},
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
        "thinking_control": item.get("thinking_control", {}),
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
    item["thinking_control"] = enriched["thinking_control"]
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
