"""Maintain an exhaustive, evidence-based migration ledger for issue #1520.

This is development tooling. It does not run in the distributed application.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LEDGER = Path(__file__).with_name("inventory.json")
IGNORED = {"__pycache__", ".pytest_cache", "node_modules", ".git", "target"}
EXECUTABLE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".sh", ".dart"}
NATIVE_IMPORTS = {
    "sqlite3", "ssl", "zlib", "cryptography", "pydantic_core", "lxml", "PIL",
    "psutil", "socket", "threading", "multiprocessing", "ctypes", "pty", "fcntl",
    "subprocess", "requests", "httpx", "aiohttp", "os",
}
TERMINAL_STATUSES = {"ported+parity", "core+vectors", "retired+approved"}
TARGETS = {"python-wasm", "data-only", "rust-wasm", "native", "core", "retired"}


def source_files(path: Path) -> list[Path]:
    """List source files while excluding disposable build and cache directories."""
    return sorted(
        item for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED for part in item.parts)
    )


def native_imports(files: list[Path]) -> list[str]:
    """Record imported modules that need a wasm compatibility decision."""
    found: set[str] = set()
    for source in files:
        if source.suffix != ".py":
            continue
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (UnicodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            )
            found.update(name.split(".")[0] for name in names if name.split(".")[0] in NATIVE_IMPORTS)
    return sorted(found)


def row(path: Path, kind: str, files: list[Path] | None = None,
        *, root: Path = ROOT) -> dict[str, Any]:
    """Create one unported row; executable legacy units default to Python wasm."""
    files = files if files is not None else source_files(path)
    executable = any(item.suffix in EXECUTABLE_SUFFIXES for item in files)
    relative = path.relative_to(root).as_posix()
    digest = hashlib.sha256()
    for source in files:
        digest.update(source.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(source.read_bytes()).digest())
    return {
        "legacy_unit": relative,
        "kind": kind,
        "source_files": len(files),
        "source_tree_sha256": digest.hexdigest(),
        "target": "python-wasm" if executable else "data-only",
        "replacement": None,
        "shapes": [],
        "parity_fixture": None,
        "status": "todo",
        "native_imports": native_imports(files),
        "evidence": [],
    }


def discover(root: Path = ROOT) -> list[dict[str, Any]]:
    """Cover every ecosystem directory, Defaults domain, and Python core module."""
    runtime = root / "tobkiri_runtime"
    ecosystem = runtime / "ecosystem"
    rows = [row(path, "ecosystem-directory", root=root)
            for path in sorted(ecosystem.iterdir() if ecosystem.is_dir() else ())
            if path.is_dir() and path.name not in IGNORED]
    defaults = ecosystem / "defaultspack"
    domain = defaults / "domain"
    rows.extend(row(path, "defaults-domain", root=root)
                for path in sorted(domain.iterdir() if domain.is_dir() else ())
                if path.is_dir() and path.name not in IGNORED)
    # Domain files outside domain subdirectories are represented explicitly too.
    rows.extend(row(path, "defaults-domain-module", [path], root=root)
                for path in sorted(domain.glob("*.py")))
    # The parent row remains an aggregate; other Defaults surfaces are separate
    # units so frontend/transport/blocks never disappear from the ledger.
    rows.extend(row(path, "defaults-surface", root=root)
                for path in sorted(defaults.iterdir() if defaults.is_dir() else ())
                if path.is_dir() and path.name not in IGNORED | {"domain"})
    for package in ("core_runtime", "tobkiri_host", "tobkiri_protocol"):
        for source in source_files(runtime / package):
            if source.suffix == ".py":
                item = row(source, "runtime-module", [source], root=root)
                # Names alone cannot justify moving domain behavior into core.
                # The generic mechanisms explicitly enumerated by Opus get a
                # provisional Rust target; every other module keeps R11.2's
                # Python-on-wasm default until its dependency analysis is done.
                mechanism = any(part in source.relative_to(runtime / package).as_posix()
                                for part in ("canonical", "signature", "journal", "authority/",
                                             "workflow_v4", "audit", "pack_trust", "pack_artifact_integrity"))
                if mechanism:
                    item["target"] = "core"
                item["target_provisional"] = True
                rows.append(item)
    return sorted(rows, key=lambda item: item["legacy_unit"])


def refresh() -> dict[str, Any]:
    """Refresh discovered units without discarding recorded parity evidence."""
    previous = json.loads(LEDGER.read_text()) if LEDGER.exists() else {"units": []}
    old = {item["legacy_unit"]: item for item in previous["units"]}
    units = discover()
    seen = set()
    for item in units:
        seen.add(item["legacy_unit"])
        prior = old.get(item["legacy_unit"])
        if prior:
            for name in ("target", "target_provisional", "replacement", "shapes", "parity_fixture", "status", "evidence", "human_approved", "retirement_reason"):
                if name in prior:
                    item[name] = prior[name]
            if prior["source_tree_sha256"] != item["source_tree_sha256"]:
                item["status"] = "todo"
                item["source_changed"] = True
                if "target_provisional" in item:
                    item["target_provisional"] = True
    # A missing source is not retirement authorization and must remain visible.
    units.extend({**item, "source_missing": True} for key, item in old.items() if key not in seen)
    result = {
        "format": "tobkiri.migration-inventory/1",
        "issue": 1520,
        "default_policy": "port every legacy feature; no deletion until all units have evidence",
        "new_harness_is_legacy_parity": False,
        "units": sorted(units, key=lambda item: item["legacy_unit"]),
    }
    LEDGER.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return result


def validate(inventory: dict[str, Any], *, deletion: bool = False,
             root: Path | None = None) -> list[str]:
    """Reject missing units, unsupported claims, and unevidenced deletion."""
    errors = []
    units = inventory.get("units", [])
    if not units:
        return ["migration inventory has no units"]
    repository = ROOT if root is None else root
    names = [item["legacy_unit"] for item in units]
    if len(names) != len(set(names)):
        errors.append("duplicate legacy unit")
    current = {item["legacy_unit"]: item for item in (
        discover() if root is None else discover(repository)
    )}
    for missing in sorted(current.keys() - set(names)):
        errors.append(f"missing legacy unit: {missing}")
    for item in units:
        name = item["legacy_unit"]
        if item.get("target") not in TARGETS:
            errors.append(f"{name}: unsupported target")
        if item.get("target") == "retired" and not item.get("human_approved"):
            errors.append(f"{name}: retirement needs human approval")
        status = item.get("status")
        if status not in TERMINAL_STATUSES | {"todo"}:
            errors.append(f"{name}: unsupported migration status {status!r}")
        if status in TERMINAL_STATUSES and not item.get("evidence"):
            errors.append(f"{name}: completion needs evidence")
        # Refresh invalidates changed sources, but validation must not depend
        # on the caller running the writer first. TODO rows may retain stale
        # historical evidence without claiming it certifies the current bytes.
        if (status in TERMINAL_STATUSES and name in current
                and item.get("source_tree_sha256")
                != current[name]["source_tree_sha256"]):
            errors.append(f"{name}: completed source differs from recorded source_tree_sha256")
        # Removed legacy sources intentionally keep their rows and evidence.
        # Their absence is not fresh retirement permission, but does not undo
        # an already evidenced replacement merely because deletion occurred.
        if status == "ported+parity" and not item.get("parity_fixture"):
            errors.append(f"{name}: parity fixture missing")
        if status == "core+vectors" and item.get("target") != "core":
            errors.append(f"{name}: core vectors require a core target")
        if status == "retired+approved" and not item.get("human_approved"):
            errors.append(f"{name}: retirement approval missing")
        if deletion and status not in TERMINAL_STATUSES:
            errors.append(f"{name}: unresolved ({status})")
        for evidence in item.get("evidence", []):
            candidate = repository / evidence
            if (not candidate.resolve().is_relative_to(repository.resolve())
                    or not candidate.is_file()):
                errors.append(f"{name}: evidence missing or outside repository: {evidence}")
        if fixture := item.get("parity_fixture"):
            candidate = repository / fixture
            if (not candidate.resolve().is_relative_to(repository.resolve())
                    or not candidate.is_file()):
                errors.append(f"{name}: parity fixture missing or outside repository: {fixture}")
    return errors


def main() -> None:
    """Refresh or check the ledger; deletion mode deliberately fails on todo."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--require-deletion-ready", action="store_true")
    args = parser.parse_args()
    inventory = refresh() if args.refresh else json.loads(LEDGER.read_text())
    errors = validate(inventory, deletion=args.require_deletion_ready)
    if errors:
        raise SystemExit("\n".join(errors))
    print(json.dumps({"units": len(inventory["units"]), "deletion_ready": all(
        item["status"] in TERMINAL_STATUSES for item in inventory["units"]
    )}, sort_keys=True))


if __name__ == "__main__":
    main()
