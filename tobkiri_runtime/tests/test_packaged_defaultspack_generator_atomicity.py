from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from scripts import generate_packaged_defaultspack_v4_bundle as generator
from scripts import generator_source_manifest
from scripts.generator_source_manifest import materialize_source_snapshot


ROOT = Path(__file__).resolve().parents[1]
SOURCE_BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
SOURCE_TREE = "89abcdef0123456789abcdef0123456789abcdef"
_SOURCE_PROVENANCE: Path | None = None


def _linux_source(path: Path, payload: bytes = b"original") -> Path:
    """Create a small recognized x86_64 ELF fixture."""
    path.write_bytes(
        b"\x7fELF\x02\x01\x01\x00"
        + b"\x00" * 10
        + b">\x00"
        + payload
    )
    path.chmod(0o755)
    return path


def _bundle_roots(root: Path) -> tuple[Path, Path]:
    """Create a clean source bundle and empty artifact output roots."""
    global _SOURCE_PROVENANCE
    owner = root / "sealed-source-owner"
    owner.mkdir(parents=True, exist_ok=True, mode=0o700)
    owner.chmod(0o700)
    snapshot = owner / "source"
    materialize_source_snapshot(ROOT, snapshot)
    snapshot.chmod(0o755)
    manifest = snapshot / "packaged_defaultspack_source_manifest.v1.json"
    provenance = snapshot / "packaging-source-provenance.v1.json"
    provenance.write_bytes(
        json.dumps(
            {
                "schema": "io.tobkiri.packaging-source-provenance.v1",
                "source_commit": SOURCE_COMMIT,
                "source_tree": SOURCE_TREE,
                "source_clean": True,
                "source_manifest_sha256": hashlib.sha256(
                    manifest.read_bytes()
                ).hexdigest(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    provenance.chmod(0o400)
    snapshot.chmod(0o555)
    _SOURCE_PROVENANCE = provenance
    bundle = root / "defaultspack" / "v4"
    artifacts = root / "defaultspack" / "platform-artifacts"
    shutil.copytree(SOURCE_BUNDLE, bundle)
    return bundle, artifacts


def _bytes(root: Path) -> dict[str, bytes]:
    """Snapshot all regular bytes below one output root."""
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _stage(
    source: Path,
    bundle: Path,
    artifacts: Path,
    *,
    relative_path: str = "Tobkiri.AppImage",
    entrypoint: str = "Tobkiri.AppImage",
) -> None:
    if _SOURCE_PROVENANCE is None:
        raise AssertionError("test source provenance was not initialized")
    generator.stage_packaged_bundle(
        source_artifact=source,
        bundle_root=bundle,
        artifact_root=artifacts,
        relative_path=relative_path,
        entrypoint=entrypoint,
        platform="linux",
        architecture="x86_64",
        bundle_identity="io.tobkiri.shell.tauri",
        source_provenance_file=_SOURCE_PROVENANCE,
    )


@pytest.mark.parametrize(
    ("relative_path", "entrypoint"),
    [
        ("../outside", "Tobkiri.AppImage"),
        ("/outside", "Tobkiri.AppImage"),
        ("Tobkiri.AppImage", "../outside"),
        ("Tobkiri.AppImage", "/outside"),
    ],
)
def test_generator_rejects_normalized_escape_before_copy(
    tmp_path: Path, relative_path: str, entrypoint: str
) -> None:
    bundle, artifacts = _bundle_roots(tmp_path)
    source = _linux_source(tmp_path / "source")
    outside = tmp_path / "outside"
    with pytest.raises(ValueError, match="unsafe"):
        _stage(
            source,
            bundle,
            artifacts,
            relative_path=relative_path,
            entrypoint=entrypoint,
        )
    assert not outside.exists()
    assert _bytes(artifacts) == {}
    assert json.loads((bundle / "bundle.lock.json").read_text())["entries"]


def test_generator_rejects_destination_symlink_without_writing_outside(
    tmp_path: Path,
) -> None:
    bundle, artifacts = _bundle_roots(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (artifacts.parent).mkdir(parents=True, exist_ok=True)
    artifacts.symlink_to(outside, target_is_directory=True)
    source = _linux_source(tmp_path / "source")
    with pytest.raises(ValueError, match="symlink"):
        _stage(source, bundle, artifacts)
    assert _bytes(outside) == {}


def test_generator_second_pack_write_fault_preserves_existing_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, artifacts = _bundle_roots(tmp_path)
    artifacts.mkdir(parents=True)
    (artifacts / "keep.txt").write_bytes(b"existing-artifact")
    source = _linux_source(tmp_path / "source")
    before_bundle = _bytes(bundle)
    before_artifacts = _bytes(artifacts)
    original_write = generator._write_json

    def fail_second_pack(path: Path, value: object) -> None:
        if path.name == "runtime.tauri.application.default.pack.v4.json":
            raise OSError("injected second Pack write fault")
        original_write(path, value)

    monkeypatch.setattr(generator, "_write_json", fail_second_pack)
    with pytest.raises(OSError, match="second Pack"):
        _stage(source, bundle, artifacts)
    assert _bytes(bundle) == before_bundle
    assert _bytes(artifacts) == before_artifacts
    assert not list(tmp_path.glob(".tobkiri-defaultspack-transaction-*"))


def test_generator_source_replace_after_snapshot_seals_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, artifacts = _bundle_roots(tmp_path)
    source = _linux_source(tmp_path / "source", b"original")
    original_snapshot = generator._snapshot_artifact

    def replace_after_snapshot(source_path: Path, destination: Path) -> Path:
        result = original_snapshot(source_path, destination)
        _linux_source(source_path, b"replaced-after-snapshot")
        return result

    monkeypatch.setattr(generator, "_snapshot_artifact", replace_after_snapshot)
    _stage(source, bundle, artifacts)
    assert (artifacts / "Tobkiri.AppImage").read_bytes().endswith(b"original")
    assert source.read_bytes().endswith(b"replaced-after-snapshot")


def test_generator_revalidates_only_staged_artifact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, artifacts = _bundle_roots(tmp_path)
    source = _linux_source(tmp_path / "source")
    original_verify = generator.verify_platform_artifact
    roots: list[Path] = []

    def observe(root: Path, variant: dict[str, object]) -> Path:
        roots.append(root)
        return original_verify(root, variant)

    monkeypatch.setattr(generator, "verify_platform_artifact", observe)
    _stage(source, bundle, artifacts)
    assert roots
    assert all(root != source.parent for root in roots)
    assert all(root.is_relative_to(tmp_path) for root in roots)


def test_generator_two_passes_have_identical_bytes(tmp_path: Path) -> None:
    source = _linux_source(tmp_path / "source")
    first_bundle, first_artifacts = _bundle_roots(tmp_path / "first")
    second_bundle, second_artifacts = _bundle_roots(tmp_path / "second")
    _stage(source, first_bundle, first_artifacts)
    _stage(source, second_bundle, second_artifacts)
    assert _bytes(first_bundle) == _bytes(second_bundle)
    assert _bytes(first_artifacts) == _bytes(second_artifacts)
    assert not list(tmp_path.rglob(".tobkiri-defaultspack-transaction-*"))


def test_generator_existing_output_rollback_on_publish_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, artifacts = _bundle_roots(tmp_path)
    artifacts.mkdir(parents=True)
    (artifacts / "keep.txt").write_bytes(b"keep")
    source = _linux_source(tmp_path / "source")
    before_bundle = _bytes(bundle)
    before_artifacts = _bytes(artifacts)
    original_replace = generator.os.replace
    calls = 0

    def fail_second_replace(source_path: str, destination_path: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish fault")
        original_replace(source_path, destination_path)

    monkeypatch.setattr(generator.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="publish fault"):
        _stage(source, bundle, artifacts)
    assert _bytes(bundle) == before_bundle
    assert _bytes(artifacts) == before_artifacts


def test_generator_requires_the_core_producer_to_create_bundle_root(
    tmp_path: Path,
) -> None:
    """A missing bundle root is a caller-contract violation, not mkdir input."""
    parent = tmp_path / "defaultspack"
    parent.mkdir()
    bundle = parent / "v4"
    artifacts = parent / "platform-artifacts"
    with pytest.raises(ValueError, match="bundle root must be a real directory"):
        generator._new_transaction(bundle, artifacts)
    assert not bundle.exists()
    assert not list(parent.glob(".tobkiri-defaultspack-transaction-*"))


def test_generator_rejects_existing_bundle_file_and_symlink(
    tmp_path: Path,
) -> None:
    """The producer contract never follows or replaces a foreign bundle root."""
    parent = tmp_path / "defaultspack"
    parent.mkdir()
    artifacts = parent / "platform-artifacts"
    bundle_file = parent / "v4-file"
    bundle_file.write_text("foreign", encoding="utf-8")
    with pytest.raises(ValueError, match="bundle root"):
        generator._new_transaction(bundle_file, artifacts)

    outside = tmp_path / "outside"
    outside.mkdir()
    bundle_link = parent / "v4-link"
    bundle_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        generator._new_transaction(bundle_link, artifacts)
    assert not list(parent.glob(".tobkiri-defaultspack-transaction-*"))


def test_generator_rejects_foreign_or_world_writable_bundle_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Output parents must remain host-owned and not group/world writable."""
    bundle, artifacts = _bundle_roots(tmp_path)
    parent = bundle.parent
    original_mode = parent.stat().st_mode
    try:
        parent.chmod(0o777)
        with pytest.raises(ValueError, match="writable permissions"):
            generator._new_transaction(bundle, artifacts)
        parent.chmod(0o755)

        monkeypatch.setattr(generator.os, "geteuid", lambda: os.getuid() + 1)
        with pytest.raises(ValueError, match="not owned"):
            generator._new_transaction(bundle, artifacts)
    finally:
        parent.chmod(original_mode)


def test_generator_rejects_hardlinked_bundle_input_without_touching_victim(
    tmp_path: Path,
) -> None:
    """A hard-linked source entry is residue, never an accepted bundle input."""
    bundle, artifacts = _bundle_roots(tmp_path)
    victim = tmp_path / "external-victim.json"
    victim.write_bytes(b"must remain")
    linked = bundle / "defaults-basepack.base.v1.json"
    linked.unlink()
    os.link(victim, linked)
    source = _linux_source(tmp_path / "source")
    with pytest.raises(ValueError, match="hard-linked"):
        _stage(source, bundle, artifacts)
    assert victim.read_bytes() == b"must remain"
    assert not list(tmp_path.glob(".tobkiri-defaultspack-transaction-*"))


@pytest.mark.parametrize(
    "source_commit", ["working-tree", "short", "a" * 40, "refs/heads/main"]
)
def test_generator_rejects_unverified_source_revision(
    tmp_path: Path, source_commit: str
) -> None:
    bundle, artifacts = _bundle_roots(tmp_path)
    source = _linux_source(tmp_path / "source")
    assert _SOURCE_PROVENANCE is not None
    provenance = json.loads(_SOURCE_PROVENANCE.read_text())
    provenance["source_commit"] = source_commit
    _SOURCE_PROVENANCE.chmod(0o600)
    _SOURCE_PROVENANCE.write_text(
        json.dumps(provenance, separators=(",", ":")), encoding="utf-8"
    )
    _SOURCE_PROVENANCE.chmod(0o400)
    with pytest.raises(
        generator_source_manifest.SourceProvenanceError,
        match="full lowercase 40-hex identity",
    ) as raised:
        _stage(source, bundle, artifacts)
    assert raised.value.code == generator_source_manifest.PROVENANCE_ERROR_SOURCE_COMMIT
    assert raised.value.reason == "source_commit must be a full lowercase 40-hex identity"
    assert str(tmp_path) not in str(raised.value)
    assert raised.value.__cause__ is None


def test_generator_requires_exact_clean_snapshot_provenance(tmp_path: Path) -> None:
    """Formal source identity and clean state are required before staging."""
    bundle, artifacts = _bundle_roots(tmp_path)
    source = _linux_source(tmp_path / "source")
    assert _SOURCE_PROVENANCE is not None
    provenance = json.loads(_SOURCE_PROVENANCE.read_text())
    provenance["source_clean"] = False
    _SOURCE_PROVENANCE.chmod(0o600)
    _SOURCE_PROVENANCE.write_text(
        json.dumps(provenance, separators=(",", ":")), encoding="utf-8"
    )
    _SOURCE_PROVENANCE.chmod(0o400)
    with pytest.raises(
        generator_source_manifest.SourceProvenanceError,
        match="source_clean",
    ) as raised:
        _stage(source, bundle, artifacts)
    assert raised.value.code == generator_source_manifest.PROVENANCE_ERROR_SOURCE_CLEAN
    assert raised.value.reason == "source_clean must be true"
    assert str(tmp_path) not in str(raised.value)
    assert raised.value.__cause__ is None


def test_generator_rejects_unverified_source_tree_without_leaking_paths(
    tmp_path: Path,
) -> None:
    """The wrapper preserves a typed tree reason without exposing its path."""
    bundle, artifacts = _bundle_roots(tmp_path)
    source = _linux_source(tmp_path / "source")
    assert _SOURCE_PROVENANCE is not None
    provenance = json.loads(_SOURCE_PROVENANCE.read_text())
    provenance["source_tree"] = "not-a-tree"
    _SOURCE_PROVENANCE.chmod(0o600)
    _SOURCE_PROVENANCE.write_text(
        json.dumps(provenance, separators=(",", ":")), encoding="utf-8"
    )
    _SOURCE_PROVENANCE.chmod(0o400)

    with pytest.raises(
        generator_source_manifest.SourceProvenanceError,
        match="full lowercase 40-hex identity",
    ) as raised:
        _stage(source, bundle, artifacts)
    assert raised.value.code == generator_source_manifest.PROVENANCE_ERROR_SOURCE_TREE
    assert raised.value.reason == "source_tree must be a full lowercase 40-hex identity"
    assert str(tmp_path) not in str(raised.value)
    assert raised.value.__cause__ is None


def test_source_snapshot_lease_rejects_chmod_and_root_swap(tmp_path: Path) -> None:
    """A direct Python caller keeps the private snapshot identity pinned."""
    _bundle_roots(tmp_path)
    assert _SOURCE_PROVENANCE is not None
    snapshot = _SOURCE_PROVENANCE.parent
    lease = generator_source_manifest.open_source_snapshot_lease(
        snapshot, _SOURCE_PROVENANCE
    )
    try:
        snapshot.chmod(0o755)
        with pytest.raises(ValueError, match="owner-writable|inventory changed"):
            lease.verify_unchanged()
    finally:
        lease.close()


def test_generator_has_no_git_subprocess_boundary() -> None:
    """The generator module cannot spawn or discover Git."""
    assert not hasattr(generator, "subprocess")
    assert not hasattr(generator, "_run_bound_git")
