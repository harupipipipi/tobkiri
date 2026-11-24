"""Bounded, content-free metadata for completed Host file mutations.

This module never reads a file or grants authority. Callers supply the exact
preimage and published bytes already held by an approved mutation owner.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import PurePosixPath
import re
from threading import Lock
import time
from typing import Any, Mapping

MAX_SAFE_INTEGER = 2**53 - 1
MAX_DIFF_BYTES = 262_144
MAX_DIFF_LINES = 10_000
MAX_DIFF_PRODUCT = 1_000_000
_ORDER_LOCK = Lock()
_LAST_ORDER = 0
_OPERATIONS = frozenset({"create", "write", "patch", "delete", "move"})
_REASONS = frozenset({"unknown", "binary", "too_large", "sensitive", "unsupported_encoding"})
_FIELDS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "file_id",
        "status",
        "operation",
        "profile_id",
        "workspace_id",
        "root_id",
        "frame_id",
        "path",
        "previous_path",
        "occurred_at_ms",
        "sequence",
        "version",
        "stats",
    }
)
_UNSUCCESSFUL = frozenset(
    {
        "failed",
        "error",
        "failure",
        "denied",
        "rejected",
        "cancelled",
        "canceled",
        "pending",
        "running",
        "queued",
        "started",
        "approval_required",
        "pending_approval",
        "requires_approval",
        "ambiguous",
        "stale",
    }
)
_SENSITIVE = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:sk-(?:or-v1-)?|gh[pousr]_)[A-Za-z0-9_-]{16,}|"
    r"(?:api[_-]?key|secret|password|access[_-]?token)\s*[=:]\s*"
    r"[\"']?[^\s\"']{8,}",
    re.IGNORECASE,
)


def line_diff_stats(
    before: bytes | None,
    after: bytes | None,
    *,
    preimage_known: bool = True,
    encoding: str = "utf-8",
    sensitive: bool = False,
) -> dict[str, Any]:
    """Count actual changed text lines, or explicitly explain unavailable stats.

    None denotes a known absent file; unknown preimages require the explicit
    preimage_known=False flag. No content, diff, length or hash is returned.
    """

    def unavailable(reason: str) -> dict[str, Any]:
        return {"status": "unavailable", "reason": reason}

    if not preimage_known:
        return unavailable("unknown")
    if sensitive:
        return unavailable("sensitive")
    if encoding.lower().replace("_", "-") not in {"utf-8", "utf8"}:
        return unavailable("unsupported_encoding")
    values = [value if value is not None else b"" for value in (before, after)]
    if any(not isinstance(value, bytes) for value in values):
        return unavailable("unknown")
    if any(len(value) > MAX_DIFF_BYTES for value in values):
        return unavailable("too_large")
    if any(b"\x00" in value for value in values):
        return unavailable("binary")
    try:
        text = [value.decode("utf-8", errors="strict") for value in values]
    except UnicodeDecodeError:
        return unavailable("binary")
    if any(_SENSITIVE.search(value) for value in text):
        return unavailable("sensitive")
    lines = [value.splitlines(keepends=True) for value in text]
    if (
        any(len(value) > MAX_DIFF_LINES for value in lines)
        or len(lines[0]) * len(lines[1]) > MAX_DIFF_PRODUCT
    ):
        return unavailable("too_large")
    added = deleted = 0
    matcher = difflib.SequenceMatcher(a=lines[0], b=lines[1], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            deleted += i2 - i1
        if tag in {"replace", "insert"}:
            added += j2 - j1
    return {"status": "available", "lines_added": added, "lines_deleted": deleted}


def _opaque(prefix: str, *values: object) -> str:
    encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return prefix + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 1024
        and not PurePosixPath(value).is_absolute()
        and re.match(r"^[A-Za-z]:", value) is None
        and "\\" not in value
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        and not any(part in {"", ".", ".."} for part in value.split("/"))
    )


def _string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 1024
        and value == value.strip()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _integer(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_SAFE_INTEGER


def validate_file_edit_receipt(value: Any) -> dict[str, Any] | None:
    """Accept exactly the public receipt schema, without trusting arbitrary data."""
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        return None
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["status"] != "committed"
        or not isinstance(value["operation"], str)
        or value["operation"] not in _OPERATIONS
        or not all(
            _string(value[key])
            for key in (
                "receipt_id",
                "file_id",
                "profile_id",
                "workspace_id",
                "root_id",
                "version",
            )
        )
        or re.fullmatch(r"file-edit:[A-Za-z0-9._~-]+", value["receipt_id"]) is None
        or not _path(value["path"])
        or not all(_integer(value[key]) for key in ("occurred_at_ms", "sequence"))
        or (value["frame_id"] is not None and not _string(value["frame_id"]))
    ):
        return None
    previous = value["previous_path"]
    if value["operation"] == "move":
        if not _path(previous) or previous == value["path"]:
            return None
    elif previous is not None:
        return None
    stats = value["stats"]
    if not isinstance(stats, Mapping):
        return None
    if stats.get("status") == "available":
        if set(stats) != {"status", "lines_added", "lines_deleted"} or not all(
            _integer(stats[key]) for key in ("lines_added", "lines_deleted")
        ):
            return None
    elif stats.get("status") == "unavailable":
        if (
            set(stats) != {"status", "reason"}
            or not isinstance(stats["reason"], str)
            or stats["reason"] not in _REASONS
        ):
            return None
    else:
        return None
    return {**dict(value), "stats": dict(stats)}


def committed_file_edit_receipt(
    *,
    mutation_id: str,
    operation: str,
    profile_id: str,
    workspace_id: str,
    root_identity: tuple[object, ...],
    path: str,
    before: bytes | None,
    after: bytes | None,
    frame_identity: str | None = None,
    previous_path: str | None = None,
    preimage_known: bool = True,
    encoding: str = "utf-8",
    sensitive: bool = False,
) -> dict[str, Any]:
    """Create metadata only after the owner successfully publishes exact bytes.

    The owner must withhold this record until Broker audit and terminal success;
    this pure helper cannot prove success or redeem an approval itself.
    """
    if _SENSITIVE.search(path) or (previous_path is not None and _SENSITIVE.search(previous_path)):
        raise ValueError("file edit metadata is unavailable")
    global _LAST_ORDER
    now_us = time.time_ns() // 1000
    with _ORDER_LOCK:
        order = max(now_us, _LAST_ORDER + 1)
        _LAST_ORDER = order
    root_id = _opaque("root:", profile_id, workspace_id, *root_identity)
    receipt_id = _opaque("file-edit:", profile_id, workspace_id, mutation_id)
    value = {
        "schema_version": 1,
        "receipt_id": receipt_id,
        "file_id": _opaque("file:", root_id, previous_path or path),
        "status": "committed",
        "operation": operation,
        "profile_id": profile_id,
        "workspace_id": workspace_id,
        "root_id": root_id,
        "frame_id": _opaque("frame:", frame_identity) if frame_identity else None,
        "path": path,
        "previous_path": previous_path,
        "occurred_at_ms": now_us // 1000,
        "sequence": order,
        "version": receipt_id,
        "stats": line_diff_stats(
            before,
            after,
            preimage_known=preimage_known,
            encoding=encoding,
            sensitive=sensitive,
        ),
    }
    validated = validate_file_edit_receipt(value)
    if validated is None:
        raise ValueError("file edit metadata is invalid")
    return validated


def file_edit_receipt_from_tool_result(tool_name: str, result: Any) -> dict[str, Any] | None:
    """Extract an explicit owner receipt through only finite success wrappers.

    No receipt is inferred from a path, byte count, model text or unified diff.
    Currently only the native-approved finite create tool produces this schema.
    """
    if not isinstance(tool_name, str) or tool_name not in {
        "coding_file_create",
        "rumi_default_tools_pack:coding_file_create",
    }:
        return None
    if not isinstance(tool_name, str):
        return None

    def visit(value: Any, depth: int) -> dict[str, Any] | None:
        if depth > 5:
            return None
        if isinstance(value, str):
            if len(value) > 16_384:
                return None
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return None
        if not isinstance(value, Mapping):
            return None
        if (
            any(
                value.get(key) is True
                for key in (
                    "is_error",
                    "approval_required",
                    "requires_approval",
                    "cancelled",
                )
            )
            or any(value.get(key) is False for key in ("success", "ok"))
            or any(
                isinstance(value.get(key), str) and value[key].strip().lower() in _UNSUCCESSFUL
                for key in ("status", "state", "phase", "outcome")
            )
            or ("error" in value and value["error"] is not None)
        ):
            return None
        if "file_edit_receipt" in value:
            receipt = validate_file_edit_receipt(value["file_edit_receipt"])
            if (
                receipt is None
                or receipt["operation"] != "create"
                or value.get("created") is not True
                or value.get("path") != receipt["path"]
                or value.get("workspace_id") != receipt["workspace_id"]
            ):
                return None
            return receipt
        results = []
        for key in ("data", "result"):
            if key not in value:
                continue
            receipt = visit(value[key], depth + 1)
            if receipt is None:
                return None
            results.append(receipt)
        if not results or any(item != results[0] for item in results):
            return None
        return results[0]

    return visit(result, 0)
