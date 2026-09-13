"""Tests for the copied Launcher panel route scanner."""

from __future__ import annotations

import importlib.util
import json
import sys
import subprocess
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
                "pack_id": "fixture_pack",
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
        "const prefix='/api/contracts/fixture_pack/';const path='/api/home/dashboard';",
    )
    assert scan_panel(panel, contract_map) == []


def test_generic_client_selected_dispatch_is_red(tmp_path: Path) -> None:
    panel, contract_map = _fixture(
        tmp_path,
        "const prefix='/api/contracts/fixture_pack/';"
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


def test_contract_map_without_pack_identity_is_red(tmp_path: Path) -> None:
    panel, contract_map = _fixture(
        tmp_path,
        "const prefix='/api/contracts/fixture_pack/';const path='/api/home/dashboard';",
    )
    document = json.loads(contract_map.read_text(encoding="utf-8"))
    document.pop("pack_id")
    contract_map.write_text(json.dumps(document), encoding="utf-8")
    assert scan_panel(panel, contract_map) == [
        {"rule": "contract_map_invalid", "path": str(contract_map)}
    ]


def test_cli_scans_explicit_build_output(tmp_path: Path) -> None:
    panel, contract_map = _fixture(
        tmp_path,
        "const prefix='/api/contracts/fixture_pack/';const path='/api/home/dashboard';",
    )
    command = [
        sys.executable, str(SCANNER_PATH), "--panel-root", str(panel),
        "--contract-map", str(contract_map),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    (panel / "assets" / "app.js").write_text(
        "const prefix='/api/contracts/fixture_pack/';", encoding="utf-8"
    )
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"][0]["rule"] == "mapped_route_missing"
