from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui_compiler_test_utils import build_args, fake_context, fixture_tree, write_pass_package

from domain.tool.ui_compiler_tools import ui_build_recursive
from domain.tool.ui_compiler_tools import ui_generate_foundation, ui_render_matrix
from domain.tool.ui_compiler_runtime.foundation_generator import FOUNDATION_SPECIALIST_ROLES
from domain.tool.ui_compiler_runtime.orchestrator import PIPELINE_SPECIALIST_TASKS
import domain.tool.ui_compiler_runtime.render_matrix as render_matrix_runtime
from domain.tool.ui_compiler_runtime.render_matrix import _browser_executable_path
from domain.tool_policy.internal_context import mark_tool_server_approval_context
from domain.ui_compiler import RecursiveUIPlanner


def test_build_recursive_rejects_without_internal_approval_and_workspace(tmp_path: Path) -> None:
    raw_yolo = ui_build_recursive(
        build_args("raw-yolo"),
        {"profile_policy": {"yolo_mode": True}, "conversation_workspace_dir": str(tmp_path)},
    )
    no_workspace = ui_build_recursive(build_args("no-workspace"), mark_tool_server_approval_context({}))

    assert raw_yolo["status"] == "error"
    assert raw_yolo["error"]["code"] == "APPROVAL_REQUIRED"
    assert no_workspace["status"] == "error"


def test_build_recursive_creates_run_bundle_tasks_and_candidates(tmp_path: Path) -> None:
    write_pass_package(tmp_path / "project")
    result = ui_build_recursive(build_args("runtime-run"), fake_context(tmp_path))

    run_root = tmp_path / ".rumi" / "ui" / "runs" / "runtime-run"
    task_files = sorted((run_root / "agent-tasks").glob("*.json"))
    task_payloads = [json.loads(path.read_text(encoding="utf-8")) for path in task_files]
    task_kinds = {payload["kind"] for payload in task_payloads}
    expected_task_count = (
        len(PIPELINE_SPECIALIST_TASKS)
        + result["data"]["summary"]["foundationCandidates"] * (1 + len(FOUNDATION_SPECIALIST_ROLES))
        + result["data"]["summary"]["candidateBundles"]
    )

    assert result["status"] == "ok"
    assert result["data"]["summary"]["foundationCandidates"] == 3
    assert result["data"]["summary"]["contracts"] == 3
    assert result["data"]["summary"]["candidateBundles"] == 5
    assert (run_root / "foundation" / "accepted" / "foundation.json").is_file()
    assert (run_root / "reports" / "final.json").is_file()
    assert len(task_files) == expected_task_count
    assert {"foundation", "foundation-typography", "intent", "topology", "leaf"} <= task_kinds
    assert all(payload["outputDir"] for payload in task_payloads)
    assert all(payload["allowedPaths"] for payload in task_payloads)


def test_stage_tools_stop_after_the_requested_runtime_stage(tmp_path: Path) -> None:
    write_pass_package(tmp_path / "project")
    foundation = ui_generate_foundation(build_args("stage-foundation"), fake_context(tmp_path))
    render = ui_render_matrix(build_args("stage-render"), fake_context(tmp_path))

    assert foundation["status"] == "ok"
    assert foundation["data"]["stage"] == "tool_ui_generate_foundation"
    assert foundation["data"]["summary"]["candidateBundles"] == 0
    assert (tmp_path / ".rumi" / "ui" / "runs" / "stage-foundation" / "foundation" / "accepted" / "foundation.json").is_file()
    assert not (tmp_path / ".rumi" / "ui" / "runs" / "stage-foundation" / "composition" / "page.manifest.json").exists()
    assert render["status"] == "ok"
    assert render["data"]["stage"] == "tool_ui_render_matrix"
    assert (tmp_path / ".rumi" / "ui" / "runs" / "stage-render" / "candidates" / "reply-composer" / "candidate-1" / "renders" / "matrix.json").is_file()
    assert not (tmp_path / ".rumi" / "ui" / "runs" / "stage-render" / "accepted" / "reply-composer").exists()


def test_render_matrix_uses_browser_when_local_chrome_is_available(tmp_path: Path) -> None:
    pytest.importorskip("playwright.sync_api", reason="optional Playwright Python package is unavailable")
    executable_path = _browser_executable_path()
    if not executable_path:
        pytest.skip("no local Chrome, Chromium, or installed Playwright browser is available")
    write_pass_package(tmp_path / "project")
    args = build_args("browser-render")
    args["options"]["browserRender"] = True
    result = ui_render_matrix(args, fake_context(tmp_path))
    render_root = (
        tmp_path
        / ".rumi"
        / "ui"
        / "runs"
        / "browser-render"
        / "candidates"
        / "reply-composer"
        / "candidate-1"
        / "renders"
    )
    dom_path = next(render_root.glob("dom-390-default-text-*.json"))
    dom = json.loads(dom_path.read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert dom["renderer"] == "playwright"
    assert dom["dom"]["document"]["clientWidth"] == 390
    assert dom["metrics"]["browserExecutablePath"] == executable_path
    assert dom["metrics"]["browserExecutableSource"] in {"system", "playwright-cache"}
    assert dom["metrics"]["browserVersion"]
    assert dom["metrics"]["browserTimeoutMs"] == 10_000
    assert dom["metrics"]["browserSandbox"] == "enabled"
    assert dom["metrics"]["browserCleanup"] == "context-and-browser-closed"
    image_path = dom_path.with_name(dom_path.name.removeprefix("dom-")).with_suffix(".png")
    assert image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_render_matrix_falls_back_to_synthetic_without_a_browser(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(render_matrix_runtime, "_browser_executable_path", lambda: "")
    write_pass_package(tmp_path / "project")
    args = build_args("synthetic-render")
    args["options"]["browserRender"] = True

    result = ui_render_matrix(args, fake_context(tmp_path))
    render_root = (
        tmp_path
        / ".rumi"
        / "ui"
        / "runs"
        / "synthetic-render"
        / "candidates"
        / "reply-composer"
        / "candidate-1"
        / "renders"
    )
    dom_path = next(render_root.glob("dom-390-default-text-*.json"))
    dom = json.loads(dom_path.read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert dom["renderer"] == "synthetic"
    assert dom["metrics"]["browserRenderFallback"] is True
    assert dom["metrics"]["browserRenderFallbackReason"] == "local Chromium executable not found"
    assert dom["metrics"]["browserExecutablePath"] is None
    assert dom["metrics"]["browserCleanup"] == "not-started"


def test_browser_discovery_uses_platform_path_candidates(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "google-chrome"
    executable.write_bytes(b"browser")
    executable.chmod(0o755)
    monkeypatch.setattr(render_matrix_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        render_matrix_runtime.shutil,
        "which",
        lambda name: str(executable) if name == "google-chrome" else None,
    )
    monkeypatch.setattr(render_matrix_runtime, "_playwright_browser_executable_path", lambda: "")

    assert _browser_executable_path() == str(executable.resolve())


def test_browser_discovery_uses_macos_application_bundle(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"browser")
    executable.chmod(0o755)
    monkeypatch.setattr(render_matrix_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(render_matrix_runtime.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(render_matrix_runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        render_matrix_runtime,
        "_validated_executable_path",
        lambda candidate: str(executable.resolve()) if str(candidate) == str(executable) else "",
    )
    monkeypatch.setattr(render_matrix_runtime, "_playwright_browser_executable_path", lambda: "")

    assert _browser_executable_path() == str(executable.resolve())


def test_browser_discovery_uses_windows_installation_roots(tmp_path: Path, monkeypatch) -> None:
    program_files = tmp_path / "Program Files"
    executable = program_files / "Google" / "Chrome" / "Application" / "chrome.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"browser")
    monkeypatch.setattr(render_matrix_runtime.platform, "system", lambda: "Windows")
    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("PROGRAMW6432", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(render_matrix_runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(render_matrix_runtime, "_playwright_browser_executable_path", lambda: "")

    assert _browser_executable_path() == str(executable.resolve())


def test_candidate_count_output_dirs_are_isolated_and_do_not_include_previous_source(tmp_path: Path) -> None:
    write_pass_package(tmp_path / "project")
    ui_build_recursive(build_args("isolation-run"), fake_context(tmp_path))
    run_root = tmp_path / ".rumi" / "ui" / "runs" / "isolation-run"
    composer_tasks = sorted((run_root / "agent-tasks").glob("*reply-composer-candidate-*.json"))

    assert len(composer_tasks) == 2
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in composer_tasks]
    assert len({payload["outputDir"] for payload in payloads}) == 2
    assert all("previous candidate" not in payload["prompt"].lower() for payload in payloads)


def test_failed_candidate_is_not_accepted_and_failed_node_is_regenerated(tmp_path: Path) -> None:
    write_pass_package(tmp_path / "project")
    partial = build_args("selector-run")
    partial["options"]["fakeFailures"] = {"reply-composer/candidate-1": "action-pressure"}
    partial_result = ui_build_recursive(partial, fake_context(tmp_path))
    selection = json.loads(
        (tmp_path / ".rumi" / "ui" / "runs" / "selector-run" / "accepted" / "reply-composer" / "selection.json")
        .read_text(encoding="utf-8")
    )

    all_failed = build_args("selector-fail")
    all_failed["options"]["fakeFailures"] = {
        "reply-composer/candidate-1": "action-pressure",
        "reply-composer/candidate-2": "action-pressure",
    }
    regenerated_result = ui_build_recursive(all_failed, fake_context(tmp_path))
    regenerated_selection = json.loads(
        (tmp_path / ".rumi" / "ui" / "runs" / "selector-fail" / "accepted" / "reply-composer" / "selection.json")
        .read_text(encoding="utf-8")
    )

    assert partial_result["status"] == "ok"
    assert selection["acceptedCandidateId"] == "candidate-2"
    assert any(item["candidateId"] == "candidate-1" for item in selection["rejected"])
    assert regenerated_result["status"] == "ok"
    assert regenerated_selection["acceptedCandidateId"] == "candidate-retry-1"
    assert (tmp_path / ".rumi" / "ui" / "runs" / "selector-fail" / "candidates" / "reply-composer" / "candidate-retry-1").is_dir()


def test_regeneration_failure_fails_run_with_split_recommendation(tmp_path: Path) -> None:
    write_pass_package(tmp_path / "project")
    args = build_args("selector-regeneration-fail")
    args["options"]["fakeFailures"] = {
        "reply-composer/candidate-1": "action-pressure",
        "reply-composer/candidate-2": "action-pressure",
        "reply-composer/candidate-retry-1": "action-pressure",
    }

    result = ui_build_recursive(args, fake_context(tmp_path))
    final = json.loads(
        (tmp_path / ".rumi" / "ui" / "runs" / "selector-regeneration-fail" / "reports" / "final.json")
        .read_text(encoding="utf-8")
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "UI_RECURSIVE_BUILD_FAILED"
    assert final["failure"]["failedNodeId"] == "reply-composer"
    assert final["failure"]["attempts"] == [
        "initial-candidates",
        "regenerate-empty-directory-candidate",
        "semantic-split-recommended",
    ]


def test_rerun_with_same_idempotency_key_returns_existing_final_report(tmp_path: Path) -> None:
    write_pass_package(tmp_path / "project")
    args = build_args("idempotent-run")
    args["idempotency_key"] = "same-request"

    first = ui_build_recursive(args, fake_context(tmp_path))
    second = ui_build_recursive(args, fake_context(tmp_path))

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert second["data"]["idempotent"] is True
    assert second["data"]["summary"] == first["data"]["summary"]


def test_calendar_fixture_contains_required_recursive_calendar_responsibilities() -> None:
    plan = RecursiveUIPlanner().plan(fixture_tree("calendar_contract"), run_id="calendar-fixture")
    contract_ids = {contract.id for contract in plan.contracts()}

    assert plan.is_executable()
    assert {"week-grid", "time-axis", "event-block", "mobile-agenda", "event-editor"}.issubset(contract_ids)


def test_advanced_webapps_fixture_contains_high_complexity_website_contracts() -> None:
    plan = RecursiveUIPlanner().plan(fixture_tree("advanced_webapps_contract"), run_id="advanced-webapps-fixture")
    contract_ids = {contract.id for contract in plan.contracts()}

    assert plan.is_executable(), [diagnostic.to_dict() for diagnostic in plan.diagnostics]
    assert len(plan.contracts()) == 25
    assert {
        "commerce-shell",
        "product-gallery",
        "option-matrix",
        "compatibility-alerts",
        "quote-summary",
        "checkout-cta",
        "clinical-shell",
        "patient-summary",
        "symptom-triage",
        "medication-history",
        "consent-review",
        "appointment-action",
        "fintech-shell",
        "transaction-summary",
        "risk-signal-stack",
        "exception-review",
        "audit-ledger",
        "approval-actions",
        "admin-shell",
        "grid-shell",
        "filter-builder",
        "bulk-action-bar",
        "row-detail-drawer",
        "audit-state-rail",
    }.issubset(contract_ids)


def test_recursive_build_dogfoods_advanced_webapps_fixture(tmp_path: Path) -> None:
    write_pass_package(tmp_path / "project")
    args = build_args("advanced-webapps-run", tree_name="advanced_webapps_contract")
    args["options"] = {
        "viewports": [390, 1440],
        "scenarios": ["default", "long", "error"],
        "textScales": [1],
        "runBuild": True,
    }

    result = ui_build_recursive(args, fake_context(tmp_path))
    run_root = tmp_path / ".rumi" / "ui" / "runs" / "advanced-webapps-run"
    final = json.loads((run_root / "reports" / "final.json").read_text(encoding="utf-8"))
    composition_report = json.loads((run_root / "composition" / "report.json").read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert result["data"]["summary"]["contracts"] == 25
    assert result["data"]["summary"]["acceptedBundles"] == 25
    assert result["data"]["summary"]["foundationCandidates"] == 3
    assert result["data"]["summary"]["candidateBundles"] >= 25
    assert final["status"] == "ok"
    assert final["summary"]["compressionFailures"] == 0
    assert final["verification"]["build"] == "passed"
    assert composition_report["leafSourceEdited"] is False
    assert composition_report["renderTree"]["nodeId"] == "advanced-webapps-suite"
    assert {child["nodeId"] for child in composition_report["renderTree"]["children"]} == {
        "commerce-shell",
        "clinical-shell",
        "fintech-shell",
        "admin-shell",
    }
