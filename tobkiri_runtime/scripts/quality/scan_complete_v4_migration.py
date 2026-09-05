#!/usr/bin/env python3
"""Emit the non-negotiable complete-v4 migration gate evidence.

The scanner delegates the repository inventory rules to the dedicated test
module so the CI gate and the handoff evidence cannot drift apart.  It has no
historical exception input: every finding is measured against the v4 target.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
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
    source_sets = report["pack_inventory"]["declared_source_sets"]
    return {
        "production_pack_directories": report["pack_inventory"][
            "production_pack_directories"
        ],
        "catalog_pack_directories": report["pack_inventory"][
            "catalog_pack_directories"
        ],
        "v4_artifacts_per_pack": report["pack_inventory"]["v4_artifacts_per_pack"],
        "v4_artifact_files": report["pack_inventory"]["v4_artifact_files"],
        "v4_pack_artifacts": len(report["pack_inventory"]["v4_pack_artifacts"]),
        "v4_profile_artifacts": len(report["pack_inventory"]["v4_profile_artifacts"]),
        "migration_status": report["pack_inventory"]["migration_status_counts"],
        "legacy_manifest_declared_packs": len(source_sets["manifest_ids"]),
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
    evidence = {
        "schema": "io.tobkiri.quality.complete-v4-migration-evidence.v2",
        "source": {
            "test_file": TEST_PATH.relative_to(ROOT).as_posix(),
            "freshness_basis": "deterministic-semantic-recomputation",
            "observed_head_sha": report["head_sha"],
            "semantic_digest": "",
        },
        "nodeids": _nodeids(),
        "counts": _counts(report),
        "gate": report["gate"],
        "gates": report["gates"],
        "pack_inventory": report["pack_inventory"],
        "findings": report["findings"],
    }
    evidence["source"]["semantic_digest"] = _semantic_digest(evidence)
    return evidence


def _semantic_document(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return evidence content without informational and self-digest fields."""

    normalized = json.loads(json.dumps(evidence))
    source = normalized.get("source")
    if isinstance(source, dict):
        source.pop("observed_head_sha", None)
        source.pop("semantic_digest", None)
    return normalized


def _semantic_digest(evidence: dict[str, Any]) -> str:
    """Digest the complete recomputed scan without recursive metadata."""

    encoded = json.dumps(
        _semantic_document(evidence),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def evidence_drift(
    tracked: dict[str, Any],
    observed: dict[str, Any],
    *,
    event_name: str,
    pr_head_sha: str = "",
) -> list[str]:
    """Return fail-closed deterministic evidence errors.

    Commit identity is deliberately informational: binding a checked-in file
    to the commit that contains that same file is recursively impossible.
    Freshness instead requires exact semantic recomputation and a content
    digest over that recomputed document.
    """

    errors: list[str] = []
    if _semantic_document(tracked) != _semantic_document(observed):
        errors.append("tracked evidence differs from the current semantic scan")
    del event_name, pr_head_sha
    for label, document in (("tracked", tracked), ("observed", observed)):
        source = document.get("source")
        if not isinstance(source, dict):
            errors.append(f"{label} evidence source is missing")
            continue
        observed_sha = source.get("observed_head_sha")
        if not isinstance(observed_sha, str) or not COMMIT_SHA_RE.fullmatch(observed_sha):
            errors.append(f"{label} informational HEAD is malformed")
        if source.get("freshness_basis") != "deterministic-semantic-recomputation":
            errors.append(f"{label} evidence freshness basis is invalid")
        semantic_digest = source.get("semantic_digest")
        if semantic_digest != _semantic_digest(document):
            errors.append(f"{label} evidence semantic digest is invalid")
    return errors


def _summary_markdown(
    evidence: dict[str, Any],
    drift: list[str],
    *,
    freshness_only: bool,
) -> str:
    """Return a concise CI summary without overstating migration completion."""

    gate_status = evidence["gate"]["status"]
    freshness_status = "FAIL" if drift else "PASS"
    enforcement = "report-only" if freshness_only else "blocking"
    lines = [
        "## Pack v4 migration evidence",
        "",
        f"- Evidence freshness: **{freshness_status}**",
        f"- Semantic migration status: **{gate_status}** ({enforcement})",
    ]
    if gate_status == "RED" and freshness_only:
        migration_findings = evidence.get("findings", {}).get(
            "migration_evidence", []
        )
        missing = sum(
            finding.get("unverified_count", 0)
            for finding in migration_findings
            if isinstance(finding, dict)
        )
        lines.append(
            "- Phase 0 keeps genuine semantic findings visible without claiming "
            f"migration completion ({missing} Pack release proof(s) missing)."
        )
    if drift:
        lines.extend(["", "Freshness errors:"])
        lines.extend(f"- {error}" for error in drift)
    return "\n".join(lines) + "\n"


def _exit_code(
    evidence: dict[str, Any],
    drift: list[str],
    *,
    freshness_only: bool,
) -> int:
    """Return the process status for strict or Phase 0 freshness enforcement."""

    if drift:
        return 1
    if freshness_only:
        return 0
    return 0 if evidence["gate"]["status"] == "GREEN" else 1


def main() -> int:
    """Write evidence and enforce strict or explicit Phase 0 exit semantics."""
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
    parser.add_argument(
        "--freshness-only",
        action="store_true",
        help=(
            "Phase 0 mode: fail on evidence drift while reporting, but not enforcing, "
            "the semantic migration status"
        ),
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="append a concise migration status report to this CI summary file",
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
                "enforcement": (
                    "freshness-only" if args.freshness_only else "semantic-and-freshness"
                ),
                "nodeids": len(evidence["nodeids"]),
                "counts": counts,
                "drift": drift,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if args.summary_file is not None:
        summary_file = (
            args.summary_file
            if args.summary_file.is_absolute()
            else ROOT / args.summary_file
        )
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        with summary_file.open("a", encoding="utf-8") as stream:
            stream.write(
                _summary_markdown(
                    evidence,
                    drift,
                    freshness_only=args.freshness_only,
                )
            )
    return _exit_code(evidence, drift, freshness_only=args.freshness_only)


if __name__ == "__main__":
    raise SystemExit(main())
