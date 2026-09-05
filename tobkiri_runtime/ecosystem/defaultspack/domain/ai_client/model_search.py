from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from domain.ai_client.audio_capability import metadata_supports_audio_input
from domain.ai_client.model_groups import normalize_model_groups


def search_models(filters: dict[str, Any] | None = None, *, profiles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    filters = dict(filters or {})
    if profiles is None:
        profiles = _profile_catalog()
    query = str(filters.get("query") or "").strip().casefold()
    type_filter = _as_set(filters.get("type") or filters.get("model_type"))
    if not type_filter:
        type_filter = {"chat", "reasoning"}
    requires = filters.get("requires") if isinstance(filters.get("requires"), dict) else {}
    speed_filter = _as_set(filters.get("speed_tier"))
    provider_id = str(filters.get("provider_id") or filters.get("provider") or "").strip()
    configured_only = bool(filters.get("configured_only", False))
    local_only = bool(filters.get("local_only", False))
    try:
        min_knowledge_level = int(filters.get("min_knowledge_level", 0) or 0)
    except (TypeError, ValueError):
        min_knowledge_level = 0
    try:
        max_results = max(1, min(100, int(filters.get("max_results", 20) or 20)))
    except (TypeError, ValueError):
        max_results = 20

    matches: list[dict[str, Any]] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        profile_type = str(profile.get("type") or "chat").strip().lower() or "chat"
        if profile_type not in type_filter:
            continue
        item = _public_model(profile)
        if query and not _matches_query(item, query):
            continue
        if provider_id and item.get("provider_id") != provider_id:
            continue
        if configured_only and not item.get("configured"):
            continue
        if local_only and not item.get("local"):
            continue
        if min_knowledge_level and int(item.get("knowledge_level") or 0) < min_knowledge_level:
            continue
        if speed_filter and str(item.get("speed_tier") or "") not in speed_filter:
            continue
        if not _matches_required_capabilities(item, requires):
            continue
        item["score"] = _score_model(item, filters)
        matches.append(item)
    matches.sort(key=lambda item: (-int(item.get("score") or 0), str(item.get("label") or item.get("profile_id") or "").casefold()))
    return {
        "models": [deepcopy(item) for item in matches[:max_results]],
        "filters_applied": {
            "query": filters.get("query", ""),
            "type": sorted(type_filter),
            "requires": deepcopy(requires),
            "min_knowledge_level": min_knowledge_level,
            "speed_tier": sorted(speed_filter),
            "configured_only": configured_only,
            "provider_id": provider_id,
            "max_results": max_results,
        },
    }


def get_model_capabilities(profile_id: str, *, profiles: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    needle = str(profile_id or "").strip()
    if not needle:
        return None
    try:
        from domain.ai_client.model_pack_store import ModelPackStore

        if ModelPackStore.is_model_pack_ref(needle):
            settings = None
            pack = ModelPackStore(settings).get(needle)
            if pack is not None:
                member_caps = [
                    get_model_capabilities(member.model, profiles=profiles)
                    for member in pack.members
                    if member.model and member.model != needle
                ]
                member_caps = [item for item in member_caps if isinstance(item, dict)]
                return {
                    "profile_id": needle,
                    "qualified_model_id": needle,
                    "provider_id": "modelpack",
                    "model_id": pack.id,
                    "display_name": pack.display_name or pack.id,
                    "supports_vision": any(bool(item.get("supports_vision") or item.get("supports_image_input")) for item in member_caps),
                    "supports_image_input": any(bool(item.get("supports_image_input") or item.get("supports_vision")) for item in member_caps),
                    "supports_audio": any(bool(item.get("supports_audio") or item.get("supports_audio_input")) for item in member_caps),
                    "supports_audio_input": any(bool(item.get("supports_audio_input") or item.get("supports_audio")) for item in member_caps),
                    "supports_tool_calling": any(bool(item.get("supports_tool_calling")) for item in member_caps),
                    "supports_thinking": any(bool(item.get("supports_thinking")) for item in member_caps),
                    "supports_fast": any(bool(item.get("supports_fast")) for item in member_caps),
                    "capability_tags": sorted(
                        {
                            tag
                            for item in member_caps
                            for tag in (item.get("capability_tags") if isinstance(item.get("capability_tags"), list) else [])
                            if str(tag).strip()
                        }
                    ),
                    "configured": any(bool(item.get("configured")) for item in member_caps) if member_caps else True,
                    "metadata": {"model_pack": True, "mode": pack.mode},
                }
    except Exception:
        pass
    for profile in profiles if profiles is not None else _profile_catalog():
        if not isinstance(profile, dict):
            continue
        aliases = {
            str(profile.get("profile_id") or ""),
            str(profile.get("qualified_model_id") or ""),
            "{}/{}".format(profile.get("provider_id") or profile.get("provider") or "", profile.get("model_id") or ""),
        }
        if needle in aliases:
            return _public_model(profile)
    return None


def get_profile_catalog() -> list[dict[str, Any]]:
    """Return the resolved model profile catalog for one runtime operation."""
    return _profile_catalog()


def recommend_model(request: dict[str, Any] | None = None, *, profiles: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    request = dict(request or {})
    filters = {
        "query": request.get("query", ""),
        "requires": request.get("requires", {}),
        "min_knowledge_level": request.get("min_knowledge_level", 0),
        "speed_tier": request.get("speed_tier"),
        "configured_only": request.get("configured_only", False),
        "provider_id": request.get("provider_id", ""),
        "max_results": request.get("max_results", 10),
    }
    result = search_models(filters, profiles=profiles)
    selected = result["models"][0] if result["models"] else None
    return {
        "selected_model": selected,
        "candidates": result["models"],
        "reason_codes": _recommendation_reasons(selected, request) if selected else ["no_matching_model"],
        "filters_applied": result["filters_applied"],
    }


def models_for_group(group_id: str, settings: dict[str, Any] | None, profiles: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    groups = normalize_model_groups((settings or {}).get("model_groups") if isinstance(settings, dict) else None)
    group = groups.get(str(group_id or "default"), groups["default"])
    candidates = [
        _public_model(profile)
        for profile in (profiles if profiles is not None else _profile_catalog())
        if isinstance(profile, dict) and _is_chat_routable_profile(profile)
    ]
    allowed = {str(item) for item in group.get("allowed_models", []) if str(item or "").strip()}
    if allowed:
        candidates = [item for item in candidates if str(item.get("profile_id") or "") in allowed or str(item.get("qualified_model_id") or "") in allowed]
    if group.get("require_vision"):
        candidates = [item for item in candidates if item.get("supports_vision")]
    if group.get("require_thinking"):
        candidates = [item for item in candidates if item.get("supports_thinking")]
    min_level = group.get("min_knowledge_level")
    if min_level is not None:
        try:
            floor = int(min_level)
            candidates = [item for item in candidates if int(item.get("knowledge_level") or 0) >= floor]
        except (TypeError, ValueError):
            pass
    max_level = group.get("max_knowledge_level")
    if max_level is not None:
        try:
            ceiling = int(max_level)
            candidates = [item for item in candidates if int(item.get("knowledge_level") or 0) <= ceiling]
        except (TypeError, ValueError):
            pass
    min_speed = str(group.get("min_speed_tier") or "").strip()
    if min_speed:
        candidates = [item for item in candidates if str(item.get("speed_tier") or "") == min_speed]
    return candidates


def _is_chat_routable_profile(profile: dict[str, Any]) -> bool:
    model_type = str(profile.get("type") or "chat").strip().lower()
    if not model_type or model_type == "chat":
        return True
    if model_type != "reasoning":
        return False
    defaults = profile.get("defaults") if isinstance(profile.get("defaults"), dict) else {}
    capabilities = _capability_dict(profile.get("capabilities"))
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
    metadata_capabilities = _capability_dict(metadata.get("capabilities"))
    return bool(
        defaults.get("chat")
        or capabilities.get("chat")
        or capabilities.get("text")
        or metadata_capabilities.get("chat")
        or metadata_capabilities.get("text")
    )


def _capability_dict(value: Any) -> dict[str, bool]:
    if isinstance(value, dict):
        return {str(key): bool(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return {str(item): True for item in value if str(item or "").strip()}
    return {}


def _profile_catalog() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    try:
        from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_profile_catalog
    except ModuleNotFoundError:
        from backend.ai_client.provider_catalog import list_profile_catalog
    try:
        profiles = [profile for profile in list_profile_catalog() if isinstance(profile, dict)]
    except Exception:
        profiles = []
    try:
        from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_model_catalog
    except ModuleNotFoundError:
        from backend.ai_client.provider_catalog import list_model_catalog
    try:
        profiles.extend(_embedding_profiles_from_models(list_model_catalog()))
    except Exception:
        pass
    try:
        profiles.extend(_openrouter_chat_reasoning_profiles(list_model_catalog("openrouter")))
    except Exception:
        pass
    try:
        from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService

        service = ModelRuntimeSettingsService()
        profiles.extend(service.runtime_defined_profiles(service.get_settings()))
    except Exception:
        pass
    return _dedupe_profiles(profiles)


def _dedupe_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        key = str(
            profile.get("qualified_model_id")
            or profile.get("profile_id")
            or profile.get("id")
            or "{}/{}".format(profile.get("provider_id") or profile.get("provider") or "", profile.get("model_id") or profile.get("model") or "")
        ).strip()
        if not key:
            continue
        if key in positions:
            existing = output[positions[key]]
            if _profile_configured(profile) and not _profile_configured(existing):
                output[positions[key]] = profile
            continue
        positions[key] = len(output)
        output.append(profile)
    return output


def _profile_configured(profile: dict[str, Any]) -> bool:
    availability = profile.get("availability") if isinstance(profile.get("availability"), dict) else {}
    return bool(
        profile.get("configured")
        or availability.get("configured")
        or availability.get("active")
        or str(availability.get("status") or "").lower() in {"configured", "active"}
    )


def _embedding_profiles_from_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        if str(model.get("type") or "").strip().lower() != "embedding":
            continue
        provider_id = str(model.get("provider_id") or model.get("provider") or "").strip()
        model_id = str(model.get("model_id") or model.get("model") or model.get("id") or "").strip()
        qualified = str(model.get("qualified_model_id") or model.get("id") or "{}/{}".format(provider_id, model_id)).strip()
        if not qualified or qualified in seen:
            continue
        seen.add(qualified)
        output.append(
            {
                **model,
                "profile_id": qualified,
                "qualified_model_id": qualified,
                "provider_id": provider_id,
                "provider": provider_id,
                "model_id": model_id,
                "model": model_id,
                "display_name": str(model.get("display_name") or model.get("name") or model_id or qualified),
                "type": "embedding",
                "capability_tags": sorted(set([*(model.get("capability_tags") if isinstance(model.get("capability_tags"), list) else []), "embedding"])),
                "recommended_roles": sorted(set([*(model.get("recommended_roles") if isinstance(model.get("recommended_roles"), list) else []), "tool_embedding"])),
                "allowed_roles": sorted(set([*(model.get("allowed_roles") if isinstance(model.get("allowed_roles"), list) else []), "tool_embedding"])),
            }
        )
    return output


def _openrouter_chat_reasoning_profiles(
    models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project catalog-only OpenRouter chat models into searchable profiles.

    The model-profile owner remains authoritative for saved profiles. This is a
    read-only union for provider inventory that has no saved profile yet.
    """
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        if str(model.get("provider_id") or model.get("provider") or "") != "openrouter":
            continue
        capabilities = {
            str(value).strip().lower()
            for value in model.get("capabilities", [])
            if str(value or "").strip()
        }
        model_type = str(model.get("type") or "").strip().lower()
        if not model_type:
            model_type = "chat"
        if model_type not in {"chat", "reasoning"}:
            continue
        if not {"chat", "text_input", "text_output"}.intersection(capabilities):
            continue
        provider_model_id = str(
            model.get("provider_model_id") or model.get("model") or ""
        ).strip()
        qualified_model_id = str(
            model.get("qualified_model_id")
            or model.get("id")
            or model.get("model_id")
            or ""
        ).strip()
        if not provider_model_id and qualified_model_id.startswith("openrouter/"):
            provider_model_id = qualified_model_id[len("openrouter/") :]
        if not qualified_model_id:
            qualified_model_id = f"openrouter/{provider_model_id}"
        if not provider_model_id or not qualified_model_id or qualified_model_id in seen:
            continue
        seen.add(qualified_model_id)
        context_length = model.get("context_length", model.get("max_context", 0))
        try:
            max_context = max(0, int(context_length or 0))
        except (TypeError, ValueError):
            max_context = 0
        metadata = (
            deepcopy(model.get("metadata"))
            if isinstance(model.get("metadata"), dict)
            else {}
        )
        metadata.setdefault("catalog_revision", model.get("catalog_revision"))
        output.append(
            {
                "profile_id": qualified_model_id,
                "qualified_model_id": qualified_model_id,
                "provider_id": "openrouter",
                "provider": "openrouter",
                "provider_display_name": "OpenRouter",
                "model_id": provider_model_id,
                "model": provider_model_id,
                "display_name": str(
                    model.get("display_name")
                    or model.get("name")
                    or provider_model_id
                ),
                "type": model_type,
                "capabilities": {
                    "chat": True,
                    "text": True,
                    "thinking": model_type == "reasoning"
                    or "thinking" in capabilities
                    or "reasoning" in capabilities,
                },
                "supports_vision": "image_input" in capabilities or "vision" in capabilities,
                "supports_image_input": "image_input" in capabilities or "vision" in capabilities,
                "supports_audio": "audio_input" in capabilities,
                "supports_audio_input": "audio_input" in capabilities,
                "supports_tool_calling": "tool_calling" in capabilities or "tool_calls" in capabilities,
                "supports_thinking": model_type == "reasoning" or "thinking" in capabilities or "reasoning" in capabilities,
                "thinking_levels": ["low", "medium", "high"]
                if model_type == "reasoning" or "thinking" in capabilities
                else [],
                "supports_fast": "fast" in capabilities,
                "max_context": max_context,
                "capability_tags": sorted(capabilities),
                "availability": {
                    "status": "available"
                    if model.get("available", True)
                    else "unavailable",
                    "catalog_source": metadata.get("inventory_source", "bundled_catalog"),
                },
                "metadata": metadata,
            }
        )
    return output


def _public_model(profile: dict[str, Any]) -> dict[str, Any]:
    profile_id = str(profile.get("profile_id") or profile.get("qualified_model_id") or profile.get("id") or "").strip()
    provider_id = str(profile.get("provider_id") or profile.get("provider") or "").strip()
    model_id = str(profile.get("model_id") or profile.get("model") or "").strip()
    label_provider = str(profile.get("provider_display_name") or profile.get("provider_name") or provider_id or "").strip()
    display_name = str(profile.get("display_name") or profile.get("name") or model_id or profile_id).strip()
    model_type = str(profile.get("type") or "chat").strip().lower() or "chat"
    availability = profile.get("availability") if isinstance(profile.get("availability"), dict) else {}
    supports_audio = metadata_supports_audio_input(profile)
    configured = bool(
        profile.get("configured")
        or availability.get("configured")
        or availability.get("active")
        or str(availability.get("status") or "").lower() in {"configured", "active"}
        or provider_id == "stub"
    )
    capability_tags = list(profile.get("capability_tags") or [])
    recommended_roles = list(profile.get("recommended_roles") or [])
    allowed_roles = list(profile.get("allowed_roles") or [])
    if model_type == "embedding":
        capability_tags = sorted({*capability_tags, "embedding"})
        recommended_roles = sorted({*recommended_roles, "tool_embedding"})
        allowed_roles = sorted({*allowed_roles, "tool_embedding"})
    return {
        "profile_id": profile_id,
        "qualified_model_id": str(profile.get("qualified_model_id") or profile_id),
        "label": f"{label_provider} / {display_name}" if label_provider else display_name,
        "display_name": display_name,
        "provider_id": provider_id,
        "provider_display_name": label_provider,
        "model_id": model_id,
        "type": model_type,
        "configured": configured,
        "local": bool(profile.get("local") or availability.get("local") or provider_id in {"stub", "ollama", "lmstudio", "vllm", "llamacpp"}),
        "requires_api_key": bool(provider_id and provider_id not in {"stub"} and not configured),
        "supports_vision": bool(profile.get("supports_vision")),
        "supports_image_input": bool(profile.get("supports_image_input") or profile.get("supports_vision")),
        "supports_audio": supports_audio,
        "supports_audio_input": supports_audio,
        "supports_tool_calling": bool(profile.get("supports_tool_calling")),
        "supports_thinking": bool(profile.get("supports_thinking")),
        "thinking_levels": list(profile.get("thinking_levels") or []),
        "supports_fast": bool(profile.get("supports_fast")),
        "speed_tier": str(profile.get("speed_tier") or "balanced"),
        "quality_tier": str(profile.get("quality_tier") or "unknown"),
        "knowledge_level": int(profile.get("knowledge_level") or 0),
        "knowledge_band": str(profile.get("knowledge_band") or "unknown"),
        "cost_tier": str(profile.get("cost_tier") or "unknown"),
        "capability_tags": capability_tags,
        "recommended_roles": recommended_roles,
        "allowed_roles": allowed_roles,
        "max_context": profile.get("max_context"),
        "availability": deepcopy(availability),
        "metadata": deepcopy(profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}),
        "notes": str(
            profile.get("notes")
            or (profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}).get("notes")
            or ""
        ),
    }


def _as_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item or "").strip()}
    return set()


def _matches_required_capabilities(item: dict[str, Any], requires: dict[str, Any]) -> bool:
    mapping = {
        "vision": "supports_vision",
        "image_input": "supports_image_input",
        "audio": "supports_audio",
        "audio_input": "supports_audio",
        "tool_calling": "supports_tool_calling",
        "tools": "supports_tool_calling",
        "thinking": "supports_thinking",
        "fast": "supports_fast",
    }
    for source, target in mapping.items():
        if requires.get(source) is True and not item.get(target):
            return False
    return True


def _score_model(item: dict[str, Any], filters: dict[str, Any]) -> int:
    score = int(item.get("knowledge_level") or 0)
    score += 25 if item.get("configured") else 0
    score += 10 if item.get("supports_thinking") and (filters.get("requires") or {}).get("thinking") else 0
    score += 10 if item.get("supports_vision") and (filters.get("requires") or {}).get("vision") else 0
    score += 10 if item.get("supports_audio") and (filters.get("requires") or {}).get("audio") else 0
    score += 8 if item.get("supports_tool_calling") and (filters.get("requires") or {}).get("tool_calling") else 0
    score += 6 if item.get("speed_tier") == "fast" else 0
    score += 4 if item.get("local") else 0
    return score


def _search_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get(key) or "")
        for key in (
            "profile_id",
            "qualified_model_id",
            "label",
            "display_name",
            "provider_id",
            "provider_display_name",
            "model_id",
            "speed_tier",
            "quality_tier",
            "knowledge_band",
            "cost_tier",
            "notes",
        )
    ]
    for key in ("capability_tags", "recommended_roles", "allowed_roles", "thinking_levels"):
        value = item.get(key)
        if isinstance(value, list):
            parts.extend(str(entry) for entry in value)
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    for key in ("notes", "source", "privacy", "provider_kind", "openai_model", "openai_base_url"):
        parts.append(str(metadata.get(key) or ""))
    provider_id = str(item.get("provider_id") or "").strip().casefold()
    model_id = str(item.get("model_id") or "").strip().casefold()
    if provider_id == "openrouter" and re.fullmatch(r"tencent/hy3(?:-preview)?(?::free)?", model_id):
        # Keep historical user searches discoverable without re-exposing the
        # expired :free catalog IDs as selectable, non-executable profiles.
        parts.extend(("hy3 free", "tencent hy3 free", "hy3 preview free"))
    return " ".join(parts).casefold()


def _normalize_search_text(value: str) -> str:
    return re.sub(r"[\W_]+", " ", value.casefold()).strip()


def _matches_query(item: dict[str, Any], query: str) -> bool:
    text = _search_text(item)
    normalized_text = _normalize_search_text(text)
    normalized_query = _normalize_search_text(query)
    if query in text or (normalized_query and normalized_query in normalized_text):
        return True
    tokens = [token for token in normalized_query.split() if token]
    return bool(tokens) and all(token in normalized_text or token in text for token in tokens)


def _recommendation_reasons(selected: dict[str, Any] | None, request: dict[str, Any]) -> list[str]:
    if not selected:
        return ["no_matching_model"]
    reasons = []
    requires = request.get("requires") if isinstance(request.get("requires"), dict) else {}
    if requires.get("vision") and selected.get("supports_vision"):
        reasons.append("requires_vision")
    if requires.get("audio") and selected.get("supports_audio"):
        reasons.append("requires_audio")
    if requires.get("tool_calling") and selected.get("supports_tool_calling"):
        reasons.append("requires_tool_calling")
    if requires.get("thinking") and selected.get("supports_thinking"):
        reasons.append("requires_thinking")
    if selected.get("speed_tier") == "fast":
        reasons.append("fast_candidate")
    if not reasons:
        reasons.append("best_available_match")
    return reasons
