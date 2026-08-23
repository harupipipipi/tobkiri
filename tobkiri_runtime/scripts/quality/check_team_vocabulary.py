"""Reject legacy Company identities from canonical Team catalog surfaces."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_PACKS = {
    "rumi_team_state_store_pack",
    "rumi_team_coordinator_pack",
    "rumi_team_agent_work_adapter_pack",
    "rumi_team_console_pack",
    "rumi_connector_team_adapter_pack",
    "rumi_operations_team_pack",
    "rumi_run_lifecycle_pack",
}
LEGACY_TOKENS = (
    "rumi_company_",
    "rumi_agent_workroom_pack",
    ".company.",
    "company.state.",
    "company.coordinate",
    "company.runtime",
    "company.work",
    "/api/company",
    "company_id",
)
ALLOWED_SUBTREES = {"legacy_ids", "migration", "compatibility", "provenance"}


def _strings(value: Any, path: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(".".join(path), value)]
    if isinstance(value, list):
        return [
            item
            for index, child in enumerate(value)
            for item in _strings(child, (*path, str(index)))
        ]
    if isinstance(value, dict):
        found: list[tuple[str, str]] = []
        for key, child in value.items():
            normalized = str(key)
            if normalized in ALLOWED_SUBTREES:
                continue
            found.extend(_strings(child, (*path, normalized)))
        return found
    return []


def check(root: Path) -> list[str]:
    catalog_path = root / "schemas" / "pack_v4_catalog.v1.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    findings: list[str] = []
    records = {record["pack_id"]: record for record in catalog["packs"]}
    for pack_id in sorted(CANONICAL_PACKS):
        record = records.get(pack_id)
        if record is None:
            findings.append(f"missing canonical Pack: {pack_id}")
            continue
        for field, value in _strings(record, (pack_id,)):
            lowered = value.casefold()
            for token in LEGACY_TOKENS:
                if token.casefold() in lowered:
                    findings.append(f"{field}: {token!r} in {value!r}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    findings = check(args.root.resolve())
    if findings:
        print("canonical Team vocabulary violations:")
        print("\n".join(f"- {finding}" for finding in findings))
        return 1
    print("canonical Team vocabulary: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
