"""Tests for nonauthoritative Host Extension facts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ops.quality.scan_host_extension_inventory import (
    build_inventory,
    canonical_json,
    main,
    render_summary,
)

RUNTIME_ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "host_extension_inventory"
SUMMARY_REPORT = RUNTIME_ROOT / "docs" / "host-extension-inventory.md"


def test_fixture_reports_schema_profile_reachability_and_advisory_ast() -> None:
    """The fixture links canonical inputs to factual static evidence."""

    report = build_inventory(FIXTURE_ROOT)

    assert report["summary"] == {
        "diagnostic_count": 0,
        "manual_review_pack_count": 1,
        "operation_count": 1,
        "pack_count": 1,
        "runtime_signals": {
            "ai_runtime_signal": 1,
            "tool_runtime_signal": 0,
            "none": 0,
        },
        "tracked_profile_count": 2,
        "tracked_profile_reachable_operation_count": 1,
        "tracked_profile_reachable_pack_count": 1,
    }
    assert [item["profile_id"] for item in report["profiles"]] == [
        "fixture-primary",
        "fixture-secondary",
    ]
    record = report["records"][0]
    operation = record["operations"][0]
    implementation = operation["implementations"][0]
    assert record["runtime_signal"] == "ai_runtime_signal"
    assert record["schema_validity"] == {
        "executable_catalog": True,
        "pack_manifest": True,
    }
    assert record["manual_review_reasons"] == ["runtime_signal_requires_human_review"]
    assert operation["reachable"] is True
    assert implementation["factory_observed"] is True
    assert implementation["io_imports"] == [
        {"category": "filesystem", "module": "pathlib"}
    ]


def test_graph_edge_requires_explicit_pack_inclusion(tmp_path: Path) -> None:
    """An edge to an omitted Pack remains evidence, never reachability."""

    root = tmp_path / "runtime"
    shutil.copytree(FIXTURE_ROOT, root)
    intent_path = (
        root / "ecosystem" / "defaultspack" / "v4" / "defaults.profile.intent.v1.json"
    )
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["packs"] = [
        {"artifact_digest": None, "pack_id": "other_pack", "role": "contribution"}
    ]
    intent_path.write_text(json.dumps(intent), encoding="utf-8")

    record = build_inventory(root)["records"][0]
    reachability = record["operations"][0]["profile_reachability"][0]
    assert reachability == {
        "graph_reachable": True,
        "included_pack": False,
        "profile_id": "fixture-primary",
        "reachable": False,
        "source_path": ("ecosystem/defaultspack/v4/defaults.profile.intent.v1.json"),
    }
    assert (
        "reachable_edge_without_explicit_pack_inclusion"
        in record["manual_review_reasons"]
    )


def test_official_schema_failure_is_visible_and_manual(tmp_path: Path) -> None:
    """Invalid canonical documents cannot become clean evidence."""

    root = tmp_path / "runtime"
    shutil.copytree(FIXTURE_ROOT, root)
    manifest_path = root / "ecosystem" / "fixture_ai_pack" / "pack.v4.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["provenance"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = build_inventory(root)
    record = report["records"][0]
    assert record["schema_validity"]["pack_manifest"] is False
    assert "official_schema_validation_failed" in record["manual_review_reasons"]
    assert any(
        item["code"] == "official_schema_validation_failed"
        and item["path"].endswith("pack.v4.json")
        for item in report["diagnostics"]
    )


def test_output_is_deterministic_and_cli_checks_explicit_artifacts(
    tmp_path: Path,
) -> None:
    """JSON is on demand while the compact summary supports diff checks."""

    first = build_inventory(FIXTURE_ROOT)
    second = build_inventory(FIXTURE_ROOT)
    assert canonical_json(first) == canonical_json(second)
    assert render_summary(first) == render_summary(second)

    json_output = tmp_path / "inventory.json"
    summary_output = tmp_path / "inventory.md"
    common = [
        "--runtime-root",
        str(FIXTURE_ROOT),
        "--json-output",
        str(json_output),
        "--summary-output",
        str(summary_output),
    ]
    assert main([*common, "--write"]) == 0
    assert main([*common, "--check"]) == 0
    summary_output.write_text("stale\n", encoding="utf-8")
    assert main([*common, "--check"]) == 1


def test_checked_in_summary_matches_repository_facts() -> None:
    """The only tracked report is the compact Markdown summary."""

    assert SUMMARY_REPORT.read_text(encoding="utf-8") == render_summary(
        build_inventory(RUNTIME_ROOT)
    )
