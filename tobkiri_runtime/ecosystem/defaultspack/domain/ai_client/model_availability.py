from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from domain.ai_client.api_key_store import (
    provider_api_metadata,
    provider_named_api_keys,
    read_provider_api_key,
)
from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService


class ModelAvailabilityService:
    """Single merge point for model visibility after provider credential changes."""

    def __init__(self, pack_root: Path | None = None) -> None:
        self._pack_root = pack_root
        self._settings = ModelRuntimeSettingsService(pack_root)

    def snapshot(self) -> dict[str, Any]:
        settings = self._settings.get_settings()
        profiles = self._profile_catalog(settings)
        return {
            "profiles": profiles,
            "runtime_profiles": self._settings.runtime_defined_profiles(settings),
            "model_groups": settings.get("model_groups", []),
            "api_routes": settings.get("api_routes", []),
            "api_bound_profiles": settings.get("api_bound_profiles", []),
        }

    def after_provider_key_saved(
        self,
        provider_id: str,
        api_id: str,
        *,
        default_model: str = "",
        allowed_models: Any = None,
    ) -> dict[str, Any]:
        provider = str(provider_id or "").strip()
        api = str(api_id or "").strip() or "default"
        explicit_models = self._explicit_models(default_model, allowed_models)
        # A successful inventory discovery is sufficient to make a connection
        # usable.  Requiring users to paste every returned model id into the
        # Settings form defeats live model discovery and leaves valid provider
        # models invisible immediately after a key is saved.
        model_ids = explicit_models or self._live_model_ids(provider, api)
        if model_ids:
            settings = self._upsert_api_bound_profiles(provider, api, model_ids)
        else:
            settings = self._settings.get_settings()
        available = self._available_api_bound_profiles_for_provider(provider, api, settings)
        if available:
            selected = self._selected_profile_id(available, settings)
            return {
                "status": "models_available",
                "profiles": available,
                "selected_profile_id": selected,
            }

        candidates = self._candidate_models(provider)
        return {
            "status": "route_required",
            "provider_id": provider,
            "api_id": api,
            "candidate_models": candidates,
            "reason": "Choose a default model or allowed models for this API key before it can appear in the composer.",
        }

    def _profile_catalog(self, settings: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            return self._settings._list_profile_catalog_for_resolution(settings)  # noqa: SLF001
        except Exception:
            return self._settings.runtime_defined_profiles(settings)

    def _available_api_bound_profiles_for_provider(
        self,
        provider_id: str,
        api_id: str,
        settings: dict[str, Any],
    ) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for profile in self._settings.runtime_defined_profiles(settings):
            if not isinstance(profile, dict):
                continue
            if not self._provider_matches(profile, provider_id):
                continue
            if not self._profile_is_configured(profile):
                continue
            metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
            profile_api_id = str(metadata.get("api_id") or profile.get("api_id") or "").strip()
            if profile_api_id and profile_api_id != api_id:
                continue
            profile_id = str(
                profile.get("profile_id")
                or profile.get("qualified_model_id")
                or profile.get("id")
                or ""
            ).strip()
            if not profile_id or profile_id in seen:
                continue
            public = dict(profile)
            public["profile_id"] = profile_id
            public.setdefault("qualified_model_id", profile_id)
            public.setdefault("display_name", profile_id)
            profiles.append(public)
            seen.add(profile_id)
        return profiles

    @staticmethod
    def _provider_matches(profile: dict[str, Any], provider_id: str) -> bool:
        return provider_id in {
            str(profile.get("provider_id") or "").strip(),
            str(profile.get("provider") or "").strip(),
            str(
                profile.get("metadata", {}).get("provider_id")
                if isinstance(profile.get("metadata"), dict)
                else ""
            ).strip(),
        }

    @staticmethod
    def _profile_is_configured(profile: dict[str, Any]) -> bool:
        availability = (
            profile.get("availability") if isinstance(profile.get("availability"), dict) else {}
        )
        if availability:
            return availability.get("configured") is True or availability.get("active") is True
        return profile.get("configured") is True

    def _upsert_api_bound_profiles(
        self, provider_id: str, api_id: str, model_ids: list[str]
    ) -> dict[str, Any]:
        settings = self._settings.get_settings()
        existing = [
            dict(item) for item in settings.get("api_bound_profiles", []) if isinstance(item, dict)
        ]
        by_profile_id = {
            str(item.get("profile_id") or "").strip(): item
            for item in existing
            if str(item.get("profile_id") or "").strip()
        }
        for model_id in model_ids:
            profile_id = f"{provider_id}/{api_id}/{model_id}"
            by_profile_id[profile_id] = {
                **by_profile_id.get(profile_id, {}),
                "profile_id": profile_id,
                "qualified_model_id": profile_id,
                "provider_id": provider_id,
                "api_id": api_id,
                "model_id": model_id,
                "display_name": f"{model_id} ({api_id})",
                "enabled": True,
            }
        return self._settings.update_settings({"api_bound_profiles": list(by_profile_id.values())})

    @staticmethod
    def _explicit_models(default_model: str, allowed_models: Any) -> list[str]:
        values: list[str] = []
        for item in [default_model, *(_as_list(allowed_models))]:
            model_id = str(item or "").strip()
            if model_id and model_id not in values:
                values.append(model_id)
        return values

    @staticmethod
    def _selected_profile_id(profiles: list[dict[str, Any]], settings: dict[str, Any]) -> str:
        preferred = str(settings.get("preferred_model") or "").strip()
        profile_ids = [str(profile.get("profile_id") or "").strip() for profile in profiles]
        if preferred in profile_ids:
            return preferred
        return profile_ids[0] if profile_ids else ""

    def _candidate_models(self, provider_id: str) -> list[dict[str, Any]]:
        models = self._catalog_models(provider_id)
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = str(
                model.get("model_id") or model.get("canonical_model_id") or model.get("id") or ""
            ).strip()
            if not model_id or model_id in seen:
                continue
            candidates.append(
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "profile_id": str(
                        model.get("qualified_model_id") or f"{provider_id}/{model_id}"
                    ),
                    "label": str(model.get("display_name") or model.get("name") or model_id),
                }
            )
            seen.add(model_id)
            if len(candidates) >= 12:
                break
        return candidates

    def _live_model_ids(self, provider_id: str, api_id: str = "") -> list[str]:
        model_ids: list[str] = []
        models = (
            self._connection_models(provider_id, api_id)
            if api_id
            else self._catalog_models(provider_id)
        )
        if api_id and not models:
            configured_connections = [
                item
                for item in provider_named_api_keys(provider_id, pack_root=self._pack_root)
                if isinstance(item, dict) and item.get("configured")
            ]
            # The provider-wide live inventory is safe as a fallback only when
            # there is exactly one configured connection.  With multiple
            # accounts/endpoints we fail closed rather than binding one
            # connection to another connection's model IDs.
            if len(configured_connections) == 1:
                models = self._catalog_models(provider_id)
        for model in models:
            if not isinstance(model, dict):
                continue
            metadata = model.get("metadata") if isinstance(model.get("metadata"), dict) else {}
            source = str(metadata.get("source") or "").strip().lower()
            # Never promote the small offline overlay as if it were an account
            # inventory.  It remains available as a candidate when discovery is
            # unavailable, while a live/native/last-known-good catalog is added
            # in full without a Settings-side model list.
            if source and source not in {
                "remote_models_endpoint",
                "openai_models_endpoint",
                "openrouter_models_api",
                "vercel_gateway_models_api",
                "vercel_ai_gateway_models_api",
                "native_server_api",
                "native_models_endpoint",
                "last_known_good_inventory",
            }:
                continue
            model_id = str(model.get("model_id") or model.get("canonical_model_id") or "").strip()
            if model_id and model_id not in model_ids:
                model_ids.append(model_id)
        return model_ids

    def _connection_models(self, provider_id: str, api_id: str) -> list[dict[str, Any]]:
        """Discover against exactly the connection that was just saved.

        The provider runtime normally selects one configured connection as its
        default.  That is useful for a simple setup, but it is incorrect for
        settings-time discovery: an account/project endpoint must never inherit
        the model IDs of another saved key.  Work on a shallow provider copy so
        the active runtime keeps its selected default unchanged.
        """
        provider_name = str(provider_id or "").strip()
        connection_name = str(api_id or "").strip()
        if not provider_name or not connection_name:
            return []
        try:
            from domain.ai_client.client import AIClient

            runtime_provider = AIClient()._providers.get(provider_name)  # noqa: SLF001
        except Exception:
            runtime_provider = None
        if runtime_provider is None or not callable(getattr(runtime_provider, "list_models", None)):
            return []

        metadata = provider_api_metadata(provider_name, connection_name, pack_root=self._pack_root)
        api_key = read_provider_api_key(provider_name, connection_name, pack_root=self._pack_root)
        credential_mode = str(metadata.get("credential_mode") or "").strip().lower()
        if not api_key and credential_mode != "none":
            return []
        provider = copy.copy(runtime_provider)
        base_url = str(metadata.get("base_url") or "").strip().rstrip("/")
        try:
            if hasattr(provider, "_api_key"):
                provider._api_key = api_key or ""
            if base_url:
                if hasattr(provider, "_base_url"):
                    provider._base_url = base_url
                if hasattr(provider, "BASE_URL"):
                    provider.BASE_URL = base_url
            models = provider.list_models()
        except Exception:
            return []
        return [dict(model) for model in models if isinstance(model, dict)]

    @staticmethod
    def _catalog_models(provider_id: str) -> list[dict[str, Any]]:
        # This post-save path must not rebuild every provider manifest merely
        # to tell one connection whether models are available.  The active
        # runtime already owns this provider's last-known-good/live inventory.
        try:
            from domain.ai_client.client import AIClient

            return [
                dict(model)
                for model in AIClient().list_models(provider=provider_id)
                if isinstance(model, dict)
            ]
        except Exception:
            pass
        try:
            from ecosystem.defaultspack.backend.ai_client.provider_catalog import list_model_catalog
        except ModuleNotFoundError:
            try:
                from backend.ai_client.provider_catalog import list_model_catalog
            except ModuleNotFoundError:
                return []
        try:
            models = list_model_catalog(provider=provider_id)
        except Exception:
            return []
        return [dict(model) for model in models if isinstance(model, dict)]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        return [item.strip() for item in value.replace(",", "\n").splitlines() if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []
