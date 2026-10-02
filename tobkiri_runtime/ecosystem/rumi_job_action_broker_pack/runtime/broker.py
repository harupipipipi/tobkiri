"""Deterministic adapter routing with persistent idempotency protection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock
from .dispatch_envelope import (
    PENDING,
    TERMINAL,
    VERSION as ENVELOPE_VERSION,
    envelope_digest,
    immutable_json,
    restored_envelope,
    retained_status,
)

ADAPTER = "rumi.action.job.adapter.v1"
SERVICE_PACK_ID = "rumi_job_action_broker_pack"
VERSION = "rumi.job-dispatch-ledger.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_FORBIDDEN = {
    "approved",
    "approval_token",
    "authority_token",
    "authority_receipt",
    "viewer_host_approved",
    "yolo_mode",
}


class JobActionBroker:
    """Route one action ID to one selected adapter and suppress replay."""

    def __init__(
        self,
        client: Any,
        profile_id: str,
        *,
        root: Path | None = None,
        canonical: bool = False,
    ) -> None:
        self.client = client
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(root or USER_DATA_DIR) / "packs" / SERVICE_PACK_ID / "profiles" / self.profile_id
        )
        self.path = self.root / "dispatch-ledger.json"
        self.lock_root = self.root / "locks"
        self.canonical = canonical

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch or cancel one idempotent job action."""
        _reject_authority_material(payload)
        if self.canonical and payload.get("profile_id", self.profile_id) != self.profile_id:
            raise PermissionError("job Profile differs from the captured owner")
        if name == "dispatch":
            return self._dispatch(payload)
        if name == "cancel":
            return self._cancel(payload)
        if name == "status":
            return self._status(payload)
        raise ValueError(f"unknown job action operation: {name}")

    def _dispatch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        _reject_authority_material(payload)
        action_id = _identifier(payload.get("action_id"), "action_id")
        key = _identifier(payload.get("idempotency_key"), "idempotency_key")
        arguments = dict(_mapping(payload.get("payload")))
        digest = _digest({"action_id": action_id, "payload": arguments})
        envelope = {
            "version": ENVELOPE_VERSION,
            "profile_id": self.profile_id,
            "action_id": action_id,
            "payload": arguments,
            "idempotency_key": key,
            "schedule_id": str(payload.get("schedule_id") or ""),
            "lease_id": str(payload.get("lease_id") or ""),
        }
        if self.canonical:
            envelope = immutable_json(envelope)
            for field in ("schedule_id", "lease_id"):
                if envelope[field]:
                    envelope[field] = _identifier(envelope[field], field)
        with NamedLock(self.lock_root, "dispatch"):
            state = self._read()
            current = state["entries"].get(key)
            if current is not None:
                if self.canonical and self._retained_envelope(current, key) is None:
                    return self._reconciliation(key, "retained_job_binding_missing")
                if current.get("payload_hash") != digest:
                    raise PermissionError("idempotency key payload does not match")
                if self.canonical:
                    retained = self._retained_envelope(current, key)
                    if retained is None:
                        return self._reconciliation(key, "retained_job_binding_missing")
                    if envelope_digest(
                        {"version": ENVELOPE_VERSION, **retained}
                    ) != envelope_digest(envelope):
                        raise PermissionError("idempotency key scheduler binding changed")
                    if self._retained_provider(current) is None:
                        return self._reconciliation(key, "retained_job_provider_unavailable")
                return {
                    "status": current["status"],
                    "deduplicated": True,
                    "idempotency_key": key,
                    "dispatch": _copy(current),
                }
            entry = {
                "idempotency_key": key,
                "action_id": action_id,
                "payload_hash": digest,
                "status": "running",
                "provider_instance_id": "",
                "result": None,
                "error": "",
                "created_at_ms": _now_ms(),
                "updated_at_ms": _now_ms(),
            }
            if self.canonical:
                entry.update(
                    dispatch_envelope=envelope,
                    dispatch_envelope_digest=envelope_digest(envelope),
                )
            state["entries"][key] = entry
            self._write(state)
        try:
            provider = self._provider(action_id)
            entry["provider_instance_id"] = str(
                provider.get("provider_instance_id") or provider.get("provider_id") or ""
            )
            entry["operation_id"] = str(provider.get("operation_id") or "")
            if self.canonical:
                entry["provider_binding"] = _provider_binding(provider)
                entry["provider_binding_digest"] = envelope_digest(entry["provider_binding"])
                # Bind the exact adapter before issuing any effect. A crash before
                # this commit is ambiguous and never authorizes redispatch.
                entry = self._finish(key, entry)
                if entry.get("cancel_requested"):
                    return self._cancel({"idempotency_key": key})
            result = self._adapter(
                provider,
                "dispatch",
                (
                    {field: value for field, value in envelope.items() if field != "version"}
                    if self.canonical
                    else {
                        "action_id": action_id,
                        "payload": arguments,
                        "idempotency_key": key,
                        "schedule_id": str(payload.get("schedule_id") or ""),
                        "lease_id": str(payload.get("lease_id") or ""),
                        "profile_id": self.profile_id,
                    }
                ),
            )
            entry["status"] = retained_status(result) if self.canonical else _status(result)
            entry["result"] = _bounded(result)
        except Exception as exc:
            entry["status"] = "reconciliation_required" if self.canonical else "failed"
            entry["error"] = f"Job dispatch failed: {type(exc).__name__}"
            self._finish(key, entry)
            if self.canonical:
                return self._reconciliation(key, "job_dispatch_outcome_unconfirmed")
            raise
        entry = self._finish(key, entry)
        return {
            "status": entry["status"],
            "idempotency_key": key,
            "result": entry["result"],
            "provider_instance_id": entry["provider_instance_id"],
        }

    def _cancel(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        key = _identifier(payload.get("idempotency_key"), "idempotency_key")
        state = self._read()
        entry = state["entries"].get(key)
        if entry is None:
            return {"status": "unknown", "idempotency_key": key}
        if self.canonical:
            envelope = self._retained_envelope(entry, key)
            if envelope is None:
                return self._reconciliation(key, "retained_job_binding_missing")
            entry["cancel_requested"] = True
            entry["status"] = "cancellation_pending"
            entry = self._finish(key, entry)
            provider = self._retained_provider(entry)
            if provider is None:
                return self._reconciliation(key, "retained_job_provider_unavailable")
            try:
                result = self._adapter(provider, "cancel", envelope)
            except Exception:
                return self._reconciliation(key, "job_cancellation_unconfirmed")
            status = retained_status(result)
            entry["status"] = status if status in TERMINAL else "cancellation_pending"
            entry["result"] = _bounded(result)
            entry = self._finish(key, entry)
            return {"status": entry["status"], "idempotency_key": key, "result": entry["result"]}
        provider_id = str(entry.get("provider_instance_id") or "")
        if not provider_id:
            return {"status": "cancellation_pending", "idempotency_key": key}
        provider = {
            "provider_instance_id": provider_id,
            "operation_id": str(entry.get("operation_id") or ""),
        }
        result = self._adapter(
            provider,
            "cancel",
            {
                "action_id": entry["action_id"],
                "idempotency_key": key,
                "profile_id": self.profile_id,
            },
        )
        entry["status"] = (
            "cancelled" if result.get("status") == "cancelled" else "cancellation_pending"
        )
        entry["result"] = _bounded(result)
        self._finish(key, entry)
        return {"status": entry["status"], "idempotency_key": key, "result": result}

    def _status(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        key = _identifier(payload.get("idempotency_key"), "idempotency_key")
        value = self._read()["entries"].get(key)
        if self.canonical and value is not None:
            envelope = self._retained_envelope(value, key)
            if envelope is None:
                return self._reconciliation(key, "retained_job_binding_missing")
            if value.get("status") in PENDING:
                provider = self._retained_provider(value)
                if provider is None:
                    return self._reconciliation(key, "retained_job_provider_unavailable")
                try:
                    result = self._adapter(provider, "status", envelope)
                except Exception:
                    return self._reconciliation(key, "job_status_unavailable")
                value["status"] = retained_status(result)
                value["result"] = _bounded(result)
                value = self._finish(key, value)
            return _copy(value)
        if (
            self.canonical
            and value is not None
            and value["status"] in {"accepted", "running"}
            and value.get("operation_id")
        ):
            result = self._adapter(
                {"operation_id": value["operation_id"]},
                "status",
                {
                    "action_id": value["action_id"],
                    "idempotency_key": key,
                    "profile_id": self.profile_id,
                },
            )
            if result.get("status") in {"ok", "completed", "failed", "cancelled"}:
                value["status"] = result["status"]
                value["result"] = _bounded(result)
                self._finish(key, value)
        return _copy(value) if value is not None else {"status": "unknown"}

    def _provider(self, action_id: str) -> Mapping[str, Any]:
        if self.canonical:
            providers = self.client.providers("tobkiri.action.job.adapter.v2")
            matches = []
            for item in providers:
                operation = str(item.get("operation_id") or "")
                if not operation:
                    continue
                description = self._adapter(
                    item,
                    "describe",
                    {"profile_id": self.profile_id},
                )
                if action_id in description.get("action_ids", []):
                    matches.append(item)
            if len(matches) != 1:
                raise RuntimeError("selected job adapter is unavailable or ambiguous")
            return matches[0]
        providers = self.client.providers(ADAPTER)
        matches = [item for item in providers if str(item.get("instance_key") or "") == action_id]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected one selected job adapter for {action_id}; found {len(matches)}"
            )
        return matches[0]

    def _adapter(
        self, provider: Mapping[str, Any], operation: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if self.canonical:
            operation_id = str(provider.get("operation_id") or "")
            if not operation_id:
                raise RuntimeError("selected job adapter operation is unavailable")
            result = self.client.invoke(
                "tobkiri.action.job.adapter.v2",
                operation_id,
                {**dict(payload), "operation": operation},
            )
        else:
            result = self.client.invoke(
                ADAPTER,
                operation,
                dict(payload),
                provider_instance_id=str(provider.get("provider_instance_id") or ""),
            )
        if not isinstance(result, Mapping):
            raise ValueError("job adapter returned an invalid result")
        return result

    def _retained_envelope(self, entry: Mapping[str, Any], key: str) -> dict[str, Any] | None:
        value = restored_envelope(entry, profile_id=self.profile_id, key=key)
        if value is not None:
            _reject_authority_material(value)
            if _digest({"action_id": value["action_id"], "payload": value["payload"]}) != entry.get(
                "payload_hash"
            ):
                raise PermissionError("retained job payload hash changed")
        return value

    def _retained_provider(self, entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
        binding = entry.get("provider_binding")
        if not isinstance(binding, Mapping):
            return None
        if envelope_digest(binding) != entry.get("provider_binding_digest"):
            raise PermissionError("retained job provider hash changed")
        if binding.get("operation_id") != entry.get("operation_id"):
            raise PermissionError("retained job provider operation changed")
        try:
            providers = self.client.providers("tobkiri.action.job.adapter.v2")
        except Exception:
            return None
        matches = [item for item in providers if _provider_binding(item) == binding]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _reconciliation(key: str, reason: str) -> dict[str, Any]:
        return {"status": "reconciliation_required", "idempotency_key": key, "reason": reason}

    def _finish(self, key: str, entry: dict[str, Any]) -> dict[str, Any]:
        with NamedLock(self.lock_root, "dispatch"):
            state = self._read()
            current = state["entries"].get(key)
            if current is None or current["payload_hash"] != entry["payload_hash"]:
                raise RuntimeError("job dispatch ledger changed during execution")
            if self.canonical:
                current_envelope = self._retained_envelope(current, key)
                incoming_envelope = self._retained_envelope(entry, key)
                if current_envelope != incoming_envelope:
                    raise PermissionError("job dispatch envelope changed during execution")
                current_binding = current.get("provider_binding")
                if isinstance(current_binding, Mapping):
                    if envelope_digest(current_binding) != current.get("provider_binding_digest"):
                        raise PermissionError("retained job provider hash changed")
                    if current_binding != entry.get("provider_binding"):
                        raise PermissionError("job provider changed during execution")
                entry["cancel_requested"] = bool(
                    entry.get("cancel_requested") or current.get("cancel_requested")
                )
                if current.get("status") == "cancelled":
                    entry["status"], entry["result"] = "cancelled", current.get("result")
            entry["updated_at_ms"] = _now_ms()
            state["entries"][key] = _copy(entry)
            self._write(state)
            return _copy(entry)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": VERSION, "entries": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("version") != VERSION:
            raise ValueError("job dispatch ledger is invalid")
        entries = value.get("entries")
        if not isinstance(entries, Mapping):
            raise ValueError("job dispatch entries are invalid")
        return {"version": VERSION, "entries": _copy(entries)}

    def _write(self, value: Mapping[str, Any]) -> None:
        _atomic_json(self.path, value)


def create_job_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the selected global job action broker."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return JobActionBroker(
            client,
            str(payload.get("profile_id") or "default"),
        ).invoke(name, payload)

    return operation


def _reject_authority_material(payload: Mapping[str, Any]) -> None:
    found = sorted(_authority_keys(payload))
    if found:
        raise PermissionError("job payload contains forbidden authority material")


def _authority_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        found = {str(key) for key in value if str(key) in _FORBIDDEN}
        for item in value.values():
            found.update(_authority_keys(item))
        return found
    if isinstance(value, (list, tuple)):
        found = set()
        for item in value:
            found.update(_authority_keys(item))
        return found
    return set()


def _identifier(value: Any, name: str) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError(f"{name} is invalid")
    return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("job action payload must be an object")
    return value


def _status(result: Any) -> str:
    if not isinstance(result, Mapping):
        return "failed"
    value = str(result.get("status") or "")
    return value if value in {"ok", "accepted", "running", "completed", "failed"} else "failed"


def _provider_binding(provider: Mapping[str, Any]) -> dict[str, str]:
    """Pin only finite identity metadata supplied by the captured public port."""
    return {
        key: value
        for key in (
            "operation_id",
            "provider_id",
            "provider_instance_id",
            "function_principal_id",
            "principal_id",
            "artifact_digest",
            "implementation_digest",
            "contract_revision_digest",
            "profile_id",
            "profile_revision",
            "activation_id",
            "plan_digest",
        )
        if isinstance((value := provider.get(key)), str) and value
    }


def _bounded(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        return {"status": "truncated", "sha256": hashlib.sha256(encoded.encode()).hexdigest()}
    return json.loads(encoded)


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".job-", suffix=".tmp")
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


_V4_ROUTES = {
    "rumi_job_action_broker_pack.job-action.broker": (
        "tobkiri.action.job.v1",
        "rumi_job_action_broker_pack.job-action-broker",
    ),
}


def _invoke_v4_owner(
    function_id: str, context: Any, payload: Mapping[str, Any], invocation: Any
) -> Mapping[str, Any]:
    del function_id
    if set(payload) - {
        "operation",
        "profile_id",
        "action_id",
        "payload",
        "idempotency_key",
        "schedule_id",
        "lease_id",
    }:
        raise PermissionError("job broker payload is invalid")
    client = invocation.contract_client(
        allowed_contract_ids=frozenset({"tobkiri.action.job.adapter.v2"}),
        consumer_pack_id=SERVICE_PACK_ID,
        include_credentials=False,
    )
    return JobActionBroker(
        client,
        context.profile_id,
        root=context.user_data_root,
        canonical=True,
    ).invoke(str(payload.get("operation") or ""), payload)


# Captured v4 entrypoints retain Broker-owned authority and profile scope.


class _OwnerHostFactoryV4:
    """Capture only exact, verified operations for this owner Function."""

    def __init__(self, function_id: str) -> None:
        self.function_id = function_id

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Bind operations and persistence to the selected Profile activation."""
        if (
            context.user_data_root is None
            or not context.profile_id
            or not context.provider_bindings
            or any(
                binding.function.function_id != self.function_id
                for binding in context.provider_bindings
            )
        ):
            raise PermissionError("owner capture scope is incomplete")
        expected_contract, expected_operation = _V4_ROUTES[self.function_id]
        if any(
            binding.operation.contract_id != expected_contract
            or binding.operation.operation_id != expected_operation
            for binding in context.provider_bindings
        ):
            raise PermissionError("owner operation binding is invalid")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if operation_id != expected_operation or (
                "profile_id" in payload and payload["profile_id"] != context.profile_id
            ):
                raise PermissionError("owner invocation scope is invalid")
            invocation.assert_current()
            result = _invoke_v4_owner(self.function_id, context, payload, invocation)
            invocation.assert_current()
            return result

        contributions = []
        for binding in context.provider_bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                raise PermissionError("owner domain binding is unavailable")
            contributions.append(
                HostProviderContributionV4(
                    contract_id=binding.operation.contract_id,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), lambda: None)


HOST_PROVIDER_FACTORY = {
    function_id: _OwnerHostFactoryV4(function_id) for function_id in _V4_ROUTES
}
