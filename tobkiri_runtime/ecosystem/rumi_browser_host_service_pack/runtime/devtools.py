"""Bounded DevTools inspection and credential-safe network observations."""

from __future__ import annotations

import json
import math
import time
from typing import Any, Mapping

from .cdp import CDPConnection, public_url

_PUBLIC_ATTRIBUTES = {
    "id",
    "class",
    "role",
    "type",
    "name",
    "placeholder",
    "aria-label",
    "aria-labelledby",
    "aria-expanded",
    "aria-checked",
    "href",
    "src",
}


def inspect_page(connection: CDPConnection) -> dict[str, Any]:
    """Return a small DOM tree without form values and URL credentials."""

    document = connection.call("DOM.getDocument", {"depth": 6, "pierce": True})
    budget = [1_000]
    dom = _public_node(document.get("root"), budget)
    layout = connection.call("Page.getLayoutMetrics")
    return {
        "dom": dom,
        "dom_truncated": budget[0] <= 0,
        "layout": {
            key: layout.get(key)
            for key in ("cssLayoutViewport", "cssVisualViewport", "cssContentSize")
            if isinstance(layout.get(key), dict)
        },
    }


def evaluate_page(connection: CDPConnection, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one explicitly approved expression in the selected page."""

    expression = payload.get("expression")
    if not isinstance(expression, str) or not 1 <= len(expression.encode("utf-8")) <= 16_384:
        raise ValueError("A JavaScript expression of at most 16 KiB is required")
    result = connection.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "timeout": 5_000,
        },
    )
    if result.get("exceptionDetails"):
        return {
            "is_error": True,
            "error_type": "browser_evaluation_failed",
            "message": "The expression raised a JavaScript exception.",
        }
    remote = result.get("result")
    if not isinstance(remote, dict):
        return {"value": None}
    if len(json.dumps(remote, ensure_ascii=False).encode("utf-8")) > 262_144:
        return {"type": remote.get("type"), "truncated": True}
    return {
        key: remote[key]
        for key in ("type", "subtype", "value", "unserializableValue")
        if key in remote
    }


def capture_network(connection: CDPConnection, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Observe one tab over a single connection for at most ten seconds."""

    duration = payload.get("duration_ms", 2_000)
    limit = payload.get("max_entries", 200)
    if isinstance(duration, bool) or not isinstance(duration, int) or not 100 <= duration <= 10_000:
        raise ValueError("Network duration_ms must be between 100 and 10000")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("Network max_entries must be between 1 and 500")
    if "reload" in payload and not isinstance(payload["reload"], bool):
        raise ValueError("Network reload must be a boolean")
    requests: list[dict[str, Any]] = []
    console: list[dict[str, Any]] = []
    current_requests: dict[str, dict[str, Any]] = {}
    request_timestamps: dict[str, float] = {}
    connection.call(
        "Network.enable",
        {
            "maxTotalBufferSize": 1_048_576,
            "maxResourceBufferSize": 65_536,
            "maxPostDataSize": 0,
        },
    )
    connection.call("Runtime.enable")
    connection.call("Log.enable")
    if payload.get("reload") is True:
        connection.call("Page.reload", {"ignoreCache": False})
    started = time.monotonic()
    deadline = started + duration / 1_000
    truncated = False
    event_count = 0
    while time.monotonic() < deadline:
        event = connection.event(min(0.2, max(0.01, deadline - time.monotonic())))
        if event is None:
            continue
        event_count += 1
        if event_count > 20_000:
            truncated = True
            break
        method = event.get("method")
        params = event.get("params")
        if not isinstance(params, dict):
            continue
        request_id = str(params.get("requestId") or "")[:256]
        if method == "Network.requestWillBeSent":
            if len(requests) + len(console) >= limit:
                truncated = True
                continue
            request = params.get("request")
            if not isinstance(request, dict):
                continue
            redirected = params.get("redirectResponse")
            previous = current_requests.get(request_id)
            if isinstance(redirected, dict) and previous is not None:
                previous["status"] = redirected.get("status")
                previous["redirected"] = True
                _set_request_duration(
                    previous, request_timestamps.get(request_id), params.get("timestamp")
                )
            new_row: dict[str, Any] = {
                "request_id": request_id,
                "url": public_url(request.get("url")),
                "method": str(request.get("method") or "")[:32],
                "type": str(params.get("type") or "")[:64],
            }
            requests.append(new_row)
            current_requests[request_id] = new_row
            timestamp = _finite_timestamp(params.get("timestamp"))
            if timestamp is not None:
                request_timestamps[request_id] = timestamp
        elif method == "Network.responseReceived":
            row = current_requests.get(request_id)
            response = params.get("response")
            if row is not None and isinstance(response, dict):
                row.update(
                    {
                        "status": response.get("status"),
                        "mime_type": str(response.get("mimeType") or "")[:128],
                        "from_cache": bool(
                            response.get("fromDiskCache") or response.get("fromServiceWorker")
                        ),
                    }
                )
        elif method == "Network.loadingFinished":
            row = current_requests.get(request_id)
            if row is not None:
                row["encoded_data_length"] = params.get("encodedDataLength")
                row["finished"] = True
                _set_request_duration(
                    row, request_timestamps.get(request_id), params.get("timestamp")
                )
        elif method == "Network.loadingFailed":
            row = current_requests.get(request_id)
            if row is not None:
                row["error"] = True
                row["canceled"] = bool(params.get("canceled"))
                _set_request_duration(
                    row, request_timestamps.get(request_id), params.get("timestamp")
                )
        elif method in {"Runtime.consoleAPICalled", "Runtime.exceptionThrown", "Log.entryAdded"}:
            if len(requests) + len(console) >= limit:
                truncated = True
                continue
            console.append(_console_metadata(str(method), params))
    return {
        "duration_ms": duration,
        "elapsed_ms": int((time.monotonic() - started) * 1_000),
        "requests": requests,
        "console": console,
        "truncated": truncated or connection.events_truncated,
        "redaction": (
            "URL credentials, query values, fragments, headers, request/response "
            "bodies and console values are excluded."
        ),
    }


def _finite_timestamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _set_request_duration(row: dict[str, Any], started: float | None, ended: Any) -> None:
    timestamp = _finite_timestamp(ended)
    if started is not None and timestamp is not None and timestamp >= started:
        row["duration_ms"] = round((timestamp - started) * 1_000, 2)


def _console_metadata(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "Runtime.consoleAPICalled":
        arguments = params.get("args")
        return {
            "level": str(params.get("type") or "log")[:64],
            "argument_count": len(arguments) if isinstance(arguments, list) else 0,
        }
    if method == "Runtime.exceptionThrown":
        detail = params.get("exceptionDetails")
        detail = detail if isinstance(detail, dict) else {}
        return {
            "level": "error",
            "type": "exception",
            "url": public_url(detail.get("url")),
            "line": detail.get("lineNumber"),
        }
    entry = params.get("entry")
    entry = entry if isinstance(entry, dict) else {}
    return {
        "level": str(entry.get("level") or "info")[:64],
        "type": str(entry.get("source") or "")[:64],
        "url": public_url(entry.get("url")),
        "line": entry.get("lineNumber"),
    }


def _public_node(raw: Any, budget: list[int]) -> dict[str, Any]:
    if not isinstance(raw, dict) or budget[0] <= 0:
        return {}
    budget[0] -= 1
    result = {
        "node_id": raw.get("nodeId"),
        "name": str(raw.get("nodeName") or "")[:128],
        "text": str(raw.get("nodeValue") or "")[:256],
    }
    attributes = raw.get("attributes")
    attrs = {}
    if isinstance(attributes, list):
        for index in range(0, min(len(attributes) - 1, 100), 2):
            name = str(attributes[index])
            if name in _PUBLIC_ATTRIBUTES:
                value = attributes[index + 1]
                attrs[name] = public_url(value) if name in {"href", "src"} else str(value)[:256]
    result["attributes"] = attrs
    children = raw.get("children")
    if isinstance(children, list):
        result["children"] = [
            _public_node(child, budget) for child in children[:200] if budget[0] > 0
        ]
    return result
