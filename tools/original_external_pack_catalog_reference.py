"""CI-only pinned original bundled and authenticated external catalog observations.

Every admitted owner is built by the original signed echo publisher and admission
transaction. Independent owner scopes are never copied: CAS records bind to their
physical owner. No private key bytes, source fixtures, or catalog bytes are emitted
in the bounded summary. Native CI consumes the separately retained owner trees.
"""
from __future__ import annotations

import argparse
import ast
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType
from typing import Any

from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_setup_review_reference import check_original

FORMAT = "io.tobkiri.original-external-pack-catalog-reference.v1"
CASES = ("absent", "admitted", "malformed-hmac", "retained-version")
LIMIT = 2 * 1024 * 1024
PACK_ID = "conformance.minimal.echo"


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def inventory(root):
    """Bounded metadata only, including no hash of secret key bytes."""
    if not root.exists():
        return []
    result = []
    for path in sorted(root.rglob("*")):
        if len(result) >= 256:
            raise ValueError("owner inventory exceeds bound")
        stat = path.lstat()
        if path.is_symlink():
            raise ValueError("unexpected symlink in fresh owner")
        result.append({"path": path.relative_to(root).as_posix(),
                       "kind": "directory" if path.is_dir() else "file",
                       "mode": stat.st_mode & 0o777,
                       **({"bytes": stat.st_size} if path.is_file() else {})})
    return result


def publisher(runtime):
    """Compile only the pinned test fixture helpers, without importing pytest."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from core_runtime.pack_artifact_integrity import write_host_install_record
    from core_runtime.pack_signature import build_signed_manifest, sign_manifest
    from tobkiri_protocol.canonical import canonical_digest

    source = runtime / "tests/test_external_pack_catalog_v4.py"
    tree = ast.parse(source.read_bytes(), filename=str(source))
    names = ("_write_json", "_file_digest", "_refresh_fixture_artifacts", "_signed_external_pack")
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in names]
    if len(nodes) != len(names) or {n.name for n in nodes} != set(names):
        raise ValueError("pinned original publisher fixture helper inventory differs")
    namespace = {"__builtins__": __builtins__, "Path": Path, "json": json,
                 "hashlib": hashlib, "shutil": shutil, "serialization": serialization,
                 "Ed25519PrivateKey": Ed25519PrivateKey,
                 "write_host_install_record": write_host_install_record,
                 "build_signed_manifest": build_signed_manifest, "sign_manifest": sign_manifest,
                 "canonical_digest": canonical_digest,
                 "FIXTURE": runtime / "tests/fixtures/conformance_minimal_echo_pack",
                 "PACK_ID": PACK_ID,
                 "CONTRACT_ID": "io.tobkiri.conformance.echo.v1"}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                              *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["_signed_external_pack"]


def observe_artifact(external, owner, entry):
    # Observe the ordinary compiler's actual read/validate order. Delegation
    # retains the pinned compiler, arguments, and complete verification tail.
    from tobkiri_host import artifact_compiler

    observed_reads = []
    original_validate = artifact_compiler.validate_file

    def validate(path, schema):
        observed_reads.append([path.name, schema])
        return original_validate(path, schema)

    artifact_compiler.validate_file = validate
    try:
        external._verify_entry_root(PACK_ID, entry, external._entry_root(entry, owner))
    finally:
        artifact_compiler.validate_file = original_validate
    if observed_reads != [
        ["pack.v4.json", "pack"],
        ["contracts.v4.json", "pack_contract_catalog"],
        ["artifact-index.v4.json", "pack_artifact_index"],
        ["executables.v4.json", "executable_catalog"],
    ]:
        raise AssertionError("original direct compiler read sequence changed")
    return {"outcome": "verified", "direct_reads": observed_reads,
            "artifact_digest": entry["artifact_digest"], "content_digest": entry["content_digest"]}


def _admit_retained_revision(
    external: ModuleType,
    runtime: Path,
    directory: Path,
) -> dict[str, Mapping[str, Any]]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from core_runtime.pack_version_selection import version_catalog_key

    publish = publisher(runtime)
    signing_key = Ed25519PrivateKey.generate()
    baseline, successor = directory / "baseline", directory / "successor"
    baseline.mkdir(mode=0o700)
    successor.mkdir(mode=0o700)
    source, trust = publish(baseline, signing_key=signing_key)
    first = external.admit_signed_external_pack(source, trust_store_path=trust)
    source, trust = publish(
        successor,
        signing_key=signing_key,
        version="1.1.0",
        runtime_suffix="\n# signed revision two\n",
    )
    second = external.admit_signed_pack_version(
        source,
        trust_store_path=trust,
        expected_predecessor_digest=first["artifact_digest"],
        expected_catalog_revision=external.control_catalog_revision(),
    )
    if (first["pack_id"] != PACK_ID or second["pack_id"] != PACK_ID
            or first["key_id"] != second["key_id"]
            or first["version_string"] != "1.0.0"
            or second["version_string"] != "1.1.0"
            or first["artifact_digest"] == second["artifact_digest"]):
        raise AssertionError("same-key original signed revision admission differs")
    return {
        PACK_ID: first,
        version_catalog_key(PACK_ID, second["artifact_digest"]): second,
    }


def _observe_retained_catalog(
    external: ModuleType,
    admitted: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    from core_runtime import pack_version_selection

    resolve_key = pack_version_selection.catalog_key_identity
    verify_root = external._verify_entry_root
    ordered: list[dict[str, str]] = []
    pending_key: str | None = None

    def observe_key(key: str, entry: Mapping[str, Any]) -> str:
        nonlocal pending_key
        if pending_key is not None:
            raise AssertionError("original identified another key before CAS verify")
        pack_id = resolve_key(key, entry)
        pending_key = key
        return pack_id

    def observe_root(pack_id: str, entry: Mapping[str, Any], root: Path) -> None:
        nonlocal pending_key
        verify_root(pack_id, entry, root)
        if pending_key is None or entry != admitted.get(pending_key):
            raise AssertionError("original verified a different CAS entry than identified")
        ordered.append({
            "key": pending_key,
            "artifact_digest": entry["artifact_digest"],
            "content_digest": entry["content_digest"],
        })
        pending_key = None

    try:
        pack_version_selection.catalog_key_identity = observe_key
        external._verify_entry_root = observe_root
        snapshot = external._load_external_pack_catalog()
    finally:
        pack_version_selection.catalog_key_identity = resolve_key
        external._verify_entry_root = verify_root
    visited_keys = [visit["key"] for visit in ordered]
    if pending_key is not None or visited_keys != sorted(admitted):
        raise AssertionError("original did not verify both revisions in key order")
    baseline = admitted[PACK_ID]
    retained = {entry["artifact_digest"]: entry for entry in admitted.values()}
    if (snapshot.records != {PACK_ID: baseline["catalog_record"]}
            or snapshot.entries != {PACK_ID: baseline}
            or snapshot.versions != {PACK_ID: retained}):
        raise AssertionError("original retained catalog projection differs")
    return {
        "ordered": ordered,
        "selected": {
            pack_id: record["artifact_digest"]
            for pack_id, record in snapshot.records.items()
        },
        "revision": snapshot.revision,
    }


def worker(original, owner, name, output):
    check_original(original)
    runtime = original / "tobkiri_runtime"
    sys.path.insert(0, str(runtime))
    os.environ["TOBKIRI_USER_DATA"] = str(owner)
    os.environ["RUMI_USER_DATA"] = str(owner)
    from core_runtime import external_pack_catalog_v4 as external
    from core_runtime.pack_boundary import load_pack_catalog

    owner.mkdir(mode=0o700, parents=True, exist_ok=False)
    retained = None
    if name != "absent":
        source_dir = owner.parent / (owner.name + "-publisher-temporary")
        source_dir.mkdir(mode=0o700)
        try:
            if name == "retained-version":
                retained = _admit_retained_revision(external, runtime, source_dir)
            else:
                source, trust = publisher(runtime)(source_dir)
                admitted = external.admit_signed_external_pack(
                    source, trust_store_path=trust,
                )
                if admitted["pack_id"] != PACK_ID:
                    raise AssertionError("actual original signed echo admission failed")
        finally:
            for path in source_dir.rglob("*"):
                if path.is_dir():
                    path.chmod(0o700)
            shutil.rmtree(source_dir)
        if name == "malformed-hmac":
            path = external._catalog_path(owner)
            payload = json.loads(path.read_bytes())
            old = payload["signature"]
            payload["signature"] = ("0" if old[0] != "0" else "1") + old[1:]
            path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    before = inventory(owner)
    bundled = load_pack_catalog()
    bundled_observation = {"ids": sorted(bundled),
                           "record_sha256": {key: digest(value) for key, value in sorted(bundled.items())}}
    sequence = ["bundled-catalog"]
    # These pass-through wrappers observe call order only; original implementations
    # and arguments remain intact, including schema and HMAC verification.
    original_read, original_validate, original_key = (external._read_json_nofollow,
                                                      external.validate_document,
                                                      external.load_signing_key)
    def read(*args, **kwargs):
        sequence.append("external-catalog-file")
        return original_read(*args, **kwargs)
    def validate(*args, **kwargs):
        sequence.append("external-catalog-schema")
        return original_validate(*args, **kwargs)
    def key(*args, **kwargs):
        sequence.append("external-catalog-key")
        return original_key(*args, **kwargs)
    try:
        external._read_json_nofollow, external.validate_document, external.load_signing_key = read, validate, key
        try:
            value = external._read_authenticated_catalog(external._catalog_path(owner), allow_missing=True)
            expected = {"outcome": "success", "value": value}
        except Exception as error:
            expected = {"outcome": "error", "error": error_graph(error, owner)}
    finally:
        external._read_json_nofollow, external.validate_document, external.load_signing_key = original_read, original_validate, original_key
    artifact_probe = None
    if name == "admitted" and expected["outcome"] == "success":
        entry = next(iter(expected["value"]["entries"].values()))
        artifact_probe = observe_artifact(external, owner, entry)
    retained_probe = None
    if retained is not None:
        if (expected["outcome"] != "success"
                or expected["value"]["entries"] != retained):
            raise AssertionError("authenticated catalog omitted a retained admission")
        retained_probe = _observe_retained_catalog(external, retained)
    after = inventory(owner)
    if before != after:
        raise AssertionError("catalog read changed owner inventory")
    if name == "absent" and (expected.get("outcome") != "success" or expected["value"]["entries"]):
        raise AssertionError("absent catalog is not empty")
    if name == "admitted" and (expected.get("outcome") != "success" or len(expected["value"]["entries"]) != 1):
        raise AssertionError("admission not visible in authenticated catalog")
    if name == "malformed-hmac" and (expected.get("outcome") != "error" or expected["error"]["message"] != "external Normal Pack catalog authentication failed"):
        raise AssertionError("same-owner malformed HMAC did not reach authentication")
    write_json(output, {"bundled": bundled_observation, "expected": expected,
                        "artifact_probe": artifact_probe,
                        **({"retained_probe": retained_probe} if retained else {}),
                        "file_sequence": sequence, "inventory_before": before,
                        "inventory_after": after})


def _stable_retained_probe(
    probe: Mapping[str, Any],
) -> tuple[list[tuple[str, str]], Mapping[str, str]]:
    # Publisher keys, content digests and catalog revisions bind to one owner.
    return ([(row["key"], row["artifact_digest"]) for row in probe["ordered"]],
            probe["selected"])


def prepare(original, destination):
    check_original(original)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    cases = []
    for name in CASES:
        directory = destination / name
        directory.mkdir(mode=0o700)
        results = {}
        for role in ("reference", "native"):
            owner, output = directory / (role + "-owner"), directory / (role + "-observation.json")
            subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "worker",
                            "--original-root", str(original), "--owner", str(owner),
                            "--case", name, "--output", str(output)], check=True,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), timeout=180)
            results[role] = json.loads(output.read_bytes())
            output.unlink()
        reference, native = results["reference"], results["native"]
        if (reference["bundled"] != native["bundled"] or reference["file_sequence"] != native["file_sequence"]
                or reference["expected"]["outcome"] != native["expected"]["outcome"]
                or (reference["artifact_probe"] or {}).get("direct_reads")
                != (native["artifact_probe"] or {}).get("direct_reads")
                or (reference["artifact_probe"] or {}).get("artifact_digest")
                != (native["artifact_probe"] or {}).get("artifact_digest")):
            raise AssertionError("independently admitted owner observations disagree")
        if (name == "retained-version"
                and _stable_retained_probe(reference["retained_probe"])
                != _stable_retained_probe(native["retained_probe"])):
            raise AssertionError("independent retained catalog visits disagree")
        cases.append({"name": name, "runtime_dir": str(original / "tobkiri_runtime"),
                      "native_user_data": str(directory / "native-owner"), **native})
    check_original(original)
    summary = {"format": FORMAT, "original_head": ORIGINAL_HEAD, "case_count": len(cases),
               "cases": cases, "native_comparison": False, "qualification": False,
               "production_enabled": False,
               "scope": "original bundled catalog and authenticated external catalog reader; "
                        "independent signed echo admissions and ordered retained CAS "
                        "verification; no activation"}
    raw = json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > LIMIT:
        raise ValueError("original catalog summary exceeds bound")
    write_json(destination / "summary.json", summary, compact=True)
    print(json.dumps({"format": FORMAT, "case_count": len(cases), "original_head": ORIGINAL_HEAD}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "worker"))
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner", type=Path)
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args()
    original = args.original_root.resolve(strict=True)
    if args.command == "prepare":
        prepare(original, args.output.resolve())
    elif args.owner is None or args.case is None:
        parser.error("worker requires --owner and --case")
    else:
        worker(original, args.owner.resolve(), args.case, args.output)


if __name__ == "__main__":
    main()
