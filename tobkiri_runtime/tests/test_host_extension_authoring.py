"""Offline Host successors preserve immutable Contracts through the real compiler."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from core_runtime import host_extension_authoring as authoring
from core_runtime.host_extension_authoring import (
    build_host_extension_successor,
    host_extension_successor_source,
)
from core_runtime.host_extension_update_v4 import (
    _authority_closure,
    _verified_pack,
    verify_host_extension_successor,
)
from core_runtime.pack_authoring import PackAuthoringError
from core_runtime.pack_signature import SIGNED_MANIFEST_RELATIVE
from scripts import migrate_pack_artifacts_v4 as migration
from scripts.stage_host_extension_successor_v4 import stage_host_extension_successor
from tests.test_host_extension_update_v4 import _sign
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import canonical_digest

ROOT = Path(__file__).resolve().parents[1]


def _read(root: Path, name: str) -> dict[str, Any]:
    return json.loads((root / name).read_bytes())


@pytest.fixture
def predecessor(tmp_path: Path) -> tuple[Path, str]:
    """Copy only the actual sealed Adapter inventory into a small owned fixture."""
    source = ROOT / "ecosystem/rumi_provider_adapters_pack"
    index = _read(source, "artifact-index.v4.json")
    paths = {item["path"] for item in index["artifacts"]} | {
        "artifact-index.v4.json",
    }
    previous = tmp_path / "previous"
    previous.mkdir()
    for name in sorted(paths):
        destination = previous / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source / name).read_bytes())
    return previous, compile_pack_root(previous).artifact.digest


def _build(previous: tuple[Path, str], target: Path, **kwargs: Any) -> Path:
    root, digest = previous
    return build_host_extension_successor(
        target,
        predecessor_root=root,
        expected_predecessor_digest=digest,
        version=kwargs.pop("version", "1.0.3"),
        replacements=kwargs.pop(
            "replacements",
            {
                "runtime/adapter.py": (root / "runtime/adapter.py").read_bytes()
                + b'\nraise RuntimeError("authoring must never execute Pack code")\n',
                "runtime/streaming.py": (root / "runtime/streaming.py").read_bytes()
                + b"\n# captured helper update\n",
            },
        ),
        **kwargs,
    )


def test_implementation_successor_preserves_contract_provenance_and_authority(
    predecessor: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """The source identity changes while entire historical Contracts stay exact."""
    previous, _ = predecessor
    before = {
        name: path.read_bytes()
        for path in previous.rglob("*")
        if path.is_file()
        for name in [path.relative_to(previous).as_posix()]
    }
    successor = _build(predecessor, tmp_path / "successor")
    old = _verified_pack(previous, require_signed=False)
    new = _verified_pack(successor, require_signed=False)
    assert new.compiled.artifact.digest != old.compiled.artifact.digest
    assert (
        new.documents["contracts.v4.json"]["contracts"]
        == old.documents["contracts.v4.json"]["contracts"]
    )
    assert (
        new.documents["contracts.v4.json"]["source_identity"]
        != old.documents["contracts.v4.json"]["source_identity"]
    )
    assert canonical_digest(_authority_closure(new)) == canonical_digest(
        _authority_closure(old)
    )
    assert new.documents["pack.v4.json"]["provenance"]["source_kind"] == "generated"
    assert (
        new.documents["pack.v4.json"]["provenance"]["repository_commit"]
        == "working-tree"
    )
    provenance = new.documents["pack.v4.json"]["provenance"]
    assert provenance["source_path"] == "urn:tobkiri:host-extension-successor-source:v1"
    recipe = host_extension_successor_source(
        predecessor_artifact_digest=old.compiled.artifact.digest,
        pack_id=new.compiled.artifact.pack_id,
        version=new.compiled.artifact.version,
        replacement_digests={
            item["path"]: item["digest"] for item in provenance["evidence"][1:]
        },
    )
    assert canonical_digest(recipe) == provenance["source_digest"]
    assert (
        canonical_digest(recipe)
        == new.documents["pack.v4.json"]["integrity"]["source_identity"]
    )
    assert not (successor / SIGNED_MANIFEST_RELATIVE).exists()
    assert {
        p.relative_to(successor).as_posix() for p in successor.rglob("*") if p.is_file()
    } == set(before)
    assert all(
        (previous / name).read_bytes() == content for name, content in before.items()
    )


def test_real_canary_signature_passes_unchanged_full_compatibility_verifier(
    predecessor: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """An actual temporary test signature proves the ordinary final signing path."""
    successor = _build(predecessor, tmp_path / "successor")
    signed = _sign(successor)  # Ephemeral test key; no Host key/policy store.
    result = verify_host_extension_successor(
        predecessor[0],
        successor,
        expected_predecessor_digest=predecessor[1],
        publisher_id="tobkiri.repository",
        signed_manifest=signed,
    )
    assert result.version == "1.0.3"
    assert result.predecessor_digest == predecessor[1]


@pytest.mark.parametrize(
    "name",
    [
        "runtime/new.py",
        "runtime/../adapter.py",
        "contracts.v4.json",
        "README.md",
        "/runtime/adapter.py",
        SIGNED_MANIFEST_RELATIVE,
    ],
)
def test_replacements_cannot_add_paths_or_modify_metadata(
    predecessor: tuple[Path, str],
    tmp_path: Path,
    name: str,
) -> None:
    """Only unchanged existing runtime classifications permit captured bytes."""
    target = tmp_path / "successor"
    with pytest.raises(ValueError):
        _build(predecessor, target, replacements={name: b"# unauthorized path\n"})
    assert not target.exists()


@pytest.mark.parametrize("version", ["1.0.2", "1.0.1", "invalid", True])
def test_version_must_advance(
    predecessor: tuple[Path, str],
    tmp_path: Path,
    version: object,
) -> None:
    """A code update cannot reuse or downgrade its predecessor version."""
    with pytest.raises(ValueError):
        _build(predecessor, tmp_path / "successor", version=version)
    assert not (tmp_path / "successor").exists()


def test_unchanged_runtime_and_stale_predecessor_are_denied(
    predecessor: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Both predecessor binding and changed implementation bytes are explicit."""
    with pytest.raises(PackAuthoringError, match="changed runtime"):
        _build(
            predecessor,
            tmp_path / "unchanged",
            replacements={
                "runtime/adapter.py": (
                    predecessor[0] / "runtime/adapter.py"
                ).read_bytes(),
            },
        )
    with pytest.raises(PackAuthoringError, match="stale"):
        build_host_extension_successor(
            tmp_path / "stale",
            predecessor_root=predecessor[0],
            expected_predecessor_digest="sha256:" + "0" * 64,
            version="1.0.3",
            replacements={"runtime/adapter.py": b"# code\n"},
        )


@pytest.mark.parametrize("change", ["contract_provenance", "capability", "path"])
def test_structural_compatibility_is_required_before_publication(
    predecessor: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """A metadata-producer regression cannot silently broaden a Host boundary."""
    original = authoring.render_artifact_documents

    def altered(manifest: dict, contracts: dict, executable: dict, index: list) -> dict:
        manifest, contracts, executable, index = deepcopy(
            (manifest, contracts, executable, index)
        )
        if change == "contract_provenance":
            contracts["contracts"][0]["provenance"]["source_digest"] = (
                "sha256:" + "0" * 64
            )
        elif change == "capability":
            manifest["requirements"]["capabilities"].append("unexpected.capability")
        else:
            index[0]["role"] = "asset"
        return original(manifest, contracts, executable, index)

    monkeypatch.setattr(authoring, "render_artifact_documents", altered)
    with pytest.raises(ValueError):
        _build(predecessor, tmp_path / "successor")
    assert not (tmp_path / "successor").exists()


def test_existing_target_and_symlink_parent_are_preserved(
    predecessor: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """Neither a prior output nor a symlink can redirect successor publication."""
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep").write_bytes(b"keep")
    with pytest.raises(PackAuthoringError):
        _build(predecessor, target)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        _build(predecessor, link / "outside")
    assert list(target.iterdir()) == [target / "keep"]


@pytest.mark.parametrize(
    "pack_id",
    [
        "defaultspack",
        "rumi_provider_adapters_pack",
        "rumi_ai_gateway_pack",
    ],
)
def test_shared_metadata_sealer_keeps_default_generation_byte_identical(
    pack_id: str,
) -> None:
    """Existing canonical sources retain the exact pre-refactor generated bytes."""
    record = next(
        item
        for item in json.loads(migration.CATALOG.read_bytes())["packs"]
        if item["pack_id"] == pack_id
    )
    rendered = migration._render_record(record)
    for name, content in rendered.items():
        assert (
            content.encode("utf-8")
            == (migration.ECOSYSTEM / pack_id / name).read_bytes()
        )


def test_cli_captures_explicit_existing_paths_only(
    predecessor: tuple[Path, str],
    tmp_path: Path,
) -> None:
    """The offline stage command consumes bytes without touching its source tree."""
    replacement = tmp_path / "replacement"
    (replacement / "runtime").mkdir(parents=True)
    code = (predecessor[0] / "runtime/adapter.py").read_bytes() + b"\n# source update\n"
    (replacement / "runtime/adapter.py").write_bytes(code)
    successor = stage_host_extension_successor(
        predecessor[0],
        tmp_path / "successor",
        expected_predecessor_digest=predecessor[1],
        version="1.0.3",
        replacement_root=replacement,
        replacement_paths=["runtime/adapter.py"],
    )
    assert (successor / "runtime/adapter.py").read_bytes() == code
    assert (replacement / "runtime/adapter.py").read_bytes() == code
    with pytest.raises(ValueError, match="unique"):
        stage_host_extension_successor(
            predecessor[0],
            tmp_path / "duplicate",
            expected_predecessor_digest=predecessor[1],
            version="1.0.3",
            replacement_root=replacement,
            replacement_paths=["runtime/adapter.py", "runtime/adapter.py"],
        )


def test_competing_destination_cannot_be_overwritten_during_publication(
    predecessor: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic no-replace publication preserves a destination appearing late."""
    target = tmp_path / "successor"
    publish = authoring._rename_noreplace

    def competitor(parent_fd: int, source: str, destination: str) -> None:
        target.mkdir()
        (target / "pack.v4.json").write_bytes(b"competing author bytes")
        publish(parent_fd, source, destination)

    monkeypatch.setattr(authoring, "_rename_noreplace", competitor)
    with pytest.raises(FileExistsError):
        _build(predecessor, target)
    assert (target / "pack.v4.json").read_bytes() == b"competing author bytes"
    assert list(target.iterdir()) == [target / "pack.v4.json"]


def test_staged_leaf_exchanged_for_fifo_is_rejected_without_blocking(
    predecessor: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-write mode operation cannot block on a substituted named pipe."""
    opened = os.open
    publish = authoring._publish_successor
    armed = {"value": False}

    def staging(*args: Any, **kwargs: Any) -> Path:
        armed["value"] = True
        return publish(*args, **kwargs)

    def exchange(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if armed["value"] and path == "adapter.py" and flags & os.O_NONBLOCK:
            armed["value"] = False
            os.unlink(path, dir_fd=kwargs["dir_fd"])
            os.mkfifo(path, dir_fd=kwargs["dir_fd"])
        return opened(path, flags, *args, **kwargs)

    monkeypatch.setattr(authoring, "_publish_successor", staging)
    monkeypatch.setattr(os, "open", exchange)
    with pytest.raises(PackAuthoringError, match="identity changed"):
        _build(predecessor, tmp_path / "successor")
    assert not (tmp_path / "successor").exists()
