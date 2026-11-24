"""Read-boundary CAS verification and exact bulk root regressions."""

from dataclasses import replace
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from core_runtime import admitted_pack_read_v4 as reads
from core_runtime import external_pack_catalog_v4 as external
from core_runtime.pack_boundary import PackBoundaryError, resolve_pack_root
from tests.test_external_pack_catalog_v4 import PACK_ID, _signed_external_pack
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.validation import validate_file


@pytest.fixture
def admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Admit real signed fixture bytes into an isolated Host-owned catalog."""
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source, trust = _signed_external_pack(tmp_path)
    return dict(external.admit_signed_external_pack(source, trust_store_path=trust))


@pytest.mark.parametrize("lookups", [1, 42])
@pytest.mark.parametrize("versions", [1, 3])
def test_bulk_read_fully_verifies_cas_twice_independent_of_root_lookups(
    admitted: dict,
    monkeypatch: pytest.MonkeyPatch,
    lookups: int,
    versions: int,
    tmp_path: Path,
) -> None:
    for index in range(1, versions):
        release = tmp_path / f"release-{index}"
        release.mkdir()
        source, trust = _signed_external_pack(
            release, version=f"1.{index}.0", runtime_suffix=f"\n# revision {index}\n"
        )
        external.admit_signed_pack_version(
            source,
            trust_store_path=trust,
            expected_predecessor_digest=admitted["artifact_digest"],
            expected_catalog_revision=external.control_catalog_revision(),
        )
    scans = []
    verified = []
    load = reads.load_external_pack_catalog
    verify = external._verify_entry_root

    def counted_load(pins):
        scans.append(dict(pins))
        return load(pins)

    def counted_verify(pack_id, entry, root):
        verified.append(pack_id)
        return verify(pack_id, entry, root)

    monkeypatch.setattr(reads, "load_external_pack_catalog", counted_load)
    monkeypatch.setattr(external, "_verify_entry_root", counted_verify)
    pins = {PACK_ID: admitted["artifact_digest"]}
    with reads.verified_pack_root_read(pins) as reader:
        roots = [reader.resolve(PACK_ID) for _ in range(lookups)]
    assert len(set(roots)) == 1
    assert scans == [pins, pins]
    assert verified == [PACK_ID] * (2 * versions)
    with pytest.raises(external.ExternalPackCatalogDenied, match="closed"):
        reader.resolve(PACK_ID)


def test_bulk_read_rejects_authenticated_catalog_revision_change(
    admitted: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    load = reads.load_external_pack_catalog
    calls = 0

    def changed_revision(pins):
        nonlocal calls
        calls += 1
        snapshot = load(pins)
        return replace(snapshot, revision="sha256:" + "f" * 64) if calls == 2 else snapshot

    monkeypatch.setattr(reads, "load_external_pack_catalog", changed_revision)
    with pytest.raises(external.ExternalPackCatalogDenied, match="catalog changed"):
        with reads.verified_pack_root_read({PACK_ID: admitted["artifact_digest"]}) as reader:
            reader.resolve(PACK_ID)


@pytest.mark.parametrize("change", ["content", "root"])
def test_bulk_read_rechecks_real_signed_cas_before_result_escapes(
    admitted: dict, change: str
) -> None:
    with pytest.raises(external.ExternalPackCatalogDenied):
        with reads.verified_pack_root_read({PACK_ID: admitted["artifact_digest"]}) as reader:
            root = reader.resolve(PACK_ID)
            if change == "content":
                runtime = root / "runtime" / "echo.py"
                runtime.chmod(0o600)
                runtime.write_bytes(runtime.read_bytes() + b"\n# changed during read\n")
            else:
                parent = root.parent
                parent.chmod(0o700)
                root.chmod(0o700)
                moved = parent / (root.name + "-old")
                root.rename(moved)
                shutil.copytree(moved, root)


def test_bulk_read_does_not_reuse_verification_between_requests(
    admitted: dict,
) -> None:
    pins = {PACK_ID: admitted["artifact_digest"]}
    with reads.verified_pack_root_read(pins) as reader:
        root = reader.resolve(PACK_ID)
    runtime = root / "runtime" / "echo.py"
    runtime.chmod(0o600)
    runtime.write_bytes(runtime.read_bytes() + b"\n# changed between reads\n")
    with pytest.raises(external.ExternalPackCatalogDenied):
        with reads.verified_pack_root_read(pins):
            pytest.fail("tampered CAS must fail at the next entry boundary")


def test_bulk_read_preserves_bundled_jail_and_exact_artifact_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    pack_id = "rumi_ai_gateway_pack"
    root = resolve_pack_root(pack_id)
    digest = compile_pack_root(root).artifact.digest
    with reads.verified_pack_root_read({pack_id: digest}) as reader:
        assert reader.resolve(pack_id) == root
        with pytest.raises(external.ExternalPackCatalogDenied, match="pin set"):
            reader.resolve("../outside")
    with reads.verified_pack_root_read({pack_id: "sha256:" + "f" * 64}) as reader:
        with pytest.raises(external.ExternalPackCatalogDenied, match="revision"):
            reader.resolve(pack_id)


@pytest.mark.parametrize("change", ["content", "root", "symlink"])
def test_bulk_read_rechecks_used_bundled_roots_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    source = resolve_pack_root("rumi_ai_gateway_pack")
    root = tmp_path / "bundled"
    shutil.copytree(source, root, ignore=shutil.ignore_patterns("__pycache__"))
    digest = compile_pack_root(root).artifact.digest
    monkeypatch.setattr(reads, "resolve_pack_root", lambda pack_id: root)
    with pytest.raises((external.ExternalPackCatalogDenied, RuntimeError, ValueError)):
        with reads.verified_pack_root_read({"rumi_ai_gateway_pack": digest}) as reader:
            reader.resolve("rumi_ai_gateway_pack")
            if change == "content":
                runtime = root / "runtime" / "gateway.py"
                runtime.write_bytes(runtime.read_bytes() + b"\n# changed\n")
            else:
                moved = tmp_path / "old-bundled"
                root.rename(moved)
                if change == "symlink":
                    root.symlink_to(moved, target_is_directory=True)
                else:
                    shutil.copytree(moved, root)


def test_bulk_read_rejects_real_authenticated_admission_during_read(
    admitted: dict,
    tmp_path: Path,
) -> None:
    release = tmp_path / "new-release"
    release.mkdir()
    source, trust = _signed_external_pack(
        release, version="1.1.0", runtime_suffix="\n# successor\n"
    )
    with pytest.raises(external.ExternalPackCatalogDenied, match="catalog changed"):
        with reads.verified_pack_root_read({PACK_ID: admitted["artifact_digest"]}) as reader:
            reader.resolve(PACK_ID)
            external.admit_signed_pack_version(
                source,
                trust_store_path=trust,
                expected_predecessor_digest=admitted["artifact_digest"],
                expected_catalog_revision=external.control_catalog_revision(),
            )


def _raw_active(pack_ids: list[str]) -> SimpleNamespace:
    manifests = {
        pack_id: validate_file(resolve_pack_root(pack_id) / "pack.v4.json", "pack")
        for pack_id in pack_ids
    }
    effective = [
        {"identity": pack_id, "artifact_digest": manifest["pack"]["artifact_digest"]}
        for pack_id, manifest in manifests.items()
    ]
    return SimpleNamespace(
        resolved=SimpleNamespace(
            profile={
                "profile_id": "defaults",
                "base": {"pack_id": "base"},
                "packs": [{"pack_id": pack_id, "role": "provider"} for pack_id in pack_ids],
            },
            plan={"effective_set": effective, "plan_digest": "sha256:" + "b" * 64},
            lock={"effective_set": effective},
        ),
        activation={},
        manifests=manifests,
    )


@pytest.mark.parametrize("pack_count", [1, 42])
def test_capture_binding_batches_external_scans_with_real_bundled_pins(
    admitted: dict, monkeypatch: pytest.MonkeyPatch, pack_count: int
) -> None:
    from copy import deepcopy

    from core_runtime.bootstrap import profile_capture
    from core_runtime.pack_boundary import load_pack_catalog
    from core_runtime.pack_control_v4 import (
        _capture_binding,
        _physical_catalog_read,
        catalog_with_admitted_pack_closure,
    )
    from core_runtime.bootstrap.profile_capture import (
        capture_default_profile,
        capture_profile,
        host_profile_catalog,
        prepare_default_profile_confirmation,
        prepare_profile_confirmation,
        runtime_user_data_root,
    )
    from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
    from core_runtime.profile_runtime_port import require_profile_runtime

    capture_default_profile(
        confirmation=prepare_default_profile_confirmation(),
    )
    catalog, _ = catalog_with_admitted_pack_closure(host_profile_catalog(), [PACK_ID])
    profile = deepcopy(catalog.profiles["defaults"])
    profile_id = "batch-read-profile"
    profile["profile_id"] = profile_id
    profile["display_name"] = "Signed Pack read fixture"
    profile["packs"].append(
        {"pack_id": PACK_ID, "artifact_digest": admitted["artifact_digest"], "role": "provider"}
    )
    profile["requested_edges"].extend(
        require_profile_runtime().dynamic_profile_edges(catalog, "defaults", (PACK_ID,))
    )
    ProfileDefinitionStore(runtime_user_data_root()).create_profile(profile)
    active = capture_profile(
        profile_id,
        confirmation=prepare_profile_confirmation(profile_id),
    )
    assert any(
        row["identity"] == PACK_ID and row["artifact_digest"] == admitted["artifact_digest"]
        for row in active.resolved.plan["effective_set"]
    )
    pack_ids = sorted(
        row["identity"]
        for row in active.resolved.plan["effective_set"]
        if row["identity"] in load_pack_catalog()
    )[:pack_count]
    assert len(pack_ids) == pack_count
    calls = []
    recaptured = []
    verify = external._verify_entry_root
    capture_active = profile_capture.capture_active_profile

    def counted_verify(pack_id, entry, root):
        calls.append(pack_id)
        return verify(pack_id, entry, root)

    def counted_active_capture(*, base_dir: Path | None = None):
        snapshot = capture_active(base_dir=base_dir)
        recaptured.append(
            (
                snapshot.resolved.profile["profile_id"],
                snapshot.resolved.plan["plan_digest"],
                snapshot.activation["activation_id"],
            )
        )
        return snapshot

    monkeypatch.setattr(external, "_verify_entry_root", counted_verify)
    monkeypatch.setattr(profile_capture, "capture_active_profile", counted_active_capture)
    with _physical_catalog_read(active) as (read, captured):
        for pack_id in pack_ids:
            read.resolve(pack_id)
        binding = _capture_binding(captured, _read=read)
    assert binding.profile_id == profile_id
    assert binding.plan_digest == active.resolved.plan["plan_digest"]
    assert calls == [PACK_ID] * 2
    assert recaptured == [
        (profile_id, active.resolved.plan["plan_digest"], active.activation["activation_id"])
    ]


@pytest.mark.parametrize("pack_count", [1, 42])
def test_contract_catalog_projection_batches_scans_and_keeps_exact_documents(
    admitted: dict, monkeypatch: pytest.MonkeyPatch, pack_count: int
) -> None:
    from core_runtime.pack_boundary import load_pack_catalog
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        RuntimeSurfaceSnapshot,
        _validated_contract_catalogs,
    )

    pack_ids = sorted(load_pack_catalog())[:pack_count]
    active = _raw_active(pack_ids)
    snapshot = RuntimeSurfaceSnapshot(
        active=active, catalog=SimpleNamespace(packs=active.manifests)
    )
    calls = []
    verify = external._verify_entry_root

    def counted_verify(pack_id, entry, root):
        calls.append(pack_id)
        return verify(pack_id, entry, root)

    monkeypatch.setattr(external, "_verify_entry_root", counted_verify)
    catalogs = _validated_contract_catalogs(snapshot)
    expected = {pack_id for pack_id in pack_ids if active.manifests[pack_id]["contracts"]}
    assert set(catalogs) == expected
    assert all(
        catalogs[pack_id]
        == validate_file(resolve_pack_root(pack_id) / "contracts.v4.json", "pack_contract_catalog")
        for pack_id in expected
    )
    assert calls == [PACK_ID] * 2


@pytest.mark.parametrize("caller", ["capture", "contracts"])
def test_read_callers_reject_final_catalog_drift_as_digest_mismatch(
    admitted: dict, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    from core_runtime.pack_control_v4 import PackControlDigestMismatch, _capture_binding
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        RuntimeSurfaceError,
        RuntimeSurfaceErrorCode,
        RuntimeSurfaceSnapshot,
        _validated_contract_catalogs,
    )

    active = _raw_active(["rumi_ai_gateway_pack"])
    load = reads.load_external_pack_catalog
    calls = 0

    def changed_revision(pins):
        nonlocal calls
        calls += 1
        snapshot = load(pins)
        return replace(snapshot, revision="sha256:" + "f" * 64) if calls == 2 else snapshot

    monkeypatch.setattr(reads, "load_external_pack_catalog", changed_revision)
    if caller == "capture":
        with pytest.raises(PackControlDigestMismatch):
            _capture_binding(active)
    else:
        snapshot = RuntimeSurfaceSnapshot(
            active=active, catalog=SimpleNamespace(packs=active.manifests)
        )
        with pytest.raises(RuntimeSurfaceError) as denial:
            _validated_contract_catalogs(snapshot)
        assert denial.value.code == RuntimeSurfaceErrorCode.DIGEST_MISMATCH


def test_contract_catalog_projection_cannot_skip_compiled_digest_mismatch(
    admitted: dict,
) -> None:
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        RuntimeSurfaceError,
        RuntimeSurfaceErrorCode,
        RuntimeSurfaceSnapshot,
        _validated_contract_catalogs,
    )

    active = _raw_active(["rumi_ai_gateway_pack", "rumi_file_inspect_pack"])
    active.resolved.lock["effective_set"][0]["artifact_digest"] = "sha256:" + "f" * 64
    snapshot = RuntimeSurfaceSnapshot(
        active=active, catalog=SimpleNamespace(packs=active.manifests)
    )
    with pytest.raises(RuntimeSurfaceError) as denial:
        _validated_contract_catalogs(snapshot)
    assert denial.value.code == RuntimeSurfaceErrorCode.DIGEST_MISMATCH


def test_contract_catalog_projection_cannot_skip_failed_bundled_jail(
    admitted: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        RuntimeSurfaceError,
        RuntimeSurfaceErrorCode,
        RuntimeSurfaceSnapshot,
        _validated_contract_catalogs,
    )

    active = _raw_active(["rumi_ai_gateway_pack"])

    def denied_root(pack_id):
        raise PackBoundaryError("cataloged Pack root must not be a symlink")

    monkeypatch.setattr(reads, "resolve_pack_root", denied_root)
    snapshot = RuntimeSurfaceSnapshot(
        active=active, catalog=SimpleNamespace(packs=active.manifests)
    )
    with pytest.raises(RuntimeSurfaceError) as denial:
        _validated_contract_catalogs(snapshot)
    assert denial.value.code == RuntimeSurfaceErrorCode.DIGEST_MISMATCH


@pytest.mark.parametrize("kind", ["base", "shell", "application", "host_extension"])
def test_contract_catalog_projection_skips_only_unmaterialized_bundle_kinds(
    admitted: dict,
    kind: str,
) -> None:
    from copy import deepcopy
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        RuntimeSurfaceError,
        RuntimeSurfaceErrorCode,
        RuntimeSurfaceSnapshot,
        _validated_contract_catalogs,
    )

    pack_id = "bundle.only.fixture"
    active = _raw_active(["rumi_ai_gateway_pack"])
    manifest = deepcopy(active.manifests["rumi_ai_gateway_pack"])
    manifest["pack"].update(id=pack_id, kind=kind)
    active.manifests[pack_id] = manifest
    active.resolved.lock["effective_set"].append(
        {
            "identity": pack_id,
            "artifact_digest": manifest["pack"]["artifact_digest"],
        }
    )
    snapshot = RuntimeSurfaceSnapshot(
        active=active, catalog=SimpleNamespace(packs=active.manifests)
    )
    if kind == "host_extension":
        with pytest.raises(RuntimeSurfaceError) as denial:
            _validated_contract_catalogs(snapshot)
        assert denial.value.code == RuntimeSurfaceErrorCode.DIGEST_MISMATCH
    else:
        assert set(_validated_contract_catalogs(snapshot)) == {"rumi_ai_gateway_pack"}
