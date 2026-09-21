"""blocks.mobile.events — イベント・承認のポーリング取得.

モバイルがPC上のツール実行状況を取得するためのエンドポイント。
承認操作は challenge/attestation 付きの /api/authority/* に一本化する。

ルート:
  GET  /api/mobile/v1/events?after={cursor}    → イベント一覧
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok


def _merged(input_data: dict) -> dict:
    if not isinstance(input_data, dict):
        return {}
    merged: dict = {}
    for container_key in ("query_params", "params", "body", "path_params", "query"):
        value = input_data.get(container_key)
        if isinstance(value, dict):
            merged.update(value)
    for key, value in input_data.items():
        if key in {"query_params", "params", "body", "path_params", "query"}:
            continue
        merged[key] = value
    return merged


def list_events(input_data, context=None):
    args = _merged(input_data)
    cursor = str(args.get("after") or args.get("cursor") or "event-0")
    # Delegate to existing authority event stream
    try:
        from core_runtime.authority import get_authority_service
        service = get_authority_service()
        events = service.events(after=cursor, limit=50)
        return ok({"events": events, "cursor": f"event-{len(events)}"})
    except Exception:
        return ok({"events": [], "cursor": cursor})


def run(input_data, context=None):
    args = _merged(input_data)
    action = str(args.get("action") or "").strip().lower()
    handlers = {
        "list_events": list_events,
    }
    handler = handlers.get(action)
    if handler is None:
        return error(f"unknown events action: {action}", "UNKNOWN_ACTION")
    return handler(input_data, context)
