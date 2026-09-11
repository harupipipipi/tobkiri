"""Data-only settings commits retain owner metadata and reject stale writes."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
from threading import Barrier

import pytest

from ecosystem.tobkiri_ui_settings_pack.runtime.store import (
    FrontendSettingsCorruptError,
    FrontendSettingsRevisionConflict,
    FrontendSettingsStore,
    MUTATION_RECEIPTS_KEY,
    REVISION_KEY,
    STATE_REVISIONS_KEY,
)
from ecosystem.defaultspack.domain.frontend_settings_client import update_settings_document


def test_legacy_path_cannot_reactivate_an_unbound_settings_owner(tmp_path):
    from ecosystem.defaultspack.domain.frontend_settings_store import FrontendSettingsStore as Client

    path = tmp_path / "legacy.json"
    path.write_text('{"private": "unchanged"}')
    client = Client(path)
    for invoke in (
        client.read,
        client.read_snapshot,
        lambda: client.compare_and_swap_document({}, expected_revision=0),
        lambda: client.compare_and_swap_state("state", {}, {}, expected_document_revision=0),
    ):
        with pytest.raises(RuntimeError, match="explicit settings owner binding"):
            invoke()
    assert path.read_text() == '{"private": "unchanged"}'
    assert list(tmp_path.iterdir()) == [path]


def test_explicit_settings_port_not_legacy_path_selects_persistence(tmp_path):
    from ecosystem.defaultspack.domain.frontend_settings_store import FrontendSettingsStore as Client

    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"private": "unchanged"}')
    owner = FrontendSettingsStore(tmp_path / "owner.json")
    client = Client(legacy, owner=owner)
    result = update_settings_document(client, lambda value: {**value, "display": "saved"})
    assert result == {"display": "saved", REVISION_KEY: 1}
    assert client.read_snapshot() == owner.read_snapshot() == result
    assert legacy.read_text() == '{"private": "unchanged"}'


def test_data_only_commit_preserves_unknown_values_and_finite_numbers(tmp_path: Path) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    before = store.update(lambda _: {"unknown": {"kept": [1, 0.8]}, "models": {}})
    proposal = deepcopy(before)
    proposal["models"]["preferred_model"] = "model-1"
    result = store.compare_and_swap_document(proposal, expected_revision=1)
    assert result == {**proposal, REVISION_KEY: 2}
    assert store.read_snapshot() == result
    assert json.loads(store.backup_path.read_text()) == before
    assert proposal[REVISION_KEY] == 1


def test_field_patch_preserves_private_state_without_returning_it(tmp_path):
    store = FrontendSettingsStore(tmp_path / "settings.json")
    before = store.update(lambda _: {
        "general": {"language": "en", "private": "not-public"},
        "unknown": {"credential": "not-public"},
        STATE_REVISIONS_KEY: {"existing-state": 4},
        MUTATION_RECEIPTS_KEY: {"existing-receipt": {"private": True}},
    })
    patch = {"general": {"language": "ja"}}
    result = store.compare_and_swap_fields(
        patch, allowed_fields={"general": frozenset({"language"})},
        expected_revision=1,
    )
    assert result == {"values": patch, "document_revision": 2}
    assert store.read_snapshot() == {
        **before, "general": {"language": "ja", "private": "not-public"},
        REVISION_KEY: 2,
    }
    assert json.loads(store.backup_path.read_text()) == before
    result["values"]["general"]["language"] = "modified-result"
    assert patch == {"general": {"language": "ja"}}
    assert store.read_snapshot()["general"]["language"] == "ja"


@pytest.mark.parametrize("changes", [
    {"general": {"private": "forged"}},
    {"unknown": {"language": "ja"}},
    {REVISION_KEY: {"language": "ja"}},
    {STATE_REVISIONS_KEY: {"language": "ja"}},
    {MUTATION_RECEIPTS_KEY: {"language": "ja"}},
])
def test_field_patch_denies_scope_before_opening_storage(tmp_path, changes):
    store = FrontendSettingsStore(tmp_path / "absent" / "settings.json")
    with pytest.raises(PermissionError, match="write policy"):
        store.compare_and_swap_fields(
            changes, allowed_fields={"general": frozenset({"language"})},
            expected_revision=0,
        )
    assert not store.path.parent.exists()


def test_field_patch_rechecks_revision_after_owner_merge(tmp_path, monkeypatch):
    store = FrontendSettingsStore(tmp_path / "settings.json")
    before = store.update(lambda _: {"general": {"language": "en"}})
    commit = store.compare_and_swap_document

    def concurrent_commit(document, *, expected_revision):
        commit({**before, "other": "concurrent"}, expected_revision=1)
        return commit(document, expected_revision=expected_revision)

    monkeypatch.setattr(store, "compare_and_swap_document", concurrent_commit)
    with pytest.raises(FrontendSettingsRevisionConflict):
        store.compare_and_swap_fields(
            {"general": {"language": "ja"}},
            allowed_fields={"general": frozenset({"language"})}, expected_revision=1,
        )
    assert store.read_snapshot() == {**before, "other": "concurrent", REVISION_KEY: 2}


def test_field_patch_does_not_repair_corrupt_primary(tmp_path):
    store = FrontendSettingsStore(tmp_path / "settings.json")
    store.path.write_bytes(b"broken")
    store.backup_path.write_text('{}')
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(FrontendSettingsCorruptError):
        store.compare_and_swap_fields(
            {"general": {"language": "ja"}},
            allowed_fields={"general": frozenset({"language"})}, expected_revision=0,
        )
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("revision", [True, False, -1, 0.0, "0", None])
def test_invalid_revision_does_not_create_storage(tmp_path: Path, revision: object) -> None:
    store = FrontendSettingsStore(tmp_path / "absent" / "settings.json")
    with pytest.raises(ValueError):
        store.compare_and_swap_document({}, expected_revision=revision)
    assert not store.path.parent.exists()


@pytest.mark.parametrize("document", [{1: "invalid key"}, {"tuple": (1, 2)}, {"n": float("nan")}])
def test_non_json_proposal_does_not_create_storage(tmp_path: Path, document: dict) -> None:
    store = FrontendSettingsStore(tmp_path / "absent" / "settings.json")
    with pytest.raises(ValueError):
        store.compare_and_swap_document(document, expected_revision=0)
    assert not store.path.parent.exists()


@pytest.mark.parametrize("field", [REVISION_KEY, STATE_REVISIONS_KEY, MUTATION_RECEIPTS_KEY])
@pytest.mark.parametrize("remove", [False, True])
def test_document_commit_cannot_edit_or_remove_owner_metadata(
    tmp_path: Path, field: str, remove: bool,
) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    before = store.update(lambda _: {
        STATE_REVISIONS_KEY: {"state": 3},
        MUTATION_RECEIPTS_KEY: {"receipt": {"fingerprint": "retained"}},
    })
    proposal = deepcopy(before)
    if remove:
        proposal.pop(field)
    else:
        proposal[field] = "forged"
    original = store.path.read_bytes()
    with pytest.raises(ValueError, match="owner metadata"):
        store.compare_and_swap_document(proposal, expected_revision=1)
    assert store.path.read_bytes() == original


def test_independent_writers_have_one_cas_winner_without_lost_update(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = FrontendSettingsStore(path)
    before = store.update(lambda _: {"unknown": "kept"})
    barrier = Barrier(2)

    def write(field: str) -> str:
        independent = FrontendSettingsStore(path)
        barrier.wait(timeout=10)
        try:
            independent.compare_and_swap_document({**before, field: True}, expected_revision=1)
            return field
        except FrontendSettingsRevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("ui", "models")))
    assert results.count("conflict") == 1
    snapshot = store.read_snapshot()
    assert snapshot[REVISION_KEY] == 2
    assert snapshot["unknown"] == "kept"
    missing = "models" if "ui" in snapshot else "ui"
    result = store.compare_and_swap_document({**snapshot, missing: True}, expected_revision=2)
    assert result["ui"] is result["models"] is True
    assert result[REVISION_KEY] == 3


def test_corrupt_primary_is_not_repaired_by_document_commit(tmp_path: Path) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    first = store.update(lambda _: {"known": "first"})
    store.update(lambda value: {**value, "known": "second"})
    store.path.write_bytes(b"{corrupt")
    backup = store.backup_path.read_bytes()
    with pytest.raises(FrontendSettingsCorruptError):
        store.compare_and_swap_document(first, expected_revision=1)
    assert store.path.read_bytes() == b"{corrupt"
    assert store.backup_path.read_bytes() == backup


def test_state_proposal_retains_receipts_without_receiving_a_callback(tmp_path: Path) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    initial = store.update(lambda _: {"models": {"enabled": False}, "unknown": "kept"})
    proposal = {**initial, "models": {"enabled": True}}
    first = store.compare_and_swap_state(
        "models.enabled", proposal, {"enabled": True},
        expected_document_revision=1, expected_revision=0,
        idempotency_key="request-one", request_fingerprint="toggle-first",
    )
    replay = store.compare_and_swap_state(
        "models.enabled", proposal, {"enabled": True},
        expected_document_revision=1, expected_revision=0,
        idempotency_key="request-one", request_fingerprint="toggle-first",
    )
    assert replay == {**first, "idempotent_replay": True}
    saved = store.read_snapshot()
    assert saved[REVISION_KEY] == 2
    assert saved[STATE_REVISIONS_KEY] == {"models.enabled": 1}
    assert saved["unknown"] == "kept"


def test_state_receipt_survives_owner_restart_without_reapplying(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    first_owner = FrontendSettingsStore(path)
    initial = first_owner.update(
        lambda _: {"models": {"enabled": False}, "unknown": {"kept": True}}
    )
    proposal = {**initial, "models": {"enabled": True}}
    first = first_owner.compare_and_swap_state(
        "models.enabled",
        proposal,
        {"enabled": True},
        expected_document_revision=1,
        expected_revision=0,
        idempotency_key="request-after-restart",
        request_fingerprint="toggle-after-restart",
    )
    committed_bytes = path.read_bytes()

    restarted_owner = FrontendSettingsStore(path)
    replay = restarted_owner.compare_and_swap_state(
        "models.enabled",
        proposal,
        {"enabled": True},
        expected_document_revision=1,
        expected_revision=0,
        idempotency_key="request-after-restart",
        request_fingerprint="toggle-after-restart",
    )

    assert replay == {**first, "idempotent_replay": True}
    assert path.read_bytes() == committed_bytes
    saved = restarted_owner.read_snapshot()
    assert saved[REVISION_KEY] == 2
    assert saved[STATE_REVISIONS_KEY] == {"models.enabled": 1}
    assert saved["unknown"] == {"kept": True}


def test_document_retry_only_after_proven_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    commit = store.compare_and_swap_document
    calls = []

    def race(document: dict, *, expected_revision: int) -> dict:
        calls.append(expected_revision)
        if len(calls) == 1:
            store.update(lambda _: {"concurrent": "kept"})
        return commit(document, expected_revision=expected_revision)

    monkeypatch.setattr(store, "compare_and_swap_document", race)
    result = update_settings_document(store, lambda current: {**current, "updated": True})
    assert result == {"concurrent": "kept", "updated": True, REVISION_KEY: 2}
    assert calls == [0, 1]


def test_ambiguous_commit_never_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FrontendSettingsStore(tmp_path / "settings.json")
    commit = store.compare_and_swap_document
    calls = []

    def lose(document: dict, *, expected_revision: int) -> dict:
        calls.append(expected_revision)
        commit(document, expected_revision=expected_revision)
        raise OSError("reply lost after write")

    monkeypatch.setattr(store, "compare_and_swap_document", lose)
    with pytest.raises(OSError, match="reply lost"):
        update_settings_document(store, lambda current: {**current, "updated": True})
    assert calls == [0]
    assert store.read_snapshot() == {"updated": True, REVISION_KEY: 1}
