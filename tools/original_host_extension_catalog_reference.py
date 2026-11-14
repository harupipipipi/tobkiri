"""CI-only observations of a genuinely admitted original Host Extension update.

Each owner independently signs and admits a successor to the original bundled
adapter Pack. This produces evidence, not a native verifier or authority grant.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

from original_authority_reference import ORIGINAL_HEAD, write_json
from original_external_pack_catalog_reference import inventory
from original_setup_review_reference import check_original

FORMAT = "io.tobkiri.original-host-extension-catalog-reference.v1"
PACK_ID = "rumi_provider_adapters_pack"
LIMIT = 2 * 1024 * 1024


def fixture_publisher(runtime: Path):
    """Extract the pinned tests' actual successor and integrity helpers."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from core_runtime.pack_artifact_integrity import write_host_install_record
    from core_runtime.pack_signature import SIGNED_MANIFEST_RELATIVE, build_signed_manifest, sign_manifest
    from tobkiri_protocol.canonical import canonical_digest, canonical_json
    from tobkiri_protocol.validation import validate_file

    sources = (
        (runtime / "tests/test_host_extension_update_v4.py", ("_read", "_write", "_digest", "_refresh")),
        (runtime / "tests/test_bundled_host_baseline_v4.py", ("_signed_successor",)),
    )
    nodes = []
    for path, names in sources:
        tree = ast.parse(path.read_bytes(), filename=str(path))
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        if len(selected) != len(names) or {node.name for node in selected} != set(names):
            raise ValueError("pinned original Host fixture helper inventory differs")
        nodes.extend(selected)
    namespace = {
        "__builtins__": __builtins__, "Path": Path, "json": json, "hashlib": hashlib,
        "serialization": serialization, "Ed25519PrivateKey": Ed25519PrivateKey,
        "write_host_install_record": write_host_install_record, "build_signed_manifest": build_signed_manifest,
        "sign_manifest": sign_manifest, "SIGNED_MANIFEST_RELATIVE": SIGNED_MANIFEST_RELATIVE,
        "canonical_digest": canonical_digest, "canonical_json": canonical_json,
        "validate_file": validate_file, "PACK_ID": PACK_ID,
    }
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<pinned-host-fixture>", "exec"), namespace)
    return namespace["_signed_successor"]


def worker(original: Path, owner: Path, output: Path):
    check_original(original)
    runtime = original / "tobkiri_runtime"
    sys.path.insert(0, str(runtime))
    os.environ["TOBKIRI_USER_DATA"] = str(owner)
    os.environ["RUMI_USER_DATA"] = str(owner)
    from core_runtime import external_pack_catalog_v4 as external
    from tobkiri_host.artifact_compiler import compile_pack_root
    from tobkiri_protocol.canonical import canonical_digest

    owner.mkdir(mode=0o700, parents=True, exist_ok=False)
    temporary = owner.parent / (owner.name + "-publisher-temporary")
    temporary.mkdir(mode=0o700)
    try:
        baseline = external.resolve_pack_root(PACK_ID)
        baseline_digest = compile_pack_root(baseline).artifact.digest
        source, trust = fixture_publisher(runtime)(SimpleNamespace(source=baseline), temporary)
        admitted = external.admit_signed_pack_version(
            source, trust_store_path=trust, expected_predecessor_digest=baseline_digest,
            expected_catalog_revision=external.control_catalog_revision(),
        )
    finally:
        shutil.rmtree(temporary)
    if (admitted["pack_id"] != PACK_ID or admitted["version_string"] != "99.0.0"
            or admitted["artifact_digest"] == baseline_digest
            or admitted["catalog_record"]["authority"] != "host-signed-external-host-extension-v4"):
        raise AssertionError("original Host Extension admission differs")

    before = inventory(owner)
    events = []
    project, verify = external._project_catalog_record, external._verify_entry_root
    def observed_project(root, signed_manifest, **kwargs):
        events.append("project-start")
        result = project(root, signed_manifest, **kwargs)
        if canonical_digest(result) != canonical_digest(admitted["catalog_record"]):
            raise AssertionError("actual projection differs from committed record")
        events.append("project-matches")
        return result
    def observed_verify(pack_id, entry, root, **kwargs):
        events.append("verify-start")
        result = verify(pack_id, entry, root, **kwargs)
        events.append("verify-success")
        return result
    try:
        external._project_catalog_record, external._verify_entry_root = observed_project, observed_verify
        snapshot = external.load_external_pack_catalog()
    finally:
        external._project_catalog_record, external._verify_entry_root = project, verify
    versions = snapshot.versions.get(PACK_ID, {})
    if versions.get(admitted["artifact_digest"]) != admitted:
        raise AssertionError("actual read omitted admitted Host Extension")
    if events != ["verify-start", "project-start", "project-matches", "verify-success"]:
        raise AssertionError("original Host Extension read sequence differs")
    after = inventory(owner)
    if before != after:
        raise AssertionError("original catalog read changed owner inventory")
    # Key IDs, signatures, HMACs and content digests bind to independent owners.
    record = dict(admitted["catalog_record"])
    record["source_provenance"] = {k: v for k, v in record["source_provenance"].items() if k != "key_id"}
    write_json(output, {
        "pack_id": PACK_ID, "baseline_digest": baseline_digest,
        "artifact_digest": admitted["artifact_digest"], "version": admitted["version_string"],
        "stable_record_digest": canonical_digest(record), "events": events,
        "retained_versions": len(versions), "selected": PACK_ID in snapshot.entries,
        "inventory_before": before, "inventory_after": after,
    })


def prepare(original: Path, destination: Path):
    check_original(original)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    results = []
    for role in ("reference", "native"):
        output = destination / (role + "-observation.json")
        subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "worker", "--original-root", str(original),
             "--owner", str(destination / (role + "-owner")), "--output", str(output)],
            check=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=180,
        )
        results.append(json.loads(output.read_bytes()))
        output.unlink()
    if results[0] != results[1]:
        raise AssertionError("independent Host Extension observations differ")
    check_original(original)
    summary = {"format": FORMAT, "original_head": ORIGINAL_HEAD, "observation": results[1],
               "native_user_data": str(destination / "native-owner"), "native_comparison": False,
               "qualification": False, "production_enabled": False,
               "scope": "original signed Host Extension admission and catalog compatibility reprojection; no activation"}
    if len(json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode()) > LIMIT:
        raise ValueError("Host Extension summary exceeds bound")
    write_json(destination / "summary.json", summary, compact=True)
    print(json.dumps({"format": FORMAT, "original_head": ORIGINAL_HEAD}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "worker"))
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner", type=Path)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    if args.command == "prepare":
        prepare(original, args.output.resolve())
    elif args.owner is None:
        parser.error("worker requires --owner")
    else:
        worker(original, args.owner.resolve(), args.output)


if __name__ == "__main__":
    main()
