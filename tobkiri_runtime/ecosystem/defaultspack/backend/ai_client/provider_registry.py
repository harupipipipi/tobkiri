"""Provider registry for provider adapters and model profiles."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from .ai_profile import ModelProfile, ModelProfileManager
from ...domain.ai_client.providers import (
    build_profile_catalog,
    get_all_known_models,
    get_provider_catalog,
)

if TYPE_CHECKING:
    from .router import ModelRouter


class ProviderRegistry:
    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir is not None else None
        self._lock = threading.RLock()
        self._providers: Dict[str, Any] = {}
        self._profiles = ModelProfileManager(self.storage_dir / "profiles" if self.storage_dir is not None else None)
        self._model_uuid_map: Dict[str, str] = {}
        self._provider_models: Dict[str, List[str]] = {}
        self._router: ModelRouter | None = None
        if self.storage_dir is not None:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            self._load_index()

    @property
    def router(self):
        if self._router is None:
            from .router import ModelRouter

            self._router = ModelRouter(self)
        return self._router

    def _index_path(self) -> Optional[Path]:
        if self.storage_dir is None:
            return None
        return self.storage_dir / "index.json"

    def _load_index(self) -> None:
        path = self._index_path()
        if path is None or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        self._model_uuid_map = dict(data.get("model_uuid_map", {}))
        self._provider_models = {
            provider_id: list(models)
            for provider_id, models in data.get("provider_models", {}).items()
        }

    def _save_index(self) -> None:
        path = self._index_path()
        if path is None:
            return
        payload = {
            "model_uuid_map": self._model_uuid_map,
            "provider_models": self._provider_models,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _provider_id(provider: Any) -> str:
        provider_id = getattr(provider, "provider_id", "") or getattr(provider, "id", "")
        if not provider_id:
            raise ValueError("provider.provider_id is required")
        return provider_id

    @staticmethod
    def _coerce_profile(profile: Any, provider_id: str = "") -> ModelProfile:
        if isinstance(profile, ModelProfile):
            if provider_id and not profile.provider_id:
                profile.provider_id = provider_id
            return profile
        if isinstance(profile, dict):
            profile_dict = dict(profile)
            if provider_id and not profile_dict.get("provider_id"):
                profile_dict["provider_id"] = provider_id
            return ModelProfile.from_dict(profile_dict)
        if isinstance(profile, str):
            return ModelProfile(provider_id=provider_id, model_id=profile, model_name=profile, profile_id=profile)
        raise TypeError(f"Unsupported profile type: {type(profile)!r}")

    def register_provider(self, provider: Any) -> Any:
        with self._lock:
            provider_id = self._provider_id(provider)
            self._providers[provider_id] = provider
            models: List[str] = []
            listed = []
            if hasattr(provider, "list_models"):
                try:
                    listed = list(provider.list_models())
                except Exception:
                    listed = []
            if not listed and hasattr(provider, "profiles"):
                try:
                    listed = list(getattr(provider, "profiles"))
                except Exception:
                    listed = []
            for model in listed:
                profile = self._coerce_profile(model, provider_id=provider_id)
                if not profile.profile_id:
                    profile.profile_id = profile.model_id or profile.model_name or profile.display_name
                self._profiles.create(profile)
                self._model_uuid_map[profile.model_uuid] = profile.profile_id
                models.append(profile.profile_id)
            self._provider_models[provider_id] = models
            self._save_index()
            return provider

    def unregister_provider(self, provider_id: str) -> bool:
        with self._lock:
            if provider_id not in self._providers:
                return False
            self._providers.pop(provider_id, None)
            for profile_id in list(self._provider_models.get(provider_id, [])):
                profile = self._profiles.read(profile_id)
                if profile is not None:
                    self._model_uuid_map.pop(profile.model_uuid, None)
            self._provider_models.pop(provider_id, None)
            self._save_index()
            return True

    def register_profile(self, profile: ModelProfile | Dict[str, Any], provider_id: str = "") -> ModelProfile:
        with self._lock:
            model_profile = self._coerce_profile(profile, provider_id=provider_id)
            if not model_profile.profile_id:
                model_profile.profile_id = model_profile.model_name or model_profile.display_name
            self._profiles.create(model_profile)
            self._model_uuid_map[model_profile.model_uuid] = model_profile.profile_id
            if model_profile.provider_id:
                self._provider_models.setdefault(model_profile.provider_id, [])
                if model_profile.profile_id not in self._provider_models[model_profile.provider_id]:
                    self._provider_models[model_profile.provider_id].append(model_profile.profile_id)
            self._save_index()
            return model_profile

    def get_provider(self, provider_id: str) -> Optional[Any]:
        with self._lock:
            return self._providers.get(provider_id)

    def list_providers(self) -> List[str]:
        with self._lock:
            return sorted(self._providers.keys())

    def list_provider_catalog(self) -> List[Dict[str, Any]]:
        with self._lock:
            active_provider_ids = list(self._providers.keys())
        return get_provider_catalog(active_provider_ids=active_provider_ids)

    def list_models(self, provider_id: Optional[str] = None) -> List[ModelProfile]:
        with self._lock:
            profiles = self._profiles.list_profiles()
            if provider_id is None:
                return profiles
            return [profile for profile in profiles if profile.provider_id == provider_id]

    def list_model_dicts(self, provider_id: Optional[str] = None) -> List[Dict[str, Any]]:
        profile_dicts = [profile.to_dict() for profile in self.list_models(provider_id=provider_id)]
        collision_index: Dict[str, int] = {}
        for profile in profile_dicts:
            key = str(profile.get("model_id") or profile.get("model_name") or "").strip().lower()
            if not key:
                continue
            collision_index[key] = collision_index.get(key, 0) + 1
        for profile in profile_dicts:
            provider = profile.get("provider_id", "")
            model_id = profile.get("model_id") or profile.get("model_name") or ""
            key = str(model_id).strip().lower()
            collision_count = collision_index.get(key, 0)
            name_collision = collision_count > 1
            qualified_model_id = "{}/{}".format(provider, model_id) if provider and model_id else model_id
            metadata = dict(profile.get("metadata", {}))
            metadata.update(
                {
                    "provider_model_key": qualified_model_id,
                    "ambiguity_key": key,
                    "name_collision": name_collision,
                    "provider_count_for_model_name": collision_count,
                }
            )
            profile["qualified_model_id"] = qualified_model_id
            profile["name_collision"] = name_collision
            profile["provider_count_for_model_name"] = collision_count
            profile["disambiguated_name"] = (
                "{} ({})".format(profile.get("display_name") or model_id, provider)
                if name_collision
                else profile.get("display_name") or model_id
            )
            profile["metadata"] = metadata
        return profile_dicts

    def list_catalog_models(self, provider_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            active_provider_ids = list(self._providers.keys())
        return get_all_known_models(provider_id=provider_id, active_provider_ids=active_provider_ids)

    def list_profile_dicts(self) -> List[Dict[str, Any]]:
        with self._lock:
            active_provider_ids = list(self._providers.keys())
        return build_profile_catalog(active_provider_ids=active_provider_ids)

    def model_uuid(self, provider_id: str, model_name: str) -> str:
        return self._profiles.lookup_model_uuid(provider_id, model_name)

    def lookup_model_uuid(self, provider_id: str, model_name: str) -> str:
        return self.model_uuid(provider_id, model_name)

    def get_model_profile(self, model_ref: str) -> Optional[ModelProfile]:
        with self._lock:
            profile = self._profiles.get_by_uuid(model_ref)
            if profile is not None:
                return profile
            profile = self._profiles.get_by_model_key(model_ref)
            if profile is not None:
                return profile
            return self._profiles.read(model_ref)

    def get_model(self, provider_id: str, model_id: str) -> Optional[ModelProfile]:
        with self._lock:
            for profile in self._profiles.list_profiles():
                if profile.provider_id == provider_id and (profile.model_id == model_id or profile.model_name == model_id):
                    return profile
            return None

    def resolve_model_uuid(self, model_uuid: str) -> Optional[ModelProfile]:
        return self.get_model_profile(model_uuid)

    def resolve_model(self, model_uuid: str) -> Optional[str]:
        with self._lock:
            return self._model_uuid_map.get(model_uuid)

    def get_provider_by_model_uuid(self, model_uuid: str) -> Optional[Any]:
        with self._lock:
            profile = self.get_model_profile(model_uuid)
            if profile is None:
                return None
            return self._providers.get(profile.provider_id)

    def get_profile_for_model(self, model_ref: str) -> Optional[ModelProfile]:
        return self.get_model_profile(model_ref)

    def _normalize_request(self, request: Any, kwargs: Dict[str, Any]) -> Any:
        if request is not None:
            return request
        if kwargs:
            return kwargs
        return {}

    def _resolve_profile(
        self,
        provider_id: str = "",
        model_id: str = "",
        model_ref: str = "",
    ) -> Optional[ModelProfile]:
        if model_ref:
            profile = self.get_model_profile(model_ref)
            if profile is not None:
                return profile
        if provider_id and model_id:
            profile = self.get_model(provider_id, model_id)
            if profile is not None:
                return profile
        if provider_id and "/" in provider_id and not model_id:
            prov, model = provider_id.split("/", 1)
            profile = self.get_model(prov, model)
            if profile is not None:
                return profile
        if provider_id:
            profile = self.get_model_profile(provider_id)
            if profile is not None:
                return profile
        return None

    def invoke(self, provider_id: str, model_id: str = "", request: Any = None, **kwargs: Any) -> Any:
        profile = self._resolve_profile(provider_id=provider_id, model_id=model_id)
        if profile is None:
            raise KeyError(f"Unknown model reference: {provider_id if not model_id else f'{provider_id}:{model_id}'}")
        with self._lock:
            provider = self._providers.get(profile.provider_id)
            if provider is None:
                raise KeyError(f"Unknown provider: {profile.provider_id}")

        payload = request if request is not None else self._normalize_request(None, kwargs)
        model_name = profile.model_id or profile.model_name
        if hasattr(provider, "request"):
            try:
                return provider.request(payload, model_name, model_profile=profile, **kwargs)
            except TypeError:
                pass
        if hasattr(provider, "invoke"):
            try:
                return provider.invoke(payload, model_profile=profile, **kwargs)
            except TypeError:
                return provider.invoke(payload, model_name, model_profile=profile, **kwargs)
        if hasattr(provider, "complete"):
            try:
                return provider.complete(payload, model_profile=profile, **kwargs)
            except TypeError:
                return provider.complete(payload, model_name, model_profile=profile, **kwargs)
        raise AttributeError(f"Provider {profile.provider_id!r} does not implement invoke/request")

    def complete(self, provider_id: str, model_id: str = "", request: Any = None, **kwargs: Any) -> Any:
        return self.invoke(provider_id, model_id=model_id, request=request, **kwargs)

    def stream(self, provider_id: str, model_id: str = "", request: Any = None, **kwargs: Any) -> Any:
        profile = self._resolve_profile(provider_id=provider_id, model_id=model_id)
        if profile is None:
            raise KeyError(f"Unknown model reference: {provider_id if not model_id else f'{provider_id}:{model_id}'}")
        with self._lock:
            provider = self._providers.get(profile.provider_id)
            if provider is None:
                raise KeyError(f"Unknown provider: {profile.provider_id}")
        payload = request if request is not None else self._normalize_request(None, kwargs)
        model_name = profile.model_id or profile.model_name
        if hasattr(provider, "stream"):
            try:
                return provider.stream(payload, model_name, model_profile=profile, **kwargs)
            except TypeError:
                try:
                    return provider.stream(payload, model_profile=profile, **kwargs)
                except TypeError:
                    pass
        result = self.invoke(profile.provider_id, model_name, request=payload, **kwargs)

        def _single() -> Iterable[Any]:
            yield result

        return _single()

    def token_count(
        self,
        *args: Any,
        value: Any = None,
        model_ref: str = "",
        provider_id: str = "",
        model_name: str = "",
    ) -> int:
        if len(args) >= 3 and value is None and not model_ref and not provider_id and not model_name:
            provider_id = str(args[0])
            model_name = str(args[1])
            value = args[2]
        elif len(args) >= 1 and value is None:
            value = args[0]

        with self._lock:
            profile = None
            if model_ref:
                profile = self.get_model_profile(model_ref)
            elif provider_id and model_name:
                profile = self.get_model_profile(self.model_uuid(provider_id, model_name))
            provider = self._providers.get(profile.provider_id) if profile is not None else None

        if provider is not None:
            if hasattr(provider, "token_count"):
                try:
                    return int(provider.token_count(value, model=getattr(profile, "model_name", "")))
                except TypeError:
                    return int(provider.token_count(value, getattr(profile, "model_name", "")))
            if hasattr(provider, "count_tokens"):
                try:
                    return int(provider.count_tokens(value, model=getattr(profile, "model_name", "")))
                except TypeError:
                    return int(provider.count_tokens(value, getattr(profile, "model_name", "")))
        if profile is not None:
            return profile.token_count(value)
        if isinstance(value, str):
            return max(0, len(value) // 4)
        total = 0
        for message in value or []:
            if isinstance(message, dict):
                content = message.get("content", "")
                if isinstance(content, str):
                    total += max(0, len(content) // 4)
        return total

    def count_tokens(self, *args: Any, **kwargs: Any) -> int:
        return self.token_count(*args, **kwargs)

    def list_model_uuids(self) -> List[str]:
        with self._lock:
            return sorted(self._model_uuid_map.keys())

    def get_model_by_uuid(self, model_uuid: str) -> Optional[ModelProfile]:
        return self.get_model_profile(model_uuid)

    def get_provider_models(self, provider_id: str) -> List[ModelProfile]:
        return self.list_models(provider_id=provider_id)

    def route_task(self, task: Dict[str, Any]) -> Optional[ModelProfile]:
        route = self.router.route(task_type=str(task.get("task_type", "")), context=task)
        provider = route.get("provider", "")
        model = route.get("model", "")
        if provider and model:
            return self.get_model(provider, model)
        if provider:
            return self.get_model_profile(provider)
        return None

    def stop(self, provider_id: str, request_id: str) -> bool:
        with self._lock:
            provider = self._providers.get(provider_id)
        if provider is None:
            return False
        if hasattr(provider, "stop"):
            try:
                return bool(provider.stop(request_id))
            except TypeError:
                return bool(provider.stop(request_id=request_id))
        return False


_REGISTRY: Optional[ProviderRegistry] = None
_REGISTRY_LOCK = threading.Lock()


def get_provider_registry(storage_dir: Optional[Path] = None) -> ProviderRegistry:
    global _REGISTRY
    if storage_dir is not None:
        return ProviderRegistry(storage_dir=storage_dir)
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = ProviderRegistry()
    return _REGISTRY


def invoke(*args: Any, **kwargs: Any) -> Any:
    registry = get_provider_registry()
    if "model_ref" in kwargs:
        model_ref = kwargs.pop("model_ref")
        request = kwargs.pop("request", args[0] if args else None)
        return registry.invoke(str(model_ref), request=request, **kwargs)
    if len(args) >= 3 and "request" not in kwargs and "model_ref" not in kwargs:
        return registry.invoke(str(args[0]), str(args[1]), request=args[2], **kwargs)
    if len(args) >= 1:
        request = kwargs.pop("request", args[1] if len(args) > 1 else None)
        return registry.invoke(str(args[0]), request=request, **kwargs)
    raise TypeError("invoke() requires at least one positional argument")


def model_uuid(provider_id: str, model_name: str) -> str:
    return get_provider_registry().model_uuid(provider_id, model_name)


def token_count(*args: Any, **kwargs: Any) -> int:
    if "model_ref" in kwargs or "provider_id" in kwargs or "model_name" in kwargs:
        value = kwargs.pop("value", args[0] if args else None)
        return get_provider_registry().token_count(
            value,
            model_ref=kwargs.pop("model_ref", ""),
            provider_id=kwargs.pop("provider_id", ""),
            model_name=kwargs.pop("model_name", ""),
        )
    if len(args) >= 3 and not kwargs:
        return get_provider_registry().token_count(args[0], args[1], args[2])
    if len(args) >= 1:
        return get_provider_registry().token_count(
            args[0],
            model_ref=kwargs.pop("model_ref", ""),
            provider_id=kwargs.pop("provider_id", ""),
            model_name=kwargs.pop("model_name", ""),
        )
    raise TypeError("token_count() requires at least one positional argument")
