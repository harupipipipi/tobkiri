"""Tests for the copied Launcher panel route scanner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = ROOT / "scripts" / "quality" / "scan_launcher_frontend_routes.py"
SPEC = importlib.util.spec_from_file_location(
    "scan_launcher_frontend_routes_test_module", SCANNER_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Launcher route scanner is unavailable: {SCANNER_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
scan_panel = MODULE.scan_panel


def _fixture(tmp_path: Path, source: str) -> tuple[Path, Path]:
    panel = tmp_path / "panel"
    assets = panel / "assets"
    assets.mkdir(parents=True)
    (panel / "index.html").write_text("<script src='assets/app.js'></script>", encoding="utf-8")
    (assets / "app.js").write_text(source, encoding="utf-8")
    contract_map = tmp_path / "map.json"
    contract_map.write_text(
        json.dumps(
            {
                "routes": [
                    {
                        "method": "GET",
                        "path": "/api/home/dashboard",
                        "presentation": "broker_result",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return panel, contract_map


def test_exact_copied_route_is_green(tmp_path: Path) -> None:
    panel, contract_map = _fixture(
        tmp_path,
        "const prefix='/api/contracts/defaultspack/';const path='/api/home/dashboard';",
    )
    assert scan_panel(panel, contract_map) == []


def test_generic_client_selected_dispatch_is_red(tmp_path: Path) -> None:
    panel, contract_map = _fixture(
        tmp_path,
        "const prefix='/api/contracts/defaultspack/';"
        "const path='/api/home/dashboard';"
        "fetch('/api/v4/dispatch',{body:JSON.stringify({contract_id,operation_id})});",
    )
    assert scan_panel(panel, contract_map) == [
        {
            "fields": ["contract_id", "operation_id"],
            "path": "assets/app.js",
            "rule": "generic_client_dispatch_bundle",
        }
    ]
