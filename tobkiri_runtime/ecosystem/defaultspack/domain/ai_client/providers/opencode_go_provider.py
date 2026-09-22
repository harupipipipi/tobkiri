from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List

from .anthropic_provider import AnthropicProvider
from .component_metadata import model_manifests_from_provider_components
from .openai_compatible_provider import OpenAICompatibleProvider


_OPENCODE_GO_MODEL_SPECS: List[Dict[str, Any]] = [
    {
        "model_id": "kimi-k2.7-code",
        "display_name": "Kimi K2.7 Code via OpenCode Go",
        "priority": 0,
        "defaults": {"chat": True},
        "transport": "openai_chat_completions",
        "endpoint_path": "/chat/completions",
        "source": "opencode_go_docs",
    },
    {
        "model_id": "kimi-k2.6",
        "display_name": "Kimi K2.6 via OpenCode Go",
        "priority": 1,
        "defaults": {"chat": True, "coding": True},
        "transport": "openai_chat_completions",
        "endpoint_path": "/chat/completions",
        "source": "opencode_go_docs",
        "tool_calls": True,
        "reasoning": True,
        "vision": True,
        "vision_verified": True,
        "thinking_disabled_for_tool_calls": True,
    },
    {
        "model_id": "glm-5.1",
        "display_name": "GLM-5.1 via OpenCode Go",
        "priority": 2,
        "defaults": {"chat": True},
        "transport": "openai_chat_completions",
        "endpoint_path": "/chat/completions",
        "source": "opencode_go_docs",
    },
    {
        "model_id": "glm-5",
        "display_name": "GLM-5 via OpenCode Go",
        "priority": 3,
        "defaults": {"chat": True},
        "transport": "openai_chat_completions",
        "endpoint_path": "/chat/completions",
        "source": "opencode_go_docs",
    },
    {
        "model_id": "deepseek-v4-pro",
        "display_name": "DeepSeek V4 Pro via OpenCode Go",
        "priority": 4,
        "defaults": {"chat": True},
        "transport": "openai_chat_completions",
        "endpoint_path": "/chat/completions",
        "source": "opencode_go_docs",
    },
    {
        "model_id": "deepseek-v4-flash",
        "display_name": "DeepSeek V4 Flash via OpenCode Go",
        "priority": 5,
        "defaults": {"chat": True, "fast": True, "cheap": True},
        "transport": "openai_chat_completions",
        "endpoint_path": "/chat/completions",
        "source": "opencode_go_docs",
    },
    {
        "model_id": "mimo-v2.5-pro",
        "display_name": "MiMo V2.5 Pro via OpenCode Go",
        "priority": 6,
        "defaults": {"chat": True, "reasoning": True},
        "transport": "openai_chat_completions",
        "endpoint_path": "/chat/completions",
        "source": "opencode_go_docs",
    },
    {
        "model_id": "mimo-v2.5",
        "display_name": "MiMo V2.5 via OpenCode Go",
        "priority": 7,
        "defaults": {"chat": True},
        "transport": "openai_chat_completions",
        "endpoint_path": "/chat/completions",
        "source": "opencode_go_docs",
    },
    {
        "model_id": "mimo-v2.5-free",
        "display_name": "MiMo V2.5 Free compatibility alias via OpenCode Go",
        "priority": 14,
        "defaults": {"chat": True},
        "transport": "openai_chat_completions",
        "endpoint_path": "/chat/completions",
        "source": "opencode_go_compatibility_alias",
        "alias_of": "opencode-go/mimo-v2.5",
        "openai_model": "mimo-v2.5",
        "compatibility_note": (
            "Legacy id maps to OpenCode Go mimo-v2.5; "
            "the real free model is opencode-zen/mimo-v2.5-free."
        ),
    },
    {
        "model_id": "minimax-m3",
        "display_name": "MiniMax M3 via OpenCode Go",
        "priority": 8,
        "defaults": {"chat": True},
        "transport": "anthropic_messages",
        "endpoint_path": "/messages",
        "source": "opencode_go_docs",
    },
    {
        "model_id": "qwen3.7-plus",
        "display_name": "Qwen3.7 Plus via OpenCode Go",
        "priority": 9,
        "defaults": {"chat": True, "general": True},
        "transport": "anthropic_messages",
        "endpoint_path": "/messages",
        "source": "opencode_go_docs",
        "reasoning": True,
        "vision": True,
    },
    {
        "model_id": "qwen3.7-max",
        "display_name": "Qwen3.7 Max via OpenCode Go",
        "priority": 10,
        "defaults": {"chat": True, "reasoning": True},
        "transport": "anthropic_messages",
        "endpoint_path": "/messages",
        "source": "opencode_go_docs",
        "reasoning": True,
    },
    {
        "model_id": "qwen3.6-plus",
        "display_name": "Qwen3.6 Plus via OpenCode Go",
        "priority": 11,
        "defaults": {"chat": True},
        "transport": "anthropic_messages",
        "endpoint_path": "/messages",
        "source": "opencode_go_docs",
    },
    {
        "model_id": "minimax-m2.7",
        "display_name": "MiniMax M2.7 via OpenCode Go",
        "priority": 12,
        "defaults": {"chat": True},
        "transport": "anthropic_messages",
        "endpoint_path": "/messages",
        "source": "opencode_go_docs",
    },
    {
        "model_id": "minimax-m2.5",
        "display_name": "MiniMax M2.5 via OpenCode Go",
        "priority": 13,
        "defaults": {"chat": True},
        "transport": "anthropic_messages",
        "endpoint_path": "/messages",
        "source": "opencode_go_docs",
    },
]
_OPENCODE_GO_MODEL_ALIASES = {"mimo-v2.5-free": "mimo-v2.5"}
_OPENCODE_GO_TOOL_CALL_MODELS = {"kimi-k2.6", "mimo-v2.5", "mimo-v2.5-pro"}
_OPENCODE_GO_REASONING_MODELS = {"kimi-k2.6", "mimo-v2.5", "mimo-v2.5-pro"}
_OPENCODE_GO_REASONING_EFFORT_MODELS = {"mimo-v2.5", "mimo-v2.5-pro"}
_OPENCODE_GO_NATIVE_THINKING_MODELS = {"kimi-k2.6"}
_OPENCODE_GO_DISABLE_THINKING_FOR_TOOL_MODELS = {"kimi-k2.6"}
_OPENCODE_GO_OPENAI_VISION_MODELS = {"kimi-k2.6"}


def _known_model_entry(spec: Dict[str, Any]) -> Dict[str, Any]:
    model_id = spec["model_id"]
    runtime_model_id = str(
        spec.get("openai_model") or _OPENCODE_GO_MODEL_ALIASES.get(model_id) or model_id
    )
    defaults = dict(spec.get("defaults", {}))
    tool_calls = bool(spec.get("tool_calls", runtime_model_id in _OPENCODE_GO_TOOL_CALL_MODELS))
    reasoning = bool(spec.get("reasoning", runtime_model_id in _OPENCODE_GO_REASONING_MODELS))
    verified_reasoning_effort = runtime_model_id in _OPENCODE_GO_REASONING_EFFORT_MODELS
    metadata = {
        "transport": spec["transport"],
        "endpoint_path": spec["endpoint_path"],
        "source": spec["source"],
    }
    for key in ("alias_of", "openai_model"):
        if spec.get(key):
            metadata[key] = spec[key]
    if spec.get("compatibility_note"):
        metadata["compatibility_note"] = spec["compatibility_note"]
    if "free_tier" in spec:
        metadata["free_tier"] = bool(spec["free_tier"])
    if tool_calls:
        metadata["tool_calls_verified"] = True
    if verified_reasoning_effort:
        metadata["reasoning_effort_verified"] = True
    for key in (
        "experimental",
        "vision_unverified",
        "vision_verified",
        "thinking_disabled_for_tool_calls",
    ):
        if spec.get(key):
            metadata[key] = True
    return {
        "id": "opencode-go/{}".format(model_id),
        "category": "llm_model",
        "version": "1",
        "provider": "opencode-go",
        "provider_id": "opencode-go",
        "model_id": model_id,
        "name": spec["display_name"],
        "display_name": spec["display_name"],
        "type": "chat",
        "enabled": True,
        "priority": spec["priority"],
        "defaults": defaults,
        "capabilities": {
            "chat": True,
            "streaming": True,
            "tool_calls": tool_calls,
            "vision": bool(spec.get("vision", defaults.get("vision", False))),
            "reasoning": reasoning,
        },
        "supports_thinking": reasoning,
        "thinking_levels": ["low", "medium", "high"] if reasoning else [],
        "default_thinking_level": "medium" if reasoning else None,
        "metadata": metadata,
    }


class OpencodeGoProvider(OpenAICompatibleProvider):
    """OpenCode Go provider with per-model endpoint routing."""

    provider_name = "opencode-go"
    display_name = "OpenCode Go"
    BASE_URL = "https://opencode.ai/zen/go/v1"
    API_VERSION = "2023-06-01"

    OPENAI_CHAT_MODELS = {
        "glm-5.1",
        "glm-5",
        "kimi-k2.7-code",
        "kimi-k2.6",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "mimo-v2.5-pro",
        "mimo-v2.5",
    }
    MODEL_ALIASES = dict(_OPENCODE_GO_MODEL_ALIASES)
    ANTHROPIC_MESSAGES_MODELS = {
        "minimax-m3",
        "minimax-m2.7",
        "minimax-m2.5",
        "qwen3.7-plus",
        "qwen3.7-max",
        "qwen3.6-plus",
    }
    MODEL_IDS = OPENAI_CHAT_MODELS | ANTHROPIC_MESSAGES_MODELS | set(MODEL_ALIASES)
    TOOL_CALL_MODELS = set(_OPENCODE_GO_TOOL_CALL_MODELS)
    KNOWN_MODELS = [_known_model_entry(spec) for spec in _OPENCODE_GO_MODEL_SPECS]
    _OPENAI_CHAT_PARAM_KEYS = {
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "response_format",
        "stream_options",
        "tool_choice",
        "reasoning_effort",
        "thinking",
    }
    _MESSAGES_PARAM_KEYS = {
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "metadata",
    }
    _anthropic_role = staticmethod(AnthropicProvider._anthropic_role)
    _tool_use_parts = staticmethod(AnthropicProvider._tool_use_parts)
    _tool_result_part = staticmethod(AnthropicProvider._tool_result_part)
    _content_parts = staticmethod(AnthropicProvider._content_parts)

    def __init__(self) -> None:
        catalog_models = model_manifests_from_provider_components("opencode-go")
        super().__init__(
            provider_id="opencode-go",
            display_name="OpenCode Go",
            api_key_env=["OPENCODE_GO_API_KEY", "OPENCODE_ZEN_API_KEY"],
            base_url_env="OPENCODE_GO_BASE_URL",
            default_base_url=self.BASE_URL,
            credential_required=True,
            known_models=catalog_models,
            remote_model_discovery=True,
        )

    @classmethod
    def _normalize_model_id(cls, model: str) -> str:
        model_id = str(model or "").strip()
        if model_id.startswith("opencode-go/"):
            model_id = model_id.split("/", 1)[1]
        return cls.MODEL_ALIASES.get(model_id, model_id)

    @classmethod
    def _assert_supported_model(cls, model: str) -> str:
        raw_model_id = str(model or "").strip()
        if raw_model_id.startswith("opencode-go/"):
            raw_model_id = raw_model_id.split("/", 1)[1]
        allowed = {
            str(item.get("model_id") or "").strip()
            for item in model_manifests_from_provider_components("opencode-go")
        }
        if raw_model_id not in allowed:
            raise RuntimeError(
                "unsupported model for opencode-go: "
                f"{model}; allowed models: {', '.join(sorted(allowed))}"
            )
        return cls._normalize_model_id(raw_model_id)

    @staticmethod
    def _translate_params(params):
        raw = dict(params or {})
        translated = {
            key: raw[key] for key in OpencodeGoProvider._OPENAI_CHAT_PARAM_KEYS if key in raw
        }
        for key in ("request_timeout", "timeout"):
            if key in raw:
                translated[key] = raw[key]
        return translated

    @staticmethod
    def _request_timeout(params) -> float:
        raw = dict(params or {})
        value = raw.get("request_timeout", raw.get("timeout", 120))
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            timeout = 120.0
        return max(2.0, min(timeout, 120.0))

    @staticmethod
    def _copy_chat_params(body, params):
        for key in OpencodeGoProvider._OPENAI_CHAT_PARAM_KEYS:
            if key in params:
                body[key] = params[key]

    @classmethod
    def _translate_messages_params(cls, params):
        raw = dict(params or {})
        translated = {key: raw[key] for key in cls._MESSAGES_PARAM_KEYS if key in raw}
        max_tokens = raw.get("max_tokens", raw.get("max_completion_tokens", 4096))
        translated["max_tokens"] = max_tokens
        if "stop" in raw and "stop_sequences" not in translated:
            stop = raw["stop"]
            translated["stop_sequences"] = stop if isinstance(stop, list) else [stop]
        return translated

    @classmethod
    def _copy_messages_params(cls, body, params):
        for key in cls._MESSAGES_PARAM_KEYS:
            if key in params:
                body[key] = params[key]

    def _messages_headers(self):
        headers = {
            "Authorization": "Bearer " + self._api_key,
            "x-api-key": self._api_key,
            "anthropic-version": self.API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "RumiAI/1.0",
        }
        return headers

    def _request_messages_json(self, path, body, *, timeout=120.0):
        self._ensure_runtime_config()
        url = self.BASE_URL + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=self._messages_headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout) as resp:
                raw_bytes = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("OpenCode Go API error {}: {}".format(exc.code, err_body))
        except urllib.error.URLError as exc:
            raise RuntimeError("OpenCode Go API connection error: {}".format(exc.reason))
        try:
            return json.loads(raw_bytes)
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError("OpenCode Go API returned invalid JSON: {}".format(raw_bytes[:500]))

    def _request_messages_stream(self, path, body, *, timeout=120.0):
        self._ensure_runtime_config()
        body["stream"] = True
        url = self.BASE_URL + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=self._messages_headers(), method="POST"
        )
        try:
            return urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("OpenCode Go API error {}: {}".format(exc.code, err_body))
        except urllib.error.URLError as exc:
            raise RuntimeError("OpenCode Go API connection error: {}".format(exc.reason))

    def list_models(self) -> List[Dict[str, Any]]:
        return self._merge_remote_models(self.KNOWN_MODELS)

    def _messages_body(self, model_id, messages, params):
        params = self._translate_messages_params(params)
        system_parts, converted = AnthropicProvider.build_request(self, messages)
        body = {
            "model": model_id,
            "messages": converted,
            "max_tokens": params.get("max_tokens", 4096),
        }
        if system_parts:
            body["system"] = system_parts
        self._copy_messages_params(body, params)
        return body

    def _complete_messages(self, model_id, messages, params):
        body = self._messages_body(model_id, messages, params)
        raw = self._request_messages_json("/messages", body, **self._request_timeout_kwargs(params))
        return AnthropicProvider.parse_response(self, raw)

    def _stream_messages(self, model_id, messages, params):
        body = self._messages_body(model_id, messages, params)
        resp = self._request_messages_stream(
            "/messages", body, **self._request_timeout_kwargs(params)
        )
        usage_accum = {"input_tokens": 0, "output_tokens": 0}
        tool_call_state = {}
        try:
            for event_type, data_str in AnthropicProvider._parse_sse(resp):
                try:
                    obj = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if event_type == "message_start":
                    msg = obj.get("message", {})
                    usage = msg.get("usage", {})
                    usage_accum["input_tokens"] = usage.get("input_tokens", 0)
                elif event_type == "content_block_start":
                    yield from AnthropicProvider._anthropic_stream_tool_call_events(
                        event_type, obj, tool_call_state
                    )
                elif event_type == "content_block_delta":
                    delta = obj.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield {
                            "type": "content_delta",
                            "delta": {"type": "text", "text": delta.get("text", "")},
                        }
                    yield from AnthropicProvider._anthropic_stream_tool_call_events(
                        event_type, obj, tool_call_state
                    )
                elif event_type == "content_block_stop":
                    yield from AnthropicProvider._anthropic_stream_tool_call_events(
                        event_type, obj, tool_call_state
                    )
                elif event_type == "message_delta":
                    delta = obj.get("delta", {})
                    usage = obj.get("usage", {})
                    usage_accum["output_tokens"] = usage.get("output_tokens", 0)
                    stop = delta.get("stop_reason", "end_turn") or "end_turn"
                    finish_map = {
                        "end_turn": "stop",
                        "max_tokens": "length",
                        "stop_sequence": "stop",
                        "tool_use": "tool_calls",
                    }
                    finish = finish_map.get(stop, stop)
                    yield from AnthropicProvider._anthropic_stream_tool_call_end_events(
                        tool_call_state
                    )
                    yield {
                        "type": "stream_end",
                        "finish_reason": finish,
                        "usage": {
                            "input_tokens": usage_accum["input_tokens"],
                            "output_tokens": usage_accum["output_tokens"],
                            "total_tokens": usage_accum["input_tokens"]
                            + usage_accum["output_tokens"],
                        },
                    }
        finally:
            resp.close()

    @staticmethod
    def _model_params(
        params,
        *,
        model_id: str,
        supports_tools: bool,
        supports_reasoning_effort: bool,
        supports_native_thinking: bool,
        tools_present: bool,
    ):
        filtered = dict(params or {})
        if not supports_tools:
            filtered.pop("tool_choice", None)
            filtered.pop("parallel_tool_calls", None)
        if not supports_reasoning_effort:
            filtered.pop("reasoning_effort", None)
        if not supports_native_thinking:
            filtered.pop("thinking", None)
        if tools_present and model_id in _OPENCODE_GO_DISABLE_THINKING_FOR_TOOL_MODELS:
            filtered["thinking"] = {"type": "disabled"}
            filtered.pop("reasoning_effort", None)
        if "request_timeout" in (params or {}):
            filtered["request_timeout"] = params["request_timeout"]
        if "timeout" in (params or {}):
            filtered["timeout"] = params["timeout"]
        return filtered

    def complete(self, model, messages, tools, params):
        model_id = self._assert_supported_model(model)
        if model_id in self.ANTHROPIC_MESSAGES_MODELS:
            return self._complete_messages(model_id, messages, params)
        supports_tools = model_id in self.TOOL_CALL_MODELS
        supports_reasoning_effort = model_id in _OPENCODE_GO_REASONING_EFFORT_MODELS
        supports_native_thinking = model_id in _OPENCODE_GO_NATIVE_THINKING_MODELS
        forward_tools = tools if supports_tools else []
        forward_params = self._model_params(
            params,
            model_id=model_id,
            supports_tools=supports_tools,
            supports_reasoning_effort=supports_reasoning_effort,
            supports_native_thinking=supports_native_thinking,
            tools_present=bool(forward_tools),
        )
        return super().complete(model_id, messages, forward_tools, forward_params)

    def stream(self, model, messages, tools, params):
        model_id = self._assert_supported_model(model)
        if model_id in self.ANTHROPIC_MESSAGES_MODELS:
            yield from self._stream_messages(model_id, messages, params)
            return
        supports_tools = model_id in self.TOOL_CALL_MODELS
        supports_reasoning_effort = model_id in _OPENCODE_GO_REASONING_EFFORT_MODELS
        supports_native_thinking = model_id in _OPENCODE_GO_NATIVE_THINKING_MODELS
        forward_tools = tools if supports_tools else []
        forward_params = self._model_params(
            params,
            model_id=model_id,
            supports_tools=supports_tools,
            supports_reasoning_effort=supports_reasoning_effort,
            supports_native_thinking=supports_native_thinking,
            tools_present=bool(forward_tools),
        )
        yield from super().stream(model_id, messages, forward_tools, forward_params)

    def embed(self, model, input_text):
        raise NotImplementedError("OpenCode Go does not support embeddings.")

    def image_gen(self, model, prompt, params):
        raise NotImplementedError("OpenCode Go does not support image generation.")

    @staticmethod
    def _anthropic_image_source(image):
        if image.startswith("data:"):
            header, b64 = image.split(",", 1) if "," in image else ("", image)
            media = "image/png"
            if "image/jpeg" in header:
                media = "image/jpeg"
            elif "image/gif" in header:
                media = "image/gif"
            elif "image/webp" in header:
                media = "image/webp"
            return {"type": "base64", "media_type": media, "data": b64}
        if image.startswith("http"):
            return {"type": "url", "url": image}
        return {"type": "base64", "media_type": "image/png", "data": image}

    def image_analyze(self, model, image, prompt):
        model_id = self._assert_supported_model(model)
        if model_id in _OPENCODE_GO_OPENAI_VISION_MODELS:
            return super().image_analyze(model_id, image, prompt)
        if model_id in self.ANTHROPIC_MESSAGES_MODELS:
            body = {
                "model": model_id,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": self._anthropic_image_source(image)},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                "max_tokens": 4096,
            }
            raw = self._request_messages_json("/messages", body)
            text = ""
            for block in raw.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
            return {"text": text}
        raise NotImplementedError("OpenCode Go vision support is not verified for this model.")

    def transcribe(self, model, audio, params):
        raise NotImplementedError("OpenCode Go does not support audio transcription.")

    def tts(self, model, text, voice):
        raise NotImplementedError("OpenCode Go does not support text-to-speech.")
