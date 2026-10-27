"""Materialize an inactive, hash-pinned v2 Pack without executing its component.

Receipt and component-header validation establish byte identity only. A receipt
can be fabricated, and neither it nor this materialization is runtime evidence.
The unchanged v1 materializer, source pins and artifacts remain separate.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from materialize_ordered_tool_bridge import (
    MAX_NATIVE_MEMBER,
    canonical,
    digest,
    no_links,
    require_hash,
)
from replay_ordered_tool_bridge_v2 import validate_evidence
from stage_ordered_tool_bridge_v2 import (
    COUNTS,
    INTERFACE,
    PACK,
    PROTOCOL,
    ROOT,
    bounded_read,
    reviewed_inputs,
    strict_json,
)

PACK_ID = "local.ai-tool-bridge-ordered-python"
PACK_VERSION = "2.0.0"
SHAPE = PACK_ID + ".bridge"
SHAPE_VERSION = "2.0"
MAX_OUTER_BYTES = 4 * 1024 * 1024
WIT_MEMBER = "wit/pack-v2.wit"


def envelope_bounds(observations: dict[str, Any]) -> dict[str, int]:
    """Preflight CoreStrictV1 outer strings, never parse ordered inner JSON."""
    requests, responses = [], []
    for case in observations["cases"]:
        if not isinstance(case["operation"], str) or not isinstance(
            case["input_json"], str
        ):
            raise ValueError("outer-envelope operation and payload must be strings")
        requests.append(
            len(
                canonical(
                    {"operation": case["operation"], "payload": case["input_json"]}
                )
            )
        )
        classification = case["classification"]
        if classification == "success":
            if not isinstance(case["output_json"], str):
                raise ValueError("outer-envelope success payload must be a string")
            responses.append(len(canonical(case["output_json"])))
        elif classification not in ("original-invalid", "excluded-not-parity"):
            raise ValueError("unknown reviewed case classification")
    if any(size > MAX_OUTER_BYTES for size in requests + responses):
        raise ValueError("frozen case exceeds native outer-envelope budget")
    return {
        "core_strict_v1_max_bytes": MAX_OUTER_BYTES,
        "max_request_bytes": max(requests, default=0),
        "max_success_response_bytes": max(responses, default=0),
    }


def _receipt_bound(
    manifest: list[dict[str, Any]], destination: str, data: bytes
) -> None:
    matches = [item for item in manifest if item["destination"] == destination]
    if (
        len(matches) != 1
        or matches[0]["sha256"] != digest(data)
        or type(matches[0]["bytes"]) is not int
        or matches[0]["bytes"] != len(data)
    ):
        raise ValueError(f"{destination} changed after source-plan verification")


def _manifest(files: dict[str, bytes]) -> dict[str, Any]:
    return {
        "format": "tobkiri.pack/1",
        "id": PACK_ID,
        "version": PACK_VERSION,
        "publisher_key": None,
        "runtime": {
            "kind": "wasm",
            "component": "component.wasm",
            "toolchain": "python/build-receipt-pinned",
            "config": {"path": "config.json"},
        },
        "shapes": [
            {
                "name": SHAPE,
                "version": SHAPE_VERSION,
                "kind": "port",
                "wit": WIT_MEMBER,
            }
        ],
        "ports": {
            "provides": [{"shape": SHAPE, "version": SHAPE_VERSION, "optional": False}],
            "consumes": [],
            "untyped": [],
        },
        "inputs": [],
        "flows": [],
        "overlays": [],
        "stores": [],
        "ui": None,
        "permissions": {
            "audit_append": False,
            "audit_read": False,
            "cross_profile_storage": False,
            "fs": [],
            "host_io": [],
            "loops": {
                "max_cost_units": 100000,
                "max_iterations": 16,
                "max_wall_ms": 300000,
            },
            "mediator": False,
            "native": False,
            "network": {"origins": []},
            "ports": [],
            "proc": [],
            "pty": False,
            "secrets": [],
            "storage": {
                "private": False,
                "public_read": False,
                "public_write": False,
            },
            "wasm": {
                "fuel_per_call": 2000000000,
                "memory_mib": 128,
                "output_bytes": MAX_OUTER_BYTES,
                "table_elements": 65536,
                "wall_ms": 300000,
            },
        },
        "files": [
            {"path": name, "size": len(data), "sha256": "sha256:" + digest(data)}
            for name, data in sorted(files.items())
        ],
    }


def _publish_no_replace(source: Path, destination: Path) -> None:
    """Atomically publish on supported Linux/macOS local filesystems."""
    old, new = os.fsencode(source), os.fsencode(destination)
    if b"\0" in old or b"\0" in new:
        raise ValueError("embedded null byte in publication path")
    if sys.platform == "linux":
        symbol = "renameat2"
        signature = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
            ctypes.c_char_p, ctypes.c_uint,
        ]
        # Linux AT_FDCWD and RENAME_NOREPLACE, not RENAME_EXCHANGE.
        arguments = (-100, old, -100, new, 1)
    elif sys.platform == "darwin":
        symbol = "renamex_np"
        signature = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        # Darwin sys/stdio.h: RENAME_EXCL, not RENAME_SWAP or RENAME_SECLUDE.
        arguments = (old, new, 0x00000004)
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace publication is unsupported")
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, symbol, None)
    if rename is None:
        raise OSError(errno.ENOTSUP, f"atomic no-replace publication requires {symbol}")
    rename.argtypes = signature
    rename.restype = ctypes.c_int
    # Both OS APIs fail if the destination exists. Unsupported kernels or
    # filesystems fail closed; never fall back to a clobbering rename.
    if rename(*arguments) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def materialize(
    artifact: Path,
    oracle: Path,
    destination: Path,
    *,
    artifact_sha256: str,
    receipt_sha256: str,
) -> dict[str, Any]:
    """Publish a fresh inactive candidate with no build or code execution.

    The parent must be a trusted, stable local directory without concurrent
    replacement. No Profile is created or enabled. Failed private partials are
    cleaned up; publication uses Linux/macOS atomic no-replace rename only after
    all validation succeeds. Unsupported platforms/filesystems fail closed.
    """
    destination = destination.absolute()
    receipt_path = artifact.with_suffix(".build.json")
    for path in (artifact, receipt_path, oracle, destination):
        no_links(path.absolute())
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("destination must be fresh with an existing trusted parent")
    binary, receipt, observations, document = validate_evidence(
        artifact, oracle, artifact_limit=MAX_NATIVE_MEMBER
    )
    if len(binary) > MAX_NATIVE_MEMBER:
        raise ValueError("component exceeds native Pack member limit")
    require_hash(binary, artifact_sha256, "artifact")
    require_hash(receipt, receipt_sha256, "receipt")
    if binary[:8] != b"\0asm\x0d\0\x01\0":
        raise ValueError("artifact is not a Wasm component")
    bounds = envelope_bounds(document)
    # Re-capture the source-stage binding; bind it and both WIT files to the
    # already reviewed complete receipt, not to mutable paths or report claims.
    current_observations, source_stage_binding = reviewed_inputs()
    if observations != current_observations:
        raise ValueError("source observations changed after receipt verification")
    source_manifest = strict_json(receipt)["source_manifest"]
    _receipt_bound(source_manifest, "reviewed-inputs-v2.json", source_stage_binding)
    files = {
        "component.wasm": binary,
        "component.build.json": receipt,
        "config.json": b"{}",
        "wit/bridge.shape.json": canonical(
            {"cardinality": "one", "streams": {}, "callbacks": {}, "conformance": None}
        ),
    }
    for name, path in (
        (WIT_MEMBER, PACK / WIT_MEMBER),
        ("wit/deps/core.wit", ROOT / "crates/tobkiri-wit/wit/core/core.wit"),
    ):
        no_links(path.absolute())
        data = bounded_read(path, 1024 * 1024)
        _receipt_bound(source_manifest, name, data)
        files[name] = data
    manifest = canonical(_manifest(files))
    with tempfile.TemporaryDirectory(
        prefix=".ordered-pack-v2-", dir=destination.parent
    ) as temporary:
        partial = Path(temporary)
        for name, data in files.items():
            path = partial / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(data)
        with (partial / "pack.json").open("xb") as stream:
            stream.write(manifest)
        no_links(destination)
        if destination.exists():
            raise ValueError("destination appeared before publication")
        _publish_no_replace(partial, destination)
    return {
        "format": "tobkiri.ordered-python-pack-materialization/2",
        "validation_scope": "byte-identity-only-not-execution",
        "pack_manifest_sha256": digest(manifest),
        "artifact_sha256": digest(binary),
        "receipt_sha256": digest(receipt),
        "source_observations_sha256": digest(observations),
        "source_observations_bindings_sha256": document["source_bindings_sha256"],
        "source_stage_bindings_sha256": digest(source_stage_binding),
        "original_oracle_sha256": document["oracle_sha256"],
        "original_frozen_inputs_sha256": document["oracle_freeze_sha256"],
        "protocol": PROTOCOL,
        "interface": INTERFACE,
        "pack_id": PACK_ID,
        "pack_version": PACK_VERSION,
        "shape": SHAPE,
        "shape_version": SHAPE_VERSION,
        "case_count": len(document["cases"]),
        "frozen_case_counts": dict(COUNTS),
        "outer_budget": bounds,
        "build_performed": False,
        "wasm_execution_performed": False,
        "native_core_acceptance": False,
        "core_runtime_acceptance": False,
        "full_host_replay_performed": False,
        "production_enabled": False,
    }


def main() -> None:
    """Materialize explicitly pinned bytes, never activate or replay a Pack."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument(
        "--oracle", type=Path, default=PACK / "integer-v2.observations.json"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--receipt-sha256", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            materialize(
                args.artifact,
                args.oracle,
                args.output,
                artifact_sha256=args.artifact_sha256,
                receipt_sha256=args.receipt_sha256,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
