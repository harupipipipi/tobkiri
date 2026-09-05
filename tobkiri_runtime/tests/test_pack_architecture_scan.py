from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = ROOT / "tobkiri_runtime" / "scripts" / "quality" / "scan_pack_architecture.py"


def _scanner():
    spec = importlib.util.spec_from_file_location("pack_architecture_scanner_test", SCANNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pack(root: Path, pack_id: str) -> Path:
    pack = root / "tobkiri_runtime" / "ecosystem" / pack_id
    pack.mkdir(parents=True)
    (pack / "ecosystem.json").write_text(json.dumps({"id": pack_id}), encoding="utf-8")
    catalog_root = root / "tobkiri_runtime" / "schemas"
    catalog_root.mkdir(parents=True, exist_ok=True)
    pack_ids = sorted(
        path.name for path in (root / "tobkiri_runtime" / "ecosystem").iterdir() if path.is_dir()
    )
    (catalog_root / "pack_v4_catalog.v1.json").write_text(
        json.dumps(
            {
                "catalog_api_version": "io.tobkiri.pack-source-catalog.v1",
                "excluded_packs": [],
                "pack_ids": pack_ids,
                "packs": [{"pack_id": item} for item in pack_ids],
            }
        ),
        encoding="utf-8",
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
    assert edge.fingerprint.startswith(("ast-v1:", "text-v1:"))
    assert edge.identity == (
        "cross_pack_import|"
        f"tobkiri_runtime/ecosystem/pack_a/{relative_path}|"
        f"{edge.fingerprint}|pack_a|pack_b"
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


def test_defaultspack_webapp_has_no_scanner_exception(tmp_path: Path) -> None:
    """The same direct-route rule applies to the bundled Pack UI."""
    defaultspack = _pack(tmp_path, "defaultspack")
    (defaultspack / "webapp").mkdir()
    (defaultspack / "webapp" / "app.ts").write_text(
        'fetch("/api/legacy-route");\n',
        encoding="utf-8",
    )

    violations = _scanner().scan_repository(tmp_path)

    assert any(
        item.rule == "direct_implementation_route" and item.source == "defaultspack"
        for item in violations
    )


def test_product_favoritism_detects_alias_and_indirect_import(tmp_path: Path) -> None:
    """Kernel code cannot hide a product Pack behind a literal alias."""
    _pack(tmp_path, "pack_a")
    _pack(tmp_path, "defaultspack")
    kernel = tmp_path / "tobkiri_runtime" / "core_runtime"
    kernel.mkdir(parents=True)
    (kernel / "unsafe.py").write_text(
        "import importlib\n"
        "PREFERRED_PACK = 'defaultspack'\n"
        "PREFERRED_MODULE = 'ecosystem.' + PREFERRED_PACK\n"
        "if requested_pack == PREFERRED_PACK:\n"
        "    importlib.import_module(PREFERRED_MODULE)\n",
        encoding="utf-8",
    )

    rules = {item.rule for item in _scanner().scan_repository(tmp_path) if item.source == "kernel"}

    assert {
        "product_pack_reference",
        "product_pack_branch",
        "product_pack_import",
    } <= rules


def test_application_composition_root_is_defaultspack_owned_but_still_scans_edges(
    tmp_path: Path,
) -> None:
    """The one product root may import itself, but not a second Pack."""
    _pack(tmp_path, "defaultspack")
    _pack(tmp_path, "pack_a")
    application = tmp_path / "tobkiri_runtime" / "app.py"
    application.parent.mkdir(parents=True, exist_ok=True)
    application.write_text(
        "from ecosystem.defaultspack.defaultspack.runtime_composition import create_kernel\n"
        "from ecosystem.pack_a.runtime import create_other_kernel\n",
        encoding="utf-8",
    )

    violations = _scanner().scan_repository(tmp_path)

    assert not any(
        item.path == "tobkiri_runtime/app.py" and item.target == "defaultspack"
        for item in violations
    )
    assert any(
        item.path == "tobkiri_runtime/app.py"
        and item.rule == "cross_pack_import"
        and item.source == "defaultspack"
        and item.target == "pack_a"
        for item in violations
    )


def test_policy_checker_literals_do_not_self_trigger_product_special_case(
    tmp_path: Path,
) -> None:
    """The exact policy checker is data about the rule, not product policy."""
    _pack(tmp_path, "defaultspack")
    checker = tmp_path / "scripts" / "quality" / "check_core_no_favoritism.py"
    checker.parent.mkdir(parents=True)
    checker.write_text(
        "PREFERRED_PACK = 'defaultspack'\nif configured_pack == PREFERRED_PACK:\n    pass\n",
        encoding="utf-8",
    )

    violations = _scanner().scan_repository(tmp_path)

    assert not any(
        item.path == "scripts/quality/check_core_no_favoritism.py"
        and item.rule.startswith("product_pack_")
        for item in violations
    )


@pytest.mark.parametrize(
    ("relative_path", "expected_source"),
    [
        ("tobkiri_runtime/application_neighbor.py", "host"),
        ("tobkiri_runtime/core_runtime/application_neighbor.py", "kernel"),
    ],
)
def test_neighboring_host_and_kernel_sources_still_detect_product_literals(
    tmp_path: Path,
    relative_path: str,
    expected_source: str,
) -> None:
    """Exact exclusions cannot hide an adjacent production or core source."""
    _pack(tmp_path, "defaultspack")
    source = tmp_path / relative_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "PREFERRED_PACK = 'defaultspack'\nif configured_pack == PREFERRED_PACK:\n    pass\n",
        encoding="utf-8",
    )

    violations = _scanner().scan_repository(tmp_path)
    matching = [item for item in violations if item.path == relative_path]

    assert {"product_pack_reference", "product_pack_branch"} <= {item.rule for item in matching}
    assert {item.source for item in matching} == {expected_source}


def test_legacy_manifest_cannot_be_used_for_pack_discovery(tmp_path: Path) -> None:
    """A legacy manifest without a v4 catalog is a fail-closed scan input."""
    scanner = _scanner()
    pack = tmp_path / "tobkiri_runtime" / "ecosystem" / "pack_a"
    pack.mkdir(parents=True)
    (pack / "ecosystem.json").write_text('{"id": "pack_a"}', encoding="utf-8")

    with pytest.raises(scanner.PackCatalogError):
        scanner.scan_repository(tmp_path)


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


def test_ast_fingerprint_survives_line_relocation(tmp_path: Path) -> None:
    scanner = _scanner()
    pack_a = _pack(tmp_path, "pack_a")
    _pack(tmp_path, "pack_b")
    consumer = pack_a / "consumer.py"
    consumer.write_text("from ecosystem.pack_b.private import value\n", encoding="utf-8")
    original = next(
        item for item in scanner.scan_repository(tmp_path) if item.rule == "cross_pack_import"
    )
    consumer.write_text(
        "\n\n\nfrom ecosystem.pack_b.private import value\n",
        encoding="utf-8",
    )
    relocated = next(
        item for item in scanner.scan_repository(tmp_path) if item.rule == "cross_pack_import"
    )

    assert original.line == 1
    assert relocated.line == 4
    assert relocated.fingerprint == original.fingerprint
    assert relocated.identity == original.identity


def test_ast_fingerprint_is_stable_across_empty_field_dump_versions() -> None:
    scanner = _scanner()
    node = (
        scanner.ast.parse(
            "if template.get('source_pack_id') != 'rumi_sandbox_runtime_pack':\n"
            "    raise RuntimeError\n"
        )
        .body[0]
        .test
    )

    assert scanner._ast_fingerprint(node) == "ast-v1:1a3a6d9506a9664e6f96"


def test_generated_tauri_runtime_is_not_scanned(tmp_path: Path) -> None:
    scanner = _scanner()
    _pack(tmp_path, "pack_b")
    generated = tmp_path / "tobkiri_launcher" / "src-tauri" / "gen" / "app" / "consumer.py"
    generated.parent.mkdir(parents=True)
    generated.write_text("from ecosystem.pack_b.private import value\n", encoding="utf-8")

    assert scanner.scan_repository(tmp_path) == []


def test_dependency_trees_are_excluded_without_hiding_real_sources(
    tmp_path: Path,
) -> None:
    scanner = _scanner()
    pack_a = _pack(tmp_path, "pack_a")
    _pack(tmp_path, "pack_b")
    dependency_source = (
        tmp_path
        / "tobkiri_runtime"
        / ".venv"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "mypy"
        / "consumer.py"
    )
    dependency_source.parent.mkdir(parents=True)
    dependency_source.write_text(
        "from ecosystem.pack_b.private import value\n",
        encoding="utf-8",
    )
    real_source = pack_a / "node_modules_helper" / "consumer.py"
    real_source.parent.mkdir()
    real_source.write_text(
        "from ecosystem.pack_b.private import value\n",
        encoding="utf-8",
    )

    cross_pack_edges = [
        item for item in scanner.scan_repository(tmp_path) if item.rule == "cross_pack_import"
    ]

    assert [item.path for item in cross_pack_edges] == [
        "tobkiri_runtime/ecosystem/pack_a/node_modules_helper/consumer.py"
    ]


def _baseline_exception(
    *,
    line: int,
    owner: str = "architecture",
    target: str = "pack_b",
    fingerprint: str = "ast-v1:0123456789abcdef0123",
    sunset_at: str = "2027-07-24",
) -> dict[str, object]:
    rule = "cross_pack_import"
    path = "tobkiri_runtime/ecosystem/pack_a/consumer.py"
    source = "pack_a"
    return {
        "identity": f"{rule}|{path}|{fingerprint}|{source}|{target}",
        "rule": rule,
        "violation_category": rule,
        "path": path,
        "line": line,
        "fingerprint": fingerprint,
        "source": source,
        "target": target,
        "owner": owner,
        "reason": "Temporary architecture exception.",
        "introduced_at": "2026-07-24",
        "fix_by_wave": 11,
        "sunset_at": sunset_at,
    }


def _baseline(*items: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(item["identity"]): item for item in items}


def test_baseline_is_shrink_only_by_semantic_edge() -> None:
    scanner = _scanner()
    approved = _baseline(_baseline_exception(line=10))

    scanner.verify_shrink_only_baseline({}, approved)
    scanner.verify_shrink_only_baseline(
        _baseline(_baseline_exception(line=25)),
        approved,
    )
    with pytest.raises(scanner.BaselineError, match="new identities"):
        scanner.verify_shrink_only_baseline(
            _baseline(
                _baseline_exception(line=25),
                _baseline_exception(
                    line=30,
                    fingerprint="ast-v1:abcdef0123456789abcd",
                ),
            ),
            approved,
        )
    with pytest.raises(scanner.BaselineError, match="metadata changed"):
        scanner.verify_shrink_only_baseline(
            _baseline(_baseline_exception(line=25, owner="someone-else")),
            approved,
        )


def test_current_scan_matches_relocated_baseline_edges_one_to_one() -> None:
    scanner = _scanner()
    baseline = _baseline(_baseline_exception(line=10))
    relocated = scanner.Violation(
        rule="cross_pack_import",
        path="tobkiri_runtime/ecosystem/pack_a/consumer.py",
        line=25,
        source="pack_a",
        target="pack_b",
        guidance="Use a contract.",
        fingerprint="ast-v1:0123456789abcdef0123",
    )
    duplicate = scanner.Violation(
        rule=relocated.rule,
        path=relocated.path,
        line=30,
        source=relocated.source,
        target=relocated.target,
        guidance=relocated.guidance,
    )

    assert scanner.find_unbaselined_violations([relocated], baseline) == []
    assert scanner.find_unbaselined_violations(
        [relocated, duplicate],
        baseline,
    ) == [duplicate]


def test_resolved_edges_must_be_removed_from_the_baseline() -> None:
    scanner = _scanner()
    resolved = _baseline_exception(line=10)
    active = _baseline_exception(line=20, target="pack_c")
    baseline = _baseline(resolved, active)
    violation = scanner.Violation(
        rule=str(active["rule"]),
        path=str(active["path"]),
        line=30,
        source=str(active["source"]),
        target=str(active["target"]),
        guidance="Use a contract.",
        fingerprint=str(active["fingerprint"]),
    )

    assert scanner.find_stale_baseline_exceptions(
        [violation],
        baseline,
    ) == [resolved]


def test_update_baseline_removes_only_resolved_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical writer may shrink, but never broaden, a reviewed baseline."""

    scanner = _scanner()
    resolved = _baseline_exception(line=10)
    active = _baseline_exception(
        line=20,
        fingerprint="ast-v1:abcdef0123456789abcd",
    )
    payload = {
        "schema_version": 2,
        "policy": "shrink_only_exact_edges",
        "exceptions": [resolved, active],
    }
    baseline = tmp_path / "baseline.json"
    reference = tmp_path / "reference.json"
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    reference.write_text(json.dumps(payload), encoding="utf-8")
    violation = scanner.Violation(
        rule=str(active["rule"]),
        path=str(active["path"]),
        line=99,
        source=str(active["source"]),
        target=str(active["target"]),
        guidance="Use a contract.",
        fingerprint=str(active["fingerprint"]),
    )
    monkeypatch.setattr(scanner, "scan_repository", lambda _root: [violation])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_pack_architecture.py",
            "--root",
            str(tmp_path),
            "--baseline",
            str(baseline),
            "--reference-baseline",
            str(reference),
            "--update-baseline",
        ],
    )

    assert scanner.main() == 0
    updated = json.loads(baseline.read_text(encoding="utf-8"))
    assert updated["schema_version"] == payload["schema_version"]
    assert updated["policy"] == payload["policy"]
    assert updated["exceptions"] == [active]


def test_expired_exceptions_fail_by_sunset_date() -> None:
    scanner = _scanner()
    expired = _baseline_exception(line=10, sunset_at="2026-07-28")
    current = _baseline_exception(
        line=20,
        fingerprint="ast-v1:abcdef0123456789abcd",
    )

    assert scanner.find_expired_baseline_exceptions(
        _baseline(expired, current),
        today=scanner.dt.date(2026, 7, 29),
    ) == [expired]


def test_main_fails_for_expired_or_stale_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scanner = _scanner()
    _pack(tmp_path, "pack_a")
    baseline = tmp_path / "baseline.json"
    reference = tmp_path / "reference.json"
    expired = _baseline_exception(line=10, sunset_at="2026-07-28")
    expired_payload = {
        "schema_version": 2,
        "policy": "shrink_only_exact_edges",
        "exceptions": [expired],
    }
    baseline.write_text(json.dumps(expired_payload), encoding="utf-8")
    reference.write_text(json.dumps(expired_payload), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_pack_architecture.py",
            "--root",
            str(tmp_path),
            "--baseline",
            str(baseline),
            "--reference-baseline",
            str(reference),
            "--today",
            "2026-07-29",
        ],
    )

    assert scanner.main() == 2
    assert "expired exceptions" in capsys.readouterr().err

    current = _baseline_exception(line=10)
    current_payload = {
        "schema_version": 2,
        "policy": "shrink_only_exact_edges",
        "exceptions": [current],
    }
    baseline.write_text(json.dumps(current_payload), encoding="utf-8")
    reference.write_text(json.dumps(current_payload), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "scan_pack_architecture.py",
            "--root",
            str(tmp_path),
            "--baseline",
            str(baseline),
            "--reference-baseline",
            str(reference),
            "--today",
            "2026-07-29",
        ],
    )

    assert scanner.main() == 2
    assert "resolved identities" in capsys.readouterr().err


def test_main_rejects_candidate_baseline_without_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A candidate cannot bootstrap its own exception authority."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "policy": "shrink_only_exact_edges",
                "exceptions": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["scan_pack_architecture.py", "--root", str(tmp_path), "--baseline", str(baseline)],
    )

    assert _scanner().main() == 2
    assert "reference baseline is required" in capsys.readouterr().err
