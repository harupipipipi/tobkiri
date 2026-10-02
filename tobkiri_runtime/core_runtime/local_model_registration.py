"""Operator-owned local model admission independent of registry Pack ownership."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from core_runtime.local_model_authority import ALLOWLIST_VERSION, LOCAL_ADAPTER
from core_runtime.local_model_transport import LocalModelBinding
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock
from tobkiri_protocol.canonical import strict_loads
from tobkiri_protocol.secure_persistence import SecureDirectory


class LocalModelRegistryPort(Protocol):
    """Registry operations supplied explicitly by the owning operator composition."""

    @property
    def profile_id(self) -> str:
        """Return the canonical Profile identity."""

    def snapshot(self) -> Mapping[str, Any]:
        """Read the current provider registry and revision."""

    def save(self, record: Mapping[str, Any], *, expected_revision: int) -> Any:
        """Save through the registry owner's revision-checked operation."""


def register(
    *, user_data: Path, profile: str, provider: str, endpoint: str, model: str,
    registry: LocalModelRegistryPort
) -> None:
    """Add one exact tuple, refusing to replace any existing configuration."""
    if (
        not isinstance(profile, str)
        or profile != validate_profile_id(profile)
        or profile != registry.profile_id
        or not isinstance(provider, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", provider) is None
    ):
        raise ValueError("Profile and provider identities must be canonical")
    LocalModelBinding(profile, provider, endpoint, (model,), 1)
    owner = SecureDirectory(user_data / "host_local_models")
    registration = {
        "profile_id": profile,
        "provider_instance_id": provider,
        "endpoint": endpoint,
        "model_ids": [model],
    }
    with NamedLock(user_data / "host_local_models" / "locks", "registration"):
        document = (
            strict_loads(owner.read_bytes_bounded("allowlist.json", max_bytes=65536))
            if owner.exists("allowlist.json")
            else {"version": ALLOWLIST_VERSION, "registrations": []}
        )
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "registrations"}
            or document["version"] != ALLOWLIST_VERSION
            or not isinstance(document["registrations"], list)
        ):
            raise ValueError("existing Host local model configuration is invalid")
        matches = [
            item
            for item in document["registrations"]
            if isinstance(item, dict)
            and item.get("profile_id") == profile
            and item.get("provider_instance_id") == provider
        ]
        if matches and matches != [registration]:
            raise ValueError(
                "existing Host registration differs; explicit replacement is required"
            )
        snapshot = registry.snapshot()
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("profile_id") != profile
            or type(snapshot.get("revision")) is not int
            or snapshot["revision"] < 0
            or not isinstance(snapshot.get("providers"), list)
            or any(not isinstance(item, Mapping) for item in snapshot["providers"])
        ):
            raise ValueError("local model registry snapshot is invalid")
        records = [
            item
            for item in snapshot["providers"]
            if item["provider_instance_id"] == provider
        ]
        expected = {
            "provider_instance_id": provider,
            "adapter_id": LOCAL_ADAPTER,
            "endpoint": endpoint,
            "credential_handle": None,
            "enabled": True,
        }
        if records and (
            len(records) != 1
            or any(records[0].get(k) != v for k, v in expected.items())
        ):
            raise ValueError(
                "existing provider connection differs; explicit replacement is required"
            )
        if not matches:
            document["registrations"].append(registration)
            encoded = json.dumps(document, sort_keys=True, allow_nan=False).encode()
            if len(encoded) > 65536:
                raise ValueError("Host local model registration limit exceeded")
            owner.write_bytes_atomic("allowlist.json", encoded)
        if not records:
            registry.save(
                {
                    **expected,
                    "display_name": "Local text model",
                    "data_residency": "local",
                },
                expected_revision=snapshot["revision"],
            )
