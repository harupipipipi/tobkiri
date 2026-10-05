"""Execute registry-selected provider protocols with scoped credentials."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import threading
import time
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
from core_runtime.http_request_lifetime import HttpRequestLifetime
from ecosystem.rumi_provider_registry_pack.runtime.local_endpoint import (
    local_openai_endpoint,
)
from core_runtime.local_model_authority import LocalModelRequest
from ecosystem.rumi_provider_adapters_pack.runtime.streaming import stream_request
from tobkiri_protocol.turn_progress_v1 import ACTION as PROGRESS_CONTRACT

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
_MAX_LOCAL_REQUEST_BYTES = 1024 * 1024
_MAX_LOCAL_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_LOCAL_TIMEOUT_SECONDS = 30.0


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


def _operation(
    client: GlobalContractClient,
    *,
    streaming: bool,
    cancellation: threading.Event | None = None,
    deadline: float | None = None,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"invoke", "stream" if streaming else "generate"}
        if name not in allowed:
            raise ValueError(f"unknown provider adapter operation: {name}")
        request = dict(payload)
        connection = _connection(client, request, streaming=streaming)
        adapter_id = str(connection.get("adapter_id") or "")
        credential_handle = _credential_handle(
            request,
            connection,
            scope="ai.stream" if streaming else "ai.generate",
        )
        adapter = _adapter(
            adapter_id,
            provider_id=str(request.get("provider_id") or ""),
        )
        # Lifetime authority comes only from the captured Host invocation.
        # Payload fields never supply a cancellation Event or monotonic deadline.
        local_lifetime = (
            {"cancellation": cancellation, "deadline": deadline}
            if adapter is _local_openai_compatible else {}
        )
        return adapter(
            client,
            request,
            connection,
            credential_handle,
            "ai.stream" if streaming else "ai.generate",
            streaming,
            **local_lifetime,
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
    connection_id = request.get("provider_connection_id")
    if request.get("endpoint") is not None:
        raise GlobalContractInvocationError(
            "denied", "provider endpoint is bound by the Host provider registry"
        )
    if connection_id is None:
        # Catalog-driven requests retain the legacy provider-slug route.
        # Explicit saved connection IDs are opaque registry identities and
        # never pass through this prefixing compatibility path.
        provider_id = str(request.get("provider_id") or "").strip()
        if not provider_id:
            raise GlobalContractInvocationError(
                "invalid_request", "provider_id is required"
            )
        expected = f"provider.{provider_id}"
    elif not isinstance(connection_id, str) or not connection_id:
        raise GlobalContractInvocationError(
            "invalid_request", "provider_connection_id is invalid"
        )
    else:
        expected = connection_id
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
    matches = [
        dict(item)
        for item in providers
        if isinstance(item, Mapping)
        and str(item.get("provider_instance_id") or "") == expected
        and item.get("enabled") is True
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
) -> str | None:
    del scope
    supplied_handle = request.get("credential_handle")
    handle = connection.get("credential_handle")
    if supplied_handle is not None:
        raise GlobalContractInvocationError(
            "denied", "credential handle is bound by the Host provider registry"
        )
    if connection.get("adapter_id") == "local-openai-compatible":
        if handle is not None:
            raise GlobalContractInvocationError(
                "denied", "local provider credentials are not permitted"
            )
        try:
            local_openai_endpoint(connection.get("endpoint"))
        except ValueError:
            raise GlobalContractInvocationError(
                "denied", "local provider endpoint is invalid"
            ) from None
        return None
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
        "local-openai-compatible": _local_openai_compatible,
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
        **dict(request.get("parameters") or {}),
        "stream": streaming,
    }
    tools = request.get("tools")
    if isinstance(tools, list) and tools:
        body["tools"] = tools
    headers = dict(DEFAULT_JSON_HEADERS)
    if streaming:
        return stream_request(
            client, request, connection, body=body, endpoint=endpoint, headers=headers,
            credential_handle=credential_handle, credential_scope=credential_scope,
        )
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
    return result


def _local_openai_compatible(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str | None,
    credential_scope: str,
    streaming: bool,
    *,
    cancellation: threading.Event | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Call one registry-bound loopback OpenAI chat endpoint without a secret."""
    del client, credential_scope
    if credential_handle is not None:
        raise GlobalContractInvocationError(
            "denied", "local provider credentials are not permitted"
        )
    endpoint = local_openai_endpoint(connection.get("endpoint"))
    body = {
        "model": _provider_model_id(request),
        "messages": list(request.get("messages") or []),
        "stream": False,
        **dict(request.get("parameters") or {}),
    }
    tools = request.get("tools")
    if isinstance(tools, list) and tools:
        body["tools"] = tools
    value = _local_post(
        endpoint, body, request, cancellation=cancellation, deadline=deadline,
    )
    choices = value.get("choices") if isinstance(value, Mapping) else None
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, Mapping) else {}
    content = message.get("content") if isinstance(message, Mapping) else ""
    result: dict[str, Any] = {
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
    return result


def _local_post(
    endpoint: str,
    body: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    cancellation: threading.Event | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Send bounded JSON to the finite local chat route without proxy use."""
    parsed = urllib.parse.urlsplit(local_openai_endpoint(endpoint))
    encoded = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_LOCAL_REQUEST_BYTES:
        raise GlobalContractInvocationError(
            "invalid_request", "local provider request is too large"
        )
    timeout = _local_timeout(request)
    lifetime: HttpRequestLifetime | None = None
    connection: http.client.HTTPConnection | None = None
    response: http.client.HTTPResponse | None = None
    try:
        lifetime = HttpRequestLifetime(
            timeout=timeout, deadline=deadline, cancellation=cancellation,
        )
        connection = http.client.HTTPConnection(
            parsed.hostname, parsed.port, timeout=lifetime.remaining(),
        )
        connection.connect()
        if connection.sock is None:
            raise OSError("local provider connection socket is unavailable")
        # HTTP/1.0 and Connection: close can clear connection.sock while the
        # response still owns a blocked buffered reader. Retain the actual TCP
        # socket; the watchdog only shuts it down, and this caller closes IO.
        owned_socket = connection.sock
        lifetime.attach(owned_socket)
        owned_socket.settimeout(lifetime.remaining())
        connection.auto_open = 0
        lifetime.check()
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=encoded,
            headers={**DEFAULT_JSON_HEADERS, "Content-Length": str(len(encoded))},
        )
        owned_socket.settimeout(lifetime.remaining())
        response = connection.getresponse()
        lifetime.check()
        owned_socket.settimeout(lifetime.remaining())
        if response.status != 200:
            raise GlobalContractInvocationError(
                "provider_unavailable", "local provider returned an error"
            )
        raw = response.read(_MAX_LOCAL_RESPONSE_BYTES + 1)
        lifetime.check()
        if len(raw) > _MAX_LOCAL_RESPONSE_BYTES:
            raise GlobalContractInvocationError(
                "invalid_response", "local provider response is too large"
            )
    except GlobalContractInvocationError:
        raise
    except (OSError, TimeoutError, http.client.HTTPException):
        raise GlobalContractInvocationError(
            "provider_unavailable", "local provider is unavailable"
        ) from None
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                if lifetime is not None:
                    lifetime.close()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GlobalContractInvocationError(
            "invalid_response", "local provider returned invalid JSON"
        ) from None
    if not isinstance(value, dict):
        raise GlobalContractInvocationError(
            "invalid_response", "local provider returned a non-object response"
        )
    return value


def _local_timeout(request: Mapping[str, Any]) -> float:
    """Return the initial bounded wall-clock budget for the entire request."""
    deadline = request.get("deadline")
    if deadline is None:
        return _MAX_LOCAL_TIMEOUT_SECONDS
    if (
        not isinstance(deadline, (int, float))
        or isinstance(deadline, bool)
        or not math.isfinite(float(deadline))
    ):
        raise GlobalContractInvocationError(
            "invalid_request", "provider deadline is invalid"
        )
    timeout = min(_MAX_LOCAL_TIMEOUT_SECONDS, float(deadline) - time.time())
    if timeout <= 0:
        raise GlobalContractInvocationError(
            "provider_unavailable", "provider deadline expired"
        )
    return timeout


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
        "stream": streaming,
    }
    headers = {
        **DEFAULT_JSON_HEADERS,
        "anthropic-version": "2023-06-01",
    }
    if streaming:
        return stream_request(
            client, request, connection, body=body, endpoint=endpoint, headers=headers,
            credential_handle=credential_handle, credential_scope=credential_scope,
            scheme="anthropic", protocol="anthropic",
        )
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
    return result


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
            invocation.assert_current()
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(
                    {REGISTRY_CONTRACT, PROGRESS_CONTRACT}
                    if operation_factory is create_stream_operation else {REGISTRY_CONTRACT}
                ),
                consumer_pack_id="rumi_provider_adapters_pack",
            )
            if operation_factory in {
                create_generate_operation, create_stream_operation,
            }:
                operation = _operation(
                    client,
                    streaming=operation_factory is create_stream_operation,
                    cancellation=invocation.envelope.cancellation_requested,
                    deadline=invocation.envelope.deadline_monotonic,
                )
            else:
                operation = operation_factory(client)
            result = operation(operation_name, payload)
            invocation.assert_current()
            return result

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
