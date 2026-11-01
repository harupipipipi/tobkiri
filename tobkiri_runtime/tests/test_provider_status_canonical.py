"""Provider observations cross the canonical Broker boundary conservatively."""

from pathlib import Path
import importlib.util
import json
import sys

import pytest
from tobkiri_protocol.canonical import canonical_json

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "status_owner_copy", BASE / "ecosystem/rumi_provider_registry_pack/runtime/process.py"
)
owner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = owner
spec.loader.exec_module(owner)


@pytest.mark.parametrize(
    "observed, expected",
    [
        (123.5, 123),
        (456.0, 456),
        (123, 123),
        (True, None),
        (None, None),
        ("123", None),
        (float("nan"), None),
        (float("inf"), None),
        (-1, None),
        (2**53, None),
        (2**10000, None),
    ],
)
def test_status_owner_normalizes_observation_before_broker_canonicalization(
    observed: object, expected: int | None
) -> None:
    result = owner._provider_connection_snapshot(
        {
            "revision": 1,
            "providers": [
                {
                    "provider_instance_id": "provider.openrouter.main",
                    "display_name": "openrouter-main",
                    "enabled": True,
                    "adapter_id": "openai-compatible",
                    "credential_handle": "credential:synthetic-marker",
                    "metadata": {"catalog_provider_id": "openrouter"},
                    "health_evidence": {
                        "verified": True,
                        "status": "available",
                        "observed_at": observed,
                    },
                }
            ],
        }
    )
    record = result["providers"][0]
    assert record["observed_at"] == expected
    assert record["adapter_id"] == "openai-compatible"
    assert record["catalog_provider_id"] == "openrouter"
    assert "synthetic-marker" not in canonical_json(result).decode()
    assert json.loads(canonical_json(result)) == result


def test_status_owner_does_not_promote_credential_presence_to_health() -> None:
    result = owner._provider_connection_snapshot(
        {
            "revision": 1,
            "providers": [
                {
                    "provider_instance_id": "provider.openrouter.main",
                    "display_name": "openrouter-main",
                    "enabled": True,
                    "credential_handle": "credential:synthetic-marker",
                    "adapter_id": "openai-compatible",
                    "health_evidence": {
                        "verified": False,
                        "status": "available",
                        "observed_at": 123.5,
                    },
                }
            ],
        }
    )
    record = result["providers"][0]
    assert record["credential_status"] == "configured"
    assert record["health_status"] == "unverified"
    assert record["reachability"] == "unknown"
    assert record["observed_at"] is None
