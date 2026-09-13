"""Verified vendor normalization, deduplication, and route fan-out."""

from __future__ import annotations

import hashlib
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

REGISTRY = "rumi.resource.connector.registry.v1"
VENDOR_ADAPTER = "rumi.service.connector.adapter.v1"
ROUTE_ADAPTER = "rumi.action.connector.route.v1"
SERVICE_PACK_ID = "rumi_connector_inbound_broker_pack"
VERSION = "rumi.connector-inbound-ledger.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_BODY = 2 * 1024 * 1024
_MAX_SKEW_MS = 5 * 60 * 1000


class InboundBroker:
    """Normalize one verified event and fan out data-only route projections."""

    def __init__(self, client: Any, profile_id: str) -> None:
        self.client = client
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(USER_DATA_DIR)
            / "packs"
            / SERVICE_PACK_ID
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "inbound-ledger.json"
        self.lock_root = self.root / "locks"

    def receive(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Verify, normalize, deduplicate, and route one inbound request."""

        connector_id = _identifier(payload.get("connector_id"), "connector_id")
        raw_body = _body(payload.get("body"))
        received_at_ms = max(0, int(payload.get("received_at_ms") or _now_ms()))
        if abs(_now_ms() - received_at_ms) > _MAX_SKEW_MS:
            raise PermissionError("connector request timestamp is outside replay window")
        connector = self.client.invoke(
            REGISTRY,
            "get",
            {"profile_id": self.profile_id, "connector_id": connector_id},
        )
        if not isinstance(connector, Mapping) or not connector.get("enabled"):
            raise PermissionError("connector is unknown or disabled")
        adapter_id = str(connector.get("adapter_id") or "")
        provider = _provider(self.client.providers(VENDOR_ADAPTER), adapter_id)
        normalized = self.client.invoke(
            VENDOR_ADAPTER,
            "verify_normalize",
            {
                "profile_id": self.profile_id,
                "connector": dict(connector),
                "headers": _headers(payload.get("headers")),
                "body": raw_body.decode("utf-8", errors="strict"),
                "received_at_ms": received_at_ms,
                "request_id": str(payload.get("request_id") or ""),
            },
            provider_instance_id=str(provider["provider_instance_id"]),
        )
        event = _event(normalized, connector_id, adapter_id, received_at_ms)
        event_id = _identifier(event["event_id"], "event_id")
        body_hash = hashlib.sha256(raw_body).hexdigest()
        duplicate = self._record(event_id, body_hash, event)
        if duplicate is not None:
            return duplicate
        routes = []
        for route in self.client.providers(ROUTE_ADAPTER):
            try:
                result = self.client.invoke(
                    ROUTE_ADAPTER,
                    "route",
                    {
                        "profile_id": self.profile_id,
                        "connector_id": connector_id,
                        "connector": _route_connector(connector),
                        "event": event,
                    },
                    provider_instance_id=str(route["provider_instance_id"]),
                )
                routes.append(
                    {
                        "provider_instance_id": route["provider_instance_id"],
                        "status": "ok",
                        "result": _bounded(result),
                    }
                )
            except Exception as exc:
                routes.append(
                    {
                        "provider_instance_id": route["provider_instance_id"],
                        "status": "failed",
                        "error": str(exc)[:1000],
                    }
                )
        self._finish(event_id, routes)
        return {
            "status": "accepted",
            "event": event,
            "routes": routes,
            "deduplicated": False,
        }

    def _record(
        self,
        event_id: str,
        body_hash: str,
        event: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        with NamedLock(self.lock_root, "inbound"):
            state = self._read()
            current = state["events"].get(event_id)
            if current is not None:
                if current["body_hash"] != body_hash:
                    raise PermissionError("connector event replay payload does not match")
                return {
                    "status": "accepted",
                    "event": _copy(current["event"]),
                    "routes": _copy(current.get("routes") or []),
                    "deduplicated": True,
                }
            state["events"][event_id] = {
                "event_id": event_id,
                "body_hash": body_hash,
                "event": _ledger_event(event),
                "routes": [],
                "created_at_ms": _now_ms(),
                "updated_at_ms": _now_ms(),
            }
            _prune(state["events"])
            self._write(state)
        return None

    def _finish(self, event_id: str, routes: list[dict[str, Any]]) -> None:
        with NamedLock(self.lock_root, "inbound"):
            state = self._read()
            current = state["events"].get(event_id)
            if current is None:
                raise RuntimeError("connector inbound ledger lost event")
            current["routes"] = _copy(routes)
            current["updated_at_ms"] = _now_ms()
            self._write(state)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": VERSION, "events": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("version") != VERSION:
            raise ValueError("connector inbound ledger is invalid")
        events = value.get("events")
        if not isinstance(events, Mapping):
            raise ValueError("connector inbound events are invalid")
        return {"version": VERSION, "events": _copy(events)}

    def _write(self, value: Mapping[str, Any]) -> None:
        _atomic_json(self.path, value)


def create_inbound_transport(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create connector inbound transport operations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "receive":
            raise ValueError(f"unknown connector inbound operation: {name}")
        return InboundBroker(
            client,
            str(payload.get("profile_id") or "default"),
        ).receive(payload)

    return operation


def _provider(
    providers: tuple[dict[str, Any], ...],
    instance_key: str,
) -> Mapping[str, Any]:
    matches = [
        item for item in providers if str(item.get("instance_key") or "") == instance_key
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one selected connector adapter for {instance_key}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _event(
    value: Any,
    connector_id: str,
    adapter_id: str,
    received_at_ms: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("connector adapter returned an invalid event")
    event = dict(value)
    event["event_id"] = _identifier(event.get("event_id"), "event_id")
    event["connector_id"] = connector_id
    event["adapter_id"] = adapter_id
    event["received_at_ms"] = received_at_ms
    event.pop("credential", None)
    event.pop("secret", None)
    return _bounded(event)


def _route_connector(value: Mapping[str, Any]) -> dict[str, Any]:
    """Expose public routing metadata without any credential reference."""

    return {
        "id": str(value.get("id") or ""),
        "adapter_id": str(value.get("adapter_id") or ""),
        "display_name": str(value.get("display_name") or ""),
        "config": _ledger_event(value.get("config") or {}),
        "enabled": bool(value.get("enabled")),
    }


def _body(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("connector body must be UTF-8 text")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_BODY:
        raise ValueError("connector body exceeds size limit")
    return encoded


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key).casefold(): str(item)[:8192]
        for key, item in value.items()
        if len(str(key)) <= 128
    }


def _identifier(value: Any, name: str) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError(f"{name} is invalid")
    return identifier


def _bounded(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise ValueError("connector normalized event exceeds size limit")
    return json.loads(encoded)


def _ledger_event(value: Any) -> Any:
    secret_parts = ("credential", "oauth", "password", "secret", "signature", "token")
    if isinstance(value, Mapping):
        return {
            str(key): _ledger_event(item)
            for key, item in value.items()
            if not any(part in str(key).casefold() for part in secret_parts)
        }
    if isinstance(value, list):
        return [_ledger_event(item) for item in value]
    return _copy(value)


def _prune(events: dict[str, Any]) -> None:
    if len(events) <= 10_000:
        return
    ordered = sorted(events.values(), key=lambda item: int(item["updated_at_ms"]))
    for item in ordered[: len(events) - 10_000]:
        events.pop(str(item["event_id"]), None)


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".inbound-", suffix=".tmp")
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

