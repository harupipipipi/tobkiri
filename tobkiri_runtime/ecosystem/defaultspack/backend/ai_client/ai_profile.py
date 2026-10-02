"""Model profile dataclasses and path-based persistence helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import NAMESPACE_DNS, uuid5

from .base_provider import RetryPolicy


def _model_uuid(provider_id: str, model_name: str) -> str:
    return str(uuid5(NAMESPACE_DNS, f"{provider_id}:{model_name}"))


def _float_or_default(value: object, default: float) -> float:
    """Return a finite numeric setting or its validated default."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return default


@dataclass
class ModelProfile:
    profile_id: str = ""
    provider_id: str = ""
    model_id: str = ""
    model_name: str = ""
    model: str = ""
    display_name: str = ""
    description: str = ""
    icon: str = ""
    context_window: int = 0
    max_output_tokens: int = 0
    max_tokens: int = 4096
    temperature: float = 0.7
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    enabled: bool = True
    uuid: str = ""
    model_uuid: str = ""
    profile_uuid: str = ""

    def __post_init__(self) -> None:
        if not self.profile_id:
            self.profile_id = self.model_id or self.model_name or self.model or self.display_name
        if not self.model_id:
            self.model_id = self.model_name or self.model or self.profile_id
        if not self.model_name:
            self.model_name = self.model_id or self.model or self.profile_id
        if not self.model:
            self.model = self.model_name or self.model_id or self.profile_id
        if not self.display_name:
            self.display_name = self.model_name or self.profile_id
        if not self.uuid:
            self.uuid = self.model_uuid or self.profile_uuid or _model_uuid(self.provider_id, self.model_name or self.profile_id)
        if not self.model_uuid:
            self.model_uuid = self.uuid
        if not self.profile_uuid:
            self.profile_uuid = self.uuid
        if not self.max_output_tokens:
            self.max_output_tokens = self.max_tokens
        if not self.max_tokens:
            self.max_tokens = self.max_output_tokens or 4096

    @property
    def id(self) -> str:
        return self.profile_id

    @property
    def model_key(self) -> str:
        return f"{self.provider_id}/{self.model_id or self.model_name}" if self.provider_id else (self.model_id or self.model_name)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.profile_id,
            "profile_id": self.profile_id,
            "profile_uuid": self.profile_uuid,
            "uuid": self.uuid,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "model": self.model,
            "display_name": self.display_name,
            "description": self.description,
            "icon": self.icon,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "retry_policy": {
                "max_retries": self.retry_policy.max_retries,
                "backoff_seconds": self.retry_policy.backoff_seconds,
                "wait_seconds": self.retry_policy.wait_seconds,
                "failover_providers": list(self.retry_policy.failover_providers),
                "retryable_errors": list(self.retry_policy.retryable_errors),
                "tool_fallback": self.retry_policy.tool_fallback,
                "on_error": self.retry_policy.on_error,
                "notify_user": self.retry_policy.notify_user,
                "output_cause": self.retry_policy.output_cause,
            },
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelProfile":
        retry_policy = data.get("retry_policy", {}) or {}
        if isinstance(retry_policy, RetryPolicy):
            policy = retry_policy
        else:
            policy = RetryPolicy(
                max_retries=int(retry_policy.get("max_retries", 3)),
                backoff_seconds=float(retry_policy.get("backoff_seconds", retry_policy.get("wait_seconds", 1.5))),
                wait_seconds=float(retry_policy.get("wait_seconds", retry_policy.get("backoff_seconds", 1.5))),
                failover_providers=list(retry_policy.get("failover_providers", [])),
                retryable_errors=list(retry_policy.get("retryable_errors", [])),
                tool_fallback=retry_policy.get("tool_fallback"),
                on_error=str(retry_policy.get("on_error", "retry")),
                notify_user=bool(retry_policy.get("notify_user", True)),
                output_cause=bool(retry_policy.get("output_cause", True)),
            )
        max_tokens = int(data.get("max_tokens", data.get("max_output_tokens", 4096)) or 0)
        max_output_tokens = int(data.get("max_output_tokens", data.get("max_tokens", max_tokens)) or 0)
        return cls(
            profile_id=data.get("profile_id", data.get("id", "")),
            provider_id=data.get("provider_id", ""),
            model_id=data.get("model_id", data.get("model_name", data.get("model", ""))),
            model_name=data.get("model_name", data.get("model", data.get("model_id", ""))),
            model=data.get("model", data.get("model_name", data.get("model_id", ""))),
            display_name=data.get("display_name", ""),
            description=data.get("description", ""),
            icon=data.get("icon", ""),
            context_window=int(data.get("context_window", 0) or 0),
            max_output_tokens=max_output_tokens,
            max_tokens=max_tokens,
            temperature=float(data.get("temperature", 0.7)),
            tags=list(data.get("tags", [])),
            metadata=dict(data.get("metadata", {})),
            retry_policy=policy,
            enabled=bool(data.get("enabled", True)),
            uuid=data.get("uuid", data.get("model_uuid", data.get("profile_uuid", ""))),
            model_uuid=data.get("model_uuid", data.get("uuid", "")),
            profile_uuid=data.get("profile_uuid", data.get("uuid", "")),
        )

    def token_count(self, value: Any) -> int:
        if isinstance(value, str):
            return max(0, len(value) // 4)
        total = 0
        for message in value or []:
            if isinstance(message, dict):
                content = message.get("content", "")
                if isinstance(content, str):
                    total += max(0, len(content) // 4)
        return total


class ModelProfileManager:
    def __init__(self, storage_dir: Optional[Path] = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir is not None else None
        self._profiles: Dict[str, ModelProfile] = {}
        if self.storage_dir is not None:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            self._load()

    def load_from_dir(self, storage_dir: Path) -> int:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._profiles.clear()
        self._load()
        return len(self._profiles)

    def _path(self, profile_id: str) -> Optional[Path]:
        if self.storage_dir is None:
            return None
        return self.storage_dir / f"{profile_id}.json"

    def _load(self) -> None:
        if self.storage_dir is None or not self.storage_dir.exists():
            return
        for path in sorted(self.storage_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            profile = ModelProfile.from_dict(data)
            key = profile.profile_id or path.stem
            self._profiles[key] = profile

    def _save(self, profile: ModelProfile) -> None:
        path = self._path(profile.profile_id)
        if path is None:
            return
        path.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def create(self, profile: ModelProfile | Dict[str, Any]) -> ModelProfile:
        if isinstance(profile, dict):
            profile = ModelProfile.from_dict(profile)
        self._profiles[profile.profile_id] = profile
        self._save(profile)
        return profile

    def save(self, profile: ModelProfile | Dict[str, Any]) -> ModelProfile:
        return self.create(profile)

    def read(self, profile_id: str) -> Optional[ModelProfile]:
        return self._profiles.get(profile_id)

    def get(self, profile_id: str) -> Optional[ModelProfile]:
        return self.read(profile_id)

    def get_profile(self, profile_id: str) -> Optional[ModelProfile]:
        return self.read(profile_id)

    def update(self, profile_id: str, updates: Dict[str, Any]) -> Optional[ModelProfile]:
        profile = self._profiles.get(profile_id)
        if profile is None:
            return None
        for key, value in updates.items():
            if key == "retry_policy":
                if isinstance(value, RetryPolicy):
                    profile.retry_policy = value
                elif isinstance(value, dict):
                    backoff_value = value.get("backoff_seconds")
                    if backoff_value is None:
                        backoff_value = value.get(
                            "wait_seconds", profile.retry_policy.backoff_seconds
                        )
                    wait_value = value.get("wait_seconds")
                    if wait_value is None:
                        wait_value = value.get(
                            "backoff_seconds", profile.retry_policy.wait_seconds
                        )
                    profile.retry_policy = RetryPolicy(
                        max_retries=int(value.get("max_retries", profile.retry_policy.max_retries)),
                        backoff_seconds=_float_or_default(
                            backoff_value, profile.retry_policy.backoff_seconds
                        ),
                        wait_seconds=_float_or_default(
                            wait_value, profile.retry_policy.wait_seconds
                        ),
                        failover_providers=list(value.get("failover_providers", profile.retry_policy.failover_providers)),
                        retryable_errors=list(value.get("retryable_errors", profile.retry_policy.retryable_errors)),
                        tool_fallback=value.get("tool_fallback", profile.retry_policy.tool_fallback),
                        on_error=str(value.get("on_error", profile.retry_policy.on_error)),
                        notify_user=bool(value.get("notify_user", profile.retry_policy.notify_user)),
                        output_cause=bool(value.get("output_cause", profile.retry_policy.output_cause)),
                    )
            elif key in {"model", "model_name", "model_id"}:
                setattr(profile, key, value)
            elif hasattr(profile, key):
                setattr(profile, key, value)
        if not profile.model_uuid:
            profile.model_uuid = _model_uuid(profile.provider_id, profile.model_name or profile.profile_id)
        if not profile.uuid:
            profile.uuid = profile.model_uuid
        if not profile.profile_uuid:
            profile.profile_uuid = profile.model_uuid
        self._save(profile)
        return profile

    def delete(self, profile_id: str) -> bool:
        profile = self._profiles.pop(profile_id, None)
        if profile is None:
            return False
        path = self._path(profile_id)
        if path is not None:
            path.unlink(missing_ok=True)
        return True

    def remove(self, profile_id: str) -> bool:
        return self.delete(profile_id)

    def list_profiles(self) -> List[ModelProfile]:
        return list(self._profiles.values())

    def list_all(self) -> List[ModelProfile]:
        return self.list_profiles()

    def list_profile_dicts(self) -> List[Dict[str, Any]]:
        return [profile.to_dict() for profile in self.list_profiles()]

    def lookup_model_uuid(self, provider_id: str, model_name: str) -> str:
        return _model_uuid(provider_id, model_name)

    def get_by_uuid(self, model_uuid: str) -> Optional[ModelProfile]:
        for profile in self._profiles.values():
            if profile.uuid == model_uuid or profile.model_uuid == model_uuid or profile.profile_uuid == model_uuid:
                return profile
        return None

    def get_by_model_key(self, model_key: str) -> Optional[ModelProfile]:
        if "/" not in model_key:
            return self._profiles.get(model_key)
        provider_id, model_name = model_key.split("/", 1)
        for profile in self._profiles.values():
            if profile.provider_id == provider_id and (profile.model_id == model_name or profile.model_name == model_name):
                return profile
        return None

    def token_count(self, value: Any, model_id: str = "") -> int:
        profile = self.get_by_uuid(model_id) or self.get_by_model_key(model_id)
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


AIProfile = ModelProfile
AIProfileManager = ModelProfileManager
