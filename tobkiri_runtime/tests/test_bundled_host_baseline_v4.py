"""Frozen bundled baselines survive Source drift without changing active pins."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import stat
from threading import Thread
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from core_runtime import bundled_host_baseline_v4 as baselines
from core_runtime import external_pack_catalog_v4 as external
from core_runtime.active_profile_store_v4 import ActiveProfileStore
from core_runtime.bootstrap import profile_capture
from core_runtime.bundled_artifact_retention_v4 import (
    BundledArtifactRetentionDenied,
    capture_retention_origin,
    verify_retained_bundled_artifact,
)
from core_runtime.pack_artifact_integrity import write_host_install_record
from core_runtime.pack_signature import (
    SIGNED_MANIFEST_RELATIVE,
    build_signed_manifest,
    sign_manifest,
)
from core_runtime.profile_pack_versions import (
    profile_pack_versions,
    select_profile_pack_version,
)
from tests.test_bundled_artifact_retention_v4 import (
    PACK_ID,
    make_retention_fixture,
)
from tests.test_host_extension_dispatch_update_v4 import (
    _approve_gateway_profile_revision,
)
from tests.test_host_extension_update_v4 import _refresh
from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.validation import validate_file


def _publish(fixture: Any) -> baselines.RetainedBundledHostBaseline:
    return baselines.retain_bundled_host_baseline(
        fixture.origin,
        bundle_root=fixture.bundle,
        source_root=fixture.source,
        source_record=fixture.record,
    )


def _signed_successor(fixture: Any, tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "successor" / PACK_ID
    shutil.copytree(fixture.source, source)
    manifest = validate_file(source / "pack.v4.json", "pack")
    manifest["pack"]["version"] = "99.0.0"
    (source / "pack.v4.json").write_bytes(canonical_json(manifest))
    runtime = source / "runtime/adapter.py"
    runtime.write_bytes(runtime.read_bytes() + b"\n# isolated signed successor\n")
    _refresh(source)
    key = Ed25519PrivateKey.generate()
    manifest = validate_file(source / "pack.v4.json", "pack")
    executable = validate_file(source / "executables.v4.json", "executable_catalog")
    signed = sign_manifest(
        build_signed_manifest(
            source,
            pack_id=PACK_ID,
            version="99.0.0",
            publisher_id="tobkiri.repository",
            core_compatibility=">=0",
            contract_versions={
                op["contract_id"]: op["contract_version"]
                for variant in executable["variants"]
                for op in variant["operations"]
            },
            requested_capabilities=manifest["requirements"]["capabilities"],
        ),
        key,
    )
    path = source / SIGNED_MANIFEST_RELATIVE
    path.parent.mkdir()
    path.write_bytes(canonical_json(signed))
    trust = tmp_path / "host-policy/publisher-trust.json"
    trust.parent.mkdir(mode=0o700)
    write_host_install_record(
        trust,
        pack_id=PACK_ID,
        install_path=source,
        record={
            "signature_required": True,
            "publisher_id": signed["publisher_id"],
            "key_id": signed["signature"]["key_id"],
            "installed_version": signed["version"],
            "signed_manifest_path": SIGNED_MANIFEST_RELATIVE,
            "contract_versions": signed["contract_versions"],
            "requested_capabilities": signed["requested_capabilities"],
        },
        publisher_record={
            "public_key_pem": key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode(),
            "allowed_pack_namespaces": [PACK_ID],
            "revoked_key_ids": [],
        },
    )
    return source, trust


def test_canonical_baseline_roundtrip_is_separate_and_noclobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    pointer = ActiveProfileStore(fixture.owner).path.read_bytes()
    retained = _publish(fixture)
    assert retained.root != fixture.source
    assert _publish(fixture) == retained
    assert (retained.root / "artifact-index.v4.json").read_bytes() == (
        fixture.source / "artifact-index.v4.json"
    ).read_bytes()
    assert retained.root.stat().st_mode & 0o777 == 0o500
    assert not (fixture.owner / "retained-bundled-artifacts-v4").exists()
    assert ActiveProfileStore(fixture.owner).path.read_bytes() == pointer
    with baselines.bundled_host_baseline_read(fixture.origin, bundle_root=fixture.bundle) as read:
        assert read.resolve(PACK_ID) == retained.root
    with pytest.raises(BundledArtifactRetentionDenied, match="closed"):
        read.resolve(PACK_ID)


def test_real_active_successor_uses_frozen_baseline_after_source_version_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal Authority capture, signed CAS and Profile ceremony stay genuine."""
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    retained = _publish(fixture)
    source, trust = _signed_successor(fixture, tmp_path)
    admitted = external.admit_signed_pack_version(
        source,
        trust_store_path=trust,
        expected_predecessor_digest=fixture.pin,
        expected_catalog_revision=external.control_catalog_revision(),
    )
    view = profile_pack_versions("defaults")
    selected = next(row for row in view["packs"] if row["pack_id"] == PACK_ID)
    select_profile_pack_version(
        {
            "profile_id": "defaults",
            "pack_id": PACK_ID,
            "artifact_digest": admitted["artifact_digest"],
            "expected_selected_digest": selected["selected_digest"],
            "expected_profile_revision": view["profile_definition_revision"],
            "expected_store_generation": view["store_generation"],
            "expected_catalog_revision": view["catalog_revision"],
        }
    )
    _approve_gateway_profile_revision(fixture.owner, "defaults")
    active = profile_capture.capture_active_profile()
    origin = capture_retention_origin(fixture.owner)
    assert dict(origin.pack_pins)[PACK_ID] == admitted["artifact_digest"] != fixture.pin
    assert active.resolved.plan["bundle_digest"] == fixture.origin.bundle_digest
    drift = tmp_path / "new-canonical-source" / PACK_ID
    shutil.copytree(source, drift, ignore=shutil.ignore_patterns(".tobkiri"))
    manifest = validate_file(drift / "pack.v4.json", "pack")
    manifest["pack"]["version"] = "100.0.0"
    (drift / "pack.v4.json").write_bytes(canonical_json(manifest))
    _refresh(drift)
    original = external.resolve_pack_root
    monkeypatch.setattr(
        external,
        "resolve_pack_root",
        lambda pack_id, *args: drift if pack_id == PACK_ID else original(pack_id, *args),
    )
    monkeypatch.setattr(
        baselines,
        "resolve_pack_root",
        lambda pack_id: drift if pack_id == PACK_ID else original(pack_id),
    )
    with pytest.raises(external.ExternalPackCatalogDenied, match="compatibility boundary"):
        external.load_external_pack_catalog({PACK_ID: admitted["artifact_digest"]})
    with baselines.bundled_host_baseline_read(origin, bundle_root=fixture.bundle) as read:
        assert read.resolve(PACK_ID) == retained.root
        snapshot = external.load_external_pack_catalog({PACK_ID: admitted["artifact_digest"]})
        cas = snapshot.roots[PACK_ID]
        assert cas != retained.root
        assert (
            external.resolve_admitted_pack_root(
                PACK_ID, artifact_digest=admitted["artifact_digest"], origin=origin
            )
            == cas
        )
        assert (
            external.load_verified_pack_executable_catalog(
                PACK_ID, validate_file(cas / "pack.v4.json", "pack"), retained_bundled_origin=origin
            )["pack_id"]
            == PACK_ID
        )
        # An unselected retained revision is verified too; zero projected rows.
        assert PACK_ID in external.load_external_pack_catalog().versions
        revision = external.control_catalog_revision()
        catalog_bytes = external._catalog_path(fixture.owner).read_bytes()
        with pytest.raises(external.ExternalPackCatalogDenied):
            external.admit_signed_pack_version(
                source,
                trust_store_path=trust,
                expected_predecessor_digest=fixture.pin,
                expected_catalog_revision=revision,
            )
        assert external._catalog_path(fixture.owner).read_bytes() == catalog_bytes
        with pytest.raises(external.ExternalPackCatalogDenied):
            external._project_catalog_record(
                source, json.loads((source / SIGNED_MANIFEST_RELATIVE).read_bytes())
            )
        with baselines.canonical_admission_baseline():
            assert baselines.current_bundled_host_baseline_read() is None
            with pytest.raises(external.ExternalPackCatalogDenied):
                external.load_external_pack_catalog()
        with pytest.raises(BundledArtifactRetentionDenied, match="active pin"):
            verify_retained_bundled_artifact(origin, pack_id=PACK_ID, artifact_digest=fixture.pin)
    # The Host's normal read path can re-capture the successor without a cycle.
    assert profile_capture.capture_active_profile().activation == active.activation
    assert (
        profile_capture.baseline_origin_for_verified_active_profile(
            active, user_data_root=fixture.owner
        ).profile_binding
        == origin.profile_binding
    )


@pytest.mark.parametrize("change", ["manifest", "record", "runtime", "index_role", "missing"])
def test_untrusted_baseline_suppliers_cannot_change_frozen_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    record = deepcopy(fixture.record)
    if change == "manifest":
        manifest = validate_file(fixture.source / "pack.v4.json", "pack")
        manifest["pack"]["version"] = "88.0.0"
        (fixture.source / "pack.v4.json").write_bytes(canonical_json(manifest))
    elif change == "record":
        record["version"] = "88.0.0"
    elif change == "runtime":
        path = fixture.source / "runtime/adapter.py"
        path.write_bytes(path.read_bytes() + b"\n# tampered\n")
    elif change == "missing":
        (fixture.source / "contracts.v4.json").unlink()
    else:
        from tobkiri_protocol.canonical import canonical_digest

        path = fixture.source / "artifact-index.v4.json"
        index = json.loads(path.read_bytes())
        index["artifacts"][0]["role"] = "runtime"
        index["integrity_seal"]["signed_digest"] = canonical_digest(
            {k: v for k, v in index.items() if k != "integrity_seal"}
        )
        path.write_bytes(canonical_json(index))
    with pytest.raises((ValueError, OSError, RuntimeError)):
        baselines.retain_bundled_host_baseline(
            fixture.origin,
            bundle_root=fixture.bundle,
            source_root=fixture.source,
            source_record=record,
        )
    assert not (fixture.owner / "retained-bundled-baselines-v4").exists()


@pytest.mark.parametrize("change", ["extra", "symlink", "hardlink", "runtime", "lock"])
def test_published_baseline_tree_is_revalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    retained = _publish(fixture)
    container = retained.root.parent.parent
    for path in (container, *container.rglob("*")):
        path.chmod(0o700 if path.is_dir() else 0o600)
    path = retained.root / "runtime/adapter.py"
    if change == "extra":
        (container / "extra.txt").write_text("extra")
    elif change in {"symlink", "hardlink"}:
        target = tmp_path / "outside.py"
        target.write_bytes(path.read_bytes())
        path.unlink()
        if change == "symlink":
            path.symlink_to(target)
        else:
            os.link(target, path)
    elif change == "runtime":
        path.write_bytes(path.read_bytes() + b"\n# altered cache\n")
    else:
        (container / "origin/bundle.lock.json").write_text("{}")
    for entry in (container, *container.rglob("*")):
        metadata = entry.lstat()
        if not stat.S_ISLNK(metadata.st_mode):
            entry.chmod(0o500 if stat.S_ISDIR(metadata.st_mode) else 0o400)
    with pytest.raises((ValueError, OSError, RuntimeError)):
        with baselines.bundled_host_baseline_read(
            fixture.origin, bundle_root=fixture.bundle
        ) as read:
            read.resolve(PACK_ID)


def test_baseline_scope_rejects_other_owner_thread_and_late_byte_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    retained = _publish(fixture)
    with baselines.bundled_host_baseline_read(fixture.origin, bundle_root=fixture.bundle) as read:
        errors: list[BaseException] = []

        def foreign_thread() -> None:
            try:
                read.resolve(PACK_ID)
            except BaseException as error:
                errors.append(error)

        thread = Thread(target=foreign_thread)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], BundledArtifactRetentionDenied)
    other = tmp_path / "other-owner"
    other.mkdir()
    with monkeypatch.context() as altered:
        altered.setenv("TOBKIRI_USER_DATA", str(other))
        with pytest.raises(BundledArtifactRetentionDenied, match="another Host owner"):
            _publish(fixture)
    with pytest.raises((ValueError, OSError)):
        with baselines.bundled_host_baseline_read(
            fixture.origin, bundle_root=fixture.bundle
        ) as read:
            read.resolve(PACK_ID)
            path = retained.root / "runtime/adapter.py"
            path.chmod(0o600)
            path.write_bytes(path.read_bytes() + b"\n# late mutation\n")
            path.chmod(0o400)


def test_late_active_pointer_change_invalidates_baseline_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    _publish(fixture)
    with pytest.raises(BundledArtifactRetentionDenied, match="pointer"):
        with baselines.bundled_host_baseline_read(
            fixture.origin, bundle_root=fixture.bundle
        ) as read:
            read.resolve(PACK_ID)
            store = ActiveProfileStore(fixture.owner)
            pointer = store.require(verify_snapshot=True)
            store.commit(pointer, expected=pointer)


def test_missing_old_full_bytes_fail_closed_without_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    foreign = tmp_path / "wrong-root"
    shutil.copytree(fixture.source, foreign)
    manifest = validate_file(foreign / "pack.v4.json", "pack")
    manifest["pack"]["version"] = "101.0.0"
    (foreign / "pack.v4.json").write_bytes(canonical_json(manifest))
    _refresh(foreign)
    monkeypatch.setattr(baselines, "resolve_pack_root", lambda _pack_id: foreign)
    with pytest.raises(BundledArtifactRetentionDenied, match="manifest differs"):
        with baselines.bundled_host_baseline_read(
            fixture.origin, bundle_root=fixture.bundle
        ) as read:
            read.resolve(PACK_ID)
    assert not (fixture.owner / "retained-bundled-baselines-v4").exists()
