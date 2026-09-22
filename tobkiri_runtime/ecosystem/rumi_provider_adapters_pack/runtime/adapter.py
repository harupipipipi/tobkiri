"""Execute registry-selected provider protocols with scoped credentials."""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Mapping
import urllib.parse

from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

REGISTRY_CONTRACT = "tobkiri.resource.ai.provider.registry.v1"
REGISTRY_GENERATE_OPERATION = (
    "rumi_provider_registry_pack.provider-registry-resource.generate"
)
REGISTRY_STREAM_OPERATION = (
    "rumi_provider_registry_pack.provider-registry-resource.stream"
)
REGISTRY_OPERATION = REGISTRY_GENERATE_OPERATION
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
        connection = _connection(client, request, streaming=streaming)
        credential_handle = _credential_handle(
            request,
            connection,
            scope="ai.stream" if streaming else "ai.generate",
        )
        adapter = _adapter(
            str(connection.get("adapter_id") or ""),
            provider_id=str(request.get("provider_id") or ""),
        )
        return adapter(
            client,
            request,
            connection,
            credential_handle,
            "ai.stream" if streaming else "ai.generate",
            streaming,
        )

    return operation


def _modality_operation(client: GlobalContractClient, *, kind: str):
    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        expected = "embed" if kind == "embedding" else "generate"
        if name not in {expected, "invoke"}:
            raise ValueError(f"unknown provider modality operation: {name}")
        request = dict(payload)
        connection = _connection(client, request)
        credential_handle = _credential_handle(
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
            return _openai_embedding(client, request, connection, credential_handle, "ai.embedding")
        return _openai_image(client, request, connection, credential_handle, "ai.image")

    return operation


def _connection(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    *,
    streaming: bool = False,
) -> dict[str, Any]:
    provider_id = str(request.get("provider_id") or "").strip()
    if not provider_id:
        raise GlobalContractInvocationError("invalid_request", "provider_id is required")
    registry_payload = {}
    profile_id = str(request.get("profile_id") or "").strip()
    if profile_id:
        registry_payload["profile_id"] = profile_id
    result = client.invoke(
        REGISTRY_CONTRACT,
        REGISTRY_STREAM_OPERATION if streaming else REGISTRY_GENERATE_OPERATION,
        registry_payload,
    )
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


def _credential_handle(
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    *,
    scope: str,
) -> str:
    del scope
    supplied_handle = request.get("credential_handle")
    handle = connection.get("credential_handle")
    if supplied_handle is not None:
        raise GlobalContractInvocationError(
            "denied", "credential handle is bound by the Host provider registry"
        )
    if handle is None:
        raise GlobalContractInvocationError(
            "not_configured", "provider credential is not configured"
        )
    if not str(handle).startswith(("credential:", "opaque:")):
        raise GlobalContractInvocationError(
            "denied", "provider adapter accepts only opaque credentials"
        )
    endpoint = urllib.parse.urlsplit(str(connection.get("endpoint") or ""))
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
    ):
        raise GlobalContractInvocationError(
            "denied", "credentialed provider endpoint requires HTTPS"
        )
    return str(handle)


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
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
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
    value = _post(
        client,
        endpoint,
        headers,
        body,
        request,
        connection=connection,
        credential_handle=credential_handle,
        credential_scope=credential_scope,
        credential_scheme="bearer",
    )
    choices = value.get("choices") if isinstance(value, Mapping) else None
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, Mapping) else {}
    content = message.get("content") if isinstance(message, Mapping) else ""
    result: dict[str, Any] = {
        "output": content if content is not None else "",
        "tool_intents": (
            list(message.get("tool_calls") or []) if isinstance(message, Mapping) else []
        ),
        "usage": dict(value.get("usage") or {}),
        "finish_reason": (first.get("finish_reason") if isinstance(first, Mapping) else None),
    }
    return _stream_result(result) if streaming else result


def _anthropic(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
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
    value = _post(
        client,
        endpoint,
        headers,
        body,
        request,
        connection=connection,
        credential_handle=credential_handle,
        credential_scope=credential_scope,
        credential_scheme="anthropic",
    )
    blocks = value.get("content") if isinstance(value, Mapping) else None
    blocks = blocks if isinstance(blocks, list) else []
    text = "".join(
        str(item.get("text") or "")
        for item in blocks
        if isinstance(item, Mapping) and item.get("type") == "text"
    )
    result: dict[str, Any] = {
        "output": text,
        "tool_intents": [],
        "usage": dict(value.get("usage") or {}),
        "finish_reason": value.get("stop_reason"),
    }
    return _stream_result(result) if streaming else result


def _openai_embedding(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
) -> dict[str, Any]:
    value = _post(
        client,
        _endpoint(connection, "/embeddings"),
        dict(DEFAULT_JSON_HEADERS),
        {"model": _provider_model_id(request), "input": request.get("input")},
        request,
        connection=connection,
        credential_handle=credential_handle,
        credential_scope=credential_scope,
        credential_scheme="bearer",
    )
    data = value.get("data")
    data = data if isinstance(data, list) else []
    vectors = [list(item.get("embedding") or []) for item in data if isinstance(item, Mapping)]
    return {"vectors": vectors, "usage": dict(value.get("usage") or {})}


def _openai_image(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
) -> dict[str, Any]:
    body = {
        "model": _provider_model_id(request),
        "prompt": request.get("prompt"),
        **dict(request.get("parameters") or {}),
    }
    value = _post(
        client,
        _endpoint(connection, "/images/generations"),
        dict(DEFAULT_JSON_HEADERS),
        body,
        request,
        connection=connection,
        credential_handle=credential_handle,
        credential_scope=credential_scope,
        credential_scheme="bearer",
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
                "artifact_id": "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
                "uri": item.get("url"),
                "base64": item.get("b64_json"),
                "revised_prompt": item.get("revised_prompt"),
            }
        )
    return {"artifacts": artifacts}


def _provider_model_id(request: Mapping[str, Any]) -> str:
    model_id = str(request.get("model_id") or "")
    provider_id = str(request.get("provider_id") or "")
    prefix = f"{provider_id}/"
    if provider_id and model_id.startswith(prefix):
        return model_id[len(prefix) :]
    return model_id


def _stream_result(result: Mapping[str, Any]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    output = str(result.get("output") or "")
    if output:
        events.append({"type": "text_delta", "delta": output})
    for intent in result.get("tool_intents") or []:
        if isinstance(intent, Mapping):
            events.append(
                {
                    "type": "tool_intent_delta",
                    "tool_intent": dict(intent),
                }
            )
    events.extend(
        [
            {"type": "usage", "usage": dict(result.get("usage") or {})},
            {"type": "finish", "finish_reason": result.get("finish_reason")},
        ]
    )
    return {"events": events}


def _endpoint(connection: Mapping[str, Any], suffix: str) -> str:
    endpoint = str(connection.get("endpoint") or "").rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        raise GlobalContractInvocationError("not_configured", "provider endpoint is not configured")
    return endpoint + suffix


def _post(
    client: GlobalContractClient,
    endpoint: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
    credential_scheme: str,
) -> dict[str, Any]:
    deadline = float(request.get("deadline") or 0)
    try:
        value = client.post_json_with_credential(
            endpoint=endpoint,
            headers=headers,
            body=body,
            credential_handle=credential_handle,
            provider_instance_id=str(connection["provider_instance_id"]),
            credential_scope=credential_scope,
            credential_scheme=credential_scheme,
            deadline=deadline,
        )
    except (OSError, PermissionError, RuntimeError, ValueError) as exc:
        raise GlobalContractInvocationError("provider_unavailable", type(exc).__name__) from None
    if not isinstance(value, dict):
        raise GlobalContractInvocationError(
            "invalid_response", "provider returned a non-object response"
        )
    return value


_PROVIDER_OPERATIONS = {
    "rumi_provider_adapters_pack.provider.compatibility.embedding": (
        "embed",
        create_embedding_operation,
    ),
    "rumi_provider_adapters_pack.provider.compatibility.generate": (
        "generate",
        create_generate_operation,
    ),
    "rumi_provider_adapters_pack.provider.compatibility.image": (
        "generate",
        create_image_operation,
    ),
    "rumi_provider_adapters_pack.provider.compatibility.stream": (
        "stream",
        create_stream_operation,
    ),
}


class ProviderAdapterHostFactoryV4:
    """Capture one adapter Function behind authenticated Host capabilities."""

    def __init__(self, function_id: str) -> None:
        self.function_id = function_id

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind adapter execution to its exact resolved operation."""
        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            for binding in context.provider_bindings
        ):
            raise PermissionError("provider adapter bindings are incomplete")
        operation_name, operation_factory = _PROVIDER_OPERATIONS[self.function_id]

        def invoke(
            _operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({REGISTRY_CONTRACT}),
                consumer_pack_id="rumi_provider_adapters_pack",
            )
            return operation_factory(client)(operation_name, payload)

        contributions = []
        for binding in context.provider_bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                raise PermissionError("provider adapter domain binding is unavailable")
            contributions.append(
                HostProviderContributionV4(
                    contract_id=binding.operation.contract_id,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), lambda: None)


HOST_PROVIDER_FACTORY = {
    function_id: ProviderAdapterHostFactoryV4(function_id)
    for function_id in _PROVIDER_OPERATIONS
}
