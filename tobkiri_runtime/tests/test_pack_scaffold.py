"""Regression tests for the v4-only compatibility scaffold entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core_runtime.pack_scaffold import PackScaffold, VALID_TEMPLATES, main
from tobkiri_protocol.validation import validate_document


@pytest.fixture
def scaffold() -> PackScaffold:
    return PackScaffold()


@pytest.mark.parametrize("template", VALID_TEMPLATES)
def test_every_template_emits_complete_v4_without_legacy_files(
    scaffold: PackScaffold,
    tmp_path: Path,
    template: str,
) -> None:
    root = scaffold.generate("example.pack", tmp_path, template=template)

    manifest = validate_document((root / "pack.v4.json").read_bytes(), "pack")
    validate_document((root / "contracts.v4.json").read_bytes(), "pack_contract_catalog")
    validate_document((root / "artifact-index.v4.json").read_bytes(), "pack_artifact_index")
    validate_document((root / "executables.v4.json").read_bytes(), "executable_catalog")

    assert manifest["pack"]["id"] == "example.pack"
    assert manifest["requirements"]["execution_boundary"] == "declarative_only"
    assert manifest["requirements"]["capabilities"] == []
    assert manifest["functions"] == []
    assert manifest["migration"]["compatibility"] == "none"
    assert not (root / "ecosystem.json").exists()
    assert not (root / "rumi.pack.v3.json").exists()


def test_scaffold_is_byte_deterministic(scaffold: PackScaffold, tmp_path: Path) -> None:
    first = scaffold.generate("example.first", tmp_path / "one")
    second = scaffold.generate("example.first", tmp_path / "two")
    first_files = {
        path.relative_to(first).as_posix(): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second).as_posix(): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files


@pytest.mark.parametrize(
    "pack_id",
    ["", "Upper.case", "noseparator", "../escape", "bad space.pack", "a/b"],
)
def test_invalid_pack_identity_is_rejected(
    scaffold: PackScaffold,
    tmp_path: Path,
    pack_id: str,
) -> None:
    with pytest.raises(ValueError, match="pack_id"):
        scaffold.generate(pack_id, tmp_path)


def test_existing_or_force_target_never_overwrites(
    scaffold: PackScaffold,
    tmp_path: Path,
) -> None:
    root = tmp_path / "example.pack"
    root.mkdir()
    (root / "owned.txt").write_text("owned\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        scaffold.generate("example.pack", tmp_path)
    with pytest.raises(FileExistsError, match="force overwrite was retired"):
        scaffold.generate("example.pack", tmp_path, force=True)
    assert (root / "owned.txt").read_text(encoding="utf-8") == "owned\n"


def test_symlink_target_is_rejected(
    scaffold: PackScaffold,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "example.pack"
    try:
        target.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(ValueError, match="symlink"):
        scaffold.generate("example.pack", tmp_path)


def test_cli_reports_v4_and_refuses_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["example.pack", "--output", str(tmp_path)]) == 0
    assert "Pack v4 scaffold created" in capsys.readouterr().out
    assert main(["example.pack", "--output", str(tmp_path), "--force"]) == 1
    assert (tmp_path / "example.pack" / "pack.v4.json").is_file()


def test_integrity_documents_bind_exact_source_and_files(
    scaffold: PackScaffold,
    tmp_path: Path,
) -> None:
    root = scaffold.generate("example.pack", tmp_path, template="full")
    manifest = json.loads((root / "pack.v4.json").read_text(encoding="utf-8"))
    contracts = json.loads((root / "contracts.v4.json").read_text(encoding="utf-8"))
    executables = json.loads((root / "executables.v4.json").read_text(encoding="utf-8"))
    index = json.loads((root / "artifact-index.v4.json").read_text(encoding="utf-8"))

    identities = {
        manifest["integrity"]["source_identity"],
        contracts["source_identity"],
        executables["source_identity"],
        index["source_identity"],
    }
    assert len(identities) == 1
    assert index["artifact_set_digest"] == manifest["pack"]["artifact_digest"]
    assert {item["path"] for item in index["artifacts"]} >= {
        "pack.v4.json",
        "contracts.v4.json",
        "scaffold-source.v1.json",
    }
