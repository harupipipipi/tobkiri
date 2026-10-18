from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui_compiler_test_utils import build_args, fake_context, fixture_tree, write_pass_package

from domain.tool.executor import ToolExecutor
from domain.tool.registry import ToolRegistry
from domain.tool.ui_compiler_tools import ui_build_recursive
from domain.tool.ui_compiler_runtime import RecursiveUIBuildOrchestrator, run_recursive_build
from domain.tool.ui_compiler_runtime.audit_orchestrator import UIQualityAuditOrchestrator
from domain.tool.ui_compiler_runtime.fake_agent_backend import FakeUIAgentBackend
from domain.tool.ui_compiler_runtime.subagent_backend import SubagentToolBackend
from domain.ui_compiler import RenderMatrix, RenderSnapshot, UIAgentResult, UIAgentTask
from domain.ui_compiler.planner import RecursiveUIPlanner


pytestmark = pytest.mark.usefixtures("defaultspack_component_catalog_selected")


def _disable_browser_renderer(monkeypatch) -> None:
    """Patch the render module used by the imported orchestrator class."""

    render_runner = RecursiveUIBuildOrchestrator.run.__globals__["RenderMatrixRunner"]
    monkeypatch.setitem(
        render_runner._render_subject.__globals__,
        "_browser_executable_path",
        lambda: "",
    )


def test_tool_registry_exposes_recursive_ui_runtime_tools() -> None:
    ToolRegistry._instance = None
    registry = ToolRegistry()

    assert registry.get("tool_ui_build_recursive")["requires_approval"] is True
    assert registry.get("tool_ui_build_recursive")["risk"] == "high"
    for tool_id in [
        "tool_ui_generate_foundation",
        "tool_ui_generate_candidates",
        "tool_ui_render_matrix",
        "tool_ui_inspect_compression",
        "tool_ui_select_candidates",
        "tool_ui_compose_page",
        "tool_ui_verify_recursive_build",
    ]:
        assert registry.get(tool_id)["write_action"] is True


def test_tool_executor_rejects_unplanned_yolo_context(tmp_path: Path) -> None:
    ToolRegistry._instance = None
    write_pass_package(tmp_path / "project")
    result = ToolExecutor().execute(
        "tool_ui_build_recursive",
        build_args("executor-run"),
        {
            "profile_policy": {"yolo_mode": True},
            "conversation_workspace_dir": str(tmp_path),
            "principal_id": "defaultspack",
            "_ui_compiler_backend": "fake",
        },
    )

    assert result["is_error"] is True
    assert result["error_type"] == "capability_plan_required"
    assert not (tmp_path / ".rumi" / "ui" / "runs" / "executor-run").exists()


def test_raw_yolo_context_cannot_call_runtime_directly(tmp_path: Path) -> None:
    write_pass_package(tmp_path / "project")
    result = ui_build_recursive(
        build_args("raw-runtime"),
        {
            "profile_policy": {"yolo_mode": True},
            "conversation_workspace_dir": str(tmp_path),
            "_ui_compiler_backend": "fake",
        },
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "APPROVAL_REQUIRED"


def test_exported_runtime_helper_does_not_bypass_approval(tmp_path: Path) -> None:
    write_pass_package(tmp_path / "project")
    result = run_recursive_build(
        build_args("raw-helper"),
        {
            "profile_policy": {"yolo_mode": True},
            "conversation_workspace_dir": str(tmp_path),
            "_ui_compiler_backend": "fake",
        },
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "APPROVAL_REQUIRED"


def test_final_report_contains_all_recursive_build_sections(tmp_path: Path) -> None:
    write_pass_package(tmp_path / "project")
    result = ui_build_recursive(build_args("report-run"), fake_context(tmp_path))
    run_root = tmp_path / ".rumi" / "ui" / "runs" / "report-run"
    final = json.loads((run_root / "reports" / "final.json").read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    for key in [
        "intent",
        "foundation",
        "topology",
        "split",
        "candidateGeneration",
        "acceptedSelection",
        "compression",
        "textPressure",
        "typography",
        "colorRoles",
        "surfaceAudit",
        "interactionBudget",
        "responsive",
        "accessibility",
        "qualityAudit",
        "buildTestLint",
        "generatedFilesSummary",
        "recursiveSplitSummary",
        "acceptedFoundationSummary",
        "acceptedLeafBundlesSummary",
        "auditSummary",
        "failedRetriedCandidateSummary",
        "acceptedFoundation",
        "accepted",
        "composition",
        "pageCompression",
        "verification",
        "inspections",
    ]:
        assert key in final
    assert final["qualityAudit"]["status"] == "pass"
    assert final["verification"]["lint"] == "passed"
    assert final["verification"]["test"] == "passed"
    assert final["verification"]["build"] == "passed"
    assert final["applyToProject"]["status"] == "applied"
    assert (tmp_path / "project" / "src" / "rumi-generated" / "report-run" / "manifest.json").is_file()
    assert final["generatedFilesSummary"]["appliedProject"]["entryHint"].endswith("App.tsx")
    assert (run_root / "intent.json").is_file()
    assert (run_root / "topology.json").is_file()
    assert (run_root / "split-manifest.json").is_file()
    assert final["generatedFilesSummary"]["plan"]["intent"].endswith("/intent.json")
    assert final["recursiveSplitSummary"]["contracts"]
    assert final["acceptedLeafBundlesSummary"]
    task_files = {path.name for path in (run_root / "agent-tasks").glob("*.json")}
    assert {
        "report-run-intent-agent.json",
        "report-run-page-topology-agent.json",
        "report-run-semantic-region-planner.json",
        "report-run-text-pressure-auditor.json",
        "report-run-compression-auditor.json",
        "report-run-candidate-selector.json",
        "report-run-composition-agent.json",
        "report-run-refinement-selector.json",
    } <= task_files
    pipeline_tasks = final["generatedFilesSummary"]["plan"].get("pipelineTasks") or []
    assert len(pipeline_tasks) >= 11


def test_recursive_build_runs_specialist_tasks_through_backend(tmp_path: Path) -> None:
    class RecordingBackend(FakeUIAgentBackend):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def run_task(self, task, context=None):
            self.calls.append((task.kind, str(task.metadata.get("role") or "")))
            return super().run_task(task, context)

    backend = RecordingBackend()
    write_pass_package(tmp_path / "project")

    result = RecursiveUIBuildOrchestrator(agent_backend=backend).run(
        build_args("specialist-call-run"),
        workspace_root=tmp_path,
        authorized=True,
        context=fake_context(tmp_path),
    )

    assert result["status"] == "ok"
    call_keys = {role or kind for kind, role in backend.calls}
    assert {
        "product-intent",
        "typography",
        "color-system",
        "spacing-density",
        "surface-policy",
        "motion-state",
        "text-pressure-auditor",
        "responsive-auditor",
    } <= call_keys


def test_recursive_build_fails_when_pipeline_specialist_fails(tmp_path: Path) -> None:
    class FailingBackend(FakeUIAgentBackend):
        def run_task(self, task, context=None):
            if task.kind == "intent":
                return UIAgentResult(status="error", task_id=task.task_id, output_dir=task.output_dir, message="intent failed")
            return super().run_task(task, context)

    write_pass_package(tmp_path / "project")

    result = RecursiveUIBuildOrchestrator(agent_backend=FailingBackend()).run(
        build_args("specialist-fail-run"),
        workspace_root=tmp_path,
        authorized=True,
        context=fake_context(tmp_path),
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "UI_RECURSIVE_BUILD_FAILED"
    assert "pipeline specialist task failed" in result["error"]["message"]


def test_browser_render_synthetic_fallback_is_reported_as_warning(tmp_path: Path, monkeypatch) -> None:
    _disable_browser_renderer(monkeypatch)
    write_pass_package(tmp_path / "project")
    args = build_args("browser-fallback-warn")
    args["options"]["browserRender"] = True

    result = ui_build_recursive(args, fake_context(tmp_path))
    final = json.loads((tmp_path / ".rumi" / "ui" / "runs" / "browser-fallback-warn" / "reports" / "final.json").read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert final["browserRender"]["status"] == "warn"
    assert final["browserRender"]["fallbackCount"] > 0


def test_production_strict_browser_render_fallback_fails_build(tmp_path: Path, monkeypatch) -> None:
    _disable_browser_renderer(monkeypatch)
    write_pass_package(tmp_path / "project")
    args = build_args("browser-fallback-fail")
    args["options"]["browserRender"] = True
    args["options"]["strictProduction"] = True

    result = ui_build_recursive(args, fake_context(tmp_path))
    final = json.loads((tmp_path / ".rumi" / "ui" / "runs" / "browser-fallback-fail" / "reports" / "final.json").read_text(encoding="utf-8"))

    assert result["status"] == "error"
    assert result["error"]["code"] == "UI_RECURSIVE_BUILD_FAILED"
    assert final["browserRender"]["status"] == "fail"
    assert final["applyToProject"]["status"] == "skipped"
    assert not (tmp_path / "project" / "src" / "rumi-generated" / "browser-fallback-fail" / "manifest.json").exists()


def test_render_matrix_stage_strict_browser_fallback_fails_before_selection(tmp_path: Path, monkeypatch) -> None:
    _disable_browser_renderer(monkeypatch)
    write_pass_package(tmp_path / "project")
    args = build_args("browser-fallback-stage-fail")
    args["options"]["browserRender"] = True
    args["options"]["strictProduction"] = True
    args["options"]["stopAfter"] = "renderMatrix"

    result = ui_build_recursive(args, fake_context(tmp_path))
    final = json.loads((tmp_path / ".rumi" / "ui" / "runs" / "browser-fallback-stage-fail" / "reports" / "final.json").read_text(encoding="utf-8"))

    assert result["status"] == "error"
    assert result["error"]["code"] == "UI_RECURSIVE_BUILD_FAILED"
    assert final["browserRender"]["status"] == "fail"
    assert final["browserRender"]["fallbackCount"] > 0


def test_quality_audit_fails_text_overload_as_first_class_section() -> None:
    plan = RecursiveUIPlanner().plan(fixture_tree(), run_id="audit-overload")
    snapshot = RenderSnapshot(
        subject_id="page",
        candidate_id="composition",
        viewport=390,
        scenario="long",
        text_scale=1,
        image_path="",
        dom_path="",
        console_path="",
        metrics={
            "viewport": 390,
            "visibleTextBlocks": 14,
            "visibleCharacters": 1600,
            "averageLineLength": 108,
            "lineClampCount": 1,
            "ellipsisCount": 3,
            "labelDensity": 3,
            "japaneseBreakQuality": 0.7,
            "visibleActions": 2,
            "allowedActions": 3,
            "mobileDisclosureUsed": True,
            "contrastMin": 4.8,
            "focusVisible": True,
            "keyboardNav": True,
            "ariaRoles": 3,
        },
    )
    foundation = {
        "direction": {"productMode": "utility"},
        "typography": {"roles": {role: {} for role in ["pageTitle", "sectionTitle", "body", "label", "caption", "numeric", "code"]}},
        "spacing": {"density": "compact"},
        "color": {"roles": ["canvas", "surface", "textPrimary", "textSecondary", "actionPrimary", "statusCritical"]},
        "surface": {"maxNestedDepth": 1},
        "primitives": ["Button", "TextInput"],
    }

    audit = UIQualityAuditOrchestrator().audit(
        plan=plan,
        foundation=foundation,
        page_matrix=RenderMatrix(subject_id="page", candidate_id="composition", snapshots=[snapshot]),
        page_compression={"status": "pass", "compressionScore": 0.95, "metrics": {}, "issues": []},
        accepted_count=3,
    )

    assert audit["status"] == "fail"
    assert "textPressure" in audit["failedAudits"]
    assert any("visible character" in issue["message"] for issue in audit["textPressure"]["issues"])


def test_quality_audit_fails_visual_abuse_budget_and_accessibility_sections() -> None:
    plan = RecursiveUIPlanner().plan(fixture_tree(), run_id="audit-visual-abuse")
    snapshot = RenderSnapshot(
        subject_id="page",
        candidate_id="composition",
        viewport=390,
        scenario="default",
        text_scale=1,
        image_path="",
        dom_path="",
        console_path="",
        metrics={
            "viewport": 390,
            "visibleTextBlocks": 4,
            "visibleCharacters": 360,
            "averageLineLength": 70,
            "visibleActions": 8,
            "allowedActions": 2,
            "horizontalOverflow": True,
            "toolbarOverflow": 1,
            "primaryActionUnreachable": 1,
            "touchTargetFailures": 1,
            "gradientCount": 2,
            "nonSemanticColorCount": 3,
            "mutedTextRatio": 0.8,
            "surfaceDepth": 4,
            "cardNestingDepth": 3,
            "borderCount": 14,
            "shadowCount": 3,
            "radiusUniformity": 0.98,
            "mobileDisclosureUsed": False,
            "contrastMin": 3.8,
            "focusVisible": False,
            "keyboardNav": False,
            "ariaRoles": 0,
        },
    )
    foundation = {
        "direction": {"productMode": "utility"},
        "typography": {"roles": {role: {} for role in ["pageTitle", "sectionTitle", "body", "label", "caption", "numeric", "code"]}},
        "spacing": {"density": "compact"},
        "color": {"roles": ["canvas", "surface", "textPrimary", "textSecondary", "actionPrimary", "statusCritical"]},
        "surface": {"maxNestedDepth": 1},
        "primitives": ["Button", "TextInput"],
    }

    audit = UIQualityAuditOrchestrator().audit(
        plan=plan,
        foundation=foundation,
        page_matrix=RenderMatrix(subject_id="page", candidate_id="composition", snapshots=[snapshot]),
        page_compression={"status": "pass", "compressionScore": 0.95, "metrics": {}, "issues": []},
        accepted_count=3,
    )

    assert {
        "topology",
        "colorRoles",
        "surfaceAudit",
        "interactionBudget",
        "responsive",
        "accessibility",
    } <= set(audit["failedAudits"])
    assert any("gradient" in issue["message"] for issue in audit["colorRoles"]["issues"])
    assert any("card nesting" in issue["message"] for issue in audit["surfaceAudit"]["issues"])
    assert any("visible action budget" in issue["message"] for issue in audit["interactionBudget"]["issues"])


def test_quality_audit_fails_missing_typography_role_map() -> None:
    plan = RecursiveUIPlanner().plan(fixture_tree(), run_id="audit-type-map")
    snapshot = RenderSnapshot(
        subject_id="page",
        candidate_id="composition",
        viewport=768,
        scenario="default",
        text_scale=1,
        image_path="",
        dom_path="",
        console_path="",
        metrics={
            "viewport": 768,
            "visibleTextBlocks": 4,
            "visibleCharacters": 300,
            "averageLineLength": 62,
            "visibleActions": 2,
            "allowedActions": 3,
            "mobileDisclosureUsed": True,
            "contrastMin": 4.8,
            "focusVisible": True,
            "keyboardNav": True,
            "ariaRoles": 2,
        },
    )
    foundation = {
        "direction": {"productMode": "utility"},
        "typography": {"roles": {"body": {}, "label": {}}},
        "spacing": {"density": "compact"},
        "color": {"roles": ["canvas", "surface", "textPrimary", "textSecondary", "actionPrimary", "statusCritical"]},
        "surface": {"maxNestedDepth": 1},
        "primitives": ["Button", "TextInput"],
    }

    audit = UIQualityAuditOrchestrator().audit(
        plan=plan,
        foundation=foundation,
        page_matrix=RenderMatrix(subject_id="page", candidate_id="composition", snapshots=[snapshot]),
        page_compression={"status": "pass", "compressionScore": 0.95, "metrics": {}, "issues": []},
        accepted_count=3,
    )

    assert "typography" in audit["failedAudits"]
    assert any("missing typography roles" in issue["message"] for issue in audit["typography"]["issues"])


def test_recursive_ui_requires_captured_operation() -> None:
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "POST",
        "/api/ui/build-recursive",
        "tobkiri.ui-build.v1",
        "defaultspack.ui-build.recursive",
    )


def test_subagent_tool_backend_runs_real_delegate_path(tmp_path: Path, monkeypatch) -> None:
    from domain.agent import subagent_orchestrator

    def fake_delegate(role_id, payload, *, model="", settings=None, call_handler=None, context=None):
        output_dir = Path(payload["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "result.txt").write_text("delegate wrote output", encoding="utf-8")
        return {"role_id": role_id, "route_kind": "agent.delegate", "model": model}

    monkeypatch.setattr(subagent_orchestrator, "run_subagent_compat", fake_delegate)
    task = UIAgentTask(
        task_id="delegate-task",
        run_id="delegate-run",
        node_id="delegate-node",
        candidate_id="candidate-1",
        kind="leaf",
        prompt="create a component",
        output_dir=str(tmp_path / "candidate"),
        allowed_paths=[str(tmp_path / "candidate")],
    )

    result = SubagentToolBackend().run_task(task, {})

    assert result.ok
    assert result.files == ["result.txt"]
    assert result.metadata["subagent"]["route_kind"] == "agent.delegate"


def test_subagent_compat_forwards_output_contract_to_delegate_params(tmp_path: Path, monkeypatch) -> None:
    from domain.agent.subagent_orchestrator import run_subagent_compat
    from domain.input import dispatcher

    captured = {}

    def fake_dispatch(envelope, context):
        captured["envelope"] = envelope.as_dict()
        captured["context"] = dict(context)
        return {"status": "ok", "delegate": {"status": "completed"}}

    monkeypatch.setattr(dispatcher, "dispatch_input", fake_dispatch)
    output_dir = tmp_path / "candidate"
    result = run_subagent_compat(
        "delegate",
        {
            "task": "create candidate bundle",
            "output_dir": str(output_dir),
            "allowed_paths": [str(output_dir)],
            "metadata": {"nodeId": "reply-composer"},
        },
        context={},
    )

    params = captured["envelope"]["params"]["params"]
    assert result["route_kind"] == "agent.delegate"
    assert params["output_dir"] == str(output_dir)
    assert params["allowed_paths"] == [str(output_dir)]
    assert params["workspace_write_contract"]["mode"] == "create-from-empty-directory"
