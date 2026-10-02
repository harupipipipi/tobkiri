"""Finite, credential-free text inference for owner-configured loopback models.

This capability is separate from cloud credential transport. The composition
root supplies its binding resolver and invocation guard; callers cannot supply
an endpoint, headers, credentials, a filesystem path, or a connection factory.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Event, Lock
from typing import Any

from core_runtime.http_request_lifetime import HttpRequestLifetime

_ENDPOINT = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/v1\Z")
_MAX_BYTES = 4 * 1024 * 1024
_ALLOWED_BODY = frozenset(
    {
        "model",
        "messages",
        "stream",
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "repeat_penalty",
        "repetition_penalty",
        "seed",
        "stop",
    }
)


@dataclass(frozen=True)
class LocalModelBinding:
    """Owner-configured model allowlist, bound to one registry revision."""

    profile_id: str
    provider_instance_id: str
    endpoint: str
    model_ids: tuple[str, ...]
    record_revision: int
    allowlist_digest: str = ""

    def __post_init__(self) -> None:
        match = _ENDPOINT.fullmatch(self.endpoint)
        if (
            not match
            or int(match[1]) > 65535
            or not self.profile_id
            or not self.provider_instance_id
            or type(self.record_revision) is not int
            or self.record_revision < 1
            or type(self.model_ids) is not tuple
            or not self.model_ids
            or any(
                not isinstance(model, str)
                or not model
                or len(model) > 256
                or any(ord(c) < 32 for c in model)
                for model in self.model_ids
            )
        ):
            raise ValueError("local model binding is invalid")


class LocalModelTransport:
    """Single-use, nonredirecting transport for one authorized Host invocation."""

    def __init__(
        self,
        *,
        profile_id: str,
        resolve_binding: Callable[[str], LocalModelBinding],
        assert_current: Callable[[], None],
        deadline_monotonic: float,
        cancellation: Event,
    ) -> None:
        self._profile_id = profile_id
        self._resolve_binding = resolve_binding
        self._assert_current = assert_current
        self._deadline = deadline_monotonic
        self._cancellation = cancellation
        self._lock = Lock()
        self._used = False

    def stream_chat(
        self, *, provider_instance_id: str, body: Mapping[str, Any], deadline: float,
        on_event: Callable[[Mapping[str, Any]], None],
    ) -> dict[str, Any]:
        """Deliver actual SSE frames from the exact owner-configured local model."""
        return self.post_chat(
            provider_instance_id=provider_instance_id, body=body, deadline=deadline,
            _on_event=on_event,
        )

    def post_chat(
        self,
        *,
        provider_instance_id: str,
        body: Mapping[str, Any],
        deadline: float,
        _on_event: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Send bounded plain-text chat to the exact current owner binding."""
        with self._lock:
            if self._used:
                raise PermissionError("local model transport already consumed")
            self._used = True
        self._assert_current()
        binding = self._resolve_binding(provider_instance_id)
        if (
            binding.profile_id != self._profile_id
            or binding.provider_instance_id != provider_instance_id
        ):
            raise PermissionError("local model binding does not match invocation")
        payload = _chat_body(body, binding, streaming=_on_event is not None)
        remaining = min(float(deadline) - time.time(), self._deadline - time.monotonic())
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError("local model request deadline elapsed")
        def authorized() -> bool:
            try:
                self._assert_current()
                return True
            except Exception:
                return False

        lifetime = HttpRequestLifetime(
            timeout=remaining,
            deadline=self._deadline,
            cancellation=self._cancellation, authority_check=authorized,
        )
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            int(binding.endpoint.removeprefix("http://127.0.0.1:").removesuffix("/v1")),
            timeout=lifetime.remaining(),
        )
        try:
            self._check(binding, lifetime)
            # A numeric IPv4 literal is used directly. HTTPConnection does not
            # consult proxy variables and never follows a redirect.
            connection.connect()
            if connection.sock is None:
                raise OSError("local model connection is unavailable")
            lifetime.attach(connection.sock)
            self._check(binding, lifetime)
            connection.request(
                "POST",
                "/v1/chat/completions",
                payload,
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            with connection.getresponse() as response:
                if response.status != 200:
                    raise RuntimeError("local model returned an unsuccessful response")
                if _on_event is not None:
                    from core_runtime.credential_stream import received_sse
                    for event in received_sse(
                        response, guard=lambda: self._check(binding, lifetime),
                    ):
                        _on_event(event)
                    self._check(binding, lifetime)
                    return {"stream_complete": True}
                data = response.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise ValueError("local model response exceeds limit")
            result = _response_json(data)
            if not isinstance(result, dict):
                raise TypeError("local model response must be an object")
            self._check(binding, lifetime)
            return result
        finally:
            lifetime.close()
            connection.close()

    def _check(self, binding: LocalModelBinding, lifetime: HttpRequestLifetime) -> None:
        lifetime.check()
        self._assert_current()
        if self._resolve_binding(binding.provider_instance_id) != binding:
            raise PermissionError("local model configuration changed")
        # Resolving the registry is a nested Broker call and may block. Fence
        # authority and deadline again after it returns, before sending bytes
        # or releasing a result to the caller.
        lifetime.check()
        self._assert_current()


def _chat_body(
    body: Mapping[str, Any], binding: LocalModelBinding, *, streaming: bool = False,
) -> bytes:
    if set(body) - _ALLOWED_BODY or body.get("model") not in binding.model_ids:
        raise PermissionError("local model request is outside configured scope")
    if body.get("stream", False) is not streaming:
        raise ValueError("local model transport accepts complete responses only")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 1024:
        raise ValueError("local model messages are invalid")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in {"system", "user", "assistant"}
            or not isinstance(message["content"], str)
        ):
            raise ValueError("local model route supports plain text messages only")
    normalized = dict(body)
    for name, lower, upper in (
        ("temperature", 0, 2),
        ("top_p", 0, 1),
        ("repeat_penalty", 0, 2),
        ("repetition_penalty", 0, 2),
    ):
        if name in normalized:
            value = normalized[name]
            if (
                type(value) not in (int, float)
                or not lower <= value <= upper
                or not math.isfinite(value)
            ):
                raise ValueError("local model sampling value is invalid")
    for name, lower, upper in (("top_k", 0, 1000), ("seed", -1, 2**32 - 1)):
        if name in normalized:
            value = normalized[name]
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("local model sampling value is invalid")
    if "stop" in normalized:
        stop = normalized["stop"]
        values = [stop] if isinstance(stop, str) else stop
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 16
            or any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in values)
        ):
            raise ValueError("local model stop sequences are invalid")
    tokens = normalized.get("max_tokens", 512)
    if type(tokens) is not int or not 1 <= tokens <= 4096:
        raise ValueError("local model token limit is invalid")
    normalized.update(max_tokens=tokens, stream=streaming)
    encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 1024 * 1024:
        raise ValueError("local model request exceeds limit")
    return encoded


def _response_json(data: bytes) -> dict[str, Any]:
    """Reject ambiguous/nonfinite/deep JSON while retaining provider timings."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate local model response key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError("nonfinite local model response number")

    try:
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError:
        raise ValueError("local model response nesting exceeds limit") from None
    if not isinstance(result, dict):
        raise TypeError("local model response must be an object")
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise ValueError("local model response nesting exceeds limit")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("nonfinite local model response number")
    return result
