"""Credential broker contracts with caller-bound secret resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .store import CredentialBrokerStore


class CredentialBrokerService:
    """Dispatch management, status, and provider-only resolution operations."""

    def __init__(self, *, user_data_root: Path | None = None) -> None:
        self.store = CredentialBrokerStore(user_data_root=user_data_root)

    def invoke(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Invoke one credential operation without logging payload contents."""
        data = dict(payload)
        consumer = str(data.pop("_contract_consumer_pack_id", "")).strip()
        if operation == "create":
            profile_id = _required_profile_id(data)
            supplied_material = data.get("secret_material")
            secret_material: Mapping[str, Any] = (
                supplied_material if isinstance(supplied_material, Mapping) else {}
            )
            result = self.store.create(
                secret_material=secret_material,
                consumer_pack_id=str(data.get("consumer_pack_id") or ""),
                provider_instance_id=str(data.get("provider_instance_id") or ""),
                profile_id=profile_id,
                scopes=[str(item) for item in data.get("scopes", [])],
                purpose=str(data.get("purpose") or "provider.invoke"),
                label=str(data.get("label") or ""),
                expires_at=_optional_float(data.get("expires_at")),
            )
            result["credential_ref"] = {
                "profile_id": result.get("profile_id", ""),
                "provider_id": result.get("provider_instance_id", ""),
                "credential_id": result.get("handle", ""),
                "key_version": result.get("key_version", ""),
                "purpose": str(data.get("purpose") or "provider.invoke"),
            }
            return result
        if operation == "revoke":
            return self.store.revoke(
                str(data.get("handle") or ""),
                profile_id=_required_profile_id(data),
            )
        if operation == "list":
            return self.store.list(profile_id=_required_profile_id(data))
        if operation == "migration.apply":
            records = data.get("records")
            if not isinstance(records, list) or not all(
                isinstance(item, Mapping) for item in records
            ):
                raise ValueError("credential migration payload is invalid")
            return self.store.migrate(
                records,
                expected_source_hash=str(data.get("expected_source_hash") or ""),
            )
        if operation == "migration.rollback":
            return self.store.rollback_migration(str(data.get("migration_id") or ""))
        if operation == "resolve":
            del consumer
            raise PermissionError("credential resolution requires the bound Host transport")
        raise ValueError(f"unknown credential operation: {operation}")


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        raise ValueError("expires_at is invalid") from None


def _required_profile_id(data: Mapping[str, Any]) -> str:
    value = str(data.get("profile_id") or "").strip()
    if not value:
        raise PermissionError("credential profile identity is missing")
    return value
