"""Verify the frozen five-component checkpoint against its exact source tree.

The current producer implementation still captures every frozen plan member.
Historical source stagers run in a fresh process to keep their module globals
separate from the current native Harness stagers. No component is built or run;
this never makes old artifact receipts evidence for the current application.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE_COMMIT = "c6d7001249c661552416672448bda123c59b33ca"
INVENTORY = (
    "tools/python313-producer/trials/2026-10-20-finalization/source/"
    "five-component-capture.json"
)
INVENTORY_SHA256 = "969853ab973238598718b847d01ea3bf7d0b08a2d11a420d8aa416e74d15d8da"


def capture() -> dict:
    """Check all82 recorded source identities, then capture both original plans."""
    raw = (ROOT / INVENTORY).read_bytes()
    if len(raw) != 14927 or hashlib.sha256(raw).hexdigest() != INVENTORY_SHA256:
        raise ValueError("original component source inventory changed")
    rows = json.loads(raw)
    if len(rows) != 82:
        raise ValueError("original component source inventory differs")
    with tempfile.TemporaryDirectory(prefix="original-component-source-") as tmp:
        source = Path(tmp).resolve()
        seen = set()
        for row in rows:
            name = row["path"]
            path = PurePosixPath(name)
            if (name in seen or path.is_absolute() or path.as_posix() != name
                    or any(part in (".", "..") for part in path.parts)
                    or any(char in name for char in "\\\0:\r\n")):
                raise ValueError("invalid original source path")
            seen.add(name)
            data = subprocess.check_output(
                ["git", "show", f"{SOURCE_COMMIT}:{name}"], cwd=ROOT,
            )
            if (len(data) != row["bytes"] or len(data) > 8 * 1024 * 1024
                    or hashlib.sha256(data).hexdigest() != row["sha256"]):
                raise ValueError(f"original source bytes differ: {name}")
            target = source.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        result = subprocess.check_output(
            [sys.executable, "-B", str(Path(__file__).resolve()),
             "--captured-source", str(source)], cwd=ROOT,
        )
    report = json.loads(result)
    if (report.get("source_commit") != SOURCE_COMMIT
            or report.get("source_files") != 82
            or report.get("captured_members") != {"bridge-v2": 12, "caller": 10}):
        raise ValueError("original component capture identity differs")
    return report


def _capture_in_fresh_process(source: Path) -> dict:
    # Imported from the current producer, with all historical stagers still
    # absent from sys.modules. Their actual __file__/ROOT point into source.
    import component_inputs

    counts = {}
    for name in ("bridge-v2", "caller"):
        raw, captures, receipt = component_inputs.capture(source, name)
        if hashlib.sha256(raw).hexdigest() != receipt["plan_sha256"]:
            raise ValueError("original plan hash differs")
        counts[name] = len(captures)
    return {
        "source_commit": SOURCE_COMMIT, "source_files": 82,
        "captured_members": counts, "current_source_qualification": False,
        "build_performed": False, "wasm_execution_performed": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captured-source", type=Path)
    options = parser.parse_args()
    print(json.dumps(
        capture() if options.captured_source is None
        else _capture_in_fresh_process(options.captured_source.resolve(strict=True)),
        sort_keys=True,
    ))
