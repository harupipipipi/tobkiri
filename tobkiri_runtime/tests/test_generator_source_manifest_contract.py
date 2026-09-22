"""Focused tests for the sealed source-provenance JSON boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import generator_source_manifest

pytestmark = pytest.mark.contract


COMMIT = "0123456789abcdef0123456789abcdef01234567"
TREE = "89abcdef0123456789abcdef0123456789abcdef"


def _provenance_bytes(manifest_digest: str, fields: list[tuple[str, object]]) -> bytes:
    """Encode a provenance object with caller-selected field ordering."""
    return json.dumps(dict(fields), separators=(",", ":")).encode("utf-8")


def _root_with_provenance(tmp_path: Path, payload: bytes) -> Path:
    """Create the minimum immutable parser fixture and bypass closure traversal."""
    root = tmp_path / "snapshot"
    root.mkdir()
    manifest = root / generator_source_manifest.SOURCE_MANIFEST_FILENAME
    manifest.write_bytes(b"manifest fixture")
    provenance = root / generator_source_manifest.SOURCE_PROVENANCE_FILENAME
    provenance.write_bytes(payload)
    provenance.chmod(0o444)
    root.chmod(0o555)
    return root


def test_provenance_field_order_is_not_a_security_condition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact key set is enforced while valid JSON ordering remains free."""
    manifest_digest = hashlib.sha256(b"manifest fixture").hexdigest()
    payload = _provenance_bytes(
        manifest_digest,
        [
            ("source_manifest_sha256", manifest_digest),
            ("source_clean", True),
            ("source_tree", TREE),
            ("schema", generator_source_manifest.SOURCE_PROVENANCE_SCHEMA),
            ("source_commit", COMMIT),
        ],
    )
    root = _root_with_provenance(tmp_path, payload)
    monkeypatch.setattr(generator_source_manifest, "verify_source_closure", lambda _: {})

    result = generator_source_manifest.load_source_provenance(root)

    assert result.source_commit == COMMIT
    assert result.source_tree == TREE
    assert result.source_manifest_sha256 == manifest_digest


def test_provenance_duplicate_key_is_rejected(tmp_path: Path) -> None:
    """Duplicate JSON keys cannot smuggle a value past strict parsing."""
    manifest_digest = hashlib.sha256(b"manifest fixture").hexdigest()
    raw = (
        b'{"schema":"'
        + generator_source_manifest.SOURCE_PROVENANCE_SCHEMA.encode()
        + b'","schema":"duplicate","source_commit":"'
        + COMMIT.encode()
        + b'","source_tree":"'
        + TREE.encode()
        + b'","source_clean":true,"source_manifest_sha256":"'
        + manifest_digest.encode()
        + b'"}'
    )
    root = _root_with_provenance(tmp_path, raw)

    with pytest.raises(
        generator_source_manifest.SourceProvenanceError,
        match="duplicate source provenance field",
    ) as raised:
        generator_source_manifest.load_source_provenance(root)
    assert raised.value.code == generator_source_manifest.PROVENANCE_ERROR_DUPLICATE_FIELD
    assert str(tmp_path) not in str(raised.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "missing",
        "wrong-type",
        "wrong-digest",
        "wrong-commit-type",
        "wrong-tree-type",
    ],
)
def test_provenance_exact_keys_types_and_digests_are_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Unknown/missing fields and invalid values fail closed."""
    manifest_digest = hashlib.sha256(b"manifest fixture").hexdigest()
    fields: dict[str, object] = {
        "schema": generator_source_manifest.SOURCE_PROVENANCE_SCHEMA,
        "source_commit": COMMIT,
        "source_tree": TREE,
        "source_clean": True,
        "source_manifest_sha256": manifest_digest,
    }
    if mutation == "unknown":
        fields["extra"] = "reject"
    elif mutation == "missing":
        del fields["source_tree"]
    elif mutation == "wrong-type":
        fields["source_clean"] = 1
    else:
        if mutation == "wrong-digest":
            fields["source_manifest_sha256"] = "SHA256:" + manifest_digest
        elif mutation == "wrong-commit-type":
            fields["source_commit"] = {"not": "a string"}
        else:
            fields["source_tree"] = 42
    root = _root_with_provenance(
        tmp_path,
        json.dumps(fields, separators=(",", ":")).encode("utf-8"),
    )
    monkeypatch.setattr(generator_source_manifest, "verify_source_closure", lambda _: {})

    expected_codes = {
        "unknown": generator_source_manifest.PROVENANCE_ERROR_UNKNOWN_FIELD,
        "missing": generator_source_manifest.PROVENANCE_ERROR_MISSING_FIELD,
        "wrong-type": generator_source_manifest.PROVENANCE_ERROR_SOURCE_CLEAN_TYPE,
        "wrong-digest": generator_source_manifest.PROVENANCE_ERROR_MANIFEST_DIGEST_FORMAT,
        "wrong-commit-type": generator_source_manifest.PROVENANCE_ERROR_SOURCE_COMMIT_TYPE,
        "wrong-tree-type": generator_source_manifest.PROVENANCE_ERROR_SOURCE_TREE_TYPE,
    }
    with pytest.raises(generator_source_manifest.SourceProvenanceError) as raised:
        generator_source_manifest.load_source_provenance(root)
    assert raised.value.code == expected_codes[mutation]
    assert raised.value.reason
    assert str(tmp_path) not in str(raised.value)


@pytest.mark.parametrize("relative", ["scripts/__pycache__/attack.pyc", "scripts/attack.pyo"])
def test_source_manifest_rejects_generated_python_bytecode(
    tmp_path: Path, relative: str
) -> None:
    """Ignored bytecode is a structural closure violation, never an input."""
    root = tmp_path / "runtime"
    for directory in generator_source_manifest.SOURCE_ROOTS:
        (root / directory).mkdir(parents=True, exist_ok=True)
    for source in generator_source_manifest.SOURCE_FILES:
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}\n")
    bytecode = root / relative
    bytecode.parent.mkdir(parents=True, exist_ok=True)
    bytecode.write_bytes(b"ignored attacker bytecode")
    with pytest.raises(ValueError, match="generated Python bytecode"):
        generator_source_manifest.build_source_manifest(root)


def test_source_manifest_declares_root_executable_catalog_sidecar() -> None:
    """The packaged source closure must copy the root v4 catalog into staging."""
    relative = "ecosystem/defaultspack/executables.v4.json"
    assert relative in generator_source_manifest.SOURCE_FILES
    manifest = generator_source_manifest.load_source_manifest()
    assert any(entry["path"] == relative for entry in manifest["files"])
