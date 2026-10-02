"""Operator registration rejects identity aliases before granting local access."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from core_runtime.local_model_authority import ALLOWLIST_VERSION, LOCAL_ADAPTER
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry

ENDPOINT = "http://127.0.0.1:18080/v1"
MODEL = "local-model"
PROVIDER = "provider.existing"


@pytest.fixture(scope="module")
def registration_script() -> ModuleType:
    """Load the operator tool without invoking its command-line entrypoint."""
    path = Path(__file__).resolve().parents[2] / "scripts/register_local_model.py"
    spec = importlib.util.spec_from_file_location("local_model_registration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "profile,provider",
    [
        (" defaults ", PROVIDER),
        ("defaults\n", PROVIDER),
        ("", PROVIDER),
        ("../defaults", PROVIDER),
        ("defaults", " " + PROVIDER),
        ("defaults", PROVIDER + " "),
        ("defaults", PROVIDER + "\n"),
        ("defaults", ""),
        ("defaults", "provider with spaces"),
        ("defaults", "provider.日本語"),
        ("defaults", "/provider"),
        ("defaults", "p" * 257),
        ("defaults", None),
        ("defaults", 1),
    ],
)
def test_noncanonical_identity_is_rejected_before_any_write(
    tmp_path: Path,
    registration_script: ModuleType,
    profile: str,
    provider: Any,
) -> None:
    with pytest.raises(ValueError):
        registration_script.register(
            user_data=tmp_path,
            profile=profile,
            provider=provider,
            endpoint=ENDPOINT,
            model=MODEL,
        )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("raw_provider", [" " + PROVIDER, PROVIDER + " "])
def test_provider_alias_cannot_replace_an_existing_remote_connection(
    tmp_path: Path,
    registration_script: ModuleType,
    raw_provider: str,
) -> None:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    registry.save(
        {
            "provider_instance_id": PROVIDER,
            "adapter_id": "openai-compatible",
            "endpoint": "https://example.invalid/v1",
            "credential_handle": None,
            "enabled": True,
        },
        expected_revision=0,
    )
    before = registry.path.read_bytes()
    with pytest.raises(ValueError, match="canonical"):
        registration_script.register(
            user_data=tmp_path,
            profile="defaults",
            provider=raw_provider,
            endpoint=ENDPOINT,
            model=MODEL,
        )
    assert registry.path.read_bytes() == before
    assert registry.snapshot()["providers"][0]["record_revision"] == 1
    assert not (tmp_path / "host_local_models").exists()


def test_exact_existing_connection_is_preserved_by_idempotent_registration(
    tmp_path: Path,
    registration_script: ModuleType,
) -> None:
    arguments = {
        "user_data": tmp_path,
        "profile": "defaults",
        "provider": PROVIDER,
        "endpoint": ENDPOINT,
        "model": MODEL,
    }
    registration_script.register(**arguments)
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    allowlist = tmp_path / "host_local_models/allowlist.json"
    before_registry = registry.path.read_bytes()
    before_allowlist = allowlist.read_bytes()
    registration_script.register(**arguments)
    assert registry.path.read_bytes() == before_registry
    assert allowlist.read_bytes() == before_allowlist
    assert json.loads(before_allowlist) == {
        "version": ALLOWLIST_VERSION,
        "registrations": [
            {
                "profile_id": "defaults",
                "provider_instance_id": PROVIDER,
                "endpoint": ENDPOINT,
                "model_ids": [MODEL],
            }
        ],
    }
    record = registry.snapshot()["providers"][0]
    assert record["adapter_id"] == LOCAL_ADAPTER
    assert record["credential_handle"] is None
    assert record["record_revision"] == 1


@pytest.mark.parametrize(
    "replacement", [{"endpoint": "http://127.0.0.1:18081/v1"}, {"model": "other"}]
)
def test_existing_owner_registration_cannot_be_replaced(
    tmp_path: Path,
    registration_script: ModuleType,
    replacement: dict[str, str],
) -> None:
    arguments = {
        "user_data": tmp_path,
        "profile": "defaults",
        "provider": PROVIDER,
        "endpoint": ENDPOINT,
        "model": MODEL,
    }
    registration_script.register(**arguments)
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    allowlist = tmp_path / "host_local_models/allowlist.json"
    before_registry = registry.path.read_bytes()
    before_allowlist = allowlist.read_bytes()
    with pytest.raises(ValueError, match="replacement"):
        registration_script.register(**{**arguments, **replacement})
    assert registry.path.read_bytes() == before_registry
    assert allowlist.read_bytes() == before_allowlist


@pytest.mark.parametrize(
    "endpoint,model",
    [
        ("http://localhost:18080/v1", MODEL),
        ("http://127.0.0.1:65536/v1", MODEL),
        ("https://127.0.0.1:18080/v1", MODEL),
        ("http://127.0.0.1:18080/v1?override=1", MODEL),
        (ENDPOINT, ""),
    ],
)
def test_invalid_loopback_binding_is_rejected_before_owner_write(
    tmp_path: Path,
    registration_script: ModuleType,
    endpoint: str,
    model: str,
) -> None:
    with pytest.raises(ValueError):
        registration_script.register(
            user_data=tmp_path,
            profile="defaults",
            provider=PROVIDER,
            endpoint=endpoint,
            model=model,
        )
    assert list(tmp_path.iterdir()) == []
