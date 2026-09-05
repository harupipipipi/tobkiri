from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))

from scripts.quality.scan_defaultspack_integrity import (  # noqa: E402
    check_v4_integrity,
)


def _copy_v4_pack(tmp_path: Path) -> Path:
    """Copy only the files consumed by the canonical v4 integrity scanner."""
    pack_root = tmp_path / "defaultspack"
    pack_root.mkdir()
    for filename in (
        "pack.v4.json",
        "contracts.v4.json",
        "artifact-index.v4.json",
        "executables.v4.json",
        "host_contract_contributions.v1.json",
        "update_metadata.v1.json",
    ):
        shutil.copy2(DEFAULTSPACK_ROOT / filename, pack_root / filename)
    shutil.copytree(DEFAULTSPACK_ROOT / "runtime", pack_root / "runtime")
    shutil.copytree(DEFAULTSPACK_ROOT / "v4", pack_root / "v4")
    return pack_root


def _v4_errors(pack_root: Path) -> list[str]:
    errors: list[str] = []
    check_v4_integrity(errors, pack_root, strict=True)
    return errors


def _bundled_defaultspack_projection(pack_root: Path) -> Path:
    """Return the generated Defaultspack Pack projection in a copied bundle."""

    return pack_root / "v4" / "packs" / "defaultspack.pack.v4.json"


def _write_projection(pack_root: Path, projection: dict[str, object]) -> None:
    """Write a deliberate projection tamper without repairing its lock pin."""

    _bundled_defaultspack_projection(pack_root).write_text(
        json.dumps(projection, indent=2) + "\n",
        encoding="utf-8",
    )


def test_defaultspack_integrity_scan_strict_passes():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/quality/scan_defaultspack_integrity.py",
            "--strict",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


def test_v4_integrity_rejects_byte_identical_defaultspack_projection(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    _bundled_defaultspack_projection(pack_root).write_bytes(
        (pack_root / "pack.v4.json").read_bytes()
    )

    errors = _v4_errors(pack_root)

    assert "bundled defaultspack Pack must be a generated projection" in errors


def test_v4_integrity_rejects_projection_provenance_input_tampering(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    projection_path = _bundled_defaultspack_projection(pack_root)
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    projection["provenance"]["source_digest"] = "sha256:" + "0" * 64
    projection["provenance"]["input_inventory_digest"] = "sha256:" + "1" * 64
    _write_projection(pack_root, projection)

    errors = _v4_errors(pack_root)

    assert any(error.endswith("source_digest") for error in errors)
    assert any(error.endswith("input_inventory_digest") for error in errors)


def test_v4_integrity_rejects_projection_generator_or_identity_tampering(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    projection_path = _bundled_defaultspack_projection(pack_root)
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    canonical = json.loads((pack_root / "pack.v4.json").read_text(encoding="utf-8"))
    projection["provenance"]["generator_digest"] = "sha256:" + "0" * 64
    projection["integrity"]["source_identity"] = canonical["integrity"][
        "source_identity"
    ]
    _write_projection(pack_root, projection)

    errors = _v4_errors(pack_root)

    assert any(error.endswith("generator_digest") for error in errors)
    assert "bundled defaultspack projection source identity is stale" in errors
    assert "bundled defaultspack projection reused canonical source identity" in errors


def test_v4_integrity_rejects_missing_document(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    (pack_root / "pack.v4.json").unlink()

    errors = _v4_errors(pack_root)

    assert any("missing" in error and "pack.v4.json" in error for error in errors)


def test_v4_integrity_rejects_tampered_runtime_bytes(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    runtime = pack_root / "runtime" / "conversation.py"
    runtime.write_bytes(runtime.read_bytes() + b"\n# tampered\n")

    errors = _v4_errors(pack_root)

    assert any("hash mismatch" in error or "digest mismatch" in error for error in errors)


def test_v4_integrity_rejects_catalog_hash_mismatch(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    index_path = pack_root / "artifact-index.v4.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    for artifact in index["artifacts"]:
        if artifact["path"] == "runtime/conversation.py":
            artifact["digest"] = "sha256:" + "0" * 64
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    errors = _v4_errors(pack_root)

    assert any("digest mismatch" in error or "hash mismatch" in error for error in errors)


def test_v4_integrity_rejects_unlisted_runtime_artifact(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    (pack_root / "runtime" / "extra.py").write_text("# extra\n", encoding="utf-8")

    errors = _v4_errors(pack_root)

    assert any("unlisted runtime artifact" in error for error in errors)


def test_v4_integrity_rejects_unlisted_bundle_artifact(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    (pack_root / "v4" / "extra.json").write_text("{}\n", encoding="utf-8")

    errors = _v4_errors(pack_root)

    assert any("extra artifact" in error for error in errors)


def test_v4_integrity_source_only_companion_allowlist_is_exact(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    (pack_root / "v4" / "defaults.profile.intent.v2.json").write_text(
        "{}\n", encoding="utf-8"
    )

    errors = _v4_errors(pack_root)

    assert any("defaults.profile.intent.v2.json" in error for error in errors)


def test_v4_integrity_rejects_path_traversal(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    index_path = pack_root / "artifact-index.v4.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    for artifact in index["artifacts"]:
        if artifact["path"] == "runtime/conversation.py":
            artifact["path"] = "../outside.py"
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    errors = _v4_errors(pack_root)

    assert any("unsafe relative path" in error or "path" in error for error in errors)


def test_v4_integrity_rejects_symlinked_runtime_artifact(tmp_path):
    pack_root = _copy_v4_pack(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("# outside\n", encoding="utf-8")
    runtime = pack_root / "runtime" / "conversation.py"
    runtime.unlink()
    runtime.symlink_to(outside)

    errors = _v4_errors(pack_root)

    assert any("symlink" in error for error in errors)
