#!/usr/bin/env python3
"""Check the new runtime boundary and the explicit final migration gate."""
from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

MECHANISMS = (
    "canon", "wit", "manifest", "profile", "flow", "gate", "store",
    "wasm", "core", "cli",
)
INTERNAL_DEPENDENCIES = {
    "canon": set(),
    "wit": set(),
    "gate": {"canon"},
    "store": {"canon", "gate"},
    "manifest": {"canon", "gate", "wit"},
    "profile": {"canon", "gate", "manifest"},
    "flow": {"canon", "gate", "profile"},
    "wasm": {"canon", "gate", "store", "manifest", "profile", "flow", "wit"},
    "core": set(MECHANISMS) - {"core", "cli"},
    "cli": {"core"},
}

# These expressions require `test`; `any(test, unix)` must remain visible.
TEST_ONLY_CFG = (
    r"#\[\s*cfg\(\s*(?:test|all\(\s*test\s*,\s*"
    r"(?:unix|windows)\s*\))\s*\)\s*\]"
)


def without_unit_tests(text: str) -> str:
    """Remove bounded cfg(test) items, preserving code after the test module."""
    tokens = re.compile(
        r'(?P<raw>(?:br|r)(?P<hash>#{0,16})".*?"(?P=hash))'
        r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])\''
        r'|//[^\n]*|/\*.*?\*/', re.S,
    )
    masked = tokens.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)
    result = list(text)
    for match in re.finditer(TEST_ONLY_CFG, masked):
        start = match.start()
        opening = masked.find("{", match.end())
        semicolon = masked.find(";", match.end())
        if semicolon >= 0 and (opening < 0 or semicolon < opening):
            result[start:semicolon + 1] = ["\n" if c == "\n" else " " for c in text[start:semicolon + 1]]
            continue
        if opening < 0:
            continue
        depth = 1
        end = opening + 1
        while end < len(masked) and depth:
            depth += (masked[end] == "{") - (masked[end] == "}")
            end += 1
        if depth:
            continue
        result[start:end] = ["\n" if c == "\n" else " " for c in text[start:end]]
    return "".join(result)


def known_pack_labels(root: Path) -> set[str]:
    labels: set[str] = set()
    for path in (root / "packs").rglob("pack.json"):
        manifest = json.loads(path.read_text())
        labels.add(manifest["id"])
        labels.update(s["name"] for s in manifest.get("shapes", []))
    # Carry the pre-migration labels while hosts still use legacy manifests.
    # Otherwise a host can keep a legacy domain dependency without matching
    # any of the newly authored local development Packs.
    for path in (root / "tobkiri_runtime" / "ecosystem").rglob("*.pack.v4.json"):
        manifest = json.loads(path.read_text())
        if label := manifest.get("pack", {}).get("id"):
            labels.add(label)
        labels.update(item["contract_id"] for item in manifest.get("contracts", []))
    for path in (root / "tobkiri_runtime" / "ecosystem").glob("*/pack.v4.json"):
        manifest = json.loads(path.read_text())
        if label := manifest.get("pack", {}).get("id"):
            labels.add(label)
        labels.update(item["contract_id"] for item in manifest.get("contracts", []))
    return labels


def check_sources(root: Path, *, complete: bool = False) -> list[str]:
    errors: list[str] = []
    labels = known_pack_labels(root)
    dirs = [root / "crates" / f"tobkiri-{n}" / "src" for n in MECHANISMS]
    dirs.extend((root / "tobkiri_launcher/core-host/src",
                 root / "tobkiri_launcher/frontend/src/core-host"))
    if complete:
        dirs.append(root / "tobkiri_launcher" / "src-tauri" / "src")
        dirs.append(root / "tobkiri_launcher" / "frontend" / "src")
    test_only_files: set[Path] = set()
    for directory in dirs:
        for path in directory.rglob("*.rs"):
            for match in re.finditer(TEST_ONLY_CFG + r'\s*(?:#\[path\s*=\s*"([^"\n]+)"\]\s*)?mod\s+(\w+)\s*;', path.read_text()):
                candidate = path.with_name(match.group(1) or match.group(2) + ".rs")
                # A test filename alone never exempts production code.
                name = re.compile(r"\bmod\s+" + re.escape(match.group(2)) + r"\s*;")
                if not name.search(without_unit_tests(path.read_text())):
                    test_only_files.add(candidate)
    constructors = re.compile(
        r'(?:PackId|ShapeName|ShapeRef)::(?:new|parse)\(\s*"([^"\n]+)"'
    )
    listener = re.compile(r"\b(?:TcpListener|UdpSocket)\b|\baxum::serve\b|\bhyper::server\b")
    for directory in dirs:
        for path in directory.rglob("*"):
            if path.suffix not in {".rs", ".js", ".jsx", ".ts", ".tsx"}:
                continue
            if path in test_only_files:
                continue
            source = without_unit_tests(path.read_text())
            relative = str(path.relative_to(root))
            for label in sorted(labels):
                if label in source:
                    errors.append(f"{relative}: Pack-owned label {label!r} in a mechanism")
            for match in constructors.finditer(source):
                errors.append(f"{relative}: literal Pack/shape constructor {match.group(1)!r}")
            if listener.search(source):
                errors.append(f"{relative}: runtime listening socket/server")
            if path.is_relative_to(root / "crates" / "tobkiri-flow"):
                for forbidden in ("wasmtime", "rusqlite", "reqwest", "tokio::process", "std::process"):
                    if forbidden in source:
                        errors.append(f"{relative}: Flow imports effect implementation {forbidden}")
    gate = root / "crates" / "tobkiri-gate" / "Cargo.toml"
    if gate.exists():
        defaults = tomllib.loads(gate.read_text()).get("features", {}).get("default", [])
        if "test-gate" in defaults:
            errors.append("tobkiri-gate: test-gate must never be a default feature")
    return errors


def check_dependencies(root: Path) -> list[str]:
    """Enforce the frozen internal DAG in production dependencies only."""
    errors: list[str] = []
    manifests = [(root / "crates" / f"tobkiri-{name}" / "Cargo.toml", allowed)
                 for name, allowed in INTERNAL_DEPENDENCIES.items()]
    manifests.append((root / "tobkiri_launcher/core-host/Cargo.toml", {"core"}))
    for path, allowed in manifests:
        if not path.exists():
            continue
        manifest = tomllib.loads(path.read_text())
        sections = [manifest.get("dependencies", {}), manifest.get("build-dependencies", {})]
        for target in manifest.get("target", {}).values():
            sections.extend((target.get("dependencies", {}), target.get("build-dependencies", {})))
        for section in sections:
            for name, details in section.items():
                package = details.get("package", name) if isinstance(details, dict) else name
                if package.startswith("tobkiri-") and package.removeprefix("tobkiri-") not in allowed:
                    errors.append(f"{path.relative_to(root)}: forbidden internal dependency {package}")
    return errors


def check_inventory(root: Path) -> list[str]:
    path = root / "migration" / "inventory.json"
    if not path.exists():
        return ["migration/inventory.json missing"]
    units = json.loads(path.read_text()).get("units", [])
    errors: list[str] = []
    if not units:
        return ["migration inventory has no units"]
    accepted = {"ported+parity", "core+vectors", "retired+approved"}
    for unit in units:
        name = unit["legacy_unit"]
        if unit.get("status") not in accepted:
            errors.append(f"{name}: unresolved migration status {unit.get('status')!r}")
        elif not unit.get("evidence"):
            errors.append(f"{name}: completion has no evidence")
        elif unit["status"] == "retired+approved" and not unit.get("human_approved"):
            errors.append(f"{name}: retirement lacks approval")
        elif unit["status"] == "ported+parity" and not unit.get("parity_fixture"):
            errors.append(f"{name}: replacement lacks frozen parity fixture")
        for evidence in unit.get("evidence", []):
            candidate = root / evidence
            if not candidate.resolve().is_relative_to(root.resolve()) or not candidate.is_file():
                errors.append(f"{name}: evidence file missing or outside repository: {evidence}")
        if fixture := unit.get("parity_fixture"):
            candidate = root / fixture
            if not candidate.resolve().is_relative_to(root.resolve()) or not candidate.is_file():
                errors.append(f"{name}: frozen parity fixture missing or outside repository: {fixture}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--stage", choices=("foundation", "complete"), required=True)
    args = parser.parse_args()
    errors = check_sources(args.root, complete=args.stage == "complete")
    errors += check_dependencies(args.root)
    if args.stage == "complete":
        errors += check_inventory(args.root)
    for error in errors[:30]:
        print(error)
    if len(errors) > 30:
        print(f"... {len(errors) - 30} further failures")
    print(f"Issue1520 {args.stage}: {len(errors)} failure(s)")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
