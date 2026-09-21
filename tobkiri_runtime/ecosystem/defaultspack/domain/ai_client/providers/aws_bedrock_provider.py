"""Amazon Bedrock regional inventory and Converse API adapter.

Bedrock's control plane is the authoritative inventory for a region.  This
adapter deliberately signs and requests ``ListFoundationModels`` rather than
shipping a release list, then uses the same account/region credentials for the
Converse API.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import hmac
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from ..api_key_store import provider_named_api_keys, read_provider_api_key
from ..base_provider import BaseProvider


class AwsBedrockProvider(BaseProvider):
    provider_id = "aws-bedrock"
    _MODEL_INVENTORY_CACHE: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
    _MODEL_INVENTORY_CACHE_TTL_SECONDS = 300

    def __init__(self):
        self._connection = self._configured_connection()
        self._credentials = self._configured_credentials(self._connection)
        self._region = self._configured_region(self._connection)
        self._ssl_ctx = ssl.create_default_context()

    @classmethod
    def _configured_connection(cls) -> Dict[str, Any]:
        for connection in provider_named_api_keys(cls.provider_id):
            if connection.get("configured"):
                return dict(connection)
        return {}

    @classmethod
    def _configured_credentials(cls, connection: Dict[str, Any]) -> Dict[str, str]:
        api_id = str(connection.get("api_id") or "").strip()
        secret = str(read_provider_api_key(cls.provider_id, api_id) or "").strip() if api_id else ""
        if not secret:
            return {}
        # Named connections keep this one structured secret encrypted.  The
        # concise colon form is convenient for an access-key/secret pair;
        # JSON additionally supports temporary-session credentials.
        try:
            decoded = json.loads(secret)
        except json.JSONDecodeError:
            decoded = {}
        if isinstance(decoded, dict) and decoded:
            access_key_id = str(
                decoded.get("access_key_id") or decoded.get("accessKeyId") or ""
            ).strip()
            secret_access_key = str(
                decoded.get("secret_access_key") or decoded.get("secretAccessKey") or ""
            ).strip()
            session_token = str(
                decoded.get("session_token") or decoded.get("sessionToken") or ""
            ).strip()
        else:
            parts = secret.split(":", 2)
            access_key_id = parts[0].strip() if parts else ""
            secret_access_key = parts[1].strip() if len(parts) > 1 else ""
            session_token = parts[2].strip() if len(parts) > 2 else ""
        if not access_key_id or not secret_access_key:
            return {}
        return {
            "access_key_id": access_key_id,
            "secret_access_key": secret_access_key,
            "session_token": session_token,
        }

    @staticmethod
    def _configured_region(connection: Dict[str, Any]) -> str:
        for value in (
            os.environ.get("AWS_REGION"),
            os.environ.get("AWS_DEFAULT_REGION"),
            connection.get("base_url"),
        ):
            candidate = str(value or "").strip()
            if not candidate:
                continue
            if candidate.startswith("http"):
                host = urllib.parse.urlsplit(candidate).hostname or ""
                pieces = host.split(".")
                if len(pieces) >= 3 and pieces[0].startswith("bedrock"):
                    return pieces[1]
            elif "." not in candidate and "/" not in candidate:
                return candidate
        return "us-east-1"

    def _inventory_scope(self) -> str:
        material = "\0".join(
            (
                self.provider_id,
                self._region,
                self._credentials.get("access_key_id", ""),
                self._credentials.get("session_token", ""),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _sign(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

    def _signed_headers(
        self, method: str, host: str, path: str, *, body: bytes = b"", service: str
    ) -> Dict[str, str]:
        if not self._credentials:
            raise RuntimeError(
                "aws-bedrock: configure AWS credentials or save an access_key_id:secret_access_key connection"
            )
        now = _datetime.datetime.now(_datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            "content-type": "application/json",
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if self._credentials.get("session_token"):
            headers["x-amz-security-token"] = self._credentials["session_token"]
        signed_names = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical_request = "\n".join(
            (method, path, "", canonical_headers, signed_names, payload_hash)
        )
        credential_scope = f"{date_stamp}/{self._region}/{service}/aws4_request"
        string_to_sign = "\n".join(
            (
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        date_key = self._sign(
            ("AWS4" + self._credentials["secret_access_key"]).encode("utf-8"), date_stamp
        )
        region_key = self._sign(date_key, self._region)
        service_key = self._sign(region_key, service)
        signing_key = self._sign(service_key, "aws4_request")
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._credentials['access_key_id']}/{credential_scope}, "
            f"SignedHeaders={signed_names}, Signature={signature}"
        )
        return {
            "Content-Type": headers["content-type"],
            "Host": headers["host"],
            "X-Amz-Content-Sha256": headers["x-amz-content-sha256"],
            "X-Amz-Date": headers["x-amz-date"],
            "X-Amz-Security-Token": headers.get("x-amz-security-token", ""),
            "Authorization": headers["authorization"],
            "Accept": "application/json",
            "User-Agent": "RumiAI/1.0",
        }

    def _request_json(
        self, method: str, host: str, path: str, *, service: str, body: Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        payload = (
            json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else b""
        )
        headers = self._signed_headers(method, host, path, body=payload, service=service)
        # urllib should not send an empty body for a GET, but its SHA256 still
        # has to be part of the signed canonical request.
        request = urllib.request.Request(
            f"https://{host}{path}",
            data=payload if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, context=self._ssl_ctx, timeout=120) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Amazon Bedrock API error {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Amazon Bedrock connection error: {error.reason}") from error
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"Amazon Bedrock returned invalid JSON: {raw[:500]}") from error
        return decoded if isinstance(decoded, dict) else {"data": decoded}

    @staticmethod
    def _model_record(raw: Any) -> Dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        model_id = str(raw.get("modelId") or "").strip()
        if not model_id:
            return None
        inputs = {
            str(value).upper() for value in raw.get("inputModalities", []) if isinstance(value, str)
        }
        outputs = {
            str(value).upper()
            for value in raw.get("outputModalities", [])
            if isinstance(value, str)
        }
        model_type = (
            "embedding" if "EMBEDDING" in outputs else "image_gen" if "IMAGE" in outputs else "chat"
        )
        lifecycle = raw.get("modelLifecycle") if isinstance(raw.get("modelLifecycle"), dict) else {}
        return {
            "id": f"aws-bedrock/{model_id}",
            "model_id": model_id,
            "provider_id": "aws-bedrock",
            "provider": "aws-bedrock",
            "name": str(raw.get("modelName") or model_id),
            "display_name": str(raw.get("modelName") or model_id),
            "type": model_type,
            "capabilities": {
                "chat": model_type == "chat" and "TEXT" in inputs and "TEXT" in outputs,
                "text_input": "TEXT" in inputs,
                "text_output": "TEXT" in outputs,
                "vision": "IMAGE" in inputs,
                "streaming": bool(raw.get("responseStreamingSupported")),
                "embeddings": model_type == "embedding",
                "image_generation": model_type == "image_gen",
            },
            "metadata": {
                "source": "aws_bedrock_list_foundation_models",
                "capability_source": "provider_reported",
                "capability_confidence": "provider_reported",
                "model_arn": raw.get("modelArn"),
                "provider_name": raw.get("providerName"),
                "input_modalities": sorted(inputs),
                "output_modalities": sorted(outputs),
                "inference_types_supported": list(raw.get("inferenceTypesSupported") or []),
                "customizations_supported": list(raw.get("customizationsSupported") or []),
                "lifecycle": lifecycle,
            },
        }

    def list_models(self) -> List[Dict[str, Any]]:
        if not self._credentials:
            return []
        scope = self._inventory_scope()
        now = time.monotonic()
        cached = self._MODEL_INVENTORY_CACHE.get(scope)
        if cached and cached[0] > now:
            return [dict(item) for item in cached[1]]
        host = f"bedrock.{self._region}.amazonaws.com"
        payload = self._request_json("GET", host, "/foundation-models", service="bedrock")
        raw_models = (
            payload.get("modelSummaries") if isinstance(payload.get("modelSummaries"), list) else []
        )
        models = [item for raw in raw_models if (item := self._model_record(raw)) is not None]
        if models:
            self._MODEL_INVENTORY_CACHE[scope] = (
                now + self._MODEL_INVENTORY_CACHE_TTL_SECONDS,
                [dict(item) for item in models],
            )
        return models

    @staticmethod
    def _content_part(value: Any) -> Dict[str, Any]:
        if isinstance(value, str):
            return {"text": value}
        if isinstance(value, list):
            text = "".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part)
                for part in value
            )
            return {"text": text}
        return {"text": str(value or "")}

    def complete(self, model, messages, tools, params):
        model_id = str(model or "").removeprefix("aws-bedrock/")
        system = []
        converted = []
        for message in list(messages or []):
            role = str(message.get("role") or "user") if isinstance(message, dict) else "user"
            content = message.get("content") if isinstance(message, dict) else message
            if role == "system":
                system.append(self._content_part(content))
            else:
                converted.append(
                    {
                        "role": "assistant" if role == "assistant" else "user",
                        "content": [self._content_part(content)],
                    }
                )
        body: Dict[str, Any] = {"messages": converted}
        if system:
            body["system"] = system
        inference = {}
        for source, target in (
            ("temperature", "temperature"),
            ("max_tokens", "maxTokens"),
            ("top_p", "topP"),
        ):
            if source in (params or {}):
                inference[target] = params[source]
        if inference:
            body["inferenceConfig"] = inference
        if tools:
            tool_specs = []
            for tool in tools:
                function = tool.get("function") if isinstance(tool, dict) else None
                if not isinstance(function, dict):
                    continue
                tool_specs.append(
                    {
                        "toolSpec": {
                            "name": str(function.get("name") or ""),
                            "description": str(function.get("description") or ""),
                            "inputSchema": {
                                "json": function.get("parameters") or {"type": "object"}
                            },
                        }
                    }
                )
            if tool_specs:
                body["toolConfig"] = {"tools": tool_specs}
        host = f"bedrock-runtime.{self._region}.amazonaws.com"
        path = "/model/" + urllib.parse.quote(model_id, safe="-_.~") + "/converse"
        payload = self._request_json("POST", host, path, service="bedrock", body=body)
        output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        message = output.get("message") if isinstance(output.get("message"), dict) else {}
        content = []
        for item in message.get("content") if isinstance(message.get("content"), list) else []:
            if isinstance(item, dict) and "text" in item:
                content.append({"type": "text", "text": str(item.get("text") or "")})
            elif isinstance(item, dict) and isinstance(item.get("toolUse"), dict):
                tool = item["toolUse"]
                content.append(
                    {
                        "type": "tool_use",
                        "id": tool.get("toolUseId", ""),
                        "name": tool.get("name", ""),
                        "input": tool.get("input", {}),
                    }
                )
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return {
            "content": content,
            "finish_reason": str(payload.get("stopReason") or "stop"),
            "usage": {
                "input_tokens": usage.get("inputTokens", 0),
                "output_tokens": usage.get("outputTokens", 0),
                "total_tokens": usage.get("totalTokens", 0),
            },
            "raw_extra": {"model": model_id, "region": self._region},
        }
