"""External-QA-oriented specifications for the provider registry pack."""

from __future__ import annotations

import json

import pytest

from ecosystem.rumi_provider_registry_pack.runtime.registry import (
    ProviderRegistry,
    ProviderRegistryConflict,
)


def test_provider_connection_is_redacted_and_health_starts_unknown(tmp_path) -> None:
    registry = ProviderRegistry("default", user_data_root=tmp_path)
    registry.save(
        {
            "provider_instance_id": "primary",
            "adapter_id": "adapter.standard",
            "credential_handle": "credential:opaque",
            "health_evidence": {"status": "available", "verified": False},
        },
        expected_revision=0,
    )

    assert registry.health()["providers"] == [
        {
            "provider_instance_id": "primary",
            "status": "unknown",
            "observed_at": None,
            "verified": False,
            "freshness": "unknown",
            "reason_code": "unknown",
        }
    ]
    assert "secret" not in str(registry.snapshot()).lower()


def test_provider_health_preserves_verified_limited_state(tmp_path) -> None:
    registry = ProviderRegistry("default", user_data_root=tmp_path)
    registry.save(
        {
            "provider_instance_id": "provider.example",
            "adapter_id": "adapter.standard",
            "health_evidence": {
                "status": "limited",
                "observed_at": 123.0,
                "verified": True,
            },
        },
        expected_revision=0,
    )

    assert registry.health()["providers"] == [
        {
            "provider_instance_id": "provider.example",
            "status": "limited",
            "observed_at": 123.0,
            "verified": True,
            "freshness": "fresh",
            "reason_code": "limited",
        }
    ]


def test_provider_registry_rejects_secret_and_stale_revision(tmp_path) -> None:
    registry = ProviderRegistry("default", user_data_root=tmp_path)
    with pytest.raises(ValueError, match="opaque handle"):
        registry.save(
            {
                "provider_instance_id": "unsafe",
                "adapter_id": "adapter.standard",
                "credential_handle": "plain-secret",
            },
            expected_revision=0,
        )
    registry.save(
        {"provider_instance_id": "safe", "adapter_id": "adapter.standard"},
        expected_revision=0,
    )
    with pytest.raises(ProviderRegistryConflict):
        registry.delete("safe", expected_revision=0)


def test_registry_rejects_credentialed_http_but_keeps_credentialless_local_http(
    tmp_path,
) -> None:
    registry = ProviderRegistry("default", user_data_root=tmp_path)

    with pytest.raises(ValueError, match="requires HTTPS") as denied:
        registry.save(
            {
                "provider_instance_id": "plaintext-secret",
                "adapter_id": "openai-compatible",
                "credential_handle": "credential:opaque-review-a",
                "endpoint": "http://127.0.0.1:11434/v1",
            },
            expected_revision=0,
        )

    assert "credential:opaque-review-a" not in str(denied.value)
    assert registry.snapshot()["revision"] == 0
    saved = registry.save(
        {
            "provider_instance_id": "local-credentialless",
            "adapter_id": "openai-compatible",
            "endpoint": "http://127.0.0.1:11434/v1",
        },
        expected_revision=0,
    )
    assert saved["provider"]["credential_handle"] is None
    assert saved["provider"]["endpoint"] == "http://127.0.0.1:11434/v1"

    persisted = json.loads(registry.path.read_text(encoding="utf-8"))
    persisted["providers"]["local-credentialless"]["credential_handle"] = (
        "credential:opaque-stale"
    )
    registry.path.write_text(json.dumps(persisted), encoding="utf-8")
    with pytest.raises(ValueError, match="requires HTTPS") as stale:
        registry.snapshot()
    assert "opaque-stale" not in str(stale.value)
