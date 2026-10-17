"""Bounded incremental Server-Sent Event decoding without transport authority."""

from __future__ import annotations

import json
import math
from typing import Any

MAX_STREAM_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 64 * 1024
MAX_STREAM_EVENTS = 4096


class SSEDecoder:
    """Decode actual received bytes without waiting for the whole response."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._data: list[bytes] = []
        self._event = "message"
        self._identifier: str | None = None
        self._seen: set[str] = set()
        self._total = 0
        self._event_bytes = 0
        self._events = 0
        self._done = False
        self._previous_cr = False

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        """Return only complete events in this received chunk, within hard bounds."""

        if not isinstance(chunk, bytes) or self._done:
            raise ValueError("provider stream framing is invalid")
        self._total += len(chunk)
        if self._total > MAX_STREAM_BYTES:
            raise ValueError("provider stream exceeds its limit")
        records = []
        for value in chunk:
            if self._done and value not in {10, 13}:
                raise ValueError("provider stream has data after its terminal event")
            if self._previous_cr:
                self._previous_cr = False
                if value == 10:
                    continue
            if value in {10, 13}:
                self._previous_cr = value == 13
                record = self._line(bytes(self._buffer))
                self._buffer.clear()
                if record is not None:
                    records.append(record)
                continue
            self._buffer.append(value)
            if len(self._buffer) + self._event_bytes > MAX_EVENT_BYTES:
                raise ValueError("provider stream event exceeds its limit")
        return records

    def finish(self) -> None:
        """Reject a truncated event instead of accepting an ambiguous response."""

        if self._buffer or self._data:
            raise ValueError("provider stream ended in a partial event")

    def _line(self, line: bytes) -> dict[str, Any] | None:
        self._event_bytes += len(line) + 1
        if self._event_bytes > MAX_EVENT_BYTES:
            raise ValueError("provider stream event exceeds its limit")
        if line:
            if line.startswith(b":"):
                return None
            field, _, raw = line.partition(b":")
            if raw.startswith(b" "):
                raw = raw[1:]
            if field == b"data":
                self._data.append(raw)
            elif field == b"event":
                self._event = raw.decode("utf-8", errors="strict")
            elif field == b"id":
                self._identifier = raw.decode("utf-8", errors="strict")
                if not self._identifier or "\x00" in self._identifier:
                    raise ValueError("provider stream event identity is invalid")
            return None
        data = b"\n".join(self._data)
        event, identifier = self._event, self._identifier
        self._data, self._event, self._identifier = [], "message", None
        self._event_bytes = 0
        if not data:
            return None
        self._events += 1
        if self._events > MAX_STREAM_EVENTS or identifier in self._seen:
            raise ValueError("provider stream event was repeated or exceeds its limit")
        if identifier is not None:
            self._seen.add(identifier)
        if data == b"[DONE]":
            self._done = True
            return {"event": event, "data": None, "done": True}

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("provider stream contains duplicate JSON keys")
                result[key] = value
            return result

        def constant(_value: str) -> None:
            raise ValueError("provider stream contains a nonfinite JSON value")

        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(value, dict):
            raise ValueError("provider stream event must be an object")
        pending = [(value, 0)]
        while pending:
            child, depth = pending.pop()
            if depth > 32:
                raise ValueError("provider stream JSON nesting exceeds its limit")
            if isinstance(child, dict):
                pending.extend((item, depth + 1) for item in child.values())
            elif isinstance(child, list):
                pending.extend((item, depth + 1) for item in child)
            elif isinstance(child, float) and not math.isfinite(child):
                raise ValueError("provider stream contains a nonfinite JSON value")
        return {"event": event, "data": value, "done": False}
