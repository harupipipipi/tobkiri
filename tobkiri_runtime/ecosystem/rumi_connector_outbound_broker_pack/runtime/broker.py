"""Receipt-gated outbound delivery with dedupe, retry, and cancellation."""

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

AUTHORITY = "rumi.service.host.authorize.v1"
REGISTRY = "rumi.resource.connector.registry.v1"
VENDOR_ADAPTER = "rumi.service.connector.adapter.v1"
SERVICE_PACK_ID = "rumi_connector_outbound_broker_pack"
VERSION = "rumi.connector-outbound-ledger.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_MESSAGE = 256 * 1024
_MAX_ATTEMPTS = 5


class OutboundBroker:
    """Own delivery lifecycle while vendors own network execution."""

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
        self.path = self.root / "outbound-ledger.json"
        self.lock_root = self.root / "locks"

    def read(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Read redacted delivery status."""

        if name != "status":
            raise ValueError(f"unknown connector outbound resource operation: {name}")
        delivery_id = _identifier(payload.get("delivery_id"), "delivery_id")
        value = self._read()["deliveries"].get(delivery_id)
        return _public(value) if isinstance(value, Mapping) else {"status": "unknown"}

    def control(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one receipt-gated send, retry, or cancel transition."""

        if name == "send":
            arguments = {
                "connector_id": _identifier(payload.get("connector_id"), "connector_id"),
                "delivery_id": _identifier(payload.get("delivery_id"), "delivery_id"),
                "message": _message(payload.get("message")),
            }
        elif name in {"retry", "cancel"}:
            arguments = {
                "delivery_id": _identifier(payload.get("delivery_id"), "delivery_id")
            }
        else:
            raise ValueError(f"unknown connector outbound action: {name}")
        self._redeem(payload, name, arguments)
        if name == "send":
            return self._send(arguments)
        if name == "retry":
            return self._retry(arguments["delivery_id"])
        return self._cancel(arguments["delivery_id"])

    def _send(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        delivery_id = str(arguments["delivery_id"])
        digest = _digest(
            {
                "connector_id": arguments["connector_id"],
                "message": arguments["message"],
            }
        )
        with NamedLock(self.lock_root, "outbound"):
            state = self._read()
            current = state["deliveries"].get(delivery_id)
            if current is not None:
                if current["payload_hash"] != digest:
                    raise PermissionError("delivery ID payload does not match")
                return {**_public(current), "deduplicated": True}
            record = {
                "delivery_id": delivery_id,
                "connector_id": arguments["connector_id"],
                "message": _copy(arguments["message"]),
                "payload_hash": digest,
                "status": "pending",
                "attempt": 0,
                "next_attempt_at_ms": 0,
                "last_error": "",
                "result": None,
                "created_at_ms": _now_ms(),
                "updated_at_ms": _now_ms(),
            }
            state["deliveries"][delivery_id] = record
            _prune(state["deliveries"])
            self._write(state)
        return self._deliver(record)

    def _retry(self, delivery_id: str) -> dict[str, Any]:
        record = self._required(delivery_id)
        if record["status"] == "cancelled":
            raise RuntimeError("cancelled delivery cannot be retried")
        if record["status"] == "sending":
            raise RuntimeError("delivery already has an active attempt")
        if record["status"] == "delivered":
            return {**_public(record), "deduplicated": True}
        if int(record["attempt"]) >= _MAX_ATTEMPTS:
            raise RuntimeError("delivery retry limit reached")
        return self._deliver(record)

    def _cancel(self, delivery_id: str) -> dict[str, Any]:
        with NamedLock(self.lock_root, "outbound"):
            state = self._read()
            record = state["deliveries"].get(delivery_id)
            if record is None:
                raise KeyError("delivery is unknown")
            if record["status"] == "delivered":
                raise RuntimeError("delivered message cannot be cancelled")
            if record["status"] == "sending":
                raise RuntimeError("in-flight delivery cannot be cancelled safely")
            record["status"] = "cancelled"
            record["message"] = None
            record["updated_at_ms"] = _now_ms()
            self._write(state)
        return _public(record)

    def _deliver(self, record: dict[str, Any]) -> dict[str, Any]:
        connector, revision = self._connector(str(record["connector_id"]))
        adapter_id = str(connector.get("adapter_id") or "")
        provider = _provider(self.client.providers(VENDOR_ADAPTER), adapter_id)
        downstream_arguments = {
            "connector": dict(connector),
            "registry_revision": revision,
            "delivery_id": str(record["delivery_id"]),
            "message": _copy(record["message"]),
        }
        issued = self.client.invoke(
            AUTHORITY,
            "authorize",
            {
                "service_pack_id": str(provider["source_pack_id"]),
                "operation": "connector.adapter.deliver",
                "authority": "connector.delivery.execute",
                "caller_id": "connector.outbound.broker",
                "caller_pack_id": SERVICE_PACK_ID,
                "caller_function_id": "connector.outbound.send",
                "profile_id": self.profile_id,
                "workspace_id": "",
                "session_id": "",
                "arguments": downstream_arguments,
                "approval_required": False,
            },
        )
        if not issued.get("authorized"):
            raise PermissionError(str(issued.get("reason") or "delivery denied"))
        current, current_revision = self._connector(str(record["connector_id"]))
        if current_revision != revision or _digest(current) != _digest(connector):
            raise RuntimeError("connector registry changed before delivery")
        record = self._claim_attempt(record)
        try:
            result = self.client.invoke(
                VENDOR_ADAPTER,
                "deliver",
                {
                    **downstream_arguments,
                    "profile_id": self.profile_id,
                    "authority_receipt": str(issued.get("receipt") or ""),
                    "caller_id": "connector.outbound.broker",
                    "caller_pack_id": SERVICE_PACK_ID,
                    "caller_function_id": "connector.outbound.send",
                    "session_id": "",
                },
                provider_instance_id=str(provider["provider_instance_id"]),
            )
            succeeded = isinstance(result, Mapping) and result.get("status") in {
                "ok",
                "accepted",
                "delivered",
            }
            record["status"] = "delivered" if succeeded else "retry_scheduled"
            record["last_error"] = "" if succeeded else _safe_error(result)
            record["result"] = _bounded_result(result)
        except Exception as exc:
            record["status"] = "retry_scheduled"
            record["last_error"] = str(exc)[:1000]
            record["result"] = None
        if record["status"] == "retry_scheduled":
            record["next_attempt_at_ms"] = _now_ms() + _retry_delay(record["attempt"])
            if int(record["attempt"]) >= _MAX_ATTEMPTS:
                record["status"] = "failed"
                record["message"] = None
        else:
            record["next_attempt_at_ms"] = 0
            record["message"] = None
        self._save_record(record)
        return _public(record)

    def _connector(self, connector_id: str) -> tuple[dict[str, Any], int]:
        state = self.client.invoke(
            REGISTRY,
            "list",
            {"profile_id": self.profile_id},
        )
        matches = [
            item
            for item in state.get("connectors") or []
            if str(item.get("id") or "") == connector_id
        ]
        if len(matches) != 1 or not matches[0].get("enabled"):
            raise PermissionError("connector is unknown or disabled")
        return dict(matches[0]), int(state.get("revision") or 0)

    def _required(self, delivery_id: str) -> dict[str, Any]:
        value = self._read()["deliveries"].get(delivery_id)
        if not isinstance(value, Mapping):
            raise KeyError("delivery is unknown")
        return dict(value)

    def _claim_attempt(self, record: Mapping[str, Any]) -> dict[str, Any]:
        with NamedLock(self.lock_root, "outbound"):
            state = self._read()
            current = state["deliveries"].get(str(record["delivery_id"]))
            if current is None or current["payload_hash"] != record["payload_hash"]:
                raise RuntimeError("delivery ledger changed during execution")
            if current["status"] in {"cancelled", "delivered", "sending"}:
                raise RuntimeError("delivery is not claimable")
            current["attempt"] = int(current["attempt"]) + 1
            current["status"] = "sending"
            current["updated_at_ms"] = _now_ms()
            self._write(state)
            return dict(current)

    def _save_record(self, record: Mapping[str, Any]) -> None:
        with NamedLock(self.lock_root, "outbound"):
            state = self._read()
            current = state["deliveries"].get(str(record["delivery_id"]))
            if current is None or current["payload_hash"] != record["payload_hash"]:
                raise RuntimeError("delivery ledger changed during execution")
            value = _copy(record)
            value["updated_at_ms"] = _now_ms()
            state["deliveries"][str(record["delivery_id"])] = value
            self._write(state)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": VERSION, "deliveries": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("version") != VERSION:
            raise ValueError("connector outbound ledger is invalid")
        deliveries = value.get("deliveries")
        if not isinstance(deliveries, Mapping):
            raise ValueError("connector outbound deliveries are invalid")
        return {"version": VERSION, "deliveries": _copy(deliveries)}

    def _write(self, value: Mapping[str, Any]) -> None:
        _atomic_json(self.path, value)

    def _redeem(
        self,
        payload: Mapping[str, Any],
        name: str,
        arguments: Mapping[str, Any],
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"connector.outbound.{name}",
                "authority": "connector.outbound.control",
                "caller_id": str(payload.get("caller_id") or ""),
                "caller_pack_id": str(payload.get("caller_pack_id") or ""),
                "caller_function_id": str(payload.get("caller_function_id") or ""),
                "profile_id": self.profile_id,
                "workspace_id": "",
                "session_id": str(payload.get("session_id") or ""),
                "arguments": dict(arguments),
            },
        )
        if not result.get("authorized"):
            raise PermissionError(str(result.get("reason") or "outbound denied"))


def create_outbound_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create redacted outbound status operations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return OutboundBroker(client, _profile(payload)).read(name, payload)

    return operation


def create_outbound_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated outbound control operations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return OutboundBroker(client, _profile(payload)).control(name, payload)

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


def _message(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("connector message must be an object")
    encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) > _MAX_MESSAGE:
        raise ValueError("connector message exceeds size limit")
    return json.loads(encoded)


def _public(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _copy(item)
        for key, item in value.items()
        if key not in {"message"}
    }


def _bounded_result(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        return {"status": "truncated", "sha256": hashlib.sha256(encoded.encode()).hexdigest()}
    return json.loads(encoded)


def _safe_error(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "connector adapter returned an invalid result"
    return str(value.get("error") or value.get("message") or "delivery failed")[:1000]


def _identifier(value: Any, name: str) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError(f"{name} is invalid")
    return identifier


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _retry_delay(attempt: int) -> int:
    return min(3_600_000, 1_000 * (2 ** max(0, min(12, attempt - 1))))


def _prune(deliveries: dict[str, Any]) -> None:
    if len(deliveries) <= 10_000:
        return
    terminal = sorted(
        (
            item
            for item in deliveries.values()
            if item["status"] in {"cancelled", "delivered", "failed"}
        ),
        key=lambda item: int(item["updated_at_ms"]),
    )
    for item in terminal[: max(0, len(deliveries) - 10_000)]:
        deliveries.pop(str(item["delivery_id"]), None)


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".outbound-", suffix=".tmp")
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

