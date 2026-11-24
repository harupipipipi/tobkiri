"""Development Shell composition through real isolated capture and Authority."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from core_runtime import bundled_artifact_retention_v4 as retention
from core_runtime import debug_prebuilt_shell_v4 as inputs
from core_runtime.active_profile_store_v4 import ActiveProfileStore
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture
from scripts import generate_packaged_defaultspack_v4_bundle as generator
from scripts import package_debug_prebuilt_shell_v4 as packager
from scripts import packaging_cleanup
from tests.conformance_support.packaged_profile import create_test_source_provenance
from tests.test_bundled_artifact_retention_v4 import (
    RetentionFixture,
    make_retention_fixture,
    retain_fixture,
)
from tobkiri_protocol.canonical import canonical_json, strict_loads
from tobkiri_protocol.platform_artifact import artifact_digest

RUNTIME = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Fixture:
    """Untrusted private suppliers and a real normally captured origin."""

    retained: RetentionFixture
    artifacts: Path
    host: Path


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    retained = make_retention_fixture(tmp_path, monkeypatch)
    supplier = tmp_path / "supplier"
    shutil.copytree(retained.bundle.parent, supplier)
    host = tmp_path / "host"
    app = json.loads(
        (
            supplier / "v4/packs/runtime.tauri.application.default.pack.v4.json"
        ).read_bytes()
    )
    for row in app["artifacts"]:
        if row["kind"] == "asset":
            destination = host / row["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                RUNTIME / "ecosystem/defaultspack" / row["path"], destination
            )
    return Fixture(
        replace(retained, bundle=supplier / "v4"), supplier / "platform-artifacts", host
    )


@pytest.fixture(scope="module")
def provenance(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return create_test_source_provenance(
        RUNTIME,
        tmp_path_factory.mktemp("debug-prebuilt-provenance"),
        provenance_record={
            "source_commit": "0123456789abcdef0123456789abcdef01234567",
            "source_tree": "89abcdef0123456789abcdef0123456789abcdef",
            "source_clean": True,
        },
    )


def _input(
    fixture: Fixture,
) -> AbstractContextManager[inputs.VerifiedPrebuiltShellInput]:
    return inputs.verified_active_prebuilt_shell(
        fixture.retained.origin,
        bundle_root=fixture.retained.bundle,
        artifact_root=fixture.artifacts,
        host_asset_root=fixture.host,
    )


def _outputs(tmp_path: Path) -> tuple[Path, Path]:
    bundle = tmp_path / "output/v4"
    artifacts = bundle.parent / "platform-artifacts"
    shutil.copytree(RUNTIME / "ecosystem/defaultspack/v4", bundle)
    artifacts.mkdir()
    (artifacts / "previous.txt").write_bytes(b"previous output")
    return bundle, artifacts


def _bytes(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_normal_capture_composes_exact_old_shell_with_current_source(
    fixture: Fixture,
    provenance: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = fixture.retained.origin
    active = profile_capture.capture_active_profile()
    before_plan = canonical_json(active.resolved.plan)
    pointer = ActiveProfileStore(origin.user_data_root).require()
    before_pointer = (origin.user_data_root / "profiles/active.json").read_bytes()
    before_envelope = (
        origin.user_data_root / pointer.activation_snapshot_path
    ).read_bytes()
    retained = retain_fixture(fixture.retained)
    bundle, artifacts = _outputs(tmp_path)
    with _input(fixture) as prebuilt:
        documents = dict(prebuilt.documents)
        variant = strict_loads(prebuilt.variant_json)
        packager.stage_debug_prebuilt_bundle(
            prebuilt=prebuilt,
            bundle_root=bundle,
            artifact_root=artifacts,
            source_provenance_file=provenance,
        )
        assert all(
            (bundle / name).read_bytes() == raw for name, raw in documents.items()
        )
        assert (
            artifact_digest(artifacts / variant["relative_path"])
            == variant["artifact_digest"]
        )
        for path, _digest in prebuilt.host_assets:
            assert (prebuilt.root / "host-assets" / path).read_bytes() == (
                fixture.host / path
            ).read_bytes()
    assert not prebuilt.root.exists()
    with pytest.raises(inputs.PrebuiltShellDenied, match="open authenticated scope"):
        inputs.assert_prebuilt_shell_current(prebuilt)
    source_adapter = (
        RUNTIME
        / "ecosystem/defaultspack/v4/packs/rumi_provider_adapters_pack.pack.v4.json"
    )
    assert (
        bundle / "packs/rumi_provider_adapters_pack.pack.v4.json"
    ).read_bytes() == source_adapter.read_bytes()
    new_digest = (
        "sha256:"
        + hashlib.sha256((bundle / "bundle.lock.json").read_bytes()).hexdigest()
    )
    assert new_digest != origin.bundle_digest
    monkeypatch.setattr(profile_capture, "_bundle_root", lambda *_args: bundle)
    after = profile_capture.capture_active_profile()
    assert canonical_json(after.resolved.plan) == before_plan
    assert (
        origin.user_data_root / "profiles/active.json"
    ).read_bytes() == before_pointer
    assert (
        origin.user_data_root / pointer.activation_snapshot_path
    ).read_bytes() == before_envelope
    recaptured = retention.capture_retention_origin(origin.user_data_root)
    assert recaptured.bundle_digest == origin.bundle_digest
    assert (
        retention.verify_retained_bundled_artifact(
            recaptured,
            pack_id=retained.pack_id,
            artifact_digest=retained.artifact_digest,
        )
        == retained.root
    )


@pytest.mark.parametrize(
    "kind", ["lock", "shell", "shell-pack", "application", "artifact", "host-asset"]
)
def test_untrusted_supplier_tampering_never_creates_an_input(
    fixture: Fixture, kind: str
) -> None:
    names = {
        "lock": "bundle.lock.json",
        "shell": "shell.tauri.default.shell.v1.json",
        "shell-pack": "packs/shell.tauri.default.pack.v4.json",
        "application": "packs/runtime.tauri.application.default.pack.v4.json",
    }
    if kind in names:
        path = fixture.retained.bundle / names[kind]
    elif kind == "artifact":
        path = next(p for p in fixture.artifacts.rglob("*") if p.is_file())
    else:
        path = next(p for p in fixture.host.rglob("*") if p.is_file())
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises((ValueError, OSError)):
        with _input(fixture):
            pytest.fail("tampered bytes must not mint a prebuilt input")


@pytest.mark.parametrize("kind", ["link", "hard-link", "fifo", "mode"])
def test_unsafe_artifact_entries_fail_before_packaging(
    fixture: Fixture,
    tmp_path: Path,
    kind: str,
) -> None:
    path = next(p for p in fixture.artifacts.rglob("*") if p.is_file())
    if kind == "mode":
        path.chmod(0o400)
    else:
        outside = tmp_path / "outside"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        if kind == "link":
            path.symlink_to(outside)
        elif kind == "hard-link":
            os.link(outside, path)
        else:
            os.mkfifo(path)
    with pytest.raises((ValueError, OSError)):
        with _input(fixture):
            pytest.fail("unsafe artifact must fail closed")


@pytest.mark.parametrize(
    "limit", ["_MAX_FILE", "_MAX_TOTAL", "_MAX_ENTRIES", "_MAX_METADATA"]
)
def test_snapshot_limits_are_finite(
    fixture: Fixture, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    monkeypatch.setattr(inputs, limit, 1)
    with pytest.raises((ValueError, OSError)):
        with _input(fixture):
            pytest.fail("bounded snapshot should reject this fixture")


@pytest.mark.parametrize("change", ["epoch", "pointer", "fence"])
def test_typed_origin_requires_fresh_normal_authority(
    fixture: Fixture, change: str
) -> None:
    origin = fixture.retained.origin
    if change == "epoch":
        with AuthorityStore(
            origin.user_data_root / "authority/v4.sqlite3"
        ) as authority:
            authority.advance_security_epoch("isolated prebuilt test")
    elif change == "pointer":
        store = ActiveProfileStore(origin.user_data_root)
        pointer = store.require()
        store.commit(pointer, expected=pointer)
    else:
        with sqlite3.connect(
            origin.user_data_root / "authority/v4.sqlite3"
        ) as connection:
            connection.execute(
                "UPDATE activation_reservations SET fencing_token=fencing_token+1"
            )
    with pytest.raises(ValueError):
        with _input(fixture):
            pytest.fail("stale capture must not authenticate old bytes")


def test_copied_origin_and_input_are_not_issued_authority(fixture: Fixture) -> None:
    with pytest.raises(ValueError, match="captured origin"):
        retention.authenticated_retention_plan(replace(fixture.retained.origin))
    with _input(fixture) as value:
        with pytest.raises(ValueError, match="open authenticated scope"):
            inputs.assert_prebuilt_shell_current(replace(value))


def test_authenticated_plan_accessor_returns_an_independent_copy(
    fixture: Fixture,
) -> None:
    plan = retention.authenticated_retention_plan(fixture.retained.origin)
    plan["shell"]["definition_digest"] = "sha256:" + "0" * 64
    assert retention.authenticated_retention_plan(fixture.retained.origin) != plan


def test_absent_prepared_output_is_rejected_without_partial_publication(
    fixture: Fixture,
    provenance: Path,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "absent/v4"
    bundle.parent.mkdir()
    with _input(fixture) as prebuilt:
        with pytest.raises(ValueError, match="bundle root must be a real directory"):
            packager.stage_debug_prebuilt_bundle(
                prebuilt=prebuilt,
                bundle_root=bundle,
                artifact_root=bundle.parent / "platform-artifacts",
                source_provenance_file=provenance,
            )
    assert not bundle.exists()
    assert not (bundle.parent / "platform-artifacts").exists()


def test_verified_snapshot_detaches_supplier_but_rejects_snapshot_drift(
    fixture: Fixture,
) -> None:
    with pytest.raises(inputs.PrebuiltShellDenied, match="snapshot changed"):
        with _input(fixture) as value:
            variant = strict_loads(value.variant_json)
            supplier = fixture.artifacts / variant["entrypoint"]
            supplier.write_bytes(supplier.read_bytes() + b"supplier drift")
            inputs.assert_prebuilt_shell_current(value)
            snapshot = value.root / "platform-artifacts" / variant["entrypoint"]
            snapshot.chmod(0o700)
            snapshot.write_bytes(snapshot.read_bytes() + b"snapshot drift")
            inputs.assert_prebuilt_shell_current(value)


def test_revocation_during_publication_rolls_back_both_outputs(
    fixture: Fixture,
    provenance: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, artifacts = _outputs(tmp_path)
    before = (_bytes(bundle), _bytes(artifacts))
    replace_path = generator.os.replace
    revoked = False

    def publish(source: Path, destination: Path) -> None:
        nonlocal revoked
        replace_path(source, destination)
        if Path(destination) == artifacts and not revoked:
            revoked = True
            with AuthorityStore(
                fixture.retained.owner / "authority/v4.sqlite3"
            ) as authority:
                authority.advance_security_epoch("isolated publication fence")

    monkeypatch.setattr(generator.os, "replace", publish)
    with pytest.raises(ValueError, match="Authority"):
        with _input(fixture) as prebuilt:
            packager.stage_debug_prebuilt_bundle(
                prebuilt=prebuilt,
                bundle_root=bundle,
                artifact_root=artifacts,
                source_provenance_file=provenance,
            )
    assert revoked
    assert (_bytes(bundle), _bytes(artifacts)) == before
    assert not list(bundle.parent.glob(".tobkiri-defaultspack-transaction-*"))


def test_snapshot_cleanup_cannot_delete_a_replacement(
    fixture: Fixture, tmp_path: Path
) -> None:
    displaced = tmp_path / "displaced"
    with pytest.raises(packaging_cleanup.PackagingCleanupError, match="identity"):
        with _input(fixture) as prebuilt:
            original = prebuilt.root
            original.rename(displaced)
            original.mkdir()
            (original / "keep").write_bytes(b"unrelated replacement")
    assert (original / "keep").read_bytes() == b"unrelated replacement"
    shutil.rmtree(original)
    shutil.rmtree(displaced)


@pytest.mark.parametrize("mode", ["release", "dev"])
def test_preparer_requires_all_explicit_debug_roots(mode: str) -> None:
    path = RUNTIME.parent / "tobkiri_launcher/scripts/prepare_viewer_runtime.py"
    spec = importlib.util.spec_from_file_location("prebuilt_preparer_test", path)
    assert spec is not None and spec.loader is not None
    preparer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparer)
    assert preparer.parse_args(["--mode", mode]).debug_prebuilt_owner_root is None
    with pytest.raises(SystemExit):
        preparer.parse_args(
            ["--mode", mode, "--debug-prebuilt-owner-root", "/untrusted"]
        )
