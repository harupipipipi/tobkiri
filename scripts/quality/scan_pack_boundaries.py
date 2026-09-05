#!/usr/bin/env python3
"""Lint Pack v4 boundaries without weakening runtime validation authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


BASELINE_API_VERSION = "io.tobkiri.pack-boundary-baseline.v1"
REPORT_API_VERSION = "io.tobkiri.pack-boundary-report.v1"
POLICY = "exact-current-shrink-only-from-reference"
FATAL_RULES = {
    "pack-boundary.scan.parse-error",
    "pack-boundary.scan.symlink",
    "pack-boundary.scan.unknown-schema",
}
VERSION_KEYS = {
    "catalog_api_version",
    "index_api_version",
    "pack_api_version",
    "profile_api_version",
    "schema",
}
KNOWN_V4_SCHEMAS = {
    "io.tobkiri.executable-catalog.v4",
    "io.tobkiri.frontend-contract-map.v4",
    "io.tobkiri.pack-artifact-index.v4",
    "io.tobkiri.pack-contract-catalog.v4",
    "io.tobkiri.pack.v4",
    "io.tobkiri.profile.v4",
    "io.tobkiri.workflow-backend-integrity.v4",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(rule_id: str, path: str, evidence: dict[str, Any]) -> str:
    material = _canonical_json(
        {"evidence": evidence, "path": path, "rule_id": rule_id}
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


def _diagnostic(
    rule_id: str,
    path: str,
    line: int,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "evidence": evidence,
        "fingerprint": _fingerprint(rule_id, path, evidence),
        "line": max(1, line),
        "path": path,
        "rule_id": rule_id,
    }


def _line_for(text: str, key: str, value: str | None = None) -> int:
    key_token = json.dumps(key)
    value_token = json.dumps(value) if value is not None else None
    for number, line in enumerate(text.splitlines(), start=1):
        if key_token in line and (value_token is None or value_token in line):
            return number
    if value_token is not None:
        for number, line in enumerate(text.splitlines(), start=1):
            if value_token in line:
                return number
    return 1


def _walk_dicts(value: Any, path: str = "$") -> Iterable[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        yield path, value
        for key in sorted(value):
            yield from _walk_dicts(value[key], f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_dicts(item, f"{path}[{index}]")


def _discover_json_files(
    repo_root: Path,
    ecosystem_root: Path,
) -> tuple[list[Path], list[dict[str, Any]]]:
    files: list[Path] = []
    diagnostics: list[dict[str, Any]] = []
    for directory, names, filenames in os.walk(ecosystem_root, followlinks=False):
        directory_path = Path(directory)
        for name in sorted(names):
            candidate = directory_path / name
            if candidate.is_symlink():
                relative = candidate.relative_to(repo_root).as_posix()
                diagnostics.append(
                    _diagnostic(
                        "pack-boundary.scan.symlink",
                        relative,
                        1,
                        {"entry_kind": "directory"},
                    )
                )
                names.remove(name)
        for name in sorted(filenames):
            if not name.endswith(".json"):
                continue
            candidate = directory_path / name
            if candidate.is_symlink():
                relative = candidate.relative_to(repo_root).as_posix()
                diagnostics.append(
                    _diagnostic(
                        "pack-boundary.scan.symlink",
                        relative,
                        1,
                        {"entry_kind": "file"},
                    )
                )
                continue
            files.append(candidate)
    return sorted(files), diagnostics


def _read_documents(
    repo_root: Path,
    files: Iterable[Path],
) -> tuple[dict[Path, tuple[Any, str]], list[dict[str, Any]]]:
    documents: dict[Path, tuple[Any, str]] = {}
    diagnostics: list[dict[str, Any]] = []
    for path in files:
        relative = path.relative_to(repo_root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
            documents[path] = (json.loads(text), text)
        except (OSError, UnicodeError) as exc:
            diagnostics.append(
                _diagnostic(
                    "pack-boundary.scan.parse-error",
                    relative,
                    1,
                    {"error": type(exc).__name__},
                )
            )
        except json.JSONDecodeError as exc:
            diagnostics.append(
                _diagnostic(
                    "pack-boundary.scan.parse-error",
                    relative,
                    exc.lineno,
                    {"column": exc.colno, "error": "JSONDecodeError"},
                )
            )
    return documents, diagnostics


def _scan_schema(
    repo_root: Path,
    path: Path,
    document: Any,
    text: str,
) -> list[dict[str, Any]]:
    if not path.name.endswith(".v4.json"):
        return []
    relative = path.relative_to(repo_root).as_posix()
    if not isinstance(document, dict):
        return [
            _diagnostic(
                "pack-boundary.scan.unknown-schema",
                relative,
                1,
                {"reason": "v4 document root is not an object"},
            )
        ]
    declarations = [
        (key, document[key]) for key in sorted(VERSION_KEYS) if key in document
    ]
    if not declarations:
        return [
            _diagnostic(
                "pack-boundary.scan.unknown-schema",
                relative,
                1,
                {"reason": "no top-level API or schema declaration"},
            )
        ]

    diagnostics: list[dict[str, Any]] = []
    for key, value in declarations:
        if not isinstance(value, str):
            diagnostics.append(
                _diagnostic(
                    "pack-boundary.scan.unknown-schema",
                    relative,
                    _line_for(text, key),
                    {"declaration": key, "reason": "schema identifier is not a string"},
                )
            )
            continue
        if re.search(r"\.v5(?:$|\.)", value):
            diagnostics.append(
                _diagnostic(
                    "pack-boundary.v4-file-uses-v5-api",
                    relative,
                    _line_for(text, key, value),
                    {"declaration": key, "value": value},
                )
            )
            continue
        if value not in KNOWN_V4_SCHEMAS:
            diagnostics.append(
                _diagnostic(
                    "pack-boundary.scan.unknown-schema",
                    relative,
                    _line_for(text, key, value),
                    {"declaration": key, "value": value},
                )
            )
    return diagnostics


def _pack_dependencies(document: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    requirements = document.get("requirements")
    if not isinstance(requirements, dict):
        return [], {}
    contracts = requirements.get("contract_dependencies")
    packs = requirements.get("pack_dependencies")
    return (
        contracts if isinstance(contracts, list) else [],
        packs if isinstance(packs, dict) else {},
    )


def _effect_classes(executable: Any) -> dict[tuple[str, str], str | None]:
    result: dict[tuple[str, str], str | None] = {}
    if not isinstance(executable, dict):
        return result
    variants = executable.get("variants")
    if not isinstance(variants, list):
        return result
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        function_id = variant.get("function_id")
        operations = variant.get("operations")
        if not isinstance(function_id, str) or not isinstance(operations, list):
            continue
        for operation in operations:
            if isinstance(operation, dict) and isinstance(
                operation.get("operation_id"), str
            ):
                result[(function_id, operation["operation_id"])] = operation.get(
                    "effect_class"
                )
    return result


def _scan_pack(
    repo_root: Path,
    path: Path,
    document: Any,
    text: str,
    executable: Any,
) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    relative = path.relative_to(repo_root).as_posix()
    diagnostics: list[dict[str, Any]] = []
    functions = document.get("functions")
    contracts = document.get("contracts")
    contract_dependencies, pack_dependencies = _pack_dependencies(document)
    if (
        functions == []
        and contracts == []
        and contract_dependencies == []
        and pack_dependencies == {}
    ):
        diagnostics.append(
            _diagnostic(
                "pack-boundary.empty-pack-boundary",
                relative,
                _line_for(text, "functions"),
                {
                    "contracts": 0,
                    "contract_dependencies": 0,
                    "functions": 0,
                    "pack_dependencies": 0,
                },
            )
        )

    migration = document.get("migration")
    if isinstance(migration, dict) and migration.get("sunset_at") == "2099-01-01":
        diagnostics.append(
            _diagnostic(
                "pack-boundary.indefinite-sunset",
                relative,
                _line_for(text, "sunset_at", "2099-01-01"),
                {"sunset_at": "2099-01-01"},
            )
        )

    operation_catalog = document.get("operation_catalog")
    operations = (
        {
            item.get("operation_id"): item
            for item in operation_catalog
            if isinstance(item, dict) and isinstance(item.get("operation_id"), str)
        }
        if isinstance(operation_catalog, list)
        else {}
    )
    function_entries = functions if isinstance(functions, list) else []
    effect_classes = _effect_classes(executable)

    pack = document.get("pack")
    if isinstance(pack, dict) and pack.get("kind") == "host_extension":
        if not operations:
            diagnostics.append(
                _diagnostic(
                    "pack-boundary.host-extension-without-external-effect",
                    relative,
                    _line_for(text, "kind", "host_extension"),
                    {"operation_id": None, "reason": "no operations"},
                )
            )
        for operation_id, operation in sorted(operations.items()):
            ceiling = operation.get("effect_ceiling")
            has_host_effect = isinstance(ceiling, list) and any(
                isinstance(item, str) and item.startswith("host:") for item in ceiling
            )
            if not has_host_effect:
                diagnostics.append(
                    _diagnostic(
                        "pack-boundary.host-extension-without-external-effect",
                        relative,
                        _line_for(text, "operation_id", operation_id),
                        {
                            "effect_ceiling": ceiling,
                            "operation_id": operation_id,
                        },
                    )
                )

    for function in function_entries:
        if (
            not isinstance(function, dict)
            or function.get("isolation") != "dedicated_process"
        ):
            continue
        function_id = function.get("id")
        function_operations = function.get("operations")
        if not isinstance(function_id, str) or not isinstance(
            function_operations, list
        ):
            continue
        for operation_id in function_operations:
            if not isinstance(operation_id, str):
                continue
            ceiling = operations.get(operation_id, {}).get("effect_ceiling")
            effect_class = effect_classes.get((function_id, operation_id))
            if effect_class == "pure" or ceiling == []:
                diagnostics.append(
                    _diagnostic(
                        "pack-boundary.pure-operation-dedicated-process",
                        relative,
                        _line_for(text, "id", function_id),
                        {
                            "effect_class": effect_class,
                            "function_id": function_id,
                            "operation_id": operation_id,
                        },
                    )
                )
    return diagnostics


def scan_repository(repo_root: Path) -> list[dict[str, Any]]:
    """Return deterministic boundary diagnostics for one repository tree."""
    repo_root = repo_root.resolve()
    ecosystem_root = repo_root / "tobkiri_runtime" / "ecosystem"
    if not ecosystem_root.is_dir():
        return [
            _diagnostic(
                "pack-boundary.scan.parse-error",
                "tobkiri_runtime/ecosystem",
                1,
                {"error": "ecosystem root is missing"},
            )
        ]

    files, diagnostics = _discover_json_files(repo_root, ecosystem_root)
    documents, read_diagnostics = _read_documents(repo_root, files)
    diagnostics.extend(read_diagnostics)
    for path, (document, text) in sorted(documents.items()):
        diagnostics.extend(_scan_schema(repo_root, path, document, text))
        for json_path, node in _walk_dicts(document):
            if node.get("normative") is True and node.get("repository_commit") == (
                "working-tree"
            ):
                relative = path.relative_to(repo_root).as_posix()
                diagnostics.append(
                    _diagnostic(
                        "pack-boundary.normative-working-tree",
                        relative,
                        _line_for(text, "repository_commit", "working-tree"),
                        {"json_path": json_path},
                    )
                )

    canonical_packs: dict[str, tuple[Path, Any, str]] = {}
    for path, (document, text) in documents.items():
        if path.parent.parent != ecosystem_root or path.name != "pack.v4.json":
            continue
        if not isinstance(document, dict):
            continue
        pack = document.get("pack")
        if isinstance(pack, dict) and isinstance(pack.get("id"), str):
            canonical_packs[pack["id"]] = (path, document, text)
        executable_path = path.parent / "executables.v4.json"
        executable = documents.get(executable_path, ({}, ""))[0]
        diagnostics.extend(_scan_pack(repo_root, path, document, text, executable))

    mirror_root = ecosystem_root / "defaultspack" / "v4" / "packs"
    for path, (document, text) in sorted(documents.items()):
        if path.parent != mirror_root or not path.name.endswith(".pack.v4.json"):
            continue
        if not isinstance(document, dict):
            continue
        pack = document.get("pack")
        pack_id = pack.get("id") if isinstance(pack, dict) else None
        canonical = canonical_packs.get(pack_id) if isinstance(pack_id, str) else None
        if canonical is None or document != canonical[1]:
            continue
        canonical_path = canonical[0].relative_to(repo_root).as_posix()
        digest = hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()
        diagnostics.append(
            _diagnostic(
                "pack-boundary.identical-projection-mirror",
                path.relative_to(repo_root).as_posix(),
                _line_for(text, "id", pack_id),
                {
                    "canonical_path": canonical_path,
                    "content_digest": f"sha256:{digest}",
                    "pack_id": pack_id,
                },
            )
        )

    return sorted(
        diagnostics,
        key=lambda item: (
            item["rule_id"],
            item["path"],
            item["line"],
            item["fingerprint"],
        ),
    )


def _summary(violations: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(item["rule_id"] for item in violations)
    return {
        "by_rule": dict(sorted(counts.items())),
        "total": len(violations),
    }


def _baseline(violations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "baseline_api_version": BASELINE_API_VERSION,
        "policy": POLICY,
        "summary": _summary(violations),
        "violations": violations,
    }


def _report(violations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "report_api_version": REPORT_API_VERSION,
        "summary": _summary(violations),
        "violations": violations,
    }


def _validate_baseline(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["baseline root must be an object"]
    expected_keys = {"baseline_api_version", "policy", "summary", "violations"}
    if set(value) != expected_keys:
        errors.append("baseline keys do not match the v1 schema")
    if value.get("baseline_api_version") != BASELINE_API_VERSION:
        errors.append("baseline_api_version is unsupported")
    if value.get("policy") != POLICY:
        errors.append("baseline policy is unsupported")
    violations = value.get("violations")
    if not isinstance(violations, list):
        return errors + ["violations must be an array"]
    fingerprints: list[str] = []
    for index, item in enumerate(violations):
        if not isinstance(item, dict):
            errors.append(f"violations[{index}] must be an object")
            continue
        if set(item) != {"evidence", "fingerprint", "line", "path", "rule_id"}:
            errors.append(f"violations[{index}] keys are invalid")
            continue
        if not isinstance(item.get("evidence"), dict):
            errors.append(f"violations[{index}].evidence must be an object")
            continue
        if not isinstance(item.get("line"), int) or item["line"] < 1:
            errors.append(f"violations[{index}].line must be a positive integer")
        if not isinstance(item.get("path"), str) or not item["path"]:
            errors.append(f"violations[{index}].path must be a non-empty string")
            continue
        if Path(item["path"]).is_absolute() or ".." in Path(item["path"]).parts:
            errors.append(f"violations[{index}].path must be repository-relative")
        if not isinstance(item.get("rule_id"), str) or not item["rule_id"]:
            errors.append(f"violations[{index}].rule_id must be a non-empty string")
            continue
        expected = _fingerprint(item["rule_id"], item["path"], item["evidence"])
        if item.get("fingerprint") != expected:
            errors.append(f"violations[{index}].fingerprint is invalid")
        fingerprints.append(str(item.get("fingerprint")))
    if len(fingerprints) != len(set(fingerprints)):
        errors.append("baseline fingerprints must be unique")
    if all(isinstance(item, dict) for item in violations):
        if violations != sorted(
            violations,
            key=lambda item: (
                str(item.get("rule_id", "")),
                str(item.get("path", "")),
                item.get("line", 0) if isinstance(item.get("line"), int) else 0,
                str(item.get("fingerprint", "")),
            ),
        ):
            errors.append("baseline violations are not deterministically sorted")
        if all(isinstance(item.get("rule_id"), str) for item in violations):
            if value.get("summary") != _summary(violations):
                errors.append("baseline summary does not match violations")
    return errors


def _load_baseline(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"cannot read baseline: {type(exc).__name__}"]
    errors = _validate_baseline(value)
    return value if isinstance(value, dict) else None, errors


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("scripts/quality/pack_boundary_baseline.json"),
    )
    parser.add_argument("--reference-baseline", type=Path)
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--report-output", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run report, explicit baseline update, or the normal baseline check."""
    args = _parse_args(argv or sys.argv[1:])
    repo_root = args.root.resolve()
    baseline_path = args.baseline
    if not baseline_path.is_absolute():
        baseline_path = repo_root / baseline_path
    violations = scan_repository(repo_root)
    fatal = [item for item in violations if item["rule_id"] in FATAL_RULES]
    report = _report(violations)
    if args.report_output:
        _write_json(args.report_output, report)
    if args.report:
        if not args.report_output:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 1 if fatal else 0
    if fatal:
        print(f"Pack boundary scan failed closed with {len(fatal)} scan diagnostics.")
        return 1
    if args.update_baseline:
        _write_json(baseline_path, _baseline(violations))
        print(f"Updated Pack boundary baseline with {len(violations)} violations.")
        return 0

    baseline, errors = _load_baseline(baseline_path)
    if errors or baseline is None:
        print("Pack boundary baseline is invalid: " + "; ".join(errors))
        return 1
    if args.reference_baseline:
        reference, reference_errors = _load_baseline(args.reference_baseline)
        if reference_errors or reference is None:
            print("Reference baseline is invalid: " + "; ".join(reference_errors))
            return 1
        current_ids = {item["fingerprint"] for item in baseline["violations"]}
        reference_ids = {item["fingerprint"] for item in reference["violations"]}
        additions = current_ids - reference_ids
        if additions:
            print(
                "Pack boundary baseline expansion is forbidden: "
                f"{len(additions)} added fingerprints."
            )
            return 1
    expected = _baseline(violations)
    if baseline != expected:
        baseline_ids = {item["fingerprint"] for item in baseline["violations"]}
        actual_ids = {item["fingerprint"] for item in violations}
        print(
            "Pack boundary baseline mismatch: "
            f"new={len(actual_ids - baseline_ids)} "
            f"stale={len(baseline_ids - actual_ids)}. "
            "Use the explicit baseline update command after review."
        )
        return 1
    print(f"Pack boundary lint passed with {len(violations)} baselined violations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
