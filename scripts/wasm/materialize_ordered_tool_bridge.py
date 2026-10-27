"""Materialize a hash-pinned ordered Python candidate Pack, never activate it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from replay_ordered_tool_bridge import validate_evidence
from stage_ordered_tool_bridge import PACK, ROOT, bounded_read

PACK_ID = "local.ai-tool-bridge-ordered-python"
SHAPE = PACK_ID + ".bridge"
MAX_NATIVE_MEMBER = 128 * 1024 * 1024


def digest(data: bytes) -> str:
    """Hash captured bytes with SHA-256."""
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    """Serialize finite manifest data deterministically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def no_links(path: Path) -> None:
    """Reject links in every existing input or destination path component."""
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink path is forbidden")


def require_hash(data: bytes, expected: str, label: str) -> None:
    """Require an explicit lowercase digest and exact captured-byte identity."""
    if (len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)
            or digest(data) != expected):
        raise ValueError(f"{label} SHA-256 mismatch")


def envelope_bounds(frozen: dict) -> dict:
    """Preflight native outer JSON size without parsing the ordered inner string."""
    limit = 4 * 1024 * 1024
    requests, responses = [], []
    for case in frozen["cases"]:
        requests.append(len(canonical({"operation": case["operation"],
                                       "payload": case["input_json"]})))
        if case["transport"]["status"] == "ok":
            responses.append(len(canonical(case["transport"]["output_json"])))
    if any(size > limit for size in requests + responses):
        raise ValueError("frozen case exceeds native outer-envelope budget")
    return {"core_strict_v1_max_bytes": limit,
            "max_request_bytes": max(requests, default=0),
            "max_success_response_bytes": max(responses, default=0)}


def materialize(artifact: Path, oracle: Path, destination: Path, *,
                artifact_sha256: str, receipt_sha256: str) -> dict:
    """Publish a fresh candidate from verified bytes, with no code execution.

    The destination parent must be a trusted local directory without concurrent
    replacement. Failed private partial directories are retained for diagnosis.
    No profile is created or enabled. Hash identity is not runtime acceptance.
    """
    receipt_path = artifact.with_suffix(".build.json")
    for path in (artifact, receipt_path, oracle, destination):
        no_links(path.absolute())
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("destination must be fresh with an existing trusted parent")
    binary, receipt, frozen, frozen_document = validate_evidence(
        artifact, oracle, artifact_limit=MAX_NATIVE_MEMBER)
    if len(binary) > MAX_NATIVE_MEMBER:
        raise ValueError("component exceeds native Pack member limit")
    bounds = envelope_bounds(frozen_document)
    require_hash(binary, artifact_sha256, "artifact")
    require_hash(receipt, receipt_sha256, "receipt")
    if binary[:8] != b"\0asm\x0d\0\x01\0":
        raise ValueError("artifact is not a Wasm component")
    for path in (PACK / "wit/pack.wit",
                 ROOT / "crates/tobkiri-wit/wit/core/core.wit"):
        no_links(path)
    files = {
        "component.wasm": binary,
        "component.build.json": receipt,
        "config.json": b"{}",
        "wit/pack.wit": bounded_read(PACK / "wit/pack.wit", 1024 * 1024),
        "wit/deps/core.wit": bounded_read(
            ROOT / "crates/tobkiri-wit/wit/core/core.wit", 1024 * 1024),
        "wit/bridge.shape.json": canonical({
            "cardinality": "one", "streams": {}, "callbacks": {},
            "conformance": None,
        }),
    }
    # These bytes were independently bound by validate_evidence's source plan.
    source_manifest = json.loads(receipt)["source_manifest"]
    for name in ("wit/pack.wit", "wit/deps/core.wit"):
        matches = [item for item in source_manifest if item["destination"] == name]
        if len(matches) != 1 or matches[0]["sha256"] != digest(files[name]):
            raise ValueError("WIT changed after source-plan verification")
    port = {"shape": SHAPE, "version": "1.0", "optional": False}
    manifest = {
        "format": "tobkiri.pack/1", "id": PACK_ID, "version": "0.1.0",
        "publisher_key": None,
        "runtime": {"kind": "wasm", "component": "component.wasm",
                    "toolchain": "python/build-receipt-pinned",
                    "config": {"path": "config.json"}},
        "shapes": [{"name": SHAPE, "version": "1.0", "kind": "port",
                    "wit": "wit/pack.wit"}],
        "ports": {"provides": [port], "consumes": [], "untyped": []},
        "inputs": [], "flows": [], "overlays": [], "stores": [], "ui": None,
        "permissions": {
            "audit_append": False, "audit_read": False,
            "cross_profile_storage": False, "fs": [], "host_io": [],
            "loops": {"max_cost_units": 100000, "max_iterations": 16,
                      "max_wall_ms": 300000},
            "mediator": False, "native": False, "network": {"origins": []},
            "ports": [], "proc": [], "pty": False, "secrets": [],
            "storage": {"private": False, "public_read": False,
                        "public_write": False},
            "wasm": {"fuel_per_call": 2000000000, "memory_mib": 128,
                     "output_bytes": 4194304, "table_elements": 65536,
                     "wall_ms": 300000},
        },
        "files": [{"path": name, "size": len(data),
                   "sha256": "sha256:" + digest(data)}
                  for name, data in sorted(files.items())],
    }
    partial = Path(tempfile.mkdtemp(prefix=".ordered-pack-", dir=destination.parent))
    try:
        for name, data in files.items():
            path = partial / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (partial / "pack.json").write_bytes(canonical(manifest))
        if destination.exists():
            raise ValueError("destination appeared before publication")
        os.rename(partial, destination)
    except BaseException as exc:
        (partial / "FAILED.txt").write_text(type(exc).__name__ + "\n")
        raise
    return {"format": "tobkiri.ordered-python-pack-materialization/1",
            "pack_manifest_sha256": digest(canonical(manifest)),
            "artifact_sha256": digest(binary), "receipt_sha256": digest(receipt),
            "oracle_sha256": digest(frozen), "shape": SHAPE,
            "case_count": len(frozen_document["cases"]), "outer_budget": bounds,
            "production_enabled": False, "core_runtime_acceptance": False}


def main() -> None:
    """Materialize only explicitly pinned existing inputs into a new directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, default=PACK / "oracle.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--receipt-sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(materialize(args.artifact, args.oracle, args.output,
                                artifact_sha256=args.artifact_sha256,
                                receipt_sha256=args.receipt_sha256), indent=2))


if __name__ == "__main__":
    main()
