#!/usr/bin/env python3
"""Fail closed when the copied Launcher panel bypasses exact v4 routes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PANEL = (
    ROOT
    / "tobkiri_runtime"
    / "core_runtime"
    / "core_pack"
    / "core_control_panel"
    / "web"
)
DEFAULT_MAP = (
    ROOT
    / "tobkiri_runtime"
    / "ecosystem"
    / "defaultspack"
    / "defaultspack"
    / "frontend_contract_map.v4.json"
)
GENERIC_DISPATCH = "/api/v4/dispatch"
CLIENT_SELECTED_FIELDS = ("contract_id", "operation_id")
RETIRED_ROUTES = (
    "/api/authority/events",
    "/api/packs/scan",
    "/api/routes/reload",
    "/api/runtime/available",
)


def scan_panel(panel_root: Path, map_path: Path) -> list[dict[str, Any]]:
    """Return copied-panel route findings without executing frontend code."""

    findings: list[dict[str, Any]] = []
    index = panel_root / "index.html"
    scripts = sorted(panel_root.rglob("*.js")) if panel_root.is_dir() else []
    if not index.is_file() or not scripts:
        return [{"rule": "copied_panel_missing", "path": str(panel_root)}]

    try:
        contract_map = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return [{"rule": "contract_map_unreadable", "path": str(map_path), "error": str(error)}]
    routes = contract_map.get("routes") if isinstance(contract_map, dict) else None
    if not isinstance(routes, list):
        return [{"rule": "contract_map_invalid", "path": str(map_path)}]

    sources = {path: path.read_text(encoding="utf-8", errors="replace") for path in scripts}
    combined = "\n".join(sources.values())
    if "/api/contracts/defaultspack/" not in combined:
        findings.append({"rule": "exact_contract_prefix_missing", "path": str(panel_root)})
    for route in routes:
        target = route.get("path") if isinstance(route, dict) else None
        presentation = route.get("presentation") if isinstance(route, dict) else None
        if presentation != "broker_result":
            continue
        if not isinstance(target, str) or target not in combined:
            findings.append(
                {"rule": "mapped_route_missing", "path": str(panel_root), "route": target}
            )

    for path, source in sources.items():
        if GENERIC_DISPATCH in source:
            selected = [field for field in CLIENT_SELECTED_FIELDS if field in source]
            if selected:
                findings.append(
                    {
                        "rule": "generic_client_dispatch_bundle",
                        "path": path.relative_to(panel_root).as_posix(),
                        "fields": selected,
                    }
                )
        for route in RETIRED_ROUTES:
            if route in source:
                findings.append(
                    {
                        "rule": "retired_route_in_panel",
                        "path": path.relative_to(panel_root).as_posix(),
                        "route": route,
                    }
                )
    return findings


def main() -> int:
    """Scan the copy-panel output and emit deterministic JSON evidence."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel-root", type=Path, default=DEFAULT_PANEL)
    parser.add_argument("--contract-map", type=Path, default=DEFAULT_MAP)
    args = parser.parse_args()
    findings = scan_panel(args.panel_root, args.contract_map)
    print(json.dumps({"findings": findings, "status": "RED" if findings else "GREEN"}))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
