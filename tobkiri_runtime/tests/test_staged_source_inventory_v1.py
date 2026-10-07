"""Preserved Source classification cannot become runtime admission or a skip."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from scripts.quality import staged_source_inventory_v1 as helper
from scripts.quality.staged_source_inventory_v1 import load_staged_source_inventory

SOURCE_IDS = ("tobkiri_surface_renderer_pack", "tobkiri_voice_agent_pack")
SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas/staged_source_inventory_v1.schema.json"
)


def _write(root: Path, payload: Any) -> None:
    path = root / "schemas/staged_source_inventory.v1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = tmp_path / "runtime"
    (root / "schemas").mkdir(parents=True)
    (root / "schemas/staged_source_inventory_v1.schema.json").write_bytes(
        SCHEMA_PATH.read_bytes()
    )
    rows = []
    for source_id in SOURCE_IDS:
        directory = root / "ecosystem" / source_id
        directory.mkdir(parents=True)
        raw = b"Independent test Source; no installed Pack or Function.\n"
        (directory / "README.md").write_bytes(raw)
        rows.append(
            {
                "source_id": source_id,
                "source_root": f"ecosystem/{source_id}",
                "state": "staged_unadmitted",
                "runtime_authority": False,
                "installed_claim": False,
                "profile_selection": False,
                "files": [
                    {
                        "path": "README.md",
                        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    }
                ],
            }
        )
    payload = {"schema": "io.tobkiri.staged-source-inventory.v1", "records": rows}
    _write(root, payload)
    catalog = {"pack_ids": ["fixture_pack"], "excluded_packs": [], "packs": []}
    for name in ("manifest_authority.v1.json", "executable_sources.v1.json"):
        (root / "schemas" / name).write_text('{"packs": {}}')
    return root, catalog, payload


def _assert_denied(root: Path, catalog: dict[str, Any]) -> None:
    result = load_staged_source_inventory(root, catalog)
    assert result.source_ids == frozenset()
    assert result.records == ()
    assert result.files_digest is None
    assert [item.rule for item in result.findings] == [
        "staged_source_inventory_invalid"
    ]
    assert result.proof()["status"] == "RED"


def test_exact_reviewed_sources_have_separate_non_admission_proof(
    tmp_path: Path,
) -> None:
    """Source proof carries actual bytes and never runtime or installed claims."""
    root, catalog, payload = _fixture(tmp_path)
    Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).validate(payload)
    result = load_staged_source_inventory(root, catalog)
    assert not result.findings
    assert result.source_ids == frozenset(SOURCE_IDS)
    proof = result.proof()
    assert proof["source_count"] == 2
    assert proof["records"] == payload["records"]
    assert (
        proof["document_digest"]
        == "sha256:"
        + hashlib.sha256(
            (root / "schemas/staged_source_inventory.v1.json").read_bytes()
        ).hexdigest()
    )
    assert all(
        row["state"] == "staged_unadmitted"
        and row["runtime_authority"] is False
        and row["installed_claim"] is False
        and row["profile_selection"] is False
        for row in proof["records"]
    )
    assert "release_verified" not in proof


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_record",
        "extra_record",
        "duplicate_identity",
        "unknown_identity",
        "unknown_top_field",
        "unknown_record_field",
        "unknown_file_field",
        "root_rebound",
        "state_admitted",
        "runtime_authority",
        "installed_claim",
        "profile_selection",
        "numeric_false",
        "duplicate_file",
        "bad_digest",
        "traversal",
        "absolute_path",
        "backslash",
        "unsorted_records",
    ],
)
def test_modified_inventory_cannot_classify_any_source(
    tmp_path: Path,
    mutation: str,
) -> None:
    """A malformed record never partially classifies the other valid root."""
    root, catalog, payload = _fixture(tmp_path)
    row = payload["records"][0]
    if mutation == "missing_record":
        payload["records"].pop()
    elif mutation == "extra_record":
        payload["records"].append(copy.deepcopy(row))
    elif mutation == "duplicate_identity":
        payload["records"][1] = copy.deepcopy(row)
    elif mutation == "unknown_identity":
        row["source_id"] = "unreviewed_pack"
    elif mutation == "unknown_top_field":
        payload["approved"] = True
    elif mutation == "unknown_record_field":
        row["approved"] = True
    elif mutation == "unknown_file_field":
        row["files"][0]["approved"] = True
    elif mutation == "root_rebound":
        row["source_root"] = "ecosystem/fixture_pack"
    elif mutation == "state_admitted":
        row["state"] = "installed"
    elif mutation in {"runtime_authority", "installed_claim", "profile_selection"}:
        row[mutation] = True
    elif mutation == "numeric_false":
        row["runtime_authority"] = 0
    elif mutation == "duplicate_file":
        row["files"].append(copy.deepcopy(row["files"][0]))
    elif mutation == "bad_digest":
        row["files"][0]["sha256"] = "sha256:" + "a" * 64
    elif mutation in {"traversal", "absolute_path", "backslash"}:
        row["files"][0]["path"] = {
            "traversal": "../README.md",
            "absolute_path": "/README.md",
            "backslash": "nested\\README.md",
        }[mutation]
    elif mutation == "unsorted_records":
        payload["records"].reverse()
    _write(root, payload)
    _assert_denied(root, catalog)


def test_duplicate_json_fields_are_not_silently_overwritten(tmp_path: Path) -> None:
    """Duplicate JSON keys fail before the record can be classified."""
    root, catalog, payload = _fixture(tmp_path)
    raw = json.dumps(payload)
    raw = raw.replace('"records":', '"schema": "other", "records":', 1)
    (root / "schemas/staged_source_inventory.v1.json").write_text(raw)
    _assert_denied(root, catalog)


@pytest.mark.parametrize("mutation", ["changed_bytes", "missing_file", "extra_file"])
def test_actual_source_file_set_and_bytes_are_exact(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Source edits or unrecorded additions invalidate the entire Source set."""
    root, catalog, _ = _fixture(tmp_path)
    path = root / "ecosystem" / SOURCE_IDS[0] / "README.md"
    if mutation == "changed_bytes":
        path.write_text("Unreviewed replacement")
    elif mutation == "missing_file":
        path.unlink()
    else:
        path.with_name("extra.py").write_text("unreviewed = True\n")
    _assert_denied(root, catalog)


@pytest.mark.parametrize(
    "name",
    [
        "pack.v4.json",
        "contracts.v4.json",
        "executables.v4.json",
        "artifact-index.v4.json",
        "ecosystem.json",
        "rumi.pack.v3.json",
        "compatibility-alias.v1.json",
        "Pack.v4.json",
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_even_digest_bound_authority_artifacts_cannot_be_staged(
    tmp_path: Path,
    name: str,
    nested: bool,
) -> None:
    """A record hash cannot authorize a Pack or fabricate a legacy alias."""
    root, catalog, payload = _fixture(tmp_path)
    relative = f"nested/{name}" if nested else name
    path = root / "ecosystem" / SOURCE_IDS[0] / relative
    path.parent.mkdir(exist_ok=True)
    raw = b"{}"
    path.write_bytes(raw)
    payload["records"][0]["files"].append(
        {
            "path": relative,
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        }
    )
    payload["records"][0]["files"].sort(key=lambda file: file["path"])
    _write(root, payload)
    _assert_denied(root, catalog)


@pytest.mark.parametrize("kind", ["file_symlink", "directory_symlink", "hardlink"])
def test_source_links_cannot_escape_or_alias_reviewed_bytes(
    tmp_path: Path,
    kind: str,
) -> None:
    """Matching bytes through links never substitute for regular Source files."""
    root, catalog, _ = _fixture(tmp_path)
    source = root / "ecosystem" / SOURCE_IDS[0] / "README.md"
    target = tmp_path / "outside.md"
    if kind == "hardlink":
        os.link(source, target)
    elif kind == "file_symlink":
        source.rename(target)
        source.symlink_to(target)
    else:
        directory = tmp_path / "outside"
        directory.mkdir()
        source.with_name("nested").symlink_to(directory, target_is_directory=True)
    _assert_denied(root, catalog)


@pytest.mark.parametrize(
    "authority", ["production", "alias", "manifest", "function", "profile"]
)
def test_source_identity_is_disjoint_from_every_bundled_authority(
    tmp_path: Path,
    authority: str,
) -> None:
    """Actual bundled selections override any false non-admission flags."""
    root, catalog, _ = _fixture(tmp_path)
    source_id = SOURCE_IDS[0]
    if authority in {"production", "alias"}:
        key = "pack_ids" if authority == "production" else "excluded_packs"
        catalog[key].append(source_id)
    elif authority == "manifest":
        (root / "schemas/manifest_authority.v1.json").write_text(
            json.dumps({"packs": {source_id: "v4-authoritative"}})
        )
    elif authority == "function":
        (root / "schemas/executable_sources.v1.json").write_text(
            json.dumps({"packs": {source_id + ".execute": {"pack_id": source_id}}})
        )
    else:
        catalog["pack_ids"].append("defaultspack")
        profile = root / "ecosystem/defaultspack/v4/defaults.profile.v5.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(json.dumps({"packs": [{"pack_id": source_id}]}))
    _assert_denied(root, catalog)


@pytest.mark.parametrize(
    "relative",
    [
        "schemas/staged_source_inventory.v1.json",
        "schemas/staged_source_inventory_v1.schema.json",
        "schemas/manifest_authority.v1.json",
        "schemas/executable_sources.v1.json",
    ],
)
def test_missing_required_source_metadata_fails_closed(
    tmp_path: Path,
    relative: str,
) -> None:
    """Missing schema or authority metadata cannot imply non-admission."""
    root, catalog, _ = _fixture(tmp_path)
    (root / relative).unlink()
    _assert_denied(root, catalog)


def test_changed_schema_definition_fails_closed(tmp_path: Path) -> None:
    """A different schema cannot reinterpret the closed Source records."""
    root, catalog, _ = _fixture(tmp_path)
    schema = root / "schemas/staged_source_inventory_v1.schema.json"
    payload = json.loads(schema.read_text())
    payload["additionalProperties"] = True
    schema.write_text(json.dumps(payload))
    _assert_denied(root, catalog)


@pytest.mark.parametrize("kind", ["root_parent", "authority_parent"])
def test_intermediate_source_path_links_are_rejected(
    tmp_path: Path,
    kind: str,
) -> None:
    """A regular leaf never hides an intermediate linked Source directory."""
    root, catalog, _ = _fixture(tmp_path)
    if kind == "root_parent":
        actual = tmp_path / "actual-parent"
        actual.mkdir()
        root.rename(actual / "runtime")
        alias = tmp_path / "linked-parent"
        alias.symlink_to(actual, target_is_directory=True)
        root = alias / "runtime"
    else:
        actual = tmp_path / "actual-schema"
        (root / "schemas").rename(actual)
        (root / "schemas").symlink_to(actual, target_is_directory=True)
    _assert_denied(root, catalog)


def test_empty_directories_are_bounded_separately_from_source_files(
    tmp_path: Path,
) -> None:
    """An unchanged one-file record cannot allow an unbounded directory walk."""
    root, catalog, _ = _fixture(tmp_path)
    stage = root / "ecosystem" / SOURCE_IDS[0]
    for index in range(32):
        (stage / f"empty-{index}").mkdir()
    _assert_denied(root, catalog)


def test_source_file_byte_limit_is_checked_before_reading(tmp_path: Path) -> None:
    """An oversized source does not allocate an unbounded verification buffer."""
    root, catalog, _ = _fixture(tmp_path)
    path = root / "ecosystem" / SOURCE_IDS[0] / "README.md"
    with path.open("wb") as stream:
        stream.truncate(1_048_577)
    _assert_denied(root, catalog)


def test_authority_metadata_uses_its_own_finite_larger_bound(tmp_path: Path) -> None:
    """A realistic authority document need not fit the tiny inventory bound."""
    root, catalog, _ = _fixture(tmp_path)
    metadata = root / "schemas/executable_sources.v1.json"
    metadata.write_text(json.dumps({"packs": {}, "padding": "a" * 200_000}))
    assert not load_staged_source_inventory(root, catalog).findings
    with metadata.open("wb") as stream:
        stream.truncate(2_097_153)
    _assert_denied(root, catalog)


def test_link_swap_without_o_nofollow_never_reads_the_wrong_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The portable fd-identity fallback rejects a swapped link before read."""
    root, catalog, _ = _fixture(tmp_path)
    source = root / "ecosystem" / SOURCE_IDS[0] / "README.md"
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"Outside the reviewed Source")
    real_open = os.open
    real_fdopen = os.fdopen
    wrong_descriptor_read = False

    def swapped_open(path: Path, flags: int) -> int:
        if Path(path) == source:
            source.rename(tmp_path / "original.md")
            source.symlink_to(outside)
        return real_open(path, flags)

    def observed_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        nonlocal wrong_descriptor_read
        metadata = os.fstat(descriptor)
        target = outside.stat()
        if (metadata.st_dev, metadata.st_ino) == (target.st_dev, target.st_ino):
            wrong_descriptor_read = True
        return real_fdopen(descriptor, *args, **kwargs)

    monkeypatch.delattr(helper.os, "O_NOFOLLOW", raising=False)
    monkeypatch.setattr(helper.os, "open", swapped_open)
    monkeypatch.setattr(helper.os, "fdopen", observed_fdopen)
    _assert_denied(root, catalog)
    assert wrong_descriptor_read is False
