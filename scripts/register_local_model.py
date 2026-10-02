"""Register one explicitly approved local text runner for a desktop test.

This operator tool does not download or launch programs, replace existing
bindings, change OS permissions, or configure cloud credentials. Run only after
starting an approved loopback runner. It is not exposed as a Pack contract.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tobkiri_runtime"))

from core_runtime.local_model_authority import (
    ALLOWLIST_VERSION,
    LOCAL_ADAPTER,
)
from core_runtime.local_model_transport import LocalModelBinding
from core_runtime.runtime_locks import NamedLock
from ecosystem.rumi_provider_registry_pack.runtime.registry import (
    ProviderRegistry,
)
from tobkiri_protocol.canonical import strict_loads
from tobkiri_protocol.secure_persistence import SecureDirectory


def register(
    *, user_data: Path, profile: str, provider: str, endpoint: str, model: str
) -> None:
    """Add one exact tuple, refusing to replace any existing configuration."""
    registry = ProviderRegistry(profile, user_data_root=user_data)
    if (
        profile != registry.profile_id
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
                    "display_name": "Local Liquid AI test model",
                    "data_residency": "local",
                },
                expected_revision=snapshot["revision"],
            )


def main() -> None:
    """Register only the exact operator-supplied Profile and loopback model."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-data", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--provider", default="provider.liquid-local")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    register(
        user_data=args.user_data,
        profile=args.profile,
        provider=args.provider,
        endpoint=args.endpoint,
        model=args.model,
    )
    print("Local model registered. Select its connection and model in Tobkiri.")


if __name__ == "__main__":
    main()
