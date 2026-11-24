"""Independent original-filesystem/captured-input reference for the stack probe.

This prepares inert test DATA on Linux CI. It never runs the candidate logic or
claims Wasm, native macOS ownership, approval or complete Setup compatibility.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
SIZE = 2 * 65536 + 17
SOURCE_NAMES = (
    "__init__.py", "canonical.py", "errors.py", "ids.py",
    "captured_inputs.py", "platform_artifact.py",
)


def fixture_bytes() -> bytes:
    """Use identical deterministic ELF DATA in the independent macOS consumer."""
    data = bytearray(index % 251 for index in range(SIZE))
    data[:6] = b"\x7fELF\x02\x01"
    data[18:20] = b"\x3e\x00"
    return bytes(data)


def prepare() -> dict:
    """Compare the actual original algorithms on filesystem and closed DATA."""
    sys.path.insert(0, str(ROOT / "tobkiri_runtime"))
    from tobkiri_protocol.captured_inputs import CapturedFile, CapturedInputs
    from tobkiri_protocol.errors import ProtocolError
    from tobkiri_protocol.platform_artifact import artifact_digest, verify_platform_artifact

    raw = fixture_bytes()
    sources = {
        name: hashlib.sha256(
            (ROOT / "tobkiri_runtime/tobkiri_protocol" / name).read_bytes()
        ).hexdigest()
        for name in SOURCE_NAMES
    }
    scenarios = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve(strict=True)
        (root / "artifact").write_bytes(raw)
        variant = {
            "relative_path": "artifact", "entrypoint": "artifact",
            "artifact_digest": artifact_digest(root / "artifact"),
            "entrypoint_digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "platform": "linux", "architecture": "x86_64",
            "bundle_identity": "io.tobkiri.input-stack-fixture",
        }
        for name in ("success", "wrong-digest"):
            selected = dict(variant)
            if name == "wrong-digest":
                selected["artifact_digest"] = "sha256:" + hashlib.sha256(
                    b"deliberately different original artifact digest"
                ).hexdigest()

            def outcome(view):
                try:
                    result = verify_platform_artifact(view, selected)
                except ProtocolError as error:
                    return {
                        "outcome": "protocol-error",
                        "error_type": type(error).__module__ + "." + type(error).__qualname__,
                        "error_args": list(error.args),
                    }
                return {"outcome": "ok", "relative_path": result.relative_to(view).as_posix()}

            native = outcome(root)
            calls = []

            def read(ordinal, offset, length):
                if ordinal != 0 or offset < 0 or offset + length > len(raw):
                    raise ValueError("reference range escaped its finite fixture")
                result = raw[offset:offset + length]
                calls.append({"ordinal": ordinal, "offset": offset,
                              "length": length, "returned": len(result)})
                return result

            capture = CapturedInputs([CapturedFile("artifact", len(raw))], read)
            captured = outcome(capture.root())
            if captured != native:
                raise ValueError("original native and captured artifact behavior differs")
            scenarios.append({
                "name": name,
                "init": json.dumps({"files": [{"path": "artifact", "size": len(raw)}],
                                    "variant": selected}, separators=(",", ":")),
                "expected": native, "expected_calls": calls,
            })
    if sources != {
        name: hashlib.sha256(
            (ROOT / "tobkiri_runtime/tobkiri_protocol" / name).read_bytes()
        ).hexdigest()
        for name in SOURCE_NAMES
    }:
        raise ValueError("original source changed while deriving the reference")
    return {
        "format": "tobkiri.native-input-stack-oracle/1",
        "input_bytes": len(raw), "input_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "original_sources": sources, "scenarios": scenarios,
        "native_original_and_captured_match": True,
        "candidate_logic_used": False, "wasm_execution_performed": False,
        "actual_os_owner_proof": False, "whole_setup_compatible": False,
        "production_enabled": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if (sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true"
            or sys.version_info[:3] != (3, 13, 16)):
        raise SystemExit("original CPython 3.13.16 Linux CI reference required")
    result = prepare()
    descriptor = os.open(arguments.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
