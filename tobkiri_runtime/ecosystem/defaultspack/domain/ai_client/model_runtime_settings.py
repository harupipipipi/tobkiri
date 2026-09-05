from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from domain.ai_client.api_key_store import (
    _secrets_dir as provider_secrets_dir,
    provider_api_metadata,
    provider_has_api_key,
    provider_named_api_keys,
    read_provider_api_key,
)
from domain.ai_client.model_groups import default_model_groups, normalize_model_groups
from domain.ai_client.model_pack_store import normalize_model_packs
from domain.ai_client.model_roles import (
    normalize_utility_model_policy,
    normalize_utility_models,
)
from domain.ai_client.rumi_process import (
    RUMI_MODEL_PACK_ID,
    ensure_default_rumi_model_pack,
    resolve_rumi_base_model,
)
from domain.frontend_settings_store import (
    FrontendSettingsStore,
    defaultspack_frontend_settings_path,
)


VALID_THINKING_LEVELS = {"none", "low", "medium", "high", "xhigh"}
DEFAULT_MODEL = "stub/default"
LEGACY_CLOUD_DEFAULT_MODELS = {
    "openrouter/tencent/hy3:free",
    "openrouter/tencent/hy3-preview:free",
}
DEFAULT_THINKING_LEVEL = "medium"
DEFAULT_DEEPTHINK_ENABLED = False
DEEPTHINK_STATE_REF = "defaultspack:models.deepthink_enabled"
CEREBRAS_REASONING_MODELS = {"gpt-oss-120b", "zai-glm-4.7"}
MODEL_SLOT_MAIN = "main"
MODEL_SLOT_LIGHTWEIGHT = "lightweight"

_settings_cache_lock = threading.RLock()
_settings_cache: dict[
    tuple[str, str], tuple[tuple[Any, ...], dict[str, Any]]
] = {}


def _file_signature(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return str(path), stat.st_mtime_ns, stat.st_size


def _settings_dependency_signature(
    settings_path: Path,
    pack_root: Path,
) -> tuple[Any, ...]:
    """Return non-secret inputs that affect resolved model settings.

    Credential values stay inside the provider credential store.  The cache
    only tracks the boolean availability state used by the settings projection
    so a secret is never copied into, or hashed by, this module.
    """
    secrets_dir = provider_secrets_dir(pack_root)
    dependency_paths = [
        settings_path,
        settings_path.with_suffix(f"{settings_path.suffix}.bak"),
        secrets_dir / "provider_api_keys.json",
        secrets_dir / "custom_providers.json",
        secrets_dir / "provider_oauth.json",
        pack_root / ".env",
        pack_root / "config" / "settings_control_center" / "oauth.env",
    ]
    file_signatures = tuple(
        signature
        for path in dependency_paths
        if (signature := _file_signature(path)) is not None
    )
    credential_state = tuple(
        (
            provider_id,
            provider_has_api_key(provider_id, pack_root=pack_root),
        )
        for provider_id in ("google", "openrouter")
    )
    return (tuple(file_signatures), credential_state)


def _invalidate_settings_cache(path: Path, pack_root: Path) -> None:
    with _settings_cache_lock:
        _settings_cache.pop((str(path), str(pack_root)), None)


class ModelRuntimeSettingsService:
    """Owns model runtime settings persisted in frontend_settings.json."""

    def __init__(self, pack_root: Path | None = None) -> None:
        self._pack_root = pack_root or Path(__file__).resolve().parents[2]
        settings_owner = pack_root if pack_root is not None else None
        self._settings_path = defaultspack_frontend_settings_path(settings_owner)
        self._settings_store = FrontendSettingsStore(self._settings_path)

    def get_settings(self) -> dict[str, Any]:
        cache_key = (str(self._settings_path), str(self._pack_root))
        signature = _settings_dependency_signature(self._settings_path, self._pack_root)
        with _settings_cache_lock:
            cached = _settings_cache.get(cache_key)
            if cached is not None and cached[0] == signature:
                return deepcopy(cached[1])

        resolved = self._read_all().get("models", {})
        resolved = resolved if isinstance(resolved, dict) else self.default_model_settings()
        with _settings_cache_lock:
            _settings_cache[cache_key] = (signature, deepcopy(resolved))
        return deepcopy(resolved)

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}

        def merge(all_settings: dict[str, Any]) -> dict[str, Any]:
            current_models = all_settings.get("models", {})
            if not isinstance(current_models, dict):
                current_models = {}
            sanitized = self.sanitize_models_patch(
                patch or {}, current_models=current_models
            )
            merged = self._deep_merge(current_models, sanitized)
            result.update(self.refresh_models_settings(merged))
            all_settings["models"] = dict(result)
            return all_settings

        self._settings_store.update(merge)
        _invalidate_settings_cache(self._settings_path, self._pack_root)
        return result

    def get_preferred_model(self) -> str:
        return str(self.get_settings().get("preferred_model") or DEFAULT_MODEL)

    def set_preferred_model(self, profile_id: str) -> dict[str, Any]:
        profile = str(profile_id or "").strip()
        if not profile:
            raise ValueError("profile_id is required")
        settings = self.update_settings({"preferred_model": profile})
        return {"profile_id": settings["preferred_model"], "settings": settings}

    def get_preferred_model_group(self) -> str:
        return str(self.get_settings().get("preferred_model_group") or "default")

    def set_preferred_model_group(self, group_id: str) -> dict[str, Any]:
        normalized = str(group_id or "").strip() or "default"
        settings = self.update_settings({"preferred_model_group": normalized})
        return {"group_id": settings["preferred_model_group"], "settings": settings}

    def set_auto_route_within_group(self, enabled: bool) -> dict[str, Any]:
        settings = self.update_settings({"auto_route_within_group": bool(enabled)})
        return {"enabled": bool(settings["auto_route_within_group"]), "settings": settings}

    def set_model_role(self, role_id: str, model_id: str) -> dict[str, Any]:
        role = str(role_id or "").strip()
        model = str(model_id or "").strip()
        if not role:
            raise ValueError("role_id is required")
        settings = self.get_settings()
        utility_models = normalize_utility_models(settings.get("utility_models"))
        utility_models[role] = model
        updated = self.update_settings({"utility_models": utility_models})
        return {"role_id": role, "model_id": updated["utility_models"].get(role, ""), "settings": updated}

    def resolve_model_candidates(self, query: str, limit: int = 8) -> dict[str, Any]:
        cleaned_query = str(query or "").strip()
        try:
            max_items = max(0, int(limit))
        except (TypeError, ValueError):
            max_items = 8
        if not cleaned_query:
            return {"query": cleaned_query, "exact": None, "candidates": []}

        settings = self.get_settings()
        favorites = {
            str(item or "").strip()
            for item in settings.get("favorite_profiles", [])
            if str(item or "").strip()
        }
        scored: list[dict[str, Any]] = []
        seen: set[str] = set()
        for profile in self._list_profile_catalog_for_resolution(settings):
            if not self._is_chat_profile(profile):
                continue
            candidate = self._candidate_from_profile(profile, favorites)
            candidate_key = str(candidate.get("profile_id") or candidate.get("qualified_model_id") or "").strip()
            if not candidate_key or candidate_key in seen:
                continue
            match_kind, base_score = self._candidate_match(candidate, cleaned_query)
            if base_score <= 0:
                continue
            candidate["score"] = self._candidate_score(candidate, base_score)
            candidate["_match_kind"] = match_kind
            seen.add(candidate_key)
            scored.append(candidate)

        scored.sort(
            key=lambda item: (
                -int(item.get("score") or 0),
                str(item.get("label") or item.get("display_name") or item.get("profile_id") or "").casefold(),
                str(item.get("profile_id") or "").casefold(),
            )
        )
        exact_id_candidates = [item for item in scored if item.get("_match_kind") == "exact_id"]
        exact_field_candidates = [item for item in scored if item.get("_match_kind") == "exact_field"]
        if len(exact_id_candidates) == 1:
            exact = self._public_candidate(exact_id_candidates[0])
        elif len(exact_id_candidates) == 0 and len(exact_field_candidates) == 1:
            exact = self._public_candidate(exact_field_candidates[0])
        else:
            exact = None
        return {
            "query": cleaned_query,
            "exact": exact,
            "candidates": [self._public_candidate(item) for item in scored[:max_items]],
        }

    def get_thinking_level(
        self,
        scope: str = "global",
        profile_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        settings = self.get_settings()
        scope = str(scope or "global")
        if scope == "profile" and profile_id:
            level = settings.get("thinking_level_by_profile", {}).get(profile_id)
        elif scope == "conversation" and conversation_id:
            level = settings.get("thinking_level_by_conversation", {}).get(conversation_id)
        else:
            level = settings.get("thinking_level")
            scope = "global"
        return {
            "scope": scope,
            "profile_id": profile_id,
            "conversation_id": conversation_id,
            "level": self._normalize_level(level),
        }

    def set_thinking_level(
        self,
        level: str,
        scope: str = "global",
        profile_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        validation = self.validate_thinking_level(level, profile_id)
        if not validation["valid"]:
            raise ValueError(validation["message"])
        normalized = validation["level"]
        scope = str(scope or "global")
        settings = self.get_settings()
        patch: dict[str, Any] = {}
        if scope == "profile":
            if not profile_id:
                raise ValueError("profile_id is required for profile thinking level")
            values = dict(settings.get("thinking_level_by_profile") or {})
            values[str(profile_id)] = normalized
            patch["thinking_level_by_profile"] = values
        elif scope == "conversation":
            if not conversation_id:
                raise ValueError("conversation_id is required for conversation thinking level")
            values = dict(settings.get("thinking_level_by_conversation") or {})
            values[str(conversation_id)] = normalized
            patch["thinking_level_by_conversation"] = values
        elif scope == "turn":
            return {"scope": "turn", "level": normalized, "persisted": False}
        else:
            patch["thinking_level"] = normalized
            scope = "global"
        updated = self.update_settings(patch)
        return {
            "scope": scope,
            "profile_id": profile_id,
            "conversation_id": conversation_id,
            "level": normalized,
            "persisted": scope != "turn",
            "settings": updated,
        }

    def get_deepthink_enabled(self) -> dict[str, Any]:
        settings = self.get_settings()
        return {
            "enabled": bool(settings.get("deepthink_enabled", DEFAULT_DEEPTHINK_ENABLED)),
            "state_ref": DEEPTHINK_STATE_REF,
            "revision": self._settings_store.state_revision(DEEPTHINK_STATE_REF),
            "warning": "DeepThinkが有効なタスクには数時間かかる可能性があります。",
        }

    def set_deepthink_enabled(
        self,
        enabled: bool | None = None,
        *,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        requested = enabled if isinstance(enabled, bool) else None

        def mutate(all_settings: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            current_models = all_settings.get("models", {})
            if not isinstance(current_models, dict):
                current_models = {}
            current_enabled = bool(
                current_models.get("deepthink_enabled", DEFAULT_DEEPTHINK_ENABLED)
            )
            next_enabled = not current_enabled if requested is None else requested
            sanitized = self.sanitize_models_patch(
                {"deepthink_enabled": next_enabled}, current_models=current_models
            )
            updated_models = self.refresh_models_settings(
                self._deep_merge(current_models, sanitized)
            )
            all_settings["models"] = dict(updated_models)
            return all_settings, {
                "enabled": next_enabled,
                "persisted": True,
                "settings": updated_models,
            }

        fingerprint = json.dumps(
            {
                "state_ref": DEEPTHINK_STATE_REF,
                "desired": requested,
                "expected_revision": expected_revision,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        updated = self._settings_store.mutate_state(
            DEEPTHINK_STATE_REF,
            mutate,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        _invalidate_settings_cache(self._settings_path, self._pack_root)
        next_enabled = bool(updated.get("enabled"))
        message = (
            "DeepThinkをONにしました。タスクには数時間かかる可能性があります。"
            if next_enabled
            else "DeepThinkをOFFにしました。"
        )
        updated["message"] = message
        updated["warning"] = "タスクには数時間かかる可能性があります。" if next_enabled else ""
        updated["state_snapshot"] = {
            "state_ref": DEEPTHINK_STATE_REF,
            "value": next_enabled,
            "revision": int(updated.get("revision") or 0),
            "freshness": "authoritative",
        }
        return updated

    def get_effective_thinking_level(
        self,
        profile_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        settings = self.get_settings()
        if conversation_id:
            by_conversation = settings.get("thinking_level_by_conversation", {})
            if isinstance(by_conversation, dict):
                level = by_conversation.get(conversation_id)
                if self._normalize_level(level) == level:
                    return {"level": level, "scope": "conversation", "conversation_id": conversation_id}
        if profile_id:
            by_profile = settings.get("thinking_level_by_profile", {})
            if isinstance(by_profile, dict):
                level = by_profile.get(profile_id)
                if self._normalize_level(level) == level:
                    return {"level": level, "scope": "profile", "profile_id": profile_id}
        return {"level": self._normalize_level(settings.get("thinking_level")), "scope": "global"}

    def validate_thinking_level(self, level: str, profile_id: str | None = None) -> dict[str, Any]:
        del profile_id
        normalized = self._normalize_level(level)
        return {
            "valid": normalized in VALID_THINKING_LEVELS and str(level or "").strip() in VALID_THINKING_LEVELS,
            "level": normalized,
            "message": "" if str(level or "").strip() in VALID_THINKING_LEVELS else "thinking level must be one of none, low, medium, high, xhigh",
        }

    def normalize_for_provider(self, provider_id: str, model_id: str, level: str) -> dict[str, Any]:
        normalized = self._normalize_level(level)
        provider = str(provider_id or "").strip().lower()
        result: dict[str, Any] = {
            "provider_id": provider_id,
            "model_id": model_id,
            "requested_level": level,
            "level": normalized,
        }
        metadata_mapping = self._thinking_provider_mapping(provider_id, model_id)
        if metadata_mapping is not None:
            mapped = metadata_mapping.get(normalized)
            if isinstance(mapped, dict):
                result["provider_params"] = dict(mapped)
            else:
                result["provider_params"] = {}
            return result
        if provider in {"openai", "openai_compatible", "openrouter", "nvidia"}:
            effort = "high" if normalized == "xhigh" else normalized
            if effort != "none":
                result["provider_params"] = {"reasoning_effort": effort}
            else:
                result["provider_params"] = {}
            result["level"] = effort if normalized == "xhigh" and provider == "openai" else normalized
        elif provider == "cerebras":
            model_key = str(model_id or "").strip()
            if model_key.startswith("cerebras/"):
                model_key = model_key.split("/", 1)[1]
            effort = "high" if normalized == "xhigh" else normalized
            if effort != "none" and model_key in CEREBRAS_REASONING_MODELS:
                result["provider_params"] = {"reasoning_effort": effort}
            else:
                result["provider_params"] = {}
            result["level"] = normalized
        elif provider == "anthropic":
            result["provider_params"] = {"thinking_level": normalized}
        elif provider == "google":
            result["provider_params"] = {"thinking_level": normalized}
        else:
            result["provider_params"] = {"thinking_level": normalized}
        return result

    @staticmethod
    def _thinking_provider_mapping(provider_id: str, model_id: str) -> dict[str, Any] | None:
        try:
            from domain.ai_client.providers import get_all_known_models
        except Exception:
            return None
        provider = str(provider_id or "").strip().lower()
        raw_model = str(model_id or "").strip()
        lookup_keys = ModelRuntimeSettingsService._model_lookup_keys(provider, raw_model)
        try:
            candidates = get_all_known_models(provider)
        except Exception:
            return None
        for item in candidates:
            if str(item.get("provider_id") or "").strip() != provider:
                continue
            item_keys = {
                str(item.get("model_id") or "").strip(),
                str(item.get("id") or "").strip(),
                str(item.get("qualified_model_id") or "").strip(),
            }
            item_model_id = str(item.get("model_id") or "").strip()
            if item_model_id:
                item_keys.add(f"{provider}/{item_model_id}")
            if lookup_keys.isdisjoint({key for key in item_keys if key}):
                continue
            thinking = item.get("thinking") if isinstance(item.get("thinking"), dict) else {}
            mapping = thinking.get("provider_mapping")
            return dict(mapping) if isinstance(mapping, dict) else None
        return None

    @staticmethod
    def _model_lookup_keys(provider_id: str, model_id: str) -> set[str]:
        provider = str(provider_id or "").strip().lower()
        raw_model = str(model_id or "").strip()
        keys = {raw_model} if raw_model else set()
        if provider and raw_model:
            qualified = f"{provider}/{raw_model}"
            keys.add(qualified)
            if raw_model.startswith(f"{provider}/"):
                keys.add(raw_model.split("/", 1)[1])
        return {key for key in keys if key}

    def default_model_settings(self) -> dict[str, Any]:
        return {
            "preferred_model": DEFAULT_MODEL,
            "model_slots": {
                MODEL_SLOT_MAIN: DEFAULT_MODEL,
                MODEL_SLOT_LIGHTWEIGHT: "",
            },
            "preferred_model_group": "default",
            "auto_route_within_group": True,
            "model_groups": default_model_groups(),
            "on_switch_to_non_vision_with_images": "auto_bridge",
            "thinking_level": DEFAULT_THINKING_LEVEL,
            "deepthink_enabled": DEFAULT_DEEPTHINK_ENABLED,
            "favorite_profiles": [DEFAULT_MODEL],
            "thinking_level_by_profile": {DEFAULT_MODEL: DEFAULT_THINKING_LEVEL},
            "thinking_level_by_conversation": {},
            "utility_models": normalize_utility_models({}),
            "utility_model_policy": normalize_utility_model_policy({}),
            "model_api_routes": "",
            "api_routes": [],
            "api_bound_profiles": [],
            "model_packs": [],
            "composite_models": [],
            "model_notes": {},
            "google_api_key": "",
            "google_api_key_configured": provider_has_api_key("google", pack_root=self._pack_root),
            "openrouter_api_key": "",
            "openrouter_api_key_configured": provider_has_api_key("openrouter", pack_root=self._pack_root),
        }

    def _runtime_rumi_base_model(self, settings: dict[str, Any] | None = None) -> str:
        effective_settings = settings if isinstance(settings, dict) else self.default_model_settings()
        default_profile_base_model = str(effective_settings.get("preferred_model") or DEFAULT_MODEL).strip()
        base_profiles = self._base_profile_catalog(settings)
        available_models: list[str] = []
        available_providers: set[str] = set()
        for profile in base_profiles:
            if not isinstance(profile, dict):
                continue
            if not self._is_real_chat_profile(profile):
                continue
            availability = profile.get("availability") if isinstance(profile.get("availability"), dict) else {}
            is_active = bool(
                availability.get("active")
                or availability.get("configured")
                or availability.get("local")
            )
            if not is_active:
                continue
            for key in ("profile_id", "qualified_model_id", "model_ref"):
                value = str(profile.get(key) or "").strip()
                if value:
                    available_models.append(value)
            provider_id = str(profile.get("provider_id") or profile.get("provider") or "").strip()
            if provider_id:
                available_providers.add(provider_id)
        try:
            from domain.ai_client.providers import detect_available_providers, get_all_known_models

            provider_map = detect_available_providers()
            available_providers.update(str(name or "").strip() for name in provider_map.keys() if str(name or "").strip())
            # Rumi base-model resolution only needs models from providers that
            # are actually available in this runtime.  Asking the catalog for
            # every manifest here makes a routine model-pack lookup walk every
            # unconfigured provider and repeatedly re-hash its metadata.
            for model in get_all_known_models(active_provider_ids=available_providers):
                if not isinstance(model, dict):
                    continue
                if not self._is_real_chat_profile(model):
                    continue
                provider_id = str(model.get("provider") or model.get("provider_id") or "").strip()
                model_id = str(model.get("id") or model.get("qualified_model_id") or "").strip()
                if provider_id and provider_id in available_providers and model_id:
                    available_models.append(model_id)
        except Exception:
            pass
        return resolve_rumi_base_model(
            available_models,
            available_providers=available_providers,
            default_profile_base_model=default_profile_base_model,
        )

    def _ensure_rumi_model_packs(self, model_packs: Any, *, settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return ensure_default_rumi_model_pack(
            model_packs,
            base_model=self._runtime_rumi_base_model(settings),
        )

    def sanitize_models_patch(
        self,
        patch: dict[str, Any],
        *,
        current_models: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sanitized = deepcopy(patch or {})
        current = current_models if isinstance(current_models, dict) else {}
        has_main_model = "main_model" in sanitized
        has_lightweight_model = "lightweight_model" in sanitized
        main_model = str(sanitized.pop("main_model", "") or "").strip()
        lightweight_model = str(sanitized.pop("lightweight_model", "") or "").strip()
        raw_slots = sanitized.get("model_slots")
        slots = dict(raw_slots) if isinstance(raw_slots, dict) else {}
        if not has_main_model and MODEL_SLOT_MAIN in slots:
            has_main_model = True
            main_model = str(slots.get(MODEL_SLOT_MAIN) or "").strip()
        if not has_lightweight_model and MODEL_SLOT_LIGHTWEIGHT in slots:
            has_lightweight_model = True
            lightweight_model = str(slots.get(MODEL_SLOT_LIGHTWEIGHT) or "").strip()
        if has_main_model:
            normalized_main = main_model or DEFAULT_MODEL
            sanitized["preferred_model"] = normalized_main
            slots[MODEL_SLOT_MAIN] = normalized_main
        elif "preferred_model" in sanitized:
            preferred = str(sanitized.get("preferred_model") or "").strip() or DEFAULT_MODEL
            sanitized["preferred_model"] = preferred
            slots[MODEL_SLOT_MAIN] = preferred
        if has_lightweight_model:
            slots[MODEL_SLOT_LIGHTWEIGHT] = lightweight_model
            utility_models = sanitized.get("utility_models")
            if isinstance(utility_models, str):
                try:
                    utility_models = json.loads(utility_models)
                except json.JSONDecodeError:
                    utility_models = {}
            current_utility = current.get("utility_models")
            utility_patch = dict(current_utility) if isinstance(current_utility, dict) else {}
            if isinstance(utility_models, dict):
                utility_patch.update(utility_models)
            utility_patch["fast_reply"] = lightweight_model
            utility_patch["subagent_default"] = lightweight_model
            sanitized["utility_models"] = utility_patch
        if slots:
            sanitized["model_slots"] = self._normalize_model_slots(slots)
        for provider_id, field_id, configured_field in (
            ("google", "google_api_key", "google_api_key_configured"),
            ("openrouter", "openrouter_api_key", "openrouter_api_key_configured"),
        ):
            raw_key = sanitized.pop(field_id, None)
            # Legacy model settings cannot carry a trusted approval context.
            # Discard submitted secret material and preserve status only; new
            # credentials must use the approved provider-key action.
            del raw_key
            sanitized[configured_field] = provider_has_api_key(
                provider_id, pack_root=self._pack_root
            )
            sanitized[field_id] = ""
        sanitized["model_api_routes"] = self._normalize_model_api_routes(
            sanitized.get("model_api_routes", "")
        )
        if "api_routes" in sanitized:
            sanitized["api_routes"] = self._normalize_api_routes(sanitized.get("api_routes"))
        if "api_bound_profiles" in sanitized:
            sanitized["api_bound_profiles"] = self._normalize_api_bound_profiles(sanitized.get("api_bound_profiles"))
        if "model_packs" in sanitized:
            sanitized["model_packs"] = self._ensure_rumi_model_packs(normalize_model_packs(
                sanitized.get("model_packs"),
                composite_models=sanitized.get("composite_models"),
            ))
        if "composite_models" in sanitized:
            sanitized["composite_models"] = self._normalize_composite_models(sanitized.get("composite_models"))
        if "model_notes" in sanitized:
            sanitized["model_notes"] = self._normalize_model_notes(sanitized.get("model_notes"))
        if "model_groups" in sanitized:
            sanitized["model_groups"] = normalize_model_groups(sanitized.get("model_groups"))
        if "utility_models" in sanitized:
            sanitized["utility_models"] = normalize_utility_models(sanitized.get("utility_models"))
        if "utility_model_policy" in sanitized:
            sanitized["utility_model_policy"] = normalize_utility_model_policy(sanitized.get("utility_model_policy"))
        if "preferred_model_group" in sanitized:
            sanitized["preferred_model_group"] = str(sanitized.get("preferred_model_group") or "default").strip() or "default"
        if "auto_route_within_group" in sanitized:
            sanitized["auto_route_within_group"] = bool(sanitized.get("auto_route_within_group"))
        if "deepthink_enabled" in sanitized:
            sanitized["deepthink_enabled"] = self._coerce_bool(
                sanitized.get("deepthink_enabled"),
                default=DEFAULT_DEEPTHINK_ENABLED,
            )
        if "on_switch_to_non_vision_with_images" in sanitized:
            policy = str(sanitized.get("on_switch_to_non_vision_with_images") or "auto_bridge").strip()
            sanitized["on_switch_to_non_vision_with_images"] = policy if policy in {"auto_bridge", "ask", "block", "ignore"} else "auto_bridge"
        return sanitized

    def refresh_models_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        models = self._deep_merge(self.default_model_settings(), values if isinstance(values, dict) else {})
        models["google_api_key"] = ""
        models["google_api_key_configured"] = provider_has_api_key("google", pack_root=self._pack_root)
        models["openrouter_api_key"] = ""
        models["openrouter_api_key_configured"] = provider_has_api_key("openrouter", pack_root=self._pack_root)

        favorite_profiles = models.get("favorite_profiles")
        if isinstance(favorite_profiles, str):
            try:
                favorite_profiles = json.loads(favorite_profiles)
            except json.JSONDecodeError:
                favorite_profiles = [line.strip() for line in favorite_profiles.splitlines()]
        if not isinstance(favorite_profiles, list):
            preferred = str(models.get("preferred_model") or DEFAULT_MODEL).strip()
            favorite_profiles = [preferred] if preferred else ["stub/default"]
        normalized_favorites: list[str] = []
        for item in favorite_profiles:
            profile_id = str(item or "").strip()
            if profile_id in LEGACY_CLOUD_DEFAULT_MODELS:
                continue
            if profile_id and profile_id not in normalized_favorites:
                normalized_favorites.append(profile_id)
        preferred_model = str(models.get("preferred_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        if preferred_model in LEGACY_CLOUD_DEFAULT_MODELS:
            preferred_model = DEFAULT_MODEL
        if preferred_model not in normalized_favorites:
            normalized_favorites.insert(0, preferred_model)
        models["preferred_model"] = preferred_model
        models["favorite_profiles"] = normalized_favorites or ["stub/default"]

        for key in ("thinking_level_by_profile", "thinking_level_by_conversation"):
            values_by_scope = models.get(key)
            if isinstance(values_by_scope, str):
                try:
                    values_by_scope = json.loads(values_by_scope)
                except json.JSONDecodeError:
                    values_by_scope = {}
            models[key] = values_by_scope if isinstance(values_by_scope, dict) else {}
        models["thinking_level"] = self._normalize_level(models.get("thinking_level"))
        models["deepthink_enabled"] = self._coerce_bool(
            models.get("deepthink_enabled"),
            default=DEFAULT_DEEPTHINK_ENABLED,
        )
        models["model_api_routes"] = self._normalize_model_api_routes(models.get("model_api_routes", ""))
        models["api_routes"] = self._normalize_api_routes(models.get("api_routes"))
        models["api_bound_profiles"] = self._normalize_api_bound_profiles(models.get("api_bound_profiles"))
        models["composite_models"] = self._normalize_composite_models(models.get("composite_models"))
        models["model_packs"] = self._ensure_rumi_model_packs(normalize_model_packs(
            models.get("model_packs"),
            composite_models=models.get("composite_models"),
        ), settings=models)
        models["model_notes"] = self._normalize_model_notes(models.get("model_notes"))
        models["preferred_model_group"] = str(models.get("preferred_model_group") or "default").strip() or "default"
        models["auto_route_within_group"] = bool(models.get("auto_route_within_group", True))
        models["model_groups"] = normalize_model_groups(models.get("model_groups"))
        models["utility_models"] = normalize_utility_models(models.get("utility_models"))
        lightweight_model = str(
            models["utility_models"].get("fast_reply")
            or models["utility_models"].get("subagent_default")
            or ""
        ).strip()
        models["model_slots"] = {
            MODEL_SLOT_MAIN: preferred_model,
            MODEL_SLOT_LIGHTWEIGHT: lightweight_model,
        }
        # Flat aliases keep the simple settings controls independent from the
        # normalized model_slots and established runtime keys.
        models["main_model"] = preferred_model
        models["lightweight_model"] = lightweight_model
        models["utility_model_policy"] = normalize_utility_model_policy(models.get("utility_model_policy"))
        switch_policy = str(models.get("on_switch_to_non_vision_with_images") or "auto_bridge").strip()
        models["on_switch_to_non_vision_with_images"] = switch_policy if switch_policy in {"auto_bridge", "ask", "block", "ignore"} else "auto_bridge"
        return models

    @staticmethod
    def _normalize_model_slots(value: Any) -> dict[str, str]:
        slots = value if isinstance(value, dict) else {}
        return {
            MODEL_SLOT_MAIN: str(slots.get(MODEL_SLOT_MAIN) or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            MODEL_SLOT_LIGHTWEIGHT: str(slots.get(MODEL_SLOT_LIGHTWEIGHT) or "").strip(),
        }

    @staticmethod
    def _normalize_model_api_routes(value: Any) -> str:
        if isinstance(value, list):
            lines = [str(item).strip() for item in value if str(item).strip()]
        else:
            lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _parse_jsonish(value: Any, fallback: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return fallback
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return fallback
        return value

    @classmethod
    def _normalize_model_notes(cls, value: Any) -> dict[str, str]:
        parsed = cls._parse_jsonish(value, {})
        if not isinstance(parsed, dict):
            return {}
        return {
            str(key).strip(): str(note or "").strip()
            for key, note in parsed.items()
            if str(key).strip() and str(note or "").strip()
        }

    @classmethod
    def _normalize_api_routes(cls, value: Any) -> list[dict[str, Any]]:
        parsed = cls._parse_jsonish(value, [])
        if isinstance(parsed, dict):
            raw_items = [
                {"model": key, **(route if isinstance(route, dict) else {"routes": route})}
                for key, route in parsed.items()
            ]
        elif isinstance(parsed, list):
            raw_items = parsed
        else:
            raw_items = []
        normalized: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            model = str(item.get("model") or item.get("model_ref") or item.get("profile_id") or "").strip()
            if not model:
                continue
            routes = item.get("routes", item.get("apis", item.get("api_refs", [])))
            if isinstance(routes, str):
                route_refs = [part.strip() for part in routes.split(",") if part.strip()]
            elif isinstance(routes, list):
                route_refs = [str(part).strip() for part in routes if str(part or "").strip()]
            else:
                route_refs = []
            if not route_refs:
                continue
            fallback_on = item.get("fallback_on", item.get("retry_on", []))
            if isinstance(fallback_on, str):
                fallback_values = [part.strip() for part in fallback_on.split(",") if part.strip()]
            elif isinstance(fallback_on, list):
                fallback_values = [str(part).strip() for part in fallback_on if str(part or "").strip()]
            else:
                fallback_values = []
            normalized.append(
                {
                    "model": model,
                    "routes": route_refs,
                    "fallback_on": fallback_values or ["429", "quota", "rate_limit", "provider_error", "timeout"],
                }
            )
        return normalized

    @classmethod
    def _normalize_api_bound_profiles(cls, value: Any) -> list[dict[str, Any]]:
        parsed = cls._parse_jsonish(value, [])
        if isinstance(parsed, dict):
            raw_items = list(parsed.values())
        elif isinstance(parsed, list):
            raw_items = parsed
        else:
            raw_items = []
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            provider_id = str(item.get("provider_id") or item.get("provider") or "").strip()
            api_id = str(item.get("api_id") or item.get("api") or "").strip()
            model_id = str(item.get("model_id") or item.get("model") or "").strip()
            if not provider_id or not api_id or not model_id:
                continue
            profile_id = str(item.get("profile_id") or f"{provider_id}/{api_id}/{model_id}").strip()
            if profile_id in seen:
                continue
            seen.add(profile_id)
            normalized.append(
                {
                    "profile_id": profile_id,
                    "qualified_model_id": profile_id,
                    "provider_id": provider_id,
                    "api_id": api_id,
                    "model_id": model_id,
                    "display_name": str(item.get("display_name") or item.get("name") or f"{model_id} ({api_id})").strip(),
                    "notes": str(item.get("notes") or "").strip(),
                    "enabled": item.get("enabled", True) is not False,
                }
            )
        return normalized

    @classmethod
    def _normalize_composite_models(cls, value: Any) -> list[dict[str, Any]]:
        parsed = cls._parse_jsonish(value, [])
        if isinstance(parsed, dict):
            raw_items = [
                {"id": key, **(item if isinstance(item, dict) else {})}
                for key, item in parsed.items()
            ]
        elif isinstance(parsed, list):
            raw_items = parsed
        else:
            raw_items = []
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            composite_id = str(item.get("id") or item.get("profile_id") or item.get("name") or "").strip()
            mode = str(item.get("mode") or item.get("type") or "fallback_chain").strip()
            if not composite_id or composite_id in seen or mode not in {"fallback_chain", "ensemble", "review_chain"}:
                continue
            raw_members = item.get("members", item.get("models", item.get("chain", [])))
            if isinstance(raw_members, str):
                members = [{"model": part.strip()} for part in raw_members.split(",") if part.strip()]
            elif isinstance(raw_members, list):
                members = [
                    part if isinstance(part, dict) else {"model": str(part or "").strip()}
                    for part in raw_members
                    if (isinstance(part, dict) or str(part or "").strip())
                ]
            else:
                members = []
            cleaned_members = []
            for member in members:
                model = str(member.get("model") or member.get("profile_id") or "").strip()
                if not model:
                    continue
                cleaned_members.append(
                    {
                        **dict(member),
                        "model": model,
                        "when": member.get("when") if isinstance(member.get("when"), dict) else {},
                        "fallback_on": member.get("fallback_on") if isinstance(member.get("fallback_on"), list) else [],
                    }
                )
            if not cleaned_members:
                continue
            normalized.append(
                {
                    "id": composite_id,
                    "profile_id": composite_id,
                    "display_name": str(item.get("display_name") or item.get("label") or composite_id).strip(),
                    "mode": mode,
                    "members": cleaned_members,
                    "merge_model": str(item.get("merge_model") or item.get("synthesizer_model") or "").strip(),
                    "conditions": item.get("conditions") if isinstance(item.get("conditions"), dict) else {},
                    "notes": str(item.get("notes") or "").strip(),
                    "enabled": item.get("enabled", True) is not False,
                }
            )
            seen.add(composite_id)
        return normalized

    def _read_all(self) -> dict[str, Any]:
        values: dict[str, Any] = {"models": self.default_model_settings()}
        saved = self._settings_store.read()
        values = self._deep_merge(values, saved)
        values["models"] = self.refresh_models_settings(values.get("models", {}))
        return values

    def _normalize_level(self, value: Any) -> str:
        level = str(value or "").strip()
        return level if level in VALID_THINKING_LEVELS else DEFAULT_THINKING_LEVEL

    @staticmethod
    def _coerce_bool(value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
        return default

    def runtime_defined_profiles(self, settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        settings = settings if isinstance(settings, dict) else self.get_settings()
        model_notes = self._normalize_model_notes(settings.get("model_notes"))
        profiles: list[dict[str, Any]] = []
        for profile in self._normalize_api_bound_profiles(settings.get("api_bound_profiles")):
            if profile.get("enabled") is False:
                continue
            provider_id = str(profile.get("provider_id") or "")
            model_id = str(profile.get("model_id") or "")
            api_id = str(profile.get("api_id") or "")
            profile_id = str(profile.get("profile_id") or f"{provider_id}/{api_id}/{model_id}")
            availability = self._api_bound_profile_availability(provider_id, api_id)
            metadata = provider_api_metadata(provider_id, api_id, pack_root=self._pack_root)
            profiles.append(
                {
                    "id": profile_id,
                    "profile_id": profile_id,
                    "qualified_model_id": profile_id,
                    "provider_id": provider_id,
                    "provider": provider_id,
                    "model_id": model_id,
                    "model": model_id,
                    "display_name": str(profile.get("display_name") or f"{model_id} ({api_id})"),
                    "name": str(profile.get("display_name") or model_id),
                    "type": "chat",
                    "configured": availability["configured"],
                    "availability": availability,
                    "metadata": {
                        "api_bound": True,
                        "api_id": api_id,
                        "base_url": str(metadata.get("base_url") or ""),
                        "quota_label": str(metadata.get("quota_label") or ""),
                        "notes": str(profile.get("notes") or model_notes.get(profile_id) or model_notes.get(f"{provider_id}/{model_id}") or ""),
                    },
                }
            )
        for composite in self._normalize_composite_models(settings.get("composite_models")):
            if composite.get("enabled") is False:
                continue
            profile_id = str(composite.get("profile_id") or composite.get("id") or "")
            profiles.append(
                {
                    "id": profile_id,
                    "profile_id": profile_id,
                    "qualified_model_id": profile_id,
                    "provider_id": "composite",
                    "provider": "composite",
                    "model_id": profile_id,
                    "model": profile_id,
                    "display_name": str(composite.get("display_name") or profile_id),
                    "name": str(composite.get("display_name") or profile_id),
                    "type": "chat",
                    "configured": True,
                    "availability": {
                        "configured": True,
                        "active": True,
                        "status": "configured",
                        "composite": True,
                    },
                    "metadata": {
                        "composite": True,
                        "mode": composite.get("mode"),
                        "notes": str(composite.get("notes") or model_notes.get(profile_id) or ""),
                    },
                }
            )
        for model_pack in self._ensure_rumi_model_packs(normalize_model_packs(
            settings.get("model_packs"),
            composite_models=settings.get("composite_models"),
        ), settings=settings):
            profile_id = "modelpack/{}".format(str(model_pack.get("id") or "").strip())
            if not profile_id or profile_id == "modelpack/":
                continue
            member_ids = [
                str(member.get("model") or "")
                for member in (model_pack.get("members") if isinstance(model_pack.get("members"), list) else [])
                if isinstance(member, dict) and str(member.get("model") or "").strip()
            ]
            base_profiles = self._base_profile_catalog(settings)
            member_profiles = [
                self._public_candidate(self._candidate_from_profile(profile, set()))
                for profile in base_profiles
                if isinstance(profile, dict)
                and str(profile.get("profile_id") or profile.get("qualified_model_id") or "") in set(member_ids)
            ]
            is_rumi_pack = str(model_pack.get("id") or "").strip() == RUMI_MODEL_PACK_ID
            if is_rumi_pack:
                configured = bool(member_profiles) and any(bool(member.get("configured")) for member in member_profiles)
                availability = {
                    "configured": configured,
                    "active": configured,
                    "status": "configured" if configured else "missing_member_model",
                    "model_pack": True,
                }
            else:
                configured = any(bool(member.get("configured")) for member in member_profiles) if member_profiles else True
                availability = {
                    "configured": True,
                    "active": True,
                    "status": "configured",
                    "model_pack": True,
                }
            def _member_supports(member: dict[str, Any], key: str) -> bool:
                if bool(member.get(key)):
                    return True
                metadata = member.get("metadata") if isinstance(member.get("metadata"), dict) else {}
                if bool(metadata.get(key)):
                    return True
                capabilities = metadata.get("capabilities") if isinstance(metadata.get("capabilities"), dict) else {}
                capability_aliases = {
                    "supports_vision": ("vision", "image_input"),
                    "supports_image_input": ("image_input", "vision"),
                    "supports_tool_calling": ("tool_calls", "tool_calling"),
                    "supports_thinking": ("thinking",),
                    "supports_fast": ("fast",),
                }
                return any(bool(capabilities.get(alias)) for alias in capability_aliases.get(key, ()))
            profiles.append(
                {
                    "id": profile_id,
                    "profile_id": profile_id,
                    "qualified_model_id": profile_id,
                    "provider_id": "modelpack",
                    "provider": "modelpack",
                    "model_id": str(model_pack.get("id") or ""),
                    "model": str(model_pack.get("id") or ""),
                    "display_name": str(model_pack.get("display_name") or model_pack.get("id") or ""),
                    "name": str(model_pack.get("display_name") or model_pack.get("id") or ""),
                    "type": "chat",
                    "configured": configured,
                    "supports_vision": any(_member_supports(member, "supports_vision") for member in member_profiles),
                    "supports_image_input": any(_member_supports(member, "supports_image_input") for member in member_profiles),
                    "supports_tool_calling": any(_member_supports(member, "supports_tool_calling") for member in member_profiles),
                    "supports_thinking": any(_member_supports(member, "supports_thinking") for member in member_profiles),
                    "supports_fast": any(_member_supports(member, "supports_fast") for member in member_profiles),
                    "capability_tags": sorted(
                        {
                            tag
                            for member in member_profiles
                            for tag in (
                                member.get("capability_tags")
                                if isinstance(member.get("capability_tags"), list)
                                else (
                                    (member.get("metadata") or {}).get("capability_tags")
                                    if isinstance((member.get("metadata") or {}).get("capability_tags"), list)
                                    else []
                                )
                            )
                            if str(tag).strip()
                        }
                    ),
                    "availability": availability,
                    "metadata": {
                        "model_pack": True,
                        "source": model_pack.get("source"),
                        "mode": model_pack.get("mode"),
                        "members": member_ids,
                    },
                }
            )
        return profiles

    def _list_profile_catalog(self, settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        profiles = self._base_profile_catalog(settings)
        if profiles:
            combined = [profile for profile in profiles if isinstance(profile, dict)]
            combined.extend(self.runtime_defined_profiles(settings))
            return combined
        return [self._fallback_stub_profile(), *self.runtime_defined_profiles(settings)]

    def _base_profile_catalog(self, settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        del settings
        try:
            from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_profile_catalog
        except ModuleNotFoundError:
            try:
                from backend.ai_client.provider_catalog import list_profile_catalog
            except ModuleNotFoundError:
                list_profile_catalog = None
        if list_profile_catalog is not None:
            try:
                profiles = list_profile_catalog()
                if isinstance(profiles, list) and profiles:
                    return [profile for profile in profiles if isinstance(profile, dict)]
            except Exception:
                pass
        return [self._fallback_stub_profile()]

    def _api_bound_profile_availability(self, provider_id: str, api_id: str) -> dict[str, Any]:
        named_key = next(
            (
                item
                for item in provider_named_api_keys(provider_id, pack_root=self._pack_root)
                if str(item.get("api_id") or "").strip() == api_id
            ),
            None,
        )
        configured = bool(
            named_key
            and named_key.get("configured")
            and read_provider_api_key(provider_id, api_id, pack_root=self._pack_root)
        )
        return {
            "configured": configured,
            "active": configured,
            "status": "configured" if configured else "missing_api_key",
            "api_bound": True,
        }

    def _list_profile_catalog_for_resolution(self, settings: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return self._list_profile_catalog(settings=settings)
        except TypeError as exc:
            if "unexpected keyword argument 'settings'" not in str(exc):
                raise
            return self._list_profile_catalog()

    @staticmethod
    def _fallback_stub_profile() -> dict[str, Any]:
        return {
            "id": DEFAULT_MODEL,
            "profile_id": DEFAULT_MODEL,
            "qualified_model_id": DEFAULT_MODEL,
            "provider_id": "stub",
            "provider": "stub",
            "provider_display_name": "Stub",
            "model_id": "default",
            "model": "default",
            "display_name": "Stub Default",
            "name": "Stub Default",
            "availability": {
                "active": True,
                "configured": True,
                "local": True,
                "status": "configured",
            },
        }

    @staticmethod
    def _is_chat_profile(profile: dict[str, Any]) -> bool:
        model_type = str(profile.get("type") or "chat").strip().lower()
        if not model_type or model_type == "chat":
            return True
        if model_type != "reasoning":
            return False

        defaults = profile.get("defaults") if isinstance(profile.get("defaults"), dict) else {}
        raw_capabilities = profile.get("capabilities")
        if isinstance(raw_capabilities, dict):
            capabilities = dict(raw_capabilities)
        elif isinstance(raw_capabilities, (list, tuple, set)):
            capabilities = {str(item): True for item in raw_capabilities if str(item or "").strip()}
        else:
            capabilities = {}
        metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
        metadata_capabilities = (
            metadata.get("capabilities")
            if isinstance(metadata.get("capabilities"), dict)
            else {}
        )
        return bool(
            defaults.get("chat")
            or capabilities.get("chat")
            or capabilities.get("text")
            or metadata_capabilities.get("chat")
            or metadata_capabilities.get("text")
        )

    @classmethod
    def _is_real_chat_profile(cls, profile: dict[str, Any]) -> bool:
        if not cls._is_chat_profile(profile):
            return False
        provider_id = str(profile.get("provider_id") or profile.get("provider") or "").strip().lower()
        profile_id = str(
            profile.get("profile_id")
            or profile.get("qualified_model_id")
            or profile.get("id")
            or ""
        ).strip().lower()
        metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
        if provider_id in {"stub", "rumi", "modelpack", "composite", "synthetic"}:
            return False
        if profile_id == DEFAULT_MODEL or profile_id.startswith("modelpack/"):
            return False
        return not bool(metadata.get("model_pack") or metadata.get("composite") or metadata.get("synthetic"))

    def _candidate_from_profile(self, profile: dict[str, Any], favorites: set[str]) -> dict[str, Any]:
        profile_id = str(profile.get("profile_id") or profile.get("id") or profile.get("qualified_model_id") or "").strip()
        qualified_model_id = str(profile.get("qualified_model_id") or profile_id).strip()
        provider_id = str(profile.get("provider_id") or profile.get("provider") or "").strip()
        model_id = str(profile.get("model_id") or profile.get("model") or "").strip()
        if not provider_id and qualified_model_id and "/" in qualified_model_id:
            provider_id, model_id_from_qualified = qualified_model_id.split("/", 1)
            model_id = model_id or model_id_from_qualified
        if not model_id and qualified_model_id and "/" in qualified_model_id:
            _provider_id, model_id = qualified_model_id.split("/", 1)
        if not qualified_model_id and provider_id and model_id:
            qualified_model_id = f"{provider_id}/{model_id}"
        if not profile_id:
            profile_id = qualified_model_id

        availability = profile.get("availability") if isinstance(profile.get("availability"), dict) else {}
        metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
        provider_display_name = str(
            profile.get("provider_display_name")
            or profile.get("provider_name")
            or provider_id
            or ""
        ).strip()
        display_name = str(
            profile.get("display_name")
            or profile.get("name")
            or profile.get("disambiguated_name")
            or model_id
            or profile_id
        ).strip()
        label = f"{provider_display_name} / {display_name}" if provider_display_name else display_name
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
        requires_api_key = bool(provider_id and provider_id not in {"stub", "rumi"} and not local and not configured)
        favorite = any(
            item in favorites
            for item in {
                profile_id,
                qualified_model_id,
                model_id,
                f"{provider_id}/{model_id}" if provider_id and model_id else "",
            }
            if item
        )

        return {
            "profile_id": profile_id,
            "qualified_model_id": qualified_model_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "display_name": display_name,
            "provider_display_name": provider_display_name,
            "configured": configured,
            "local": local,
            "requires_api_key": requires_api_key,
            "api_key_required": requires_api_key,
            "api_key_configured": configured,
            "availability": deepcopy(availability),
            "metadata": deepcopy(metadata),
            "notes": str(profile.get("notes") or metadata.get("notes") or "").strip(),
            "type": str(profile.get("type") or "chat"),
            "favorite": favorite,
            "label": label,
            "disambiguated_name": str(profile.get("disambiguated_name") or "").strip(),
            "score": 0,
        }

    def _candidate_match(self, candidate: dict[str, Any], query: str) -> tuple[str, int]:
        normalized_query = self._normalize_search_key(query)
        if not normalized_query:
            return "", 0

        provider_id = str(candidate.get("provider_id") or "").strip()
        model_id = str(candidate.get("model_id") or "").strip()
        provider_display_name = str(candidate.get("provider_display_name") or "").strip()
        provider_model_id = f"{provider_id}/{model_id}" if provider_id and model_id else ""
        provider_display_model_id = f"{provider_display_name}/{model_id}" if provider_display_name and model_id else ""

        exact_id_fields = {
            str(candidate.get("profile_id") or ""),
            str(candidate.get("qualified_model_id") or ""),
        }
        exact_fields = {
            str(candidate.get("display_name") or ""),
            str(candidate.get("model_id") or ""),
            provider_model_id,
            provider_display_model_id,
            str(candidate.get("label") or ""),
            str(candidate.get("disambiguated_name") or ""),
        }
        search_fields = exact_id_fields | exact_fields | {
            provider_id,
            provider_display_name,
            str(candidate.get("notes") or ""),
            str((candidate.get("metadata") or {}).get("notes") if isinstance(candidate.get("metadata"), dict) else ""),
        }
        normalized_exact_ids = {self._normalize_search_key(item) for item in exact_id_fields if item}
        normalized_exact_fields = {self._normalize_search_key(item) for item in exact_fields if item}
        normalized_search_fields = {
            self._normalize_search_key(item)
            for item in search_fields
            if item and self._normalize_search_key(item)
        }

        if normalized_query in normalized_exact_ids:
            return "exact_id", 1000
        if normalized_query in normalized_exact_fields:
            return "exact_field", 950
        if any(item.startswith(normalized_query) for item in normalized_search_fields):
            return "prefix", 700
        if any(normalized_query in item for item in normalized_search_fields):
            return "substring", 500
        return "", 0

    @staticmethod
    def _candidate_score(candidate: dict[str, Any], base_score: int) -> int:
        return (
            base_score
            + (24 if candidate.get("configured") else 0)
            + (12 if candidate.get("local") else 0)
            + (6 if candidate.get("favorite") else 0)
        )

    @staticmethod
    def _normalize_search_key(value: Any) -> str:
        return " ".join(str(value or "").strip().casefold().split())

    @staticmethod
    def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            key: deepcopy(value)
            for key, value in candidate.items()
            if not str(key).startswith("_")
        }

    def _deep_merge(self, base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(base)
        for key, value in (patch or {}).items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result
