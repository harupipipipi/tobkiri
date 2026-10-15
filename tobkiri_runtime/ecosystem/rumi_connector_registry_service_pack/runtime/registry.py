"""Profile-scoped connector metadata without secret or transport ownership."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

AUTHORITY = "rumi.service.host.authorize.v1"
SERVICE_PACK_ID = "rumi_connector_registry_service_pack"
VERSION = "rumi.connector-registry.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_KEYS = {
    "access_token",
    "api_key",
    "client_secret",
    "password",
    "refresh_token",
    "secret",
    "signing_secret",
    "token",
}


class ConnectorConflict(RuntimeError):
    """Raised when connector state changed since it was authorized."""


class ConnectorRegistry:
    """Own connector instances and opaque credential references."""

    def __init__(self, profile_id: str, *, root: Path | None = None) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(root or USER_DATA_DIR)
            / "packs"
            / SERVICE_PACK_ID
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "connectors.json"
        self.lock_root = self.root / "locks"

    def snapshot(self) -> dict[str, Any]:
        """Return all connector metadata without resolving credentials."""

        state = self._read()
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "connectors": [
                state["connectors"][key] for key in sorted(state["connectors"])
            ],
        }

    def get(self, connector_id: str) -> dict[str, Any] | None:
        """Return one connector instance."""

        value = self._read()["connectors"].get(_identifier(connector_id))
        return _copy(value) if isinstance(value, Mapping) else None

    def apply(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one exact revision-bound registry transition."""

        with NamedLock(self.lock_root, "connectors"):
            state = self._read()
            _assert_revision(state, int(arguments["expected_revision"]))
            connector_id = _identifier(arguments["connector_id"])
            now_ms = int(time.time() * 1000)
            if name == "register":
                if connector_id in state["connectors"]:
                    raise ValueError("connector already exists")
                record = {
                    "id": connector_id,
                    "adapter_id": _identifier(arguments["adapter_id"]),
                    "display_name": str(arguments["display_name"])[:120],
                    "credential_ref": str(arguments["credential_ref"])[:255],
                    "config": _copy(arguments["config"]),
                    "enabled": bool(arguments["enabled"]),
                    "created_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                }
                state["connectors"][connector_id] = record
            else:
                record = state["connectors"].get(connector_id)
                if record is None:
                    raise KeyError("connector is unknown")
                if name == "remove":
                    del state["connectors"][connector_id]
                    state["revision"] += 1
                    self._write(state)
                    return {"removed": connector_id, "revision": state["revision"]}
                if name == "update":
                    for key, value in arguments["updates"].items():
                        record[key] = _copy(value)
                elif name in {"enable", "disable"}:
                    record["enabled"] = name == "enable"
                else:
                    raise ValueError(f"unknown connector action: {name}")
                record["updated_at_ms"] = now_ms
            state["revision"] += 1
            self._write(state)
            return {"connector": _copy(record), "revision": state["revision"]}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": VERSION, "revision": 0, "connectors": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("version") != VERSION:
            raise ValueError("connector registry state is invalid")
        connectors = value.get("connectors")
        if not isinstance(connectors, Mapping):
            raise ValueError("connector registry records are invalid")
        return {
            "version": VERSION,
            "revision": max(0, int(value.get("revision") or 0)),
            "connectors": _copy(connectors),
        }

    def _write(self, value: Mapping[str, Any]) -> None:
        _atomic_json(self.path, value)


def create_connector_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create connector registry read operations."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        registry = ConnectorRegistry(_profile(payload))
        if name == "list":
            return registry.snapshot()
        if name == "get":
            return registry.get(str(payload.get("connector_id") or ""))
        raise ValueError(f"unknown connector resource operation: {name}")

    return operation


def create_connector_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated connector registry transitions."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        arguments = _arguments(name, payload)
        _redeem(client, payload, name, arguments)
        return ConnectorRegistry(_profile(payload)).apply(name, arguments)

    return operation


def _arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if name not in {"register", "update", "remove", "enable", "disable"}:
        raise ValueError(f"unknown connector action: {name}")
    arguments: dict[str, Any] = {
        "connector_id": str(payload.get("connector_id") or ""),
        "expected_revision": max(0, int(payload.get("expected_revision") or 0)),
    }
    if name == "register":
        arguments.update(
            {
                "adapter_id": str(payload.get("adapter_id") or ""),
                "display_name": str(payload.get("display_name") or "Connector"),
                "credential_ref": str(payload.get("credential_ref") or ""),
                "config": dict(_mapping(payload.get("config"))),
                "enabled": bool(payload.get("enabled", True)),
            }
        )
    elif name == "update":
        arguments["updates"] = _updates(payload.get("updates"))
    return arguments


def _redeem(
    client: Any,
    payload: Mapping[str, Any],
    name: str,
    arguments: Mapping[str, Any],
) -> None:
    result = client.invoke(
        AUTHORITY,
        "redeem",
        {
            "receipt": str(payload.get("authority_receipt") or ""),
            "service_pack_id": SERVICE_PACK_ID,
            "operation": f"connector.registry.{name}",
            "authority": "connector.registry.manage",
            "caller_id": str(payload.get("caller_id") or ""),
            "caller_pack_id": str(payload.get("caller_pack_id") or ""),
            "caller_function_id": str(payload.get("caller_function_id") or ""),
            "profile_id": _profile(payload),
            "workspace_id": "",
            "session_id": str(payload.get("session_id") or ""),
            "arguments": dict(arguments),
        },
    )
    if not result.get("authorized"):
        raise PermissionError(str(result.get("reason") or "connector authority denied"))


def _updates(value: Any) -> dict[str, Any]:
    updates = dict(_mapping(value))
    allowed = {"display_name", "credential_ref", "config"}
    if set(updates) - allowed:
        raise ValueError("connector update contains unsupported fields")
    if "display_name" in updates:
        updates["display_name"] = str(updates["display_name"])[:120]
    if "credential_ref" in updates:
        updates["credential_ref"] = str(updates["credential_ref"])[:255]
    if "config" in updates:
        updates["config"] = dict(_mapping(updates["config"]))
    return updates


def _identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError("connector identifier is invalid")
    return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("connector config must be an object")
    if _contains_secret_key(value):
        raise PermissionError(
            "connector config may contain only an opaque credential_ref"
        )
    return value


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in _SECRET_KEYS or _contains_secret_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_key(item) for item in value)
    return False


def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise ConnectorConflict("connector registry revision is stale")


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".connector-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

