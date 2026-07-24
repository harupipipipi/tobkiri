"""Fail CI when the resolved command protocol regresses to no-op behavior."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULTSPACK = ROOT / "ecosystem" / "defaultspack"
for entry in (ROOT, DEFAULTSPACK):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from domain.frontend.command_protocol import CommandProtocolRegistry  # noqa: E402

ALLOWED_EXECUTION_KINDS = {
    "host_operation",
    "pack_operation",
    "state_mutation",
}
SECRET_FRAGMENTS = {
    "cookie",
    "password",
    "secret",
    "api_key",
    "access_token",
    "approval_token",
}


def scan() -> dict[str, Any]:
    registry = CommandProtocolRegistry()
    catalog = registry.catalog()
    commands = catalog["commands"]
    failures: list[str] = []
    identities: set[str] = set()
    for command in commands:
        canonical_id = str(command.get("canonical_id") or "")
        if not canonical_id or canonical_id in identities:
            failures.append(f"invalid or duplicate identity: {canonical_id!r}")
        identities.add(canonical_id)
        availability = command.get("availability") or {}
        if availability.get("status") != "available":
            failures.append(f"{canonical_id}: unavailable")
        execution = command.get("execution") or {}
        if execution.get("kind") not in ALLOWED_EXECUTION_KINDS:
            failures.append(f"{canonical_id}: unresolved execution")
        if "legacy_type" in execution:
            failures.append(f"{canonical_id}: legacy execution leaked into v1")
        if "legacy" in command:
            failures.append(f"{canonical_id}: legacy command leaked into v1")
        authorization = command.get("authorization") or {}
        if authorization.get("approval_required") and not authorization.get(
            "permissions"
        ):
            failures.append(f"{canonical_id}: approval has no operation capability")
        if _contains_secret(command):
            failures.append(f"{canonical_id}: secret-shaped field in public catalog")
    error_diagnostics = [
        item
        for item in catalog.get("diagnostics") or []
        if str(item.get("level") or "").lower() in {"error", "fatal"}
    ]
    if error_diagnostics:
        failures.append("catalog contains error diagnostics")
    matrix = registry.conformance_matrix()
    if len(matrix) != len(commands):
        failures.append("behavioral conformance matrix is incomplete")
    if any(not item.get("completion_semantics") for item in matrix):
        failures.append("behavioral conformance matrix contains a no-op")
    if any(item.get("verified_handler") is not True for item in matrix):
        failures.append("behavioral conformance matrix contains an unprobed handler")
    if failures:
        raise SystemExit("\n".join(sorted(failures)))
    return {
        "api_version": catalog["api_version"],
        "catalog_revision": catalog["catalog_revision"],
        "command_count": len(commands),
        "canonical_ids": sorted(identities),
    }


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in SECRET_FRAGMENTS):
                return True
            if _contains_secret(item):
                return True
    elif isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--check-inventory", action="store_true")
    args = parser.parse_args()
    inventory = json.dumps(scan(), ensure_ascii=False, indent=2) + "\n"
    if args.inventory:
        if args.check_inventory:
            if (
                not args.inventory.is_file()
                or args.inventory.read_text(encoding="utf-8") != inventory
            ):
                raise SystemExit("command protocol inventory drift detected")
        else:
            args.inventory.parent.mkdir(parents=True, exist_ok=True)
            args.inventory.write_text(inventory, encoding="utf-8")
    print(inventory, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
