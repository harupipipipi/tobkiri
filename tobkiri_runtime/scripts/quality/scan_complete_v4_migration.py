#!/usr/bin/env python3
"""Emit the non-negotiable complete-v4 migration gate evidence.

The scanner delegates the repository inventory rules to the dedicated test
module so the CI gate and the handoff evidence cannot drift apart.  It has no
historical exception input: every finding is measured against the v4 target.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TEST_PATH = ROOT / "tobkiri_runtime" / "tests" / "test_complete_v4_migration_gate.py"
DEFAULT_OUTPUT = (
    ROOT
    / "tobkiri_runtime"
    / "scripts"
    / "quality"
    / "evidence"
    / "complete_v4_migration_red_64b2240e.json"
)
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _load_gate_module() -> ModuleType:
    """Load the dedicated gate helpers without importing application code."""
    runtime_path = ROOT / "tobkiri_runtime"
    if str(runtime_path) not in sys.path:
        sys.path.insert(0, str(runtime_path))
    spec = importlib.util.spec_from_file_location(
        "complete_v4_migration_gate", TEST_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load gate module: {TEST_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _nodeids() -> list[str]:
    """Return deterministic pytest nodeids from the dedicated test file."""
    tree = ast.parse(TEST_PATH.read_text(encoding="utf-8"), filename=str(TEST_PATH))
    relative = TEST_PATH.relative_to(ROOT).as_posix()
    return [
        f"{relative}::{node.name}"
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


def _counts(report: dict[str, Any]) -> dict[str, Any]:
    """Flatten evidence lists into handoff-friendly deterministic counts."""
    findings = report["findings"]
    source_sets = report["pack_inventory"]["authority_source_sets"]
    return {
        "production_pack_directories": report["pack_inventory"][
            "production_pack_directories"
        ],
        "expected_production_pack_directories": report["pack_inventory"][
            "expected_production_pack_directories"
        ],
        "v4_artifacts_per_pack": report["pack_inventory"]["v4_artifacts_per_pack"],
        "v4_artifact_files": report["pack_inventory"]["v4_artifact_files"],
        "v4_pack_artifacts": len(report["pack_inventory"]["v4_pack_artifacts"]),
        "v4_profile_artifacts": len(report["pack_inventory"]["v4_profile_artifacts"]),
        "authority_classification": report["pack_inventory"]["authority_counts"],
        "manifest_authority_source_packs": len(source_sets["manifest_ids"]),
        "v4_only_packs": len(source_sets["v4_only_ids"]),
        "canonical_source_packs": len(report["pack_inventory"]["canonical_source_ids"]),
        "gates": {
            key: len(value)
            for key, value in findings.items()
            if isinstance(value, list)
        },
    }


def build_evidence() -> dict[str, Any]:
    """Build the complete current-tree evidence document."""
    gate_module = _load_gate_module()
    report = gate_module._audit_snapshot()
    return {
        "schema": "io.tobkiri.quality.complete-v4-migration-evidence.v1",
        "source": {
            "test_file": TEST_PATH.relative_to(ROOT).as_posix(),
            "observed_head_sha": report["head_sha"],
        },
        "nodeids": _nodeids(),
        "counts": _counts(report),
        "gate": report["gate"],
        "gates": report["gates"],
        "pack_inventory": report["pack_inventory"],
        "findings": report["findings"],
    }


def _semantic_document(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return evidence content without the self-referential commit field."""

    normalized = json.loads(json.dumps(evidence))
    source = normalized.get("source")
    if isinstance(source, dict):
        source.pop("observed_head_sha", None)
    return normalized


def provenance_errors(
    *,
    tracked_sha: object,
    event_name: str,
    current_sha: str,
    current_parents: tuple[str, ...],
    pr_head_sha: str = "",
    pr_head_parents: tuple[str, ...] = (),
) -> list[str]:
    """Validate evidence provenance against one explicit CI event topology."""

    if not isinstance(tracked_sha, str) or not COMMIT_SHA_RE.fullmatch(tracked_sha):
        return [f"tracked observed_head_sha is missing or malformed: {tracked_sha!r}"]
    if not COMMIT_SHA_RE.fullmatch(current_sha) or any(
        not COMMIT_SHA_RE.fullmatch(parent) for parent in current_parents
    ):
        return ["checkout commit topology is malformed"]

    accepted: set[str]
    if event_name == "push":
        accepted = {current_sha}
        if current_parents:
            accepted.add(current_parents[0])
    elif event_name == "pull_request":
        if not COMMIT_SHA_RE.fullmatch(pr_head_sha):
            return ["pull_request head SHA is missing or malformed"]
        if pr_head_sha not in current_parents:
            return ["pull_request head SHA is not a direct checkout parent"]
        if not pr_head_parents or any(
            not COMMIT_SHA_RE.fullmatch(parent) for parent in pr_head_parents
        ):
            return ["pull_request head topology is missing or malformed"]
        accepted = {pr_head_sha, pr_head_parents[0]}
    else:
        return [f"unsupported or missing CI event name: {event_name!r}"]

    if tracked_sha not in accepted:
        return [
            "tracked observed_head_sha is stale for "
            f"{event_name}: expected one of {sorted(accepted)}, got {tracked_sha!r}"
        ]
    return []


def _git_sha(*args: str) -> str:
    """Return one exact commit SHA or fail the freshness check."""

    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _git_parents(revision: str) -> tuple[str, ...]:
    """Return the ordered direct parents for one verified revision."""

    verified = _git_sha("rev-parse", "--verify", f"{revision}^{{commit}}")
    if not COMMIT_SHA_RE.fullmatch(verified):
        raise RuntimeError(f"invalid commit revision: {revision}")
    parents = _git_sha("show", "-s", "--format=%P", verified)
    return tuple(parents.split()) if parents else ()


def evidence_drift(
    tracked: dict[str, Any],
    observed: dict[str, Any],
    *,
    event_name: str,
    pr_head_sha: str = "",
) -> list[str]:
    """Return fail-closed semantic and provenance evidence errors."""

    errors: list[str] = []
    if _semantic_document(tracked) != _semantic_document(observed):
        errors.append("tracked evidence differs from the current semantic scan")

    source = tracked.get("source")
    tracked_sha = source.get("observed_head_sha") if isinstance(source, dict) else None
    try:
        current_sha = _git_sha("rev-parse", "--verify", "HEAD^{commit}")
        current_parents = _git_parents(current_sha)
        pr_head_parents = (
            _git_parents(pr_head_sha)
            if event_name == "pull_request" and COMMIT_SHA_RE.fullmatch(pr_head_sha)
            else ()
        )
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        errors.append(f"evidence provenance cannot inspect git topology: {error}")
        return errors
    errors.extend(
        provenance_errors(
            tracked_sha=tracked_sha,
            event_name=event_name,
            current_sha=current_sha,
            current_parents=current_parents,
            pr_head_sha=pr_head_sha,
            pr_head_parents=pr_head_parents,
        )
    )
    return errors


def main() -> int:
    """Write evidence and return non-zero while any migration gate is RED."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="evidence JSON destination (default: the checked-in quality evidence path)",
    )
    parser.add_argument(
        "--check-against",
        type=Path,
        help="compare the temporary scan against checked-in evidence",
    )
    parser.add_argument(
        "--event-name",
        default=os.environ.get("GITHUB_EVENT_NAME", ""),
        help="explicit CI event name (push or pull_request)",
    )
    parser.add_argument(
        "--pr-head-sha",
        default=os.environ.get("TOBKIRI_PR_HEAD_SHA", ""),
        help="exact pull_request head SHA supplied by the workflow event",
    )
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    evidence = build_evidence()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    drift: list[str] = []
    if args.check_against is not None:
        tracked_path = (
            args.check_against
            if args.check_against.is_absolute()
            else ROOT / args.check_against
        )
        try:
            tracked = json.loads(tracked_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            drift = [f"tracked evidence is unreadable: {error}"]
        else:
            if not isinstance(tracked, dict):
                drift = ["tracked evidence is not a JSON object"]
            else:
                drift = evidence_drift(
                    tracked,
                    evidence,
                    event_name=args.event_name,
                    pr_head_sha=args.pr_head_sha,
                )
    counts = evidence["counts"]
    try:
        output_name = output.relative_to(ROOT).as_posix()
    except ValueError:
        output_name = str(output)
    print(
        json.dumps(
            {
                "output": output_name,
                "status": evidence["gate"]["status"],
                "nodeids": len(evidence["nodeids"]),
                "counts": counts,
                "drift": drift,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if evidence["gate"]["status"] == "GREEN" and not drift else 1


if __name__ == "__main__":
    raise SystemExit(main())
