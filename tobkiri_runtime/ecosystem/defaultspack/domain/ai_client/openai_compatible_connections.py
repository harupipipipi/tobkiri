from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import urllib.parse
import uuid


_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_AUTH_MODES = {"none", "bearer", "api_key_header", "basic"}
_CREDENTIAL_PROVIDER_ID = "openai_compatible"
_CREDENTIAL_MATERIAL_TYPE = "connection_auth"


def connections_path(pack_root: Path | None = None) -> Path:
    root = pack_root or Path(__file__).resolve().parents[3]
    return root / "user_data" / "shared" / "openai_compatible_connections.json"


def list_connections(*, pack_root: Path | None = None) -> list[dict[str, Any]]:
    payload = _read(pack_root)
    return [deepcopy(payload[key]) for key in sorted(payload)]


def get_connection(connection_id: str, *, pack_root: Path | None = None) -> dict[str, Any] | None:
    value = _read(pack_root).get(_normalize_id(connection_id))
    return deepcopy(value) if value else None


def selected_connection_id(*, pack_root: Path | None = None) -> str:
    """Return the persisted active connection ID, if it still exists."""
    records = _read(pack_root)
    selected = _read_selected_connection_id(pack_root)
    if selected in records:
        return selected
    return next(iter(sorted(records)), "")


def selected_connection(*, pack_root: Path | None = None) -> dict[str, Any] | None:
    """Return the active safe connection definition, if configured."""
    connection_id = selected_connection_id(pack_root=pack_root)
    return get_connection(connection_id, pack_root=pack_root) if connection_id else None


def select_connection(connection_id: str, *, pack_root: Path | None = None) -> dict[str, Any]:
    """Select one existing connection for the generic provider runtime."""
    normalized = _normalize_id(connection_id)
    records = _read(pack_root)
    if normalized not in records:
        raise KeyError("OpenAI-compatible connection is unknown")
    _write(records, pack_root, selected_connection_id=normalized)
    return deepcopy(records[normalized])


def save_connection(definition: dict[str, Any], *, pack_root: Path | None = None) -> dict[str, Any]:
    raw_id = str(definition.get("connection_id") or "").strip().lower()
    connection_id = _normalize_id(raw_id) if raw_id else f"connection-{uuid.uuid4().hex[:12]}"
    base_url = _http_url(definition.get("base_url"), field="base_url")
    auth_mode = str(definition.get("auth_mode") or "none").strip().lower()
    if auth_mode not in _AUTH_MODES:
        raise ValueError(f"Unsupported auth mode: {auth_mode}")
    model_list = definition.get("model_list") if isinstance(definition.get("model_list"), dict) else {}
    manual_models = []
    for item in definition.get("manual_models", []):
        if isinstance(item, str) and item.strip() and item.strip() not in manual_models:
            manual_models.append(item.strip())
        elif isinstance(item, dict) and str(item.get("id") or "").strip():
            manual_models.append({
                "id": str(item["id"]).strip(),
                "type": str(item.get("type") or "unknown").strip().lower(),
                "capabilities": dict(item.get("capabilities") or {}),
            })
    record = {
        "schema_version": 1,
        "connection_id": connection_id,
        "label": str(definition.get("label") or connection_id).strip(),
        "base_url": base_url,
        "auth_mode": auth_mode,
        "auth_header": str(definition.get("auth_header") or "X-API-Key").strip(),
        "api_key_env": str(definition.get("api_key_env") or "").strip(),
        "username_env": str(definition.get("username_env") or "").strip(),
        "model_list": {
            "enabled": bool(model_list.get("enabled")),
            "url": _optional_http_url(model_list.get("url")),
            "path": str(model_list.get("path") or "/models").strip(),
            "items_path": str(model_list.get("items_path") or "data").strip(),
            "next_path": str(model_list.get("next_path") or "next").strip(),
            "cursor_param": str(model_list.get("cursor_param") or "cursor").strip(),
            "max_pages": max(1, min(100, int(model_list.get("max_pages") or 20))),
        },
        "manual_models": manual_models,
    }
    # Only environment-variable references are persisted; secret values are rejected.
    forbidden = {"api_key", "token", "password", "authorization", "headers"}
    if forbidden.intersection({str(key).lower() for key in definition}):
        raise ValueError("Connection definitions must not contain secret values or headers")
    records = _read(pack_root)
    records[connection_id] = record
    selected = _read_selected_connection_id(pack_root)
    _write(
        records,
        pack_root,
        selected_connection_id=selected if selected in records else connection_id,
    )
    return deepcopy(record)


def delete_connection(connection_id: str, *, pack_root: Path | None = None) -> bool:
    records = _read(pack_root)
    normalized = _normalize_id(connection_id)
    removed = records.pop(normalized, None) is not None
    if removed:
        selected = _read_selected_connection_id(pack_root)
        _write(
            records,
            pack_root,
            selected_connection_id=selected if selected in records else next(iter(sorted(records)), ""),
        )
        delete_connection_auth(normalized, pack_root=pack_root)
    return removed


def save_connection_auth(
    connection_id: str,
    api_key: str,
    *,
    username: str = "",
    pack_root: Path | None = None,
) -> dict[str, str]:
    """Save one connection's credential in the existing secret store."""
    normalized = _normalize_id(connection_id)
    secret = str(api_key or "")
    if not secret:
        raise ValueError("API key is required")
    from domain.connections.store import save_connection_credential

    result = save_connection_credential(
        _CREDENTIAL_PROVIDER_ID,
        _CREDENTIAL_MATERIAL_TYPE,
        {"api_key": secret, "username": str(username or "")},
        connection_id=normalized,
        pack_root=pack_root,
    )
    reference = result.get("credential_ref")
    return {str(key): str(value) for key, value in reference.items()} if isinstance(reference, dict) else {}


def delete_connection_auth(connection_id: str, *, pack_root: Path | None = None) -> None:
    """Remove one connection credential without touching other connections."""
    from domain.connections.store import delete_connection_credential

    delete_connection_credential(
        _CREDENTIAL_PROVIDER_ID,
        _CREDENTIAL_MATERIAL_TYPE,
        connection_id=_normalize_id(connection_id),
        pack_root=pack_root,
    )


def connection_status(*, pack_root: Path | None = None) -> dict[str, Any]:
    """Return settings-safe connection metadata without credential material."""
    from domain.connections.store import connection_credential_ref

    selected = selected_connection_id(pack_root=pack_root)
    connections = []
    for connection in list_connections(pack_root=pack_root):
        connection_id = str(connection["connection_id"])
        credential_ref = connection_credential_ref(
            _CREDENTIAL_PROVIDER_ID,
            _CREDENTIAL_MATERIAL_TYPE,
            connection_id=connection_id,
            pack_root=pack_root,
        )
        connections.append(
            {
                **connection,
                "selected": connection_id == selected,
                "credential_configured": bool(credential_ref),
                "credential_ref": credential_ref,
            }
        )
    return {"selected_connection_id": selected, "connections": connections}


def resolve_connection_secret(
    connection: dict[str, Any], *, pack_root: Path | None = None
) -> tuple[str, str]:
    """Resolve stored credentials first, retaining environment compatibility."""
    connection_id = str(connection.get("connection_id") or "").strip()
    if connection_id:
        try:
            from domain.connections.store import read_connection_credential

            stored = read_connection_credential(
                _CREDENTIAL_PROVIDER_ID,
                _CREDENTIAL_MATERIAL_TYPE,
                connection_id=_normalize_id(connection_id),
                pack_root=pack_root,
            )
            key = str(stored.get("api_key") or "")
            username = str(stored.get("username") or "")
            if key:
                return key, username
        except Exception:
            pass
    key = os.environ.get(str(connection.get("api_key_env") or ""), "") if connection.get("api_key_env") else ""
    username = os.environ.get(str(connection.get("username_env") or ""), "") if connection.get("username_env") else ""
    return str(key), str(username)


def _normalize_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not _ID.fullmatch(text):
        raise ValueError("connection_id must match [a-z0-9][a-z0-9_-]{0,63}")
    return text


def _http_url(value: Any, *, field: str) -> str:
    """Validate a secret-free absolute HTTP(S) endpoint for persistence."""
    text = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{field} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not embed credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not include query or fragment data")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", "")
    )


def _optional_http_url(value: Any) -> str:
    return _http_url(value, field="model_list.url") if str(value or "").strip() else ""


def _read(pack_root: Path | None) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(connections_path(pack_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = payload.get("connections") if isinstance(payload, dict) else None
    return {str(key): dict(value) for key, value in records.items() if isinstance(value, dict)} if isinstance(records, dict) else {}


def _read_selected_connection_id(pack_root: Path | None) -> str:
    try:
        payload = json.loads(connections_path(pack_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    raw = payload.get("selected_connection_id") if isinstance(payload, dict) else ""
    try:
        return _normalize_id(raw) if raw else ""
    except ValueError:
        return ""


def _write(
    records: dict[str, dict[str, Any]],
    pack_root: Path | None,
    *,
    selected_connection_id: str = "",
) -> None:
    path = connections_path(pack_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "schema_version": 1,
            "selected_connection_id": selected_connection_id,
            "connections": records,
        },
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
