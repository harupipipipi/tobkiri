#!/usr/bin/env python3
"""Scan repository pack boundaries as stable source-to-target edges."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".dart"}
IMPORT_RE = re.compile(
    r"(?:import|export)\s+(?:[^'\"]+?\s+from\s+)?['\"]([^'\"]+)['\"]|"
    r"import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
)
PACK_PATH_RE = re.compile(r"(?:^|/)ecosystem/([a-zA-Z0-9_.-]+)(?:/|$)")
API_LITERAL_RE = re.compile(r"['\"](/api/(?!contracts/)[^'\"]*)['\"]")
PACK_DISCOVERY_RE = re.compile(
    r"(?:glob|rglob|iterdir)\s*\([^\n)]*\)|(?:all_installed|installed_packs)",
    re.IGNORECASE,
)
SECRET_INJECTION_RE = re.compile(
    r"(?:os\.environ|process\.env|Platform\.environment).{0,100}"
    r"(?:api[_-]?key|token|secret|credential)",
    re.IGNORECASE | re.DOTALL,
)
KERNEL_DOMAIN_RE = re.compile(
    r"(?:pack_id|domain|feature|product)\s*(?:==|!=|in)\s*"
    r"(?:['\"]|\{)[^\n]{0,100}",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class Violation:
    """One stable architecture edge violation."""

    rule: str
    path: str
    line: int
    source: str
    target: str
    guidance: str

    @property
    def identity(self) -> str:
        """Return the exact shrink-only baseline identity."""
        return f"{self.rule}|{self.path}|{self.line}|{self.source}|{self.target}"


class BaselineError(ValueError):
    """Raised when an exception baseline is missing or can broaden silently."""


def discover_pack_roots(ecosystem_dir: Path) -> dict[str, Path]:
    """Return manifest-backed pack roots without importing pack code."""
    roots: dict[str, Path] = {}
    if not ecosystem_dir.is_dir():
        return roots
    for manifest in sorted(ecosystem_dir.glob("*/ecosystem.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pack_id = str(payload.get("id") or manifest.parent.name).strip()
        if pack_id:
            roots[pack_id] = manifest.parent.resolve()
    return roots


def load_baseline(path: Path) -> dict[str, dict[str, Any]]:
    """Load a strict exact-edge exception baseline."""
    if not path.is_file():
        raise BaselineError(f"baseline is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"baseline is unreadable: {exc}") from exc
    if (
        payload.get("schema_version") != 1
        or payload.get("policy") != "shrink_only_exact_edges"
        or not isinstance(payload.get("exceptions"), list)
    ):
        raise BaselineError(
            "baseline must use schema_version 1, shrink_only_exact_edges, "
            "and exceptions[]"
        )
    required = {
        "identity",
        "rule",
        "path",
        "line",
        "source",
        "target",
        "owner",
        "reason",
        "introduced_at",
        "removal_wave",
        "sunset_at",
        "violation_category",
    }
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(payload["exceptions"]):
        if not isinstance(item, dict) or not required <= set(item):
            raise BaselineError(f"baseline exception {index} lacks required metadata")
        for field in required - {"line", "removal_wave"}:
            value = str(item[field]).strip()
            if not value or any(marker in value for marker in ("*", "?", "[", "]")):
                raise BaselineError(
                    f"baseline exception {index} has broad {field}: {value!r}"
                )
        identity = str(item["identity"])
        expected = (
            f"{item['rule']}|{item['path']}|{item['line']}|"
            f"{item['source']}|{item['target']}"
        )
        if identity != expected or identity in result:
            raise BaselineError(f"invalid or duplicate baseline identity: {identity}")
        if not isinstance(item["line"], int) or item["line"] < 1:
            raise BaselineError(f"invalid line for {identity}")
        if not isinstance(item["removal_wave"], int) or not (
            0 <= item["removal_wave"] <= 10
        ):
            raise BaselineError(f"invalid removal_wave for {identity}")
        if str(item["violation_category"]) != str(item["rule"]):
            raise BaselineError(
                f"violation_category must equal rule for {identity}"
            )
        for field in ("introduced_at", "sunset_at"):
            try:
                dt.date.fromisoformat(str(item[field]))
            except ValueError as exc:
                raise BaselineError(
                    f"{field} must be an ISO date for {identity}"
                ) from exc
        result[identity] = item
    return result


def verify_shrink_only_baseline(
    baseline: dict[str, dict[str, Any]],
    reference: dict[str, dict[str, Any]],
) -> None:
    """Reject new or mutated exceptions relative to an approved baseline."""
    additions = sorted(set(baseline) - set(reference))
    if additions:
        raise BaselineError(
            "shrink-only baseline contains new identities: " + ", ".join(additions)
        )
    for identity, item in baseline.items():
        if item != reference[identity]:
            raise BaselineError(
                f"shrink-only baseline metadata changed for {identity}"
            )


def scan_repository(root: Path) -> list[Violation]:
    """Scan all supported source files and return deterministic violations."""
    ecosystem = root / "tobkiri_runtime" / "ecosystem"
    pack_roots = discover_pack_roots(ecosystem)
    pack_names = set(pack_roots)
    violations: set[Violation] = set()
    violations.update(_scan_manifest_graph(root, pack_roots))
    for path in _source_files(root):
        source_pack = _owning_pack(path, pack_roots)
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            violations.update(
                _scan_python(root, path, text, source_pack, pack_names, pack_roots)
            )
        else:
            violations.update(
                _scan_script_imports(root, path, text, source_pack, pack_roots)
            )
        violations.update(
            _scan_literal_paths(root, path, text, source_pack, pack_names)
        )
        violations.update(_scan_runtime_policy(root, path, text, source_pack))
        if (
            source_pack
            and source_pack != "defaultspack"
            and "/webapp/" in path.as_posix()
        ):
            for match in API_LITERAL_RE.finditer(text):
                violations.add(
                    _violation(
                        root,
                        path,
                        text,
                        match.start(),
                        "direct_implementation_route",
                        source_pack,
                        match.group(1),
                        "Consume a global action or data-source contract.",
                    )
                )
    return sorted(violations)


def _source_files(root: Path) -> Iterable[Path]:
    ignored = {
        ".git",
        "node_modules",
        "target",
        "dist",
        "build",
        "assets",
        "__pycache__",
    }
    for path in root.rglob("*"):
        if path.suffix not in SOURCE_SUFFIXES or any(
            part in ignored for part in path.parts
        ):
            continue
        if "/tests/" in path.as_posix() or "/fixtures/" in path.as_posix():
            continue
        yield path


def _scan_manifest_graph(
    root: Path, pack_roots: dict[str, Path]
) -> set[Violation]:
    """Validate the complete declared pack dependency graph."""
    found: set[Violation] = set()
    known = set(pack_roots)
    for pack_id, pack_root in pack_roots.items():
        manifest = pack_root / "ecosystem.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        dependencies = payload.get("dependencies", [])
        if isinstance(dependencies, dict):
            dependencies = list(dependencies)
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if isinstance(dependency, dict):
                target = str(
                    dependency.get("pack_id") or dependency.get("id") or ""
                ).strip()
            else:
                target = str(dependency).strip()
            if target and target not in known:
                found.add(
                    _line_violation(
                        root,
                        manifest,
                        1,
                        "unknown_manifest_dependency",
                        pack_id,
                        target,
                    )
                )
    return found


def _owning_pack(path: Path, roots: dict[str, Path]) -> str:
    resolved = path.resolve()
    for pack_id, root in roots.items():
        if resolved.is_relative_to(root):
            return pack_id
    return "kernel" if "core_runtime" in path.parts else "host"


def _scan_python(
    root: Path,
    path: Path,
    text: str,
    source_pack: str,
    pack_names: set[str],
    pack_roots: dict[str, Path],
) -> set[Violation]:
    found: set[Violation] = set()
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return found
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
            if node.level:
                target_path = (
                    path.parent / Path(*([".."] * (node.level - 1)))
                ).resolve()
                target_pack = _owning_pack(target_path, pack_roots)
                if target_pack != source_pack:
                    found.add(
                        _line_violation(
                            root,
                            path,
                            node.lineno,
                            "cross_pack_import",
                            source_pack,
                            target_pack,
                        )
                    )
        elif isinstance(node, ast.Call):
            function = ast.unparse(node.func)
            if function in {"importlib.import_module", "__import__"} and node.args:
                value = _literal_string(node.args[0])
                if value:
                    modules = [value]
        for module in modules:
            target_pack = _module_pack(module, pack_names)
            if target_pack and target_pack != source_pack:
                found.add(
                    _line_violation(
                        root,
                        path,
                        node.lineno,
                        "cross_pack_import",
                        source_pack,
                        target_pack,
                    )
                )
        if isinstance(node, ast.Compare) and "pack_id" in ast.unparse(node.left):
            for comparator in node.comparators:
                target = _literal_string(comparator)
                if target in pack_names and target != source_pack:
                    found.add(
                        _line_violation(
                            root,
                            path,
                            node.lineno,
                            "foreign_pack_id_branch",
                            source_pack,
                            target,
                        )
                    )
    return found


def _scan_script_imports(
    root: Path,
    path: Path,
    text: str,
    source_pack: str,
    pack_roots: dict[str, Path],
) -> set[Violation]:
    found: set[Violation] = set()
    for match in IMPORT_RE.finditer(text):
        specifier = match.group(1) or match.group(2) or ""
        if not specifier.startswith("."):
            continue
        target_path = (path.parent / specifier).resolve()
        target_pack = _owning_pack(target_path, pack_roots)
        if target_pack != source_pack:
            found.add(
                _violation(
                    root,
                    path,
                    text,
                    match.start(),
                    "cross_pack_import",
                    source_pack,
                    target_pack,
                    "Import a generated global-contract binding instead.",
                )
            )
    return found


def _scan_literal_paths(
    root: Path,
    path: Path,
    text: str,
    source_pack: str,
    pack_names: set[str],
) -> set[Violation]:
    found: set[Violation] = set()
    for match in PACK_PATH_RE.finditer(text.replace("\\", "/")):
        target = match.group(1)
        if target in pack_names and target != source_pack:
            found.add(
                _violation(
                    root,
                    path,
                    text,
                    match.start(),
                    "sibling_pack_path",
                    source_pack,
                    target,
                    "Resolve the resource through a global contract handle.",
                )
            )
    return found


def _scan_runtime_policy(
    root: Path, path: Path, text: str, source_pack: str
) -> set[Violation]:
    """Detect discovery, secret, and kernel domain-policy bypasses."""
    found: set[Violation] = set()
    normalized = path.as_posix()
    discovery_surface = any(
        marker in normalized.lower()
        for marker in ("pack", "profile", "discover", "manifest", "startup")
    )
    if source_pack == "kernel" and discovery_surface:
        for match in PACK_DISCOVERY_RE.finditer(text):
            found.add(
                _violation(
                    root,
                    path,
                    text,
                    match.start(),
                    "unscoped_pack_discovery",
                    source_pack,
                    "all-installed-packs",
                    "Discover only the resolved profile effective pack set.",
                )
            )
        for match in KERNEL_DOMAIN_RE.finditer(text):
            found.add(
                _violation(
                    root,
                    path,
                    text,
                    match.start(),
                    "kernel_domain_branch",
                    source_pack,
                    match.group(0).strip(),
                    "Resolve behavior through a typed global contract.",
                )
            )
    if "provider_compiler" not in normalized and "secret" not in normalized:
        for match in SECRET_INJECTION_RE.finditer(text):
            found.add(
                _violation(
                    root,
                    path,
                    text,
                    match.start(),
                    "unscoped_global_secret",
                    source_pack,
                    "process-environment",
                    "Request a profile- and provider-scoped credential handle.",
                )
            )
    return found


def _module_pack(module: str, pack_names: set[str]) -> str | None:
    parts = module.split(".")
    for part in parts:
        if part in pack_names:
            return part
    return None


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _line_violation(
    root: Path,
    path: Path,
    line: int,
    rule: str,
    source: str,
    target: str,
) -> Violation:
    return Violation(
        rule,
        path.relative_to(root).as_posix(),
        line,
        source,
        target,
        "Replace the concrete edge with a typed global contract.",
    )


def _violation(
    root: Path,
    path: Path,
    text: str,
    offset: int,
    rule: str,
    source: str,
    target: str,
    guidance: str,
) -> Violation:
    return Violation(
        rule,
        path.relative_to(root).as_posix(),
        text.count("\n", 0, offset) + 1,
        source,
        target,
        guidance,
    )


def _sarif(violations: list[Violation]) -> dict[str, Any]:
    rules = sorted({item.rule for item in violations})
    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "Rumi Pack Architecture",
                        "rules": [{"id": rule} for rule in rules],
                    }
                },
                "results": [
                    {
                        "ruleId": item.rule,
                        "message": {
                            "text": (
                                f"{item.source} -> {item.target}. {item.guidance}"
                            )
                        },
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": item.path},
                                    "region": {"startLine": item.line},
                                }
                            }
                        ],
                    }
                    for item in violations
                ],
            }
        ],
    }


def main() -> int:
    """Run the repository scan and fail on non-baselined exact edges."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[3])
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--reference-baseline", type=Path)
    parser.add_argument("--format", choices=("text", "json", "sarif"), default="text")
    args = parser.parse_args()
    root = args.root.resolve()
    baseline_path = args.baseline or (
        root / "scripts" / "quality" / "pack_architecture_baseline.json"
    )
    try:
        baseline = load_baseline(baseline_path)
        if args.reference_baseline:
            reference = load_baseline(args.reference_baseline)
            verify_shrink_only_baseline(baseline, reference)
    except BaselineError as exc:
        print(f"pack-architecture: {exc}", file=sys.stderr)
        return 2
    violations = scan_repository(root)
    active = [item for item in violations if item.identity not in baseline]
    if args.format == "json":
        print(
            json.dumps(
                {
                    "violations": [
                        asdict(item) | {"identity": item.identity}
                        for item in active
                    ]
                },
                indent=2,
            )
        )
    elif args.format == "sarif":
        print(json.dumps(_sarif(active), indent=2))
    else:
        for item in active:
            print(
                f"{item.path}:{item.line}: {item.rule}: "
                f"{item.source} -> {item.target}; {item.guidance}"
            )
        print(
            f"pack-architecture: detected={len(violations)} "
            f"baselined={len(violations) - len(active)} new={len(active)}"
        )
    return 1 if active else 0


if __name__ == "__main__":
    raise SystemExit(main())
