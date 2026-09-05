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
from typing import Any, Iterable, Mapping

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
IGNORED_SOURCE_DIRECTORY_PATHS = (Path("tobkiri_launcher") / "src-tauri" / "gen",)
APPLICATION_COMPOSITION_ROOT = Path("tobkiri_runtime") / "app.py"
PRODUCT_SPECIAL_CASE_EXCLUDED_PATHS = frozenset(
    {
        APPLICATION_COMPOSITION_ROOT,
        Path("scripts") / "quality" / "check_core_no_favoritism.py",
    }
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
        return f"{self.rule}|{self.path}|{self.fingerprint}|{self.source}|{self.target}"


class BaselineError(ValueError):
    """Raised when an exception baseline is missing or can broaden silently."""


class PackCatalogError(ValueError):
    """Raised when the canonical Pack inventory cannot be trusted."""


PACK_CATALOG_API_VERSION = "io.tobkiri.pack-source-catalog.v1"


def _pack_catalog_path(ecosystem_dir: Path) -> Path:
    """Return the canonical Pack catalog beside an ecosystem directory."""
    return ecosystem_dir.parent / "schemas" / "pack_v4_catalog.v1.json"


def _load_pack_catalog(ecosystem_dir: Path) -> dict[str, Any]:
    """Load and minimally validate the v4 Pack inventory.

    The scanner intentionally does not discover authority from legacy
    ``ecosystem.json`` files.  Those files can still be inspected by an
    explicitly offline migration tool, but they are not an architecture-scan
    input.
    """
    path = _pack_catalog_path(ecosystem_dir)
    if not path.is_file():
        raise PackCatalogError(f"canonical v4 Pack catalog is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackCatalogError(f"canonical v4 Pack catalog is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise PackCatalogError("canonical v4 Pack catalog must be an object")
    if payload.get("catalog_api_version") != PACK_CATALOG_API_VERSION:
        raise PackCatalogError("unknown canonical v4 Pack catalog version")
    pack_ids = payload.get("pack_ids")
    records = payload.get("packs")
    if not isinstance(pack_ids, list) or not isinstance(records, list):
        raise PackCatalogError("canonical v4 Pack catalog inventory is malformed")
    if any(not isinstance(pack_id, str) or not pack_id.strip() for pack_id in pack_ids):
        raise PackCatalogError("canonical v4 Pack catalog contains an invalid Pack ID")
    if pack_ids != sorted(pack_ids) or len(set(pack_ids)) != len(pack_ids):
        raise PackCatalogError("canonical v4 Pack catalog Pack IDs are not unique and sorted")
    record_ids: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("pack_id"), str):
            raise PackCatalogError("canonical v4 Pack catalog contains a malformed record")
        record_ids.append(record["pack_id"])
    if record_ids != pack_ids:
        raise PackCatalogError("canonical v4 Pack catalog IDs and records disagree")
    return payload


def discover_pack_roots(ecosystem_dir: Path) -> dict[str, Path]:
    """Return Pack roots from the canonical v4 inventory, never legacy files."""
    payload = _load_pack_catalog(ecosystem_dir)
    return {
        pack_id: (ecosystem_dir / pack_id).resolve()
        for pack_id in payload["pack_ids"]
        if (ecosystem_dir / pack_id).is_dir()
    }


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
            "baseline must use schema_version 2, shrink_only_exact_edges, and exceptions[]"
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
                raise BaselineError(f"baseline exception {index} has broad {field}: {value!r}")
        identity = str(item["identity"])
        expected = (
            f"{item['rule']}|{item['path']}|{item['fingerprint']}|{item['source']}|{item['target']}"
        )
        if identity != expected or identity in result:
            raise BaselineError(f"invalid or duplicate baseline identity: {identity}")
        if not isinstance(item["line"], int) or item["line"] < 1:
            raise BaselineError(f"invalid line for {identity}")
        if not isinstance(item["fix_by_wave"], int) or item["fix_by_wave"] < 1:
            raise BaselineError(f"invalid fix_by_wave for {identity}")
        if str(item["violation_category"]) != str(item["rule"]):
            raise BaselineError(f"violation_category must equal rule for {identity}")
        for field in ("introduced_at", "sunset_at"):
            try:
                dt.date.fromisoformat(str(item[field]))
            except ValueError as exc:
                raise BaselineError(f"{field} must be an ISO date for {identity}") from exc
        result[identity] = item
    return result


def verify_shrink_only_baseline(
    baseline: dict[str, dict[str, Any]],
    reference: dict[str, dict[str, Any]],
) -> None:
    """Reject new or changed exceptions while allowing line relocation."""
    additions = sorted(set(baseline) - set(reference))
    if additions:
        raise BaselineError("shrink-only baseline contains new identities: " + ", ".join(additions))
    for identity, item in baseline.items():
        if _relocation_stable_metadata(item) != _relocation_stable_metadata(reference[identity]):
            raise BaselineError(f"shrink-only baseline metadata changed for {identity}")


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
    return [item for identity, item in baseline.items() if identity not in active_identities]


def write_shrunk_baseline(
    path: Path,
    baseline: dict[str, dict[str, Any]],
    violations: Iterable[Violation],
) -> int:
    """Remove only resolved baseline identities through the canonical scanner.

    The reference check is performed by the caller before this function runs.
    This writer deliberately never adds an identity, relocates an existing
    exception, or changes its review metadata.  A newly observed edge makes
    the update fail instead of silently broadening the candidate baseline.
    """

    materialized = list(violations)
    unbaselined = find_unbaselined_violations(materialized, baseline)
    if unbaselined:
        identities = ", ".join(item.identity for item in unbaselined)
        raise BaselineError("cannot update baseline with unbaselined identities: " + identities)
    active_identities = {item.identity for item in materialized}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"baseline is unreadable: {exc}") from exc
    exceptions = payload.get("exceptions") if isinstance(payload, dict) else None
    if not isinstance(exceptions, list):
        raise BaselineError("baseline exceptions are unavailable for shrink update")
    shrunk = [
        item
        for item in exceptions
        if isinstance(item, dict) and item.get("identity") in active_identities
    ]
    if len(shrunk) > len(exceptions):
        raise BaselineError("baseline shrink update is invalid")
    payload["exceptions"] = shrunk
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(exceptions) - len(shrunk)


def _relocation_stable_metadata(item: dict[str, Any]) -> str:
    return json.dumps(
        {key: value for key, value in item.items() if key not in {"identity", "line"}},
        sort_keys=True,
        separators=(",", ":"),
    )


def find_expired_baseline_exceptions(
    baseline: dict[str, dict[str, Any]], *, today: dt.date
) -> list[dict[str, Any]]:
    """Return exceptions whose sunset date has passed."""
    return [
        item for item in baseline.values() if dt.date.fromisoformat(str(item["sunset_at"])) < today
    ]


def scan_repository(root: Path) -> list[Violation]:
    """Scan all supported source files and return deterministic violations."""
    ecosystem = root / "tobkiri_runtime" / "ecosystem"
    catalog = _load_pack_catalog(ecosystem)
    pack_names = set(catalog["pack_ids"])
    pack_roots = {
        pack_id: (ecosystem / pack_id).resolve()
        for pack_id in pack_names
        if (ecosystem / pack_id).is_dir()
    }
    violations: set[Violation] = set()
    violations.update(_scan_catalog_disk_alignment(root, ecosystem, pack_names))
    violations.update(_scan_catalog_graph(root, catalog, pack_names))
    for path in _source_files(root):
        source_pack = _source_pack(root, path, pack_roots)
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            violations.update(_scan_python(root, path, text, source_pack, pack_names, pack_roots))
        else:
            violations.update(_scan_script_imports(root, path, text, source_pack, pack_roots))
        violations.update(_scan_literal_paths(root, path, text, source_pack, pack_names))
        violations.update(_scan_runtime_policy(root, path, text, source_pack))
        if "/webapp/" in path.as_posix():
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
        if path.suffix == ".py" and not _is_product_special_case_exclusion(root, path):
            violations.update(
                _scan_product_special_cases(root, path, text, source_pack, pack_names)
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
    if any(part in IGNORED_SOURCE_DIRECTORY_NAMES for part in directory_parts):
        return True
    return any(
        relative.is_relative_to(ignored_path) for ignored_path in IGNORED_SOURCE_DIRECTORY_PATHS
    )


def _scan_catalog_disk_alignment(
    root: Path, ecosystem: Path, pack_names: set[str]
) -> set[Violation]:
    """Report Pack directories that disagree with the canonical inventory."""
    found: set[Violation] = set()
    disk_names = (
        {
            path.name
            for path in ecosystem.iterdir()
            if path.is_dir() and path.name != "setup_pack" and not path.name.startswith(".")
        }
        if ecosystem.is_dir()
        else set()
    )
    for pack_id in sorted(pack_names - disk_names):
        found.add(
            _line_violation(
                root,
                _pack_catalog_path(ecosystem),
                1,
                "missing_declared_pack_directory",
                "v4-pack-catalog",
                pack_id,
                _value_fingerprint("catalog-v1", f"missing:{pack_id}"),
            )
        )
    for pack_id in sorted(disk_names - pack_names):
        found.add(
            _line_violation(
                root,
                ecosystem / pack_id,
                1,
                "unlisted_pack_directory",
                "disk-ecosystem",
                pack_id,
                _value_fingerprint("catalog-v1", f"unlisted:{pack_id}"),
            )
        )
    return found


def _scan_catalog_graph(
    root: Path, catalog: Mapping[str, Any], pack_names: set[str]
) -> set[Violation]:
    """Validate the complete dependency graph declared by the v4 catalog."""
    found: set[Violation] = set()
    catalog_path = _pack_catalog_path(root / "tobkiri_runtime" / "ecosystem")
    records = catalog.get("packs", [])
    for record in records:
        if not isinstance(record, Mapping):
            continue
        pack_id = str(record.get("pack_id") or "").strip()
        dependencies = record.get("dependencies", [])
        if isinstance(dependencies, dict):
            dependencies = list(dependencies)
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if isinstance(dependency, dict):
                target = str(dependency.get("pack_id") or dependency.get("id") or "").strip()
            else:
                target = str(dependency).strip()
            if target and target not in pack_names:
                found.add(
                    _line_violation(
                        root,
                        catalog_path,
                        1,
                        "unknown_manifest_dependency",
                        pack_id,
                        target,
                        _value_fingerprint(
                            "catalog-v1",
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


def _source_pack(root: Path, path: Path, roots: dict[str, Path]) -> str:
    """Return a source Pack, recognizing the single product composition root."""
    if path.relative_to(root) == APPLICATION_COMPOSITION_ROOT:
        return "defaultspack"
    return _owning_pack(path, roots)


def _is_product_special_case_exclusion(root: Path, path: Path) -> bool:
    """Exclude only approved composition/policy sources from literal policy checks."""
    return path.relative_to(root) in PRODUCT_SPECIAL_CASE_EXCLUDED_PATHS


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
                target_path = (path.parent / Path(*([".."] * (node.level - 1)))).resolve()
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
            imported_pack = _module_pack(module, pack_names)
            if imported_pack and imported_pack != source_pack:
                found.add(
                    _line_violation(
                        root,
                        path,
                        getattr(node, "lineno", 1),
                        "cross_pack_import",
                        source_pack,
                        imported_pack,
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


def _pack_references(value: str, pack_names: set[str]) -> set[str]:
    """Return Pack IDs named by a module, path, or qualified identifier."""
    if not value:
        return set()
    parts = set(part for part in re.split(r"[./:]", value) if part)
    return {pack_id for pack_id in pack_names if value == pack_id or pack_id in parts}


def _pack_values(
    node: ast.AST | None,
    aliases: Mapping[str, set[str]],
    pack_names: set[str],
) -> set[str]:
    """Resolve the small constant subset needed for product-branch checks."""
    if node is None:
        return set()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _pack_references(node.value, pack_names)
    if isinstance(node, ast.Name):
        return set(aliases.get(node.id, set()))
    if isinstance(node, ast.JoinedStr):
        result: set[str] = set()
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                result.update(_pack_values(value.value, aliases, pack_names))
        return result
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _pack_values(node.left, aliases, pack_names) | _pack_values(
            node.right, aliases, pack_names
        )
    return set()


def _pack_aliases(tree: ast.AST, pack_names: set[str]) -> dict[str, set[str]]:
    """Resolve literal and import aliases without executing application code."""
    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                references = _pack_references(imported.name, pack_names)
                if references:
                    aliases[imported.asname or imported.name.split(".", 1)[0]] = references
        elif isinstance(node, ast.ImportFrom):
            module_references = _pack_references(node.module or "", pack_names)
            for imported in node.names:
                references = module_references or _pack_references(imported.name, pack_names)
                if references:
                    aliases[imported.asname or imported.name] = references
        elif isinstance(node, ast.Assign):
            references = _pack_values(node.value, aliases, pack_names)
            if references:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = references
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            references = _pack_values(node.value, aliases, pack_names)
            if references:
                aliases[node.target.id] = references
    return aliases


def _scan_product_special_cases(
    root: Path,
    path: Path,
    text: str,
    source_pack: str,
    pack_names: set[str],
) -> set[Violation]:
    """Reject product Pack special cases, including simple indirection.

    Pack-owned code may name its own implementation, but kernel/Host code must
    remain catalog-driven.  The check deliberately resolves only literals and
    import aliases; dynamic values remain an explicit unknown for the runtime
    gate rather than becoming a trusted exception here.
    """
    if source_pack in pack_names:
        return set()
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return set()
    aliases = _pack_aliases(tree, pack_names)
    found: set[Violation] = set()

    def add(node: ast.AST, rule: str, target: str) -> None:
        found.add(
            _line_violation(
                root,
                path,
                getattr(node, "lineno", 1),
                rule,
                source_pack,
                target,
                _ast_fingerprint(node),
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = (
                ",".join(alias.name for alias in node.names)
                if isinstance(node, ast.Import)
                else node.module or ""
            )
            references = _pack_references(module, pack_names)
            if references:
                add(node, "product_pack_import", ",".join(sorted(references)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            references = _pack_values(value, aliases, pack_names)
            if references:
                add(node, "product_pack_reference", ",".join(sorted(references)))
        elif isinstance(node, ast.Call):
            function = ast.unparse(node.func)
            if function in {"importlib.import_module", "__import__"} and node.args:
                references = _pack_values(node.args[0], aliases, pack_names)
                if references:
                    add(node, "product_pack_import", ",".join(sorted(references)))
        elif isinstance(node, ast.Compare):
            references = _pack_values(node.left, aliases, pack_names)
            for comparator in node.comparators:
                references.update(_pack_values(comparator, aliases, pack_names))
            if references:
                add(node, "product_pack_branch", ",".join(sorted(references)))
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


def _scan_runtime_policy(root: Path, path: Path, text: str, source_pack: str) -> set[Violation]:
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
                    fingerprint=(f"{item.fingerprint}:occurrence-v1-{occurrence}"),
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
                        "message": {"text": (f"{item.source} -> {item.target}. {item.guidance}")},
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
    """Run the repository scan against an independently supplied baseline."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[3])
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--reference-baseline", type=Path)
    parser.add_argument("--update-baseline", action="store_true")
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
    if args.reference_baseline is None:
        print(
            "pack-architecture: protected reference baseline is required; "
            "refusing to treat the candidate baseline as its own authority",
            file=sys.stderr,
        )
        return 2
    reference_path = args.reference_baseline.resolve()
    if reference_path == baseline_path.resolve():
        print(
            "pack-architecture: candidate and reference baselines must be different files",
            file=sys.stderr,
        )
        return 2
    try:
        baseline = load_baseline(baseline_path)
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
    try:
        violations = scan_repository(root)
    except PackCatalogError as exc:
        print(f"pack-architecture: {exc}", file=sys.stderr)
        return 2
    stale = find_stale_baseline_exceptions(violations, baseline)
    active = find_unbaselined_violations(violations, baseline)
    if args.update_baseline:
        if active:
            identities = ", ".join(item.identity for item in active)
            print(
                "pack-architecture: cannot update baseline with new identities: " + identities,
                file=sys.stderr,
            )
            return 1
        try:
            removed = write_shrunk_baseline(baseline_path, baseline, violations)
        except BaselineError as exc:
            print(f"pack-architecture: {exc}", file=sys.stderr)
            return 2
        print(f"pack-architecture: updated baseline by removing {removed} resolved identities")
        return 0
    if stale:
        identities = ", ".join(str(item["identity"]) for item in stale)
        print(
            "pack-architecture: baseline contains resolved identities; "
            f"remove them to preserve shrink-only enforcement: {identities}",
            file=sys.stderr,
        )
        return 2
    if args.format == "json":
        print(
            json.dumps(
                {"violations": [asdict(item) | {"identity": item.identity} for item in active]},
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
