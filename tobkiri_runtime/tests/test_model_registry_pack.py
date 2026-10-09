"""External-QA-oriented contract tests for the Wave 5 model registry."""

from __future__ import annotations

import hashlib
import json
import os
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


@pytest.mark.parametrize('enabled', ['false', 'true', 0, 1, None, [], {}])
def test_model_owner_rejects_non_boolean_enabled_before_writing(tmp_path, enabled):
    registry = ModelRegistry('default', user_data_root=tmp_path)
    with pytest.raises(ValueError, match='enabled must be a boolean'):
        registry.save({**_route(), 'enabled': enabled}, expected_revision=0)
    assert not registry.path.exists()


def test_model_parameters_preserve_token_limits_and_tokenizer_options(tmp_path):
    registry = ModelRegistry('default', user_data_root=tmp_path)
    parameters = {'max_tokens': 4096, 'max_output_tokens': 1024,
                  'max_completion_tokens': 2048, 'token_healing': True,
                  'eos_token': '<end>', 'temperature': 0.3}
    saved = registry.save({**_route(), 'parameters': parameters}, expected_revision=0)
    assert saved['profile']['parameters'] == parameters
    assert registry.get('daily')['parameters'] == parameters
    assert registry.resolve('daily')['profile']['parameters'] == parameters


@pytest.mark.parametrize('key', ['token', 'access_token', 'refreshToken',
                                 'api-token', 'Authorization', 'api_key', 'password'])
def test_model_parameters_still_filter_recognizable_credential_fields(tmp_path, key):
    registry = ModelRegistry('default', user_data_root=tmp_path)
    saved = registry.save({**_route(), 'parameters': {key: 'fixture-only',
                                                    'max_tokens': 64}},
                          expected_revision=0)
    assert saved['profile']['parameters'] == {'max_tokens': 64}


def test_atomic_registry_temporary_is_exclusive_and_does_not_clobber_collision(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ecosystem.rumi_model_registry_pack.runtime import registry as module

    nonce = "a" * 32
    monkeypatch.setattr(module.uuid, "uuid4", lambda: SimpleNamespace(hex=nonce))
    destination = tmp_path / "legacy-model-registry.json"
    collision = tmp_path / f".{nonce}.tmp"
    collision.write_bytes(b"unrelated existing file")
    with pytest.raises(FileExistsError):
        module._atomic_json(destination, {"value": "new"})
    assert collision.read_bytes() == b"unrelated existing file"
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows MAX_PATH regression")
def test_windows_model_registry_backup_uses_bounded_temporary_name(tmp_path):
    from ecosystem.rumi_model_registry_pack.runtime.registry import _atomic_json

    padding = 216 - len(str(tmp_path)) - 1
    if not 1 <= padding <= 200:
        pytest.skip("temporary base is outside the bounded native fixture range")
    root = tmp_path / ("p" * padding)
    destination = root / "legacy-model-registry.json"
    _atomic_json(destination, {"value": "old"})
    _atomic_json(destination, {"value": "new"})
    assert json.loads(destination.read_text()) == {"value": "new"}
    assert sorted(path.name for path in root.iterdir()) == [destination.name]
