"""Incrementally decode bounded MCP SSE events without retaining diagnostics."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO


SSE_EVENT_LIMIT = 2 * 1024 * 1024
SSE_QUEUE_LIMIT = 16


def read_sse_events(stream: BinaryIO) -> Iterator[tuple[str, str]]:
    """Yield complete events, limiting every wire line and event to two MiB."""
    event_type = ""
    data: list[str] = []
    event_bytes = 0
    first_line = True
    while raw := stream.readline(SSE_EVENT_LIMIT + 1):
        event_bytes += len(raw)
        if event_bytes > SSE_EVENT_LIMIT:
            raise ValueError("MCP SSE event exceeds the byte limit")
        try:
            line = raw.decode("utf-8-sig" if first_line else "utf-8")
        except UnicodeDecodeError:
            raise ValueError("MCP SSE event is not valid UTF-8") from None
        first_line = False
        line = line.removesuffix("\n").removesuffix("\r")
        if not line:
            if data:
                yield event_type, "\n".join(data)
            event_type, data, event_bytes = "", [], 0
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                event_type = value
            elif field == "data":
                data.append(value)
