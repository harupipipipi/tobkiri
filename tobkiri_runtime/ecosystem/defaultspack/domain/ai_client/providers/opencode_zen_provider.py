from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from ..api_key_store import read_provider_api_key
from .anthropic_provider import AnthropicProvider
from .openai_provider import OpenAIProvider


class OpencodeZenProvider(AnthropicProvider):
    """OpenCode Zen provider backed by the account-visible live inventory."""

    provider_name = "opencode-zen"
    display_name = "OpenCode Zen"
    DEFAULT_BASE_URL = "https://opencode.ai/zen"
    MODEL_INVENTORY_TTL_SECONDS = 300
    ANTHROPIC_MESSAGES_MODELS: set[str] = set()
    OPENAI_CHAT_MODELS: set[str] = set()
    MODEL_IDS = ANTHROPIC_MESSAGES_MODELS | OPENAI_CHAT_MODELS
    KNOWN_MODELS: List[Dict[str, Any]] = []
    VERIFIED_TOOL_MODELS = {"deepseek-v4-flash-free", "mimo-v2.5-free"}
    _OPENAI_CHAT_PARAM_KEYS = {
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "stream_options",
        "tool_choice",
        "parallel_tool_calls",
    }
    _message_reasoning_content = staticmethod(OpenAIProvider._message_reasoning_content)

    def __init__(self) -> None:
        self._api_key = str(read_provider_api_key("opencode-zen", "default") or "")
        self._ssl_ctx = ssl.create_default_context()
        self.BASE_URL = os.environ.get("OPENCODE_ZEN_BASE_URL", self.DEFAULT_BASE_URL).rstrip("/")
        self._model_inventory_cache: List[Dict[str, Any]] = []
        self._model_inventory_expires_at = 0.0

    @classmethod
    def _normalize_model_id(cls, model: str) -> str:
        model_id = str(model or "").strip()
        if model_id.startswith("opencode-zen/"):
            model_id = model_id.split("/", 1)[1]
        if model_id.startswith("opencode/"):
            model_id = model_id.split("/", 1)[1]
        return model_id

    def _assert_supported_model(self, model: str) -> str:
        model_id = self._normalize_model_id(model)
        if not self._model_inventory_cache and self._api_key:
            self.list_models()
        discovered = {
            str(item.get("model_id") or "").strip()
            for item in self._model_inventory_cache
            if isinstance(item, dict)
        }
        if model_id not in self.MODEL_IDS | self.VERIFIED_TOOL_MODELS | discovered:
            raise RuntimeError(f"unsupported model: {model_id}")
        return model_id

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": "Bearer " + self._api_key,
            "x-api-key": self._api_key,
            "anthropic-version": self.API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "RumiAI/1.0",
        }

    def _openai_headers(self) -> Dict[str, str]:
        return {
            "Authorization": "Bearer " + self._api_key,
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "RumiAI/1.0",
        }

    def _request_openai_json(self, path, body, *, timeout=120.0):
        url = self.BASE_URL + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._openai_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout) as resp:
                raw_bytes = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("OpenCode Zen API error {}: {}".format(exc.code, err_body))
        except urllib.error.URLError as exc:
            raise RuntimeError("OpenCode Zen API connection error: {}".format(exc.reason))
        try:
            return json.loads(raw_bytes)
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError("OpenCode Zen API returned invalid JSON: {}".format(raw_bytes[:500]))

    def _request_openai_stream(self, path, body, *, timeout=120.0):
        url = self.BASE_URL + path
        body["stream"] = True
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._openai_headers(), method="POST")
        try:
            return urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("OpenCode Zen API error {}: {}".format(exc.code, err_body))
        except urllib.error.URLError as exc:
            raise RuntimeError("OpenCode Zen API connection error: {}".format(exc.reason))

    def list_models(self) -> List[Dict[str, Any]]:
        now = time.monotonic()
        if self._model_inventory_cache and now < self._model_inventory_expires_at:
            return [dict(model) for model in self._model_inventory_cache]
        if not self._api_key:
            return []
        request = urllib.request.Request(
            self.BASE_URL + "/v1/models",
            headers=self._openai_headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError:
            return self._inventory_fallback("http_error")
        except urllib.error.URLError:
            return self._inventory_fallback("connection_error")
        except TimeoutError:
            return self._inventory_fallback("timeout")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return self._inventory_fallback("invalid_response")
        records = payload.get("data") if isinstance(payload, dict) else []
        models = []
        for raw in records if isinstance(records, list) else []:
            item = raw if isinstance(raw, dict) else {"id": raw}
            model_id = str(item.get("id") or item.get("model_id") or item.get("name") or "").strip()
            if not model_id or any(model["model_id"] == model_id for model in models):
                continue
            display_name = str(item.get("display_name") or item.get("displayName") or model_id)
            raw_capabilities = item.get("capabilities")
            tool_calling = model_id in self.VERIFIED_TOOL_MODELS
            if isinstance(raw_capabilities, dict):
                tool_calling = tool_calling or bool(
                    raw_capabilities.get("tool_calling")
                    or raw_capabilities.get("tools")
                    or raw_capabilities.get("function_calling")
                )
            elif isinstance(raw_capabilities, list):
                tool_calling = tool_calling or bool(
                    {str(value).strip().casefold() for value in raw_capabilities}
                    & {"tool_calling", "tools", "function_calling"}
                )
            model = {
                "id": f"opencode-zen/{model_id}",
                "model_id": model_id,
                "provider_id": "opencode-zen",
                "provider": "opencode-zen",
                "name": display_name,
                "display_name": display_name,
                "type": "chat",
                "capabilities": {
                    "chat": True,
                    "text_input": True,
                    "text_output": True,
                    "streaming": True,
                    "tool_calling": tool_calling,
                    "image_input": False,
                    "vision": False,
                },
                "metadata": {
                    "transport": "openai_chat_completions",
                    "endpoint_path": "/v1/chat/completions",
                    "source": "openai_models_endpoint",
                    "source_endpoint": "/v1/models",
                    "inventory_source": "live",
                    "visibility_scope": "account",
                    "tool_calling_verified": tool_calling,
                },
            }
            models.append(model)
        if models:
            self._model_inventory_cache = [dict(model) for model in models]
            self._model_inventory_expires_at = now + self.MODEL_INVENTORY_TTL_SECONDS
            return [dict(model) for model in models]
        return self._inventory_fallback("empty_inventory")

    def _inventory_fallback(self, reason: str) -> List[Dict[str, Any]]:
        if self._model_inventory_cache:
            models: List[Dict[str, Any]] = []
            for raw in self._model_inventory_cache:
                model = dict(raw)
                metadata = dict(model.get("metadata") or {})
                metadata.update(
                    {
                        "inventory_source": "last_known_good",
                        "inventory_fallback_reason": reason,
                        "inventory_stale": True,
                    }
                )
                model["metadata"] = metadata
                models.append(model)
            return models
        return []

    @staticmethod
    def _params_with_token_floor(params: Dict[str, Any] | None) -> Dict[str, Any]:
        next_params = dict(params or {})
        try:
            requested = int(next_params.get("max_tokens", 4096) or 4096)
        except (TypeError, ValueError):
            requested = 4096
        next_params["max_tokens"] = max(requested, 96)
        return next_params

    @staticmethod
    def _model_needs_token_floor(model_id: str) -> bool:
        token = str(model_id or "").strip().lower()
        return token.startswith(("deepseek-", "minimax-"))

    @classmethod
    def _openai_params(cls, params: Dict[str, Any] | None) -> Dict[str, Any]:
        raw = dict(params or {})
        translated = {key: raw[key] for key in cls._OPENAI_CHAT_PARAM_KEYS if key in raw}
        for key in ("request_timeout", "timeout"):
            if key in raw:
                translated[key] = raw[key]
        return translated

    @staticmethod
    def _request_timeout(params: Dict[str, Any] | None) -> float:
        raw = dict(params or {})
        value = raw.get("request_timeout", raw.get("timeout", 120))
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            timeout = 120.0
        return max(2.0, min(timeout, 120.0))

    def _request_timeout_kwargs(self, params: Dict[str, Any] | None) -> Dict[str, float]:
        raw = dict(params or {})
        if "request_timeout" not in raw and "timeout" not in raw:
            return {}
        return {"timeout": self._request_timeout(raw)}

    @classmethod
    def _copy_openai_chat_params(cls, body: Dict[str, Any], params: Dict[str, Any]) -> None:
        for key in cls._OPENAI_CHAT_PARAM_KEYS:
            if key in params:
                body[key] = params[key]

    def _complete_openai_chat(self, model_id, messages, tools, params):
        params = self._openai_params(params)
        body = {"model": model_id, "messages": OpenAIProvider.build_request(self, messages)}
        if tools:
            body["tools"] = self._normalize_openai_tools(tools)
        self._copy_openai_chat_params(body, params)
        raw = self._request_openai_json(
            "/v1/chat/completions",
            body,
            **self._request_timeout_kwargs(params),
        )
        return OpenAIProvider.parse_response(self, raw)

    def _stream_openai_chat(self, model_id, messages, tools, params):
        params = self._openai_params(params)
        body = {"model": model_id, "messages": OpenAIProvider.build_request(self, messages)}
        if tools:
            body["tools"] = self._normalize_openai_tools(tools)
        self._copy_openai_chat_params(body, params)
        body.setdefault("stream_options", {"include_usage": True})
        resp = self._request_openai_stream(
            "/v1/chat/completions",
            body,
            **self._request_timeout_kwargs(params),
        )
        tool_call_state = {}
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        finish_reason = "stop"
        try:
            for payload in OpenAIProvider._parse_sse_lines(resp):
                payload = str(payload or "").strip()
                if not payload:
                    continue
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                usage_raw = obj.get("usage") or {}
                if usage_raw:
                    usage = {
                        "input_tokens": usage_raw.get("prompt_tokens", 0),
                        "output_tokens": usage_raw.get("completion_tokens", 0),
                        "total_tokens": usage_raw.get("total_tokens", 0),
                    }
                choices = obj.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta", {})
                delta = delta if isinstance(delta, dict) else {}
                # OpenCode Zen occasionally emits the completed tool call on
                # ``message`` (or directly on the choice) instead of streaming
                # it through ``delta``.  Preserve that valid OpenAI-compatible
                # response rather than ending with ``finish_reason=tool_calls``
                # and no executable call.
                if not delta.get("tool_calls"):
                    message = choice.get("message")
                    if isinstance(message, dict) and message.get("tool_calls"):
                        delta = {**delta, "tool_calls": message["tool_calls"]}
                    elif choice.get("tool_calls"):
                        delta = {**delta, "tool_calls": choice["tool_calls"]}
                if isinstance(delta.get("tool_calls"), dict):
                    delta = {**delta, "tool_calls": [delta["tool_calls"]]}
                text = delta.get("content")
                if text:
                    yield {"type": "content_delta", "delta": {"type": "text", "text": text}}
                reasoning_text = (
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("thinking")
                )
                if reasoning_text:
                    yield {
                        "type": "reasoning_delta",
                        "delta": {"type": "text", "text": str(reasoning_text)},
                    }
                yield from OpenAIProvider._stream_tool_call_events(delta, tool_call_state)
                finish = choice.get("finish_reason")
                if finish:
                    finish_reason = str(finish)
                    for current in tool_call_state.values():
                        if current.get("started") and not current.get("ended"):
                            current["ended"] = True
                            yield {
                                "type": "tool_call_end",
                                "id": current.get("id", ""),
                                "name": current.get("name", ""),
                            }
            if finish_reason == "tool_calls" and not any(
                current.get("started") for current in tool_call_state.values()
            ):
                recovery_body = {
                    key: value for key, value in body.items() if key != "stream_options"
                }
                recovered = OpenAIProvider.parse_response(
                    self,
                    self._request_openai_json(
                        "/v1/chat/completions",
                        recovery_body,
                        **self._request_timeout_kwargs(params),
                    ),
                )
                recovered_usage = recovered.get("usage") or {}
                usage = {
                    key: int(usage.get(key) or 0) + int(recovered_usage.get(key) or 0)
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                    )
                }
                for item in recovered.get("content") or []:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
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
            yield {
                "type": "stream_end",
                "finish_reason": finish_reason,
                "usage": usage,
            }
        finally:
            resp.close()

    def complete(self, model, messages, tools, params):
        model_id = self._assert_supported_model(model)
        self._assert_text_only_messages(messages)
        if model_id in self.ANTHROPIC_MESSAGES_MODELS:
            del tools
            return super().complete(model_id, messages, [], self._params_with_token_floor(params))
        next_params = (
            self._params_with_token_floor(params)
            if self._model_needs_token_floor(model_id)
            else params
        )
        return self._complete_openai_chat(model_id, messages, tools, next_params)

    def stream(self, model, messages, tools, params):
        model_id = self._assert_supported_model(model)
        self._assert_text_only_messages(messages)
        if model_id in self.ANTHROPIC_MESSAGES_MODELS:
            del tools
            yield from super().stream(model_id, messages, [], self._params_with_token_floor(params))
            return
        next_params = (
            self._params_with_token_floor(params)
            if self._model_needs_token_floor(model_id)
            else params
        )
        yield from self._stream_openai_chat(model_id, messages, tools, next_params)

    @staticmethod
    def _assert_text_only_messages(messages: Any) -> None:
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            blocks = content if isinstance(content, list) else [content]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "").strip().casefold()
                source = block.get("source")
                source_media_type = (
                    str(source.get("media_type") or "").strip().casefold()
                    if isinstance(source, dict)
                    else ""
                )
                media_type = (
                    str(block.get("media_type") or block.get("mime_type") or "").strip().casefold()
                )
                if (
                    block_type in {"image", "image_url", "input_image"}
                    or source_media_type.startswith("image/")
                    or media_type.startswith("image/")
                ):
                    raise RuntimeError(
                        "OpenCode Zen is configured as text-only; image input is not supported"
                    )

    @staticmethod
    def _normalize_openai_tools(tools):
        normalized = []
        for raw in tools if isinstance(tools, list) else []:
            if not isinstance(raw, dict):
                raise RuntimeError("OpenCode Zen Tool must be an object")
            if raw.get("type") == "function" and isinstance(raw.get("function"), dict):
                function = dict(raw["function"])
                if not function.get("name"):
                    raise RuntimeError("OpenCode Zen Tool name is required")
                function.setdefault(
                    "parameters",
                    {"type": "object", "properties": {}},
                )
                normalized.append({"type": "function", "function": function})
                continue
            name = str(raw.get("name") or "").strip()
            schema = raw.get("input_schema")
            if not name or not isinstance(schema, dict):
                raise RuntimeError("OpenCode Zen Tool must use a supported function schema")
            normalized.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": str(raw.get("description") or ""),
                        "parameters": dict(schema),
                    },
                }
            )
        return normalized
