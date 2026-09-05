import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

import json
import urllib.request
import urllib.error
import urllib.parse
import ssl
from typing import Any, Dict, List

from domain.ai_client.base_provider import BaseProvider
from domain.ai_client.api_key_store import read_provider_api_key


class GensparkProvider(BaseProvider):
    """Genspark API プロバイダー (GPT-5 Mini 等を Genspark エンドポイント経由で呼び出す)

    環境変数:
        GENSPARK_API_KEY : Genspark API キー（必須）
        GENSPARK_BASE_URL: ベース URL（省略時は ~/.genspark_llm.yaml または
                           OPENAI_BASE_URL にフォールバック）
    """

    DEFAULT_BASE_URL = "https://www.genspark.ai/api/llm_proxy/v1"

    KNOWN_MODELS = [
        {"id": "genspark/gpt-5", "name": "GPT-5", "provider": "genspark", "type": "chat"},
        {"id": "genspark/gpt-5.1", "name": "GPT-5.1", "provider": "genspark", "type": "chat"},
        {"id": "genspark/gpt-5.2", "name": "GPT-5.2", "provider": "genspark", "type": "chat"},
        {"id": "genspark/gpt-5-mini", "name": "GPT-5 Mini", "provider": "genspark", "type": "chat"},
        {"id": "genspark/gpt-5-nano", "name": "GPT-5 Nano", "provider": "genspark", "type": "chat"},
        {
            "id": "genspark/gpt-5-codex",
            "name": "GPT-5 Codex",
            "provider": "genspark",
            "type": "chat",
        },
        {
            "id": "genspark/gpt-5.2-codex",
            "name": "GPT-5.2 Codex",
            "provider": "genspark",
            "type": "chat",
        },
    ]
    # Account inventory is fetched from the OpenAI-compatible endpoint.
    KNOWN_MODELS: List[Dict[str, Any]] = []

    def __init__(self, api_key: str | None = None):
        self._api_key = str(api_key or self._resolve_api_key() or "").strip()
        self._base_url = self._resolve_base_url()
        self._ssl_ctx = ssl.create_default_context()

    # ── key / url resolution ─────────────────────────────────────────────

    def _resolve_api_key(self):
        """
        API キー解決の優先順位:
        1. GENSPARK_API_KEY 環境変数
        2. ~/.genspark_llm.yaml の openai.api_key
        3. OPENAI_API_KEY 環境変数（OpenAI 互換エンドポイントで共用する場合）
        """
        key = read_provider_api_key("genspark", "legacy") or ""
        if key:
            return key
        try:
            import yaml

            config_path = os.path.join(os.path.expanduser("~"), ".genspark_llm.yaml")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                key = (config or {}).get("openai", {}).get("api_key", "")
                if key:
                    return key
        except Exception:
            pass
        return read_provider_api_key("openai", "legacy") or ""

    def _resolve_base_url(self):
        """
        ベース URL 解決の優先順位:
        1. GENSPARK_LLM_BASE_URL 環境変数（明示的な LLM API 用 URL）
        2. ~/.genspark_llm.yaml の openai.base_url
        3. OPENAI_BASE_URL 環境変数
        4. DEFAULT_BASE_URL

        注意: GENSPARK_BASE_URL は Genspark サービス全体の URL であり、
              LLM API パスを含まないため使用しない。
        """
        # 明示的な LLM 用 URL が指定されていればそれを使う
        url = os.environ.get("GENSPARK_LLM_BASE_URL", "")
        if url:
            return url.rstrip("/")
        # ~/.genspark_llm.yaml から読む（GenSpark 公式設定ファイル）
        try:
            import yaml

            config_path = os.path.join(os.path.expanduser("~"), ".genspark_llm.yaml")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                url = (config or {}).get("openai", {}).get("base_url", "")
                if url:
                    return url.rstrip("/")
        except Exception:
            pass
        # OPENAI_BASE_URL 環境変数（互換エンドポイント共用の場合）
        url = os.environ.get("OPENAI_BASE_URL", "")
        if url:
            return url.rstrip("/")
        return self.DEFAULT_BASE_URL

    # ── internal helpers ────────────────────────────────────────────────

    def _headers(self, content_type="application/json"):
        h = {
            "Authorization": "Bearer " + self._api_key,
            "User-Agent": "RumiAI/1.0",
        }
        if content_type:
            h["Content-Type"] = content_type
        return h

    def list_models(self):
        if not self._api_key:
            return []
        request = urllib.request.Request(
            self._base_url + "/models",
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
        records = (
            payload.get("data")
            if isinstance(payload, dict)
            else payload.get("models")
            if isinstance(payload, dict)
            else []
        )
        models = []
        for raw in records if isinstance(records, list) else []:
            source = raw if isinstance(raw, dict) else {"id": raw}
            model_id = str(
                source.get("id") or source.get("model_id") or source.get("name") or ""
            ).strip()
            if not model_id or any(item["model_id"] == model_id for item in models):
                continue
            models.append(
                {
                    "id": f"genspark/{model_id}",
                    "model_id": model_id,
                    "provider_id": "genspark",
                    "provider": "genspark",
                    "name": str(
                        source.get("display_name") or source.get("displayName") or model_id
                    ),
                    "display_name": str(
                        source.get("display_name") or source.get("displayName") or model_id
                    ),
                    "type": "chat",
                    "capabilities": {
                        "chat": True,
                        "text_input": True,
                        "text_output": True,
                        "streaming": True,
                    },
                    "metadata": {
                        "source": "openai_models_endpoint",
                        "source_endpoint": "/models",
                        "visibility_scope": "account",
                    },
                }
            )
        return models

    def _request_json(self, path, body):
        """POST して JSON をパースして返す"""
        if not self._api_key:
            raise RuntimeError(
                "Genspark API key is not set. Set GENSPARK_API_KEY environment variable."
            )
        url = self._base_url + path
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=120) as resp:
                raw_bytes = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise RuntimeError(
                    "Genspark API authentication error (401): Invalid API key. err={}".format(
                        err_body
                    )
                )
            elif e.code == 429:
                raise RuntimeError("Genspark API rate limit exceeded (429): {}".format(err_body))
            elif e.code == 400:
                raise RuntimeError("Genspark API bad request (400): {}".format(err_body))
            raise RuntimeError("Genspark API error {}: {}".format(e.code, err_body))
        except urllib.error.URLError as e:
            raise RuntimeError("Genspark API connection error: {}".format(e.reason))
        try:
            return json.loads(raw_bytes)
        except (json.JSONDecodeError, ValueError):
            raise RuntimeError("Genspark API returned invalid JSON: {}".format(raw_bytes[:500]))

    def _request_stream(self, path, body):
        """POST して SSE ストリームレスポンスを返す"""
        if not self._api_key:
            raise RuntimeError(
                "Genspark API key is not set. Set GENSPARK_API_KEY environment variable."
            )
        url = self._base_url + path
        body["stream"] = True
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        try:
            resp = urllib.request.urlopen(req, context=self._ssl_ctx, timeout=120)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise RuntimeError("Genspark API authentication error (401): {}".format(err_body))
            elif e.code == 429:
                raise RuntimeError("Genspark API rate limit exceeded (429): {}".format(err_body))
            raise RuntimeError("Genspark API error {}: {}".format(e.code, err_body))
        except urllib.error.URLError as e:
            raise RuntimeError("Genspark API connection error: {}".format(e.reason))
        return resp

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

    def build_request(self, messages):
        """StandardMessage → OpenAI 互換形式（Genspark は OpenAI 互換）"""
        converted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
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

    def parse_response(self, raw):
        """OpenAI 互換レスポンス JSON → StandardResponse"""
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
        return {
            "content": content,
            "finish_reason": finish,
            "usage": usage,
            "raw_extra": {
                "id": raw.get("id", ""),
                "model": raw.get("model", ""),
            },
        }

    # ── 7 required methods (BaseProvider interface) ──────────────────────

    def complete(self, model, messages, tools, params):
        body = {"model": model, "messages": self.build_request(messages)}
        if tools:
            body["tools"] = tools
        for k in (
            "temperature",
            "max_tokens",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "stop",
            "response_format",
        ):
            if k in params:
                body[k] = params[k]
        raw = self._request_json("/chat/completions", body)
        return self.parse_response(raw)

    def stream(self, model, messages, tools, params):
        body = {"model": model, "messages": self.build_request(messages)}
        if tools:
            body["tools"] = tools
        for k in (
            "temperature",
            "max_tokens",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "stop",
            "response_format",
        ):
            if k in params:
                body[k] = params[k]
        resp = self._request_stream("/chat/completions", body)
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
                    finish = choices[0].get("finish_reason")
                    if finish:
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
        """vision: 画像解析"""
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
        """音声テキスト変換（Genspark がサポートする場合）"""
        raise NotImplementedError("Genspark provider does not support transcription.")

    def tts(self, model, text, voice):
        """テキスト音声合成（Genspark がサポートする場合）"""
        raise NotImplementedError("Genspark provider does not support TTS.")
