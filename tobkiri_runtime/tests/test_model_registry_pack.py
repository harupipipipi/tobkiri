"""External-QA-oriented contract tests for the Wave 5 model registry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from ecosystem.rumi_model_registry_pack.runtime.registry import (
    ModelRegistry,
    ModelRegistryConflict,
)


def _hash(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def test_profile_alias_and_stale_revision_are_owner_atomic(tmp_path) -> None:
    registry = ModelRegistry("default", user_data_root=tmp_path)
    saved = registry.save(
        {
            "model_profile_id": "daily",
            "model_id": "catalog/model",
            "credential_handle": "credential:opaque",
            "requirements": {"tool_calling": True},
        },
        expected_revision=0,
    )
    registry.set_alias("default", "daily", expected_revision=1)

    assert registry.resolve("default")["profile"]["model_id"] == "catalog/model"
    with pytest.raises(ModelRegistryConflict):
        registry.delete("daily", expected_revision=saved["store_revision"])


def test_secret_values_are_rejected(tmp_path) -> None:
    registry = ModelRegistry("default", user_data_root=tmp_path)

    with pytest.raises(ValueError, match="opaque handle"):
        registry.save(
            {
                "model_profile_id": "unsafe",
                "model_id": "catalog/model",
                "credential_handle": "plain-secret",
            },
            expected_revision=0,
        )


def test_migration_requires_source_hash_and_can_rollback(tmp_path) -> None:
    registry = ModelRegistry("default", user_data_root=tmp_path)
    source = {
        "profiles": [
            {
                "model_profile_id": "daily",
                "display_name": "daily",
                "model_id": "catalog/model",
                "requirements": {},
                "credential_handle": None,
                "parameters": {},
                "enabled": False,
                "metadata": {},
            }
        ],
        "aliases": {"default": "daily"},
    }
    result = registry.migrate(
        source["profiles"],
        source["aliases"],
        expected_source_hash=_hash(source),
    )

    assert registry.resolve("default")["resolved_profile_id"] == "daily"
    assert registry.get("daily")["enabled"] is False
    assert registry.rollback_migration(result["migration_id"])["rolled_back"]
    assert registry.snapshot()["profiles"] == []


@pytest.mark.parametrize("enabled", [1, "true"])
@pytest.mark.parametrize(
    "change",
    [
        {"model_id": "another/model"},
        {"metadata": {"provider_connection_id": "provider.other"}},
    ],
)
def test_truthy_activation_cannot_bypass_the_existing_route_guard(
    tmp_path: Path,
    enabled: Any,
    change: dict[str, Any],
) -> None:
    """Owner-side boolean coercion must enforce the same activation identity."""
    registry = ModelRegistry("default", user_data_root=tmp_path)
    record = {
        "model_profile_id": "daily",
        "model_id": "catalog/model",
        "metadata": {"provider_connection_id": "provider.fixture"},
    }
    registry.save({**record, "enabled": False}, expected_revision=0)
    before = registry.snapshot()

    with pytest.raises(ValueError, match="route identity changed"):
        registry.save(
            {**record, **change, "enabled": enabled},
            expected_revision=1,
            preserve_existing=True,
        )

    assert registry.snapshot() == before


def test_omitted_enabled_keeps_an_existing_disabled_route_disabled(
    tmp_path: Path,
) -> None:
    """An ordinary preserved update does not request activation implicitly."""
    registry = ModelRegistry("default", user_data_root=tmp_path)
    registry.save(
        {
            "model_profile_id": "daily",
            "model_id": "catalog/model",
            "enabled": False,
            "metadata": {"provider_connection_id": "provider.fixture"},
        },
        expected_revision=0,
    )

    saved = registry.save(
        {
            "model_profile_id": "daily",
            "model_id": "another/model",
            "metadata": {"provider_connection_id": "provider.other"},
        },
        expected_revision=1,
        preserve_existing=True,
    )

    assert saved["profile"]["enabled"] is False
