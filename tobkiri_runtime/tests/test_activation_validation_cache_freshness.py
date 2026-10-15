"""Warm schema successes never replace live activation integrity checks."""

from __future__ import annotations

import copy
import json
import os
import shutil
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

import core_runtime.profile_content_projection as projections
import ecosystem.defaultspack.domain.runtime_v4.service as runtime_service
from core_runtime.authority.v4 import AuthorityStore, AuthorityStoreError
from core_runtime.profile_content_projection import ProfileContentProjectionError
from core_runtime.profile_projection_migration import RETIREMENTS
from ecosystem.defaultspack.domain.runtime_v4 import (
    ActiveDefaultProfile,
    ActivationStore,
    BundledCatalog,
    ProfileResolutionDenied,
    ResolvedDefaultProfile,
)
from ecosystem.defaultspack.domain.runtime_v4._record_validation_cache import (
    _RecordValidationCache,
)
from tests.conformance_support.packaged_profile import load_packaged_profile_catalog
from tests.test_defaultspack_runtime_v4 import _authority, _resolve
from tobkiri_protocol.canonical import canonical_digest, canonical_json


@dataclass
class _WarmActivation:
    store: ActivationStore
    authority: AuthorityStore
    catalog: BundledCatalog
    active: ActiveDefaultProfile
    cache: _RecordValidationCache

    @property
    def pointer_path(self) -> Path:
        return self.store.state_root / "active.json"

    @property
    def envelope_path(self) -> Path:
        pointer = json.loads(self.pointer_path.read_bytes())
        return self.store.state_root / "activations" / pointer["envelope_path"]


def _assert_records_warm(fixture: _WarmActivation) -> None:
    expected = {
        "profile": fixture.active.resolved.profile,
        "profile_lock": fixture.active.resolved.lock,
        "resolved_plan": fixture.active.resolved.plan,
    }
    for kind, record in expected.items():
        assert any(
            key[0] == kind
            and key[2].validator is runtime_service.validate_document
            and key[3] == canonical_json(record)
            for key in fixture.cache._entries
        ), f"{kind} must be warm for the actual service validator"


@pytest.fixture(scope="module")
def packaged_records() -> tuple[BundledCatalog, ResolvedDefaultProfile]:
    """Resolve a real content selection over the official packaged artifacts."""
    catalog = load_packaged_profile_catalog()
    source = copy.deepcopy(catalog.profiles["defaults"])
    projection = next(
        item.resolved() for item in RETIREMENTS if item.legacy_pack_id == "rumi_local_agent_pack"
    )
    source["content_projections"] = [projection]
    source = runtime_service.validate_document(source, "profile")
    catalog = replace(catalog, profiles={**catalog.profiles, "defaults": source})
    resolved = _resolve(catalog)
    assert resolved.plan["content_projections"] == [projection]
    return catalog, resolved


@pytest.fixture
def warm_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packaged_records: tuple[BundledCatalog, ResolvedDefaultProfile],
) -> Iterator[_WarmActivation]:
    """Warm the real load path against private, initially identical resources."""
    catalog, resolved = packaged_records
    runtime_root = tmp_path / "runtime"
    projection_root = runtime_root / "profile_projections"
    shutil.copytree(projections.PROJECTION_ROOT, projection_root)
    monkeypatch.setattr(projections, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(projections, "PROJECTION_ROOT", projection_root)
    assert catalog.artifact_root is not None
    artifact_root = tmp_path / "artifacts"
    shutil.copytree(catalog.artifact_root, artifact_root)
    catalog = replace(catalog, artifact_root=artifact_root)
    cache = _RecordValidationCache()
    monkeypatch.setattr(runtime_service, "_RECORD_VALIDATION_CACHE", cache)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    try:
        store = ActivationStore(
            tmp_path / "state",
            workspace,
            profile_id="defaults",
            authority=authority,
            catalog=catalog,
        )
        store.activate(
            resolved,
            activation_id="activation:defaults-warm-validation",
            created_at="2026-08-10T00:00:00Z",
        )
        active = store.load_active_snapshot()
        fixture = _WarmActivation(store, authority, catalog, active, cache)
        _assert_records_warm(fixture)
        assert store.load_active_snapshot() == active
        _assert_records_warm(fixture)
        yield fixture
    finally:
        authority.close()


def _rewrite_same_size_and_mtime(path: Path) -> None:
    before = path.stat()
    original = path.read_bytes()
    assert original
    replacement = original[:-1] + bytes([original[-1] ^ 1])
    path.write_bytes(replacement)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert path.read_bytes() != original


def _redirect_same_bytes(path: Path) -> None:
    moved = path.with_name(path.name + ".original")
    path.rename(moved)
    path.symlink_to(moved)


def test_warm_reload_skips_only_the_three_record_validations(
    warm_activation: _WarmActivation, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = warm_activation
    validate = runtime_service.validate_document
    graph = fixture.store._validate_record_graph
    calls: Counter[str] = Counter()

    def tracked(document: Any, kind: str, **kwargs: Any) -> dict[str, Any]:
        calls[kind] += 1
        return validate(document, kind, **kwargs)

    def checked_graph(*records: Any) -> None:
        calls["graph"] += 1
        graph(*records)

    monkeypatch.setattr(runtime_service, "validate_document", tracked)
    monkeypatch.setattr(fixture.store, "_validate_record_graph", checked_graph)
    # The changed validator identity starts cold, even for identical bytes.
    fixture.store.load_active_snapshot()
    _assert_records_warm(fixture)
    before = calls.copy()
    assert all(before[kind] == 1 for kind in ("profile", "profile_lock", "resolved_plan"))
    fixture.store.load_active_snapshot()
    assert calls - before == {"activation": 2, "graph": 2}


@pytest.mark.parametrize("mutation", ["same_size_mtime", "symlink"])
def test_warm_records_recheck_live_projection_content(
    warm_activation: _WarmActivation, mutation: str
) -> None:
    """Unchanged Profile/Lock/Plan bytes cannot authorize altered projections."""
    fixture = warm_activation
    selection = fixture.active.resolved.plan["content_projections"][0]
    root = projections.RUNTIME_ROOT / selection["artifact_root"]
    path = next(path for path in sorted(root.rglob("*")) if path.is_file())
    pointer_before = fixture.pointer_path.read_bytes()
    envelope_before = fixture.envelope_path.read_bytes()
    if mutation == "same_size_mtime":
        _rewrite_same_size_and_mtime(path)
        expected = "stale"
    else:
        _redirect_same_bytes(path)
        expected = "symlink"

    with pytest.raises(ProfileContentProjectionError, match=expected):
        fixture.store.load_active_snapshot()
    assert fixture.pointer_path.read_bytes() == pointer_before
    assert fixture.envelope_path.read_bytes() == envelope_before
    _assert_records_warm(fixture)


@pytest.mark.parametrize("mutation", ["same_size_mtime", "symlink"])
def test_warm_records_reverify_selected_executable(
    warm_activation: _WarmActivation, mutation: str
) -> None:
    """A successful prior load never memoizes the selected executable check."""
    fixture = warm_activation
    variant = fixture.catalog.shells["shell.tauri.default"]["launch"]["variants"][0]
    assert fixture.catalog.artifact_root is not None
    path = fixture.catalog.artifact_root / variant["entrypoint"]
    if mutation == "same_size_mtime":
        _rewrite_same_size_and_mtime(path)
    else:
        _redirect_same_bytes(path)
    with pytest.raises(ProfileResolutionDenied, match="artifact rejected"):
        fixture.store.load_active_snapshot()
    _assert_records_warm(fixture)


@pytest.mark.parametrize("change", ["retirement", "epoch"])
def test_warm_records_recheck_live_authority(warm_activation: _WarmActivation, change: str) -> None:
    """Retirement is visible even with identical pointer, records, and epoch."""
    fixture = warm_activation
    pointer_before = fixture.pointer_path.read_bytes()
    envelope_before = fixture.envelope_path.read_bytes()
    epoch_before = fixture.authority.security_epoch
    if change == "retirement":
        reservation = fixture.authority.active_activation_reservation(
            fixture.active.activation["activation_id"]
        )
        assert reservation is not None
        fixture.authority.transition_activation(
            reservation["reservation_id"],
            expected_state="active",
            new_state="retired",
        )
        assert fixture.authority.security_epoch == epoch_before
    else:
        fixture.authority.advance_security_epoch("warm cache regression")
        assert fixture.authority.security_epoch > epoch_before

    with pytest.raises(ProfileResolutionDenied, match="authority, fence, or SecurityEpoch"):
        fixture.store.load_active_snapshot()
    assert fixture.pointer_path.read_bytes() == pointer_before
    assert fixture.envelope_path.read_bytes() == envelope_before
    _assert_records_warm(fixture)


def test_warm_second_locked_read_observes_retirement_during_artifact_check(
    warm_activation: _WarmActivation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real artifact success cannot carry authority past an intervening retire."""
    fixture = warm_activation
    pointer_before = fixture.pointer_path.read_bytes()
    envelope_before = fixture.envelope_path.read_bytes()
    epoch_before = fixture.authority.security_epoch
    reservation = fixture.authority.active_activation_reservation(
        fixture.active.activation["activation_id"]
    )
    assert reservation is not None
    verify = fixture.store._verify_selected_artifact
    completed: list[bool] = []

    def verify_then_retire(profile: Mapping[str, Any], **kwargs: Any) -> None:
        verify(profile, **kwargs)
        completed.append(True)
        fixture.authority.transition_activation(
            reservation["reservation_id"],
            expected_state="active",
            new_state="retired",
        )

    monkeypatch.setattr(fixture.store, "_verify_selected_artifact", verify_then_retire)
    with pytest.raises(ProfileResolutionDenied, match="authority, fence, or SecurityEpoch"):
        fixture.store.load_active_snapshot()
    assert completed == [True]
    assert fixture.authority.security_epoch == epoch_before
    assert fixture.pointer_path.read_bytes() == pointer_before
    assert fixture.envelope_path.read_bytes() == envelope_before
    _assert_records_warm(fixture)


@pytest.mark.parametrize("target", ["path", "_guard_path"])
def test_warm_records_reject_replaced_authority_persistence(
    warm_activation: _WarmActivation, target: str
) -> None:
    """Same-byte DB and lifecycle-lock replacements lose their fence."""
    fixture = warm_activation
    path = getattr(fixture.authority, target)
    moved = path.with_name(path.name + ".original")
    path.rename(moved)
    path.write_bytes(moved.read_bytes())
    path.chmod(0o600)
    try:
        with pytest.raises(AuthorityStoreError):
            fixture.store.load_active_snapshot()
        _assert_records_warm(fixture)
    finally:
        path.unlink()
        moved.rename(path)


def test_warm_records_do_not_bypass_audit_crypto_key_fence(
    warm_activation: _WarmActivation,
) -> None:
    """The real audit crypto path still rejects a same-byte key replacement.

    Activation reservation reads do not use key material. This deliberately
    exercises the public authenticated audit read on the same AuthorityStore.
    """
    fixture = warm_activation
    assert any(
        event["event_type"] == "activation" and event["event_state"] == "active"
        for event in fixture.authority.audit_events()
    )
    path = fixture.authority.key_path
    moved = path.with_name(path.name + ".original")
    path.rename(moved)
    path.write_bytes(moved.read_bytes())
    path.chmod(0o600)
    try:
        with pytest.raises(AuthorityStoreError, match="encryption key path is unsafe"):
            fixture.authority.audit_events()
        _assert_records_warm(fixture)
    finally:
        path.unlink()
        moved.rename(path)


@pytest.mark.parametrize("target", ["pointer", "envelope", "lock"])
def test_warm_records_reject_symlinked_activation_state(
    warm_activation: _WarmActivation, target: str
) -> None:
    """Warm records do not bypass secure pointer, snapshot, or lock reads."""
    fixture = warm_activation
    paths = {
        "pointer": fixture.pointer_path,
        "envelope": fixture.envelope_path,
        "lock": next(fixture.store.state_root.glob(".activation-*.lock")),
    }
    _redirect_same_bytes(paths[target])
    with pytest.raises(ProfileResolutionDenied, match="unavailable"):
        fixture.store.load_active_snapshot()
    _assert_records_warm(fixture)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fencing_token", 999),
        ("security_epoch", 999),
        ("profile_authority_snapshot_digest", "sha256:" + "0" * 64),
    ],
)
def test_warm_records_do_not_cache_activation_authority_fields(
    warm_activation: _WarmActivation, field: str, value: object
) -> None:
    """Rehashing an envelope does not authenticate forged activation fields."""
    fixture = warm_activation
    envelope_path = fixture.envelope_path
    envelope = json.loads(envelope_path.read_bytes())
    envelope["activation"][field] = value
    envelope_path.write_bytes(canonical_json(envelope) + b"\n")
    pointer = json.loads(fixture.pointer_path.read_bytes())
    pointer["envelope_digest"] = canonical_digest(envelope)
    fixture.pointer_path.write_bytes(canonical_json(pointer) + b"\n")

    with pytest.raises(ProfileResolutionDenied, match="authority, fence, or SecurityEpoch"):
        fixture.store.load_active_snapshot()
    _assert_records_warm(fixture)


def test_warm_schema_success_does_not_replace_record_graph_validation(
    warm_activation: _WarmActivation,
) -> None:
    """Even cached, schema-valid records must satisfy cross-record digests."""
    fixture = warm_activation
    envelope_path = fixture.envelope_path
    envelope = json.loads(envelope_path.read_bytes())
    altered_lock = copy.deepcopy(envelope["lock"])
    altered_lock["profile_revision"] = "sha256:" + "0" * 64
    fixture.cache.validate(
        altered_lock, "profile_lock", validator=runtime_service.validate_document
    )
    assert any(
        key[0] == "profile_lock" and key[3] == canonical_json(altered_lock)
        for key in fixture.cache._entries
    )
    envelope["lock"] = altered_lock
    envelope_path.write_bytes(canonical_json(envelope) + b"\n")
    pointer = json.loads(fixture.pointer_path.read_bytes())
    pointer["envelope_digest"] = canonical_digest(envelope)
    fixture.pointer_path.write_bytes(canonical_json(pointer) + b"\n")

    with pytest.raises(ProfileResolutionDenied, match="ProfileLock or ResolvedPlan is stale"):
        fixture.store.load_active_snapshot()


def test_warm_records_reject_same_metadata_envelope_mutation(
    warm_activation: _WarmActivation,
) -> None:
    """Envelope bytes are reread even when inode, length, and mtime survive."""
    fixture = warm_activation
    path = fixture.envelope_path
    before = path.stat()
    original = path.read_bytes()
    altered = original.replace(b'"fencing_token":1', b'"fencing_token":2', 1)
    assert altered != original and len(altered) == len(original)
    path.write_bytes(altered)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    with pytest.raises(ProfileResolutionDenied, match="envelope digest changed"):
        fixture.store.load_active_snapshot()
    _assert_records_warm(fixture)
