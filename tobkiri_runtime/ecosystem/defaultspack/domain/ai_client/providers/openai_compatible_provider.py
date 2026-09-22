from __future__ import annotations

import json
import hashlib
import hmac
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..model_metadata_schema import (
    context_window_value,
    normalize_capability_map,
    normalize_request_features,
    normalize_routing_defaults,
)

from .openai_provider import OpenAIProvider
from .profile_catalog import merge_curated_and_profiles, profile_dir_for


class OpenAICompatibleProvider(OpenAIProvider):
    """OpenAI-compatible provider with both legacy and manifest constructors."""

    provider_name = ""
    KNOWN_MODELS: List[Dict[str, Any]] = []
    curated_models: List[Dict[str, Any]] = []
    DISPLAY_NAME = "OpenAI Compatible"
    _SUPPRESS_DEFAULT_REASONING_PARAM = "_suppress_default_reasoning_effort"
    _CEREBRAS_REQUEST_DEFAULTS: Dict[str, Dict[str, Any]] = {
        "gpt-oss-120b": {
            "temperature": 1,
            "top_p": 1,
            "reasoning_effort": "high",
        },
        "llama3.1-8b": {
            "max_completion_tokens": 2048,
            "temperature": 0.2,
            "top_p": 1,
        },
    }
    _CEREBRAS_REASONING_MODELS = {"gpt-oss-120b", "zai-glm-4.7"}

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        known_models=None,
        *,
        provider_id: str = "",
        display_name: str = "",
        api_key_env: str | Sequence[str] | set[str] = "",
        base_url_env: str = "",
        default_base_url: str = "",
        credential_required: bool = True,
        extra_headers: Optional[Dict[str, str]] = None,
        remote_model_discovery: bool | None = None,
        remote_model_discovery_requires_auth: bool = True,
        remote_model_list_path: str | None = None,
        remote_model_base_url: str | None = None,
        remote_model_cache_ttl_seconds: int | None = None,
        remote_model_pagination: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        default_provider_id = str(
            provider_id or getattr(self.__class__, "provider_name", "") or "openai_compatible"
        )
        self.provider_id = default_provider_id
        self.display_name = str(
            display_name or getattr(self.__class__, "display_name", "") or self.provider_id
        )
        self.DISPLAY_NAME = self.display_name
        self._api_key_envs = self._normalize_env_names(api_key_env)
        self._api_key_env = self._api_key_envs[0] if self._api_key_envs else ""
        self._base_url_env = str(base_url_env or "")
        self._default_base_url = str(default_base_url or self.BASE_URL).strip().rstrip("/")
        self._credential_required = bool(credential_required)
        self._extra_headers = dict(extra_headers or {})
        if remote_model_discovery is None:
            remote_model_discovery = bool(getattr(self.__class__, "remote_model_discovery", False))
        if remote_model_list_path is None:
            remote_model_list_path = str(
                getattr(self.__class__, "remote_model_list_path", "/models") or "/models"
            )
        if remote_model_cache_ttl_seconds is None:
            remote_model_cache_ttl_seconds = getattr(
                self.__class__, "remote_model_cache_ttl_seconds", 21600
            )
        self._remote_model_discovery = bool(remote_model_discovery)
        self._remote_model_discovery_requires_auth = bool(remote_model_discovery_requires_auth)
        self._remote_model_list_path = str(remote_model_list_path or "/models").strip() or "/models"
        self._remote_model_base_url = str(remote_model_base_url or "").strip().rstrip("/")
        self._remote_model_pagination = dict(remote_model_pagination or {})
        try:
            self._remote_model_cache_ttl_seconds = max(60, int(remote_model_cache_ttl_seconds))
        except (TypeError, ValueError):
            self._remote_model_cache_ttl_seconds = 21600

        self._api_key = str(api_key or "").strip()
        resolved_base_url = str(base_url or self._default_base_url or "").strip()
        self._base_url = resolved_base_url.rstrip("/") if resolved_base_url else ""
        self.BASE_URL = self._base_url
        seed_models = known_models
        if seed_models is None:
            seed_models = self.list_curated_models()
        self.KNOWN_MODELS = self._normalize_known_models(seed_models or [])

    def stream(self, model, messages, tools, params):
        """Recover a tool call when a compatible stream omits its payload."""

        pending_end: dict[str, Any] | None = None
        saw_tool_call = False
        for event in super().stream(model, messages, tools, params):
            if not isinstance(event, dict):
                yield event
                continue
            event_type = str(event.get("type") or "")
            if event_type == "stream_end":
                pending_end = dict(event)
                continue
            if event_type == "tool_call_start":
                saw_tool_call = True
            yield event

        pending_end = pending_end or {
            "type": "stream_end",
            "finish_reason": "stop",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        }
        if (
            pending_end.get("finish_reason") == "tool_calls"
            and not saw_tool_call
        ):
            recovered_raw = self.complete(model, messages, tools, params)
            recovered: dict[str, object] = (
                {str(key): value for key, value in recovered_raw.items()}
                if isinstance(recovered_raw, dict)
                else {}
            )
            recovered_usage_value = recovered.get("usage")
            recovered_usage: dict[str, object] = (
                {str(key): value for key, value in recovered_usage_value.items()}
                if isinstance(recovered_usage_value, dict)
                else {}
            )
            stream_usage_value = pending_end.get("usage")
            stream_usage: dict[str, object] = (
                {str(key): value for key, value in stream_usage_value.items()}
                if isinstance(stream_usage_value, dict)
                else {}
            )
            pending_end["usage"] = {
                key: self._usage_int(stream_usage.get(key))
                + self._usage_int(recovered_usage.get(key))
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
            }
            content_value = recovered.get("content")
            content = content_value if isinstance(content_value, list) else []
            for item in content:
                if (
                    not isinstance(item, dict)
                    or item.get("type") != "tool_use"
                ):
                    continue
                call_id = str(item.get("id") or "tool_call_recovered")
                name = str(item.get("name") or "")
                arguments = item.get("input")
                yield {
                    "type": "tool_call_start",
                    "id": call_id,
                    "name": name,
                }
                if arguments not in (None, ""):
                    yield {
                        "type": "tool_call_delta",
                        "id": call_id,
                        "name": name,
                        "arguments_chunk": (
                            arguments
                            if isinstance(arguments, str)
                            else json.dumps(
                                arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        ),
                    }

                yield {
                    "type": "tool_call_end",
                    "id": call_id,
                    "name": name,
                }
        yield pending_end

    @staticmethod
    def _usage_int(value: object) -> int:
        """Normalize a provider usage counter without trusting its payload type."""
        if isinstance(value, (bool, int, float, str)):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
        return 0

    @classmethod
    def profile_dir(cls):
        provider_name = str(getattr(cls, "provider_name", "") or "").strip()
        if not provider_name:
            return None
        return profile_dir_for(provider_name, __file__)

    @classmethod
    def list_curated_models(cls) -> List[Dict[str, Any]]:
        source = getattr(cls, "curated_models", None) or getattr(cls, "KNOWN_MODELS", [])
        return [dict(item) for item in source]

    @classmethod
    def from_manifest(
        cls,
        manifest: Dict[str, Any],
        *,
        api_key: str = "",
        model_manifests: Optional[List[Dict[str, Any]]] = None,
        allow_declared_models: bool = True,
    ) -> "OpenAICompatibleProvider":
        provider_id = str(manifest.get("id", "")).strip() or "openai_compatible"
        known_models: List[Dict[str, Any]] = cls.list_curated_models()
        known_model_map = {
            str(item.get("id", "")).strip(): dict(item)
            for item in known_models
            if str(item.get("id", "")).strip()
        }
        for item in model_manifests or []:
            model_id = str(item.get("model_id", "")).strip()
            if not model_id:
                continue
            qualified_model_id = f"{provider_id}/{model_id}"
            known_model_map[qualified_model_id] = {
                "id": qualified_model_id,
                "model_id": model_id,
                "name": item.get("display_name", model_id),
                "display_name": item.get("display_name", model_id),
                "provider": provider_id,
                "provider_id": provider_id,
                "type": item.get("type", "chat"),
                "defaults": normalize_routing_defaults(item),
                "routing": dict(item.get("routing", {}))
                if isinstance(item.get("routing"), dict)
                else {},
                "metadata": dict(item.get("metadata", {})),
                "capabilities": normalize_capability_map(item.get("capabilities", {})),
                "request_features": normalize_request_features(item.get("request_features", {})),
                "context_window": context_window_value(item, default=0),
                "max_context": context_window_value(item, default=0),
                "max_context_tokens": context_window_value(item, default=0),
                "thinking": dict(item.get("thinking", {}))
                if isinstance(item.get("thinking"), dict)
                else {},
            }
        known_models = list(known_model_map.values())
        if not known_models:
            known_models = list(manifest.get("models", []))
        if allow_declared_models and not known_models and manifest.get("default_model"):
            default_model = str(manifest.get("default_model")).strip()
            defaults = {"chat": True}
            for use_case, candidate in (manifest.get("default_model_for", {}) or {}).items():
                if str(candidate).strip() == default_model:
                    defaults[str(use_case)] = True
            known_models = [
                {
                    "id": f"{provider_id}/{default_model}",
                    "model_id": default_model,
                    "name": default_model,
                    "display_name": default_model,
                    "provider": provider_id,
                    "provider_id": provider_id,
                    "type": "chat",
                    "defaults": defaults,
                }
            ]
        return cls(
            provider_id=provider_id,
            display_name=str(manifest.get("display_name", provider_id)),
            api_key=api_key,
            api_key_env=manifest.get("api_key_env", ""),
            base_url_env=str(manifest.get("base_url_env", "")),
            default_base_url=str(manifest.get("default_base_url", "https://api.openai.com/v1")),
            credential_required=bool(manifest.get("credential_required", True)),
            known_models=known_models,
            extra_headers=dict(manifest.get("headers", {})),
            # Every OpenAI-compatible API may publish additional models at
            # /models.  Discover them by default; providers that do not expose
            # the endpoint fall back to their declared catalog without failing.
            remote_model_discovery=str(
                (
                    (manifest.get("config") or {})
                    if isinstance(manifest.get("config"), dict)
                    else {}
                ).get("model_sync")
                or "remote_merge"
            )
            .strip()
            .lower()
            in {"remote_merge", "remote_discovery"},
            remote_model_discovery_requires_auth=bool(
                (
                    (manifest.get("config") or {})
                    if isinstance(manifest.get("config"), dict)
                    else {}
                ).get(
                    "model_list_requires_auth",
                    True,
                )
            ),
            remote_model_list_path=str(
                (
                    (manifest.get("config") or {})
                    if isinstance(manifest.get("config"), dict)
                    else {}
                ).get("model_list_path")
                or "/models"
            ),
            remote_model_base_url=str(
                (
                    (manifest.get("config") or {})
                    if isinstance(manifest.get("config"), dict)
                    else {}
                ).get("model_list_base_url")
                or ""
            ),
            remote_model_cache_ttl_seconds=(
                (manifest.get("config") or {}) if isinstance(manifest.get("config"), dict) else {}
            ).get("model_cache_ttl_seconds", 21600),
            remote_model_pagination=(
                (manifest.get("config") or {}) if isinstance(manifest.get("config"), dict) else {}
            ).get("model_list_pagination")
            or {},
        )

    @staticmethod
    def _normalize_env_names(value: Any) -> List[str]:
        if isinstance(value, str):
            env_name = value.strip()
            return [env_name] if env_name else []
        if isinstance(value, (list, tuple, set)):
            normalized: List[str] = []
            for item in value:
                env_name = str(item or "").strip()
                if env_name and env_name not in normalized:
                    normalized.append(env_name)
            return normalized
        return []

    def _normalize_known_models(self, raw_models) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in list(raw_models or []):
            model = self._normalize_known_model(raw)
            if model is None:
                continue
            if model["id"] in seen:
                continue
            seen.add(model["id"])
            normalized.append(model)
        return normalized

    def _normalize_known_model(self, raw) -> Optional[Dict[str, Any]]:
        if isinstance(raw, str):
            model_id = raw.split("/", 1)[1] if "/" in raw else raw
            if not model_id:
                return None
            qualified_model_id = raw if "/" in raw else f"{self.provider_id}/{model_id}"
            return {
                "id": qualified_model_id,
                "provider": self.provider_id,
                "name": model_id,
                "type": "chat",
            }
        if not isinstance(raw, dict):
            return None
        qualified_model_id = str(raw.get("id", "")).strip()
        model_id = str(raw.get("model_id", "")).strip()
        if qualified_model_id and "/" in qualified_model_id and not model_id:
            _, model_id = qualified_model_id.split("/", 1)
        if not model_id:
            model_id = str(raw.get("model_name") or raw.get("name") or "").strip()
        if not model_id:
            return None
        if not qualified_model_id:
            qualified_model_id = f"{self.provider_id}/{model_id}"
        display_name = str(raw.get("display_name") or raw.get("name") or model_id)
        normalized: Dict[str, object] = {
            "id": qualified_model_id,
            "model_id": model_id,
            "provider_id": self.provider_id,
            "provider": self.provider_id,
            "name": display_name,
            "display_name": display_name,
            "type": str(raw.get("type", "chat")),
        }
        defaults = normalize_routing_defaults(raw)
        metadata = dict(raw.get("metadata", {}))
        capabilities = self._public_capability_map(raw.get("capabilities", []))
        if defaults:
            normalized["defaults"] = defaults
        if metadata:
            normalized["metadata"] = metadata
        if capabilities:
            normalized["capabilities"] = capabilities
        if isinstance(raw.get("routing"), dict):
            normalized["routing"] = dict(raw["routing"])
        if isinstance(raw.get("request_features"), dict):
            normalized["request_features"] = normalize_request_features(raw["request_features"])
        if isinstance(raw.get("thinking"), dict):
            normalized["thinking"] = dict(raw["thinking"])
            thinking = raw["thinking"]
            if "supports_thinking" not in normalized:
                normalized["supports_thinking"] = bool(thinking.get("supported"))
            if isinstance(thinking.get("levels"), list):
                normalized["thinking_levels"] = list(thinking.get("levels") or [])
            if "default_level" in thinking:
                normalized["default_thinking_level"] = thinking.get("default_level")
        for key in (
            "context_window",
            "max_context",
            "max_context_tokens",
            "supports_thinking",
            "thinking_levels",
            "default_thinking_level",
        ):
            if key in raw:
                normalized[key] = raw[key]
        return normalized

    def _known_model_entry(self, model: str) -> Dict[str, Any]:
        model_ref = str(model or "").strip()
        model_id = (
            model_ref.split("/", 1)[1]
            if "/" in model_ref and model_ref.startswith(f"{self.provider_id}/")
            else model_ref
        )
        qualified = f"{self.provider_id}/{model_id}" if model_id else model_ref
        for item in self.KNOWN_MODELS:
            if not isinstance(item, dict):
                continue
            if model_ref in {
                str(item.get("id") or "").strip(),
                str(item.get("model_id") or "").strip(),
            }:
                return item
            if qualified and qualified == str(item.get("id") or "").strip():
                return item
        return {}

    @staticmethod
    def _public_capability_map(raw_capabilities: Any) -> Dict[str, Any]:
        capability_map = normalize_capability_map(raw_capabilities)
        if isinstance(raw_capabilities, dict):
            # Keep task capabilities reported by the live catalog even when
            # they are not part of the chat-oriented normalization schema.
            for key in (
                "embeddings",
                "rerank",
                "image_generation",
                "video_generation",
                "tts",
                "transcription",
                "moderation",
            ):
                if key in raw_capabilities:
                    value = raw_capabilities[key]
                    capability_map[key] = (
                        bool(value.get("supported")) if isinstance(value, dict) else bool(value)
                    )
        capability_map.setdefault(
            "chat", bool(capability_map.get("text_input") or capability_map.get("text_output"))
        )
        capability_map.setdefault("vision", bool(capability_map.get("image_input")))
        capability_map.setdefault("reasoning", bool(capability_map.get("thinking")))
        capability_map.setdefault("tool_calls", bool(capability_map.get("tool_calling")))
        return capability_map

    @staticmethod
    def _capability_map(model_entry: Dict[str, Any]) -> Dict[str, Any]:
        raw = model_entry.get("capabilities") if isinstance(model_entry, dict) else {}
        capability_map = normalize_capability_map(raw)
        metadata = model_entry.get("metadata") if isinstance(model_entry, dict) else {}
        if isinstance(metadata, dict) and isinstance(metadata.get("capabilities"), dict):
            capability_map.update(normalize_capability_map(metadata["capabilities"]))
        return capability_map

    def _model_request_defaults(self, model: str, model_entry: Dict[str, Any]) -> Dict[str, Any]:
        metadata = model_entry.get("metadata") if isinstance(model_entry, dict) else {}
        for key in ("request_defaults", "default_request_params"):
            defaults = metadata.get(key) if isinstance(metadata, dict) else None
            if isinstance(defaults, dict):
                return dict(defaults)
        model_id = str(model or "").strip()
        if "/" in model_id and model_id.startswith(f"{self.provider_id}/"):
            model_id = model_id.split("/", 1)[1]
        if self.provider_id == "cerebras":
            return dict(self._CEREBRAS_REQUEST_DEFAULTS.get(model_id, {}))
        return {}

    def _model_supports_reasoning(self, model: str, model_entry: Dict[str, Any]) -> bool:
        capability_map = self._capability_map(model_entry)
        if "thinking" in capability_map:
            return bool(capability_map.get("thinking"))
        thinking_value = model_entry.get("thinking")
        thinking: Dict[str, object] = (
            {str(key): value for key, value in thinking_value.items()}
            if isinstance(thinking_value, dict)
            else {}
        )
        if "supported" in thinking:
            return bool(thinking.get("supported"))
        if isinstance(model_entry, dict) and "supports_thinking" in model_entry:
            return bool(model_entry.get("supports_thinking"))
        model_id = str(model or "").strip()
        if "/" in model_id and model_id.startswith(f"{self.provider_id}/"):
            model_id = model_id.split("/", 1)[1]
        return self.provider_id == "cerebras" and model_id in self._CEREBRAS_REASONING_MODELS

    @staticmethod
    def _translate_params(params):
        raw = dict(params or {})
        translated = OpenAIProvider._translate_params(raw)
        thinking_level = str(raw.get("thinking_level") or "").strip().lower()
        reasoning_effort = str(raw.get("reasoning_effort") or "").strip().lower()
        if thinking_level == "none" or reasoning_effort == "none":
            translated.pop("reasoning_effort", None)
            translated[OpenAICompatibleProvider._SUPPRESS_DEFAULT_REASONING_PARAM] = True
        return translated

    def _translate_cerebras_model_params(
        self, model: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        translated = dict(params or {})
        suppress_default_reasoning = bool(
            translated.pop(self._SUPPRESS_DEFAULT_REASONING_PARAM, False)
        )
        if str(translated.get("reasoning_effort") or "").strip().lower() == "none":
            suppress_default_reasoning = True
            translated.pop("reasoning_effort", None)
        if "max_tokens" in translated:
            if "max_completion_tokens" not in translated:
                translated["max_completion_tokens"] = translated["max_tokens"]
            translated.pop("max_tokens", None)

        model_entry = self._known_model_entry(model)
        for key, value in self._model_request_defaults(model, model_entry).items():
            if key == "reasoning_effort" and suppress_default_reasoning:
                continue
            translated.setdefault(key, value)

        if not self._model_supports_reasoning(model, model_entry):
            translated.pop("reasoning_effort", None)
        return translated

    def _translate_model_params(self, model, params):
        if self.provider_id == "cerebras":
            return self._translate_cerebras_model_params(model, params)
        if self.provider_id == "vercel-ai-gateway":
            translated = dict(params or {})
            extra_body = (
                dict(translated.get("extra_body", {}))
                if isinstance(translated.get("extra_body"), dict)
                else {}
            )
            reasoning_effort = str(translated.pop("reasoning_effort", "") or "").strip().lower()
            if reasoning_effort:
                extra_body.setdefault("reasoning", {"effort": reasoning_effort})
            for key in (
                "models",
                "providerOptions",
                "reasoning",
                "service_tier",
                "web_search_options",
            ):
                if key in translated:
                    extra_body[key] = translated.pop(key)
            if extra_body:
                translated["extra_body"] = extra_body
            return translated
        return super()._translate_model_params(model, params)

    def build_request(self, messages):
        converted = super().build_request(messages)
        if self.provider_id == "groq":
            for message in converted:
                if isinstance(message, dict) and message.get("role") == "tool":
                    message.pop("name", None)
        return converted

    def list_models(self):
        provider_name = str(self.provider_id or getattr(self, "provider_name", "") or "").strip()
        profile_dir = self.profile_dir()
        if provider_name and profile_dir is not None:
            base_models = merge_curated_and_profiles(provider_name, self.KNOWN_MODELS, profile_dir)
        else:
            base_models = [dict(model) for model in self.KNOWN_MODELS]
        return self._merge_remote_models(base_models)

    def _merge_remote_models(self, base_models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in base_models:
            normalized = self._normalize_known_model(item)
            if normalized is None:
                continue
            model_id = str(normalized.get("id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            merged.append(normalized)
        for item in self._remote_discovered_models():
            normalized = self._normalize_known_model(item)
            if normalized is None:
                continue
            model_id = str(normalized.get("id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            merged.append(normalized)
        return merged

    def _remote_discovered_models(self) -> List[Dict[str, Any]]:
        if (
            not self._remote_model_discovery
            or not self._base_url
            or (self._remote_model_discovery_requires_auth and not self._api_key)
        ):
            return []
        cache = self._load_remote_model_cache()
        now = int(time.time())
        if cache and int(cache.get("expires_at") or 0) > now:
            return self._normalize_remote_models(cache.get("models"))
        try:
            fetched = self._fetch_remote_models()
        except Exception:
            fetched = []
        if fetched:
            self._save_remote_model_cache(fetched, now=now)
            return fetched
        return self._normalize_remote_models(cache.get("models")) if cache else []

    def _remote_model_cache_path(self) -> Path:
        cache_root = (
            Path(__file__).resolve().parents[3] / "user_data" / "shared" / "provider_model_cache"
        )
        cache_root.mkdir(parents=True, exist_ok=True)
        # A provider id is not an inventory scope: the same provider may be
        # configured with different accounts, projects, or custom endpoints.
        # Keep the cache filename opaque and account/endpoint-scoped so a
        # visible model list can never leak from one connection to another.
        return cache_root / f"{self.provider_id}.{self._inventory_scope_hash()}.models.json"

    def _inventory_scope_hash(self) -> str:
        """Return a stable opaque cache scope without persisting credentials.

        The API key is used only as an HMAC key and is never written to disk.
        Including the resolved endpoint also isolates region/project endpoints
        that happen to share a credential.
        """
        endpoint = self._base_url.rstrip("/")
        material = f"{self.provider_id}\0{endpoint}".encode("utf-8")
        key = (self._api_key or "no-credential").encode("utf-8")
        return hmac.new(key, material, hashlib.sha256).hexdigest()[:24]

    def _load_remote_model_cache(self) -> Dict[str, Any] | None:
        path = self._remote_model_cache_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        # Do not reuse legacy provider-only cache records.  They were not
        # connection-scoped and therefore cannot satisfy the inventory privacy
        # contract.  The next successful discovery transparently replaces them.
        if payload.get("inventory_scope") != self._inventory_scope_hash():
            return None
        return payload

    def _save_remote_model_cache(
        self, models: List[Dict[str, Any]], *, now: int | None = None
    ) -> None:
        path = self._remote_model_cache_path()
        timestamp = int(now if now is not None else time.time())
        payload = {
            "provider_id": self.provider_id,
            "inventory_scope": self._inventory_scope_hash(),
            "saved_at": timestamp,
            "expires_at": timestamp + self._remote_model_cache_ttl_seconds,
            "models": models,
        }
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            return

    def _fetch_remote_models(self) -> List[Dict[str, Any]]:
        model_base_url = self._remote_model_base_url or self._base_url
        url = model_base_url.rstrip("/") + self._remote_model_list_path
        timeout_seconds = max(
            2,
            min(
                20,
                int(os.environ.get("RUMI_DEFAULTSPACK_REMOTE_MODEL_DISCOVERY_TIMEOUT", "6") or "6"),
            ),
        )
        raw_models: List[Dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        # Providers with a public OpenAI-style endpoint commonly include the
        # entire catalog in one response.  When they do paginate, preserve
        # every account-visible page instead of silently exposing page one.
        for _ in range(self._remote_model_max_pages()):
            request_url = self._remote_model_page_url(url, cursor)
            if not request_url:
                break
            req = urllib.request.Request(
                request_url, headers=self._headers(content_type=""), method="GET"
            )
            try:
                with urllib.request.urlopen(
                    req, context=self._ssl_ctx, timeout=timeout_seconds
                ) as resp:
                    raw_bytes = resp.read().decode("utf-8")
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                break
            try:
                payload = json.loads(raw_bytes)
            except (json.JSONDecodeError, ValueError):
                break
            page_models, next_cursor = self._remote_models_page(payload)
            raw_models.extend(page_models)
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return self._normalize_remote_models(raw_models)

    def _remote_model_max_pages(self) -> int:
        try:
            return max(1, min(100, int(self._remote_model_pagination.get("max_pages", 100))))
        except (TypeError, ValueError):
            return 100

    def _remote_model_page_url(self, url: str, cursor: str) -> str:
        if not cursor:
            return url
        # A number of catalog APIs return an opaque cursor, while others
        # return the complete URL for the next page.  Both are inventory
        # pagination, not model ids.  Follow absolute/relative links only on
        # the configured endpoint origin so a compromised catalog response
        # cannot redirect an authenticated discovery request elsewhere.
        cursor_url = urllib.parse.urlsplit(cursor)
        base_url = urllib.parse.urlsplit(url)
        if cursor_url.scheme and cursor_url.netloc:
            if (cursor_url.scheme, cursor_url.netloc) == (base_url.scheme, base_url.netloc):
                return cursor
            return ""
        if cursor.startswith("/"):
            return urllib.parse.urlunsplit(
                (
                    base_url.scheme,
                    base_url.netloc,
                    cursor_url.path,
                    cursor_url.query,
                    cursor_url.fragment,
                )
            )
        parameter = (
            str(self._remote_model_pagination.get("cursor_param") or "after").strip() or "after"
        )
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query.append((parameter, cursor))
        return urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urllib.parse.urlencode(query),
                parsed.fragment,
            )
        )

    def _remote_models_page(self, payload: Any) -> tuple[List[Dict[str, Any]], str]:
        # Official catalog APIs use several equivalent envelopes.  Normalize
        # the envelope here rather than adding a provider-owned model file for
        # each vendor.  A bare list and string-only ids are also valid lists.
        raw_models = self._remote_model_records(payload)
        if not isinstance(payload, dict):
            return raw_models, ""
        models = raw_models
        pagination_value = payload.get("pagination")
        pagination: Dict[str, object] = (
            {str(key): value for key, value in pagination_value.items()}
            if isinstance(pagination_value, dict)
            else {}
        )
        links_value = payload.get("links")
        links: Dict[str, object] = (
            {str(key): value for key, value in links_value.items()}
            if isinstance(links_value, dict)
            else {}
        )
        page_value = payload.get("page")
        page: Dict[str, object] = (
            {str(key): value for key, value in page_value.items()}
            if isinstance(page_value, dict)
            else {}
        )
        configured_field = str(self._remote_model_pagination.get("next_cursor_field") or "").strip()
        candidates = [
            payload.get(configured_field) if configured_field else None,
            payload.get("next_cursor"),
            payload.get("next_page_token"),
            payload.get("nextPageToken"),
            payload.get("next_page"),
            payload.get("nextPage"),
            payload.get("next_page_url"),
            payload.get("next"),
            pagination.get(configured_field) if configured_field else None,
            pagination.get("next_cursor"),
            pagination.get("next_page_token"),
            pagination.get("nextPageToken"),
            pagination.get("next_page"),
            pagination.get("next_page_url"),
            pagination.get("next"),
            links.get("next"),
            page.get("next"),
        ]
        next_cursor = next(
            (str(value).strip() for value in candidates if str(value or "").strip()), ""
        )
        return models, next_cursor

    @staticmethod
    def _remote_model_records(payload: Any) -> List[Dict[str, Any]]:
        def records(value: Any) -> List[Dict[str, Any]]:
            if isinstance(value, list):
                normalized: List[Dict[str, Any]] = []
                for item in value:
                    if isinstance(item, dict):
                        normalized.append(dict(item))
                    elif isinstance(item, str) and item.strip():
                        normalized.append({"id": item.strip()})
                return normalized
            return []

        direct = records(payload)
        if direct or not isinstance(payload, dict):
            return direct
        # These are response envelope names, not provider-specific model
        # snapshots.  Keep the extraction shallow so fields within an actual
        # model record are never mistaken for a second inventory.
        for name in ("data", "models", "results", "items", "model_list", "modelList"):
            value = payload.get(name)
            direct = records(value)
            if direct:
                return direct
            if isinstance(value, dict):
                for nested_name in ("data", "models", "results", "items"):
                    nested = records(value.get(nested_name))
                    if nested:
                        return nested
        for name in ("result", "response"):
            container = payload.get(name)
            if not isinstance(container, dict):
                continue
            for nested_name in ("data", "models", "results", "items"):
                nested = records(container.get(nested_name))
                if nested:
                    return nested
        return []

    def _normalize_remote_models(self, raw_models: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw_models, list):
            return []
        normalized: List[Dict[str, Any]] = []
        for raw in raw_models:
            model = self._normalize_remote_model(raw)
            if model is not None:
                normalized.append(model)
        return normalized

    def _normalize_remote_model(self, raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(
            raw.get("id")
            or raw.get("model_id")
            or raw.get("model_name")
            or raw.get("modelId")
            or raw.get("model")
            or raw.get("slug")
            or raw.get("identifier")
            or raw.get("name")
            or ""
        ).strip()
        if not model_id:
            return None
        provider_prefix = f"{self.provider_id}/"
        # Some gateways already qualify ids in their /models response.  The
        # public model id is the provider-local portion; retaining the prefix
        # here would make invocation send it twice.
        if model_id.startswith(provider_prefix):
            model_id = model_id[len(provider_prefix) :]
        qualified_model_id = f"{self.provider_id}/{model_id}"
        model_type = self._remote_model_type(model_id, raw)
        capability_map = self._remote_model_capabilities(model_id, model_type, raw)
        if raw.get("supports_image_in"):
            capability_map.update({"image_input": True, "vision": True})
        if raw.get("supports_video_in"):
            capability_map["video_input"] = True
        if raw.get("supports_reasoning"):
            capability_map.update({"thinking": True, "reasoning": True})
        metadata: Dict[str, Any] = {
            "source": "remote_models_endpoint",
            "capability_source": "remote_models_endpoint",
            "capability_confidence": "unknown",
        }
        for key in ("owned_by", "object", "created"):
            value = raw.get(key)
            if value not in (None, ""):
                metadata[f"remote_{key}"] = value
        model: Dict[str, object] = {
            "id": qualified_model_id,
            "model_id": model_id,
            "provider_id": self.provider_id,
            "provider": self.provider_id,
            "display_name": str(raw.get("display_name") or raw.get("name") or model_id),
            "name": str(raw.get("display_name") or raw.get("name") or model_id),
            "type": model_type,
            "capabilities": capability_map,
            "thinking": {"supported": False, "levels": [], "provider_mapping": {}},
            "metadata": metadata,
        }
        for key in ("context_length", "max_context", "max_context_tokens", "max_context_length"):
            try:
                context_window = int(raw.get(key) or 0)
            except (TypeError, ValueError):
                context_window = 0
            if context_window > 0:
                model["context_window"] = context_window
                model["max_context"] = context_window
                model["max_context_tokens"] = context_window
                break
        for key in ("max_completion_tokens", "max_output_tokens", "max_tokens"):
            try:
                max_output = int(raw.get(key) or 0)
            except (TypeError, ValueError):
                max_output = 0
            if max_output > 0:
                metadata["max_output_tokens"] = max_output
                break
        return model

    @staticmethod
    def _remote_model_type(model_id: str, raw: Optional[Dict[str, Any]] = None) -> str:
        declared = (
            str(
                (raw or {}).get("type")
                or (raw or {}).get("model_type")
                or (raw or {}).get("modelType")
                or (raw or {}).get("task")
                or (raw or {}).get("task_type")
                or ""
            )
            .strip()
            .lower()
        )
        normalized_declared = declared.replace("-", "_").replace(" ", "_")
        declared_types = {
            "chat": "chat",
            "text": "chat",
            "text_generation": "chat",
            "completion": "chat",
            "embeddings": "embedding",
            "embedding": "embedding",
            "rerank": "rerank",
            "image": "image_gen",
            "image_generation": "image_gen",
            "text2image": "image_gen",
            "image2image": "image_gen",
            "text2video": "video_gen",
            "speech": "tts",
            "tts": "tts",
            "transcription": "transcription",
            "stt": "transcription",
            "moderation": "moderation",
        }
        if normalized_declared in declared_types:
            return declared_types[normalized_declared]
        input_modalities = {
            str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
            for field in ("input_modalities", "modalities")
            for value in OpenAICompatibleProvider._remote_feature_values((raw or {}).get(field))
            if str(value or "").strip()
        }
        output_modalities = {
            str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
            for value in OpenAICompatibleProvider._remote_feature_values(
                (raw or {}).get("output_modalities")
            )
            if str(value or "").strip()
        }
        modalities = input_modalities | output_modalities
        if any("embed" in value for value in modalities):
            return "embedding"
        if any("rerank" in value for value in modalities):
            return "rerank"
        if any(
            value in {"image", "image_generation", "text_to_image"} for value in output_modalities
        ):
            return "image_gen"
        if any(
            value in {"video", "video_generation", "text_to_video"} for value in output_modalities
        ):
            return "video_gen"
        if any(
            value in {"audio", "speech", "tts", "text_to_speech"} for value in output_modalities
        ):
            return "tts"
        lowered = str(model_id or "").strip().lower()
        if not lowered:
            return "chat"
        if "embed" in lowered:
            return "embedding"
        if any(token in lowered for token in ("tts", "speech")):
            return "tts"
        if any(token in lowered for token in ("transcribe", "stt", "whisper")):
            return "transcription"
        if any(token in lowered for token in ("guard", "moderation", "safeguard")):
            return "moderation"
        return "chat"

    @classmethod
    def _remote_model_capabilities(
        cls,
        model_id: str,
        model_type: str,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        is_chat_like = model_type in {"chat", "reasoning", "vision"}
        capabilities = {
            "chat": is_chat_like,
            "text_input": is_chat_like,
            "text_output": is_chat_like,
            "streaming": is_chat_like,
            "thinking": False,
            "reasoning": False,
            "tool_calling": False,
            "tool_calls": False,
            "parallel_tool_calls": False,
            "image_input": False,
            "vision": False,
            "embeddings": model_type == "embedding",
            "rerank": model_type == "rerank",
            "image_generation": model_type == "image_gen",
            "video_generation": model_type == "video_gen",
            "tts": model_type == "tts",
            "transcription": model_type == "transcription",
        }
        reported = (raw or {}).get("capabilities")
        if isinstance(reported, dict):
            normalized = normalize_capability_map(reported)
            aliases = {
                "completion_chat": "chat",
                "function_calling": "tool_calling",
                "vision": "image_input",
                "stream": "streaming",
            }
            for key, value in reported.items():
                canonical = aliases.get(str(key), str(key))
                enabled = bool(value.get("supported")) if isinstance(value, dict) else bool(value)
                if canonical in capabilities:
                    capabilities[canonical] = enabled
            for key, value in normalized.items():
                if key in capabilities:
                    capabilities[key] = value
        elif isinstance(reported, (list, tuple, set, str)):
            reported_values = [reported] if isinstance(reported, str) else reported
            for value in reported_values:
                canonical = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
                aliases = {
                    "completion": "chat",
                    "chat_completions": "chat",
                    "function_calling": "tool_calling",
                    "tools": "tool_calling",
                    "embeddings": "embeddings",
                    "image_generation": "image_generation",
                    "text_to_image": "image_generation",
                    "text_to_video": "video_generation",
                    "text_to_speech": "tts",
                    "speech_to_text": "transcription",
                    "audio_transcription": "transcription",
                    "vision": "image_input",
                }
                canonical = aliases.get(canonical, canonical)
                if canonical in capabilities:
                    capabilities[canonical] = True
        endpoints = {
            str(item).strip().lower()
            for item in cls._remote_feature_values((raw or {}).get("endpoints"))
            if str(item).strip()
        }
        features = {
            str(item).strip().lower()
            for item in cls._remote_feature_values((raw or {}).get("features"))
            if str(item).strip()
        }
        tasks = {
            str(item).strip().lower()
            for field in ("tasks", "supported_tasks", "modalities")
            for item in cls._remote_feature_values((raw or {}).get(field))
            if str(item).strip()
        }
        input_modalities = {
            str(item).strip().lower()
            for item in cls._remote_feature_values((raw or {}).get("input_modalities"))
            if str(item).strip()
        }
        output_modalities = {
            str(item).strip().lower()
            for item in cls._remote_feature_values((raw or {}).get("output_modalities"))
            if str(item).strip()
        }
        feature_set = endpoints | features | tasks
        all_modalities = feature_set | input_modalities | output_modalities
        if {
            "chat",
            "chat-completions",
            "chat_completions",
            "completions",
            "text-generation",
            "text_generation",
        } & feature_set:
            capabilities.update({"chat": True, "text_input": True, "text_output": True})
        if any("embed" in value for value in all_modalities):
            capabilities["embeddings"] = True
        if any("rerank" in value for value in all_modalities):
            capabilities["rerank"] = True
        if any(
            value in {"image", "image_generation", "text_to_image", "images/generations"}
            for value in feature_set | output_modalities
        ):
            capabilities["image_generation"] = True
        if any(
            value in {"video", "video_generation", "text_to_video"}
            for value in feature_set | output_modalities
        ):
            capabilities["video_generation"] = True
        if any(
            value in {"tts", "speech", "text_to_speech"}
            for value in feature_set | output_modalities
        ):
            capabilities["tts"] = True
        if any(
            value in {"transcription", "stt", "speech_to_text", "audio_transcription"}
            for value in feature_set | input_modalities
        ):
            capabilities["transcription"] = True
        if {
            "tools",
            "tool_calls",
            "tool-calling",
            "tool_calling",
            "function-calling",
            "function_calling",
        } & feature_set:
            capabilities["tool_calling"] = True
        if capabilities.get("tool_calling"):
            capabilities["tool_calls"] = True
        if "image" in input_modalities:
            capabilities["image_input"] = True
        if capabilities.get("image_input"):
            capabilities["vision"] = True
        return capabilities

    @staticmethod
    def _remote_feature_values(value: Any) -> List[Any]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return list(value)
        if isinstance(value, dict):
            values: List[Any] = []
            for key, enabled in value.items():
                if isinstance(enabled, dict):
                    enabled = enabled.get("supported", enabled.get("enabled", True))
                if enabled:
                    values.append(key)
            return values
        return []

    def _headers(self, content_type="application/json"):
        headers = dict(self._extra_headers)
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        headers.setdefault("User-Agent", "RumiAI/1.0")
        headers.setdefault("Accept", "application/json")
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _ensure_runtime_config(self) -> None:
        if self._credential_required and not self._api_key:
            missing = ", ".join(self._api_key_envs) or "api_key"
            raise RuntimeError(f"{self.provider_id}: missing API key env ({missing})")
        if not self._base_url:
            raise RuntimeError(f"{self.provider_id}: base URL is not configured")
        self.BASE_URL = self._base_url

    def _request_json(self, path, body, *, timeout=120.0):
        self._ensure_runtime_config()
        return super()._request_json(path, body, timeout=timeout)

    def _request_stream(self, path, body, *, timeout=120.0):
        self._ensure_runtime_config()
        return super()._request_stream(path, body, timeout=timeout)

    def _request_multipart(self, path, fields, files):
        self._ensure_runtime_config()
        return super()._request_multipart(path, fields, files)
