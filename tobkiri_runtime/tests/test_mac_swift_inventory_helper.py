from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ecosystem.rumi_default_tools_pack.domain.computer.mac import swift_host as swift_host_module
from ecosystem.rumi_default_tools_pack.domain.computer.mac.swift_host import (
    MacSwiftComputerHost,
    SwiftHelperResolutionFacts,
)


def _host(tmp_path: Path) -> MacSwiftComputerHost:
    source = tmp_path / "ComputerUseHost.swift"
    source.write_text("// source", encoding="utf-8")
    binary = tmp_path / "helper"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return MacSwiftComputerHost(source_path=source, binary_path=binary)


def _completed(payload: object, *, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="PRIVATE_STDERR",
    )


def test_run_with_facts_classifies_expected_contract_without_exporting_marker(
    tmp_path, monkeypatch
):
    host = _host(tmp_path)
    monkeypatch.setattr(
        host,
        "_ensure_binary_with_facts",
        lambda: (
            host._binary_path,
            SwiftHelperResolutionFacts(available=True, binary_class="override_mismatch"),
        ),
    )
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.mac.swift_host.platform.system",
        lambda: "Darwin",
    )
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.mac.swift_host.subprocess.run",
        lambda *args, **kwargs: _completed(
            {
                "ok": True,
                "result": {
                    "inventory_diagnostic_contract": "rumi.mac.window_inventory.v3",
                    "windows": [],
                    "inventory_diagnostics": {"selection_native_snapshot_atomic": True},
                },
            }
        ),
    )

    result, facts = host.run_with_facts("computer.windows", {"inventory_diagnostics": True})

    assert result["windows"] == []
    assert "inventory_diagnostic_contract" not in result
    assert facts["selection_swift_helper_response_contract"] == "valid_success"
    assert facts["selection_swift_helper_contract_version_class"] == "expected"
    assert facts["selection_swift_helper_binary_class"] == "override_expected"
    assert facts["selection_swift_helper_invoked"] is True
    assert "PRIVATE" not in str(facts)


@pytest.mark.parametrize("marker", (None, "rumi.mac.window_inventory.v2", "CANARY_CONTRACT"))
def test_run_with_facts_rejects_missing_or_non_v3_inventory_contract(
    tmp_path, monkeypatch, marker
):
    host = _host(tmp_path)
    monkeypatch.setattr(
        host,
        "_ensure_binary_with_facts",
        lambda: (
            host._binary_path,
            SwiftHelperResolutionFacts(available=True, binary_class="pack_reused_current"),
        ),
    )
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.mac.swift_host.platform.system",
        lambda: "Darwin",
    )
    result = {"windows": [], "inventory_diagnostics": {"selection_swift_inventory_contract_valid": True}}
    if marker is not None:
        result["inventory_diagnostic_contract"] = marker
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.mac.swift_host.subprocess.run",
        lambda *args, **kwargs: _completed({"ok": True, "result": result}),
    )

    _, facts = host.run_with_facts("computer.windows", {"inventory_diagnostics": True})

    assert facts["selection_swift_helper_response_contract"] == "valid_success"
    assert facts["selection_swift_helper_contract_version_class"] in {"missing", "mismatch"}
    assert facts["selection_swift_helper_contract_version_class"] != "expected"


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("not-json", 0, "invalid_json"),
        (json.dumps([]), 0, "non_object"),
        (json.dumps({"ok": True, "result": {}}), 2, "process_failure"),
    ],
)
def test_run_with_facts_closes_helper_response_failures(
    tmp_path, monkeypatch, stdout, returncode, expected
):
    host = _host(tmp_path)
    monkeypatch.setattr(
        host,
        "_ensure_binary_with_facts",
        lambda: (
            host._binary_path,
            SwiftHelperResolutionFacts(available=True, binary_class="pack_reused_current"),
        ),
    )
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.mac.swift_host.platform.system",
        lambda: "Darwin",
    )
    monkeypatch.setattr(
        "ecosystem.rumi_default_tools_pack.domain.computer.mac.swift_host.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr="PRIVATE_STDERR"
        ),
    )

    result, facts = host.run_with_facts("computer.windows", {})

    assert result == {}
    assert facts["selection_swift_helper_response_contract"] == expected
    assert "PRIVATE_STDERR" not in str(facts)


def test_binary_resolution_reports_isolated_current_without_paths(tmp_path, monkeypatch):
    host = _host(tmp_path)
    host._binary_path.touch()
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "isolated"))
    monkeypatch.delenv("RUMI_MAC_COMPUTER_USE_HOST", raising=False)

    binary, facts = host._ensure_binary_with_facts()

    assert binary == host._binary_path
    assert facts.binary_class == "isolated_reused_current"
    assert facts.available is True
    assert str(tmp_path) not in str(facts.payload())


def test_helper_path_and_signature_stability_export_only_equality(tmp_path, monkeypatch):
    host = _host(tmp_path)
    monkeypatch.setattr(swift_host_module, "_LAST_HELPER_PATH", None)
    monkeypatch.setattr(swift_host_module, "_LAST_HELPER_SIGNATURE_CLASS", None)
    monkeypatch.setattr(swift_host_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        host, "_ensure_binary_with_facts",
        lambda: (
            host._binary_path,
            SwiftHelperResolutionFacts(
                available=True, binary_class="pack_reused_current",
                persistence_class="reused_current",
            ),
        ),
    )
    payload = {
        "ok": True,
        "result": {
            "inventory_diagnostic_contract": "rumi.mac.window_inventory.v3",
            "inventory_diagnostics": {"selection_swift_helper_signing_class": "ad_hoc"},
            "windows": [],
        },
    }
    monkeypatch.setattr(swift_host_module.subprocess, "run", lambda *args, **kwargs: _completed(payload))

    _, first = host.run_with_facts("computer.windows", {})
    _, second = host.run_with_facts("computer.windows", {})

    assert first["selection_swift_helper_path_stability"] == "first_observation"
    assert first["selection_swift_helper_signature_stability"] == "first_observation"
    assert second["selection_swift_helper_path_stability"] == "same"
    assert second["selection_swift_helper_signature_stability"] == "same"
    assert first["selection_swift_helper_persistence_class"] == "reused_current"
    assert str(tmp_path) not in str(first)


def test_native_inventory_uses_preflights_and_never_permission_request_apis():
    source = (
        Path(__file__).resolve().parent.parent
        / "ecosystem" / "rumi_default_tools_pack" / "domain" / "computer"
        / "mac" / "ComputerUseHost.swift"
    ).read_text(encoding="utf-8")

    assert "AXIsProcessTrusted()" in source
    assert "CGPreflightScreenCaptureAccess()" in source
    assert ".optionAll" in source
    assert "AXIsProcessTrustedWithOptions" not in source
    assert "CGRequestScreenCaptureAccess" not in source
    assert '"selection_swift_all_windows_nonactionable": true' in source
