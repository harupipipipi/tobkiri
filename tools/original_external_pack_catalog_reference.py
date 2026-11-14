"""CI-only pinned original bundled and authenticated external catalog observations.

Every admitted owner is built by the original signed echo publisher and admission
transaction. Independent owner scopes are never copied: CAS records bind to their
physical owner. No private key bytes, source fixtures, or catalog bytes are emitted
in the bounded summary. Native CI consumes the separately retained owner trees.
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

from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_setup_review_reference import check_original

FORMAT = "io.tobkiri.original-external-pack-catalog-reference.v1"
CASES = ("absent", "admitted", "malformed-hmac")
LIMIT = 2 * 1024 * 1024


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
                 "PACK_ID": "conformance.minimal.echo",
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
        pack_id = "conformance.minimal.echo"
        external._verify_entry_root(pack_id, entry, external._entry_root(entry, owner))
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
            "artifact_digest": entry["artifact_digest"]}


def worker(original, owner, name, output):
    check_original(original)
    runtime = original / "tobkiri_runtime"
    sys.path.insert(0, str(runtime))
    os.environ["TOBKIRI_USER_DATA"] = str(owner)
    os.environ["RUMI_USER_DATA"] = str(owner)
    from core_runtime import external_pack_catalog_v4 as external
    from core_runtime.pack_boundary import load_pack_catalog

    owner.mkdir(mode=0o700, parents=True, exist_ok=False)
    if name != "absent":
        source_dir = owner.parent / (owner.name + "-publisher-temporary")
        source_dir.mkdir(mode=0o700)
        try:
            source, trust = publisher(runtime)(source_dir)
            admitted = external.admit_signed_external_pack(source, trust_store_path=trust)
            if admitted["pack_id"] != "conformance.minimal.echo":
                raise AssertionError("actual original signed echo admission failed")
            for path in (source, *source.rglob("*")):
                if path.is_dir():
                    path.chmod(0o700)
        finally:
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
                        "file_sequence": sequence, "inventory_before": before,
                        "inventory_after": after})


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
                or reference["artifact_probe"] != native["artifact_probe"]):
            raise AssertionError("independently admitted owner observations disagree")
        cases.append({"name": name, "runtime_dir": str(original / "tobkiri_runtime"),
                      "native_user_data": str(directory / "native-owner"), **native})
    check_original(original)
    summary = {"format": FORMAT, "original_head": ORIGINAL_HEAD, "case_count": len(cases),
               "cases": cases, "native_comparison": False, "qualification": False,
               "production_enabled": False,
               "scope": "original bundled catalog and authenticated external catalog reader; "
                        "independent real signed echo admissions and original CAS verification; no activation"}
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
