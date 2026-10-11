"""Normalize actual incremental provider frames while the transport is open."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from tobkiri_protocol.turn_progress_v1 import (
    ACTION,
    ACTION_OPERATION,
    MAX_BYTES,
    MAX_EVENTS,
    validate_event,
)


class ProviderStream:
    """Bound one response, rejecting events after its terminal provider frame."""

    def __init__(self, protocol: str, publish: Callable[[Mapping[str, Any]], None]) -> None:
        self.protocol, self.publish = protocol, publish
        self.events: list[dict[str, Any]] = []
        self.text: list[str] = []
        self.tools: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] = {}
        self.finish: str | None = None
        self.done, self.size = False, 0

    def emit(self, event: dict[str, Any]) -> None:
        """Publish received display deltas before accumulating the final receipt."""
        self.size += len(json.dumps(event, ensure_ascii=False).encode())
        if len(self.events) >= MAX_EVENTS or self.size > MAX_BYTES:
            raise ValueError("normalized provider stream exceeds its limit")
        if event["type"] in {"text_delta", "thinking_delta", "finish"}:
            self.publish(validate_event(event))
        self.events.append(event)

    def receive(self, frame: Mapping[str, Any]) -> None:
        """Consume one sanitized actual frame, without timers or synthetic splitting."""
        if self.done:
            raise ValueError("provider emitted after terminal frame")
        data = frame.get("data")
        if frame.get("done") or (isinstance(data, Mapping) and data.get("type") == "message_stop"):
            if self.finish is None:
                raise ValueError("provider terminal frame lacks finish reason")
            for tool in self.tools.values():
                self.emit({"type": "tool_intent_delta", "tool_intent": tool})
            self.emit({"type": "usage", "usage": self.usage})
            self.emit({"type": "finish", "finish_reason": self.finish})
            self.done = True
            return
        if not isinstance(data, Mapping):
            raise ValueError("provider stream frame is invalid")
        if self.protocol == "anthropic":
            self._anthropic(data)
        else:
            self._openai(data)

    def _delta(self, value: Any, *, thinking: bool = False) -> None:
        if not isinstance(value, str):
            raise ValueError("provider text delta is invalid")
        if self.finish is not None and value:
            raise ValueError("provider delta follows its finish reason")
        if value:
            if thinking:
                # Raw reasoning is not a public display summary. Preserve only state.
                value = ""
            if not thinking:
                self.text.append(value)
            self.emit({"type": "thinking_delta" if thinking else "text_delta", "delta": value})

    def _openai(self, data: Mapping[str, Any]) -> None:
        if data.get("error"):
            raise ValueError("provider returned a stream error")
        if "usage" in data and data["usage"] is not None:
            if not isinstance(data["usage"], Mapping):
                raise ValueError("provider stream usage is invalid")
            self.usage = dict(data["usage"])
        choices = data.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("provider stream choices are invalid")
        if not choices:
            return
        choice = choices[0]
        if not isinstance(choice, Mapping) or choice.get("index", 0) != 0:
            raise ValueError("provider stream choice index is invalid")
        delta = choice.get("delta", {})
        if not isinstance(delta, Mapping):
            raise ValueError("provider stream delta is invalid")
        if delta.get("content") is not None:
            self._delta(delta["content"])
        if delta.get("reasoning_content") is not None:
            self._delta(delta["reasoning_content"], thinking=True)
        for part in delta.get("tool_calls") or []:
            if (
                not isinstance(part, Mapping)
                or type(part.get("index")) is not int
                or not 0 <= part["index"] < 16
            ):
                raise ValueError("provider tool delta index is invalid")
            if self.finish is not None:
                raise ValueError("provider tool delta follows finish")
            index = part["index"]
            tool = self.tools.setdefault(
                index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
            function = part.get("function") or {}
            if not isinstance(function, Mapping):
                raise ValueError("provider tool function is invalid")
            for destination, key, source in (
                (tool, "id", part),
                (tool["function"], "name", function),
                (tool["function"], "arguments", function),
            ):
                value = source.get(key, "")
                if not isinstance(value, str):
                    raise ValueError("provider tool delta is invalid")
                destination[key] += value
                if len(destination[key].encode()) > 64 * 1024:
                    raise ValueError("provider tool delta exceeds limit")
        if choice.get("finish_reason") is not None:
            if self.finish is not None:
                raise ValueError("provider repeated finish reason")
            self.finish = str(choice["finish_reason"])

    def _anthropic(self, data: Mapping[str, Any]) -> None:
        kind = data.get("type")
        if kind == "error":
            raise ValueError("provider returned a stream error")
        if kind == "message_start":
            self.usage.update((data.get("message") or {}).get("usage") or {})
        elif kind == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                self._delta(delta.get("text"))
            elif delta.get("type") == "thinking_delta":
                self._delta(delta.get("thinking"), thinking=True)
            else:
                raise ValueError("unsupported provider stream delta")
        elif kind == "message_delta":
            reason = (data.get("delta") or {}).get("stop_reason")
            if reason is not None:
                if self.finish is not None:
                    raise ValueError("provider repeated finish reason")
                self.finish = str(reason)
            self.usage.update(data.get("usage") or {})
        elif kind not in {"ping", "content_block_start", "content_block_stop"}:
            raise ValueError("unknown provider stream event")

    def result(self) -> dict[str, Any]:
        """Return completion data only after the real terminal frame was received."""
        if not self.done:
            raise ValueError("provider stream did not complete")
        return {
            "status": "ok",
            "output": "".join(self.text),
            "events": self.events,
            "tool_intents": list(self.tools.values()),
            "usage": self.usage,
            "finish_reason": self.finish,
            "delivery_mode": "incremental",
        }


def stream_request(
    client: Any,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    *,
    body: Mapping[str, Any],
    endpoint: str | None,
    headers: Mapping[str, str],
    credential_handle: str = "",
    credential_scope: str = "",
    scheme: str = "bearer",
    protocol: str = "openai",
    local: bool = False,
) -> dict[str, Any]:
    """Own one transport and publish only its sanitized received display frames."""
    cursor = 0

    def publish(event: Mapping[str, Any]) -> None:
        nonlocal cursor
        if request.get("progress_id") is not None:
            cursor += 1
            client.invoke(
                ACTION,
                ACTION_OPERATION,
                {
                    "phase": "publish",
                    "progress_id": request["progress_id"],
                    "cursor": cursor,
                    "event": dict(event),
                },
            )

    stream = ProviderStream(protocol, publish)
    try:
        if request.get("progress_id") is not None:
            client.invoke(
                ACTION,
                ACTION_OPERATION,
                {
                    "phase": "claim",
                    "progress_id": request["progress_id"],
                },
            )
        if local:
            client.stream_local_model_chat(
                provider_instance_id=connection["provider_instance_id"],
                body=body,
                deadline=float(request.get("deadline") or 0),
                on_event=stream.receive,
            )
        else:
            client.stream_json_with_credential(
                endpoint=endpoint,
                headers={**headers, "Accept": "text/event-stream"},
                body=body,
                credential_handle=credential_handle,
                provider_instance_id=connection["provider_instance_id"],
                credential_scope=credential_scope,
                credential_scheme=scheme,
                deadline=float(request.get("deadline") or 0),
                on_event=stream.receive,
            )
        return stream.result()
    except (OSError, PermissionError, RuntimeError, ValueError, TypeError):
        raise GlobalContractInvocationError("stream_failed", "provider stream failed") from None
