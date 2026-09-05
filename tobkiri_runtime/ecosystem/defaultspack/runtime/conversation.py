"""Canonical Pack v4 conversation entrypoints."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Mapping
import hashlib
import hmac
import json
import math
import re
import secrets
from typing import Any

AI_GENERATE_CONTRACT = "tobkiri.service.ai.generate.v1"
AI_GENERATE_OPERATION = "rumi_ai_gateway_pack.ai-gateway.generate"
AI_STREAM_CONTRACT = "tobkiri.service.ai.stream.v1"
AI_STREAM_OPERATION = "rumi_ai_gateway_pack.ai-gateway.stream"
_AI_CONTRACTS = frozenset({AI_GENERATE_CONTRACT, AI_STREAM_CONTRACT})

# This module is both the in-Host compatibility entrypoint and the staged
# PackVM implementation.  Keep these bridge values self-contained: a staged
# artifact is executed with ``python -I -S`` and cannot import Host modules.
PACKVM_BRIDGE_PROTOCOL = "io.tobkiri.packvm.bridge.v1"
PACKVM_BRIDGE_REQUEST_KIND = "tobkiri.packvm.bridge.request.v1"
PACKVM_BRIDGE_RESULT_KIND = "tobkiri.packvm.bridge.result.v1"
PACKVM_CONTINUATION_KIND = "tobkiri.packvm.continuation.v1"
PACKVM_BRIDGE_VERSION = 1

_PACKVM_OPERATION = "complete"
_MAX_BRIDGE_REQUEST_BYTES = 64 * 1024
_MAX_BRIDGE_RESULT_BYTES = 512 * 1024
_MAX_JSON_DEPTH = 8
_MAX_JSON_CONTAINER_ITEMS = 64
_MAX_JSON_STRING_LENGTH = 16 * 1024
_MAX_CONTINUATIONS_PER_GUEST = 256
_NONCE_RE = re.compile(r"[0-9a-f]{48}\Z")
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_BRIDGE_TARGET = {
    "contract_id": AI_GENERATE_CONTRACT,
    "operation_id": AI_GENERATE_OPERATION,
}
_CONSUMED_CONTINUATION_NONCES: set[str] = set()
_CONSUMED_CONTINUATION_ORDER: deque[str] = deque()


def get_container() -> Any:
    """Load the Host container only for the in-Host compatibility surface.

    The staged PackVM ABI never calls this function.  Leaving the Host import
    here instead of at module load time keeps a single staged file importable
    with ``python -I -S``.
    """

    from core_runtime.di_container import get_container as host_get_container

    return host_get_container()


def invoke(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Invoke canonical AI generation through the captured Host Broker."""
    request = _conversation_request(payload, surface="defaultspack.conversation")
    value = _client().invoke(
        AI_GENERATE_CONTRACT,
        AI_GENERATE_OPERATION,
        request,
    )
    if not isinstance(value, Mapping):
        raise RuntimeError("conversation Provider returned a non-object result")
    return _project_completion(value)


def stream(payload: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Invoke canonical AI streaming through the captured Host Broker."""
    request = _conversation_request(
        payload,
        surface="defaultspack.conversation.stream",
    )
    value = _client().invoke(
        AI_STREAM_CONTRACT,
        AI_STREAM_OPERATION,
        request,
    )
    events = value.get("events") if isinstance(value, Mapping) else None
    if not isinstance(events, list):
        raise RuntimeError("conversation Provider returned an invalid stream")
    return iter(_project_stream(events))


def _client() -> Any:
    """Return the captured Host dispatcher for the non-guest entrypoint."""

    from core_runtime.global_contract_dispatch import (
        GlobalContractClient,
        GlobalContractUnavailable,
    )

    session = get_container().get_or_none("v4_dispatch_session")
    if session is None:
        raise GlobalContractUnavailable(
            "Pack v4 dispatch session is required for conversation"
        )
    return GlobalContractClient(
        session=session,
        allowed_contract_ids=_AI_CONTRACTS,
        consumer_pack_id="defaultspack",
    )


def _conversation_request(
    payload: Mapping[str, Any],
    *,
    surface: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError("conversation payload must be an object")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty array")
    if any(not isinstance(item, Mapping) for item in messages):
        raise ValueError("every message must be an object")
    request = dict(payload)
    session = get_container().get_or_none("v4_dispatch_session")
    if session is not None:
        from core_runtime.global_contract_dispatch import captured_profile_id

        request.setdefault("profile_id", captured_profile_id(session))
    requirements = request.get("requirements")
    requirements = dict(requirements) if isinstance(requirements, Mapping) else {}
    requirements["request_surface"] = surface
    request["requirements"] = requirements
    return request


def tobkiri_packvm_invoke(
    operation_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the isolated Conversation ABI through the Host capability bridge.

    A first ``complete`` call returns one bounded, digest-pinned request for
    the Host-owned AI capability.  The authenticated Host channel must consume
    the continuation nonce exactly once and invoke this function again with a
    matching ``bridge_result``.  The guest neither imports Host DI nor opens a
    network connection.
    """

    if operation_id != _PACKVM_OPERATION:
        raise ValueError("unsupported PackVM conversation operation")
    if not isinstance(payload, Mapping):
        raise TypeError("PackVM conversation payload must be an object")

    # The outer operation payload can include Host/UI routing metadata.  It is
    # deliberately not bridged: only messages survive, and the guest creates
    # its own fixed capability request below.
    if (
        "messages" in payload
        and "continuation" not in payload
        and "bridge_result" not in payload
    ):
        return _bridge_request_for_messages(payload["messages"])
    if set(payload) == {"continuation", "bridge_result"}:
        return _resume_bridge_result(
            payload["continuation"],
            payload["bridge_result"],
        )
    raise ValueError(
        "PackVM conversation payload must contain messages or an exact bridge "
        "continuation result"
    )


def _bridge_request_for_messages(messages: Any) -> dict[str, Any]:
    """Create one Host-mediated generate request from a bounded turn."""

    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty array")
    if len(messages) > _MAX_JSON_CONTAINER_ITEMS:
        raise ValueError("messages exceeds the PackVM bridge limit")
    if any(not isinstance(item, Mapping) for item in messages):
        raise ValueError("every message must be an object")

    request = {
        "messages": _bounded_json_value(messages),
        "requirements": {"request_surface": "defaultspack.conversation"},
    }
    _assert_encoded_size(request, _MAX_BRIDGE_REQUEST_BYTES, "bridge request")
    request_digest = _canonical_digest(request)
    continuation = {
        "kind": PACKVM_CONTINUATION_KIND,
        "protocol": PACKVM_BRIDGE_PROTOCOL,
        "version": PACKVM_BRIDGE_VERSION,
        "operation_id": _PACKVM_OPERATION,
        "nonce": secrets.token_hex(24),
        "target": dict(_BRIDGE_TARGET),
        "request_digest": request_digest,
    }
    return {
        "kind": PACKVM_BRIDGE_REQUEST_KIND,
        "protocol": PACKVM_BRIDGE_PROTOCOL,
        "version": PACKVM_BRIDGE_VERSION,
        "target": dict(_BRIDGE_TARGET),
        "request": request,
        "request_digest": request_digest,
        "continuation": continuation,
    }


def _resume_bridge_result(
    continuation: Any,
    bridge_result: Any,
) -> dict[str, Any]:
    """Validate one authenticated Host response and project its completion."""

    checked_continuation = _validate_continuation(continuation)
    checked_result = _validate_bridge_result(bridge_result, checked_continuation)
    _consume_continuation_nonce(checked_continuation["nonce"])
    result = checked_result["result"]
    if result["status"] == "error":
        return _project_bridge_error(result["error"])
    return _project_completion(result["value"])


def _validate_continuation(value: Any) -> dict[str, Any]:
    """Reject a continuation whose identity or target is not exact."""

    continuation = _exact_object(
        value,
        {
            "kind",
            "protocol",
            "version",
            "operation_id",
            "nonce",
            "target",
            "request_digest",
        },
        "bridge continuation",
    )
    if (
        continuation["kind"] != PACKVM_CONTINUATION_KIND
        or continuation["protocol"] != PACKVM_BRIDGE_PROTOCOL
        or continuation["version"] != PACKVM_BRIDGE_VERSION
        or continuation["operation_id"] != _PACKVM_OPERATION
    ):
        raise ValueError("bridge continuation identity is invalid")
    nonce = continuation["nonce"]
    if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None:
        raise ValueError("bridge continuation nonce is invalid")
    target = _validate_bridge_target(continuation["target"])
    request_digest = continuation["request_digest"]
    if not _is_sha256_digest(request_digest):
        raise ValueError("bridge continuation request digest is invalid")
    return {
        "kind": PACKVM_CONTINUATION_KIND,
        "protocol": PACKVM_BRIDGE_PROTOCOL,
        "version": PACKVM_BRIDGE_VERSION,
        "operation_id": _PACKVM_OPERATION,
        "nonce": nonce,
        "target": target,
        "request_digest": request_digest,
    }


def _validate_bridge_result(
    value: Any,
    continuation: Mapping[str, Any],
) -> dict[str, Any]:
    """Accept only a Host result bound to one exact continuation."""

    bridge_result = _exact_object(
        value,
        {
            "kind",
            "protocol",
            "version",
            "operation_id",
            "nonce",
            "target",
            "request_digest",
            "result",
            "result_digest",
        },
        "bridge result",
    )
    if (
        bridge_result["kind"] != PACKVM_BRIDGE_RESULT_KIND
        or bridge_result["protocol"] != PACKVM_BRIDGE_PROTOCOL
        or bridge_result["version"] != PACKVM_BRIDGE_VERSION
        or bridge_result["operation_id"] != _PACKVM_OPERATION
        or bridge_result["nonce"] != continuation["nonce"]
        or bridge_result["request_digest"] != continuation["request_digest"]
        or _validate_bridge_target(bridge_result["target"])
        != continuation["target"]
    ):
        raise ValueError("bridge result does not match its continuation")
    result = _validate_bridge_outcome(bridge_result["result"])
    _assert_encoded_size(result, _MAX_BRIDGE_RESULT_BYTES, "bridge result")
    result_digest = bridge_result["result_digest"]
    if not _is_sha256_digest(result_digest) or not hmac.compare_digest(
        result_digest,
        _canonical_digest(result),
    ):
        raise ValueError("bridge result digest is invalid")
    return {
        "result": result,
        "result_digest": result_digest,
    }


def _validate_bridge_target(value: Any) -> dict[str, str]:
    """Return the only capability target this guest may request."""

    target = _exact_object(value, set(_BRIDGE_TARGET), "bridge target")
    if target != _BRIDGE_TARGET:
        raise ValueError("bridge target is not permitted")
    return dict(_BRIDGE_TARGET)


def _validate_bridge_outcome(value: Any) -> dict[str, Any]:
    """Validate the bounded result or typed error returned by the Host."""

    if not isinstance(value, Mapping):
        raise TypeError("bridge result must be an object")
    status = value.get("status")
    if status == "ok":
        outcome = _exact_object(value, {"status", "value"}, "bridge success")
        if not isinstance(outcome["value"], Mapping):
            raise TypeError("bridge success value must be an object")
        return {"status": "ok", "value": _bounded_json_value(outcome["value"])}
    if status == "error":
        outcome = _exact_object(value, {"status", "error"}, "bridge error")
        error = _exact_object(outcome["error"], {"code", "message"}, "bridge error")
        code = error["code"]
        message = error["message"]
        if not isinstance(code, str) or _ERROR_CODE_RE.fullmatch(code) is None:
            raise ValueError("bridge error code is invalid")
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > 512
        ):
            raise ValueError("bridge error message is invalid")
        return {"status": "error", "error": {"code": code, "message": message}}
    raise ValueError("bridge result status is invalid")


def _consume_continuation_nonce(nonce: str) -> None:
    """Fence duplicate resumes in this guest; the Host fences cross-VM replay."""

    if nonce in _CONSUMED_CONTINUATION_NONCES:
        raise ValueError("bridge continuation was already consumed")
    if len(_CONSUMED_CONTINUATION_NONCES) >= _MAX_CONTINUATIONS_PER_GUEST:
        oldest = _CONSUMED_CONTINUATION_ORDER.popleft()
        _CONSUMED_CONTINUATION_NONCES.remove(oldest)
    _CONSUMED_CONTINUATION_NONCES.add(nonce)
    _CONSUMED_CONTINUATION_ORDER.append(nonce)


def _project_bridge_error(error: Mapping[str, Any]) -> dict[str, Any]:
    """Project a Host error without exposing any unbounded bridge metadata."""

    return {
        "content": [],
        "tool_calls": [],
        "error": {"code": error["code"], "message": error["message"]},
    }


def _exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    """Copy an object only if it has the exact protocol field set."""

    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} fields are invalid")
    return dict(value)


def _bounded_json_value(value: Any, *, depth: int = 0) -> Any:
    """Copy a JSON value while bounding nesting, item count, and scalar size."""

    if depth > _MAX_JSON_DEPTH:
        raise ValueError("bridge JSON nesting exceeds the limit")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -(2**53) < value < 2**53:
            raise ValueError("bridge JSON integer is outside the safe range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("bridge JSON number must be finite")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_JSON_STRING_LENGTH:
            raise ValueError("bridge JSON string exceeds the limit")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("bridge JSON object exceeds the limit")
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError("bridge JSON object key is invalid")
            copied[key] = _bounded_json_value(item, depth=depth + 1)
        return copied
    if isinstance(value, list):
        if len(value) > _MAX_JSON_CONTAINER_ITEMS:
            raise ValueError("bridge JSON array exceeds the limit")
        return [_bounded_json_value(item, depth=depth + 1) for item in value]
    raise TypeError("bridge value must be JSON-compatible")


def _assert_encoded_size(value: Any, limit: int, label: str) -> None:
    """Ensure a canonical JSON envelope stays within its explicit byte cap."""

    if len(_canonical_json(value)) > limit:
        raise ValueError(f"{label} exceeds the size limit")


def _canonical_digest(value: Any) -> str:
    """Return the canonical SHA-256 identity used by the bridge channel."""

    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    """Encode bridge data deterministically without permissive JSON extensions."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("bridge value is not canonical JSON") from error


def _is_sha256_digest(value: Any) -> bool:
    """Return whether *value* has the exact canonical digest syntax."""

    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    suffix = value.removeprefix("sha256:")
    return len(suffix) == 64 and all(character in "0123456789abcdef" for character in suffix)


def _project_completion(value: Mapping[str, Any]) -> dict[str, Any]:
    tool_intents = [
        dict(item)
        for item in value.get("tool_intents") or []
        if isinstance(item, Mapping)
    ]
    return {
        **dict(value),
        "content": _content_blocks(value.get("output"), tool_intents),
        "tool_calls": tool_intents,
    }


def _content_blocks(
    output: Any,
    tool_intents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if isinstance(output, list):
        blocks.extend(dict(item) for item in output if isinstance(item, Mapping))
    elif output not in {None, ""}:
        blocks.append({"type": "text", "text": str(output)})
    for intent in tool_intents:
        blocks.append(
            {
                "type": "tool_use",
                "id": str(intent.get("intent_id") or ""),
                "name": str(intent.get("operation") or ""),
                "input": dict(intent.get("arguments") or {}),
            }
        )
    return blocks


def _project_stream(events: list[Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    for item in events:
        if not isinstance(item, Mapping):
            continue
        event_type = str(item.get("type") or "")
        if event_type in {"text_delta", "thinking_delta"}:
            text = str(item.get("delta") or "")
            if text:
                projected.append(
                    {
                        "type": (
                            "content_delta"
                            if event_type == "text_delta"
                            else "reasoning_delta"
                        ),
                        "delta": {"type": "text", "text": text},
                    }
                )
        elif event_type == "tool_intent_delta":
            intent = item.get("tool_intent")
            if isinstance(intent, Mapping):
                projected.append(
                    {
                        "type": "tool_use",
                        "id": str(intent.get("intent_id") or ""),
                        "name": str(intent.get("operation") or ""),
                        "input": dict(intent.get("arguments") or {}),
                    }
                )
        elif event_type == "usage":
            if isinstance(item.get("usage"), Mapping):
                usage = dict(item["usage"])
                if isinstance(item.get("usage_cost"), Mapping):
                    usage["usage_cost"] = dict(item["usage_cost"])
        elif event_type == "finish":
            projected.append(
                {
                    "type": "stream_end",
                    "finish_reason": str(item.get("finish_reason") or "stop"),
                    "usage": usage,
                }
            )
        elif event_type == "error":
            projected.append(
                {
                    "type": "stream_end",
                    "finish_reason": "error",
                    "usage": usage,
                }
            )
    return projected


__all__ = [
    "AI_GENERATE_CONTRACT",
    "AI_GENERATE_OPERATION",
    "AI_STREAM_CONTRACT",
    "AI_STREAM_OPERATION",
    "PACKVM_BRIDGE_PROTOCOL",
    "PACKVM_BRIDGE_REQUEST_KIND",
    "PACKVM_BRIDGE_RESULT_KIND",
    "PACKVM_CONTINUATION_KIND",
    "PACKVM_BRIDGE_VERSION",
    "invoke",
    "stream",
    "tobkiri_packvm_invoke",
]
