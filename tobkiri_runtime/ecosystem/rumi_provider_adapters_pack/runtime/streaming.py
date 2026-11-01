"""Normalize actual incremental provider frames while the transport is open."""

from __future__ import annotations

import http.client
import json
import logging
import math
import re
import threading
import time
import urllib.error
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import (
    GlobalContractInvocationError, HostGenerationBudgetError,
)
from tobkiri_protocol.turn_progress_v1 import (
    ACTION,
    ACTION_OPERATION,
    MAX_BYTES,
    MAX_EVENTS,
    MAX_DELTA_BYTES,
    validate_event,
)


_REASONS = frozenset(
    {
        "timeout",
        "network",
        "http_status",
        "parser",
        "cancelled",
        "progress",
        "policy",
        "unknown",
    }
)
_STAGES = frozenset(
    {
        "prepare",
        "dns",
        "connect",
        "tls",
        "send",
        "headers",
        "body",
        "decode",
        "normalize",
        "progress_claim",
        "progress_publish",
        "finalize",
    }
)
_MAX_DURATION_MS = 86_400_000
_LOGGER = logging.getLogger(__name__)


def _request_id(value: object) -> str:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"request\.[0-9a-f]{32}", value)
        else "unknown"
    )


def _log(record: Mapping[str, object]) -> None:
    _LOGGER.warning("%s", json.dumps(dict(record), sort_keys=True))


class RequestFailureDiagnostics:
    """Observe one captured request without authorizing or cancelling work."""

    def __init__(
        self,
        request_id: object,
        *,
        parent_request_id: object = None,
        clock: Callable[[], float] = time.monotonic,
        sink: Callable[[Mapping[str, object]], None] = _log,
    ) -> None:
        self._request_id = _request_id(request_id)
        self._parent_id = _request_id(parent_request_id)
        self._clock, self._sink = clock, sink
        self._lock = threading.RLock()
        self._started = self._now()
        self._stage_started = self._started
        self._stage = "prepare"
        self._interruption = "unknown"
        self._emitted = False

    def _now(self) -> float:
        try:
            value = self._clock()
            return (
                float(value)
                if type(value) in {int, float} and math.isfinite(value)
                else 0.0
            )
        except Exception:
            return 0.0

    def stage(self, stage: str) -> None:
        """Select only a fixed stage; no caller strings enter the record."""
        with self._lock:
            normalized = stage if type(stage) is str and stage in _STAGES else "prepare"
            if normalized != self._stage:
                self._stage, self._stage_started = normalized, self._now()

    def interrupted(self, reason: str) -> None:
        """Latch a trusted lifetime observation across its owned watchdog thread."""
        with self._lock:
            if self._interruption == "unknown":
                self._interruption = (
                    reason
                    if type(reason) is str
                    and reason
                    in {
                        "timeout",
                        "cancelled",
                        "policy",
                    }
                    else "unknown"
                )

    def failure(self, error: BaseException, *, reason: str | None = None) -> None:
        """Emit at most once, before a boundary removes the original error type."""
        with self._lock:
            if self._emitted:
                return
            self._emitted = True
            if self._interruption != "unknown":
                reason = self._interruption
            elif reason is None:
                if self._stage in {"progress_claim", "progress_publish"}:
                    reason = "progress"
                elif isinstance(error, TimeoutError):
                    reason = "timeout"
                elif isinstance(error, InterruptedError):
                    reason = "cancelled"
                elif isinstance(error, urllib.error.HTTPError):
                    reason = "http_status"
                elif isinstance(error, PermissionError):
                    reason = "policy"
                elif isinstance(error, (OSError, http.client.HTTPException)):
                    reason = "network"
                elif isinstance(error, (ValueError, TypeError, UnicodeError, KeyError)):
                    reason = "parser"
            bucket = "none"
            if isinstance(error, urllib.error.HTTPError) and type(error.code) is int:
                status = error.code
                bucket = (
                    "redirect"
                    if 300 <= status < 400
                    else (
                        "auth"
                        if status in {401, 403}
                        else (
                            "rate_limit"
                            if status == 429
                            else (
                                "client"
                                if 400 <= status < 500
                                else "server" if 500 <= status < 600 else "other"
                            )
                        )
                    )
                )
            now = self._now()
            record = {
                "event": "host_request_failure",
                "request_id": self._request_id,
                "parent_request_id": self._parent_id,
                "reason": (
                    reason if type(reason) is str and reason in _REASONS else "unknown"
                ),
                "stage": self._stage,
                "http_bucket": bucket,
                "elapsed_ms": self._duration(now, self._started),
                "stage_elapsed_ms": self._duration(now, self._stage_started),
            }
        try:
            self._sink(record)
        except Exception:
            pass

    @staticmethod
    def _duration(now: float, started: float) -> int:
        elapsed = now - started
        if not math.isfinite(elapsed) or elapsed <= 0:
            return 0
        return min(_MAX_DURATION_MS, int(min(elapsed, 86_400) * 1000))


_TEXT_BATCH_BYTES = 4096


class DisplayProgressBuffer:
    """Coalesce adjacent display deltas; canonical provider events stay separate.

    The first nonempty text is immediate. Later text is joined by byte size,
    without timers or background work, and drained before a kind change or a
    real finish. A failed publication is terminal and is never retried.
    """

    def __init__(self, publish: Callable[[Mapping[str, Any]], None]) -> None:
        self._publish = publish
        self._pending: dict[str, Any] | None = None
        self._pending_bytes = 0
        self._first_text_published = False
        self._last_published_kind: str | None = None
        self._failed = False
        self._finished = False

    def publish(self, event: Mapping[str, Any]) -> None:
        """Publish only validated actual display data, preserving its order."""
        if self._failed or self._finished:
            raise ValueError("display progress is already terminal")
        try:
            clean = validate_event(event)
        except (TypeError, ValueError):
            self._failed = True
            raise
        kind = clean["type"]
        if kind not in {"text_delta", "thinking_delta"}:
            self._flush()
            self._send(clean)
            self._finished = kind == "finish"
            return
        if kind == "thinking_delta" and clean["delta"] == "":
            self._flush()
            # Reasoning has already been removed by ProviderStream. Repeated
            # empty notifications carry no additional display information.
            if self._last_published_kind != kind:
                self._send(clean)
            return
        if kind == "text_delta" and clean["delta"]:
            if not self._first_text_published:
                self._flush()
                self._send(clean)
                self._first_text_published = True
                return
        size = len(clean["delta"].encode("utf-8"))
        if self._pending is not None and (
            self._pending["type"] != kind or self._pending_bytes + size > MAX_DELTA_BYTES
        ):
            self._flush()
        if self._pending is None:
            self._pending = clean
        else:
            self._pending["delta"] += clean["delta"]
        self._pending_bytes += size
        if self._pending_bytes >= _TEXT_BATCH_BYTES:
            self._flush()

    def _flush(self) -> None:
        if self._pending is not None:
            pending, self._pending = self._pending, None
            self._pending_bytes = 0
            self._send(pending)

    def _send(self, event: Mapping[str, Any]) -> None:
        try:
            self._publish(event)
        except BaseException:
            self._failed = True
            raise
        self._last_published_kind = str(event["type"])


class ProviderStream:
    """Bound one response, rejecting events after its terminal provider frame."""

    def __init__(self, protocol: str, publish: Callable[[Mapping[str, Any]], None]) -> None:
        self.protocol, self.publish = protocol, publish
        self._display_progress = DisplayProgressBuffer(publish)
        self.events: list[dict[str, Any]] = []
        self.text: list[str] = []
        self.tools: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] = {}
        self.finish: str | None = None
        self.done, self.size = False, 0

    def emit(self, event: dict[str, Any]) -> None:
        """Retain every bounded event and coalesce only provisional display writes."""
        self.size += len(json.dumps(event, ensure_ascii=False).encode())
        if len(self.events) >= MAX_EVENTS or self.size > MAX_BYTES:
            raise ValueError("normalized provider stream exceeds its limit")
        if event["type"] in {"text_delta", "thinking_delta", "finish"}:
            self._display_progress.publish(event)
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
    diagnostics: RequestFailureDiagnostics | None = None,
) -> dict[str, Any]:
    """Own one transport and publish only its sanitized received display frames."""
    cursor = 0

    def publish(event: Mapping[str, Any]) -> None:
        nonlocal cursor
        if request.get("progress_id") is not None:
            cursor += 1
            if diagnostics is not None:
                diagnostics.stage("progress_publish")
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
            if diagnostics is not None:
                diagnostics.stage("normalize")

    stream = ProviderStream(protocol, publish)
    def receive(frame: Mapping[str, Any]) -> None:
        if diagnostics is not None:
            diagnostics.stage("normalize")
        try:
            stream.receive(frame)
        except Exception as error:
            if diagnostics is not None:
                diagnostics.failure(error)
            raise
        if diagnostics is not None:
            diagnostics.stage("body")

    try:
        if request.get("progress_id") is not None:
            if diagnostics is not None:
                diagnostics.stage("progress_claim")
            client.invoke(
                ACTION,
                ACTION_OPERATION,
                {
                    "phase": "claim",
                    "progress_id": request["progress_id"],
                },
            )
        if diagnostics is not None:
            diagnostics.stage("body")
        if local:
            client.stream_local_model_chat(
                provider_instance_id=connection["provider_instance_id"],
                body=body,
                deadline=float(request.get("deadline") or 0),
                on_event=receive,
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
                on_event=receive,
            )
        if diagnostics is not None:
            diagnostics.stage("finalize")
        return stream.result()
    except HostGenerationBudgetError as error:
        if diagnostics is not None:
            diagnostics.failure(error, reason="policy")
        raise GlobalContractInvocationError(
            "generation_budget_denied", "Host generation budget denied",
        ) from None
    except (OSError, PermissionError, RuntimeError, ValueError, TypeError) as error:
        if diagnostics is not None:
            diagnostics.failure(error)
        raise GlobalContractInvocationError("stream_failed", "provider stream failed") from None
