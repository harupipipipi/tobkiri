from __future__ import annotations

import json
import hashlib
import os
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
from domain.frontend_settings_catalog import SettingsCatalogInputs  # noqa: E402
from domain import frontend_settings_store as settings_module  # noqa: E402


def test_corrupt_diagnostic_is_owned_locked_private_and_preserves_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    content = b"{invalid\xff"
    store.path.write_bytes(content)
    store.path.chmod(0o600)
    original_write = store._atomic_write_bytes
    lock = settings_module._thread_lock(store.path)

    def try_other_thread() -> bool:
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        return acquired

    def checked_write(path: Path, value: bytes, *, mode: int) -> None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(try_other_thread).result(timeout=5) is False
        original_write(path, value, mode=mode)

    monkeypatch.setattr(store, "_atomic_write_bytes", checked_write)
    for _ in range(2):
        with pytest.raises(FrontendSettingsCorruptError):
            store.read(preserve_corrupt=True)
    copies = list(tmp_path.glob("settings.json.corrupt-*.bak"))
    assert len(copies) == 1
    assert copies[0].read_bytes() == store.path.read_bytes() == content
    assert hashlib.sha256(content).hexdigest() in copies[0].name
    if os.name != "nt":
        assert copies[0].stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_backup_collision_is_not_overwritten(tmp_path: Path) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    content = b"{broken"
    store.path.write_bytes(content)
    backup = store.path.with_name(
        f"settings.json.corrupt-{hashlib.sha256(content).hexdigest()}.bak"
    )
    backup.write_bytes(b"keep existing evidence")
    with pytest.raises(FrontendSettingsCorruptError, match="backup differs"):
        store.read(preserve_corrupt=True)
    assert backup.read_bytes() == b"keep existing evidence"
    assert store.path.read_bytes() == content


def test_snapshot_and_ordinary_corrupt_read_do_not_create_diagnostics(tmp_path: Path) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    store.path.write_bytes(b"{broken")
    for read in (store.read_snapshot, store.read):
        with pytest.raises(FrontendSettingsCorruptError):
            read()
    assert not list(tmp_path.glob("*.corrupt-*.bak"))


@pytest.mark.parametrize("has_models", [False, True])
def test_explicit_settings_catalog_never_discovers_ambient_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_models: bool
) -> None:
    registry = FrontendRegistry(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("ambient discovery is not permitted")

    for name in (
        "_external_io_template_catalog",
        "_input_profile_options",
        "_output_profile_options",
        "_model_options",
        "_model_route_options",
    ):
        monkeypatch.setattr(registry, name, forbidden)
    monkeypatch.setattr("domain.frontend.registry.provider_key_status", forbidden)
    options = [{"value": "captured-model", "label": "Captured Model"}] if has_models else []
    inputs = SettingsCatalogInputs(
        input_templates=[],
        output_templates=[],
        input_profile_options=[],
        output_profile_options=[],
        model_options=options,
        model_route_options=options,
        api_key_status=[],
    )
    sections = registry._settings_sections([], [], template_catalog={}, inputs=inputs)
    fields = {
        (section["id"], field["id"]): field
        for section in sections
        for field in section["fields"]
    }
    assert fields["general", "composer_placeholder"]["default"] == "メッセージを入力..."
    assert fields["models", "preferred_model"]["options"] == options
    assert fields["models", "model_api_routes"]["options"] == options
    assert fields["models", "model_api_routes"]["api_keys"] == []
    fields["models", "preferred_model"]["options"].append({"value": "changed"})
    assert inputs.model_options == options
    assert fields["models", "model_api_routes"]["options"] == options
    assert list(tmp_path.iterdir()) == []


def _process_update(path_text: str, key: str, value: str) -> None:
    store = FrontendSettingsStore(Path(path_text))
    store.update(lambda current: {**current, key: value})


def test_snapshot_does_not_create_a_missing_store(tmp_path: Path) -> None:
    store = FrontendSettingsStore(tmp_path / "absent" / "settings.json")
    assert store.read_snapshot() == {}
    assert list(tmp_path.iterdir()) == []


def test_snapshot_leaves_valid_settings_and_revision_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    document = {"general": {"language": "ja"}, REVISION_KEY: 7}
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    snapshot = FrontendSettingsStore(path).read_snapshot()
    assert snapshot == document
    snapshot["general"]["language"] = "en"
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_snapshot_does_not_recover_corrupt_settings_from_backup(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = FrontendSettingsStore(path)
    path.write_text("{broken", encoding="utf-8")
    store.backup_path.write_text('{"general": {"language": "ja"}}', encoding="utf-8")
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir()}
    with pytest.raises(FrontendSettingsCorruptError, match="snapshot is corrupt"):
        store.read_snapshot()
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir()} == before


@pytest.mark.parametrize("content", [b"[]", b"null", b"42", b"\xff"])
def test_snapshot_rejects_invalid_documents_without_changes(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(content)
    with pytest.raises(FrontendSettingsCorruptError, match="snapshot is corrupt"):
        FrontendSettingsStore(path).read_snapshot()
    assert path.read_bytes() == content
    assert list(tmp_path.iterdir()) == [path]


def test_snapshot_propagates_access_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")

    def denied(path: Path) -> dict:
        raise PermissionError("read denied")

    monkeypatch.setattr(store, "_load_mapping", denied)
    with pytest.raises(PermissionError, match="read denied"):
        store.read_snapshot()
    assert list(tmp_path.iterdir()) == []


def test_snapshot_observes_complete_documents_during_updates(tmp_path: Path) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    store.update(lambda _: {"counter": 0, "mirror": 0})

    def write(index: int) -> None:
        store.update(lambda _: {"counter": index, "mirror": index})

    def read(_: int) -> None:
        value = store.read_snapshot()
        assert value["counter"] == value["mirror"]
        assert isinstance(value[REVISION_KEY], int)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(write, index) for index in range(30)]
        futures += [pool.submit(read, index) for index in range(100)]
        for future in futures:
            future.result()


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


def test_deepthink_value_and_revision_use_one_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ModelRuntimeSettingsService(pack_root=tmp_path)
    state_ref = "defaultspack:models.deepthink_enabled"
    snapshots = iter([
        {"models": {"deepthink_enabled": False}, "_state_revisions": {state_ref: 3}},
        {"models": {"deepthink_enabled": True}, "_state_revisions": {state_ref: 4}},
    ])
    reads = []

    def read() -> dict:
        snapshot = next(snapshots)
        reads.append(snapshot)
        return snapshot

    monkeypatch.setattr(service._settings_store, "read", read)
    # Keep this interleaving test focused on snapshot identity; actual model
    # normalization is exercised by the service and endpoint regressions.
    monkeypatch.setattr(service, "default_model_settings", lambda: {})
    monkeypatch.setattr(service, "refresh_models_settings", lambda value: value)
    first = service.get_deepthink_enabled()
    assert (first["enabled"], first["revision"]) == (False, 3)
    assert len(reads) == 1
    second = service.get_deepthink_enabled()
    assert (second["enabled"], second["revision"]) == (True, 4)
    assert len(reads) == 2


@pytest.mark.parametrize("revision", [None, True, -1, "3", 1.0, {}, []])
def test_snapshot_revision_rejects_non_revision_values(revision: object) -> None:
    assert settings_module.settings_state_revision(
        {"_state_revisions": {"state": revision}}, "state"
    ) == 0


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


def test_idempotency_receipt_cannot_replay_for_another_state(tmp_path: Path) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    store.mutate_state(
        "state:first", lambda current: (current, {"enabled": True}),
        idempotency_key="same-key-123", request_fingerprint="same-payload",
    )
    original = store.path.read_bytes()

    def forbidden(current: dict) -> tuple[dict, dict]:
        pytest.fail("a retained identity must not execute another mutation")

    with pytest.raises(FrontendSettingsIdempotencyConflict):
        store.mutate_state(
            "state:second", forbidden,
            idempotency_key="same-key-123", request_fingerprint="same-payload",
        )
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("receipts", [
    None, [], "invalid", {"same-key-123": None},
    {"same-key-123": {"fingerprint": "same-payload", "result": []}},
])
def test_corrupt_receipt_never_becomes_permission_to_repeat_a_write(
    tmp_path: Path, receipts: object,
) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    original = json.dumps({"_mutation_receipts": receipts, "custom": {"keep": 1}}).encode()
    store.path.write_bytes(original)

    def forbidden(current: dict) -> tuple[dict, dict]:
        pytest.fail("corrupt receipts must not permit mutation")

    with pytest.raises(FrontendSettingsCorruptError):
        store.mutate_state(
            "state:first", forbidden,
            idempotency_key="same-key-123", request_fingerprint="same-payload",
        )
    assert store.path.read_bytes() == original
    assert not store.backup_path.exists()


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
