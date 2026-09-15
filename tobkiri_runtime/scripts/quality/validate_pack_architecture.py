#!/usr/bin/env python3
"""Validate v4 schemas, provenance, ADR links, scanners, and inventory."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from tobkiri_protocol.canonical import strict_loads  # noqa: E402
from tobkiri_protocol.inventory import (  # noqa: E402
    DESIGN_INPUTS_RELATIVE_PATH,
    INVENTORY_RELATIVE_PATH,
    NORMATIVE_DOCUMENTS,
    inventory_drift,
    write_inventory,
)
from tobkiri_protocol.provenance import sha256_file  # noqa: E402
from tobkiri_protocol.scanners import scan_v4_scope  # noqa: E402
from tobkiri_protocol.validation import (  # noqa: E402
    SCHEMA_DIR,
    load_schema,
    validate_document,
)

SCHEMA_HASHES_PATH = Path("tobkiri_runtime/tobkiri_protocol/schema_hashes.json")
SCHEMA_HASHES_VERSION = "io.tobkiri.schema-hashes.v1"
ADR_RE = re.compile(r"^# ADR-(\d+):", re.MULTILINE)
STATUS_RE = re.compile(r"^- Status:\s*(\w+)", re.MULTILINE)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


class ArchitectureValidationError(ValueError):
    """Raised when the normative architecture control plane is inconsistent."""


def validate_architecture(root: Path, *, check_inventory: bool = True) -> list[str]:
    """Return all deterministic validation errors for the architecture layer."""
    root = root.resolve()
    errors: list[str] = []
    errors.extend(_validate_design_inputs(root))
    errors.extend(_validate_schema_files(root))
    errors.extend(_validate_schema_hashes(root))
    errors.extend(_validate_adrs(root))
    errors.extend(_validate_internal_links(root))
    v4_findings = scan_v4_scope(root)
    errors.extend(
        f"{finding.path}:{finding.line}: {finding.rule_id}: {finding.message}"
        for finding in v4_findings
    )
    if check_inventory:
        inventory_path = root / INVENTORY_RELATIVE_PATH
        if inventory_drift(root, inventory_path):
            errors.append(f"architecture inventory drift detected: {inventory_path}")
        elif inventory_path.is_file():
            try:
                inventory = validate_document(inventory_path.read_bytes(), "inventory")
                errors.extend(_validate_inventory_evidence(root, inventory))
            except Exception as exc:
                errors.append(f"architecture inventory is invalid: {exc}")
    return errors


def write_schema_hashes(root: Path) -> Path:
    """Generate the tracked schema hash manifest."""
    target = root / SCHEMA_HASHES_PATH
    files = {
        path.relative_to(SCHEMA_DIR).as_posix(): sha256_file(path)
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    }
    payload = {
        "schema": SCHEMA_HASHES_VERSION,
        "generator": "tobkiri-protocol",
        "generator_version": "1.0.0",
        "files": files,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def _validate_design_inputs(root: Path) -> list[str]:
    errors: list[str] = []
    path = root / DESIGN_INPUTS_RELATIVE_PATH
    try:
        payload = strict_loads(path.read_bytes())
    except Exception as exc:
        return [f"design inputs cannot be read: {exc}"]
    if not isinstance(payload, Mapping):
        return ["design inputs must be an object"]
    if payload.get("status") != "normative-provenance":
        errors.append("design inputs status must be normative-provenance")
    actual = tuple(payload.get("normative_documents", ()))
    expected = tuple(NORMATIVE_DOCUMENTS)
    if actual != expected:
        errors.append("design input normative_documents order/content drifted")
    for relative in expected:
        if not (root / relative).is_file():
            errors.append(f"missing normative document: {relative}")
    external = payload.get("informative_external_inputs", ())
    if not isinstance(external, list):
        errors.append("informative_external_inputs must be a list")
    else:
        for index, item in enumerate(external):
            if not isinstance(item, Mapping):
                errors.append(f"external input {index} must be an object")
                continue
            if item.get("tracked") is not False or item.get("normative") is not False:
                errors.append(f"external input {index} must remain non-normative and untracked")
    return errors


def _validate_schema_files(root: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        try:
            load_schema(path.name)
        except Exception as exc:
            errors.append(f"{path.relative_to(root)}: invalid JSON Schema: {exc}")
    return errors


def _validate_schema_hashes(root: Path) -> list[str]:
    errors: list[str] = []
    path = root / SCHEMA_HASHES_PATH
    if not path.is_file():
        return [f"schema hash manifest is missing: {path}"]
    try:
        payload = strict_loads(path.read_bytes())
    except Exception as exc:
        return [f"schema hash manifest is invalid: {exc}"]
    expected = {
        item.relative_to(SCHEMA_DIR).as_posix(): sha256_file(item)
        for item in sorted(SCHEMA_DIR.glob("*.schema.json"))
    }
    if not isinstance(payload, Mapping) or payload.get("schema") != SCHEMA_HASHES_VERSION:
        errors.append("schema hash manifest has an unsupported schema")
    elif payload.get("files") != expected:
        errors.append("schema hash manifest drift detected")
    return errors


def _validate_adrs(root: Path) -> list[str]:
    errors: list[str] = []
    docs_root = root / "tobkiri_runtime" / "docs"
    found: dict[str, Path] = {}
    for path in sorted(docs_root.glob("ADR-*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = ADR_RE.search(text)
        if match is None:
            continue
        number = match.group(1)
        if number in found:
            errors.append(f"duplicate ADR number {number}: {found[number]} and {path}")
        found[number] = path
        status = STATUS_RE.search(text)
        if number in {"014", "015", "016"} and (status is None or status.group(1) != "Accepted"):
            errors.append(f"ADR-{number} must remain Accepted")
    for number in ("014", "015", "016"):
        if number not in found:
            errors.append(f"required ADR-{number} is missing")
    route_map = docs_root / "README.md"
    if route_map.is_file():
        text = route_map.read_text(encoding="utf-8")
        for relative in (
            "ADR-014_BOUNDARY_CAPABILITY_GRANTS.txt",
            "ADR-015_RUNTIME_SECURITY_LIFECYCLE.txt",
            "ADR-016_BASE_SHELL_APPLICATION_MODEL.txt",
            "TOBKIRI_PACK_ARCHITECTURE_IMPLEMENTATION_PLAN.txt",
            "PACK_ARCHITECTURE_DESIGN_INPUTS.json",
        ):
            if relative not in text:
                errors.append(f"docs/README.md does not route to {relative}")
    return errors


def _validate_internal_links(root: Path) -> list[str]:
    errors: list[str] = []
    docs_root = root / "tobkiri_runtime" / "docs"
    documents = [docs_root / "README.md", *(root / item for item in NORMATIVE_DOCUMENTS)]
    for path in documents:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for target in MARKDOWN_LINK_RE.findall(text):
            target = target.strip().split(maxsplit=1)[0]
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path = unquote(target.split("#", maxsplit=1)[0])
            if not target_path:
                continue
            resolved = (path.parent / target_path).resolve()
            if not resolved.exists():
                errors.append(f"{path.relative_to(root)}: broken internal link {target}")
    return errors


def _validate_inventory_evidence(root: Path, inventory: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    inputs = inventory.get("inputs", {})
    if isinstance(inputs, Mapping):
        for item in inputs.get("normative_documents", []):
            if isinstance(item, Mapping) and not (root / str(item.get("path", ""))).is_file():
                errors.append(f"inventory evidence path is missing: {item.get('path')}")
    records = inventory.get("records", {})
    if isinstance(records, Mapping):
        for category in ("artifacts", "manifests", "execution_routes"):
            for item in records.get(category, []):
                if isinstance(item, Mapping) and not (root / str(item.get("path", ""))).is_file():
                    errors.append(f"inventory record path is missing: {item.get('path')}")
    findings = inventory.get("findings", {})
    if isinstance(findings, Mapping) and findings.get("v4"):
        errors.append("tracked inventory contains v4 findings")
    return errors


def main(argv: list[str] | None = None) -> int:
    """Run architecture validation or regenerate tracked artifacts."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parents[3])
    parser.add_argument("--write-inventory", action="store_true")
    parser.add_argument("--write-schema-hashes", action="store_true")
    parser.add_argument("--no-inventory", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.write_schema_hashes:
        write_schema_hashes(root)
    if args.write_inventory:
        write_inventory(root)
    errors = validate_architecture(root, check_inventory=not args.no_inventory)
    if errors:
        for error in errors:
            print(f"pack-architecture-v4: {error}", file=sys.stderr)
        return 1
    print("pack-architecture-v4: schemas, provenance, scanners, links, and inventory are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
