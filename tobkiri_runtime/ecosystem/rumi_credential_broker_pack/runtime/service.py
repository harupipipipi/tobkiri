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
            return self.store.create(
                secret_material=(
                    data.get("secret_material")
                    if isinstance(data.get("secret_material"), Mapping)
                    else {}
                ),
                consumer_pack_id=str(data.get("consumer_pack_id") or ""),
                provider_instance_id=str(
                    data.get("provider_instance_id") or ""
                ),
                scopes=[str(item) for item in data.get("scopes", [])],
                label=str(data.get("label") or ""),
                expires_at=_optional_float(data.get("expires_at")),
            )
        if operation == "revoke":
            return self.store.revoke(str(data.get("handle") or ""))
        if operation == "list":
            return self.store.list()
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
            return self.store.rollback_migration(
                str(data.get("migration_id") or "")
            )
        if operation == "resolve":
            if not consumer:
                raise PermissionError("credential consumer identity is missing")
            return {
                "secret_material": self.store.resolve(
                    str(data.get("handle") or ""),
                    consumer_pack_id=consumer,
                    provider_instance_id=str(
                        data.get("provider_instance_id") or ""
                    ),
                    scope=str(data.get("scope") or ""),
                )
            }
        raise ValueError(f"unknown credential operation: {operation}")


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        raise ValueError("expires_at is invalid") from None

