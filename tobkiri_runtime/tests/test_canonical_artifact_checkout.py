"""Cross-platform byte-integrity checks for canonical Pack v4 artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile

import pytest

from ecosystem.defaultspack.domain.runtime_v4 import (
    BundleIntegrityError,
    BundledCatalog,
)


ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = ROOT.parent
ECOSYSTEM = ROOT / "ecosystem"
BUNDLE = ECOSYSTEM / "defaultspack" / "v4"
PACK_ARTIFACT_NAMES = (
    "artifact-index.v4.json",
    "contracts.v4.json",
    "executables.v4.json",
    "pack.v4.json",
)


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _integrity_bound_paths() -> tuple[Path, ...]:
    """Return every canonical v4 authority and raw-byte-bound artifact."""
    paths = {REPOSITORY_ROOT / ".gitattributes"}
    paths.update(path for path in BUNDLE.rglob("*") if path.is_file())
    paths.update((ROOT / "schemas").glob("*.json"))
    paths.update((ROOT / "tobkiri_protocol" / "schemas").glob("*.json"))
    paths.add(ROOT / "tobkiri_protocol" / "schema_hashes.json")

    for index_path in ECOSYSTEM.glob("*/artifact-index.v4.json"):
        pack_root = index_path.parent
        paths.update(pack_root / name for name in PACK_ARTIFACT_NAMES)
        index = json.loads(index_path.read_bytes())
        paths.update(pack_root / item["path"] for item in index["artifacts"])

    missing = [path for path in paths if not path.is_file()]
    assert not missing, f"canonical artifacts are missing: {missing}"
    return tuple(sorted(paths))


def _copy_paths(paths: tuple[Path, ...], destination: Path) -> None:
    for source in paths:
        relative = source.relative_to(REPOSITORY_ROOT)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _extract_index_blobs(archive: Path, destination: Path) -> None:
    """Extract regular Git archive members without trusting archive paths."""
    destination.mkdir()
    with tarfile.open(archive) as stream:
        for member in stream.getmembers():
            relative = PurePosixPath(member.name)
            assert not relative.is_absolute() and ".." not in relative.parts
            if not member.isfile():
                continue
            source = stream.extractfile(member)
            assert source is not None
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())


def _assert_pack_artifact_digests(root: Path) -> None:
    for index_path in (root / "tobkiri_runtime" / "ecosystem").glob("*/artifact-index.v4.json"):
        index = json.loads(index_path.read_bytes())
        for artifact in index["artifacts"]:
            raw = (index_path.parent / artifact["path"]).read_bytes()
            actual = f"sha256:{hashlib.sha256(raw).hexdigest()}"
            assert actual == artifact["digest"], artifact["path"]


def test_windows_checkout_preserves_all_canonical_v4_bytes(tmp_path: Path) -> None:
    """An autocrlf checkout must equal every canonical Git index blob."""
    paths = _integrity_bound_paths()
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    _copy_paths(paths, source_repo)
    _run_git("init", "--quiet", cwd=source_repo)
    _run_git("config", "user.email", "artifact-checkout@test.invalid", cwd=source_repo)
    _run_git("config", "user.name", "Artifact Checkout Test", cwd=source_repo)
    _run_git("config", "core.autocrlf", "false", cwd=source_repo)
    _run_git("add", ".", cwd=source_repo)
    _run_git("commit", "--quiet", "-m", "canonical artifacts", cwd=source_repo)

    archive = tmp_path / "index.tar"
    _run_git("archive", "--format=tar", f"--output={archive}", "HEAD", cwd=source_repo)
    index_tree = tmp_path / "index"
    _extract_index_blobs(archive, index_tree)

    checkout = tmp_path / "windows-checkout"
    checkout.mkdir()
    _run_git(
        "--git-dir",
        str(source_repo / ".git"),
        "--work-tree",
        str(checkout),
        "-c",
        "core.autocrlf=true",
        "checkout",
        "--quiet",
        "--force",
        "HEAD",
        cwd=tmp_path,
    )

    for original in paths:
        relative = original.relative_to(REPOSITORY_ROOT)
        index_bytes = (index_tree / relative).read_bytes()
        assert index_bytes == original.read_bytes(), relative
        assert (checkout / relative).read_bytes() == index_bytes, relative

    checkout_bundle = checkout / BUNDLE.relative_to(REPOSITORY_ROOT)
    BundledCatalog.load(checkout_bundle)
    _assert_pack_artifact_digests(checkout)


def test_bundle_path_and_byte_tampering_still_fail_closed(tmp_path: Path) -> None:
    """Line-ending policy must not weaken path or byte integrity checks."""
    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE, copied)
    manifest = copied / "packs" / "defaults-basepack.pack.v4.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(BundleIntegrityError, match="digest changed"):
        BundledCatalog.load(copied)

    shutil.rmtree(copied)
    shutil.copytree(BUNDLE, copied)
    lock_path = copied / "bundle.lock.json"
    lock = json.loads(lock_path.read_bytes())
    lock["entries"][0]["path"] = "../pack.v4.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    (tmp_path / "pack.v4.json").write_bytes(b"{}\n")
    with pytest.raises(BundleIntegrityError, match="escapes root"):
        BundledCatalog.load(copied)


def test_bundle_artifact_symlink_escape_fails_closed(tmp_path: Path) -> None:
    """A lock entry cannot redirect through a worktree symlink."""
    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE, copied)
    manifest = copied / "packs" / "defaults-basepack.pack.v4.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(manifest.read_bytes())
    manifest.unlink()
    try:
        manifest.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises(BundleIntegrityError, match="escapes root"):
        BundledCatalog.load(copied)
