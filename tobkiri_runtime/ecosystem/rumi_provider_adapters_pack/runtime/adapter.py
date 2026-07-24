"""Execute registry-selected provider protocols with scoped credentials."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)

REGISTRY_CONTRACT = "rumi.resource.ai.provider.registry.v1"
CREDENTIAL_CONTRACT = "rumi.service.credential.resolve.v1"
DEFAULT_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "RumiAI/1.0",
}


def create_generate_operation(client: GlobalContractClient):
    """Create a non-streaming provider execution operation."""
    return _operation(client, streaming=False)


def create_stream_operation(client: GlobalContractClient):
    """Create a streaming provider execution operation."""
    return _operation(client, streaming=True)


def create_embedding_operation(client: GlobalContractClient):
    """Create an OpenAI-compatible embedding provider operation."""
    return _modality_operation(client, kind="embedding")


def create_image_operation(client: GlobalContractClient):
    """Create an OpenAI-compatible image provider operation."""
    return _modality_operation(client, kind="image")


def _operation(client: GlobalContractClient, *, streaming: bool):
    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"invoke", "stream" if streaming else "generate"}
        if name not in allowed:
            raise ValueError(f"unknown provider adapter operation: {name}")
        request = dict(payload)
        connection = _connection(client, request)
        credential = _credential(
            client,
            request,
            connection,
            scope="ai.stream" if streaming else "ai.generate",
        )
        adapter = _adapter(
            str(connection.get("adapter_id") or ""),
            provider_id=str(request.get("provider_id") or ""),
        )
        return adapter(request, connection, credential, streaming)

    return operation


def _modality_operation(client: GlobalContractClient, *, kind: str):
    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        expected = "embed" if kind == "embedding" else "generate"
        if name not in {expected, "invoke"}:
            raise ValueError(f"unknown provider modality operation: {name}")
        request = dict(payload)
        connection = _connection(client, request)
        credential = _credential(
            client,
            request,
            connection,
            scope=f"ai.{kind}",
        )
        adapter_id = str(connection.get("adapter_id") or "")
        if adapter_id not in {"openai", "openai-compatible"}:
            raise GlobalContractInvocationError(
                "incompatible", "provider modality protocol is unavailable"
            )
        if kind == "embedding":
            return _openai_embedding(request, connection, credential)
        return _openai_image(request, connection, credential)

    return operation


def _connection(
    client: GlobalContractClient,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    provider_id = str(request.get("provider_id") or "").strip()
    if not provider_id:
        raise GlobalContractInvocationError(
            "invalid_request", "provider_id is required"
        )
    registry_payload = {}
    profile_id = str(request.get("profile_id") or "").strip()
    if profile_id:
        registry_payload["profile_id"] = profile_id
    result = client.invoke(REGISTRY_CONTRACT, "list", registry_payload)
    providers = result.get("providers") if isinstance(result, Mapping) else None
    providers = providers if isinstance(providers, list) else []
    expected = f"provider.{provider_id}"
    matches = [
        dict(item)
        for item in providers
        if isinstance(item, Mapping)
        and str(item.get("provider_instance_id") or "") == expected
        and bool(item.get("enabled", True))
    ]
    if len(matches) != 1:
        raise GlobalContractInvocationError(
            "not_configured", "provider connection is not configured"
        )
    return matches[0]


def _credential(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    *,
    scope: str,
) -> dict[str, Any]:
    handle = request.get("credential_handle") or connection.get(
        "credential_handle"
    )
    if handle is None:
        return {}
    if not str(handle).startswith(("credential:", "opaque:")):
        raise GlobalContractInvocationError(
            "denied", "provider adapter accepts only opaque credentials"
        )
    result = client.invoke(
        CREDENTIAL_CONTRACT,
        "resolve",
        {
            "handle": handle,
            "provider_instance_id": connection["provider_instance_id"],
            "scope": scope,
        },
    )
    material = result.get("secret_material") if isinstance(result, Mapping) else None
    if not isinstance(material, Mapping):
        raise GlobalContractInvocationError(
            "denied", "credential resolution returned no material"
        )
    return dict(material)


def _adapter(
    adapter_id: str,
    *,
    provider_id: str = "",
) -> Callable[..., dict[str, Any]]:
    if adapter_id == "llm":
        adapter_id = "anthropic" if provider_id == "anthropic" else "openai-compatible"
    adapters = {
        "openai-compatible": _openai_compatible,
        "openai": _openai_compatible,
        "openrouter": _openai_compatible,
        "anthropic": _anthropic,
    }
    try:
        return adapters[adapter_id]
    except KeyError:
        raise GlobalContractInvocationError(
            "incompatible", "provider adapter protocol is unavailable"
        ) from None


def _openai_compatible(
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential: Mapping[str, Any],
    streaming: bool,
) -> dict[str, Any]:
    endpoint = _endpoint(connection, "/chat/completions")
    body = {
        "model": _provider_model_id(request),
        "messages": list(request.get("messages") or []),
        "stream": False,
        **dict(request.get("parameters") or {}),
    }
    tools = request.get("tools")
    if isinstance(tools, list) and tools:
        body["tools"] = tools
    headers = dict(DEFAULT_JSON_HEADERS)
    token = credential.get("api_key") or credential.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    value = _post(endpoint, headers, body, request)
    choices = value.get("choices") if isinstance(value, Mapping) else None
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, Mapping) else {}
    content = message.get("content") if isinstance(message, Mapping) else ""
    result = {
        "output": content if content is not None else "",
        "tool_intents": (
            list(message.get("tool_calls") or [])
            if isinstance(message, Mapping)
            else []
        ),
        "usage": dict(value.get("usage") or {}),
        "finish_reason": (
            first.get("finish_reason") if isinstance(first, Mapping) else None
        ),
    }
    return _stream_result(result) if streaming else result


def _anthropic(
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential: Mapping[str, Any],
    streaming: bool,
) -> dict[str, Any]:
    endpoint = _endpoint(connection, "/messages")
    parameters = dict(request.get("parameters") or {})
    body = {
        "model": _provider_model_id(request),
        "messages": list(request.get("messages") or []),
        "max_tokens": int(parameters.pop("max_tokens", 1024)),
        **parameters,
    }
    headers = {
        **DEFAULT_JSON_HEADERS,
        "anthropic-version": "2023-06-01",
    }
    token = credential.get("api_key") or credential.get("token")
    if token:
        headers["x-api-key"] = str(token)
    value = _post(endpoint, headers, body, request)
    blocks = value.get("content") if isinstance(value, Mapping) else None
    blocks = blocks if isinstance(blocks, list) else []
    text = "".join(
        str(item.get("text") or "")
        for item in blocks
        if isinstance(item, Mapping) and item.get("type") == "text"
    )
    result = {
        "output": text,
        "tool_intents": [],
        "usage": dict(value.get("usage") or {}),
        "finish_reason": value.get("stop_reason"),
    }
    return _stream_result(result) if streaming else result


def _openai_embedding(
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential: Mapping[str, Any],
) -> dict[str, Any]:
    value = _post(
        _endpoint(connection, "/embeddings"),
        _bearer_headers(credential),
        {"model": _provider_model_id(request), "input": request.get("input")},
        request,
    )
    data = value.get("data")
    data = data if isinstance(data, list) else []
    vectors = [
        list(item.get("embedding") or [])
        for item in data
        if isinstance(item, Mapping)
    ]
    return {"vectors": vectors, "usage": dict(value.get("usage") or {})}


def _openai_image(
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential: Mapping[str, Any],
) -> dict[str, Any]:
    body = {
        "model": _provider_model_id(request),
        "prompt": request.get("prompt"),
        **dict(request.get("parameters") or {}),
    }
    value = _post(
        _endpoint(connection, "/images/generations"),
        _bearer_headers(credential),
        body,
        request,
    )
    data = value.get("data")
    artifacts = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, Mapping):
            continue
        material = str(item.get("url") or item.get("b64_json") or "")
        if not material:
            continue
        artifacts.append(
            {
                "artifact_id": "sha256:"
                + hashlib.sha256(material.encode("utf-8")).hexdigest(),
                "uri": item.get("url"),
                "base64": item.get("b64_json"),
                "revised_prompt": item.get("revised_prompt"),
            }
        )
    return {"artifacts": artifacts}


def _bearer_headers(credential: Mapping[str, Any]) -> dict[str, str]:
    headers = dict(DEFAULT_JSON_HEADERS)
    token = credential.get("api_key") or credential.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _provider_model_id(request: Mapping[str, Any]) -> str:
    model_id = str(request.get("model_id") or "")
    provider_id = str(request.get("provider_id") or "")
    prefix = f"{provider_id}/"
    if provider_id and model_id.startswith(prefix):
        return model_id[len(prefix):]
    return model_id


def _stream_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "events": [
            {"type": "text_delta", "delta": str(result.get("output") or "")},
            {"type": "usage", "usage": dict(result.get("usage") or {})},
            {"type": "finish", "finish_reason": result.get("finish_reason")},
        ]
    }


def _endpoint(connection: Mapping[str, Any], suffix: str) -> str:
    endpoint = str(connection.get("endpoint") or "").rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        raise GlobalContractInvocationError(
            "not_configured", "provider endpoint is not configured"
        )
    return endpoint + suffix


def _post(
    endpoint: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    deadline = float(request.get("deadline") or 0)
    timeout = min(60.0, max(0.1, deadline - time.time()))
    http_request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        code = "quota" if exc.code == 429 else "provider_unavailable"
        raise GlobalContractInvocationError(code, f"provider HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise GlobalContractInvocationError(
            "provider_unavailable", type(exc).__name__
        ) from None
    if not isinstance(value, dict):
        raise GlobalContractInvocationError(
            "invalid_response", "provider returned a non-object response"
        )
    return value
