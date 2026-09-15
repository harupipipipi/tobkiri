from __future__ import annotations

import json
import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from .component_metadata import model_manifests_from_provider_components
from .openai_compatible_provider import OpenAICompatibleProvider
from ..oauth_store import get_provider_access_token
from .profile_catalog import merge_curated_and_profiles, profile_dir_for

_GOOGLE_FUNCTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_GOOGLE_FUNCTION_NAME_ALLOWED_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_TRANSIENT_GOOGLE_HTTP_CODES = {429, 500, 502, 503, 504}
_TRANSIENT_GOOGLE_CONNECTION_TOKENS = (
    "connection reset",
    "connection aborted",
    "remote end closed",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "temporary failure",
)


def _retry_sleep(delay: float) -> None:
    time.sleep(delay)


class GoogleProvider(OpenAICompatibleProvider):
    """Google Gemini provider using Google's OpenAI-compatible Gemini endpoint."""

    provider_name = "google"
    display_name = "Google"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
    PROFILE_DIR = profile_dir_for("google", __file__)

    curated_models: List[Dict[str, Any]] = [
        {
            "id": "google/gemini-2.5-pro",
            "model_id": "gemini-2.5-pro",
            "name": "Gemini 2.5 Pro",
            "display_name": "Gemini 2.5 Pro",
            "provider": "google",
            "provider_id": "google",
            "type": "chat",
            "capabilities": ["chat", "tool_calls", "vision"],
            "supports_thinking": True,
            "thinking_levels": ["none", "low", "medium", "high"],
            "default_thinking_level": "medium",
            "defaults": {"chat": True, "large": True},
        },
        {
            "id": "google/gemini-2.5-flash",
            "model_id": "gemini-2.5-flash",
            "name": "Gemini 2.5 Flash",
            "display_name": "Gemini 2.5 Flash",
            "provider": "google",
            "provider_id": "google",
            "type": "chat",
            "capabilities": ["chat", "tool_calls", "vision"],
            "supports_thinking": True,
            "thinking_levels": ["none", "low", "medium", "high"],
            "default_thinking_level": "medium",
            "defaults": {"fast": True},
        },
        {
            "id": "google/gemini-3-pro-preview",
            "model_id": "gemini-3-pro-preview",
            "name": "Gemini 3 Pro Preview",
            "display_name": "Gemini 3 Pro Preview",
            "provider": "google",
            "provider_id": "google",
            "type": "chat",
            "capabilities": ["chat", "tool_calls", "vision"],
            "supports_thinking": True,
            "thinking_levels": ["low", "high"],
            "default_thinking_level": "high",
        },
        {
            "id": "google/gemini-3-flash-preview",
            "model_id": "gemini-3-flash-preview",
            "name": "Gemini 3 Flash Preview",
            "display_name": "Gemini 3 Flash Preview",
            "provider": "google",
            "provider_id": "google",
            "type": "chat",
            "capabilities": ["chat", "tool_calls", "vision"],
            "supports_thinking": True,
            "thinking_levels": ["none", "low", "medium", "high"],
            "default_thinking_level": "medium",
        },
        {
            "id": "google/gemini-2.5-flash-lite",
            "model_id": "gemini-2.5-flash-lite",
            "name": "Gemini 2.5 Flash-Lite",
            "display_name": "Gemini 2.5 Flash-Lite",
            "provider": "google",
            "provider_id": "google",
            "type": "chat",
            "capabilities": ["chat", "tool_calls", "vision"],
            "supports_thinking": True,
            "thinking_levels": ["none", "low", "medium", "high"],
            "default_thinking_level": "medium",
        },
        {
            "id": "google/gemma-4-31b-it",
            "model_id": "gemma-4-31b-it",
            "name": "Gemma 4 31B IT",
            "display_name": "Gemma 4 31B IT",
            "provider": "google",
            "provider_id": "google",
            "type": "chat",
            "capabilities": ["chat", "tool_calls", "vision"],
            "supports_thinking": True,
            "thinking_levels": ["minimal", "high"],
            "default_thinking_level": "high",
        },
        {
            "id": "google/gemma-4-26b-a4b-it",
            "model_id": "gemma-4-26b-a4b-it",
            "name": "Gemma 4 26B A4B IT",
            "display_name": "Gemma 4 26B A4B IT",
            "provider": "google",
            "provider_id": "google",
            "type": "chat",
            "capabilities": ["chat", "tool_calls", "vision"],
            "supports_thinking": True,
            "thinking_levels": ["minimal", "high"],
            "default_thinking_level": "high",
        },
        {
            "id": "google/gemma-3-27b-it",
            "model_id": "gemma-3-27b-it",
            "name": "Gemma 3 27B IT",
            "display_name": "Gemma 3 27B IT",
            "provider": "google",
            "provider_id": "google",
            "type": "chat",
            "capabilities": ["chat", "vision"],
        },
        {
            "id": "google/gemma-3n-e4b-it",
            "model_id": "gemma-3n-e4b-it",
            "name": "Gemma 3n E4B IT",
            "display_name": "Gemma 3n E4B IT",
            "provider": "google",
            "provider_id": "google",
            "type": "chat",
            "capabilities": ["chat", "vision"],
        },
        {
            "id": "google/gemini-embedding-001",
            "model_id": "gemini-embedding-001",
            "name": "Gemini Embedding 001",
            "display_name": "Gemini Embedding 001",
            "provider": "google",
            "provider_id": "google",
            "type": "embedding",
            "defaults": {"embedding": True},
        },
        {
            "id": "google/text-embedding-004",
            "model_id": "text-embedding-004",
            "name": "Text Embedding 004",
            "display_name": "Text Embedding 004",
            "provider": "google",
            "provider_id": "google",
            "type": "embedding",
        },
    ]
    CURATED_MODELS: List[Dict[str, Any]] = []
    KNOWN_MODELS: List[Dict[str, Any]] = []
    _MODEL_INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _MODEL_INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self):
        catalog_models = model_manifests_from_provider_components("google")
        super().__init__(
            provider_id="google",
            display_name="Google",
            api_key_env=["GOOGLE_API_KEY", "GEMINI_API_KEY"],
            base_url_env="GOOGLE_BASE_URL",
            default_base_url=self.BASE_URL,
            known_models=catalog_models,
        )
        self._base_url = self._normalize_google_base_url(self._base_url)
        self.BASE_URL = self._base_url
        self._runtime_bearer_token = ""

    def _oauth_access_token(self) -> str:
        return str(get_provider_access_token("google") or "").strip()

    def _active_bearer_token(self) -> str:
        runtime_token = str(getattr(self, "_runtime_bearer_token", "") or "").strip()
        if runtime_token:
            return runtime_token
        return self._oauth_access_token() or str(self._api_key or "").strip()

    def _headers(self, content_type="application/json"):
        headers = dict(getattr(self, "_extra_headers", {}))
        bearer_token = self._active_bearer_token()
        if bearer_token:
            headers["Authorization"] = "Bearer " + bearer_token
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _ensure_runtime_config(self) -> None:
        bearer_token = self._oauth_access_token() or str(self._api_key or "").strip()
        self._runtime_bearer_token = bearer_token
        if self._credential_required and not bearer_token:
            missing = ", ".join(self._api_key_envs) or "api_key"
            raise RuntimeError(
                f"{self.provider_id}: missing API key env ({missing}) or browser OAuth connection"
            )
        if not self._base_url:
            raise RuntimeError(f"{self.provider_id}: base URL is not configured")
        self.BASE_URL = self._base_url

    @staticmethod
    def _normalize_google_base_url(base_url: str) -> str:
        value = str(base_url or "").strip().rstrip("/")
        if not value:
            return value
        parsed = urllib.parse.urlparse(value)
        if parsed.netloc != "generativelanguage.googleapis.com":
            return value
        path = parsed.path.rstrip("/")
        if path in {"/v1", "/v1beta"}:
            return urllib.parse.urlunparse(parsed._replace(path=f"{path}/openai")).rstrip("/")
        return value

    @classmethod
    def _load_profile_models(cls):
        catalog_models = model_manifests_from_provider_components("google")
        return merge_curated_and_profiles("google", catalog_models, cls.PROFILE_DIR)

    def _native_models_base_url(self) -> str:
        parsed = urllib.parse.urlparse(str(self._base_url or ""))
        if parsed.netloc != "generativelanguage.googleapis.com":
            return ""
        path = parsed.path.rstrip("/")
        if path.endswith("/openai"):
            path = path[: -len("/openai")]
        return urllib.parse.urlunparse(
            parsed._replace(path=path or "/v1beta", query="", fragment="")
        ).rstrip("/")

    def _model_inventory_scope(self) -> str:
        token = self._oauth_access_token() or str(self._api_key or "")
        return hashlib.sha256(
            (self._native_models_base_url() + "\0" + token).encode("utf-8")
        ).hexdigest()

    def _fetch_native_models_page(self, page_token: str = "") -> dict[str, Any]:
        base = self._native_models_base_url()
        token = self._oauth_access_token()
        if not base or not (token or self._api_key):
            return {}
        query = {"pageSize": "1000"}
        if page_token:
            query["pageToken"] = page_token
        if not token:
            query["key"] = str(self._api_key or "")
        request = urllib.request.Request(
            base + "/models?" + urllib.parse.urlencode(query),
            headers={"Authorization": f"Bearer {token}"} if token else {},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
        ):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _native_model_record(raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("name") or raw.get("baseModelId") or "").strip()
        if model_id.startswith("models/"):
            model_id = model_id.split("/", 1)[1]
        if not model_id:
            return None
        actions = raw.get("supportedGenerationMethods") or raw.get("supportedActions") or []
        actions = {str(item) for item in actions} if isinstance(actions, list) else set()
        is_embedding = bool(actions & {"embedContent", "batchEmbedContents", "embedText"})
        is_chat = bool(actions & {"generateContent", "streamGenerateContent", "createInteraction"})
        model_type = "chat" if is_chat else "embedding" if is_embedding else "chat"
        return {
            "id": f"google/{model_id}",
            "model_id": model_id,
            "provider_id": "google",
            "provider": "google",
            "name": str(raw.get("displayName") or model_id),
            "display_name": str(raw.get("displayName") or model_id),
            "type": model_type,
            "context_window": int(raw.get("inputTokenLimit") or 0),
            "max_context": int(raw.get("inputTokenLimit") or 0),
            "capabilities": {
                "chat": is_chat,
                "text_input": is_chat,
                "text_output": is_chat,
                "streaming": "streamGenerateContent" in actions or is_chat,
                "embedding": is_embedding,
                "image_input": "generateContent" in actions,
                "vision": "generateContent" in actions,
            },
            "metadata": {
                "source": "native_models_endpoint",
                "capability_source": "native_models_endpoint",
                "capability_confidence": "provider_reported",
                "max_output_tokens": int(raw.get("outputTokenLimit") or 0),
            },
        }

    def list_models(self):
        scope = self._model_inventory_scope()
        cached = self._MODEL_INVENTORY_CACHE.get(scope) if scope else None
        now = time.monotonic()
        if cached and cached[0] > now:
            return [dict(model) for model in cached[1]]
        models: List[Dict[str, Any]] = []
        page_token = ""
        seen_tokens = set()
        for _ in range(100):
            page = self._fetch_native_models_page(page_token)
            for raw in page.get("models", []) if isinstance(page.get("models"), list) else []:
                model = self._native_model_record(raw)
                if model and all(item["model_id"] != model["model_id"] for item in models):
                    models.append(model)
            next_token = str(page.get("nextPageToken") or "").strip()
            if not next_token or next_token in seen_tokens:
                break
            seen_tokens.add(next_token)
            page_token = next_token
        if models and scope:
            self._MODEL_INVENTORY_CACHE[scope] = (
                now + self._MODEL_INVENTORY_CACHE_TTL_SECONDS,
                [dict(model) for model in models],
            )
            return models
        return self._normalize_known_models(self._load_profile_models())

    @staticmethod
    def _translate_thinking_level(model: str, thinking_level: str) -> str | None:
        model_id = str(model or "").strip()
        level = str(thinking_level or "").strip().lower()
        if not level:
            return None
        if level == "xhigh":
            level = "high"

        if model_id.startswith("gemini-3-pro"):
            if level == "low":
                return "low"
            if level in {"medium", "high"}:
                return "high"
            return None

        if model_id.startswith("gemini-3-flash"):
            if level == "none":
                return "minimal"
            if level in {"low", "medium", "high"}:
                return level
            return None

        if model_id.startswith("gemini-2.5"):
            if level == "none" and model_id.startswith("gemini-2.5-pro"):
                return None
            if level in {"none", "low", "medium", "high"}:
                return level
            return None

        if model_id.startswith("gemma-4"):
            if level in {"minimal", "low"}:
                return "minimal"
            if level in {"medium", "high"}:
                return "high"
            return None

        return None

    @staticmethod
    def _use_native_generative_api(model: str) -> bool:
        return str(model or "").strip().startswith("gemma-4")

    @classmethod
    def _translate_params(cls, params, model: str = ""):
        translated = dict(params or {})
        thinking_level = str(translated.pop("thinking_level", "") or "").strip()
        reasoning_effort = cls._translate_thinking_level(model, thinking_level)
        if reasoning_effort and "reasoning_effort" not in translated:
            translated["reasoning_effort"] = reasoning_effort
        return translated

    @staticmethod
    def _native_thinking_config(params: Dict[str, Any]) -> Dict[str, Any] | None:
        level = (
            str(params.get("thinking_level") or params.get("reasoning_effort") or "")
            .strip()
            .lower()
        )
        if level == "xhigh":
            level = "high"
        if level in {"minimal", "low"}:
            return {"thinkingLevel": "MINIMAL"}
        if level == "high":
            return {"thinkingLevel": "HIGH"}
        return None

    @staticmethod
    def _native_text_part(value: Any) -> Dict[str, Any] | None:
        if isinstance(value, str):
            if value == "":
                return None
            return {"text": value}
        if not isinstance(value, dict):
            return None
        block_type = str(value.get("type") or "").lower()
        if block_type == "text":
            return {"text": str(value.get("text") or "")}
        if block_type == "image_url":
            image_url = value.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else value.get("url")
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                header, data = url.split(";base64,", 1)
                mime_type = header.replace("data:", "", 1) or "image/png"
                return {"inlineData": {"mimeType": mime_type, "data": data}}
        if block_type in {"audio", "input_audio"}:
            audio_value = value.get("input_audio")
            audio: dict[str, object] = (
                {str(key): item for key, item in audio_value.items()}
                if isinstance(audio_value, dict)
                else {
                    str(key): item for key, item in value.items()
                }
            )
            data_value = audio.get("data")
            audio_format = str(audio.get("format") or "webm")
            if isinstance(data_value, str) and data_value:
                return {
                    "inlineData": {
                        "mimeType": f"audio/{audio_format}",
                        "data": data_value,
                    }
                }
        return None

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @staticmethod
    def _remap_tool_name(name: str, name_map: Dict[str, str] | None = None) -> str:
        text = str(name or "").strip()
        if not text or not isinstance(name_map, dict):
            return text
        return str(name_map.get(text) or text)

    @staticmethod
    def _sanitize_google_function_name(name: str, used: set[str] | None = None) -> str:
        original = str(name or "").strip()
        if _GOOGLE_FUNCTION_NAME_RE.fullmatch(original):
            candidate = original
        else:
            candidate = _GOOGLE_FUNCTION_NAME_ALLOWED_RE.sub("_", original).strip()
            if not candidate:
                candidate = "_tool"
            if not re.match(r"^[A-Za-z_]", candidate):
                candidate = "_" + candidate
            candidate = candidate[:128] or "_tool"
        registry = used if used is not None else set()
        if candidate not in registry:
            registry.add(candidate)
            return candidate
        base = candidate[:120] or "_tool"
        suffix = 2
        while True:
            deduped = f"{base}_{suffix}"[:128]
            if deduped not in registry:
                registry.add(deduped)
                return deduped
            suffix += 1

    @classmethod
    def _tool_name_maps(cls, tools: Any) -> tuple[Dict[str, str], Dict[str, str]]:
        if not isinstance(tools, list):
            return {}, {}
        forward: Dict[str, str] = {}
        reverse: Dict[str, str] = {}
        used: set[str] = set()
        for tool in tools:
            function_def = tool.get("function") if isinstance(tool, dict) else None
            name = str(
                function_def.get("name")
                if isinstance(function_def, dict)
                else tool.get("name")
                if isinstance(tool, dict)
                else ""
            ).strip()
            if not name:
                continue
            alias = cls._sanitize_google_function_name(name, used)
            forward[name] = alias
            reverse[alias] = name
        return forward, reverse

    @classmethod
    def _native_function_call_part(
        cls,
        tool_call: Any,
        name_map: Dict[str, str] | None = None,
    ) -> Dict[str, Any] | None:
        if not isinstance(tool_call, dict):
            return None
        function_value = tool_call.get("function")
        function_def: dict[str, object] = (
            {str(key): value for key, value in function_value.items()}
            if isinstance(function_value, dict)
            else {}
        )
        name = str(function_def.get("name") or tool_call.get("name") or "").strip()
        if not name:
            return None
        name = cls._remap_tool_name(name, name_map)
        args = cls._json_value(function_def.get("arguments", tool_call.get("arguments", {})))
        function_call = {
            "name": name,
            "args": args if isinstance(args, dict) else {"value": args},
        }
        call_id = str(tool_call.get("id") or tool_call.get("tool_call_id") or "").strip()
        if call_id:
            function_call["id"] = call_id
        return {"functionCall": function_call}

    @classmethod
    def _native_function_response_part(
        cls,
        message: Dict[str, Any],
        name_map: Dict[str, str] | None = None,
    ) -> Dict[str, Any] | None:
        name = str(message.get("name") or message.get("tool_name") or "").strip()
        if not name:
            return None
        name = cls._remap_tool_name(name, name_map)
        content = cls._json_value(message.get("content", ""))
        response = content if isinstance(content, dict) else {"result": content}
        function_response = {"name": name, "response": response}
        call_id = str(message.get("tool_call_id") or message.get("id") or "").strip()
        if call_id:
            function_response["id"] = call_id
        return {"functionResponse": function_response}

    @classmethod
    def _native_build_contents(
        cls,
        messages: Any,
        name_map: Dict[str, str] | None = None,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any] | None]:
        contents: List[Dict[str, Any]] = []
        system_parts: List[Dict[str, Any]] = []
        for message in list(messages or []):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user").lower()
            raw_content = message.get("content", "")
            raw_parts = raw_content if isinstance(raw_content, list) else [raw_content]
            parts = (
                []
                if role == "tool"
                else [part for part in (cls._native_text_part(item) for item in raw_parts) if part]
            )
            if role == "assistant":
                parts.extend(
                    part
                    for part in (
                        cls._native_function_call_part(item, name_map)
                        for item in message.get("tool_calls", []) or []
                    )
                    if part
                )
            elif role == "tool":
                response_part = cls._native_function_response_part(message, name_map)
                if response_part:
                    parts.append(response_part)
            if not parts:
                continue
            if role == "system":
                system_parts.extend(parts)
                continue
            native_role = "model" if role == "assistant" else "user"
            contents.append({"role": native_role, "parts": parts})
        system_instruction = {"parts": system_parts} if system_parts else None
        return contents, system_instruction

    @staticmethod
    def _native_schema(value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            return {"type": "object", "properties": {}, "required": []}
        schema = dict(value)
        normalized: Dict[str, Any] = {}
        schema_type = schema.get("type")
        if isinstance(schema_type, (list, tuple)):
            type_values = [str(item).strip() for item in schema_type if str(item).strip()]
            non_null = [item for item in type_values if item != "null"]
            if len(non_null) == 1 and len(non_null) != len(type_values):
                normalized["type"] = non_null[0]
                normalized["nullable"] = True
            elif non_null:
                normalized["type"] = non_null[0]
        for key, item in schema.items():
            if key == "properties" and isinstance(item, dict):
                normalized[key] = {
                    str(prop_key): GoogleProvider._native_schema(prop_value)
                    for prop_key, prop_value in item.items()
                    if isinstance(prop_value, dict)
                }
            elif key == "items":
                normalized[key] = GoogleProvider._native_schema(item)
            elif key in {"type", "description", "enum", "required", "nullable", "format"}:
                if key != "type" or "type" not in normalized:
                    normalized[key] = item
        if "type" not in normalized:
            normalized["type"] = "object"
        if isinstance(normalized.get("type"), list):
            schema_types = [
                str(item) for item in normalized["type"] if item and str(item) != "null"
            ]
            normalized["type"] = schema_types[0] if schema_types else "object"
            if any(str(item) == "null" for item in schema.get("type", [])):
                normalized.setdefault("nullable", True)
        if normalized.get("type") == "object":
            normalized.setdefault("properties", {})
            normalized.setdefault("required", [])
        if normalized.get("type") == "array":
            normalized["items"] = GoogleProvider._native_schema(
                normalized.get("items")
                if isinstance(normalized.get("items"), dict)
                else {"type": "object", "properties": {}, "required": []}
            )
        return normalized

    @classmethod
    def _native_tools(
        cls,
        tools: Any,
        name_map: Dict[str, str] | None = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(tools, list):
            return []
        native_tools: List[Dict[str, Any]] = []
        function_declarations: List[Dict[str, Any]] = []
        for tool in tools:
            function_def = tool.get("function") if isinstance(tool, dict) else None
            name = str(
                function_def.get("name")
                if isinstance(function_def, dict)
                else tool.get("name")
                if isinstance(tool, dict)
                else ""
            ).strip()
            normalized = name.lower().replace("-", "_").replace(".", "_")
            if normalized in {"google_search", "googlesearch"}:
                native_tools.append({"googleSearch": {}})
                continue
            if not isinstance(function_def, dict) or not name:
                continue
            declaration: Dict[str, Any] = {"name": cls._remap_tool_name(name, name_map)}
            description = function_def.get("description")
            if isinstance(description, str) and description:
                declaration["description"] = description
            declaration["parameters"] = cls._native_schema(function_def.get("parameters"))
            function_declarations.append(declaration)
        if function_declarations:
            native_tools.append({"functionDeclarations": function_declarations})
        return native_tools

    def _native_body(
        self,
        model: str,
        messages: Any,
        tools: Any,
        params: Dict[str, Any],
        name_map: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        contents, system_instruction = self._native_build_contents(messages, name_map)
        body: Dict[str, Any] = {"contents": contents}
        if system_instruction:
            body["systemInstruction"] = system_instruction
        generation_config: Dict[str, Any] = {}
        thinking_config = self._native_thinking_config(params)
        if thinking_config:
            generation_config["thinkingConfig"] = thinking_config
        for source, target in (
            ("temperature", "temperature"),
            ("max_tokens", "maxOutputTokens"),
            ("top_p", "topP"),
        ):
            if source in params:
                generation_config[target] = params[source]
        if generation_config:
            body["generationConfig"] = generation_config
        native_tools = self._native_tools(tools, name_map)
        if native_tools:
            body["tools"] = native_tools
        extra_body = params.get("extra_body")
        google_body = (
            extra_body.get("google")
            if isinstance(extra_body, dict) and isinstance(extra_body.get("google"), dict)
            else None
        )
        if google_body:
            for key, value in google_body.items():
                if key == "thinking_config" and isinstance(value, dict):
                    generation_config = dict(body.get("generationConfig", {}))
                    generation_config["thinkingConfig"] = {
                        "thinkingLevel": value.get("thinking_level")
                        or value.get("thinkingLevel")
                        or value.get("level")
                        or "HIGH",
                        **(
                            {"includeThoughts": value.get("include_thoughts")}
                            if "include_thoughts" in value
                            else {}
                        ),
                    }
                    body["generationConfig"] = generation_config
                elif key not in {"thinking_config"}:
                    body[key] = value
        return body

    @staticmethod
    def _request_timeout(params: Dict[str, Any] | None = None) -> float:
        params = params if isinstance(params, dict) else {}
        raw = params.get("request_timeout") or params.get("timeout")
        if isinstance(raw, bool):
            value = float(raw)
        elif isinstance(raw, (int, float, str)):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = 120.0
        else:
            value = 120.0
        return max(5.0, min(value, 120.0))

    def _native_request_json(
        self,
        model: str,
        body: Dict[str, Any],
        *,
        stream: bool = False,
        timeout: float | None = None,
    ):
        self._ensure_runtime_config()
        action = "streamGenerateContent" if stream else "generateContent"
        quoted_model = urllib.parse.quote(str(model), safe="")
        url = "https://generativelanguage.googleapis.com/v1beta/models/{}:{}".format(
            quoted_model, action
        )
        if stream:
            url += "?alt=sse"
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        bearer_token = self._active_bearer_token()
        if bearer_token and bearer_token == str(self._api_key or "").strip():
            headers["x-goog-api-key"] = bearer_token
        elif bearer_token:
            headers["Authorization"] = "Bearer " + bearer_token
        max_attempts = 5
        request_timeout = self._request_timeout(
            {"request_timeout": timeout} if timeout is not None else None
        )
        for attempt in range(max_attempts):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                return urllib.request.urlopen(req, context=self._ssl_ctx, timeout=request_timeout)
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")
                if exc.code in _TRANSIENT_GOOGLE_HTTP_CODES and attempt < max_attempts - 1:
                    _retry_sleep(0.5 * (attempt + 1))
                    continue
                transient = (
                    " (temporary Google backend error; retry shortly)"
                    if exc.code in _TRANSIENT_GOOGLE_HTTP_CODES
                    else ""
                )
                raise RuntimeError(
                    "Google API error {}{}: {}".format(exc.code, transient, err_body)
                )
            except urllib.error.URLError as exc:
                if self._is_transient_google_connection_error(exc) and attempt < max_attempts - 1:
                    _retry_sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError("Google API connection error: {}".format(exc.reason))
            except (TimeoutError, OSError) as exc:
                if self._is_transient_google_connection_error(exc) and attempt < max_attempts - 1:
                    _retry_sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError("Google API connection error: {}".format(exc))
        raise RuntimeError("Google API request failed")

    @staticmethod
    def _is_transient_google_connection_error(exc: Exception) -> bool:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, ConnectionResetError, ConnectionAbortedError)):
            return True
        message = str(reason or exc).lower()
        return any(token in message for token in _TRANSIENT_GOOGLE_CONNECTION_TOKENS)

    @staticmethod
    def _is_transient_google_api_error(exc: Exception) -> bool:
        message = str(exc)
        lowered = message.lower()
        return bool(
            re.search(r"(Google API error|OpenAI API error) (429|500|502|503|504)", message)
        ) or any(
            token in lowered
            for token in ("internal error encountered",) + _TRANSIENT_GOOGLE_CONNECTION_TOKENS
        )

    def _request_json(self, path, body, *, timeout=None):
        max_attempts = 5
        last_error: Exception | None = None
        request_kwargs = {"timeout": timeout} if timeout is not None else {}
        for attempt in range(max_attempts):
            try:
                return super()._request_json(path, body, **request_kwargs)
            except RuntimeError as exc:
                last_error = exc
                if attempt >= max_attempts - 1 or not self._is_transient_google_api_error(exc):
                    break
                _retry_sleep(0.5 * (attempt + 1))
        raise last_error or RuntimeError("Google API request failed")

    def _request_stream(self, path, body, *, timeout=None):
        max_attempts = 5
        last_error: Exception | None = None
        request_kwargs = {"timeout": timeout} if timeout is not None else {}
        for attempt in range(max_attempts):
            try:
                return super()._request_stream(path, body, **request_kwargs)
            except RuntimeError as exc:
                last_error = exc
                if attempt >= max_attempts - 1 or not self._is_transient_google_api_error(exc):
                    break
                _retry_sleep(0.5 * (attempt + 1))
        raise last_error or RuntimeError("Google API stream request failed")

    @staticmethod
    def _native_usage(raw: Dict[str, Any]) -> Dict[str, int]:
        usage = raw.get("usageMetadata") if isinstance(raw, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = int(usage.get("candidatesTokenCount") or 0)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": int(usage.get("totalTokenCount") or input_tokens + output_tokens),
        }

    @classmethod
    def _native_extract_parts(
        cls,
        raw: Dict[str, Any],
        reverse_name_map: Dict[str, str] | None = None,
    ) -> tuple[str, str, str, List[Dict[str, Any]]]:
        text_parts: List[str] = []
        thought_parts: List[str] = []
        tool_uses: List[Dict[str, Any]] = []
        finish_reason = "stop"
        candidates = raw.get("candidates") if isinstance(raw, dict) else []
        for candidate in candidates if isinstance(candidates, list) else []:
            if isinstance(candidate, dict) and candidate.get("finishReason"):
                finish_reason = str(candidate.get("finishReason") or "stop").lower()
            content = candidate.get("content") if isinstance(candidate, dict) else {}
            parts = content.get("parts") if isinstance(content, dict) else []
            for part in parts if isinstance(parts, list) else []:
                if not isinstance(part, dict):
                    continue
                text = str(part.get("text") or "")
                function_call = (
                    part.get("functionCall") if isinstance(part.get("functionCall"), dict) else None
                )
                if function_call:
                    call_id = str(
                        function_call.get("id")
                        or function_call.get("name")
                        or f"google_call_{len(tool_uses) + 1}"
                    ).strip()
                    name = cls._remap_tool_name(
                        str(function_call.get("name") or ""), reverse_name_map
                    )
                    tool_uses.append(
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": name,
                            "input": json.dumps(
                                function_call.get("args") or {}, ensure_ascii=False
                            ),
                        }
                    )
                    continue
                if not text:
                    continue
                if part.get("thought") is True:
                    thought_parts.append(text)
                else:
                    text_parts.append(text)
        if tool_uses:
            finish_reason = "tool_calls"
        return "".join(text_parts), "".join(thought_parts), finish_reason, tool_uses

    def _native_complete(self, model, messages, tools, params):
        name_map, reverse_name_map = self._tool_name_maps(tools)
        body = self._native_body(model, messages, tools, dict(params or {}), name_map)
        with self._native_request_json(model, body, timeout=self._request_timeout(params)) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        text, thought, finish_reason, tool_uses = self._native_extract_parts(raw, reverse_name_map)
        content = [{"type": "text", "text": text}]
        content.extend(tool_uses)
        response = {
            "content": content,
            "finish_reason": finish_reason,
            "usage": self._native_usage(raw),
        }
        if thought:
            response["metadata"] = {
                "thinking": {
                    "state": "completed",
                    "transcript": thought,
                    "source": "google_native_thought",
                }
            }
        return response

    def _native_stream(self, model, messages, tools, params):
        name_map, reverse_name_map = self._tool_name_maps(tools)
        body = self._native_body(model, messages, tools, dict(params or {}), name_map)
        resp = self._native_request_json(
            model, body, stream=True, timeout=self._request_timeout(params)
        )
        finish_reason = "stop"
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        try:
            for payload in self._parse_sse_lines(resp):
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                text, thought, candidate_finish, tool_uses = self._native_extract_parts(
                    obj, reverse_name_map
                )
                if thought:
                    yield {"type": "thinking_delta", "delta": {"type": "text", "text": thought}}
                if text:
                    yield {"type": "content_delta", "delta": {"type": "text", "text": text}}
                for tool_use in tool_uses:
                    yield {
                        "type": "tool_call_start",
                        "id": tool_use.get("id", ""),
                        "name": tool_use.get("name", ""),
                    }
                    yield {
                        "type": "tool_call_delta",
                        "id": tool_use.get("id", ""),
                        "name": tool_use.get("name", ""),
                        "arguments_chunk": tool_use.get("input", "{}"),
                    }
                    yield {
                        "type": "tool_call_end",
                        "id": tool_use.get("id", ""),
                        "name": tool_use.get("name", ""),
                    }
                if candidate_finish:
                    finish_reason = candidate_finish
                usage = self._native_usage(obj) or usage
            yield {"type": "stream_end", "finish_reason": finish_reason, "usage": usage}
        finally:
            resp.close()

    @staticmethod
    def _copy_chat_params(body, params):
        for key in (
            "temperature",
            "max_tokens",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "stop",
            "response_format",
            "reasoning_effort",
            "tool_choice",
            "parallel_tool_calls",
            "stream_options",
        ):
            if key in params:
                body[key] = params[key]
        extra_body = params.get("extra_body")
        if isinstance(extra_body, dict):
            body.update(extra_body)

    @staticmethod
    def _split_inline_thoughts(text: str) -> tuple[list[str], str]:
        thoughts: list[str] = []

        def collect(match: re.Match[str]) -> str:
            thought = str(match.group(1) or "").strip()
            if thought:
                thoughts.append(thought)
            return ""

        visible = re.sub(
            r"<thought>(.*?)</thought>", collect, str(text or ""), flags=re.DOTALL
        ).strip()
        return thoughts, visible

    def parse_response(self, raw):
        parsed = super().parse_response(raw)
        thinking_parts: list[str] = []
        for block in parsed.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                thoughts, visible = self._split_inline_thoughts(str(block.get("text") or ""))
                thinking_parts.extend(thoughts)
                block["text"] = visible
        if thinking_parts:
            metadata = dict(parsed.get("metadata") or {})
            existing_value = metadata.get("thinking")
            existing: dict[str, object] = (
                {str(key): value for key, value in existing_value.items()}
                if isinstance(existing_value, dict)
                else {}
            )
            metadata["thinking"] = {
                **existing,
                "state": "completed",
                "transcript": "\n\n".join(thinking_parts),
                "source": "google_inline_thought",
            }
            parsed["metadata"] = metadata
        return parsed

    def _build_request_with_tool_name_map(
        self,
        messages: Any,
        name_map: Dict[str, str] | None = None,
    ) -> List[Dict[str, Any]]:
        request = super().build_request(messages)
        if not isinstance(name_map, dict) or not name_map:
            return request
        remapped: List[Dict[str, Any]] = []
        for message in request:
            if not isinstance(message, dict):
                remapped.append(message)
                continue
            current = dict(message)
            if current.get("role") == "assistant" and isinstance(current.get("tool_calls"), list):
                tool_calls = []
                for tool_call in current.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        tool_calls.append(tool_call)
                        continue
                    mapped_call = dict(tool_call)
                    function_def = (
                        mapped_call.get("function")
                        if isinstance(mapped_call.get("function"), dict)
                        else None
                    )
                    if isinstance(function_def, dict):
                        mapped_function = dict(function_def)
                        mapped_function["name"] = self._remap_tool_name(
                            mapped_function.get("name", ""), name_map
                        )
                        mapped_call["function"] = mapped_function
                    elif mapped_call.get("name"):
                        mapped_call["name"] = self._remap_tool_name(
                            mapped_call.get("name", ""), name_map
                        )
                    tool_calls.append(mapped_call)
                current["tool_calls"] = tool_calls
            elif current.get("role") == "tool":
                current["name"] = self._remap_tool_name(current.get("name", ""), name_map)
            remapped.append(current)
        return remapped

    def _parse_response_with_tool_name_map(
        self,
        raw: Dict[str, Any],
        reverse_name_map: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        parsed = self.parse_response(raw)
        if not isinstance(reverse_name_map, dict) or not reverse_name_map:
            return parsed
        for block in parsed.get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                block["name"] = self._remap_tool_name(block.get("name", ""), reverse_name_map)
        return parsed

    @classmethod
    def _sanitize_tool(
        cls,
        tool: Any,
        name_map: Dict[str, str] | None = None,
    ) -> Dict[str, Any] | None:
        """Return the OpenAI-compatible function-tool shape Google accepts."""
        if not isinstance(tool, dict):
            return None
        function_def = tool.get("function")
        if not isinstance(function_def, dict):
            return None
        name = str(function_def.get("name") or "").strip()
        if not name:
            return None
        sanitized_function: Dict[str, Any] = {"name": cls._remap_tool_name(name, name_map)}
        description = function_def.get("description")
        if isinstance(description, str) and description:
            sanitized_function["description"] = description
        parameters = function_def.get("parameters")
        if isinstance(parameters, dict):
            sanitized_function["parameters"] = parameters
        else:
            sanitized_function["parameters"] = {"type": "object", "properties": {}, "required": []}
        return {"type": "function", "function": sanitized_function}

    @classmethod
    def _sanitize_tools(
        cls,
        tools: Any,
        name_map: Dict[str, str] | None = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(tools, list):
            return []
        sanitized: List[Dict[str, Any]] = []
        for tool in tools:
            item = cls._sanitize_tool(tool, name_map)
            if item is not None:
                sanitized.append(item)
        return sanitized

    def complete(self, model, messages, tools, params):
        if self._use_native_generative_api(model):
            return self._native_complete(model, messages, tools, params)
        translated = self._translate_params(params, model)
        name_map, reverse_name_map = self._tool_name_maps(tools)
        body = {
            "model": model,
            "messages": self._build_request_with_tool_name_map(messages, name_map),
        }
        sanitized_tools = self._sanitize_tools(tools, name_map)
        if sanitized_tools:
            body["tools"] = sanitized_tools
        self._copy_chat_params(body, translated)
        raw = self._request_json(
            "/chat/completions", body, **self._request_timeout_kwargs(translated)
        )
        return self._parse_response_with_tool_name_map(raw, reverse_name_map)

    def stream(self, model, messages, tools, params):
        if self._use_native_generative_api(model):
            yield from self._native_stream(model, messages, tools, params)
            return
        translated = self._translate_params(params, model)
        name_map, reverse_name_map = self._tool_name_maps(tools)
        body = {
            "model": model,
            "messages": self._build_request_with_tool_name_map(messages, name_map),
        }
        sanitized_tools = self._sanitize_tools(tools, name_map)
        if sanitized_tools:
            body["tools"] = sanitized_tools
        self._copy_chat_params(body, translated)
        body.setdefault("stream_options", {"include_usage": True})
        resp = self._request_stream(
            "/chat/completions", body, **self._request_timeout_kwargs(translated)
        )
        tool_call_state: dict[str, dict[str, object]] = {}
        try:
            for payload in self._parse_sse_lines(resp):
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                text = delta.get("content")
                if text:
                    yield {"type": "content_delta", "delta": {"type": "text", "text": text}}
                remapped_delta = dict(delta)
                remapped_tool_calls = []
                for tool_call in delta.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        remapped_tool_calls.append(tool_call)
                        continue
                    mapped_call = dict(tool_call)
                    function_delta = (
                        mapped_call.get("function")
                        if isinstance(mapped_call.get("function"), dict)
                        else None
                    )
                    if isinstance(function_delta, dict) and function_delta.get("name"):
                        mapped_function = dict(function_delta)
                        mapped_function["name"] = self._remap_tool_name(
                            mapped_function.get("name", ""), reverse_name_map
                        )
                        mapped_call["function"] = mapped_function
                    remapped_tool_calls.append(mapped_call)
                remapped_delta["tool_calls"] = remapped_tool_calls
                yield from self._stream_tool_call_events(remapped_delta, tool_call_state)
                finish = choices[0].get("finish_reason")
                if finish:
                    for current in tool_call_state.values():
                        if current.get("started") and not current.get("ended"):
                            current["ended"] = True
                            yield {
                                "type": "tool_call_end",
                                "id": current.get("id", ""),
                                "name": current.get("name", ""),
                            }
                    usage_raw = obj.get("usage") or {}
                    yield {
                        "type": "stream_end",
                        "finish_reason": finish,
                        "usage": {
                            "input_tokens": usage_raw.get("prompt_tokens", 0),
                            "output_tokens": usage_raw.get("completion_tokens", 0),
                            "total_tokens": usage_raw.get("total_tokens", 0),
                        },
                    }
        finally:
            resp.close()
