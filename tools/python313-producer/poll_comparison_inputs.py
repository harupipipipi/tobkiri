"""Authenticate saved paired producer artifacts before the separate C oracle."""

from __future__ import annotations

import json
from pathlib import Path
import re
import stat

from build_native import fingerprint
import ci_link_trace_source
import poll_producer_pair

PREFIXES = {
    "original": "poll-original-producer",
    "candidate": "poll-candidate-producer",
}
HELPER = "ui-poll-c-helper/work/_tobkiri_poll_oracle.abi3.so"
HELPER_RECEIPT = "ui-poll-c-helper/helper-build.json"
MEMBERS = {
    "poll-producer-pair.json",
    HELPER,
    HELPER_RECEIPT,
    *(
        prefix + "/" + name
        for prefix in PREFIXES.values()
        for name in (
            "cli-build.json",
            "cli-source.json",
            "work/target/release/componentize-py",
        )
    ),
}
REQUIRED_STEPS = {
    "Build original native and WASI Python with actual target headers",
    "Build one actual raw C helper for both later components",
    "Build the separate exact-source inactive libc",
    "Rebuild the shared original frozen Rust runtimes",
    "Package both explicit comparison asset sets without replacing the SDK",
    "Build both separately identified CLIs with the same observation feature",
    "Require exactly one asset difference and unchanged source and lock",
    "Keep the real paired producers and single helper on CI only",
}


def physical(path: Path) -> None:
    """Require a physical directory, never an extracted or user-supplied alias."""
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise ValueError("physical comparison input root required")


def read_json(path: Path, maximum: int = 16 * 1024 * 1024) -> dict:
    """Bound metadata before decoding any archived control objects."""
    fingerprint(path, maximum)
    value = json.loads(path.read_bytes())
    if type(value) is not dict:
        raise ValueError("comparison input metadata must be an object")
    return value


def validate_pins(pins: dict) -> None:
    """Accept a complete committed pin set, never inferred or partial hashes."""
    if (
        pins.get("format") != "tobkiri.ui-poll-comparison-inputs/1"
        or pins.get("repository") != "harupipipipi/tobkiri"
        or pins.get("workflow") != ".github/workflows/issue-1520-ui-poll-producers.yml"
        or type(pins.get("head")) is not str
        or re.fullmatch("[0-9a-f]{40}", pins["head"]) is None
        or any(
            type(pins.get(name)) is not int or pins[name] <= 0
            for name in ("run", "job", "artifact", "artifact_bytes")
        )
        or type(pins.get("artifact_sha256")) is not str
        or re.fullmatch("[0-9a-f]{64}", pins["artifact_sha256"]) is None
        or type(pins.get("files")) is not dict
        or set(pins["files"]) != MEMBERS
    ):
        raise ValueError("complete authenticated paired supplier pins required")
    for name, identity in pins["files"].items():
        maximum = (
            128 * 1024 * 1024 if name.endswith("componentize-py") else 16 * 1024 * 1024
        )
        if (
            type(identity) is not dict
            or set(identity) != {"bytes", "sha256"}
            or type(identity["bytes"]) is not int
            or not 0 < identity["bytes"] <= maximum
            or type(identity["sha256"]) is not str
            or re.fullmatch("[0-9a-f]{64}", identity["sha256"]) is None
        ):
            raise ValueError("each decoded supplier member requires its exact pin")


def authenticate(pins: dict, run: dict, job: dict, artifact: dict) -> None:
    """Require actual API run/job/artifact success with the committed identity."""
    validate_pins(pins)
    if (
        run.get("id") != pins["run"]
        or run.get("head_sha") != pins["head"]
        or run.get("path") != pins["workflow"]
        or run.get("repository", {}).get("full_name") != pins["repository"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or job.get("id") != pins["job"]
        or job.get("run_id") != pins["run"]
        or job.get("name") != "paired-producers"
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or not REQUIRED_STEPS
        <= {
            s.get("name")
            for s in job.get("steps", [])
            if s.get("status") == "completed" and s.get("conclusion") == "success"
        }
        or artifact.get("id") != pins["artifact"]
        or artifact.get("name") != "ui-empty-poll-paired-producers-inactive"
        or artifact.get("expired") is not False
        or artifact.get("size_in_bytes") != pins["artifact_bytes"]
        or artifact.get("digest") != "sha256:" + pins["artifact_sha256"]
        or artifact.get("workflow_run", {}).get("id") != pins["run"]
        or artifact.get("workflow_run", {}).get("head_sha") != pins["head"]
    ):
        raise ValueError("actual paired supplier run/job/artifact identity differs")


def verify_bundle(bundle: Path, pins: dict) -> dict:
    """Rehash every decoded member before any binary is made executable."""
    physical(bundle)
    validate_pins(pins)
    seen = set()
    allowed_directories = {
        parent.as_posix()
        for name in MEMBERS
        for parent in Path(name).parents
        if parent.as_posix() != "."
    }
    for path in bundle.rglob("*"):
        name = path.relative_to(bundle).as_posix()
        mode = path.lstat().st_mode
        if path.resolve() != path:
            raise ValueError("decoded paired supplier path is an alias")
        if stat.S_ISDIR(mode) and name in allowed_directories:
            continue
        if not stat.S_ISREG(mode) or path.stat().st_nlink != 1 or name not in MEMBERS:
            raise ValueError("unexpected or nonregular decoded supplier member")
        maximum = (
            128 * 1024 * 1024 if name.endswith("componentize-py") else 16 * 1024 * 1024
        )
        if fingerprint(path, maximum) != pins["files"][name]:
            raise ValueError("decoded paired supplier bytes differ")
        seen.add(name)
    if seen != MEMBERS:
        raise ValueError("decoded paired supplier is incomplete")
    pair = read_json(bundle / "poll-producer-pair.json")
    if (
        pair.get("format") != "tobkiri.ui-empty-poll-producer-pair/1"
        or pair.get("outcome") != "success"
        or pair.get("changed_asset") != "libc.so.zst"
        or pair.get("same_helper_required_for_both_components") is not True
        or any(
            pair.get(name) is not False
            for name in (
                "componentization",
                "guest_execution",
                "trace_observed",
                "final_provider_resolution_verified",
                "c_abi_verified",
                "cold_schema_verified",
                "production_enabled",
            )
        )
    ):
        raise ValueError("actual supplier pair has a different or incomplete scope")
    poll_producer_pair.require_pair(pair["original"], pair["candidate"])
    for variant, prefix in PREFIXES.items():
        producer, source = (
            read_json(bundle / prefix / name)
            for name in ("cli-build.json", "cli-source.json")
        )
        if (
            producer != pair[variant]["producer"]
            or source != pair[variant]["source"]
            or producer.get("binary")
            != pins["files"][prefix + "/work/target/release/componentize-py"]
            or producer.get("inputs_unchanged") is not True
            or producer.get("producer_cli") is not True
            or producer.get("production_enabled") is not False
            or source.get("ci_link_trace", {}).get("feature")
            != ci_link_trace_source.FEATURE
            or source["ci_link_trace"].get("upstream_link_sha256")
            != ci_link_trace_source.ORIGINAL_LINK_SHA256
        ):
            raise ValueError(
                "actual CLI/source receipts do not match the supplier pair"
            )
    helper = read_json(bundle / HELPER_RECEIPT)
    if (
        helper.get("outcome") != "success"
        or helper.get("compiled") is not True
        or helper.get("inputs_unchanged") is not True
        or helper.get("helper") != pair.get("helper")
        or pair["helper"] != pins["files"][HELPER]
        or pair.get("helper_receipt") != pins["files"][HELPER_RECEIPT]
        or helper.get("inspection", {}).get("shared_imports_verified") is not True
    ):
        raise ValueError("actual compiled C helper custody differs")
    return {"pair": pair, "helper": helper}
