import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import json
import hashlib
import time
import urllib.request
import urllib.error
import urllib.parse
import base64
import ssl

from ..base_provider import BaseProvider
from ..api_key_store import read_provider_api_key


class OpenAIProvider(BaseProvider):
    """OpenAI API provider whose catalog comes from the authenticated account."""

    BASE_URL = "https://api.openai.com/v1"

    # Never seed availability from a release snapshot. /models is scoped to
    # the API key and is the only authority for what this account may use.
    KNOWN_MODELS: list[dict[str, object]] = []
    _MODEL_INVENTORY_CACHE: dict[str, tuple[float, list[dict[str, object]]]] = {}
    _MODEL_INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self, api_key: str | None = None):
        self._api_key = str(api_key or read_provider_api_key("openai", "legacy") or "").strip()
        # Provider discovery must not disappear merely because a minimal host
        # environment lacks Windows certificate-location variables.  Requests
        # still use urllib's verified default context when this construction is
        # deferred; no insecure TLS fallback is introduced.
        self._ssl_ctx: ssl.SSLContext | None
        try:
            self._ssl_ctx = ssl.create_default_context()
        except ssl.SSLError:
            self._ssl_ctx = None

    # ── internal helpers ────────────────────────────────────────────────

    def _headers(self, content_type="application/json"):
        h = {
            "Authorization": "Bearer " + self._api_key,
            "User-Agent": "RumiAI/1.0",
            "Accept": "application/json",
        }
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _request_json(self, path, body, *, timeout=120.0):
        """POST して JSON をパースして返す"""
        url = self.BASE_URL + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout) as resp:
                raw_bytes = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError("OpenAI API error {}: {}".format(e.code, err_body))
        except urllib.error.URLError as e:
            raise RuntimeError("OpenAI API connection error: {}".format(e.reason))
        try:
            return json.loads(raw_bytes)
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError("OpenAI API returned invalid JSON: {}".format(raw_bytes[:500]))

    def _model_inventory_scope(self):
        return hashlib.sha256(str(self._api_key or "").encode("utf-8")).hexdigest()

    def _fetch_live_models(self):
        if not self._api_key:
            return []
        request = urllib.request.Request(
            self.BASE_URL + "/models",
            headers=self._headers(content_type=None),
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
            return []
        records = payload.get("data") if isinstance(payload, dict) else []
        return records if isinstance(records, list) else []

    @staticmethod
    def _live_model_record(raw):
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("id") or "").strip()
        if not model_id:
            return None
        token = model_id.lower()
        if "embedding" in token:
            model_type = "embedding"
        elif "transcrib" in token or "whisper" in token:
            model_type = "transcription"
        elif "tts" in token:
            model_type = "tts"
        elif "image" in token or token.startswith("dall-e"):
            model_type = "image_gen"
        else:
            model_type = "chat"
        chat = model_type == "chat"
        return {
            "id": f"openai/{model_id}",
            "model_id": model_id,
            "provider_id": "openai",
            "provider": "openai",
            "name": model_id,
            "display_name": model_id,
            "type": model_type,
            "capabilities": {
                "chat": chat,
                "text_input": chat or model_type == "embedding",
                "text_output": chat,
                "streaming": chat,
                "embeddings": model_type == "embedding",
                "image_generation": model_type == "image_gen",
                "transcription": model_type == "transcription",
                "tts": model_type == "tts",
            },
            "metadata": {
                "source": "native_models_endpoint",
                "source_endpoint": "/models",
                "visibility_scope": "account",
                "capability_confidence": "inferred_from_model_id",
                "owned_by": str(raw.get("owned_by") or ""),
                "created": raw.get("created"),
            },
        }

    def list_models(self):
        if not self._api_key:
            return []
        scope = self._model_inventory_scope()
        cached = self._MODEL_INVENTORY_CACHE.get(scope)
        now = time.monotonic()
        if cached and cached[0] > now:
            return [dict(model) for model in cached[1]]
        models: list[dict[str, object]] = []
        for raw in self._fetch_live_models():
            model = self._live_model_record(raw)
            if model and all(item["model_id"] != model["model_id"] for item in models):
                models.append(model)
        if models:
            self._MODEL_INVENTORY_CACHE[scope] = (
                now + self._MODEL_INVENTORY_CACHE_TTL_SECONDS,
                [dict(model) for model in models],
            )
        return models

    def _request_stream(self, path, body, *, timeout=120.0):
        """POST して SSE ストリームを返す (generator)"""
        url = self.BASE_URL + path
        body["stream"] = True
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            resp = urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError("OpenAI API error {}: {}".format(e.code, err_body))
        except urllib.error.URLError as e:
            raise RuntimeError("OpenAI API connection error: {}".format(e.reason))
        return resp

    def _request_multipart(self, path, fields, files):
        """multipart/form-data で POST"""
        boundary = "----RumiAIBoundary9876543210"
        body_parts = []
        for key, value in fields.items():
            body_parts.append("--{}".format(boundary).encode())
            body_parts.append('Content-Disposition: form-data; name="{}"'.format(key).encode())
            body_parts.append(b"")
            if isinstance(value, bytes):
                body_parts.append(value)
            else:
                body_parts.append(str(value).encode("utf-8"))
        for key, (filename, filedata, mime) in files.items():
            body_parts.append("--{}".format(boundary).encode())
            body_parts.append(
                'Content-Disposition: form-data; name="{}"; filename="{}"'.format(
                    key, filename
                ).encode()
            )
            body_parts.append("Content-Type: {}".format(mime).encode())
            body_parts.append(b"")
            body_parts.append(filedata)
        body_parts.append("--{}--".format(boundary).encode())
        body_bytes = b"\r\n".join(body_parts)
        ct = "multipart/form-data; boundary={}".format(boundary)
        url = self.BASE_URL + path
        req = urllib.request.Request(url, data=body_bytes, method="POST")
        req.add_header("Authorization", "Bearer " + self._api_key)
        req.add_header("Content-Type", ct)
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError("OpenAI API error {}: {}".format(e.code, err_body))
        except urllib.error.URLError as e:
            raise RuntimeError("OpenAI API connection error: {}".format(e.reason))

    @staticmethod
    def _parse_sse_lines(resp):
        """HTTPResponse から SSE の data 行を yield する"""
        buf = b""
        for chunk in iter(lambda: resp.read(4096), b""):
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode("utf-8", errors="replace").strip()
                if line.startswith("data: "):
                    payload = line[6:]
                    if payload == "[DONE]":
                        return
                    yield payload

    # ── build_request / parse_response ──────────────────────────────────

    @staticmethod
    def _openai_tool_call(raw):
        if not isinstance(raw, dict):
            return None
        function = raw.get("function")
        if raw.get("type") == "function" and isinstance(function, dict):
            return raw
        function = function if isinstance(function, dict) else {}
        name = str(raw.get("name") or raw.get("tool_name") or function.get("name") or "")
        if not name:
            return None
        arguments = (
            raw.get("input")
            if "input" in raw
            else raw.get("args", function.get("arguments", {}))
        )
        if not isinstance(arguments, str):
            arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return {
            "id": str(raw.get("id") or raw.get("tool_call_id") or ""),
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }

    def build_request(self, messages):
        """StandardMessage → OpenAI 形式。OpenAI はほぼそのまま。"""
        converted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "assistant" and isinstance(content, list):
                standard_tool_calls = []
                text_parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(str(block.get("text") or ""))
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    tool_call = OpenAIProvider._openai_tool_call(block)
                    if tool_call:
                        standard_tool_calls.append(tool_call)
                if standard_tool_calls:
                    entry = {
                        "role": "assistant",
                        "content": "".join(text_parts),
                        "tool_calls": standard_tool_calls,
                    }
                    reasoning_content = self._message_reasoning_content(msg)
                    if reasoning_content:
                        entry["reasoning_content"] = reasoning_content
                    converted.append(entry)
                    continue
            if role == "assistant" and msg.get("tool_calls"):
                tool_calls = [
                    normalized
                    for raw in msg.get("tool_calls", [])
                    if (normalized := OpenAIProvider._openai_tool_call(raw)) is not None
                ]
                entry = {
                    "role": "assistant",
                    "content": content if isinstance(content, str) else "",
                    "tool_calls": tool_calls,
                }
                reasoning_content = self._message_reasoning_content(msg)
                if reasoning_content:
                    entry["reasoning_content"] = reasoning_content
                converted.append(entry)
                continue
            if role == "tool":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id", ""),
                        "name": msg.get("name", ""),
                        "content": content
                        if isinstance(content, str)
                        else json.dumps(content, ensure_ascii=False),
                    }
                )
                continue
            if isinstance(content, list):
                parts = []
                for c in content:
                    if c.get("type") == "text":
                        parts.append({"type": "text", "text": c.get("text", "")})
                    elif c.get("type") == "image_url":
                        parts.append({"type": "image_url", "image_url": c.get("image_url", {})})
                    elif c.get("type") == "image" and c.get("source"):
                        src = c["source"]
                        b64 = src.get("data", "")
                        media = src.get("media_type", "image/png")
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:{};base64,{}".format(media, b64)},
                            }
                        )
                    else:
                        parts.append(c)
                converted.append({"role": role, "content": parts})
            else:
                converted.append({"role": role, "content": content})
        return converted

    @staticmethod
    def _message_reasoning_content(msg):
        for key in ("reasoning_content", "reasoning", "thinking"):
            value = msg.get(key)
            if isinstance(value, str) and value.strip():
                return value
        metadata = msg.get("metadata")
        if isinstance(metadata, dict):
            value = metadata.get("reasoning_content")
            if isinstance(value, str) and value.strip():
                return value
            thinking = metadata.get("thinking")
            if isinstance(thinking, dict):
                transcript = thinking.get("transcript")
                if isinstance(transcript, str) and transcript.strip():
                    return transcript
        return ""

    def parse_response(self, raw):
        """OpenAI chat completion JSON → StandardResponse"""
        choice = raw.get("choices", [{}])[0]
        message = choice.get("message", {})
        text = message.get("content", "") or ""
        finish = choice.get("finish_reason", "stop") or "stop"
        usage_raw = raw.get("usage", {})
        usage = {
            "input_tokens": usage_raw.get("prompt_tokens", 0),
            "output_tokens": usage_raw.get("completion_tokens", 0),
            "total_tokens": usage_raw.get("total_tokens", 0),
        }
        content = [{"type": "text", "text": text}]
        reasoning_content = (
            message.get("reasoning_content")
            or message.get("reasoning")
            or message.get("thinking")
            or ""
        )
        reasoning_content = str(reasoning_content) if reasoning_content else ""
        tool_calls = message.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc.get("function", {}).get("name", ""),
                        "input": tc.get("function", {}).get("arguments", "{}"),
                    }
                )
        metadata: dict[str, object] = {}
        if reasoning_content:
            metadata["reasoning_content"] = reasoning_content
            metadata["thinking"] = {"transcript": reasoning_content}
        return {
            "content": content,
            "finish_reason": finish,
            "usage": usage,
            "metadata": metadata,
            **({"reasoning_content": reasoning_content} if reasoning_content else {}),
            "raw_extra": {"id": raw.get("id", ""), "model": raw.get("model", "")},
        }

    # ── 9 required methods ──────────────────────────────────────────────

    @staticmethod
    def _translate_params(params):
        translated = dict(params or {})
        thinking_level = str(translated.pop("thinking_level", "") or "").strip()
        if (
            thinking_level in {"low", "medium", "high", "xhigh"}
            and "reasoning_effort" not in translated
        ):
            translated["reasoning_effort"] = "high" if thinking_level == "xhigh" else thinking_level
        return translated

    @staticmethod
    def _request_timeout(params):
        raw = dict(params or {})
        value = raw.get("request_timeout", raw.get("timeout", 120))
        if isinstance(value, bool):
            timeout = float(value)
        elif isinstance(value, (int, float, str)):
            try:
                timeout = float(value)
            except (TypeError, ValueError):
                timeout = 120.0
        else:
            timeout = 120.0
        return max(2.0, min(timeout, 120.0))

    def _request_timeout_kwargs(self, params):
        raw = dict(params or {})
        if "request_timeout" not in raw and "timeout" not in raw:
            return {}
        return {"timeout": self._request_timeout(raw)}

    def _translate_model_params(self, model, params):
        del model
        return dict(params or {})

    @staticmethod
    def _copy_chat_params(body, params):
        for k in (
            "temperature",
            "max_tokens",
            "max_completion_tokens",
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
            if k in params:
                body[k] = params[k]
        extra_body = params.get("extra_body")
        if isinstance(extra_body, dict):
            body.update(extra_body)

    @staticmethod
    def _stream_tool_call_events(delta, state):
        for tool_call in delta.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            index = str(tool_call.get("index", len(state)))
            current = state.setdefault(
                index, {"id": "", "name": "", "started": False, "ended": False}
            )
            if tool_call.get("id"):
                current["id"] = str(tool_call.get("id"))
            function_delta_value = tool_call.get("function")
            function_delta: dict[str, object] = (
                {str(key): value for key, value in function_delta_value.items()}
                if isinstance(function_delta_value, dict)
                else {}
            )
            if function_delta.get("name"):
                current["name"] = str(function_delta.get("name"))
            call_id = current["id"] or f"tool_call_{index}"
            if not current["id"]:
                current["id"] = call_id
            name = current["name"]
            if not current["started"] and (
                tool_call.get("id") or name or function_delta.get("arguments")
            ):
                current["started"] = True
                yield {"type": "tool_call_start", "id": call_id, "name": name}
            if function_delta.get("arguments"):
                yield {
                    "type": "tool_call_delta",
                    "id": call_id,
                    "name": name,
                    "arguments_chunk": str(function_delta.get("arguments")),
                }

    def complete(self, model, messages, tools, params):
        params = self._translate_params(params)
        params = self._translate_model_params(model, params)
        body = {"model": model, "messages": self.build_request(messages)}
        if tools:
            body["tools"] = tools
        self._copy_chat_params(body, params)
        raw = self._request_json("/chat/completions", body, **self._request_timeout_kwargs(params))
        return self.parse_response(raw)

    def stream(self, model, messages, tools, params):
        params = self._translate_params(params)
        params = self._translate_model_params(model, params)
        body = {"model": model, "messages": self.build_request(messages)}
        if tools:
            body["tools"] = tools
        self._copy_chat_params(body, params)
        body.setdefault("stream_options", {"include_usage": True})
        resp = self._request_stream(
            "/chat/completions", body, **self._request_timeout_kwargs(params)
        )
        tool_call_state: dict[str, dict[str, object]] = {}
        try:
            for payload in self._parse_sse_lines(resp):
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
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
                    yield from self._stream_tool_call_events(delta, tool_call_state)
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
                elif obj.get("usage"):
                    pass
        finally:
            resp.close()

    def embed(self, model, input_text):
        if isinstance(input_text, str):
            input_text = [input_text]
        body = {"model": model, "input": input_text}
        raw = self._request_json("/embeddings", body)
        embeddings = [item["embedding"] for item in raw.get("data", [])]
        usage_raw = raw.get("usage", {})
        return {
            "embeddings": embeddings,
            "usage": {
                "input_tokens": usage_raw.get("prompt_tokens", 0),
                "total_tokens": usage_raw.get("total_tokens", 0),
            },
        }

    def image_gen(self, model, prompt, params):
        body = {"model": model, "prompt": prompt}
        body["n"] = params.get("n", 1)
        body["size"] = params.get("size", "1024x1024")
        body["quality"] = params.get("quality", "standard")
        body["response_format"] = params.get("response_format", "b64_json")
        if "style" in params:
            body["style"] = params["style"]
        raw = self._request_json("/images/generations", body)
        images = []
        for item in raw.get("data", []):
            if "b64_json" in item:
                images.append("data:image/png;base64," + item["b64_json"])
            elif "url" in item:
                images.append(item["url"])
        return {"images": images}

    def image_analyze(self, model, image, prompt):
        """vision: GPT-4o 等で画像解析"""
        if image.startswith("data:"):
            image_content = {"type": "image_url", "image_url": {"url": image}}
        elif image.startswith("http"):
            image_content = {"type": "image_url", "image_url": {"url": image}}
        else:
            image_content = {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + image},
            }
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    image_content,
                ],
            }
        ]
        body = {"model": model, "messages": messages}
        raw = self._request_json("/chat/completions", body)
        text = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"text": text}

    def transcribe(self, model, audio, params):
        """Whisper API で音声をテキストに変換"""
        if audio.startswith("data:"):
            header, b64data = audio.split(",", 1) if "," in audio else ("", audio)
            audio_bytes = base64.b64decode(b64data)
        elif audio.startswith("http"):
            req = urllib.request.Request(audio)
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=120) as resp:
                audio_bytes = resp.read()
        else:
            audio_bytes = base64.b64decode(audio)
        ext = params.get("format", "mp3")
        filename = "audio." + ext
        mime = "audio/" + ext
        fields = {"model": model}
        if "language" in params:
            fields["language"] = params["language"]
        if "prompt" in params:
            fields["prompt"] = params["prompt"]
        resp_bytes = self._request_multipart(
            "/audio/transcriptions", fields, {"file": (filename, audio_bytes, mime)}
        )
        try:
            result = json.loads(resp_bytes.decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError(
                "OpenAI API returned invalid JSON for transcription: {}".format(resp_bytes[:500])
            )
        return {"text": result.get("text", "")}

    def tts(self, model, text, voice):
        voice = voice or "alloy"
        body = {"model": model, "input": text, "voice": voice}
        url = self.BASE_URL + "/audio/speech"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=120) as resp:
                audio_bytes = resp.read()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError("OpenAI API error {}: {}".format(e.code, err_body))
        except urllib.error.URLError as e:
            raise RuntimeError("OpenAI API connection error: {}".format(e.reason))
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        return {"audio": "data:audio/mp3;base64," + b64}
