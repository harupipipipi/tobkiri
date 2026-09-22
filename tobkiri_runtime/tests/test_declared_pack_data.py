"""Pinned declarative Pack data must never become a filesystem or code grant."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable

import pytest

import tobkiri_host.artifact_materialization as materialization
from tobkiri_host.errors import InvalidArtifactError
from tobkiri_protocol.canonical import canonical_digest


PACK_ID = "rumi_default_tools_pack"
SOURCE = Path(__file__).resolve().parents[1] / "ecosystem" / PACK_ID


@pytest.fixture
def data_pack(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / PACK_ID
    root.mkdir()
    manifest_bytes = (SOURCE / "pack.v4.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    (root / "pack.v4.json").write_bytes(manifest_bytes)
    for artifact in manifest["artifacts"]:
        target = root / artifact["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((SOURCE / artifact["path"]).read_bytes())
    return root, manifest["pack"]["artifact_digest"]


def _capture(
    root: Path, digest: str, prefix: str = "tools/"
) -> tuple[materialization.MaterializedArtifactFile, ...]:
    return materialization.capture_declared_pack_data(
        root, pack_id=PACK_ID, artifact_digest=digest, path_prefix=prefix
    )


def _reseal(root: Path, mutate: Callable[[dict[str, Any]], Any]) -> str:
    """Simulate a different Host-approved manifest for structural denials."""
    path = root / "pack.v4.json"
    manifest = json.loads(path.read_bytes())
    mutate(manifest)
    digest = canonical_digest(manifest["artifacts"])
    manifest["pack"]["artifact_digest"] = digest
    manifest["integrity"]["artifact_set_digest"] = digest
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return digest


def test_shipped_definitions_are_captured_without_functions_or_disk_writes(
    data_pack: tuple[Path, str],
) -> None:
    root, digest = data_pack
    before = {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }
    root.chmod(0o555)
    try:
        captured = _capture(root, digest)
        assert root.stat().st_mode & 0o777 == 0o555
    finally:
        root.chmod(0o755)
    expected = set(root.glob("tools/*/manifest.json"))
    assert expected
    assert {root / item.path for item in captured} == expected
    assert all(not item.executable for item in captured)
    calculator = next(item for item in captured if "/calculator/" in item.path)
    assert json.loads(calculator.content)["id"] == "calculator"
    assert {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    } == before
    (root / calculator.path).write_bytes(b"changed after capture")
    assert json.loads(calculator.content)["id"] == "calculator"


@pytest.mark.parametrize(
    "prefix", ["", "/", "../", "./", "tools", "tools//", "tools/../", "tools\\/"]
)
def test_data_prefix_cannot_escape_or_alias_a_directory(
    data_pack: tuple[Path, str], prefix: str
) -> None:
    with pytest.raises(InvalidArtifactError, match="prefix"):
        _capture(*data_pack, prefix)


def test_undeclared_file_is_not_discovered(data_pack: tuple[Path, str]) -> None:
    root, digest = data_pack
    extra = root / "tools/undeclared/manifest.json"
    extra.parent.mkdir()
    extra.write_bytes(b'{"id": "undeclared"}')
    assert extra not in {root / item.path for item in _capture(root, digest)}


@pytest.mark.parametrize("damage", ["changed", "missing", "symlink", "hardlink", "directory_link"])
def test_declared_data_requires_regular_unchanged_files(
    data_pack: tuple[Path, str], tmp_path: Path, damage: str
) -> None:
    root, digest = data_pack
    target = root / "tools/calculator/manifest.json"
    if damage == "changed":
        target.write_bytes(b"tampered")
    elif damage == "missing":
        target.unlink()
    elif damage == "directory_link":
        outside = tmp_path / "outside-tools"
        (root / "tools").rename(outside)
        (root / "tools").symlink_to(outside, target_is_directory=True)
    else:
        outside = tmp_path / "outside.json"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        if damage == "symlink":
            target.symlink_to(outside)
        else:
            os.link(outside, target)
    with pytest.raises(InvalidArtifactError):
        _capture(root, digest)


def test_rehashed_declarations_cannot_replace_the_host_selected_digest(
    data_pack: tuple[Path, str],
) -> None:
    root, original_digest = data_pack
    target = root / "tools/calculator/manifest.json"
    target.write_bytes(b'{"id": "forged"}')
    changed_digest = _reseal(
        root,
        lambda manifest: next(
            item
            for item in manifest["artifacts"]
            if item["path"].endswith("calculator/manifest.json")
        ).update(digest="sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()),
    )
    assert changed_digest != original_digest
    with pytest.raises(InvalidArtifactError, match="identity"):
        _capture(root, original_digest)


@pytest.mark.parametrize("damage", ["pack_id", "duplicate", "alias_path", "executable"])
def test_data_selection_does_not_accept_ambiguous_or_executable_declarations(
    data_pack: tuple[Path, str], damage: str
) -> None:
    root, digest = data_pack

    def mutate(manifest: dict[str, Any]) -> None:
        item = next(item for item in manifest["artifacts"] if item["path"].startswith("tools/"))
        if damage == "pack_id":
            manifest["pack"]["id"] = "different-pack"
        elif damage == "duplicate":
            manifest["artifacts"].append(dict(item))
        elif damage == "alias_path":
            item["path"] = item["path"].replace("tools/", "tools/../tools/", 1)
        else:
            item["kind"] = "executable"

    digest = _reseal(root, mutate)
    with pytest.raises(InvalidArtifactError):
        _capture(root, digest)


@pytest.mark.parametrize("limit", ["file", "total", "count"])
def test_pack_data_capture_has_finite_allocation_limits(
    data_pack: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    root, digest = data_pack
    if limit == "file":
        maximum = (root / "pack.v4.json").stat().st_size * 2
        target = root / "tools/calculator/manifest.json"
        target.write_bytes(b"x" * (maximum + 1))
        digest = _reseal(
            root,
            lambda manifest: next(
                item
                for item in manifest["artifacts"]
                if item["path"].endswith("calculator/manifest.json")
            ).update(digest="sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()),
        )
        monkeypatch.setattr(materialization, "_MAX_DATA_FILE_BYTES", maximum)
    elif limit == "total":
        monkeypatch.setattr(materialization, "_MAX_DATA_TOTAL_BYTES", 1)
    else:
        monkeypatch.setattr(materialization, "_MAX_DATA_FILES", 1)
    with pytest.raises(InvalidArtifactError, match="limit"):
        _capture(root, digest)


@pytest.mark.parametrize("swap", ["root", "manifest"])
def test_capture_rejects_changes_between_manifest_and_data_reads(
    data_pack: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    root, digest = data_pack
    replacement = tmp_path / "replacement"
    shutil.copytree(root, replacement)
    original_read = materialization._read_regular_file
    changed = False

    def read_then_swap(descriptor: int, relative: str, **kwargs: Any) -> tuple[bytes, int]:
        nonlocal changed
        result = original_read(descriptor, relative, **kwargs)
        if not changed:
            changed = True
            if swap == "root":
                root.rename(tmp_path / "old")
                replacement.rename(root)
            else:
                manifest = root / "pack.v4.json"
                manifest.write_bytes(manifest.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(materialization, "_read_regular_file", read_then_swap)
    with pytest.raises(InvalidArtifactError, match="changed"):
        _capture(root, digest)


def test_data_capture_uses_the_existing_windows_bounded_reader(
    data_pack: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, digest = data_pack
    monkeypatch.setattr(materialization, "_requires_windows_secure_reader", lambda: True)
    original = materialization.SecureDirectory.read_bytes_bounded
    reads = []

    def bounded(
        directory: materialization.SecureDirectory, relative: str, *, max_bytes: int | None
    ) -> bytes:
        reads.append((relative, max_bytes))
        return original(directory, relative, max_bytes=max_bytes)

    monkeypatch.setattr(materialization.SecureDirectory, "read_bytes_bounded", bounded)
    assert _capture(root, digest)
    assert reads
    assert all(limit == materialization._MAX_DATA_FILE_BYTES for _path, limit in reads)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO regression")
def test_fifo_substitution_is_rejected_before_open_can_block(
    data_pack: tuple[Path, str],
) -> None:
    """A child timeout makes a blocking-open regression fail without hanging CI."""
    root, digest = data_pack
    target = root / "tools/calculator/manifest.json"
    target.unlink()
    os.mkfifo(target)
    script = """
import sys
from pathlib import Path
from tobkiri_host.artifact_materialization import capture_declared_pack_data
from tobkiri_host.errors import InvalidArtifactError
try:
    capture_declared_pack_data(
        Path(sys.argv[1]), pack_id=sys.argv[2], artifact_digest=sys.argv[3],
        path_prefix='tools/',
    )
except InvalidArtifactError:
    raise SystemExit(0)
raise SystemExit('FIFO was accepted as Pack data')
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(root), PACK_ID, digest],
        cwd=SOURCE.parents[1],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
