"""Non-negotiable, current-tree gates for the complete Tobkiri v4 migration.

The inventory is deliberately finite: only the four canonical artifacts in
each direct ``ecosystem`` Pack directory are counted.  Source checks use
Python ASTs or comment/string-stripped Rust call tokens so schemas, type
declarations, tests, Playwright, development helpers, and display text do not
become runtime findings.
"""

from __future__ import annotations

import ast
import hashlib
import json
import ntpath
import os
import re
import subprocess
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import pytest

from tobkiri_protocol.validation import load_schema, validate_file


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "tobkiri_runtime"
ECOSYSTEM = RUNTIME / "ecosystem"

EXPECTED_PRODUCTION_PACK_COUNT = 143
V4_PROJECTION_GENERATOR = "tobkiri.scripts.migrate_manifest_authority/v2"
PACK_ARTIFACTS = {
    "artifact-index.v4.json": "pack_artifact_index",
    "pack.v4.json": "pack",
    "contracts.v4.json": "pack_contract_catalog",
    "executables.v4.json": "executable_catalog",
}
SOURCE_SUFFIXES = {".py", ".rs"}
IGNORED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "tests",
    "fixtures",
    "schemas",
    "schema",
    "playwright",
    "dev",
    "display",
}

# These are executable production roots, not a repository-wide source list.
# The ``__main__`` path deliberately follows the current runtime shim so a
# still-reachable legacy authority cannot disappear merely because another
# module has a v4-looking name.
PYTHON_ENTRY_ROOTS = (
    RUNTIME / "app.py",
    RUNTIME / "core_runtime" / "bootstrap" / "runtime.py",
    RUNTIME / "core_runtime" / "bootstrap" / "profile_capture.py",
    RUNTIME / "tobkiri" / "__main__.py",
    RUNTIME / "tobkiri" / "cli_shell.py",
    RUNTIME / "tobkiri_host" / "runtime.py",
    RUNTIME / "ecosystem" / "defaultspack" / "defaultspack" / "desktop_app.py",
    RUNTIME / "ecosystem" / "defaultspack" / "run_http.py",
    RUNTIME / "ecosystem" / "defaultspack" / "domain" / "runtime_v4" / "__init__.py",
)

LEGACY_SYMBOLS = frozenset(
    {
        "FunctionRegistry",
        "InterfaceRegistry",
        "CapabilityExecutor",
        "AuthorityService",
        "PermissionManager",
    }
)
LEGACY_AUTHORITY_MODULES = frozenset(
    {
        "core_runtime.authority.service",
        "core_runtime.authority",
        "backend_core.ecosystem.registry",
    }
)
LEGACY_ENTRY_MODULES = frozenset(
    {
        "core_runtime.global_contracts.manifest",
        "core_runtime.manifest_projection",
        "core_runtime.setup_pack",
        "ecosystem.setup_pack",
    }
)
INSTALLED_LOOKUP_NAMES = frozenset(
    {
        "all_installed",
        "installed_packs",
        "_discover_installed_packs",
        "discover_installed_packs",
        "list_installed_packs",
    }
)
PROJECTION_CALL_NAMES = frozenset(
    {"generate_legacy_ecosystem_projection", "project_legacy_ecosystem"}
)
FALLBACK_NAMES = frozenset(
    {
        "active_provider_fallback",
        "fallback_provider",
        "implicit_fallback",
        "promotion_fallback",
        "auto_promote",
        "auto_promotion",
    }
)
OLD_COMPOSITION_MODULE = "domain.pack_architecture"
VALID_MANIFEST_AUTHORITIES = frozenset({"v4-authoritative"})
_CHILD_FAILURE_DIAGNOSTIC_PREFIX = "fresh-home child process failed: "

_CHILD_DIAGNOSTIC_ENV_KEYS = (
    "TOBKIRI_USER_DATA",
    "RUMI_USER_DATA",
    "RUMI_SANDBOX_LIMA_STATE",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPYCACHEPREFIX",
    "PYTHONPATH",
    "PYTHONHOME",
    "PATH",
    "HOME",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "RUMI_API_BIND_ADDRESS",
    "RUMI_ALLOW_LEGACY_REMOTE_BEARER",
)

PACK_API_SOURCE = RUNTIME / "core_runtime" / "pack_api_server.py"
PACK_API_AUTH_SOURCE = RUNTIME / "core_runtime" / "api" / "auth_gate.py"
LEGACY_API_SYMBOLS = frozenset(
    {
        "APIRouteTableMixin",
        "ContractToolCatalog",
        "EcosystemNodeRegistry",
        "discover_pack_locations",
        "get_approval_manager",
        "get_capability_executor",
        "get_hmac_key_manager",
        "HMACKeyManager",
    }
)
LEGACY_API_ENV_AUTHORITY = frozenset(
    {
        "RUMI_ALLOW_LEGACY_REMOTE_BEARER",
        "RUMI_API_BIND_ADDRESS",
    }
)
LEGACY_LIVE_ROUTES = frozenset(
    {
        "/api/packs",
        "/api/authority/events",
        "/api/runtime/available",
        "/api/packs/scan",
        "/api/routes/reload",
    }
)

RETIRED_PROFILE_AND_EXECUTION_MODULES = frozenset(
    {
        RUNTIME / "core_runtime" / "global_contracts" / "manifest.py",
        RUNTIME / "core_runtime" / "resolved_profile.py",
        RUNTIME / "core_runtime" / "runtime_profile_resolver.py",
        RUNTIME / "core_runtime" / "setup_pack.py",
        RUNTIME / "core_runtime" / "core_pack" / "core_setup" / "check_profile.py",
        RUNTIME / "core_runtime" / "core_pack" / "core_setup" / "save_profile.py",
        RUNTIME / "core_runtime" / "api" / "oauth_handlers.py",
        RUNTIME / "core_runtime" / "api" / "flow_handlers.py",
        RUNTIME / "core_runtime" / "flow_loader.py",
        RUNTIME / "core_runtime" / "kernel_flow_execution.py",
        RUNTIME / "core_runtime" / "kernel_handlers_runtime.py",
        RUNTIME / "core_runtime" / "python_file_executor.py",
        RUNTIME / "core_runtime" / "secure_executor.py",
        RUNTIME / "core_runtime" / "lib_executor.py",
        RUNTIME / "core_runtime" / "unit_executor.py",
        ECOSYSTEM / "defaultspack" / "transport" / "http.py",
        ECOSYSTEM / "defaultspack" / "transport" / "stdio.py",
        ECOSYSTEM / "defaultspack" / "transport" / "cli.py",
        ECOSYSTEM / "defaultspack" / "domain" / "flow" / "engine.py",
        ECOSYSTEM / "setup_pack" / "pack_selector.py",
    }
)

AUTHORITY_ENV_NAMES = frozenset(
    {
        "RUMI_AUTO_APPROVE_LOCAL",
        "RUMI_ALLOW_HOST_EXECUTION",
        "TOBKIRI_ALLOW_HOST_EXECUTION",
    }
)
SHELL_ROOT_NAMES = frozenset(
    {
        "select_presentation",
        "select_presentation_impl",
        "launch_selected_presentation",
        "launch_selected_presentation_impl",
        "validate_selection",
        "validate_production_artifact",
        "artifact_path",
        "safe_artifact_path",
        "load_catalog",
    }
)
SAFE_LAUNCH_CONTEXTS = frozenset(
    {
        "host_broker",
        "defaultspack",
        "uv",
        "codesign",
        "launchservices",
        "dock_registration",
    }
)


def _production_files() -> tuple[Path, ...]:
    """Return source files in the production surfaces for Rust inspection."""
    roots = (
        RUNTIME / "core_runtime",
        RUNTIME / "backend_core",
        RUNTIME / "ecosystem",
        RUNTIME / "tobkiri_host",
        ROOT / "tobkiri_launcher" / "src-tauri" / "src",
        ROOT / "tobkiri_launcher" / "scripts",
        ROOT / "pack-shell" / "src",
    )
    paths: set[Path] = set()
    for base in roots:
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES:
                paths.add(path)
    return tuple(sorted(path for path in paths if not _ignored_source(path)))


def _ignored_source(path: Path) -> bool:
    """Exclude only explicit non-production directory contexts."""
    return any(part.lower() in IGNORED_PARTS for part in path.parts)


def _relative(path: Path) -> str:
    """Return a stable repository-relative path."""
    return path.relative_to(ROOT).as_posix()


def _finding(path: Path, line: int, rule: str, **extra: Any) -> dict[str, Any]:
    """Build one deterministic scanner finding."""
    symbol = extra.pop("symbol", None) or rule
    try:
        display_path = _relative(path)
    except ValueError:
        display_path = path.as_posix()
    return {
        "path": display_path,
        "line": line,
        "symbol": symbol,
        "rule": rule,
        **extra,
    }


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _sha256(path: Path) -> str:
    """Return the protocol digest for one regular file."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _production_pack_dirs() -> tuple[Path, ...]:
    """Return exactly the direct Pack roots under ``ecosystem``."""
    return tuple(
        sorted(
            path
            for path in ECOSYSTEM.iterdir()
            if path.is_dir() and path.name != "setup_pack" and not path.name.startswith(".")
        )
    )


def _v4_pack_artifacts() -> list[Path]:
    """Return only the 141 direct ``pack.v4.json`` files."""
    return [path / "pack.v4.json" for path in _production_pack_dirs()]


def _v4_profile_artifacts() -> list[Path]:
    """Return the explicit v4 Profile entrypoint, not recursive Pack bundles."""
    path = ECOSYSTEM / "defaultspack" / "v4" / "defaults.profile.v4.json"
    return [path] if path.is_file() else []


def _v4_artifact_findings() -> list[dict[str, Any]]:
    """Validate and compile the exact 141 x 4 direct artifact set."""
    findings: list[dict[str, Any]] = []
    for pack_dir in _production_pack_dirs():
        values: dict[str, Mapping[str, Any]] = {}
        for name, schema_name in PACK_ARTIFACTS.items():
            path = pack_dir / name
            if not path.is_file():
                findings.append(_finding(path, 1, "missing_v4_artifact", artifact=name))
                continue
            try:
                values[name] = validate_file(path, schema_name)
            except Exception as exc:  # schema validator emits stable diagnostics
                findings.append(
                    _finding(
                        path,
                        1,
                        "invalid_v4_artifact",
                        artifact=name,
                        error=str(exc)[:240],
                    )
                )

        manifest = values.get("pack.v4.json")
        index = values.get("artifact-index.v4.json")
        contracts = values.get("contracts.v4.json")
        if not manifest or not index or not contracts:
            continue
        pack_id = manifest.get("pack", {}).get("id")
        source_identity = manifest.get("integrity", {}).get("source_identity")
        if pack_id != pack_dir.name:
            findings.append(
                _finding(
                    pack_dir / "pack.v4.json",
                    1,
                    "pack_identity_mismatch",
                    expected=pack_dir.name,
                    actual=pack_id,
                )
            )
        if set((index.get("pack_id"), contracts.get("pack_id"), pack_id)) != {pack_dir.name}:
            findings.append(_finding(pack_dir / "pack.v4.json", 1, "artifact_pack_id_mismatch"))
        if set(
            (
                source_identity,
                index.get("source_identity"),
                contracts.get("source_identity"),
            )
        ) != {source_identity}:
            findings.append(_finding(pack_dir / "pack.v4.json", 1, "source_identity_mismatch"))

        expected_artifacts = {
            "pack.v4.json": ("canonical_manifest", _sha256(pack_dir / "pack.v4.json")),
            "contracts.v4.json": ("contract_catalog", _sha256(pack_dir / "contracts.v4.json")),
        }
        indexed = {item.get("path"): item for item in index.get("artifacts", [])}
        for name, (role, digest) in expected_artifacts.items():
            item = indexed.get(name)
            if (
                not isinstance(item, Mapping)
                or item.get("role") != role
                or item.get("digest") != digest
            ):
                findings.append(
                    _finding(
                        pack_dir / "artifact-index.v4.json",
                        1,
                        "artifact_digest_mismatch",
                        artifact=name,
                    )
                )
        if index.get("artifact_set_digest") != manifest.get("pack", {}).get("artifact_digest"):
            findings.append(_finding(pack_dir / "pack.v4.json", 1, "artifact_set_digest_mismatch"))
        integrity = manifest.get("integrity", {})
        if integrity.get("contract_catalog_digest") != _sha256(pack_dir / "contracts.v4.json"):
            findings.append(
                _finding(pack_dir / "pack.v4.json", 1, "contract_catalog_digest_mismatch")
            )
        try:
            from tobkiri_host.artifact_compiler import compile_pack_root

            compile_pack_root(pack_dir)
        except Exception as exc:
            findings.append(
                _finding(
                    pack_dir / "executables.v4.json",
                    1,
                    "production_compiler_rejected_pack",
                    error=f"{type(exc).__name__}: {exc}"[:240],
                )
            )
    return findings


def _v4_profile_findings() -> list[dict[str, Any]]:
    """Validate the explicit Profile artifact and its exact selection shape."""
    findings: list[dict[str, Any]] = []
    for path in _v4_profile_artifacts():
        try:
            profile = validate_file(path, "profile")
        except Exception as exc:
            findings.append(_finding(path, 1, "invalid_v4_profile", error=str(exc)[:240]))
            continue
        if profile.get("profile_id") != "defaults" or profile.get("state") != "needs_resolution":
            findings.append(_finding(path, 1, "profile_scope_mismatch"))
        if not isinstance(profile.get("base"), Mapping) or not isinstance(
            profile.get("shell"), Mapping
        ):
            findings.append(_finding(path, 1, "profile_selection_not_exact"))
    return findings


def _authority_source_sets() -> dict[str, set[str]]:
    """Load canonical manifest and v4 catalog source sets without discovery."""
    manifest_catalog = _load_json(RUNTIME / "schemas" / "manifest_authority.v1.json")
    v4_catalog = _load_json(RUNTIME / "schemas" / "pack_v4_catalog.v1.json")
    manifest_ids = (
        set(manifest_catalog.get("packs", {}))
        if isinstance(manifest_catalog, Mapping)
        and isinstance(manifest_catalog.get("packs"), Mapping)
        else set()
    )
    v4_ids = (
        set(v4_catalog.get("pack_ids", ()))
        if isinstance(v4_catalog, Mapping) and isinstance(v4_catalog.get("pack_ids"), list)
        else set()
    )
    direct_ids = {path.name for path in _production_pack_dirs()}
    manifest_source_ids = {
        path.name for path in _production_pack_dirs() if (path / "ecosystem.json").is_file()
    }
    return {
        "direct_ids": direct_ids,
        "manifest_ids": manifest_ids,
        "manifest_source_ids": manifest_source_ids,
        "v4_ids": v4_ids,
        "v4_only_ids": direct_ids - manifest_source_ids,
    }


def _source_set_delta(expected: set[str], observed: set[str]) -> dict[str, list[str]] | None:
    """Return explicit missing/extra diagnostics for one canonical source set."""
    if expected == observed:
        return None
    return {
        "missing": sorted(expected - observed),
        "extra": sorted(observed - expected),
    }


def _manifest_authority_counts() -> tuple[Counter[str], list[dict[str, Any]]]:
    """Return canonical authority classes, including derived v4-only Packs."""
    catalog = _load_json(RUNTIME / "schemas" / "manifest_authority.v1.json")
    classified = catalog.get("packs", {}) if isinstance(catalog, Mapping) else {}
    v4_only_ids = _authority_source_sets()["v4_only_ids"]
    records = []
    for path in _production_pack_dirs():
        pack_id = path.name
        authority = classified.get(pack_id)
        records.append(
            {
                "pack_id": pack_id,
                "classified_as": authority or ("v4-only" if pack_id in v4_only_ids else None),
                "manifest_authority": authority,
                "v4_only": pack_id in v4_only_ids,
                "v4_artifacts": all((path / name).is_file() for name in PACK_ARTIFACTS),
            }
        )
    return Counter(str(record["classified_as"]) for record in records), records


def _authority_resolved_plan_findings() -> list[dict[str, Any]]:
    """Require exact Authority ownership and the narrow ResolvedPlan scope."""
    findings: list[dict[str, Any]] = []
    manifest_path = RUNTIME / "schemas" / "manifest_authority.v1.json"
    v4_catalog_path = RUNTIME / "schemas" / "pack_v4_catalog.v1.json"
    manifest_catalog = _load_json(manifest_path)
    v4_catalog = _load_json(v4_catalog_path)
    classified = manifest_catalog.get("packs", {}) if isinstance(manifest_catalog, Mapping) else {}
    v4_entries = v4_catalog.get("packs", ()) if isinstance(v4_catalog, Mapping) else ()
    raw_v4_pack_id_list = v4_catalog.get("pack_ids") if isinstance(v4_catalog, Mapping) else None
    v4_pack_id_list = raw_v4_pack_id_list if isinstance(raw_v4_pack_id_list, list) else []
    sources = _authority_source_sets()
    direct_ids = sources["direct_ids"]
    manifest_ids = sources["manifest_ids"]
    manifest_source_ids = sources["manifest_source_ids"]
    v4_ids = sources["v4_ids"]
    v4_only_ids = sources["v4_only_ids"]
    entry_ids = {item.get("pack_id") for item in v4_entries if isinstance(item, Mapping)}
    entry_id_list = [item.get("pack_id") for item in v4_entries if isinstance(item, Mapping)]

    def add_scope_finding(path: Path, rule: str, symbol: str, **details: Any) -> None:
        findings.append(_finding(path, 1, rule, symbol=symbol, **details))

    if not isinstance(classified, Mapping) or any(
        value not in VALID_MANIFEST_AUTHORITIES for value in classified.values()
    ):
        add_scope_finding(
            manifest_path,
            "authority_catalog_value_invalid",
            "manifest_authority",
            classified=dict(sorted(classified.items()))
            if isinstance(classified, Mapping)
            else classified,
        )
    catalog_delta = _source_set_delta(direct_ids, manifest_ids)
    expected_authority_counts = Counter({"v4-authoritative": EXPECTED_PRODUCTION_PACK_COUNT})
    observed_authority_counts = (
        Counter(str(classified.get(pack_id)) for pack_id in direct_ids)
        if isinstance(classified, Mapping)
        else Counter()
    )
    if catalog_delta is not None or observed_authority_counts != expected_authority_counts:
        add_scope_finding(
            manifest_path,
            "authority_catalog_scope_mismatch",
            "manifest_authority",
            **(catalog_delta or {"missing": [], "extra": []}),
            expected_authority_counts=dict(sorted(expected_authority_counts.items())),
            observed_authority_counts=dict(sorted(observed_authority_counts.items())),
        )
    manifest_delta = _source_set_delta(manifest_ids - v4_only_ids, manifest_source_ids)
    if manifest_delta is not None:
        add_scope_finding(
            manifest_path,
            "manifest_canonical_source_set_mismatch",
            "manifest_authority",
            **manifest_delta,
            v4_only=sorted(v4_only_ids),
        )
    v4_entry_delta = _source_set_delta(v4_ids, entry_ids)
    if (
        v4_entry_delta is not None
        or len(v4_pack_id_list) != len(v4_ids)
        or len(entry_id_list) != len(entry_ids)
    ):
        add_scope_finding(
            v4_catalog_path,
            "v4_catalog_entry_set_mismatch",
            "pack_v4_catalog",
            **(v4_entry_delta or {"missing": [], "extra": []}),
            duplicate_pack_ids=sorted(
                {
                    pack_id
                    for pack_id in list(v4_pack_id_list) + entry_id_list
                    if list(v4_pack_id_list).count(pack_id) > 1 or entry_id_list.count(pack_id) > 1
                }
            ),
        )
    v4_direct_delta = _source_set_delta(v4_ids, direct_ids)
    if v4_direct_delta is not None:
        add_scope_finding(
            v4_catalog_path,
            "v4_catalog_direct_scope_mismatch",
            "pack_v4_catalog",
            **v4_direct_delta,
        )
    canonical_ids = manifest_source_ids | v4_only_ids
    canonical_delta = _source_set_delta(canonical_ids, direct_ids)
    v4_only_delta = _source_set_delta(direct_ids - manifest_source_ids, v4_only_ids)
    if canonical_delta is not None or v4_only_delta is not None:
        add_scope_finding(
            v4_catalog_path,
            "canonical_pack_source_set_mismatch",
            "canonical_pack_source_set",
            **(canonical_delta or {"missing": [], "extra": []}),
            v4_only_missing=(v4_only_delta or {}).get("missing", []),
            v4_only_extra=(v4_only_delta or {}).get("extra", []),
            v4_only=sorted(v4_only_ids),
        )

    for pack_id in sorted(direct_ids & manifest_ids):
        pack_dir = ECOSYSTEM / pack_id
        authority = classified.get(pack_id) if isinstance(classified, Mapping) else None
        if authority != "v4-authoritative":
            add_scope_finding(
                pack_dir / "pack.v4.json",
                "non_v4_production_authority",
                pack_id,
            )
    for pack_id in sorted(v4_only_ids & direct_ids):
        pack_dir = ECOSYSTEM / pack_id
        if (pack_dir / "ecosystem.json").exists() or (pack_dir / "rumi.pack.v3.json").exists():
            add_scope_finding(
                pack_dir / "pack.v4.json",
                "v4_only_pack_has_legacy_source",
                pack_id,
            )
    plan_schema = load_schema("resolved_plan")
    required_plan = frozenset(plan_schema.get("required", ()))
    properties_plan = frozenset(plan_schema.get("properties", ()))
    expected_plan = frozenset(
        {
            "plan_api_version",
            "profile_id",
            "profile_revision",
            "profile_definition_digest",
            "catalog_revision",
            "bundle_digest",
            "profile_authority_snapshot_digest",
            "security_epoch",
            "base",
            "shell",
            "application",
            "effective_set",
            "requested_edges_digest",
            "constraints_digest",
            "closure_digest",
            "provenance_digest",
            "bindings",
            "plan_digest",
        }
    )
    if required_plan != expected_plan or properties_plan != expected_plan:
        findings.append(
            {
                "path": "tobkiri_runtime/tobkiri_protocol/schemas/resolved_plan_v1.schema.json",
                "line": 1,
                "rule": "resolved_plan_scope_mismatch",
                "required": sorted(required_plan),
                "properties": sorted(properties_plan),
            }
        )
    contracts_path = RUNTIME / "tobkiri_host" / "contracts.py"
    try:
        tree = ast.parse(contracts_path.read_text(encoding="utf-8"), filename=str(contracts_path))
        operation_catalog = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "OperationCatalog"
        )
        methods = {
            node.name
            for node in operation_catalog.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if not {"__init__", "resolve"}.issubset(methods):
            raise LookupError("OperationCatalog exact resolve scope is missing")
    except (OSError, SyntaxError, LookupError) as exc:
        findings.append(
            _finding(contracts_path, 1, "resolved_plan_runtime_scope_missing", error=str(exc))
        )
    return findings


def _python_tree(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return None


def _module_candidates(
    current: Path,
    module: str,
    level: int = 0,
    imported_names: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    """Resolve a Python import to local files without importing application code."""
    parts = tuple(part for part in module.split(".") if part)
    if level:
        base = current.parent
        for _ in range(max(level - 1, 0)):
            base = base.parent
        roots = (base,)
    else:
        roots = (
            RUNTIME,
            RUNTIME / "ecosystem",
            RUNTIME / "ecosystem" / "defaultspack",
        )

    candidates: list[Path] = []
    for root in roots:
        module_path = root.joinpath(*parts) if parts else root
        candidates.extend(
            (
                module_path.with_suffix(".py"),
                module_path / "__init__.py",
            )
        )
        for name in imported_names:
            child = module_path / name
            candidates.extend((child.with_suffix(".py"), child / "__init__.py"))
    return tuple(dict.fromkeys(path for path in candidates if path.is_file()))


def _python_import_targets(path: Path, tree: ast.AST) -> tuple[Path, ...]:
    """Return local import edges from one parsed production module."""
    targets: set[Path] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.update(_module_candidates(path, alias.name))
        elif isinstance(node, ast.ImportFrom):
            targets.update(
                _module_candidates(
                    path,
                    node.module or "",
                    node.level,
                    tuple(alias.name for alias in node.names if alias.name != "*"),
                )
            )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr != "import_module" or not node.args:
                continue
            value = node.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                targets.update(_module_candidates(path, value.value))
    return tuple(sorted(targets))


@lru_cache(maxsize=1)
def _reachable_python_trees() -> dict[Path, ast.AST]:
    """Build the production import graph from the declared executable roots."""
    queue = list(PYTHON_ENTRY_ROOTS)
    seen: set[Path] = set()
    graph: dict[Path, ast.AST] = {}
    while queue:
        path = queue.pop(0)
        if path in seen or _ignored_source(path):
            continue
        seen.add(path)
        tree = _python_tree(path)
        if tree is None:
            continue
        graph[path] = tree
        queue.extend(target for target in _python_import_targets(path, tree) if target not in seen)
    return graph


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _owner(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return current.name
    return "<module>"


def _called_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _legacy_aliases(tree: ast.AST) -> set[str]:
    """Return only imported legacy executable names in one module."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.rsplit(".", 1)[-1] in LEGACY_SYMBOLS:
                    aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in LEGACY_SYMBOLS:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _is_legacy_entry_module(module: str) -> bool:
    """Match retired entry roots and every importable child module."""

    return any(
        module == root or module.startswith(root + ".")
        for root in LEGACY_ENTRY_MODULES
    )


def _ast_legacy_runtime_findings_for_tree(path: Path, tree: ast.AST) -> list[dict[str, Any]]:
    """Find executable legacy imports and calls in one reachable module."""
    findings: list[dict[str, Any]] = []
    parents = _parents(tree)
    aliases = _legacy_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if (
                    module in LEGACY_AUTHORITY_MODULES
                    or _is_legacy_entry_module(module)
                    or module.rsplit(".", 1)[-1] in LEGACY_SYMBOLS
                ):
                    findings.append(
                        _finding(
                            path,
                            node.lineno,
                            "legacy_registry_import",
                            module=module,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported = {alias.name for alias in node.names}
            if (
                module in LEGACY_AUTHORITY_MODULES
                or _is_legacy_entry_module(module)
                or imported & LEGACY_SYMBOLS
            ):
                findings.append(
                    _finding(
                        path,
                        node.lineno,
                        "legacy_registry_import",
                        module=module,
                        symbols=sorted(imported & LEGACY_SYMBOLS),
                    )
                )
        elif isinstance(node, ast.Call):
            name = _called_name(node)
            if name in LEGACY_SYMBOLS or name in aliases:
                findings.append(
                    _finding(
                        path,
                        node.lineno,
                        "legacy_registry_call",
                        symbol=name,
                        owner=_owner(node, parents),
                    )
                )
            if name in INSTALLED_LOOKUP_NAMES:
                findings.append(
                    _finding(
                        path,
                        node.lineno,
                        "runtime_installed_lookup",
                        symbol=name,
                        owner=_owner(node, parents),
                    )
                )
    return findings


def _ast_legacy_runtime_findings() -> list[dict[str, Any]]:
    """Find legacy runtime edges reachable from production entry roots."""
    findings: list[dict[str, Any]] = []
    for path, tree in _reachable_python_trees().items():
        findings.extend(_ast_legacy_runtime_findings_for_tree(path, tree))
    return sorted(findings, key=lambda item: (item["path"], item["line"], item["rule"]))


def _pack_api_hardcoded_legacy_findings() -> list[dict[str, Any]]:
    """Reject legacy symbols, env authority, and per-route handler branches."""

    findings: list[dict[str, Any]] = []
    for source in (PACK_API_SOURCE, PACK_API_AUTH_SOURCE):
        tree = _python_tree(source)
        if tree is None:
            findings.append(_finding(source, 1, "missing_pack_api_source"))
            continue
        parent_map = _parents(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in LEGACY_API_SYMBOLS:
                        findings.append(
                            _finding(
                                source,
                                node.lineno,
                                "legacy_api_symbol_import",
                                symbol=alias.name,
                            )
                        )
            elif isinstance(node, ast.Call):
                name = _called_name(node)
                if name in LEGACY_API_SYMBOLS:
                    findings.append(
                        _finding(
                            source,
                            node.lineno,
                            "legacy_api_symbol_call",
                            symbol=name,
                        )
                    )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in LEGACY_API_ENV_AUTHORITY:
                    findings.append(
                        _finding(
                            source,
                            node.lineno,
                            "legacy_api_environment_authority",
                            symbol=node.value,
                        )
                    )
                if node.value in LEGACY_LIVE_ROUTES:
                    owner = _owner(node, parent_map)
                    if owner in {"do_GET", "do_POST", "do_PUT", "do_DELETE"}:
                        findings.append(
                            _finding(
                                source,
                                node.lineno,
                                "hardcoded_legacy_route_branch",
                                symbol=node.value,
                                owner=owner,
                            )
                        )
    return findings


def _ast_authority_bypass_findings() -> list[dict[str, Any]]:
    """Find executable legacy authority bypasses on the reachable graph."""
    findings: list[dict[str, Any]] = []
    for path, tree in _reachable_python_trees().items():
        aliases = _legacy_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _called_name(node)
                if name in LEGACY_SYMBOLS or name in aliases:
                    findings.append(
                        _finding(path, node.lineno, "authority_bypass_call", symbol=name)
                    )
                if any(
                    keyword.arg == "approved"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                ):
                    findings.append(_finding(path, node.lineno, "client_approval_flag"))
    return findings


def _ast_projection_findings() -> list[dict[str, Any]]:
    """Find projection calls in runtime code; offline scripts are not runtime."""
    findings: list[dict[str, Any]] = []
    for path, tree in _reachable_python_trees().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) in PROJECTION_CALL_NAMES:
                findings.append(
                    _finding(
                        path, node.lineno, "runtime_projection_call", symbol=_called_name(node)
                    )
                )
    return findings


def _ast_fallback_findings() -> list[dict[str, Any]]:
    """Find executable fallback/promotion symbols via AST names and calls."""
    findings: list[dict[str, Any]] = []
    for path, tree in _reachable_python_trees().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) in FALLBACK_NAMES:
                findings.append(
                    _finding(path, node.lineno, "implicit_fallback_call", symbol=_called_name(node))
                )
    return findings


def _ast_old_composition_findings() -> list[dict[str, Any]]:
    """Find deleted composition imports, not schema fields or display text."""
    findings: list[dict[str, Any]] = []
    for path, tree in _reachable_python_trees().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == OLD_COMPOSITION_MODULE or alias.name.startswith(
                        f"{OLD_COMPOSITION_MODULE}."
                    ):
                        findings.append(
                            _finding(
                                path, node.lineno, "deleted_composition_import", module=alias.name
                            )
                        )
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                OLD_COMPOSITION_MODULE
            ):
                findings.append(
                    _finding(path, node.lineno, "deleted_composition_import", module=node.module)
                )
    return findings


def _strip_rust_comments_and_strings(source: str) -> str:
    """Remove test items, comments, and string contents with line offsets."""
    production_source = _strip_rust_test_items(source)
    output: list[str] = []
    index = 0
    while index < len(production_source):
        if production_source.startswith("//", index):
            end = production_source.find("\n", index)
            end = len(production_source) if end == -1 else end
            output.append(_blank_rust_segment(production_source[index:end]))
            index = end
            continue
        if production_source.startswith("/*", index):
            end = index + 2
            depth = 1
            while end < len(production_source) and depth:
                if production_source.startswith("/*", end):
                    depth += 1
                    end += 2
                elif production_source.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            output.append(_blank_rust_segment(production_source[index:end]))
            index = end
            continue

        raw_match = re.match(r"(?:br|r)(#+)?\"", production_source[index:])
        if raw_match:
            hashes = raw_match.group(1) or ""
            opening_end = index + raw_match.end()
            marker = '"' + hashes
            closing = production_source.find(marker, opening_end)
            end = len(production_source) if closing == -1 else closing + len(marker)
            output.append(_blank_rust_segment(production_source[index:end]))
            index = end
            continue

        quote_index = index + 1 if production_source.startswith('b"', index) else index
        if production_source[quote_index : quote_index + 1] == '"':
            end = quote_index + 1
            escaped = False
            while end < len(production_source):
                char = production_source[end]
                end += 1
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    break
            output.append(_blank_rust_segment(production_source[index:end]))
            index = end
            continue

        output.append(production_source[index])
        index += 1
    return "".join(output)


def _strip_rust_comments(source: str) -> str:
    """Blank Rust comments without changing offsets used for diagnostics."""
    return re.sub(r"//[^\n]*|/\*.*?\*/", _preserve_lines, source, flags=re.S)


def _blank_rust_segment(value: str) -> str:
    """Blank Rust lexical text while preserving its line and column offsets."""
    return "".join("\n" if char == "\n" else " " for char in value)


def _preserve_lines(match: re.Match[str]) -> str:
    return "".join("\n" if char == "\n" else " " for char in match.group(0))


def _rust_item_end(source: str, start: int) -> int:
    """Return the end of the Rust item following a test-only attribute."""
    index = start
    while index < len(source) and source[index].isspace():
        index += 1
    brace = source.find("{", index)
    semicolon = source.find(";", index)
    if semicolon != -1 and (brace == -1 or semicolon < brace):
        return semicolon + 1
    if brace == -1:
        return len(source)
    depth = 0
    quote: str | None = None
    escaped = False
    for position in range(brace, len(source)):
        char = source[position]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return position + 1
    return len(source)


def _strip_rust_test_items(source: str) -> str:
    """Remove ``cfg(test)`` modules/functions and ``#[test]`` items."""
    result = source
    attribute_pattern = re.compile(r"#\s*\[\s*(?:cfg\s*\(\s*test\s*\)|test)\s*\]")
    while True:
        masked = _strip_rust_comments(result)
        matches = list(attribute_pattern.finditer(masked))
        if not matches:
            return result
        changed = False
        for match in reversed(matches):
            end = _rust_item_end(result, match.end())
            if end <= match.start():
                continue
            replacement = _blank_rust_segment(result[match.start() : end])
            result = result[: match.start()] + replacement + result[end:]
            changed = True
        if not changed:
            return result


def _rust_function_at(source: str, offset: int) -> str:
    matches = list(re.finditer(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)", source[:offset]))
    return matches[-1].group(1) if matches else "<module>"


def _rust_call_argument(source: str, offset: int) -> str:
    """Return one balanced Rust call's argument text."""
    opening = source.find("(", offset)
    if opening == -1:
        return ""
    depth = 0
    quote: str | None = None
    escaped = False
    for position in range(opening, len(source)):
        char = source[position]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : position]
    return ""


def _rust_literal_argument(source: str, offset: int) -> str | None:
    """Return a simple string literal argument, if one is present."""
    argument = _rust_call_argument(source, offset)
    raw_match = re.search(r'(?:br|r)(#+)"(.*?)"\1', argument, flags=re.S)
    if raw_match:
        return raw_match.group(2)
    match = re.search(r'"([^"\\]*(?:\\.[^"\\]*)*)"', argument)
    return match.group(1) if match else None


def _rust_context_is_safe(function: str) -> bool:
    """Recognize safe lifecycle roles by symbol semantics, not file names."""
    lowered = function.lower()
    return any(
        re.search(r"(?:^|_)uv(?:_|$)", lowered) if context == "uv" else context in lowered
        for context in SAFE_LAUNCH_CONTEXTS
    )


def _rust_is_shell_root(function: str) -> bool:
    """Return whether a Rust function is a Shell select/verify/launch root."""
    lowered = function.lower()
    if _rust_context_is_safe(function):
        return False
    return function in SHELL_ROOT_NAMES or any(
        token in lowered for token in ("shell", "presentation", "artifact", "selection")
    )


def _rust_function_body(source: str, function: str) -> str | None:
    """Return one Rust function body using balanced production braces."""
    source_without_comments = _strip_rust_comments(source)
    match = re.search(
        rf"\bfn\s+{re.escape(function)}\s*\([^{{;]*\)\s*(?:->[^{{]+)?\{{",
        source_without_comments,
    )
    if match is None:
        return None
    opening = source_without_comments.find("{", match.start(), match.end())
    end = _rust_item_end(source_without_comments, opening)
    if end <= opening + 1:
        return None
    return source_without_comments[opening + 1 : end - 1]


def _rust_verified_launch_contract(source: str) -> bool:
    """Recognize the signed-catalog/pinned-artifact launch contract."""
    spec = _rust_function_body(source, "verified_launch_spec")
    launcher = _rust_function_body(source, "launch_verified_artifact")
    if spec is None or launcher is None:
        return False
    required_spec_calls = (
        re.search(r"artifact_path\s*\.\s*is_absolute\s*\(\s*\)", spec),
        re.search(r"PathBuf::from\s*\(\s*\"/usr/bin/open\"\s*\)", spec),
        re.search(r"artifact_path\s*\.\s*as_os_str\s*\(\s*\)", spec),
        re.search(r"program\s*:\s*artifact_path\s*\.\s*to_path_buf", spec),
    )
    required_launcher_calls = (
        re.search(r"verified_launch_spec\s*\(", launcher),
        re.search(r"Command::new\s*\(\s*&spec\.program\s*\)", launcher),
        re.search(r"\.args\s*\(\s*&spec\.args\s*\)", launcher),
        re.search(r"\.spawn\s*\(\s*\)", launcher),
    )
    return all(required_spec_calls) and all(required_launcher_calls)


def _rust_call_findings_for_source(path: Path, source: str) -> list[dict[str, Any]]:
    """Inspect only authority env and semantically unverified Shell calls."""
    findings: list[dict[str, Any]] = []
    production_source = _strip_rust_test_items(source)
    stripped = _strip_rust_comments_and_strings(production_source)
    env_pattern = re.compile(r"\b(?:std::)?env::var(?:_os)?\s*\(")
    command_env_pattern = re.compile(r"\.(?:env|envs)\s*\(")
    command_pattern = re.compile(r"\b(?:std::process::)?Command::new\s*\(")
    for match in env_pattern.finditer(stripped):
        function = _rust_function_at(stripped, match.start())
        literal = _rust_literal_argument(production_source, match.start())
        authority_env = literal in AUTHORITY_ENV_NAMES
        shell_env = _rust_is_shell_root(function)
        if not authority_env and not shell_env:
            continue
        if _rust_context_is_safe(function):
            continue
        line = stripped.count("\n", 0, match.start()) + 1
        findings.append(
            _finding(path, line, "launcher_env", symbol=literal or function, function=function)
        )
    for match in command_env_pattern.finditer(stripped):
        function = _rust_function_at(stripped, match.start())
        if not _rust_is_shell_root(function):
            continue
        line = stripped.count("\n", 0, match.start()) + 1
        findings.append(
            _finding(path, line, "launcher_env", symbol="Command.env", function=function)
        )
    for match in command_pattern.finditer(stripped):
        function = _rust_function_at(stripped, match.start())
        if not _rust_is_shell_root(function):
            continue
        literal = _rust_literal_argument(production_source, match.start())
        argument = _rust_call_argument(production_source, match.start()).strip()
        if (
            function == "launch_verified_artifact"
            and argument == "&spec.program"
            and _rust_verified_launch_contract(production_source)
        ):
            continue
        line = stripped.count("\n", 0, match.start()) + 1
        findings.append(
            _finding(
                path,
                line,
                "launcher_direct_command",
                symbol=literal or "Command::new",
                function=function,
            )
        )
    return findings


def _rust_call_findings() -> list[dict[str, Any]]:
    """Find scoped Launcher calls after Rust test/code text is removed."""
    findings: list[dict[str, Any]] = []
    for path in _production_files():
        if path.suffix != ".rs" or "tobkiri_launcher" not in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        findings.extend(_rust_call_findings_for_source(path, source))
    return findings


def _launcher_safety_findings() -> list[dict[str, Any]]:
    """Scan Launcher calls and verify v3 projections against v4 artifacts."""
    findings = _rust_call_findings()
    for path in sorted(ECOSYSTEM.glob("*/rumi.pack.v3.json")):
        value = _load_json(path)
        if not isinstance(value, Mapping):
            continue
        pack_dir = path.parent
        v4 = _load_json(pack_dir / "pack.v4.json")
        v4_functions = v4.get("functions", []) if isinstance(v4, Mapping) else []
        v4_implementation_digests = {
            str(function.get("implementation_digest"))
            for function in v4_functions
            if isinstance(function, Mapping) and function.get("implementation_digest")
        }
        for entry in value.get("entrypoints", []):
            if not isinstance(entry, Mapping) or entry.get("loader") not in {
                "python",
                "process",
            }:
                continue
            module = str(entry.get("module") or "").strip()
            candidate = RUNTIME.joinpath(*module.split(".")).with_suffix(".py") if module else None
            actual = _sha256(candidate) if candidate is not None and candidate.is_file() else ""
            declared = str(entry.get("artifact_hash") or "")
            if (
                not actual
                or declared != actual
                or (v4_implementation_digests and actual not in v4_implementation_digests)
            ):
                findings.append(
                    _finding(
                        path,
                        1,
                        "unverified_v3_entrypoint",
                        loader=entry.get("loader"),
                        module=module,
                        declared=declared,
                        actual=actual,
                    )
                )
        provenance = value.get("provenance")
        ecosystem = _load_json(pack_dir / "ecosystem.json")
        expected_content_hash = _expected_v3_content_hash(pack_dir, value, ecosystem)
        expected_build_identity = _expected_v4_build_identity(v4)
        if not isinstance(provenance, Mapping):
            findings.append(_finding(path, 1, "unverified_v3_provenance"))
            continue
        ecosystem_provenance = (
            ecosystem.get("provenance") if isinstance(ecosystem, Mapping) else None
        )
        if (
            str(provenance.get("content_hash") or "") != expected_content_hash
            or str(provenance.get("build_identity") or "") != expected_build_identity
            or not isinstance(ecosystem_provenance, Mapping)
            or str(ecosystem_provenance.get("content_hash") or "") != expected_content_hash
            or str(ecosystem_provenance.get("build_identity") or "") != expected_build_identity
        ):
            findings.append(
                _finding(
                    path,
                    1,
                    "unverified_v3_provenance",
                    expected_content_hash=expected_content_hash,
                    expected_build_identity=expected_build_identity,
                )
            )
    return sorted(findings, key=lambda item: (item["path"], item["line"], item["rule"]))


def _expected_v4_build_identity(v4: Any) -> str:
    """Return the build identity derived from one canonical v4 artifact."""
    if not isinstance(v4, Mapping):
        return ""
    pack = v4.get("pack")
    if not isinstance(pack, Mapping):
        return ""
    pack_id = str(pack.get("id") or "").strip()
    version = str(pack.get("version") or "").strip()
    return f"{pack_id}:{version}" if pack_id and version else ""


def _expected_v3_content_hash(
    pack_dir: Path,
    manifest: Mapping[str, Any],
    ecosystem: Any,
) -> str:
    """Resolve the actual content hash represented by one v3 projection."""
    metadata = ecosystem.get("metadata") if isinstance(ecosystem, Mapping) else None
    integrity = metadata.get("integrity") if isinstance(metadata, Mapping) else None
    relative = integrity.get("artifact_manifest") if isinstance(integrity, Mapping) else None
    if relative:
        index_path = (pack_dir / str(relative)).resolve()
        if index_path.is_file():
            return _sha256(index_path)
        return ""
    hashes = {
        str(item.get("artifact_hash") or "")
        for item in manifest.get("entrypoints", [])
        if isinstance(item, Mapping) and item.get("artifact_hash")
    }
    if len(hashes) == 1:
        return next(iter(hashes))
    if hashes:
        return ""
    v4 = _load_json(pack_dir / "pack.v4.json")
    pack = v4.get("pack") if isinstance(v4, Mapping) else None
    return str(pack.get("artifact_digest") or "") if isinstance(pack, Mapping) else ""


def _double_authority_findings() -> list[dict[str, Any]]:
    """Detect a reachable legacy inventory authority, not a directory presence."""
    findings: list[dict[str, Any]] = []
    path = RUNTIME / "backend_core" / "ecosystem" / "registry.py"
    tree = _python_tree(path)
    if tree is None:
        return findings
    has_inventory_read = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"iterdir", "glob", "rglob"}
        for node in ast.walk(tree)
    )
    if not has_inventory_read:
        return findings

    registry_module_reached = False
    graph = _reachable_python_trees()
    for source_path, source_tree in graph.items():
        aliases = {"Registry", "get_registry", "reload_registry"}
        for node in ast.walk(source_tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "backend_core.ecosystem.registry":
                        registry_module_reached = True
                        findings.append(
                            _finding(
                                source_path,
                                node.lineno,
                                "double_authority_reachable",
                                symbol=alias.asname or "backend_core.ecosystem.registry",
                                target="backend_core.ecosystem.registry",
                            )
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module == "backend_core.ecosystem.registry":
                    registry_module_reached = True
                    aliases.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name in {"Registry", "get_registry", "reload_registry"}
                    )
                    findings.append(
                        _finding(
                            source_path,
                            node.lineno,
                            "double_authority_reachable",
                            symbol="backend_core.ecosystem.registry",
                            target="backend_core.ecosystem.registry",
                        )
                    )
            elif isinstance(node, ast.Call) and _called_name(node) in aliases:
                registry_module_reached = True
                findings.append(
                    _finding(
                        source_path,
                        node.lineno,
                        "double_authority_reachable",
                        symbol=_called_name(node),
                        target="backend_core.ecosystem.registry",
                    )
                )
    if registry_module_reached:
        findings.extend(
            _finding(
                path,
                node.lineno,
                "double_authority_inventory_read",
                symbol=_called_name(node),
                target="ecosystem directory inventory",
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"iterdir", "glob", "rglob"}
        )
    return findings


def _offline_projection_findings() -> list[dict[str, Any]]:
    """Require finite v4 binding for all legacy compatibility projections."""
    findings: list[dict[str, Any]] = []
    catalog = _load_json(RUNTIME / "schemas" / "manifest_authority.v1.json")
    authorities = catalog.get("packs", {}) if isinstance(catalog, Mapping) else {}
    source_sets = _authority_source_sets()
    for pack_dir in _production_pack_dirs():
        if pack_dir.name in source_sets["v4_only_ids"] or pack_dir.name not in authorities:
            continue
        legacy_path = pack_dir / "ecosystem.json"
        value = _load_json(legacy_path)
        metadata = value.get("metadata") if isinstance(value, Mapping) else None
        generated = metadata.get("generated_from") if isinstance(metadata, Mapping) else None
        authority = authorities.get(pack_dir.name)
        expected = {
            "format": "rumi.ecosystem.v1",
            "generated": True,
            "read_only_projection": True,
            "manifest_authority": "v4-authoritative",
            "projection_owner": "scripts/migrate_manifest_authority.py",
        }
        if (
            authority != "v4-authoritative"
            or not isinstance(metadata, Mapping)
            or any(metadata.get(key) != expected_value for key, expected_value in expected.items())
        ):
            findings.append(_finding(legacy_path, 1, "projection_marker_or_owner_missing"))
            continue
        if (
            not isinstance(generated, Mapping)
            or generated.get("source") != "pack.v4.json"
            or generated.get("generator") != V4_PROJECTION_GENERATOR
        ):
            findings.append(_finding(legacy_path, 1, "projection_source_marker_missing"))
        v4 = _load_json(pack_dir / "pack.v4.json")
        source_identity = (
            v4.get("integrity", {}).get("source_identity") if isinstance(v4, Mapping) else None
        )
        if generated.get("source_content_hash") != source_identity:
            findings.append(_finding(legacy_path, 1, "projection_source_identity_mismatch"))
        v4 = _load_json(pack_dir / "pack.v4.json")
        integrity = v4.get("integrity") if isinstance(v4, Mapping) else None
        canonical_v4 = metadata.get("canonical_v4") if isinstance(metadata, Mapping) else None
        if not isinstance(integrity, Mapping) or not isinstance(canonical_v4, Mapping):
            findings.append(_finding(legacy_path, 1, "canonical_v4_projection_missing"))
            continue
        pack = v4.get("pack") if isinstance(v4, Mapping) else None
        if (
            canonical_v4.get("artifact") != "pack.v4.json"
            or canonical_v4.get("generator") != V4_PROJECTION_GENERATOR
            or canonical_v4.get("source_identity") != integrity.get("source_identity")
            or canonical_v4.get("artifact_digest")
            != (pack.get("artifact_digest") if isinstance(pack, Mapping) else None)
        ):
            findings.append(
                _finding(
                    legacy_path,
                    1,
                    "canonical_v4_projection_identity_mismatch",
                )
            )
    return findings


def _source_identity(path: Path) -> str:
    """Compute the canonical v3 source identity through the offline helper."""
    from scripts.offline_legacy_projection import source_manifest_identity

    value = _load_json(path)
    return source_manifest_identity(value) if isinstance(value, Mapping) else ""


def _audit_snapshot() -> dict[str, Any]:
    """Collect deterministic current-tree evidence with no baseline or skip."""
    pack_dirs = _production_pack_dirs()
    artifact_findings = _v4_artifact_findings()
    authority_findings = _authority_resolved_plan_findings()
    legacy_findings = _ast_legacy_runtime_findings()
    bypass_findings = _ast_authority_bypass_findings()
    projection_calls = _ast_projection_findings()
    fallback_findings = _ast_fallback_findings()
    old_composition = _ast_old_composition_findings()
    double_authority = _double_authority_findings()
    launcher = _launcher_safety_findings()
    projection = _offline_projection_findings()
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source_sets = _authority_source_sets()
    gates = {
        "artifact_contracts": artifact_findings,
        "authority_resolved_plan_scope": authority_findings,
        "legacy_registry_and_installed_lookup": legacy_findings
        + bypass_findings
        + old_composition
        + fallback_findings
        + projection_calls,
        "double_authority": double_authority,
        "launcher_safety": launcher,
        "offline_projection": projection,
    }
    return {
        "schema": "io.tobkiri.quality.complete-v4-migration.v2",
        "head_sha": head_sha,
        "gate": {
            "status": "GREEN" if all(not findings for findings in gates.values()) else "RED",
            "clean": all(not findings for findings in gates.values()),
            "expected_green": {name: 0 for name in gates},
        },
        "gates": {
            name: {"status": "GREEN" if not findings else "RED", "findings": findings}
            for name, findings in gates.items()
        },
        "pack_inventory": {
            "production_pack_directories": len(pack_dirs),
            "expected_production_pack_directories": EXPECTED_PRODUCTION_PACK_COUNT,
            "v4_artifacts_per_pack": len(PACK_ARTIFACTS),
            "v4_artifact_files": len(pack_dirs) * len(PACK_ARTIFACTS),
            "v4_pack_artifacts": [_relative(path) for path in _v4_pack_artifacts()],
            "v4_profile_artifacts": [_relative(path) for path in _v4_profile_artifacts()],
            "authority_counts": dict(sorted(_manifest_authority_counts()[0].items())),
            "authority_records": _manifest_authority_counts()[1],
            "authority_source_sets": {name: sorted(values) for name, values in source_sets.items()},
            "canonical_source_ids": sorted(
                source_sets["manifest_ids"] | source_sets["v4_only_ids"]
            ),
        },
        "findings": gates,
    }


def _assert_zero(name: str, findings: list[dict[str, Any]]) -> None:
    """Fail with deterministic evidence and never baseline a finding."""
    assert not findings, f"{name} RED: count={len(findings)} evidence={findings[:8]}"


def test_production_v4_pack_and_profile_artifacts_are_complete() -> None:
    """The exact direct artifact set is 143 Packs x 4 compiler inputs."""
    assert len(_production_pack_dirs()) == EXPECTED_PRODUCTION_PACK_COUNT
    assert len(_v4_pack_artifacts()) == EXPECTED_PRODUCTION_PACK_COUNT
    assert len(_v4_pack_artifacts()) * len(PACK_ARTIFACTS) == 572
    _assert_zero("v4 artifact contracts", _v4_artifact_findings())


def test_authority_and_resolved_plan_scope_is_exact() -> None:
    """Authority ownership and ResolvedPlan fields remain exact and finite."""
    assert _source_set_delta({"manifest.pack"}, set()) == {
        "missing": ["manifest.pack"],
        "extra": [],
    }
    assert _source_set_delta(set(), {"injected.pack"}) == {
        "missing": [],
        "extra": ["injected.pack"],
    }
    _assert_zero("Authority/ResolvedPlan scope", _authority_resolved_plan_findings())


def test_legacy_registry_and_installed_lookup_are_zero() -> None:
    """Legacy Registry, runtime inventory lookup, bypass, and projection calls are zero."""
    legacy_fixture = ast.parse(
        """
from backend_core.ecosystem.registry import Registry

def dispatch(registry):
    return registry.all_installed()
""",
        filename="legacy_fixture.py",
    )
    fixture_findings = _ast_legacy_runtime_findings_for_tree(
        Path("legacy_fixture.py"), legacy_fixture
    )
    assert any(item["rule"] == "runtime_installed_lookup" for item in fixture_findings)
    retired_entry_fixture = ast.parse(
        """
from core_runtime.setup_pack import SetupPackManager
from core_runtime.global_contracts.manifest import load_manifest
from ecosystem.setup_pack.pack_selector import PackSelector
""",
        filename="retired_entry_fixture.py",
    )
    retired_findings = _ast_legacy_runtime_findings_for_tree(
        Path("retired_entry_fixture.py"), retired_entry_fixture
    )
    assert {item["module"] for item in retired_findings} == {
        "core_runtime.global_contracts.manifest",
        "core_runtime.setup_pack",
        "ecosystem.setup_pack.pack_selector",
    }
    _assert_zero(
        "legacy Registry/all-installed runtime lookup",
        _ast_legacy_runtime_findings()
        + _ast_authority_bypass_findings()
        + _ast_old_composition_findings()
        + _ast_fallback_findings()
        + _ast_projection_findings(),
    )


def test_retired_profile_setup_and_direct_execution_modules_are_unreachable() -> None:
    """Canonical production entries cannot import legacy state or executors."""

    reachable = set(_reachable_python_trees())
    assert reachable.isdisjoint(RETIRED_PROFILE_AND_EXECUTION_MODULES), sorted(
        _relative(path) for path in reachable & RETIRED_PROFILE_AND_EXECUTION_MODULES
    )


def test_pack_api_has_no_hardcoded_legacy_handler_reachability() -> None:
    """The production HTTP adapter contains no legacy route implementation."""

    _assert_zero(
        "hard-coded legacy Pack API reachability",
        _pack_api_hardcoded_legacy_findings(),
    )


def _child_output_for_diagnostic(value: object) -> str | None:
    """Convert subprocess output to stable text, including timeout output."""

    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _child_failure_diagnostic(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    returncode: int | None,
    stdout: object,
    stderr: object,
) -> str:
    """Format exact child process context without exposing unrelated secrets."""

    payload = {
        "command": command,
        "cwd": str(cwd),
        "environment": {
            key: environment.get(key) for key in _CHILD_DIAGNOSTIC_ENV_KEYS
        },
        "returncode": returncode,
        "stdout": _child_output_for_diagnostic(stdout),
        "stderr": _child_output_for_diagnostic(stderr),
    }
    return _CHILD_FAILURE_DIAGNOSTIC_PREFIX + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    )


def _child_failure_payload(diagnostic: str) -> Mapping[str, Any]:
    """Decode the structured JSON payload from a child failure diagnostic."""

    if not diagnostic.startswith(_CHILD_FAILURE_DIAGNOSTIC_PREFIX):
        raise AssertionError("child failure diagnostic prefix is missing")
    payload = json.loads(diagnostic[len(_CHILD_FAILURE_DIAGNOSTIC_PREFIX) :])
    if not isinstance(payload, dict):
        raise AssertionError("child failure diagnostic payload is not an object")
    return payload


def _windows_path_is_within_root(
    candidate: str | Path,
    root: str | Path,
) -> bool:
    """Check Windows drive/UNC containment without string-prefix matching."""

    candidate_text = str(candidate)
    root_text = str(root)
    if not ntpath.isabs(candidate_text) or not ntpath.isabs(root_text):
        return False
    candidate_normalized = ntpath.normcase(ntpath.normpath(candidate_text))
    root_normalized = ntpath.normcase(ntpath.normpath(root_text))
    try:
        return ntpath.commonpath((candidate_normalized, root_normalized)) == root_normalized
    except ValueError:
        return False


def _test_owned_path_is_within_root(candidate: str | Path, root: Path) -> bool:
    """Use the current host's path semantics for test-owned child paths."""

    if os.name == "nt":
        return _windows_path_is_within_root(candidate, root)
    candidate_path = Path(candidate)
    if not candidate_path.is_absolute() or not root.is_absolute():
        return False
    try:
        candidate_path.relative_to(root)
    except ValueError:
        return False
    return True


def test_child_failure_diagnostic_preserves_exact_process_context(tmp_path: Path) -> None:
    """The child failure path must retain command, environment, and both streams."""

    command = ["python.exe", "-c", "raise SystemExit(7)"]
    environment = {
        "TOBKIRI_USER_DATA": str(tmp_path / "fresh-home"),
        "RUMI_USER_DATA": str(tmp_path / "fresh-home"),
        "PATH": r"C:\hostedtoolcache\windows\Python\3.11.9\x64",
    }

    diagnostic = _child_failure_diagnostic(
        command,
        cwd=tmp_path,
        environment=environment,
        returncode=7,
        stdout="child stdout",
        stderr="Traceback: child stderr",
    )

    payload = _child_failure_payload(diagnostic)
    environment_payload = payload["environment"]
    assert isinstance(environment_payload, Mapping)
    assert payload["command"] == command
    assert _test_owned_path_is_within_root(payload["cwd"], tmp_path)
    assert _test_owned_path_is_within_root(
        environment_payload["TOBKIRI_USER_DATA"], tmp_path / "fresh-home"
    )
    assert _test_owned_path_is_within_root(
        environment_payload["RUMI_USER_DATA"], tmp_path / "fresh-home"
    )
    assert environment_payload["PATH"] == r"C:\hostedtoolcache\windows\Python\3.11.9\x64"
    assert payload["returncode"] == 7
    assert payload["stdout"] == "child stdout"
    assert payload["stderr"] == "Traceback: child stderr"


@pytest.mark.parametrize(
    ("root", "candidate", "expected"),
    [
        (
            r"C:\Users\runneradmin\AppData\Local\Temp\fresh home",
            r"C:\Users\runneradmin\AppData\Local\Temp\fresh home\child\日本語.txt",
            True,
        ),
        (
            r"C:\Users\runneradmin\AppData\Local\Temp\fresh home",
            r"C:\Users\runneradmin\AppData\Local\Temp\fresh homepage\child",
            False,
        ),
        (
            r"\\server\share\pytest root\新しい home",
            r"\\server\share\pytest root\新しい home\fresh-home\state.json",
            True,
        ),
        (
            r"\\server\share\pytest root\新しい home",
            r"\\other-server\share\pytest root\新しい home\fresh-home\state.json",
            False,
        ),
        (
            r"C:\Users\runneradmin\AppData\Local\Temp\fresh home",
            r"C:\Users\runneradmin\AppData\Local\Temp\fresh home\child\..\state.json",
            True,
        ),
        (
            r"C:\Users\runneradmin\AppData\Local\Temp\fresh home",
            r"D:\Users\runneradmin\AppData\Local\Temp\fresh home\state.json",
            False,
        ),
    ],
)
def test_windows_child_paths_decode_json_before_root_containment(
    root: str,
    candidate: str,
    expected: bool,
) -> None:
    """Escaped Windows paths are checked as ntpath values, not JSON text."""

    encoded = json.dumps({"path": candidate}, ensure_ascii=False)
    decoded = json.loads(encoded)

    assert "\\\\" in encoded
    assert decoded["path"] == candidate
    assert _windows_path_is_within_root(decoded["path"], root) is expected


def test_fresh_home_legacy_api_probes_are_retired_without_manager_imports(
    tmp_path: Path,
) -> None:
    """A clean local server yields only typed retirement for legacy probes."""

    script = r"""
import http.client
import json
import os
import sys
import traceback

from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager

observed = []
server = None
current_probe = {"phase": "construct"}
try:
    os.environ["RUMI_API_BIND_ADDRESS"] = "0.0.0.0"
    os.environ["RUMI_ALLOW_LEGACY_REMOTE_BEARER"] = "1"
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="verified-local"),
    )
    for cycle in (1, 2):
        current_probe = {"cycle": cycle, "phase": "server.start"}
        server.start()
        try:
            for method, path in (
                ("GET", "/api/packs"),
                ("GET", "/api/authority/events"),
                ("GET", "/api/runtime/available"),
                ("POST", "/api/packs/scan"),
                ("POST", "/api/routes/reload"),
                ("POST", "/api/v4/dispatch"),
                ("GET", "/api/flows"),
                ("POST", "/api/flows/legacy/run"),
                ("POST", "/api/executors/python-file-call"),
                ("POST", "/api/blocks/python-file-call"),
                ("POST", "/api/functions/direct-invoke"),
                ("GET", "/api/setup/complete"),
                ("POST", "/api/setup/complete"),
                ("PUT", "/api/setup/complete"),
                ("PATCH", "/api/setup/complete"),
                ("DELETE", "/api/setup/complete"),
                ("OPTIONS", "/api/setup/complete"),
                ("HEAD", "/api/setup/complete"),
                ("GET", "/api/setup/complete?probe=fresh-home"),
            ):
                current_probe = {
                    "cycle": cycle,
                    "method": method,
                    "path": path,
                }
                connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
                connection.request(
                    method,
                    path,
                    body="{}" if method in {"POST", "PUT", "PATCH", "DELETE", "OPTIONS"} else None,
                    headers={
                        "Authorization": "Bearer fresh-valid-internal-token",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                raw = response.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                observed.append(
                    (
                        cycle,
                        method,
                        path,
                        response.status,
                        payload.get("data", {}).get("state"),
                        payload.get("data", {}).get("write_set"),
                    )
                )
                connection.close()
        finally:
            current_probe = {"cycle": cycle, "phase": "server.stop"}
            server.stop()
except BaseException as error:
    print(
        json.dumps(
            {
                "child_error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
                "current_probe": current_probe,
                "observed_count": len(observed),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    traceback.print_exc()
    raise
blocked_modules = sorted(
    name
    for name in sys.modules
    if name in {
        "core_runtime.approval_manager",
        "core_runtime.capability_executor",
        "core_runtime.ecosystem_nodes",
        "core_runtime.manifest_loader",
        "core_runtime.api.control_panel_handlers",
        "core_runtime.api.router_table",
        "core_runtime.global_contracts.manifest",
        "core_runtime.api.flow_handlers",
        "core_runtime.kernel_flow_execution",
        "core_runtime.kernel_handlers_runtime",
        "core_runtime.python_file_executor",
        "core_runtime.resolved_profile",
        "core_runtime.runtime_profile_resolver",
        "core_runtime.setup_pack",
        "ecosystem.setup_pack.pack_selector",
    }
)
print(
    json.dumps(
        {
            "observed": observed,
            "blocked_modules": blocked_modules,
            "bound_host": server.host,
        }
    )
)
"""
    command = [sys.executable, "-c", script]
    fresh_user_data = tmp_path / "fresh-home"
    child_environment = os.environ.copy()
    child_environment.update(
        {
            "TOBKIRI_USER_DATA": str(fresh_user_data),
            "RUMI_USER_DATA": str(fresh_user_data),
            "RUMI_SANDBOX_LIMA_STATE": str(
                fresh_user_data / "sandbox" / "lima-runtime.json"
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(tmp_path / "python-cache"),
        }
    )
    try:
        completed = subprocess.run(
            command,
            cwd=RUNTIME,
            env=child_environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            _child_failure_diagnostic(
                command,
                cwd=RUNTIME,
                environment=child_environment,
                returncode=None,
                stdout=error.stdout,
                stderr=error.stderr,
            )
        ) from error
    assert completed.returncode == 0, _child_failure_diagnostic(
        command,
        cwd=RUNTIME,
        environment=child_environment,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    evidence = json.loads(completed.stdout)
    assert evidence["blocked_modules"] == []
    assert evidence["bound_host"] == "127.0.0.1"
    expected_cycle = [
        [1, "GET", "/api/packs", 410, "legacy_api_retired", []],
        [1, "GET", "/api/authority/events", 410, "legacy_api_retired", []],
        [1, "GET", "/api/runtime/available", 410, "legacy_api_retired", []],
        [1, "POST", "/api/packs/scan", 410, "legacy_api_retired", []],
        [1, "POST", "/api/routes/reload", 410, "legacy_api_retired", []],
        [1, "POST", "/api/v4/dispatch", 410, "legacy_api_retired", []],
        [1, "GET", "/api/flows", 410, "legacy_api_retired", []],
        [1, "POST", "/api/flows/legacy/run", 410, "legacy_api_retired", []],
        [1, "POST", "/api/executors/python-file-call", 410, "legacy_api_retired", []],
        [1, "POST", "/api/blocks/python-file-call", 410, "legacy_api_retired", []],
        [1, "POST", "/api/functions/direct-invoke", 410, "legacy_api_retired", []],
        [1, "GET", "/api/setup/complete", 410, "legacy_setup_retired", []],
        [1, "POST", "/api/setup/complete", 410, "legacy_setup_retired", []],
        [1, "PUT", "/api/setup/complete", 410, "legacy_setup_retired", []],
        [1, "PATCH", "/api/setup/complete", 410, "legacy_setup_retired", []],
        [1, "DELETE", "/api/setup/complete", 410, "legacy_setup_retired", []],
        [1, "OPTIONS", "/api/setup/complete", 410, "legacy_setup_retired", []],
        [1, "HEAD", "/api/setup/complete", 410, None, None],
        [
            1,
            "GET",
            "/api/setup/complete?probe=fresh-home",
            410,
            "legacy_setup_retired",
            [],
        ],
    ]
    assert evidence["observed"] == expected_cycle + [[2, *item[1:]] for item in expected_cycle]


def test_double_authority_is_zero_by_production_reachability() -> None:
    """Directory presence alone is not a double-authority finding."""
    _assert_zero("double authority", _double_authority_findings())


def test_retired_setup_functions_and_conformance_pack_are_not_production_packs() -> None:
    assert not (RUNTIME / "bootstrap.py").exists()
    assert not (RUNTIME / "rumi_setup").exists()
    assert not (RUNTIME / "tobkiri_host" / "conformance").exists()
    lifecycle_source = (
        RUNTIME / "core_runtime" / "app_lifecycle_manager.py"
    ).read_text(encoding="utf-8")
    assert "setup_pack_selection.json" not in lifecycle_source
    functions_root = ECOSYSTEM / "defaultspack" / "functions"
    for function_id in (
        "list_setup_packs",
        "install_setup_pack",
        "grant_all_ok",
        "revoke_all_ok",
    ):
        assert not (functions_root / function_id / "manifest.json").exists()
        assert not (functions_root / function_id / "main.py").exists()
    assert not (ECOSYSTEM / "conformance_minimal_echo_pack").exists()
    assert (
        RUNTIME
        / "tests"
        / "fixtures"
        / "conformance_minimal_echo_pack"
        / "pack.v4.json"
    ).is_file()


def test_launcher_env_path_direct_and_unverified_fallback_are_zero() -> None:
    """Launcher has no unscoped environment, process, or unverified entrypoint path."""
    launcher_fixture = """
#[cfg(test)]
fn test_fallback() {
    std::process::Command::new("sh").spawn();
    std::env::var("PATH");
}

fn launch_shell() {
    std::process::Command::new("sh").env("PATH", "/tmp").spawn();
    std::env::var("PATH");
}

fn host_broker_lifecycle() {
    std::process::Command::new("python").spawn();
    std::env::var("RUMI_VIEWER_HOST_BROKER_CONNECTION");
}

fn uv_lifecycle() {
    std::env::var_os("PATH");
}

fn defaultspack_lifecycle() {
    std::process::Command::new("python").spawn();
    std::env::var("RUMI_DEFAULTSPACK_DEBUG_RUN_ID");
}

fn codesign() {
    std::process::Command::new("/usr/bin/codesign").status();
}

fn launchservices() {
    std::process::Command::new("lsregister").status();
}
"""
    fixture_findings = _rust_call_findings_for_source(Path("launcher_fixture.rs"), launcher_fixture)
    assert [item["function"] for item in fixture_findings] == [
        "launch_shell",
        "launch_shell",
        "launch_shell",
    ]
    presentation_path = ROOT / "tobkiri_launcher" / "src-tauri" / "src" / "presentation.rs"
    presentation_findings = _rust_call_findings_for_source(
        presentation_path, presentation_path.read_text(encoding="utf-8")
    )
    assert not [
        item for item in presentation_findings if item["function"] == "launch_verified_artifact"
    ]
    bad_launch = _rust_call_findings_for_source(
        Path("bad_launch_fixture.rs"),
        'fn launch_verified_artifact() { std::process::Command::new("sh").spawn(); }',
    )
    assert [item["rule"] for item in bad_launch] == ["launcher_direct_command"]
    _assert_zero("Launcher env/PATH/direct/unverified fallback", _launcher_safety_findings())


def test_offline_projection_has_marker_owner_and_source_identity() -> None:
    """Every v3 projection carries its offline marker, owner, and source identity."""
    _assert_zero("offline projection marker/owner/source identity", _offline_projection_findings())


def test_v4_runtime_and_protocol_composition_apis_are_live() -> None:
    """The live check uses runtime_v4 and protocol composition, never pack_architecture."""
    pack_root = RUNTIME / "ecosystem" / "defaultspack"
    if str(pack_root) not in sys.path:
        sys.path.insert(0, str(pack_root))
    from domain.runtime_v4 import BundledCatalog, ResolvedDefaultProfile, resolve_default_profile
    from tobkiri_protocol.composition import compose_runtime_profile, load_verified_catalog

    assert BundledCatalog is not None
    assert ResolvedDefaultProfile is not None
    assert callable(resolve_default_profile)
    assert callable(compose_runtime_profile)
    assert callable(load_verified_catalog)


def test_current_sha_green_evidence_reports_no_findings() -> None:
    """Current SHA evidence is measured directly and is green after migration."""
    report = _audit_snapshot()
    expected_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    assert report["head_sha"] == expected_head
    assert report["gate"]["status"] == "GREEN"
    assert report["gate"]["clean"] is True
    assert report["pack_inventory"]["production_pack_directories"] == 143
    assert report["pack_inventory"]["v4_artifact_files"] == 572
