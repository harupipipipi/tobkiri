#!/usr/bin/env python3
"""Classify launcher log findings without hiding integrity failures."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class FindingRule:
    key: str
    category: str
    severity: str
    pattern: re.Pattern[str]
    explanation: str


RULES = (
    FindingRule(
        "defaultspack_port_collision",
        "startup_race",
        "error",
        re.compile(r"(?:OSError\s*\(?48\)?|Address already in use)", re.I),
        "Concurrent or stale Defaultspack launch attempted to bind an occupied port.",
    ),
    FindingRule(
        "defaultspack_existing_ready",
        "startup_state",
        "info",
        re.compile(r"existing_ready\s*[=:]\s*true", re.I),
        "A ready existing Defaultspack instance was observed; correlate with port-collision timestamps.",
    ),
    FindingRule(
        "flow_load_errors",
        "integrity",
        "error",
        re.compile(r"flows_registered\s*=\s*\d+.*flow_errors\s*=\s*[1-9]\d*", re.I),
        "One or more flows failed registration and must remain visible as data/schema errors.",
    ),
    FindingRule(
        "v3_process_manifest_invalid",
        "integrity",
        "error",
        re.compile(r"v3_process_manifest_invalid", re.I),
        "A v3 process manifest failed validation.",
    ),
    FindingRule(
        "invalid_manifest",
        "integrity",
        "error",
        re.compile(r"(?<!v3_process_)invalid_manifest", re.I),
        "A pack or provider manifest failed validation.",
    ),
    FindingRule(
        "frontend_pack_hash_mismatch",
        "integrity",
        "error",
        re.compile(r"frontend_pack_hash_mismatch", re.I),
        "Built frontend files do not match the declared pack hash.",
    ),
    FindingRule(
        "missing_pack",
        "integrity",
        "error",
        re.compile(r"missing_pack", re.I),
        "A referenced pack is absent.",
    ),
    FindingRule(
        "missing_provider",
        "integrity",
        "error",
        re.compile(r"missing_provider", re.I),
        "A referenced provider is absent.",
    ),
    FindingRule(
        "unsupported_profile_version_none",
        "integrity",
        "error",
        re.compile(r"unsupported profile version:\s*None", re.I),
        "The profile schema version is missing; it must not be silently defaulted.",
    ),
    FindingRule(
        "host_execution_warning",
        "host_execution",
        "warning",
        re.compile(r"host_execution", re.I),
        "Host execution warning; inspect permission, broker, and capability context separately from startup timing.",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--fail-on-integrity",
        action="store_true",
        help="Write the full report, then exit 2 when integrity errors are present.",
    )
    parser.add_argument(
        "--fail-on-startup-race",
        action="store_true",
        help="Write the full report, then exit 3 when startup-race errors are present.",
    )
    return parser.parse_args()


def lines(paths: Iterable[Path]) -> Iterable[tuple[Path, int, str]]:
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                yield path, number, line.rstrip("\n")


def analyze(paths: Iterable[Path]) -> dict[str, object]:
    path_list = list(paths)
    counts: Counter[str] = Counter()
    matched_lines: Counter[str] = Counter()
    examples: dict[str, list[dict[str, object]]] = {}
    for path, number, line in lines(path_list):
        line_matched = False
        for rule in RULES:
            if not rule.pattern.search(line):
                continue
            line_matched = True
            counts[rule.key] += 1
            bucket = examples.setdefault(rule.key, [])
            if len(bucket) < 5:
                bucket.append({"path": str(path), "line": number, "text": line[:1_000]})
        if line_matched:
            matched_lines[str(path)] += 1

    findings = []
    category_occurrences: Counter[str] = Counter()
    severity_occurrences: Counter[str] = Counter()
    for rule in RULES:
        count = counts[rule.key]
        if count == 0:
            continue
        category_occurrences[rule.category] += count
        severity_occurrences[rule.severity] += count
        findings.append({
            "key": rule.key,
            "category": rule.category,
            "severity": rule.severity,
            "count": count,
            "explanation": rule.explanation,
            "examples": examples.get(rule.key, []),
        })
    return {
        "summary": {
            "files": len(path_list),
            "matched_lines": sum(matched_lines.values()),
            "by_category": dict(category_occurrences),
            "by_severity": dict(severity_occurrences),
        },
        "sources": [
            {"path": str(path), "matched_lines": matched_lines[str(path)]}
            for path in path_list
        ],
        "findings": findings,
    }


def has_error(report: dict[str, object], category: str) -> bool:
    findings = report.get("findings", [])
    return any(
        isinstance(item, dict)
        and item.get("category") == category
        and item.get("severity") == "error"
        and isinstance(item.get("count"), int)
        and item["count"] > 0
        for item in findings
    ) if isinstance(findings, list) else False


def write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main() -> int:
    args = parse_args()
    report = analyze(args.logs)
    write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.fail_on_integrity and has_error(report, "integrity"):
        return 2
    if args.fail_on_startup_race and has_error(report, "startup_race"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
