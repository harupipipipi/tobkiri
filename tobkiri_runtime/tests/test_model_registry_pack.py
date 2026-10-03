"""External-QA-oriented contract tests for the Wave 5 model registry."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest

from ecosystem.rumi_model_registry_pack.runtime.registry import (
    ModelRegistry,
    ModelRegistryConflict,
)
from core_runtime.runtime_locks import NamedLock


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


def _route(model_id: str = "catalog/model") -> dict[str, object]:
    return {
        "model_profile_id": "daily", "model_id": model_id,
        "display_name": "Daily",
        "metadata": {"provider_connection_id": "provider.fixture"},
    }


@pytest.mark.parametrize("second_model", ["catalog/model", "different/model"])
def test_concurrent_creates_cannot_replace_the_winning_record(
    tmp_path: Path, second_model: str,
) -> None:
    """Even identical concurrent requests must satisfy the revision fence."""
    registry = ModelRegistry("default", user_data_root=tmp_path)
    barrier = Barrier(2)

    def create(model_id: str) -> object:
        barrier.wait(timeout=5)
        try:
            return registry.create(_route(model_id), expected_revision=0)
        except ModelRegistryConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["catalog/model", second_model]))

    winners = [value for value in results if isinstance(value, dict)]
    assert len(winners) == 1
    assert sum(isinstance(value, ModelRegistryConflict) for value in results) == 1
    snapshot = registry.snapshot()
    assert snapshot["revision"] == 1
    assert snapshot["profiles"] == [winners[0]["profile"]]
    assert snapshot["profiles"][0]["record_revision"] == 1


def test_create_rechecks_revision_after_waiting_for_the_owner_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waiting create cannot confirm or overwrite an intervening full update."""
    registry = ModelRegistry("default", user_data_root=tmp_path)
    registry.save(_route(), expected_revision=0)
    waiting = Event()
    original_acquire = NamedLock.acquire

    def acquire(lock: NamedLock) -> object:
        waiting.set()
        return original_acquire(lock)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with NamedLock(registry.lock_root, "model-registry"):
            monkeypatch.setattr(NamedLock, "acquire", acquire)
            future = pool.submit(registry.create, _route(), expected_revision=1)
            assert waiting.wait(timeout=5)
            # Simulate the mutation held by the competing owner, under its lock.
            state = registry._read()
            state["profiles"]["daily"]["parameters"] = {"temperature": 0.75}
            state["revision"] = 2
            registry._write(state)
            before = registry.path.read_bytes()
        with pytest.raises(ModelRegistryConflict, match="stale"):
            future.result(timeout=5)

    assert registry.path.read_bytes() == before


def test_create_does_not_shadow_alias_or_accept_update_fields(tmp_path: Path) -> None:
    registry = ModelRegistry("default", user_data_root=tmp_path)
    registry.save({**_route(), "model_profile_id": "target"}, expected_revision=0)
    registry.set_alias("daily", "target", expected_revision=1)
    with pytest.raises(ModelRegistryConflict, match="reserved"):
        registry.create(_route(), expected_revision=2)
    with pytest.raises(ValueError, match="creation fields"):
        registry.create({**_route(), "enabled": True}, expected_revision=2)
    assert registry.resolve("daily")["resolved_profile_id"] == "target"


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
                "enabled": True,
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
    assert registry.rollback_migration(result["migration_id"])["rolled_back"]
    assert registry.snapshot()["profiles"] == []
