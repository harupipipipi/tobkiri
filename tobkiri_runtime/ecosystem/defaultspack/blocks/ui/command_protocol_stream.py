from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error
from domain.frontend.command_protocol import CommandProtocolRegistry
from domain.frontend.invocation_events import InvocationEventError


def _header(headers: dict[str, Any], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value or "").strip()
    return ""


def _encode_event(event: dict[str, Any]) -> bytes:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return (
        f"id: {int(event['sequence'])}\n"
        f"event: {event['type']}\n"
        f"data: {payload}\n\n"
    ).encode("utf-8")


def _stream(
    registry: CommandProtocolRegistry,
    invocation_id: str,
    after_sequence: int,
    wait_seconds: float,
    owner_key: str,
) -> Iterator[bytes]:
    deadline = time.monotonic() + wait_seconds
    sequence = after_sequence
    while True:
        events = registry.events.resume(
            invocation_id,
            after_sequence=sequence,
            limit=500,
            owner_key=owner_key,
        )
        for event in events:
            sequence = int(event["sequence"])
            yield _encode_event(event)
        snapshot = registry.events.snapshot(
            invocation_id,
            owner_key=owner_key,
        )
        if snapshot["terminal"] or time.monotonic() >= deadline:
            return
        yield b": keepalive\n\n"
        time.sleep(0.25)


def run(input_data, context):
    payload = input_data if isinstance(input_data, dict) else {}
    invocation_id = str(payload.get("invocation_id") or "").strip()
    if not invocation_id:
        return error("invocation_id is required", "INVALID_INPUT")
    try:
        headers = payload.get("_headers")
        headers = headers if isinstance(headers, dict) else {}
        cursor = payload.get("after_sequence")
        if cursor in (None, ""):
            cursor = _header(headers, "last-event-id") or 0
        after_sequence = int(cursor)
        wait_seconds = float(payload.get("wait_seconds") or 15)
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if not 0 <= wait_seconds <= 30:
            raise ValueError("wait_seconds must be between 0 and 30")
        registry = CommandProtocolRegistry()
        owner_key = registry.owner_key(payload, context or {})
        registry.reconcile_approval(payload, context or {})
    except (TypeError, ValueError, InvocationEventError) as exc:
        return error(str(exc), "INVALID_INPUT")
    return {
        "_sse": True,
        "events": _stream(
            registry,
            invocation_id,
            after_sequence,
            wait_seconds,
            owner_key,
        ),
    }
