from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = (
    ROOT
    / "tobkiri_runtime"
    / "scripts"
    / "quality"
    / "scan_pack_architecture.py"
)


def _scanner():
    spec = importlib.util.spec_from_file_location(
        "pack_architecture_scanner_test", SCANNER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pack(root: Path, pack_id: str) -> Path:
    pack = root / "tobkiri_runtime" / "ecosystem" / pack_id
    pack.mkdir(parents=True)
    (pack / "ecosystem.json").write_text(
        json.dumps({"id": pack_id}), encoding="utf-8"
    )
    return pack


@pytest.mark.parametrize(
    ("relative_path", "source"),
    [
        ("consumer.py", "from ecosystem.pack_b.private import value\n"),
        ("consumer.ts", "import value from '../pack_b/private'\n"),
        ("consumer.dart", "import '../pack_b/private.dart';\n"),
    ],
)
def test_cross_pack_imports_are_exact_edges(
    tmp_path: Path, relative_path: str, source: str
) -> None:
    scanner = _scanner()
    pack_a = _pack(tmp_path, "pack_a")
    _pack(tmp_path, "pack_b")
    (pack_a / relative_path).write_text(source, encoding="utf-8")

    violations = scanner.scan_repository(tmp_path)

    edge = next(item for item in violations if item.rule == "cross_pack_import")
    assert edge.source == "pack_a"
    assert edge.target == "pack_b"
    assert edge.identity.startswith(
        "cross_pack_import|"
        f"tobkiri_runtime/ecosystem/pack_a/{relative_path}|1|"
    )


def test_foreign_pack_branch_and_sibling_path_are_detected(tmp_path: Path) -> None:
    scanner = _scanner()
    pack_a = _pack(tmp_path, "pack_a")
    _pack(tmp_path, "pack_b")
    (pack_a / "consumer.py").write_text(
        "if request.pack_id == 'pack_b':\n"
        "    path = 'tobkiri_runtime/ecosystem/pack_b/private.db'\n",
        encoding="utf-8",
    )

    rules = {item.rule for item in scanner.scan_repository(tmp_path)}

    assert "foreign_pack_id_branch" in rules
    assert "sibling_pack_path" in rules


def test_unscoped_kernel_discovery_secret_and_domain_branch_are_detected(
    tmp_path: Path,
) -> None:
    scanner = _scanner()
    _pack(tmp_path, "pack_a")
    kernel = tmp_path / "tobkiri_runtime" / "core_runtime"
    kernel.mkdir(parents=True)
    (kernel / "unsafe.py").write_text(
        "packs = ecosystem_root.glob('*')\n"
        "token = os.environ.get('GLOBAL_API_TOKEN')\n"
        "if pack_id == 'pack_a':\n"
        "    pass\n",
        encoding="utf-8",
    )

    rules = {item.rule for item in scanner.scan_repository(tmp_path)}

    assert {
        "unscoped_pack_discovery",
        "unscoped_global_secret",
        "kernel_domain_branch",
    } <= rules


def test_baseline_rejects_wildcards_and_missing_metadata(tmp_path: Path) -> None:
    scanner = _scanner()
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exceptions": [{"identity": "cross_pack_import|*"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(scanner.BaselineError):
        scanner.load_baseline(baseline)


def test_baseline_is_shrink_only_by_exact_identity() -> None:
    scanner = _scanner()
    approved = {
        "edge-a": {"identity": "edge-a", "owner": "architecture"}
    }

    scanner.verify_shrink_only_baseline({}, approved)
    with pytest.raises(scanner.BaselineError, match="new identities"):
        scanner.verify_shrink_only_baseline(
            {**approved, "edge-b": {"identity": "edge-b"}}, approved
        )
    with pytest.raises(scanner.BaselineError, match="metadata changed"):
        scanner.verify_shrink_only_baseline(
            {"edge-a": {"identity": "edge-a", "owner": "someone-else"}},
            approved,
        )
