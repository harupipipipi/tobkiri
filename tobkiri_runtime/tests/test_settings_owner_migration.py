"""Typed storage-owner cutover: preconditions, idempotency and fencing.

These tests exercise ``FrontendSettingsStore.adopt_legacy_document`` and the
legacy client's ``migrate_to_owner`` seam on isolated ``tmp_path`` documents.
No live user data, running app or Profile storage is touched; the live
cutover remains a separate native acceptance.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import threading
import time

import pytest

from ecosystem.tobkiri_ui_settings_pack.runtime.store import (
    FrontendSettingsCorruptError,
    FrontendSettingsIdempotencyConflict,
    FrontendSettingsRevisionConflict,
    FrontendSettingsStore,
    MUTATION_RECEIPTS_KEY,
    OWNER_MIGRATIONS_KEY,
    REVISION_KEY,
    STATE_REVISIONS_KEY,
)
from ecosystem.tobkiri_ui_settings_pack.runtime import store as settings_store


MIGRATION_ID = "settings-owner-migration-0001"


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _legacy_document() -> dict:
    return {
        "general": {"language": "ja", "private": "legacy-only"},
        "models": {"preferred_model": "legacy-model", "google_api_key": "secret"},
        REVISION_KEY: 4,
        STATE_REVISIONS_KEY: {"models": 3},
        MUTATION_RECEIPTS_KEY: {"legacy-receipt": {"fingerprint": "fp", "result": {}}},
    }


def test_same_path_cutover_records_owner_migration(tmp_path: Path) -> None:
    path = tmp_path / "defaultspack" / "shared" / "frontend_settings.json"
    _write(path, _legacy_document())
    before = path.read_bytes()
    owner = FrontendSettingsStore(path)
    result = owner.adopt_legacy_document(
        migration_id=MIGRATION_ID,
        legacy_writer_state="stopped",
        expected_source_revision=4,
    )
    assert result == {
        "migration_id": MIGRATION_ID,
        "mode": "same_path",
        "source_digest": "sha256:" + __import__("hashlib").sha256(before).hexdigest(),
        "source_revision": 4,
        "document_revision": 5,
        "migrated": True,
        "idempotent_replay": False,
        "legacy_retired": None,
    }
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted[REVISION_KEY] == 5
    record = persisted[OWNER_MIGRATIONS_KEY][MIGRATION_ID]
    assert record["legacy_writer_state"] == "stopped"
    assert record["result"]["document_revision"] == 5
    # A same-path cutover retires nothing: the owner file is the document.
    assert list(tmp_path.glob("**/*.migrated-*")) == []


def test_explicit_owner_path_is_also_a_same_path_transfer(tmp_path: Path) -> None:
    path = tmp_path / "shared" / "frontend_settings.json"
    _write(path, {"general": {"language": "en"}, REVISION_KEY: 2})
    owner = FrontendSettingsStore(path)
    result = owner.adopt_legacy_document(
        migration_id=MIGRATION_ID,
        legacy_writer_state="authorization_revoked",
        expected_source_revision=2,
        expected_destination_revision=2,
        legacy_path=path,
    )
    assert result["mode"] == "same_path"
    assert result["document_revision"] == 3


def test_distinct_legacy_document_is_adopted_and_retired(tmp_path: Path) -> None:
    legacy = tmp_path / "bundle" / "user_data" / "shared" / "frontend_settings.json"
    owner_path = tmp_path / "user-data" / "defaultspack" / "shared" / "frontend_settings.json"
    legacy_document = _legacy_document()
    _write(legacy, legacy_document)
    owner = FrontendSettingsStore(owner_path)
    result = owner.adopt_legacy_document(
        migration_id=MIGRATION_ID,
        legacy_writer_state="stopped",
        expected_source_revision=4,
        legacy_path=legacy,
    )
    assert result["mode"] == "adopted"
    assert result["document_revision"] == 1
    assert result["legacy_retired"] is True
    # The adopted document keeps the legacy lineage wholesale, including
    # private fields, logical revisions and receipts; the owner adds only its
    # own revision and the migration record.
    adopted = json.loads(owner_path.read_text(encoding="utf-8"))
    assert adopted["general"] == legacy_document["general"]
    assert adopted["models"] == legacy_document["models"]
    assert adopted[STATE_REVISIONS_KEY] == {"models": 3}
    assert adopted[MUTATION_RECEIPTS_KEY] == legacy_document[MUTATION_RECEIPTS_KEY]
    assert adopted[REVISION_KEY] == 1
    assert adopted[OWNER_MIGRATIONS_KEY][MIGRATION_ID]["result"]["migrated"] is True
    # The original path is retired beside its bytes so a stale writer cannot
    # silently continue the original document; nothing is deleted.
    assert not legacy.exists()
    retired = list(legacy.parent.glob("frontend_settings.json.migrated-*"))
    assert len(retired) == 1
    assert json.loads(retired[0].read_text(encoding="utf-8")) == legacy_document


def test_adoption_respects_expected_destination_revision(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    owner_path = tmp_path / "owner.json"
    _write(legacy, {"general": {"language": "ja"}, REVISION_KEY: 0})
    owner = FrontendSettingsStore(owner_path)
    owner.update(lambda current: {**current, "general": {"language": "en"}})
    result = owner.adopt_legacy_document(
        migration_id=MIGRATION_ID,
        legacy_writer_state="stopped",
        expected_source_revision=0,
        expected_destination_revision=1,
        legacy_path=legacy,
    )
    assert result["document_revision"] == 2
    with pytest.raises(FrontendSettingsRevisionConflict):
        owner.adopt_legacy_document(
            migration_id="settings-owner-migration-0002",
            legacy_writer_state="stopped",
            expected_source_revision=0,
            expected_destination_revision=5,
            legacy_path=legacy,
        )


def test_migration_replay_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, _legacy_document())
    owner = FrontendSettingsStore(path)
    first = owner.adopt_legacy_document(
        migration_id=MIGRATION_ID,
        legacy_writer_state="stopped",
        expected_source_revision=4,
    )
    settled = path.read_bytes()
    replay = owner.adopt_legacy_document(
        migration_id=MIGRATION_ID,
        legacy_writer_state="stopped",
        expected_source_revision=4,
    )
    assert replay["idempotent_replay"] is True
    assert {k: v for k, v in replay.items() if k != "idempotent_replay"} == {
        k: v for k, v in first.items() if k != "idempotent_replay"
    }
    assert path.read_bytes() == settled


def test_migration_replay_after_source_retirement(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    _write(legacy, _legacy_document())
    owner = FrontendSettingsStore(tmp_path / "owner.json")
    first = owner.adopt_legacy_document(
        migration_id=MIGRATION_ID,
        legacy_writer_state="stopped",
        expected_source_revision=4,
        legacy_path=legacy,
    )
    assert not legacy.exists()
    replay = owner.adopt_legacy_document(
        migration_id=MIGRATION_ID,
        legacy_writer_state="stopped",
        expected_source_revision=4,
        legacy_path=legacy,
    )
    assert replay["idempotent_replay"] is True
    assert replay["document_revision"] == first["document_revision"]


def test_migration_id_cannot_be_reused_for_other_inputs(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, _legacy_document())
    owner = FrontendSettingsStore(path)
    owner.adopt_legacy_document(
        migration_id=MIGRATION_ID,
        legacy_writer_state="stopped",
        expected_source_revision=4,
    )
    for changed in (
        {"expected_source_revision": 5},
        {"legacy_writer_state": "authorization_revoked"},
        {"legacy_path": tmp_path / "elsewhere.json"},
    ):
        with pytest.raises(FrontendSettingsIdempotencyConflict):
            owner.adopt_legacy_document(
                migration_id=MIGRATION_ID,
                legacy_writer_state=changed.pop("legacy_writer_state", "stopped"),
                expected_source_revision=changed.pop("expected_source_revision", 4),
                **changed,
            )


@pytest.mark.parametrize(
    "writer_state",
    ["active", "running", "", None, "STOPPED", "revoked", "unknown", True],
)
def test_migration_requires_legacy_writer_attestation(
    tmp_path: Path, writer_state: object
) -> None:
    path = tmp_path / "settings.json"
    _write(path, _legacy_document())
    before = path.read_bytes()
    owner = FrontendSettingsStore(path)
    with pytest.raises(PermissionError, match="precondition"):
        owner.adopt_legacy_document(
            migration_id=MIGRATION_ID,
            legacy_writer_state=writer_state,
            expected_source_revision=4,
        )
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expected_source_revision": 5},
        {"expected_source_revision": -1},
        {"expected_source_revision": "4"},
        {"expected_source_revision": True},
        {"migration_id": "short"},
        {"migration_id": "x" * 257},
        {"migration_id": ""},
    ],
)
def test_migration_rejects_invalid_typed_inputs(tmp_path: Path, kwargs: dict) -> None:
    path = tmp_path / "settings.json"
    _write(path, _legacy_document())
    before = path.read_bytes()
    owner = FrontendSettingsStore(path)
    call = {
        "migration_id": MIGRATION_ID,
        "legacy_writer_state": "stopped",
        "expected_source_revision": 4,
        **kwargs,
    }
    with pytest.raises((ValueError, FrontendSettingsRevisionConflict)):
        owner.adopt_legacy_document(**call)
    assert path.read_bytes() == before


def test_migration_rejects_unavailable_or_control_paths(tmp_path: Path) -> None:
    owner_path = tmp_path / "owner.json"
    _write(owner_path, {REVISION_KEY: 0})
    owner = FrontendSettingsStore(owner_path)
    for bad in (
        tmp_path / "missing.json",
        tmp_path,
        owner.backup_path,
        owner.lock_path,
    ):
        with pytest.raises(ValueError):
            owner.adopt_legacy_document(
                migration_id=MIGRATION_ID,
                legacy_writer_state="stopped",
                expected_source_revision=0,
                legacy_path=bad,
            )
    symlink = tmp_path / "linked.json"
    try:
        symlink.symlink_to(owner_path)
    except OSError:
        pytest.skip("symlink unavailable")
    with pytest.raises(ValueError):
        owner.adopt_legacy_document(
            migration_id=MIGRATION_ID,
            legacy_writer_state="stopped",
            expected_source_revision=0,
            legacy_path=symlink,
        )


def test_migration_never_repairs_a_corrupt_source(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{not json", encoding="utf-8")
    owner_path = tmp_path / "owner.json"
    owner = FrontendSettingsStore(owner_path)
    with pytest.raises(FrontendSettingsCorruptError):
        owner.adopt_legacy_document(
            migration_id=MIGRATION_ID,
            legacy_writer_state="stopped",
            expected_source_revision=0,
            legacy_path=legacy,
        )
    assert legacy.read_text(encoding="utf-8") == "{not json"
    assert not owner_path.exists()
    non_object = tmp_path / "array.json"
    non_object.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(FrontendSettingsCorruptError):
        owner.adopt_legacy_document(
            migration_id=MIGRATION_ID,
            legacy_writer_state="stopped",
            expected_source_revision=0,
            legacy_path=non_object,
        )


def test_migration_lock_wait_obeys_cancellation_and_deadline(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, _legacy_document())
    owner = FrontendSettingsStore(path)
    thread_lock = settings_store._thread_lock(path)
    thread_lock.acquire()
    cancellation = threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                owner.adopt_legacy_document,
                migration_id=MIGRATION_ID,
                legacy_writer_state="stopped",
                expected_source_revision=4,
                lock_cancellation=cancellation,
                lock_deadline=time.monotonic() + 30,
            )
            time.sleep(0.05)
            cancellation.set()
            with pytest.raises(InterruptedError):
                future.result(timeout=5)
            # The thread lock is held by this thread; a competing wait must
            # honor a finite deadline rather than blocking forever.
            future = pool.submit(
                owner.adopt_legacy_document,
                migration_id=MIGRATION_ID,
                legacy_writer_state="stopped",
                expected_source_revision=4,
                lock_deadline=time.monotonic() + 0.2,
            )
            with pytest.raises(TimeoutError):
                future.result(timeout=5)
    finally:
        thread_lock.release()
    assert OWNER_MIGRATIONS_KEY not in json.loads(path.read_text(encoding="utf-8"))


def test_live_legacy_writer_blocks_adoption_until_deadline(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy" / "frontend_settings.json"
    _write(legacy, _legacy_document())
    owner = FrontendSettingsStore(tmp_path / "owner" / "frontend_settings.json")
    legacy_store = FrontendSettingsStore(legacy)
    with legacy_store._locked():
        with pytest.raises(TimeoutError):
            owner.adopt_legacy_document(
                migration_id=MIGRATION_ID,
                legacy_writer_state="stopped",
                expected_source_revision=4,
                legacy_path=legacy,
                lock_deadline=time.monotonic() + 0.15,
            )
    assert legacy.exists()
    assert not owner.path.exists()


def test_migration_metadata_stays_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, _legacy_document())
    owner = FrontendSettingsStore(path)
    owner.adopt_legacy_document(
        migration_id=MIGRATION_ID,
        legacy_writer_state="stopped",
        expected_source_revision=4,
    )
    current = owner.read_snapshot()
    assert OWNER_MIGRATIONS_KEY in current
    # Later owner writes preserve the record; the record is not a writable
    # section and a document CAS cannot delete or replace it.
    committed = owner.compare_and_swap_fields(
        {"general": {"language": "en"}},
        allowed_fields={"general": frozenset({"language"})},
        expected_revision=current[REVISION_KEY],
    )
    assert committed["document_revision"] == 6
    assert OWNER_MIGRATIONS_KEY in owner.read_snapshot()
    with pytest.raises(PermissionError):
        owner.compare_and_swap_fields(
            {OWNER_MIGRATIONS_KEY: {"forged": {}}},
            allowed_fields={OWNER_MIGRATIONS_KEY: frozenset({"forged"})},
            expected_revision=6,
        )
    proposal = deepcopy(owner.read_snapshot())
    del proposal[OWNER_MIGRATIONS_KEY]
    with pytest.raises(ValueError, match="owner metadata"):
        owner.compare_and_swap_document(proposal, expected_revision=6)


def test_unbound_client_cannot_migrate(tmp_path: Path) -> None:
    from ecosystem.defaultspack.domain.frontend_settings_store import (
        FrontendSettingsStore as Client,
    )

    legacy = tmp_path / "legacy.json"
    _write(legacy, _legacy_document())
    before = legacy.read_bytes()
    client = Client(legacy)
    with pytest.raises(RuntimeError, match="explicit settings owner binding"):
        client.migrate_to_owner(
            migration_id=MIGRATION_ID,
            legacy_writer_state="stopped",
            expected_source_revision=4,
        )
    assert legacy.read_bytes() == before
    assert list(tmp_path.iterdir()) == [legacy]


def test_client_migrate_to_owner_same_path_and_cross_path(tmp_path: Path) -> None:
    from ecosystem.defaultspack.domain.frontend_settings_store import (
        FrontendSettingsStore as Client,
    )

    shared = tmp_path / "shared" / "frontend_settings.json"
    _write(shared, {"general": {"language": "ja"}, REVISION_KEY: 1})
    owner = FrontendSettingsStore(shared)
    client = Client(shared, owner=owner)
    result = client.migrate_to_owner(
        migration_id=MIGRATION_ID,
        legacy_writer_state="stopped",
        expected_source_revision=1,
    )
    assert result["mode"] == "same_path"
    assert result["document_revision"] == 2

    bundle = tmp_path / "bundle" / "user_data" / "shared" / "frontend_settings.json"
    _write(bundle, _legacy_document())
    bound_client = Client(bundle, owner=owner)
    adopted = bound_client.migrate_to_owner(
        migration_id="settings-owner-migration-0002",
        legacy_writer_state="authorization_revoked",
        expected_source_revision=4,
    )
    assert adopted["mode"] == "adopted"
    assert adopted["legacy_retired"] is True
    assert not bundle.exists()
    persisted = owner.read_snapshot()
    assert persisted["models"]["preferred_model"] == "legacy-model"
    assert len(persisted[OWNER_MIGRATIONS_KEY]) == 2


def test_migration_record_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    _write(path, _legacy_document())
    owner = FrontendSettingsStore(path)
    for index in range(20):
        document = json.loads(path.read_text(encoding="utf-8"))
        owner.adopt_legacy_document(
            migration_id=f"settings-owner-migration-{index:04d}",
            legacy_writer_state="stopped",
            expected_source_revision=document[REVISION_KEY],
        )
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert len(persisted[OWNER_MIGRATIONS_KEY]) == 16
    assert "settings-owner-migration-0000" not in persisted[OWNER_MIGRATIONS_KEY]
    assert "settings-owner-migration-0019" in persisted[OWNER_MIGRATIONS_KEY]
