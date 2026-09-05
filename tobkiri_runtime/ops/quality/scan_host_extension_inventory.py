#!/usr/bin/env python3
"""Build deterministic, nonauthoritative Host Extension facts."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

RUNTIME_IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_IMPORT_ROOT))

from tobkiri_protocol.errors import SchemaValidationError  # noqa: E402
from tobkiri_protocol.validation import validate_document  # noqa: E402

DEFAULT_SUMMARY = Path("docs/host-extension-inventory.md")
INTENT_SUFFIX = ".profile.intent.v1.json"
PROFILE_SUFFIX = ".profile.v4.json"
SIGNALS = ("ai_runtime_signal", "tool_runtime_signal", "none")
IO_MODULES = (
    ("urllib", "network"),
    ("requests", "network"),
    ("socket", "network"),
    ("ssl", "network"),
    ("subprocess", "process"),
    ("pathlib", "filesystem"),
    ("shutil", "filesystem"),
    ("tempfile", "filesystem"),
    ("sqlite3", "database"),
    ("io", "stream"),
    ("os", "host_os"),
)


def canonical_json(value: Mapping[str, Any]) -> str:
    """Serialize a report deterministically."""

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _diagnose(
    diagnostics: list[dict[str, str]],
    code: str,
    root: Path,
    path: Path,
    detail: str,
    pack_id: str = "",
) -> None:
    diagnostics.append(
        {
            "code": code,
            "detail": detail,
            "pack_id": pack_id,
            "path": _relative(root, path),
        }
    )


def _symlink_component(root: Path, path: Path) -> Path | None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return path if path.is_symlink() else None
    current = root.absolute()
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return current
    return None


def _load_json(
    path: Path,
    root: Path,
    diagnostics: list[dict[str, str]],
    pack_id: str = "",
) -> dict[str, Any] | None:
    if _symlink_component(root, path) is not None:
        _diagnose(diagnostics, "symlink_input", root, path, "not followed", pack_id)
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _diagnose(diagnostics, "missing_input", root, path, "file is missing", pack_id)
        return None
    except json.JSONDecodeError as exc:
        detail = f"line {exc.lineno}, column {exc.colno}"
        _diagnose(diagnostics, "json_parse_error", root, path, detail, pack_id)
        return None
    except (OSError, UnicodeError) as exc:
        _diagnose(
            diagnostics, "input_read_error", root, path, type(exc).__name__, pack_id
        )
        return None
    if not isinstance(value, dict):
        detail = "expected a JSON object"
        _diagnose(diagnostics, "invalid_document_shape", root, path, detail, pack_id)
        return None
    return value


def _schema_valid(
    value: Mapping[str, Any],
    schema: str,
    path: Path,
    root: Path,
    diagnostics: list[dict[str, str]],
    pack_id: str = "",
) -> bool:
    try:
        validate_document(value, schema)
        return True
    except SchemaValidationError as exc:
        details = tuple(exc.diagnostics)
        detail = details[0] if details else str(exc)
        if len(details) > 1:
            detail += f" (+{len(details) - 1} more)"
        _diagnose(
            diagnostics,
            "official_schema_validation_failed",
            root,
            path,
            f"{schema}: {detail}",
            pack_id,
        )
        return False


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item for item in value if isinstance(item, str)})


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        if parent:
            return f"{parent}.{node.attr}"
    return None


def _io_category(name: str) -> str | None:
    for prefix, category in IO_MODULES:
        if name == prefix or name.startswith(f"{prefix}."):
            return category
    return None


def _empty_analysis(status: str) -> dict[str, Any]:
    return {"factory_observed": False, "io_imports": [], "status": status}


def _scan_python(
    path: Path,
    root: Path,
    diagnostics: list[dict[str, str]],
    pack_id: str,
) -> dict[str, Any]:
    if _symlink_component(root, path) is not None:
        _diagnose(
            diagnostics, "symlink_implementation", root, path, "not followed", pack_id
        )
        return _empty_analysis("symlink")
    if path.suffix != ".py":
        code = "unsupported_implementation_language"
        _diagnose(diagnostics, code, root, path, path.suffix or "no suffix", pack_id)
        return _empty_analysis("unsupported")
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _diagnose(diagnostics, "missing_implementation", root, path, "missing", pack_id)
        return _empty_analysis("missing")
    except (OSError, UnicodeError) as exc:
        code = "implementation_read_error"
        _diagnose(diagnostics, code, root, path, type(exc).__name__, pack_id)
        return _empty_analysis("read_error")
    try:
        tree = ast.parse(source, filename=_relative(root, path))
    except SyntaxError as exc:
        detail = f"line {exc.lineno or 0}, column {exc.offset or 0}"
        _diagnose(diagnostics, "python_parse_error", root, path, detail, pack_id)
        return _empty_analysis("parse_error")

    imports: set[tuple[str, str]] = set()
    factory = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                category = _io_category(alias.name)
                if category:
                    imports.add((alias.name, category))
        elif isinstance(node, ast.ImportFrom) and node.module:
            category = _io_category(node.module)
            if category:
                imports.add((node.module, category))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            factory |= any(
                _dotted(target) == "HOST_PROVIDER_FACTORY" for target in targets
            )

    return {
        "status": "parsed",
        "factory_observed": factory,
        "io_imports": [
            {"category": category, "module": module}
            for module, category in sorted(imports)
        ],
    }


def _tracked_profile_paths(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "ecosystem"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        paths = [root / item.decode() for item in result.stdout.split(b"\0") if item]
    except (FileNotFoundError, OSError, subprocess.CalledProcessError):
        paths = list((root / "ecosystem").rglob("*.json"))
    return sorted(
        {
            path
            for path in paths
            if path.name.endswith(INTENT_SUFFIX) or path.name.endswith(PROFILE_SUFFIX)
        },
        key=lambda path: _relative(root, path),
    )


def _profile_key(root: Path, path: Path) -> str:
    relative = _relative(root, path)
    for suffix in (INTENT_SUFFIX, PROFILE_SUFFIX):
        if relative.endswith(suffix):
            return relative[: -len(suffix)]
    return relative


def _graph(
    value: Mapping[str, Any],
) -> tuple[list[dict[str, str]], set[str]]:
    fields = (
        "caller_function_id",
        "contract_id",
        "operation_id",
        "target_provider_id",
    )
    edges = [
        {field: str(item[field]) for field in fields}
        for item in _objects(value.get("requested_edges"))
        if all(isinstance(item.get(field), str) for field in fields)
    ]
    edges.sort(key=lambda item: tuple(item[field] for field in fields))
    callers = {item["caller_function_id"] for item in edges}
    targets = {item["target_provider_id"] for item in edges}
    roots = sorted(callers - targets)
    reachable = set(roots)
    while True:
        discovered = {
            edge["target_provider_id"]
            for edge in edges
            if edge["caller_function_id"] in reachable
        }
        expanded = reachable | discovered
        if expanded == reachable:
            break
        reachable = expanded
    return edges, reachable


def _read_profile(
    root: Path,
    path: Path,
    diagnostics: list[dict[str, str]],
) -> dict[str, Any] | None:
    value = _load_json(path, root, diagnostics)
    if value is None:
        return None
    if value.get("intent_api_version") == "io.tobkiri.profile-intent.v1":
        schema, authority = "profile_intent", "authoritative_intent"
    elif str(value.get("profile_api_version") or "").startswith("io.tobkiri.profile.v"):
        schema = "profile"
        authority = "compatibility_fallback"
    else:
        detail = "unsupported Profile document"
        _diagnose(diagnostics, "unknown_profile_version", root, path, detail)
        return None
    if not _schema_valid(value, schema, path, root, diagnostics):
        return None
    edges, reachable = _graph(value)
    packs = {
        str(item["pack_id"]): str(item.get("role") or "unknown")
        for item in _objects(value.get("packs"))
        if isinstance(item.get("pack_id"), str)
    }
    return {
        "edges": edges,
        "input_authority": authority,
        "packs": packs,
        "profile_id": str(value["profile_id"]),
        "reachable_nodes": reachable,
        "schema_valid": True,
        "source_path": _relative(root, path),
        "status": "analyzed",
    }


def _profiles(
    root: Path,
    diagnostics: list[dict[str, str]],
) -> list[dict[str, Any]]:
    paths = _tracked_profile_paths(root)
    intent_keys = {
        _profile_key(root, path) for path in paths if path.name.endswith(INTENT_SUFFIX)
    }
    selected = [
        path
        for path in paths
        if not (
            path.name.endswith(PROFILE_SUFFIX)
            and _profile_key(root, path) in intent_keys
        )
    ]
    contexts = [
        context
        for path in selected
        if (context := _read_profile(root, path, diagnostics)) is not None
    ]
    counts = Counter(item["profile_id"] for item in contexts)
    for context in contexts:
        if counts[context["profile_id"]] > 1:
            context["status"] = "conflict_excluded"
            path = root / context["source_path"]
            _diagnose(
                diagnostics,
                "duplicate_selected_profile_id",
                root,
                path,
                context["profile_id"],
            )
    contexts.sort(key=lambda item: (item["profile_id"], item["source_path"]))
    return contexts


def _runtime_signal(capabilities: Sequence[str], contracts: Sequence[str]) -> str:
    if any(item.startswith("ai.") for item in capabilities) or any(
        item.startswith("tobkiri.service.ai.") for item in contracts
    ):
        return "ai_runtime_signal"
    if any(item.startswith("tool.") for item in capabilities) or any(
        item.startswith("tobkiri.service.tool.") for item in contracts
    ):
        return "tool_runtime_signal"
    return "none"


def _operations(
    root: Path,
    pack_root: Path,
    pack_id: str,
    manifest: Mapping[str, Any],
    executables: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    diagnostics: list[dict[str, str]],
    analysis_cache: dict[Path, dict[str, Any]],
) -> list[dict[str, Any]]:
    function_by_operation: dict[str, Mapping[str, Any]] = {}
    for function in _objects(manifest.get("functions")):
        for operation_id in _strings(function.get("operations")):
            function_by_operation[operation_id] = function
    variants_by_operation: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for variant in _objects(executables.get("variants")):
        for operation in _objects(variant.get("operations")):
            operation_id = operation.get("operation_id")
            if isinstance(operation_id, str):
                variants_by_operation.setdefault(operation_id, []).append(
                    (variant, operation)
                )

    records: list[dict[str, Any]] = []
    for declaration in _objects(manifest.get("operation_catalog")):
        operation_id = declaration.get("operation_id")
        if not isinstance(operation_id, str):
            continue
        function = function_by_operation.get(operation_id, {})
        provider_id = str(declaration.get("provider_id") or function.get("id") or "")
        reachability = []
        for profile in profiles:
            graph_reachable = any(
                edge["operation_id"] == operation_id
                and edge["target_provider_id"] == provider_id
                and edge["caller_function_id"] in profile["reachable_nodes"]
                for edge in profile["edges"]
            )
            included = pack_id in profile["packs"]
            reachability.append(
                {
                    "graph_reachable": graph_reachable,
                    "included_pack": included,
                    "profile_id": profile["profile_id"],
                    "reachable": (
                        profile["status"] == "analyzed" and included and graph_reachable
                    ),
                    "source_path": profile["source_path"],
                }
            )
        implementations = []
        for variant, _operation in sorted(
            variants_by_operation.get(operation_id, []),
            key=lambda pair: str(pair[0].get("variant_id") or ""),
        ):
            raw_path = variant.get("implementation_path")
            if not isinstance(raw_path, str):
                continue
            pure_path = PurePosixPath(raw_path)
            if pure_path.is_absolute() or ".." in pure_path.parts:
                path = pack_root / "executables.v4.json"
                _diagnose(
                    diagnostics,
                    "unsafe_implementation_path",
                    root,
                    path,
                    raw_path,
                    pack_id,
                )
                continue
            path = pack_root.joinpath(*pure_path.parts)
            if path not in analysis_cache:
                analysis_cache[path] = _scan_python(path, root, diagnostics, pack_id)
            implementations.append(
                {
                    **analysis_cache[path],
                    "path": _relative(root, path),
                    "variant_id": str(variant.get("variant_id") or ""),
                }
            )
        if not implementations:
            path = pack_root / "executables.v4.json"
            _diagnose(
                diagnostics,
                "operation_has_no_executable",
                root,
                path,
                operation_id,
                pack_id,
            )
        records.append(
            {
                "contract_id": str(declaration.get("contract_reference") or ""),
                "implementations": implementations,
                "operation_id": operation_id,
                "profile_reachability": reachability,
                "provider_id": provider_id,
                "reachable": any(item["reachable"] for item in reachability),
            }
        )
    return sorted(records, key=lambda item: item["operation_id"])


def _manual_reasons(
    signal: str,
    schemas_valid: bool,
    operations: Sequence[Mapping[str, Any]],
    pack_has_diagnostics: bool,
) -> list[str]:
    reasons: list[str] = []
    if signal != "none":
        reasons.append("runtime_signal_requires_human_review")
    if not schemas_valid:
        reasons.append("official_schema_validation_failed")
    if pack_has_diagnostics:
        reasons.append("scanner_diagnostic_requires_review")
    if signal == "none":
        return sorted(set(reasons))
    if not any(operation["reachable"] for operation in operations):
        reasons.append("no_tracked_profile_reachable_operation")
    if any(
        item["graph_reachable"] and not item["included_pack"]
        for operation in operations
        for item in operation["profile_reachability"]
    ):
        reasons.append("reachable_edge_without_explicit_pack_inclusion")
    implementations = [
        implementation
        for operation in operations
        for implementation in operation["implementations"]
    ]
    if not implementations:
        reasons.append("no_resolved_implementation")
    elif not all(item["status"] == "parsed" for item in implementations):
        reasons.append("implementation_analysis_incomplete")
    return sorted(set(reasons))


def build_inventory(
    runtime_root: Path,
) -> dict[str, Any]:
    """Collect facts without executing Pack code or changing runtime state."""

    root = runtime_root.resolve()
    diagnostics: list[dict[str, str]] = []
    profiles = _profiles(root, diagnostics)
    manifests: list[tuple[Path, dict[str, Any], bool]] = []
    for pack_root in sorted((root / "ecosystem").iterdir(), key=lambda path: path.name):
        manifest_path = pack_root / "pack.v4.json"
        if not manifest_path.exists() and not manifest_path.is_symlink():
            continue
        manifest = _load_json(manifest_path, root, diagnostics)
        if manifest is None:
            continue
        pack = manifest.get("pack")
        if not isinstance(pack, Mapping) or pack.get("kind") != "host_extension":
            continue
        pack_id = pack.get("id")
        if not isinstance(pack_id, str):
            continue
        valid = _schema_valid(
            manifest,
            "pack",
            manifest_path,
            root,
            diagnostics,
            pack_id,
        )
        manifests.append((pack_root, manifest, valid))

    records = []
    analysis_cache: dict[Path, dict[str, Any]] = {}
    for pack_root, manifest, manifest_valid in sorted(
        manifests, key=lambda item: str(item[1]["pack"]["id"])
    ):
        pack = manifest["pack"]
        pack_id = str(pack["id"])
        executable_path = pack_root / "executables.v4.json"
        executables = _load_json(executable_path, root, diagnostics, pack_id) or {}
        executable_valid = bool(executables) and _schema_valid(
            executables,
            "executable_catalog",
            executable_path,
            root,
            diagnostics,
            pack_id,
        )
        if executables.get("pack_id") != pack_id:
            executable_valid = False
            _diagnose(
                diagnostics,
                "executable_pack_id_mismatch",
                root,
                executable_path,
                str(executables.get("pack_id")),
                pack_id,
            )
        operations = _operations(
            root,
            pack_root,
            pack_id,
            manifest,
            executables,
            profiles,
            diagnostics,
            analysis_cache,
        )
        requirements = manifest.get("requirements")
        requirements = requirements if isinstance(requirements, Mapping) else {}
        capabilities = _strings(requirements.get("capabilities"))
        contracts = sorted(
            {
                operation["contract_id"]
                for operation in operations
                if operation["contract_id"]
            }
        )
        signal = _runtime_signal(capabilities, contracts)
        pack_has_diagnostics = any(item["pack_id"] == pack_id for item in diagnostics)
        records.append(
            {
                "manual_review_reasons": _manual_reasons(
                    signal,
                    manifest_valid and executable_valid,
                    operations,
                    pack_has_diagnostics,
                ),
                "operations": operations,
                "pack_id": pack_id,
                "runtime_signal": signal,
                "schema_validity": {
                    "executable_catalog": executable_valid,
                    "pack_manifest": manifest_valid,
                },
            }
        )

    diagnostics = sorted(
        {tuple(sorted(item.items())): item for item in diagnostics}.values(),
        key=lambda item: (item["code"], item["path"], item["pack_id"]),
    )
    signal_counts = Counter(item["runtime_signal"] for item in records)
    operation_count = sum(len(item["operations"]) for item in records)
    return {
        "diagnostics": diagnostics,
        "method": {"authority": "none_report_only"},
        "profiles": [
            {
                "edge_count": len(item["edges"]),
                "input_authority": item["input_authority"],
                "pack_count": len(item["packs"]),
                "profile_id": item["profile_id"],
                "schema_valid": item["schema_valid"],
                "source_path": item["source_path"],
                "status": item["status"],
            }
            for item in profiles
        ],
        "records": records,
        "summary": {
            "diagnostic_count": len(diagnostics),
            "manual_review_pack_count": sum(
                bool(item["manual_review_reasons"]) for item in records
            ),
            "operation_count": operation_count,
            "pack_count": len(records),
            "runtime_signals": {
                signal: signal_counts.get(signal, 0) for signal in SIGNALS
            },
            "tracked_profile_count": len(profiles),
            "tracked_profile_reachable_operation_count": sum(
                operation["reachable"]
                for record in records
                for operation in record["operations"]
            ),
            "tracked_profile_reachable_pack_count": sum(
                any(operation["reachable"] for operation in record["operations"])
                for record in records
            ),
        },
    }


def render_summary(report: Mapping[str, Any]) -> str:
    """Render the compact tracked artifact."""

    summary = report["summary"]
    signals = summary["runtime_signals"]
    lines = [
        "# Host Extension inventory",
        "",
        "Nonauthoritative, read-only facts. This report never grants runtime admission.",
        "",
        "## Totals",
        "",
        f"- Packs: {summary['pack_count']}",
        f"- Operations: {summary['operation_count']}",
        f"- Tracked Profiles: {summary['tracked_profile_count']}",
        f"- Tracked-Profile-reachable packs: {summary['tracked_profile_reachable_pack_count']}",
        f"- Tracked-Profile-reachable operations: {summary['tracked_profile_reachable_operation_count']}",
        f"- AI Runtime signals: {signals['ai_runtime_signal']}",
        f"- Tool Runtime signals: {signals['tool_runtime_signal']}",
        f"- No AI/Tool Runtime signal: {signals['none']}",
        f"- Manual-review packs: {summary['manual_review_pack_count']}",
        f"- Diagnostics: {summary['diagnostic_count']}",
        "",
        "## Profile inputs",
        "",
        "| Profile | Authority | Schema | Packs | Edges | Source |",
        "|---|---|---|---:|---:|---|",
    ]
    for profile in report["profiles"]:
        lines.append(
            f"| `{profile['profile_id']}` | {profile['input_authority']} | "
            f"{'valid' if profile['schema_valid'] else 'invalid'} | "
            f"{profile['pack_count']} | {profile['edge_count']} | "
            f"`{profile['source_path']}` |"
        )
    lines.extend(
        [
            "",
            "## Pack facts",
            "",
            "| Pack | Ops | Runtime signal | Reachable | Schema | Manual-review reasons |",
            "|---|---:|---|---:|---|---|",
        ]
    )
    for record in report["records"]:
        reachable = sum(item["reachable"] for item in record["operations"])
        schema = record["schema_validity"]
        schema_text = "valid" if all(schema.values()) else "invalid"
        reasons = ", ".join(record["manual_review_reasons"]) or "-"
        lines.append(
            f"| `{record['pack_id']}` | {len(record['operations'])} | "
            f"{record['runtime_signal']} | {reachable} | {schema_text} | {reasons} |"
        )
    lines.extend(["", "## Diagnostics", ""])
    groups = Counter(item["code"] for item in report["diagnostics"])
    if groups:
        lines.extend(f"- `{code}`: {count}" for code, count in sorted(groups.items()))
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Limits",
            "",
            "- Official schema validity is input validity, not runtime admission.",
            "- Profile reachability requires explicit Pack inclusion but is not activation proof.",
            "- AST I/O and `HOST_PROVIDER_FACTORY` observations are advisory only.",
            "- Dynamic imports, reflection, native code, and runtime-added edges may be missed.",
        ]
    )
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--format", choices=("json", "summary"), default="summary")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the inventory CLI."""

    args = _parser().parse_args(argv)
    root = args.runtime_root.resolve()
    report = build_inventory(root)
    json_text = canonical_json(report)
    summary_text = render_summary(report)
    summary_path = args.summary_output or root / DEFAULT_SUMMARY
    outputs = [(summary_path, summary_text)]
    if args.json_output:
        outputs.append((args.json_output, json_text))
    if args.write:
        for path, text in outputs:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        print(
            "host-extension-inventory: wrote compact facts for "
            f"{report['summary']['pack_count']} packs, "
            f"{report['summary']['operation_count']} operations"
        )
        return 0
    if args.check:
        stale = []
        for path, expected in outputs:
            try:
                current = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                current = ""
            if current != expected:
                stale.append(str(path))
        if stale:
            print(
                "host-extension-inventory: stale: " + ", ".join(stale), file=sys.stderr
            )
            return 1
        print(
            "host-extension-inventory: current "
            f"({report['summary']['pack_count']} packs, "
            f"{report['summary']['diagnostic_count']} diagnostics)"
        )
        return 0
    sys.stdout.write(json_text if args.format == "json" else summary_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
