#!/usr/bin/env python3
"""Measure Tobkiri Launcher bundle/runtime size with rollback-friendly JSON output."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


REPORT_VERSION = 1
DISCARDABLE_NAMES = {".DS_Store", "manual-defaultspack.out"}
DISCARDABLE_SUFFIXES = {".log", ".jsonl", ".map", ".pyc", ".pyo"}
DISCARDABLE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


@dataclass(frozen=True)
class PathMetric:
    path: str
    bytes: int
    files: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, help="Built .app directory (optional).")
    parser.add_argument("--runtime-root", type=Path, help="Resources/app or staged runtime directory.")
    parser.add_argument("--baseline", type=Path, help="Previous JSON report for deltas.")
    parser.add_argument("--output", type=Path, required=True, help="Output JSON path.")
    parser.add_argument("--label", default="", help="Build/commit label stored in the report.")
    parser.add_argument("--largest", type=int, default=30, help="Number of largest files to include.")
    parser.add_argument(
        "--fail-on-growth",
        action="append",
        default=[],
        metavar="METRIC",
        help="Exit 2 if the named metric grew versus --baseline; repeatable.",
    )
    return parser.parse_args()


def iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            yield path


def metric(path: Path) -> PathMetric:
    if not path.exists():
        return PathMetric(str(path), 0, 0)
    if path.is_file():
        return PathMetric(str(path), path.stat().st_size, 1)
    total = 0
    count = 0
    for item in iter_files(path):
        total += item.stat().st_size
        count += 1
    return PathMetric(str(path), total, count)


def resolve_runtime_root(app: Path | None, runtime_root: Path | None) -> Path:
    if runtime_root:
        return runtime_root.resolve()
    if not app:
        raise ValueError("Pass --runtime-root or --app")
    candidate = app.resolve() / "Contents" / "Resources" / "app"
    if not candidate.exists():
        raise FileNotFoundError(f"Runtime root not found: {candidate}")
    return candidate


def is_discardable_candidate(path: Path) -> bool:
    return (
        path.name in DISCARDABLE_NAMES
        or path.suffix.lower() in DISCARDABLE_SUFFIXES
        or any(part in DISCARDABLE_PARTS for part in path.parts)
    )


def build_report(
    app: Path | None,
    runtime_root: Path,
    largest: int,
    *,
    label: str = "",
) -> dict[str, object]:
    roots: dict[str, Path] = {"resources_app": runtime_root}
    if app:
        roots["installed_app"] = app.resolve()
    for name, relative in {
        "ecosystem": "ecosystem",
        "core_runtime": "core_runtime",
        "frontend_web": "core_runtime/core_pack/core_control_panel/web",
        "bundled_tools": "bundled",
    }.items():
        roots[name] = runtime_root / relative

    metrics = {name: asdict(metric(path)) for name, path in roots.items()}
    top_level = []
    if runtime_root.exists():
        for child in runtime_root.iterdir():
            item = asdict(metric(child))
            item["name"] = child.name
            top_level.append(item)
        top_level.sort(key=lambda item: int(item["bytes"]), reverse=True)

    extension_bytes: Counter[str] = Counter()
    extension_files: Counter[str] = Counter()
    largest_files: list[tuple[int, str]] = []
    discardable_bytes = 0
    discardable_files: list[tuple[int, str]] = []
    for path in iter_files(runtime_root):
        size = path.stat().st_size
        extension = path.suffix.lower() or "<none>"
        extension_bytes[extension] += size
        extension_files[extension] += 1
        relative = path.relative_to(runtime_root).as_posix()
        largest_files.append((size, relative))
        if is_discardable_candidate(path.relative_to(runtime_root)):
            discardable_bytes += size
            discardable_files.append((size, relative))
    largest_files.sort(reverse=True)
    discardable_files.sort(reverse=True)

    return {
        "report_version": REPORT_VERSION,
        "label": label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "top_level": top_level,
        "extensions": [
            {
                "extension": extension,
                "bytes": size,
                "files": extension_files[extension],
            }
            for extension, size in extension_bytes.most_common()
        ],
        "largest_files": [
            {"path": path, "bytes": size}
            for size, path in largest_files[: max(0, largest)]
        ],
        "discardable_candidates": {
            "bytes": discardable_bytes,
            "files": len(discardable_files),
            "largest_files": [
                {"path": path, "bytes": size}
                for size, path in discardable_files[: max(0, largest)]
            ],
            "note": "Measurement only; no file is deleted automatically.",
        },
    }


def add_deltas(report: dict[str, object], baseline: dict[str, object]) -> None:
    current_metrics = report.get("metrics", {})
    baseline_metrics = baseline.get("metrics", {})
    deltas: dict[str, int] = {}
    if isinstance(current_metrics, dict) and isinstance(baseline_metrics, dict):
        for name, current in current_metrics.items():
            previous = baseline_metrics.get(name)
            if not isinstance(current, dict) or not isinstance(previous, dict):
                continue
            current_bytes = current.get("bytes")
            previous_bytes = previous.get("bytes")
            if isinstance(current_bytes, int) and isinstance(previous_bytes, int):
                deltas[name] = current_bytes - previous_bytes
    report["delta_bytes"] = deltas


def growth_failures(report: dict[str, object], names: Iterable[str]) -> list[dict[str, object]]:
    deltas = report.get("delta_bytes", {})
    metrics = report.get("metrics", {})
    failures = []
    for name in names:
        if not isinstance(metrics, dict) or name not in metrics:
            failures.append({"metric": name, "error": "unknown metric"})
            continue
        delta = deltas.get(name) if isinstance(deltas, dict) else None
        if not isinstance(delta, int):
            failures.append({"metric": name, "error": "baseline metric unavailable"})
        elif delta > 0:
            failures.append({"metric": name, "delta_bytes": delta})
    return failures


def write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main() -> int:
    args = parse_args()
    runtime_root = resolve_runtime_root(args.app, args.runtime_root)
    report = build_report(args.app, runtime_root, args.largest, label=args.label)
    if args.baseline:
        with args.baseline.open("r", encoding="utf-8") as handle:
            add_deltas(report, json.load(handle))
    failures = growth_failures(report, args.fail_on_growth)
    if failures:
        report["budget_failures"] = failures
    write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
