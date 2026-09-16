from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
_LOCK = threading.RLock()
_REPLACE_RETRY_ATTEMPTS = 8


def _pack_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_chat_dir() -> Path:
    override = os.environ.get("RUMI_DEFAULTSPACK_CHAT_STORE_PATH")
    if override:
        return Path(override).parent
    user_data = os.environ.get("RUMI_USER_DATA")
    if user_data:
        return Path(user_data) / "defaultspack" / "shared" / "chat"
    return _pack_root() / "user_data" / "shared" / "chat"


def default_approval_state_path() -> Path:
    return default_chat_dir() / "approval_state.json"


def _conversation_state_path(conversation_id: str) -> Path | None:
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return None
    if "/" in conversation_id or "\\" in conversation_id or conversation_id in {".", ".."}:
        return None
    root = default_chat_dir() / "conversations"
    return root / conversation_id / "approval_state.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix="." + path.name + ".",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_atomic_file(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _is_transient_replace_error(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    errno_value = getattr(exc, "errno", None)
    if isinstance(exc, PermissionError):
        return True
    if winerror in {5, 32}:
        return True
    if errno_value in {errno.EACCES, errno.EBUSY, errno.EPERM}:
        return True
    message = str(exc).lower()
    return "access is denied" in message or "permission denied" in message


def _replace_atomic_file(tmp_path: Path, path: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(_REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(tmp_path, path)
            return
        except OSError as exc:
            last_error = exc
            if not _is_transient_replace_error(exc) or attempt >= _REPLACE_RETRY_ATTEMPTS - 1:
                break
            time.sleep(min(0.05 * (2 ** attempt), 0.5))
    if last_error is not None and _is_transient_replace_error(last_error):
        raise PermissionError("approval state file is temporarily locked; retry later") from None
    if last_error is not None:
        raise last_error


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_request(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    request_id = str(value.get("request_id") or "").strip()
    if not request_id:
        return None
    details = value.get("details")
    if not isinstance(details, dict):
        details = {}
    decision_at = value.get("decision_at")
    payload = {
        "request_id": request_id,
        "operation": str(value.get("operation") or ""),
        "risk_level": str(value.get("risk_level") or "high"),
        "args_hash": str(value.get("args_hash") or ""),
        "details": dict(details),
        "created_at": _coerce_int(value.get("created_at")),
        "expires_at": _coerce_int(value.get("expires_at")),
        "status": str(value.get("status") or "pending"),
        "decision_at": None if decision_at is None else _coerce_int(decision_at),
    }
    if value.get("display_summary") is not None:
        payload["display_summary"] = str(value.get("display_summary"))
    return payload


def _requests_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_requests = payload
    elif isinstance(payload, dict):
        raw_requests = payload.get("requests")
        if raw_requests is None:
            raw_requests = payload.get("approval_requests")
        if isinstance(raw_requests, dict):
            raw_requests = list(raw_requests.values())
    else:
        raw_requests = None
    if not isinstance(raw_requests, list):
        return []
    requests: list[dict[str, Any]] = []
    for item in raw_requests:
        request = normalize_request(item)
        if request is not None:
            requests.append(request)
    return requests


def _prefer_newer(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    existing_time = _coerce_int(existing.get("decision_at") or existing.get("created_at"))
    candidate_time = _coerce_int(candidate.get("decision_at") or candidate.get("created_at"))
    if candidate_time >= existing_time:
        return candidate
    return existing


def _merge_json_requests(requests: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for request in requests:
        normalized = normalize_request(request)
        if normalized is None:
            continue
        request_id = normalized["request_id"]
        if request_id in merged:
            merged[request_id] = _prefer_newer(merged[request_id], normalized)
        else:
            merged[request_id] = normalized
    return sorted(
        merged.values(),
        key=lambda item: (_coerce_int(item.get("created_at")), str(item.get("request_id") or "")),
        reverse=True,
    )


def load_approval_state_requests() -> list[dict[str, Any]]:
    with _LOCK:
        requests: list[dict[str, Any]] = []
        requests.extend(_requests_from_payload(_read_json(default_approval_state_path())))
        conversations_root = default_chat_dir() / "conversations"
        try:
            conversation_paths = sorted(conversations_root.glob("*/approval_state.json"))
        except OSError:
            conversation_paths = []
        for path in conversation_paths:
            requests.extend(_requests_from_payload(_read_json(path)))
        return _merge_json_requests(requests)


def refresh_approval_state_mirrors(
    requests: Iterable[dict[str, Any]],
    *,
    preserve_json_only: bool = True,
) -> None:
    with _LOCK:
        merged: dict[str, dict[str, Any]] = {}
        if preserve_json_only:
            for request in load_approval_state_requests():
                merged[request["request_id"]] = request
        for request in requests:
            normalized = normalize_request(request)
            if normalized is not None:
                merged[normalized["request_id"]] = normalized

        ordered = sorted(
            merged.values(),
            key=lambda item: (_coerce_int(item.get("created_at")), str(item.get("request_id") or "")),
            reverse=True,
        )
        _write_state(default_approval_state_path(), ordered)
        _write_conversation_states(ordered)


def clear_approval_state_mirrors() -> None:
    with _LOCK:
        _write_state(default_approval_state_path(), [])
        conversations_root = default_chat_dir() / "conversations"
        try:
            paths = sorted(conversations_root.glob("*/approval_state.json"))
        except OSError:
            paths = []
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _write_state(path: Path, requests: list[dict[str, Any]]) -> None:
    _atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "updated_at": int(time.time()),
            "requests": requests,
        },
    )


def _write_conversation_states(requests: list[dict[str, Any]]) -> None:
    by_conversation: dict[str, list[dict[str, Any]]] = {}
    for request in requests:
        details = request.get("details")
        if not isinstance(details, dict):
            continue
        conversation_id = str(details.get("conversation_id") or "").strip()
        if not conversation_id:
            continue
        by_conversation.setdefault(conversation_id, []).append(request)

    conversations_root = default_chat_dir() / "conversations"
    try:
        existing_paths = set(conversations_root.glob("*/approval_state.json"))
    except OSError:
        existing_paths = set()

    written_paths: set[Path] = set()
    for conversation_id, items in by_conversation.items():
        path = _conversation_state_path(conversation_id)
        if path is None:
            continue
        written_paths.add(path)
        _write_state(path, items)

    for path in existing_paths - written_paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
