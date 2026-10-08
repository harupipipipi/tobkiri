"""Authenticated publication fence for development Host data migration."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import secrets
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_bytes
from tobkiri_protocol.secure_persistence import SecureDirectory

JOURNAL = "development-host-handover.json"
KEY = "development-host-handover.key"
SCHEMA = "io.tobkiri.development-host-handover-journal.v1"


def read_handover_journal(root: Path) -> dict[str, Any] | None:
    """Read exact authenticated migration evidence without creating state."""
    if not root.exists():
        return None
    if not (root / JOURNAL).exists() and not (root / JOURNAL).is_symlink():
        witnesses = (
            root / KEY,
            root / "development-host-staging",
            root / "packvm-vz" / JOURNAL,
        )
        if any(path.exists() or path.is_symlink() for path in witnesses):
            raise ValueError(
                "Development Host handover requires its authenticated recovery journal"
            )
        return None
    storage = SecureDirectory(root, create=False)
    if not storage.exists(JOURNAL):
        return None
    value = json.loads(storage.read_bytes_bounded(JOURNAL, max_bytes=128 * 1024))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("Development Host handover journal is invalid")
    authentication = value.pop("authentication", None)
    key = storage.read_bytes_bounded(KEY, max_bytes=32)
    expected = hmac.new(key, canonical_bytes(value), hashlib.sha256).hexdigest()
    if (
        len(key) != 32
        or not isinstance(authentication, str)
        or not hmac.compare_digest(authentication, expected)
    ):
        raise ValueError("Development Host handover journal authentication failed")
    return value


def write_handover_journal(root: Path, value: Mapping[str, Any]) -> None:
    """Durably record an unpublished migration using a fresh local key."""
    storage = SecureDirectory(root, create=False)
    if not storage.exists(KEY):
        storage.write_bytes_atomic(
            KEY, secrets.token_bytes(32), before_publish=lambda: _require_missing_key(storage)
        )
    key = storage.read_bytes_bounded(KEY, max_bytes=32)
    if len(key) != 32:
        raise ValueError("Development Host handover key is invalid")
    payload = {
        "schema": SCHEMA,
        **{key: item for key, item in value.items() if key != "authentication"},
    }
    payload["authentication"] = hmac.new(key, canonical_bytes(payload), hashlib.sha256).hexdigest()
    storage.write_bytes_atomic(JOURNAL, canonical_bytes(payload))


def require_completed_handover(root: Path) -> None:
    """Withhold Profile activation and dispatch during partial publication."""
    journal = read_handover_journal(root)
    if journal is not None and journal.get("stage") != "completed":
        raise ValueError("Development Host handover requires recovery before activation")


def _require_missing_key(storage: SecureDirectory) -> None:
    if storage.exists(KEY):
        raise ValueError("Development Host handover key appeared during publication")
