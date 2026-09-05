from __future__ import annotations

import json
import multiprocessing
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.frontend_settings_store import (  # noqa: E402
    FrontendSettingsCorruptError,
    FrontendSettingsIdempotencyConflict,
    FrontendSettingsRevisionConflict,
    FrontendSettingsStore,
    REVISION_KEY,
)
from domain.ai_client.model_runtime_settings import (  # noqa: E402
    ModelRuntimeSettingsService,
)
from domain.frontend.registry import FrontendRegistry  # noqa: E402


def _process_update(path_text: str, key: str, value: str) -> None:
    store = FrontendSettingsStore(Path(path_text))
    store.update(lambda current: {**current, key: value})


def test_concurrent_thread_updates_preserve_disjoint_keys(tmp_path: Path) -> None:
    path = tmp_path / "frontend_settings.json"
    store = FrontendSettingsStore(path)

    def update(index: int) -> None:
        store.update(lambda current: {**current, f"key_{index}": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(40)))

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert all(saved[f"key_{index}"] == index for index in range(40))
    assert saved[REVISION_KEY] == 40


def test_registry_and_model_service_updates_share_one_transaction(
    tmp_path: Path,
) -> None:
    registry = FrontendRegistry(pack_root=tmp_path)
    models = ModelRuntimeSettingsService(pack_root=tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        registry_update = pool.submit(
            registry.update_settings,
            {"preview": {"auto_open": True}},
        )
        model_update = pool.submit(
            models.update_settings,
            {"preferred_model_group": "group-b"},
        )
        registry_update.result()
        model_update.result()

    saved = FrontendSettingsStore(
        tmp_path / "user_data" / "shared" / "frontend_settings.json"
    ).read()
    assert saved["preview"]["auto_open"] is True
    assert saved["models"]["preferred_model_group"] == "group-b"
    assert saved[REVISION_KEY] == 2


def test_concurrent_process_updates_preserve_disjoint_keys(tmp_path: Path) -> None:
    path = tmp_path / "frontend_settings.json"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_process_update,
            args=(str(path), f"process_{index}", str(index)),
        )
        for index in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    saved = FrontendSettingsStore(path).read()
    assert all(saved[f"process_{index}"] == str(index) for index in range(6))
    assert saved[REVISION_KEY] == 6


def test_reader_never_observes_partial_json(tmp_path: Path) -> None:
    path = tmp_path / "frontend_settings.json"
    store = FrontendSettingsStore(path)
    store.update(lambda current: {**current, "counter": 0})

    def write(index: int) -> None:
        store.update(lambda current: {**current, "counter": index})

    def read(_: int) -> None:
        saved = store.read()
        assert isinstance(saved["counter"], int)

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(write, index) for index in range(50)]
        futures.extend(pool.submit(read, index) for index in range(150))
        for future in futures:
            future.result()


def test_state_mutation_is_revisioned_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "frontend_settings.json"
    store = FrontendSettingsStore(path)

    def enable(current):
        current["models"] = {"deepthink_enabled": True}
        return current, {"enabled": True}

    first = store.mutate_state(
        "defaultspack:models.deepthink_enabled",
        enable,
        expected_revision=0,
        idempotency_key="deepthink-request-1",
        request_fingerprint="enabled:true",
    )
    replay = store.mutate_state(
        "defaultspack:models.deepthink_enabled",
        enable,
        expected_revision=0,
        idempotency_key="deepthink-request-1",
        request_fingerprint="enabled:true",
    )

    assert first["revision"] == 1
    assert first["idempotent_replay"] is False
    assert replay["revision"] == 1
    assert replay["idempotent_replay"] is True
    assert store.state_revision("defaultspack:models.deepthink_enabled") == 1


def test_state_mutation_rejects_stale_revision_and_key_reuse(tmp_path: Path) -> None:
    store = FrontendSettingsStore(tmp_path / "frontend_settings.json")

    def enable(current):
        current["models"] = {"deepthink_enabled": True}
        return current, {"enabled": True}

    store.mutate_state(
        "defaultspack:models.deepthink_enabled",
        enable,
        idempotency_key="deepthink-request-1",
        request_fingerprint="enabled:true",
    )

    with pytest.raises(FrontendSettingsRevisionConflict):
        store.mutate_state(
            "defaultspack:models.deepthink_enabled",
            enable,
            expected_revision=0,
        )
    with pytest.raises(FrontendSettingsIdempotencyConflict):
        store.mutate_state(
            "defaultspack:models.deepthink_enabled",
            enable,
            idempotency_key="deepthink-request-1",
            request_fingerprint="enabled:false",
        )


def test_settings_endpoint_field_patch_preserves_unrelated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = tmp_path / "frontend_settings.json"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(settings_path))
    store = FrontendSettingsStore(settings_path)
    store.mutate_state(
        "defaultspack:models.deepthink_enabled",
        lambda current: (
            {
                **current,
                "models": {"deepthink_enabled": True},
                "theme": {"font_size": 14},
            },
            {"enabled": True},
        ),
    )

    from blocks.ui.settings import run

    result = run(
        {
            "_method": "PUT",
            "patches": [{"section": "theme", "field": "font_size", "value": 16}],
        },
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["values"]["models"]["deepthink_enabled"] is True
    assert result["data"]["values"]["theme"]["font_size"] == 16
    assert store.state_revision("defaultspack:models.deepthink_enabled") == 1


def test_corrupt_primary_recovers_from_valid_backup(tmp_path: Path) -> None:
    path = tmp_path / "frontend_settings.json"
    store = FrontendSettingsStore(path)
    store.update(lambda current: {**current, "value": "first"})
    store.update(lambda current: {**current, "value": "second"})
    path.write_text("{broken", encoding="utf-8")

    recovered = store.read()

    assert recovered["value"] == "first"
    assert json.loads(path.read_text(encoding="utf-8")) == recovered


def test_corrupt_primary_without_backup_is_explicit(tmp_path: Path) -> None:
    path = tmp_path / "frontend_settings.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(FrontendSettingsCorruptError):
        FrontendSettingsStore(path).read()


def test_atomic_write_propagates_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "frontend_settings.json"
    store = FrontendSettingsStore(path)

    def denied(_source: Path, _destination: Path) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("os.replace", denied)
    with pytest.raises(PermissionError, match="denied"):
        store.update(lambda current: {**current, "value": True})
