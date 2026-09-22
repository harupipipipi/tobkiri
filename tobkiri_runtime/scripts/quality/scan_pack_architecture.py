#!/usr/bin/env python3
"""Scan repository pack boundaries as stable source-to-target edges."""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".dart"}
IGNORED_SOURCE_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".next",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "assets",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "target",
        "venv",
    }
)
IGNORED_SOURCE_DIRECTORY_PATHS = (
    Path("tobkiri_launcher") / "src-tauri" / "gen",
)
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
    """One architecture violation with a stable semantic fingerprint."""

    rule: str
    path: str
    line: int
    source: str
    target: str
    guidance: str
    fingerprint: str = ""

    @property
    def identity(self) -> str:
        """Return the line-independent shrink-only baseline identity."""
        return (
            f"{self.rule}|{self.path}|{self.fingerprint}|"
            f"{self.source}|{self.target}"
        )


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
        payload.get("schema_version") != 2
        or payload.get("policy") != "shrink_only_exact_edges"
        or not isinstance(payload.get("exceptions"), list)
    ):
        raise BaselineError(
            "baseline must use schema_version 2, shrink_only_exact_edges, "
            "and exceptions[]"
        )
    required = {
        "identity",
        "rule",
        "path",
        "line",
        "fingerprint",
        "source",
        "target",
        "owner",
        "reason",
        "introduced_at",
        "fix_by_wave",
        "sunset_at",
        "violation_category",
    }
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(payload["exceptions"]):
        if not isinstance(item, dict) or not required <= set(item):
            raise BaselineError(f"baseline exception {index} lacks required metadata")
        for field in required - {"line", "fix_by_wave"}:
            value = str(item[field]).strip()
            if not value or any(marker in value for marker in ("*", "?", "[", "]")):
                raise BaselineError(
                    f"baseline exception {index} has broad {field}: {value!r}"
                )
        identity = str(item["identity"])
        expected = (
            f"{item['rule']}|{item['path']}|{item['fingerprint']}|"
            f"{item['source']}|{item['target']}"
        )
        if identity != expected or identity in result:
            raise BaselineError(f"invalid or duplicate baseline identity: {identity}")
        if not isinstance(item["line"], int) or item["line"] < 1:
            raise BaselineError(f"invalid line for {identity}")
        if not isinstance(item["fix_by_wave"], int) or item["fix_by_wave"] < 1:
            raise BaselineError(f"invalid fix_by_wave for {identity}")
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
    """Reject new or changed exceptions while allowing line relocation."""
    additions = sorted(set(baseline) - set(reference))
    if additions:
        raise BaselineError(
            "shrink-only baseline contains new identities: "
            + ", ".join(additions)
        )
    for identity, item in baseline.items():
        if _relocation_stable_metadata(item) != _relocation_stable_metadata(
            reference[identity]
        ):
            raise BaselineError(
                f"shrink-only baseline metadata changed for {identity}"
            )


def find_unbaselined_violations(
    violations: Iterable[Violation],
    baseline: dict[str, dict[str, Any]],
) -> list[Violation]:
    """Return violations absent from the exact semantic baseline."""
    return [item for item in violations if item.identity not in baseline]


def find_stale_baseline_exceptions(
    violations: Iterable[Violation],
    baseline: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return exceptions whose exact semantic edge no longer exists."""
    active_identities = {item.identity for item in violations}
    return [
        item
        for identity, item in baseline.items()
        if identity not in active_identities
    ]


def _relocation_stable_metadata(item: dict[str, Any]) -> str:
    return json.dumps(
        {
            key: value
            for key, value in item.items()
            if key not in {"identity", "line"}
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def find_expired_baseline_exceptions(
    baseline: dict[str, dict[str, Any]], *, today: dt.date
) -> list[dict[str, Any]]:
    """Return exceptions whose sunset date has passed."""
    return [
        item
        for item in baseline.values()
        if dt.date.fromisoformat(str(item["sunset_at"])) < today
    ]


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
                        match.group(0),
                    )
                )
    return _disambiguate_fingerprints(violations)


def _source_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.suffix not in SOURCE_SUFFIXES:
            continue
        if _is_ignored_source_path(root, path):
            continue
        if "/tests/" in path.as_posix() or "/fixtures/" in path.as_posix():
            continue
        yield path


def _is_ignored_source_path(root: Path, path: Path) -> bool:
    """Return whether a source belongs to an explicit dependency/generated tree."""
    relative = path.relative_to(root)
    directory_parts = relative.parts[:-1]
    if any(
        part in IGNORED_SOURCE_DIRECTORY_NAMES
        for part in directory_parts
    ):
        return True
    return any(
        relative.is_relative_to(ignored_path)
        for ignored_path in IGNORED_SOURCE_DIRECTORY_PATHS
    )


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
                        _value_fingerprint(
                            "manifest-v1",
                            json.dumps(
                                {"pack": pack_id, "dependency": dependency},
                                sort_keys=True,
                            ),
                        ),
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
                            _ast_fingerprint(node),
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
                        _ast_fingerprint(node),
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
                            _ast_fingerprint(node),
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
                    match.group(0),
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
                    match.group(0),
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
                    match.group(0),
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
                    match.group(0),
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
                    match.group(0),
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


def _ast_fingerprint(node: ast.AST) -> str:
    """Return a location-independent Python AST fingerprint for one edge."""
    try:
        dumped = ast.dump(
            node,
            annotate_fields=True,
            include_attributes=False,
            show_empty=False,
        )
    except TypeError:
        # Python 3.13 added ``show_empty`` and stopped rendering empty list
        # fields by default. Normalize older runtimes to that representation
        # so exact-edge identities remain stable across the supported CI
        # Python versions.
        dumped = ast.dump(
            node,
            annotate_fields=True,
            include_attributes=False,
        )
        previous = None
        while dumped != previous:
            previous = dumped
            dumped = re.sub(r"(?<=\()\w+=\[\], ", "", dumped)
            dumped = re.sub(r", \w+=\[\](?=[,)])", "", dumped)
    return _value_fingerprint(
        "ast-v1",
        dumped,
    )


def _text_fingerprint(text: str, offset: int, matched_text: str) -> str:
    """Return a location-independent fingerprint for a non-Python edge."""
    line_start = text.rfind("\n", 0, offset) + 1
    line_end = text.find("\n", offset)
    if line_end < 0:
        line_end = len(text)
    statement = text[line_start:line_end]
    normalized = re.sub(r"\s+", "", statement)
    if not normalized:
        normalized = re.sub(r"\s+", "", matched_text)
    return _value_fingerprint("text-v1", normalized)


def _value_fingerprint(kind: str, value: str) -> str:
    """Return a short, namespaced SHA-256 fingerprint for stable identities."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"{kind}:{digest}"


def _disambiguate_fingerprints(
    violations: Iterable[Violation],
) -> list[Violation]:
    """Disambiguate repeated identical edges without reintroducing line IDs."""
    grouped: dict[str, list[Violation]] = {}
    for item in violations:
        grouped.setdefault(item.identity, []).append(item)
    result: list[Violation] = []
    for items in grouped.values():
        if len(items) == 1:
            result.extend(items)
            continue
        for occurrence, item in enumerate(sorted(items), start=1):
            result.append(
                replace(
                    item,
                    fingerprint=(
                        f"{item.fingerprint}:occurrence-v1-{occurrence}"
                    ),
                )
            )
    return sorted(result)


def _line_violation(
    root: Path,
    path: Path,
    line: int,
    rule: str,
    source: str,
    target: str,
    fingerprint: str,
) -> Violation:
    return Violation(
        rule,
        path.relative_to(root).as_posix(),
        line,
        source,
        target,
        "Replace the concrete edge with a typed global contract.",
        fingerprint,
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
    matched_text: str,
) -> Violation:
    return Violation(
        rule,
        path.relative_to(root).as_posix(),
        text.count("\n", 0, offset) + 1,
        source,
        target,
        guidance,
        _text_fingerprint(text, offset, matched_text),
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
    parser.add_argument(
        "--today",
        type=dt.date.fromisoformat,
        default=dt.date.today(),
        help="ISO date used to enforce exception sunset dates (default: today)",
    )
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
    expired = find_expired_baseline_exceptions(baseline, today=args.today)
    if expired:
        identities = ", ".join(str(item["identity"]) for item in expired)
        print(
            "pack-architecture: baseline contains expired exceptions; "
            f"remove or renew them through review: {identities}",
            file=sys.stderr,
        )
        return 2
    violations = scan_repository(root)
    stale = find_stale_baseline_exceptions(violations, baseline)
    if stale:
        identities = ", ".join(str(item["identity"]) for item in stale)
        print(
            "pack-architecture: baseline contains resolved identities; "
            f"remove them to preserve shrink-only enforcement: {identities}",
            file=sys.stderr,
        )
        return 2
    active = find_unbaselined_violations(violations, baseline)
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
