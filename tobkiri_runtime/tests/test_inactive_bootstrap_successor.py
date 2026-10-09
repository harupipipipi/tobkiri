"""A completed inert import requires an exact, immutable Setup successor review."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from core_runtime.active_profile_store_v4 import ActiveProfileStore
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture
from core_runtime.bootstrap.profile_publication import ProfilePublicationJournal
from core_runtime.bootstrap.profile_registry import register_bootstrap_definition
from core_runtime.development_host_handover_guard import (
    read_handover_journal,
    write_handover_journal,
)
from core_runtime.profile_definition_store_v4 import (
    ProfileDefinitionStore,
    ProfileDefinitionStoreConflict,
)
from ecosystem.defaultspack.defaultspack.development_host_data import (
    _sanitize_definition,
)
from ecosystem.defaultspack.defaultspack.setup_contract import (
    validate_defaults_setup_payload,
)
from ecosystem.defaultspack.domain.runtime_v4 import ProfileResolutionDenied
from tests.test_profile_architecture_review_c import _packaged_catalog_revision
from tobkiri_protocol.canonical import canonical_digest


@pytest.fixture(scope="module")
def catalogs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Any]:
    """Stage public, synthetic before/after artifacts with the real generator."""
    root = tmp_path_factory.mktemp("inactive-successor-public-bundles")
    return (
        _packaged_catalog_revision(root / "before", b"before"),
        _packaged_catalog_revision(root / "after", b"after"),
    )


@pytest.fixture
def imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalogs: tuple[Any, Any]
) -> tuple[Path, ProfileDefinitionStore, dict[str, Any]]:
    """Use the real immutable import CAS and an authenticated synthetic fence."""
    before, after = catalogs
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setattr(profile_capture, "_bundle_root", lambda _base=None: after.root)
    store = ProfileDefinitionStore(user_data)
    store.bootstrap_defaults(after.profiles["defaults"])
    prior = store.snapshot()
    inert = _sanitize_definition(before.profiles["defaults"])

    def completed(snapshot: dict[str, Any]) -> None:
        write_handover_journal(
            user_data,
            {
                "stage": "completed",
                "destination_profile_generation": snapshot["generation"],
                "destination_profile_snapshot_digest": canonical_digest(snapshot),
                "synthetic_test_only": True,
            },
        )

    store.import_profile_successors(
        [inert],
        expected_store_generation=prior["generation"],
        expected_snapshot_digest=canonical_digest(prior),
        on_committed=completed,
    )
    return user_data, store, inert


def test_import_requires_a_new_review_and_appends_the_exact_successor(
    imported: tuple[Path, ProfileDefinitionStore, dict[str, Any]],
    catalogs: tuple[Any, Any],
) -> None:
    """Only the fresh Host review authorizes new pins and immutable succession."""
    user_data, store, inert = imported
    original = store.snapshot()
    journal_before = read_handover_journal(user_data)
    with pytest.raises(ProfileDefinitionStoreConflict, match="conflicts"):
        register_bootstrap_definition(user_data, catalogs[1].profiles["defaults"])
    review_catalog, confirmation = profile_capture.prepare_bootstrap_profile_review()
    predecessor = confirmation["definition_predecessor"]
    assert predecessor["definition_digest"] == canonical_digest(inert)
    assert predecessor["profile_revision"] == canonical_digest(inert)
    assert predecessor["snapshot_digest"] == canonical_digest(original)
    assert predecessor["store_generation"] == original["generation"]
    assert predecessor["handover_digest"] == canonical_digest(journal_before)
    assert store.snapshot() == original
    runtime = profile_capture.require_profile_runtime()
    listing = runtime.setup_listing(
        review_catalog,
        confirmation,
        active=False,
        activation_denied=False,
        denial_diagnostic=None,
    )
    assert validate_defaults_setup_payload(listing) == listing
    assert listing["state"] == "review_required"

    active = profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    registered = store.get_profile("defaults")
    assert registered is not None
    assert registered.parent_revision == canonical_digest(inert)
    assert registered.profile == catalogs[1].profiles["defaults"]
    assert (
        store.snapshot()["profiles"][0]["revisions"][:-1]
        == original["profiles"][0]["revisions"]
    )
    assert store.bootstrap_state() == {
        "state": "not_required",
        "template_profile_revision": None,
    }
    assert read_handover_journal(user_data) == journal_before
    assert (
        ActiveProfileStore(user_data).require().profile_revision
        == active.resolved.plan["profile_revision"]
    )
    assert not ProfilePublicationJournal(user_data).exists()
    assert profile_capture.capture_bootstrap_profile().activation == active.activation
    before_replay = store.snapshot()
    with pytest.raises(ProfileResolutionDenied, match="stale or tampered"):
        profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    assert store.snapshot() == before_replay


@pytest.mark.parametrize(
    "mutation", ["edit", "tombstone", "other_profile", "partial", "fence_tamper"]
)
def test_changed_or_untrusted_import_cannot_use_a_prior_review(
    imported: tuple[Path, ProfileDefinitionStore, dict[str, Any]],
    mutation: str,
) -> None:
    """Registry changes and invalid migration evidence cannot become approvals."""
    user_data, store, inert = imported
    confirmation = profile_capture.prepare_bootstrap_profile_confirmation()
    if mutation == "edit":
        changed = deepcopy(inert)
        changed["display_name"] = "Edited after import"
        store.update_profile("defaults", changed)
    elif mutation == "tombstone":
        store.delete_profile("defaults")
    elif mutation == "other_profile":
        changed = deepcopy(inert)
        changed["profile_id"] = "another"
        store.create_profile(changed)
    elif mutation == "partial":
        value = read_handover_journal(user_data)
        assert value is not None
        value["stage"] = "data-staged"
        write_handover_journal(user_data, value)
    else:
        path = user_data / "development-host-handover.json"
        path.write_bytes(path.read_bytes().replace(b'"completed"', b'"tampered!"'))
    before = store.snapshot()
    with pytest.raises(
        (ProfileResolutionDenied, ProfileDefinitionStoreConflict, ValueError)
    ):
        profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    assert store.snapshot() == before
    assert not (
        user_data / "workspaces" / "defaults" / "activation" / "active.json"
    ).exists()


@pytest.mark.parametrize(
    "field",
    [
        "definition_digest",
        "snapshot_digest",
        "handover_digest",
        "store_generation",
        "pack_ids",
    ],
)
def test_predecessor_tamper_is_not_authority(
    imported: tuple[Path, ProfileDefinitionStore, dict[str, Any]],
    field: str,
) -> None:
    """Even a re-digested renderer body must equal current Host-issued facts."""
    _user_data, store, _inert = imported
    confirmation = profile_capture.prepare_bootstrap_profile_confirmation()
    binding = confirmation["definition_predecessor"]
    binding[field] = (
        999
        if field == "store_generation"
        else []
        if field == "pack_ids"
        else "sha256:" + "0" * 64
    )
    confirmation["confirmation_digest"] = canonical_digest(
        {
            key: value
            for key, value in confirmation.items()
            if key != "confirmation_digest"
        }
    )
    before = store.snapshot()
    with pytest.raises(ProfileResolutionDenied, match="stale or tampered"):
        profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    assert store.snapshot() == before


@pytest.mark.parametrize("race", ["definition", "host_active", "workspace_active"])
def test_race_after_review_validation_denies_before_definition_append(
    imported: tuple[Path, ProfileDefinitionStore, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    """The publication journal is not a waiver of current predecessor fences."""
    user_data, store, inert = imported
    confirmation = profile_capture.prepare_bootstrap_profile_confirmation()
    original_begin = ProfilePublicationJournal.begin
    observed: list[dict[str, Any]] = []

    def begin(self: ProfilePublicationJournal, **kwargs: Any) -> None:
        original_begin(self, **kwargs)
        if race == "definition":
            changed = deepcopy(inert)
            changed["display_name"] = "Concurrent edit"
            store.update_profile("defaults", changed)
        else:
            path = (
                user_data / "profiles" / "active.json"
                if race == "host_active"
                else user_data
                / "workspaces"
                / "defaults"
                / "activation"
                / "active.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        observed.append(store.snapshot())

    monkeypatch.setattr(ProfilePublicationJournal, "begin", begin)
    with pytest.raises((ProfileResolutionDenied, ProfileDefinitionStoreConflict)):
        profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    assert store.snapshot() == observed[0]


def test_stale_authority_epoch_denies_successor_before_append(
    imported: tuple[Path, ProfileDefinitionStore, dict[str, Any]],
) -> None:
    """The predecessor field cannot renew a stale Authority confirmation."""
    user_data, store, _inert = imported
    confirmation = profile_capture.prepare_bootstrap_profile_confirmation()
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        authority.advance_security_epoch(reason="synthetic stale confirmation")
    before = store.snapshot()
    with pytest.raises(ProfileResolutionDenied, match="stale or tampered"):
        profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    assert store.snapshot() == before


def test_missing_handover_cannot_authorize_registry_replacement(
    imported: tuple[Path, ProfileDefinitionStore, dict[str, Any]],
    catalogs: tuple[Any, Any],
) -> None:
    """A plain registry cannot manufacture the new imported-predecessor path."""
    user_data, store, _inert = imported
    (user_data / "development-host-handover.json").unlink()
    (user_data / "development-host-handover.key").unlink()
    confirmation = profile_capture.prepare_bootstrap_profile_confirmation()
    assert "definition_predecessor" not in confirmation
    before = store.snapshot()
    with pytest.raises(ProfileDefinitionStoreConflict, match="conflicts"):
        register_bootstrap_definition(user_data, catalogs[1].profiles["defaults"])
    assert store.snapshot() == before


def test_changed_bundle_requires_a_new_exact_review(
    imported: tuple[Path, ProfileDefinitionStore, dict[str, Any]],
    catalogs: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import evidence cannot authorize a different catalog or Shell target."""
    _user_data, store, _inert = imported
    confirmation = profile_capture.prepare_bootstrap_profile_confirmation()
    monkeypatch.setattr(
        profile_capture, "_bundle_root", lambda _base=None: catalogs[0].root
    )
    before = store.snapshot()
    with pytest.raises(ProfileResolutionDenied, match="stale or tampered"):
        profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    assert store.snapshot() == before


def test_registry_cas_rejects_an_edit_after_final_snapshot_check(
    imported: tuple[Path, ProfileDefinitionStore, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual immutable append still compares the reviewed registry generation."""
    _user_data, store, inert = imported
    confirmation = profile_capture.prepare_bootstrap_profile_confirmation()
    original_update = ProfileDefinitionStore.update_profile
    seen: list[dict[str, Any]] = []

    def update(self: ProfileDefinitionStore, *args: Any, **kwargs: Any) -> Any:
        if not seen:
            changed = deepcopy(inert)
            changed["display_name"] = "Edit before CAS"
            original_update(self, "defaults", changed)
            seen.append(self.snapshot())
        return original_update(self, *args, **kwargs)

    monkeypatch.setattr(ProfileDefinitionStore, "update_profile", update)
    with pytest.raises(ProfileDefinitionStoreConflict):
        profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    assert store.snapshot() == seen[0]


def test_target_swap_after_confirmation_check_denies_before_publication(
    imported: tuple[Path, ProfileDefinitionStore, dict[str, Any]],
    catalogs: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source registered after approval must be the actually reviewed target."""
    user_data, store, _inert = imported
    confirmation = profile_capture.prepare_bootstrap_profile_confirmation()
    original_catalog = profile_capture.host_profile_catalog

    def catalog(base_dir: Path | None = None) -> Any:
        result = original_catalog(base_dir)
        monkeypatch.setattr(
            profile_capture, "_bundle_root", lambda _base=None: catalogs[0].root
        )
        return result

    monkeypatch.setattr(profile_capture, "host_profile_catalog", catalog)
    before = store.snapshot()
    with pytest.raises(ProfileResolutionDenied, match="target changed"):
        profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    assert store.snapshot() == before
    assert not ProfilePublicationJournal(user_data).exists()
