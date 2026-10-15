"""Focused tests for the Host runtime receipt collector's pure bindings.

The full admission → install → isolated-conformance chain requires an
activated Host, so these tests cover the receipt-construction rules that must
never weaken: digest binding, cross-instance consistency, and fail-closed
semantics for unverifiable probe output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from tests.conformance_support import collect_pack_runtime_receipts as collector


def _step(step_name: str, **fields: Any) -> dict[str, Any]:
    step = {
        "kind": step_name,
        "status": "verified",
        "pack_id": fields.pop("pack_id", "pack-a"),
        "target_digest": fields.pop("target_digest", "sha256:" + "a" * 64),
        "host_instance_id": "host:test",
        "observed_at": "2026-09-16T00:00:00Z",
        **fields,
    }
    step["receipt_digest"] = collector.canonical_digest(step)
    return step


def _probe_result(pack_id: str, target_digest: str, **overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "io.tobkiri.quality.pack-isolated-conformance-result.v1",
        "pack_id": pack_id,
        "target_digest": target_digest,
        "host_instance_id": "host-process:pid:4242:start:test",
        "observed_at": "2026-09-16T00:00:01Z",
        "checks": [
            {"check": name, "status": "verified"}
            for name in collector.CONFORMANCE_CHECKS
        ],
        "operation_inventory": {"count": 2, "operations": [], "routes": {}},
        "install": {"content_digest": "sha256:" + "b" * 64},
    }
    result.update(overrides)
    return result


TARGET = "sha256:" + "a" * 64
CONTENT = "sha256:" + "b" * 64


def test_suite_digest_binds_inputs() -> None:
    digest_a = collector.conformance_suite_digest("pack-a", TARGET)
    digest_b = collector.conformance_suite_digest("pack-b", TARGET)
    digest_c = collector.conformance_suite_digest("pack-a", "sha256:" + "f" * 64)
    assert digest_a != digest_b
    assert digest_a != digest_c
    assert digest_a.startswith("sha256:")


def test_zero_operation_conformance_step_requires_bound_digests() -> None:
    install = _step("install")
    step = collector.collect_conformance_step(
        probe_result=_probe_result(
            "pack-a", TARGET, operation_inventory={"count": 0, "operations": []}
        ),
        result_digest="sha256:" + "c" * 64,
        pack_id="pack-a",
        target_digest=TARGET,
        semantic_kind="zero-operation",
        expected_operation_count=0,
        parent_content_digest=CONTENT,
        install_step=install,
    )
    assert step["status"] == "verified"
    assert step["install_receipt_digest"] == install["receipt_digest"]
    assert step["result_digest"] == "sha256:" + "c" * 64
    assert step["test_suite_digest"] == collector.conformance_suite_digest(
        "pack-a", TARGET
    )
    assert step["receipt_digest"] == collector.canonical_digest(
        {k: v for k, v in step.items() if k != "receipt_digest"}
    )


def test_conformance_step_not_applicable_for_admission_only() -> None:
    install = _step("install")
    probe = _probe_result(
        "pack-a", TARGET, operation_inventory={"count": 0, "operations": []}
    )
    step = collector.collect_conformance_step(
        probe_result=probe,
        result_digest="sha256:" + "c" * 64,
        pack_id="pack-a",
        target_digest=TARGET,
        semantic_kind="admission-only",
        expected_operation_count=0,
        parent_content_digest=CONTENT,
        install_step=install,
    )
    assert step["status"] == "not-applicable"
    assert step["reason"].strip()


def test_conformance_step_rejects_cross_instance_digest_mismatch() -> None:
    with pytest.raises(collector.CollectorError, match="content digest"):
        collector.collect_conformance_step(
            probe_result=_probe_result("pack-a", TARGET),
            result_digest="sha256:" + "c" * 64,
            pack_id="pack-a",
            target_digest=TARGET,
            semantic_kind="operation-mapping",
            expected_operation_count=2,
            parent_content_digest="sha256:" + "9" * 64,
            install_step=_step("install"),
        )


def test_conformance_step_rejects_operation_count_drift() -> None:
    with pytest.raises(collector.CollectorError, match="operations"):
        collector.collect_conformance_step(
            probe_result=_probe_result("pack-a", TARGET),
            result_digest="sha256:" + "c" * 64,
            pack_id="pack-a",
            target_digest=TARGET,
            semantic_kind="operation-mapping",
            expected_operation_count=5,
            parent_content_digest=CONTENT,
            install_step=_step("install"),
        )


def test_conformance_step_rejects_failed_check() -> None:
    probe = _probe_result("pack-a", TARGET)
    probe["checks"][2]["status"] = "failed"
    with pytest.raises(collector.CollectorError, match="checks failed"):
        collector.collect_conformance_step(
            probe_result=probe,
            result_digest="sha256:" + "c" * 64,
            pack_id="pack-a",
            target_digest=TARGET,
            semantic_kind="operation-mapping",
            expected_operation_count=2,
            parent_content_digest=CONTENT,
            install_step=_step("install"),
        )


def test_load_receipt_ledger_rejects_generator_authored(tmp_path: Path) -> None:
    path = tmp_path / "receipts.json"
    path.write_text(
        json.dumps(
            {
                "schema": collector.RECEIPT_LEDGER_SCHEMA,
                "authority": collector.RECEIPT_LEDGER_AUTHORITY,
                "generator_id": "script",
                "receipts": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(collector.CollectorError):
        collector._load_receipt_ledger(path)


def test_load_receipt_ledger_preserves_existing(tmp_path: Path) -> None:
    path = tmp_path / "receipts.json"
    existing = {"pack-x": {"pack_id": "pack-x", "marker": True}}
    path.write_text(
        json.dumps(
            {
                "schema": collector.RECEIPT_LEDGER_SCHEMA,
                "authority": collector.RECEIPT_LEDGER_AUTHORITY,
                "generator_id": None,
                "receipts": existing,
            }
        ),
        encoding="utf-8",
    )
    assert collector._load_receipt_ledger(path)["receipts"] == existing


def test_selected_packs_requires_review(tmp_path: Path) -> None:
    reviews: Mapping[str, Any] = {"pack-a": object()}
    args = type(
        "Args", (), {"packs": "pack-a,pack-missing"}
    )()
    with pytest.raises(collector.CollectorError, match="pack-missing"):
        collector._selected_packs(args, reviews)
    args_all = type("Args", (), {"packs": ""})()
    assert collector._selected_packs(args_all, reviews) == ["pack-a"]


@pytest.mark.parametrize("semantic_kind", ["operation-mapping", "admission-only"])
def test_collector_does_not_convert_inventory_counts_into_execution(
    semantic_kind: str,
) -> None:
    """A successful compile/install probe cannot prove its operations ran."""

    probe = _probe_result("pack-a", TARGET)
    probe["operation_inventory"]["verified_count"] = 2
    probe["operation_executions"] = [{"outcome": "passed"}]
    with pytest.raises(collector.CollectorError, match="no supported actual operation"):
        collector.collect_conformance_step(
            probe_result=probe,
            result_digest="sha256:" + "c" * 64,
            pack_id="pack-a",
            target_digest=TARGET,
            semantic_kind=semantic_kind,
            expected_operation_count=2,
            parent_content_digest=CONTENT,
            install_step=_step("install"),
        )


@pytest.mark.parametrize("count", [True, "0", -1, None])
def test_collector_rejects_malformed_inventory_count(count: Any) -> None:
    """Booleans and coerced strings cannot become zero-operation evidence."""

    probe = _probe_result(
        "pack-a", TARGET, operation_inventory={"count": count, "operations": []}
    )
    with pytest.raises(collector.CollectorError, match="lacks operation inventory"):
        collector.collect_conformance_step(
            probe_result=probe,
            result_digest="sha256:" + "c" * 64,
            pack_id="pack-a",
            target_digest=TARGET,
            semantic_kind="zero-operation",
            expected_operation_count=0,
            parent_content_digest=CONTENT,
            install_step=_step("install"),
        )


@pytest.mark.parametrize("mutation", ["empty", "omitted", "duplicate", "malformed"])
def test_collector_requires_exact_conformance_checks(mutation: str) -> None:
    """An empty or incomplete check list cannot manufacture zero-op success."""

    probe = _probe_result(
        "pack-a", TARGET, operation_inventory={"count": 0, "operations": []}
    )
    if mutation == "empty":
        probe["checks"] = []
    elif mutation == "omitted":
        probe["checks"].pop()
    elif mutation == "duplicate":
        probe["checks"].append(probe["checks"][0])
    else:
        probe["checks"][0] = None
    with pytest.raises(collector.CollectorError, match="incomplete checks"):
        collector.collect_conformance_step(
            probe_result=probe,
            result_digest="sha256:" + "c" * 64,
            pack_id="pack-a",
            target_digest=TARGET,
            semantic_kind="zero-operation",
            expected_operation_count=0,
            parent_content_digest=CONTENT,
            install_step=_step("install"),
        )


def test_execution_probe_support_is_fail_closed() -> None:
    """The real subprocess probe uses this guard before admission/install."""

    collector._require_supported_execution_probe("empty", 0)
    with pytest.raises(collector.CollectorError, match="2 inventoried operations"):
        collector._require_supported_execution_probe("executable", 2)
