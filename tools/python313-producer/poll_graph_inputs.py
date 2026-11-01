"""Authenticate all final-component/trace bytes before read-only CI analysis."""

from __future__ import annotations

from pathlib import Path
import re
import stat

from build_native import fingerprint
from poll_comparison_inputs import physical, read_json

HERE = Path(__file__).resolve().parent
PINS = HERE / "poll-graph-inputs.json"
REQUIRED = {
    "Rehash every decoded CLI helper receipt and pair before execution",
    "Call the same raw C cases with genuine WASIp2 in two private Stores",
    "Keep actual components and complete module observations on CI only",
}


def validate(pins: dict) -> None:
    """Require all 31 committed file identities, including every tiny library."""
    names = {"comparison.json"}
    for variant in ("original", "candidate"):
        names.add(f"{variant}/work/stage/component.wasm")
        names.update(
            f"{variant}/work/link-trace/{name}"
            for name in (
                *(f"{i:04}.wasm" for i in range(11)),
                "libraries.tsv",
                "complete.tsv",
                "preinit-linked-component.wasm",
            )
        )
    if (
        pins.get("format") != "tobkiri.ui-poll-graph-inputs/1"
        or pins.get("repository") != "harupipipipi/tobkiri"
        or pins.get("workflow") != ".github/workflows/issue-1520-ui-poll-reference.yml"
        or pins.get("artifact_name")
        != "ui-empty-poll-reference-linked-modules-inactive"
        or re.fullmatch(r"[0-9a-f]{40}", str(pins.get("head"))) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(pins.get("artifact_sha256"))) is None
        or any(
            type(pins.get(n)) is not int or pins[n] <= 0
            for n in ("run", "job", "artifact", "artifact_bytes")
        )
        or set(pins.get("files", {})) != names
    ):
        raise ValueError("complete committed component graph pins required")
    for value in pins["files"].values():
        if (
            set(value) != {"bytes", "sha256"}
            or type(value["bytes"]) is not int
            or not 0 < value["bytes"] <= 32 * 1024 * 1024
            or re.fullmatch(r"[0-9a-f]{64}", str(value["sha256"])) is None
        ):
            raise ValueError("invalid pinned graph member")
    if pins["files"]["comparison.json"] != {
        "bytes": pins.get("comparison_bytes"),
        "sha256": pins.get("comparison_sha256"),
    }:
        raise ValueError("comparison report pin differs")


def authenticate(pins: dict, root: Path) -> None:
    """Check actual successful run/job and unexpired binary artifact metadata."""
    validate(pins)
    run, job, artifact = (
        read_json(root / name) for name in ("run.json", "job.json", "artifact.json")
    )
    if (
        run.get("id") != pins["run"]
        or run.get("head_sha") != pins["head"]
        or run.get("path") != pins["workflow"]
        or run.get("repository", {}).get("full_name") != pins["repository"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or job.get("id") != pins["job"]
        or job.get("run_id") != pins["run"]
        or job.get("name") != "actual-c-reference"
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or not REQUIRED
        <= {
            s.get("name")
            for s in job.get("steps", [])
            if s.get("status") == "completed" and s.get("conclusion") == "success"
        }
        or artifact.get("id") != pins["artifact"]
        or artifact.get("name") != pins["artifact_name"]
        or artifact.get("expired") is not False
        or artifact.get("size_in_bytes") != pins["artifact_bytes"]
        or artifact.get("digest") != "sha256:" + pins["artifact_sha256"]
        or artifact.get("workflow_run", {}).get("id") != pins["run"]
        or artifact.get("workflow_run", {}).get("head_sha") != pins["head"]
    ):
        raise ValueError("actual graph predecessor authentication differs")


def verify_bundle(root: Path, pins: dict) -> dict:
    """Rehash the entire decoded bundle; never execute any archived member."""
    physical(root)
    validate(pins)
    allowed = {
        parent.as_posix()
        for name in pins["files"]
        for parent in Path(name).parents
        if parent.as_posix() != "."
    }
    found = {}
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if path.resolve() != path:
            raise ValueError("graph input alias is forbidden")
        if stat.S_ISDIR(mode) and name in allowed:
            continue
        if (
            not stat.S_ISREG(mode)
            or path.stat().st_nlink != 1
            or name not in pins["files"]
        ):
            raise ValueError("unexpected graph input member")
        found[name] = fingerprint(path, 32 * 1024 * 1024)
        if found[name] != pins["files"][name]:
            raise ValueError("decoded graph input bytes differ")
    if found != pins["files"]:
        raise ValueError("incomplete graph input bundle")
    return found
