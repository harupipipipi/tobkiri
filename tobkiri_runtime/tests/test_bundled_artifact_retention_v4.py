"""Exact bundled retention under real isolated active Authority bindings."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any

import pytest

from core_runtime import bundled_artifact_retention_v4 as retention
from core_runtime.active_profile_store_v4 import (
    ActiveProfileStore,
    ActiveProfileStoreIntegrityError,
)
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture
from tobkiri_protocol.canonical import canonical_digest, canonical_json

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
PACK_ID = "rumi_provider_adapters_pack"


def _bundle_root() -> Path:
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    return packaged_profile_bundle_root()


@dataclass(frozen=True)
class RetentionFixture:
    """Private fixture owner and untrusted suppliers for integration tests."""

    owner: Path
    bundle: Path
    source: Path
    record: dict[str, Any]
    origin: retention.BundledArtifactOrigin
    pin: str


def make_retention_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RetentionFixture:
    """Capture the canonical packaged fixture using real isolated Authority."""
    owner = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(owner))
    profile_capture.capture_default_profile(
        confirmation=profile_capture.prepare_default_profile_confirmation()
    )
    origin = retention.capture_retention_origin(owner)
    source = tmp_path / PACK_ID
    shutil.copytree(RUNTIME_ROOT / "ecosystem" / PACK_ID, source)
    catalog = json.loads((RUNTIME_ROOT / "schemas/pack_v4_catalog.v1.json").read_bytes())
    record = next(row for row in catalog["packs"] if row["pack_id"] == PACK_ID)
    return RetentionFixture(
        owner, _bundle_root(), source, record, origin, dict(origin.pack_pins)[PACK_ID]
    )


@pytest.fixture
def retention_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RetentionFixture:
    return make_retention_fixture(tmp_path, monkeypatch)


def retain_fixture(fixture: RetentionFixture) -> retention.RetainedBundledArtifact:
    """Publish only the fixture's verified bytes into its private owner."""
    return retention.retain_bundled_artifact(
        fixture.origin,
        bundle_root=fixture.bundle,
        source_root=fixture.source,
        source_record=fixture.record,
    )


def _verify(fixture: RetentionFixture) -> Path:
    return retention.verify_retained_bundled_artifact(
        fixture.origin, pack_id=PACK_ID, artifact_digest=fixture.pin
    )


def _rewrite_index(source: Path, mutate: Any) -> None:
    path = source / "artifact-index.v4.json"
    index = json.loads(path.read_bytes())
    mutate(index)
    index["integrity_seal"]["signed_digest"] = canonical_digest(
        {key: value for key, value in index.items() if key != "integrity_seal"}
    )
    path.write_bytes(canonical_json(index))


def _unseal_directory(path: Path) -> None:
    path.chmod(0o700)


def test_retains_exact_bound_bytes_without_recapture(
    retention_fixture: RetentionFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = retention_fixture
    retained = retain_fixture(fixture)
    assert retained.root != fixture.source
    assert retained.artifact_digest == fixture.pin
    assert retained.bundle_digest == fixture.origin.bundle_digest
    assert (retained.root / "pack.v4.json").read_bytes() == (
        fixture.source / "pack.v4.json"
    ).read_bytes()
    assert retained.root.stat().st_mode & 0o777 == 0o500
    assert (retained.root / "pack.v4.json").stat().st_mode & 0o777 == 0o400

    def forbidden_capture(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("retained read must not capture recursively")

    monkeypatch.setattr(profile_capture, "capture_active_profile", forbidden_capture)
    monkeypatch.setattr(retention, "_rename_noreplace", forbidden_capture)
    assert _verify(fixture) == retained.root
    # Idempotent retain validates the winner rather than overwriting it.
    assert retain_fixture(fixture).root == retained.root


@pytest.mark.parametrize(
    "change",
    [
        {"bundle_digest": "sha256:" + "1" * 64},
        {"pack_pins": ((PACK_ID, "sha256:" + "2" * 64),)},
        {"profile_id": "other"},
        {"activation_id": "activation:other"},
        {"plan_digest": "sha256:" + "3" * 64},
        {"security_epoch": 999},
        {"fencing_token": 999},
    ],
)
def test_rejects_replaced_origin(
    retention_fixture: RetentionFixture, change: dict[str, Any]
) -> None:
    forged = replace(retention_fixture.origin, **change)
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="captured origin"):
        retention.assert_retention_origin_current(forged)


def test_requires_exact_requested_pin(retention_fixture: RetentionFixture) -> None:
    fixture = retention_fixture
    with pytest.raises(TypeError, match="artifact_digest"):
        retention.verify_retained_bundled_artifact(fixture.origin, pack_id=PACK_ID)
    for pack_id, pin in ((PACK_ID, "sha256:" + "0" * 64), ("unknown", fixture.pin)):
        with pytest.raises(retention.BundledArtifactRetentionDenied, match="active pin"):
            retention.verify_retained_bundled_artifact(
                fixture.origin, pack_id=pack_id, artifact_digest=pin
            )
    assert not (fixture.owner / "retained-bundled-artifacts-v4").exists()


@pytest.mark.parametrize("kind", ["extra", "role"])
def test_rejects_self_resealed_index_inventory(
    retention_fixture: RetentionFixture, kind: str
) -> None:
    fixture = retention_fixture

    def mutate(index: dict[str, Any]) -> None:
        if kind == "extra":
            (fixture.source / "unexpected.py").write_bytes(b"pass\n")
            index["artifacts"].append(
                {"path": "unexpected.py", "digest": "sha256:" + "0" * 64, "role": "sidecar"}
            )
        else:
            runtime = next(row for row in index["artifacts"] if row["role"] == "runtime")
            runtime["role"] = "sidecar"

    _rewrite_index(fixture.source, mutate)
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="inventory or roles"):
        retain_fixture(fixture)
    assert not (fixture.owner / "retained-bundled-artifacts-v4").exists()


@pytest.mark.parametrize("kind", ["manifest", "record", "runtime", "lock", "projection"])
def test_rejects_tampered_suppliers(
    retention_fixture: RetentionFixture, tmp_path: Path, kind: str
) -> None:
    fixture = retention_fixture
    if kind in {"lock", "projection"}:
        bundle = tmp_path / "untrusted-bundle"
        shutil.copytree(fixture.bundle, bundle)
        fixture = replace(fixture, bundle=bundle)
        path = bundle / ("bundle.lock.json" if kind == "lock" else f"packs/{PACK_ID}.pack.v4.json")
        path.write_bytes(path.read_bytes() + b"\n")
    elif kind == "record":
        fixture = replace(fixture, record={**fixture.record, "description": "tampered"})
    else:
        path = fixture.source / ("pack.v4.json" if kind == "manifest" else "runtime/streaming.py")
        path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(retention.BundledArtifactRetentionDenied):
        retain_fixture(fixture)
    assert not (fixture.owner / "retained-bundled-artifacts-v4").exists()


@pytest.mark.parametrize("kind", ["file", "empty-directory", "deep-empty-directory"])
def test_rejects_extra_cache_entries(retention_fixture: RetentionFixture, kind: str) -> None:
    fixture = retention_fixture
    retained = retain_fixture(fixture)
    _unseal_directory(retained.root)
    if kind == "file":
        (retained.root / "unexpected.py").write_bytes(b"pass\n")
    elif kind == "empty-directory":
        (retained.root / "empty").mkdir()
    else:
        (retained.root / "empty/one/two/three").mkdir(parents=True)
    retained.root.chmod(0o500)
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="extra or unsafe"):
        _verify(fixture)


def test_rejects_mutated_cache_bytes(retention_fixture: RetentionFixture) -> None:
    fixture = retention_fixture
    retained = retain_fixture(fixture)
    path = retained.root / "runtime/streaming.py"
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="artifact bytes"):
        _verify(fixture)


def test_rejects_stale_authority_epoch(retention_fixture: RetentionFixture) -> None:
    fixture = retention_fixture
    retain_fixture(fixture)
    with AuthorityStore(fixture.owner / "authority/v4.sqlite3") as authority:
        authority.advance_security_epoch("isolated retention regression")
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="Authority"):
        _verify(fixture)


def test_rejects_other_owner_and_symlink_source(
    retention_fixture: RetentionFixture, tmp_path: Path
) -> None:
    fixture = retention_fixture
    forged = replace(fixture.origin, user_data_root=tmp_path / "other-owner")
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="captured origin"):
        retention.assert_retention_origin_current(forged)
    link = tmp_path / "source-link"
    link.symlink_to(fixture.source, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        retain_fixture(replace(fixture, source=link))


def test_rejects_source_root_swap_during_read(
    retention_fixture: RetentionFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = retention_fixture
    original_read = retention._read
    swapped = False

    def read(reader: Any, path: str, **kwargs: Any) -> bytes:
        nonlocal swapped
        result = original_read(reader, path, **kwargs)
        if not swapped and path == "artifact-index.v4.json":
            swapped = True
            fixture.source.rename(fixture.source.with_name("removed-source"))
            fixture.source.mkdir()
        return result

    monkeypatch.setattr(retention, "_read", read)
    with pytest.raises((ValueError, OSError)):
        retain_fixture(fixture)
    assert swapped
    assert not (fixture.owner / "retained-bundled-artifacts-v4").exists()


def test_rechecks_authority_before_atomic_publication(
    retention_fixture: RetentionFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = retention_fixture
    original_seal = retention._seal_tree
    changed = False

    def seal(descriptor: int, **kwargs: Any) -> None:
        nonlocal changed
        original_seal(descriptor, **kwargs)
        if not changed:
            changed = True
            with AuthorityStore(fixture.owner / "authority/v4.sqlite3") as authority:
                authority.advance_security_epoch("isolated pre-publication race")

    monkeypatch.setattr(retention, "_seal_tree", seal)
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="Authority"):
        retain_fixture(fixture)
    assert changed
    final = fixture.owner / retention._container_relative(fixture.origin, PACK_ID)
    assert not final.exists()


def test_cache_read_rechecks_authority_after_verification(
    retention_fixture: RetentionFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = retention_fixture
    retain_fixture(fixture)
    original_verify = retention._verify_stored

    def verify(*args: Any, **kwargs: Any) -> Path:
        result = original_verify(*args, **kwargs)
        with AuthorityStore(fixture.owner / "authority/v4.sqlite3") as authority:
            authority.advance_security_epoch("isolated post-verification race")
        return result

    monkeypatch.setattr(retention, "_verify_stored", verify)
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="Authority"):
        _verify(fixture)


@pytest.mark.parametrize("timing", ["before", "after"])
def test_rejects_changed_pointer_generation(
    retention_fixture: RetentionFixture, monkeypatch: pytest.MonkeyPatch, timing: str
) -> None:
    fixture = retention_fixture
    retain_fixture(fixture)

    def change_pointer() -> None:
        store = ActiveProfileStore(fixture.owner)
        pointer = store.require(verify_snapshot=True)
        store.commit(pointer, expected=pointer)

    if timing == "before":
        change_pointer()
    else:
        original = retention._verify_stored

        def verify(*args: Any, **kwargs: Any) -> Path:
            result = original(*args, **kwargs)
            change_pointer()
            return result

        monkeypatch.setattr(retention, "_verify_stored", verify)
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="pointer"):
        _verify(fixture)


def test_rejects_tampered_authority_fence(retention_fixture: RetentionFixture) -> None:
    fixture = retention_fixture
    retain_fixture(fixture)
    # Deliberate corruption of this test's private Authority database only.
    with sqlite3.connect(fixture.owner / "authority/v4.sqlite3") as connection:
        connection.execute(
            "UPDATE activation_reservations SET fencing_token=fencing_token+1 "
            "WHERE activation_id=?",
            (fixture.origin.activation_id,),
        )
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="Authority"):
        _verify(fixture)


def test_rejects_tampered_activation_envelope(retention_fixture: RetentionFixture) -> None:
    fixture = retention_fixture
    retain_fixture(fixture)
    pointer = ActiveProfileStore(fixture.owner).require(verify_snapshot=True)
    path = fixture.owner / pointer.activation_snapshot_path
    envelope = json.loads(path.read_bytes())
    envelope["plan"]["bundle_digest"] = "sha256:" + "0" * 64
    path.write_bytes(canonical_json(envelope))
    with pytest.raises(ActiveProfileStoreIntegrityError, match="snapshot digest"):
        _verify(fixture)


def test_rejects_owner_root_replacement(retention_fixture: RetentionFixture) -> None:
    fixture = retention_fixture
    retain_fixture(fixture)
    moved = fixture.owner.with_name("previous-owner")
    fixture.owner.rename(moved)
    fixture.owner.mkdir(mode=0o700)
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="owner"):
        _verify(fixture)


def test_rejects_cache_parent_swap_during_publication(
    retention_fixture: RetentionFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = retention_fixture
    original = retention._rename_noreplace
    parent = (fixture.owner / retention._container_relative(fixture.origin, PACK_ID)).parent
    moved = parent.with_name("previous-cache-parent")

    def rename(descriptor: int, source: str, destination: str) -> None:
        original(descriptor, source, destination)
        parent.rename(moved)
        parent.mkdir(mode=0o700)

    monkeypatch.setattr(retention, "_rename_noreplace", rename)
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="parent changed"):
        retain_fixture(fixture)
    assert not (parent / PACK_ID).exists()


def test_rejects_unsealed_published_cache(retention_fixture: RetentionFixture) -> None:
    fixture = retention_fixture
    retained = retain_fixture(fixture)
    retained.root.parent.parent.chmod(0o700)
    with pytest.raises(retention.BundledArtifactRetentionDenied, match="unsafe"):
        _verify(fixture)
