from __future__ import annotations

import json
import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_component_catalog_selected")

from domain.tool.executor import ToolExecutor  # noqa: E402
from domain.tool.registry import ToolRegistry  # noqa: E402
from domain.tool.ui_compiler_tools import ui_commit_plan  # noqa: E402
from domain.tool_policy.internal_context import mark_tool_server_approval_context  # noqa: E402
from domain.ui_compiler import (  # noqa: E402
    ComplexitySignals,
    LeafBudget,
    RecursiveUIPlanner,
    UICompilerArtifactStore,
    budget_violations,
    calculate_complexity,
    compile_ui_plan,
)


def _attached_plan_context(tool_id: str, **context: object) -> dict[str, object]:
    from core_runtime.capability_plan import canonical_capability_plan_digest

    tool = ToolRegistry().get(tool_id)
    assert isinstance(tool, dict), tool_id
    schema = tool.get("schema")
    if not isinstance(schema, dict):
        contract = tool.get("contract")
        schema = (
            contract.get("input_schema")
            if isinstance(contract, dict)
            and isinstance(contract.get("input_schema"), dict)
            else {}
        )
    plan = {
        "schema_version": "tobkiri.capability-plan/v1",
        "plan_id": f"plan_ui_compiler_{tool_id}",
        "registry_revision": "registry_test",
        "effective_capabilities": [],
        "provider_selections": {},
        "tools": {
            "attached": [tool_id],
            "schema_hashes": {
                tool_id: hashlib.sha256(
                    json.dumps(
                        schema,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
            },
        },
    }
    plan["digest"] = canonical_capability_plan_digest(plan)
    return {"principal_id": "defaultspack", "capability_plan": plan, **context}


def _valid_inbox_tree() -> dict:
    return {
        "id": "inbox-page-frame",
        "purpose": "Process unresolved conversations quickly.",
        "density": "compact",
        "implementationMode": "component-with-slots",
        "importance": "pageFrame",
        "responsibilities": {
            "visualRoles": ["toolbar", "reply-composer"],
            "controls": [],
            "mutations": [],
            "states": [],
            "responsiveTopologies": ["inbox-two-region-frame"],
        },
        "slots": [
            {
                "id": "toolbar",
                "acceptsNodeId": "inbox-toolbar",
                "purpose": "Filter controls",
                "minWidth": 280,
            },
            {
                "id": "reply-composer",
                "acceptsNodeId": "reply-composer",
                "purpose": "Reply region",
                "minWidth": 280,
            },
        ],
        "children": [
            {
                "id": "inbox-toolbar",
                "purpose": "Filter the conversation set.",
                "density": "compact",
                "importance": "secondaryRegion",
                "responsibilities": {
                    "visualRoles": ["toolbar"],
                    "controls": ["query", "onQueryChange", "status-filter"],
                    "mutations": [],
                    "states": ["default", "filtered"],
                },
                "inputs": ["query"],
                "events": ["onQueryChange"],
                "requiredStates": ["default", "filtered"],
                "allowedPrimitives": ["Button", "SegmentedControl", "SearchField"],
                "visibleActionBudget": 3,
            },
            {
                "id": "reply-composer",
                "purpose": "Send a safe reply to the selected conversation.",
                "primaryPerceptualTask": "Understand draft state and send readiness.",
                "density": "comfortable",
                "importance": "primaryRegion",
                "layoutEnvelope": {
                    "minWidth": 280,
                    "preferredWidth": 560,
                    "maxWidth": 760,
                    "heightBehavior": "content",
                    "mobileBehavior": "sticky-bottom",
                },
                "responsibilities": {
                    "visualRoles": ["reply-composer"],
                    "controls": [
                        "draft",
                        "onDraftChange",
                        "isSending",
                        "error",
                        "onSend",
                        "onRetry",
                        "attachments",
                        "onAttach",
                    ],
                    "mutations": ["send-reply"],
                    "states": ["empty", "editing", "sending", "error", "sent"],
                    "responsiveTopologies": ["composer-mobile-stack"],
                },
                "ownership": [
                    {
                        "id": "send-reply",
                        "controls": ["error", "onSend", "onRetry"],
                        "mutations": ["send-reply"],
                        "states": ["sending", "error"],
                    }
                ],
                "inputs": ["draft", "isSending", "error", "attachments"],
                "events": ["onDraftChange", "onSend", "onRetry", "onAttach"],
                "requiredStates": ["empty", "editing", "sending", "error", "sent"],
                "allowedPrimitives": ["Button", "TextArea", "InlineAlert", "IconButton"],
                "visibleActionBudget": 3,
                "splitHints": [
                    {
                        "id": "reply-composer-draft-input",
                        "purpose": "Capture and review the reply draft.",
                        "density": "comfortable",
                        "importance": "primaryRegion",
                        "layoutEnvelope": {
                            "minWidth": 280,
                            "preferredWidth": 560,
                            "maxWidth": 760,
                        },
                        "responsibilities": {
                            "visualRoles": ["reply-composer"],
                            "controls": ["draft", "onDraftChange"],
                            "mutations": [],
                            "states": ["empty", "editing"],
                            "responsiveTopologies": ["composer-mobile-stack"],
                        },
                        "inputs": ["draft"],
                        "events": ["onDraftChange"],
                        "requiredStates": ["empty", "editing"],
                        "allowedPrimitives": ["TextArea"],
                        "visibleActionBudget": 1,
                    },
                    {
                        "id": "reply-composer-send-controls",
                        "purpose": "Expose send, retry, and readiness actions.",
                        "density": "comfortable",
                        "importance": "primaryRegion",
                        "layoutEnvelope": {
                            "minWidth": 280,
                            "preferredWidth": 560,
                            "maxWidth": 760,
                        },
                        "responsibilities": {
                            "visualRoles": [],
                            "controls": ["isSending", "error", "onSend", "onRetry"],
                            "mutations": ["send-reply"],
                            "states": ["sending", "error", "sent"],
                        },
                        "inputs": ["isSending", "error"],
                        "events": ["onSend", "onRetry"],
                        "requiredStates": ["sending", "error", "sent"],
                        "allowedPrimitives": ["Button", "InlineAlert"],
                        "visibleActionBudget": 3,
                    },
                    {
                        "id": "reply-composer-attachment-tray",
                        "purpose": "Show attachment state without crowding send controls.",
                        "density": "comfortable",
                        "importance": "secondaryRegion",
                        "layoutEnvelope": {
                            "minWidth": 280,
                            "preferredWidth": 560,
                            "maxWidth": 760,
                        },
                        "responsibilities": {
                            "visualRoles": [],
                            "controls": ["attachments", "onAttach"],
                            "mutations": [],
                            "states": [],
                        },
                        "inputs": ["attachments"],
                        "events": ["onAttach"],
                        "allowedPrimitives": ["IconButton", "InlineAlert"],
                        "visibleActionBudget": 1,
                    },
                ],
            },
        ],
    }


def test_complexity_formula_and_budget_violations() -> None:
    signals = ComplexitySignals(
        unique_visual_roles=10,
        interactive_controls=3,
        meaningful_states=4,
        async_mutations=1,
        responsive_topologies=2,
        special_layout_algorithms=1,
    )

    assert calculate_complexity(signals) == 41
    assert budget_violations(signals, LeafBudget(max_complexity=40)) == ["complexity"]


def test_valid_tree_generates_page_frame_and_leaf_contracts() -> None:
    plan = RecursiveUIPlanner().plan(_valid_inbox_tree(), run_id="inbox-demo")
    contract_by_id = {contract.id: contract for contract in plan.contracts()}

    assert plan.is_executable()
    assert plan.to_dict()["summary"]["overBudgetLeafCount"] == 0
    assert set(contract_by_id) == {
        "inbox-page-frame",
        "inbox-toolbar",
        "reply-composer-draft-input",
        "reply-composer-send-controls",
        "reply-composer-attachment-tray",
    }
    frame = contract_by_id["inbox-page-frame"]
    assert frame.implementation_mode == "component-with-slots"
    assert [slot.id for slot in frame.slots] == ["toolbar", "reply-composer"]
    frame_payload = frame.to_dict()
    assert frame_payload["slots"][0]["acceptsNodeId"] == "inbox-toolbar"
    assert frame_payload["slotMappings"] == [
        {"slotId": "toolbar", "nodeId": "inbox-toolbar"},
        {"slotId": "reply-composer", "nodeId": "reply-composer"},
    ]
    assert contract_by_id["reply-composer-send-controls"].candidate_count == 2


def test_contractless_leaf_is_not_executable() -> None:
    result = compile_ui_plan({"ui_tree": {"id": "root", "implementationMode": "group-only"}})

    assert result["status"] == "error"
    assert result["error"]["code"] == "PLAN_NOT_EXECUTABLE"
    partial = result["data"]["partialPlan"]
    assert partial["summary"]["unimplementedLeafCount"] == 1
    assert any(item["code"] == "UNIMPLEMENTED_LEAF_NODE" for item in partial["diagnostics"])


def test_component_slots_must_map_to_children() -> None:
    tree = _valid_inbox_tree()
    tree["slots"][0].pop("acceptsNodeId")
    unknown = _valid_inbox_tree()
    unknown["slots"][1]["acceptsNodeId"] = "missing-region"

    missing = compile_ui_plan({"ui_tree": tree})
    bad_ref = compile_ui_plan({"ui_tree": unknown})

    assert missing["status"] == "error"
    assert any(
        item["code"] == "REQUIRED_SLOT_UNASSIGNED"
        for item in missing["data"]["partialPlan"]["diagnostics"]
    )
    assert bad_ref["status"] == "error"
    assert any(
        item["code"] == "SLOT_ACCEPTS_UNKNOWN_CHILD"
        for item in bad_ref["data"]["partialPlan"]["diagnostics"]
    )


def test_composition_only_cannot_define_slots() -> None:
    result = compile_ui_plan(
        {
            "ui_tree": {
                "id": "frame",
                "implementationMode": "composition-only",
                "slots": [{"id": "main", "acceptsNodeId": "main-region"}],
                "children": [{"id": "main-region"}],
            }
        }
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "PLAN_NOT_EXECUTABLE"
    assert any(
        item["code"] == "INVALID_UI_TREE"
        for item in result["data"]["partialPlan"]["diagnostics"]
    )


def test_explicit_children_cannot_hide_parent_complexity() -> None:
    result = compile_ui_plan(
        {
            "ui_tree": {
                "id": "inbox",
                "complexity": {"interactiveControls": 100, "asyncMutations": 20},
                "events": ["onReply", "onAssign", "onResolve"],
                "children": [{"id": "empty-child"}],
            }
        }
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "PLAN_NOT_EXECUTABLE"
    codes = {item["code"] for item in result["data"]["diagnostics"]}
    assert "REQUIRES_RESPONSIBILITY_IDS" in codes


def test_split_hints_must_cover_parent_responsibilities() -> None:
    tree = _valid_inbox_tree()
    composer = tree["children"][1]
    composer["splitHints"][1]["responsibilities"]["controls"].remove("onRetry")
    composer["splitHints"][1]["events"].remove("onRetry")

    result = compile_ui_plan({"ui_tree": tree})

    assert result["status"] == "error"
    diagnostics = result["data"]["diagnostics"]
    assert any(
        item["code"] == "RESPONSIBILITY_COVERAGE_MISSING"
        and item["details"]["responsibilityId"] == "onRetry"
        for item in diagnostics
    )


def test_layout_and_responsive_responsibilities_must_be_named_and_covered() -> None:
    unnamed = compile_ui_plan(
        {
            "ui_tree": {
                "id": "calendar-page",
                "complexity": {
                    "uniqueVisualRoles": 1,
                    "specialLayoutAlgorithms": 1,
                    "responsiveTopologies": 2,
                },
                "responsibilities": {
                    "visualRoles": ["calendar-grid"],
                    "responsiveTopologies": ["desktop-week-grid"],
                },
                "children": [
                    {
                        "id": "calendar-grid",
                        "responsibilities": {
                            "visualRoles": ["calendar-grid"],
                            "responsiveTopologies": ["desktop-week-grid"],
                        },
                    }
                ],
            }
        }
    )
    dropped = compile_ui_plan(
        {
            "ui_tree": {
                "id": "calendar-page",
                "complexity": {
                    "uniqueVisualRoles": 1,
                    "specialLayoutAlgorithms": 1,
                    "responsiveTopologies": 1,
                },
                "responsibilities": {
                    "visualRoles": ["calendar-grid"],
                    "layoutAlgorithms": ["overlap-layout"],
                    "responsiveTopologies": ["desktop-week-grid"],
                },
                "children": [
                    {
                        "id": "calendar-grid",
                        "responsibilities": {
                            "visualRoles": ["calendar-grid"],
                            "responsiveTopologies": ["desktop-week-grid"],
                        },
                    }
                ],
            }
        }
    )

    assert unnamed["status"] == "error"
    unnamed_diagnostics = unnamed["data"]["partialPlan"]["diagnostics"]
    assert any(
        item["code"] == "REQUIRES_RESPONSIBILITY_IDS"
        and "layoutAlgorithms" in item["details"]["missingEvidence"]
        and "responsiveTopologies" in item["details"]["missingEvidence"]
        for item in unnamed_diagnostics
    )
    assert dropped["status"] == "error"
    assert any(
        item["code"] == "RESPONSIBILITY_COVERAGE_MISSING"
        and item["details"]["category"] == "layoutAlgorithms"
        and item["details"]["responsibilityId"] == "overlap-layout"
        for item in dropped["data"]["partialPlan"]["diagnostics"]
    )


def test_input_event_and_mutation_state_ownership_must_stay_together() -> None:
    tree = _valid_inbox_tree()
    composer = tree["children"][1]
    composer["splitHints"][0]["responsibilities"]["controls"].remove("onDraftChange")
    composer["splitHints"][1]["responsibilities"]["controls"].append("onDraftChange")

    result = compile_ui_plan({"ui_tree": tree})

    assert result["status"] == "error"
    diagnostics = result["data"]["diagnostics"]
    assert any(item["code"] == "OWNERSHIP_BOUNDARY_SPLIT" for item in diagnostics)


def test_budget_overflow_without_semantic_split_is_not_executable() -> None:
    result = compile_ui_plan(
        {
            "ui_tree": {
                "id": "dense-board",
                "purpose": "Coordinate many work items in one operational page.",
                "importance": "primaryRegion",
                "responsibilities": {
                    "visualRoles": ["board", "toolbar", "wip", "details"],
                    "controls": [
                        "query",
                        "onQueryChange",
                        "filter",
                        "onFilter",
                        "select",
                        "onSelect",
                        "bulk-action",
                        "onBulkApply",
                    ],
                    "mutations": ["bulk-apply", "status-update"],
                    "states": ["empty", "loading", "loaded", "error", "saving"],
                },
                "inputs": ["query", "filter", "select"],
                "events": ["onQueryChange", "onFilter", "onSelect", "onBulkApply"],
            }
        }
    )

    assert result["status"] == "error"
    diagnostics = result["data"]["diagnostics"]
    assert diagnostics[0]["code"] == "REQUIRES_SEMANTIC_DECOMPOSITION"
    assert result["data"]["partialPlan"]["summary"]["contractCount"] == 0


def test_duplicate_and_unsafe_ids_are_rejected() -> None:
    duplicate = compile_ui_plan(
        {
            "ui_tree": {
                "id": "root",
                "children": [{"id": "toolbar"}, {"id": "toolbar"}],
            }
        }
    )
    unsafe = compile_ui_plan({"ui_tree": {"id": "a b"}})

    assert duplicate["status"] == "error"
    assert any(item["code"] == "INVALID_NODE_ID" for item in duplicate["data"]["diagnostics"])
    assert unsafe["status"] == "error"
    assert unsafe["error"]["code"] == "PLAN_NOT_EXECUTABLE"


def test_config_guardrails_and_resource_limits_are_fail_closed() -> None:
    too_small_budget = compile_ui_plan(
        {"ui_tree": {"id": "empty"}, "config": {"leafBudget": {"maxComplexity": 0.1}}}
    )
    guardrail_override = compile_ui_plan(
        {"ui_tree": {"id": "empty"}, "config": {"generation": {"rootMayWriteUi": True}}}
    )
    unknown_nested_policy = compile_ui_plan(
        {"ui_tree": {"id": "empty"}, "config": {"generation": {"unknown": True}}}
    )
    partial = compile_ui_plan({"ui_tree": {"id": "empty"}, "config": {"viewports": [390]}})

    assert too_small_budget["status"] == "error"
    assert guardrail_override["status"] == "error"
    assert unknown_nested_policy["status"] == "error"
    assert partial["status"] == "ok"
    generation = partial["data"]["plan"]["config"]["trustedPolicy"]["generation"]
    assert generation["rootMayWriteUi"] is False
    assert generation["regenerateInsteadOfPatch"] is True


def test_depth_and_unknown_schema_keys_are_rejected() -> None:
    tree = {"id": "node-0"}
    current = tree
    for index in range(1, 15):
        child = {"id": f"node-{index}"}
        current["children"] = [child]
        current = child

    deep = compile_ui_plan({"ui_tree": tree})
    unknown = compile_ui_plan({"ui_tree": {"id": "empty", "surprise": True}})

    assert deep["status"] == "error"
    assert unknown["status"] == "error"


def test_slots_ownership_and_metadata_are_resource_limited() -> None:
    too_many_slots = compile_ui_plan(
        {
            "ui_tree": {
                "id": "frame",
                "implementationMode": "component-with-slots",
                "slots": [
                    {
                        "id": f"slot-{index}",
                        "acceptsNodeId": f"child-{index}",
                    }
                    for index in range(9)
                ],
            }
        }
    )
    too_many_ownership_groups = compile_ui_plan(
        {
            "ui_tree": {
                "id": "editor",
                "ownership": [
                    {"id": f"group-{index}"}
                    for index in range(33)
                ],
            }
        }
    )
    huge_metadata = compile_ui_plan(
        {
            "ui_tree": {
                "id": "editor",
                "metadata": {"blob": "x" * 9000},
            }
        }
    )

    assert too_many_slots["status"] == "error"
    assert too_many_slots["error"]["code"] == "PLAN_NOT_EXECUTABLE"
    assert too_many_ownership_groups["status"] == "error"
    assert huge_metadata["status"] == "error"


def test_read_only_compile_rejects_persist_and_compile_tool_needs_no_approval() -> None:
    ToolRegistry._instance = None
    tool = ToolRegistry().get("tool_ui_compile_plan")
    persist = compile_ui_plan({"ui_tree": _valid_inbox_tree(), "persist": True})
    executed = ToolExecutor().execute(
        "tool_ui_compile_plan",
        {"ui_tree": _valid_inbox_tree(), "run_id": "tool-run"},
        _attached_plan_context(
            "tool_ui_compile_plan",
            profile_policy={"yolo_mode": True},
        ),
    )

    assert tool is not None
    assert tool["requires_approval"] is False
    assert tool["write_action"] is False
    assert tool["execution"]["handler"] == "domain.tool.ui_compiler_tools:ui_compile_plan"
    assert persist["status"] == "error"
    assert persist["error"]["code"] == "PERSIST_NOT_SUPPORTED_ON_COMPILE_ENDPOINT"
    assert executed["is_error"] is False
    assert executed["widget"]["type"] == "ui_compile_plan"


def test_commit_requires_internal_authorization_and_trusted_workspace(tmp_path: Path) -> None:
    raw_approved = ui_commit_plan(
        {"ui_tree": _valid_inbox_tree(), "run_id": "raw-approved"},
        {"_tool_server_approved": True, "conversation_workspace_dir": str(tmp_path)},
    )
    raw_yolo = ui_commit_plan(
        {"ui_tree": _valid_inbox_tree(), "run_id": "raw-yolo"},
        {"profile_policy": {"yolo_mode": True}, "conversation_workspace_dir": str(tmp_path)},
    )
    no_workspace = ui_commit_plan(
        {"ui_tree": _valid_inbox_tree(), "run_id": "no-workspace"},
        mark_tool_server_approval_context({}),
    )
    outside_root = ui_commit_plan(
        {"ui_tree": _valid_inbox_tree(), "run_id": "approved-run", "artifact_root": "/tmp/nope"},
        mark_tool_server_approval_context({"conversation_workspace_dir": str(tmp_path)}),
    )
    allowed = ui_commit_plan(
        {"ui_tree": _valid_inbox_tree(), "run_id": "approved-run"},
        mark_tool_server_approval_context({"conversation_workspace_dir": str(tmp_path)}),
    )
    executor_yolo = ToolExecutor().execute(
        "tool_ui_commit_plan",
        {"ui_tree": _valid_inbox_tree(), "run_id": "executor-yolo"},
        {
            "profile_policy": {"yolo_mode": True},
            "conversation_workspace_dir": str(tmp_path),
            "principal_id": "defaultspack",
            **_attached_plan_context("tool_ui_commit_plan"),
        },
    )

    assert raw_approved["status"] == "error"
    assert raw_yolo["status"] == "error"
    assert no_workspace["error"]["code"] == "WORKSPACE_REQUIRED"
    assert outside_root["status"] == "error"
    assert outside_root["error"]["code"] == "INVALID_REQUEST"
    assert allowed["status"] == "ok"
    assert allowed["data"]["artifacts"]["relativePath"] == ".rumi/ui/runs/approved-run"
    assert executor_yolo["is_error"] is False
    assert executor_yolo["widget"]["type"] == "ui_commit_plan"
    assert (tmp_path / ".rumi" / "ui" / "runs" / "approved-run" / "manifest.json").is_file()
    assert (tmp_path / ".rumi" / "ui" / "runs" / "executor-yolo" / "manifest.json").is_file()
    assert not Path("/tmp/nope/.rumi/ui/runs/approved-run").exists()


def test_service_validates_run_id_and_idempotency_key(tmp_path: Path) -> None:
    invalid_compile_run = compile_ui_plan(
        {"ui_tree": {"id": "empty"}, "run_id": "../bad"}
    )
    oversized_commit_key = ui_commit_plan(
        {
            "ui_tree": _valid_inbox_tree(),
            "run_id": "valid-run",
            "idempotency_key": "x" * 10000,
        },
        mark_tool_server_approval_context({"conversation_workspace_dir": str(tmp_path)}),
    )

    assert invalid_compile_run["status"] == "error"
    assert invalid_compile_run["error"]["code"] == "INVALID_UI_PLAN"
    assert oversized_commit_key["status"] == "error"
    assert oversized_commit_key["error"]["code"] == "INVALID_UI_PLAN"
    assert not (tmp_path / ".rumi" / "ui" / "runs" / "valid-run").exists()


def test_artifact_store_is_run_scoped_and_rejects_overwrites(tmp_path: Path) -> None:
    store = UICompilerArtifactStore(tmp_path / ".rumi" / "ui")
    plan_a = RecursiveUIPlanner().plan(_valid_inbox_tree(), run_id="run-a")
    plan_b = RecursiveUIPlanner().plan(_valid_inbox_tree(), run_id="run-b")

    artifacts_a = store.save_plan(plan_a)
    artifacts_b = store.save_plan(plan_b)

    assert artifacts_a["relativePath"] == ".rumi/ui/runs/run-a"
    assert artifacts_b["relativePath"] == ".rumi/ui/runs/run-b"
    assert (tmp_path / ".rumi" / "ui" / "runs" / "run-a" / "contracts" / "inbox-toolbar.json").is_file()
    assert (tmp_path / ".rumi" / "ui" / "runs" / "run-b" / "contracts" / "inbox-toolbar.json").is_file()
    with pytest.raises(FileExistsError):
        store.save_plan(plan_a)

    idempotent_first = store.save_plan(
        RecursiveUIPlanner().plan(_valid_inbox_tree(), run_id="run-c"),
        idempotency_key="same-request",
    )
    idempotent_second = store.save_plan(
        RecursiveUIPlanner().plan(_valid_inbox_tree(), run_id="run-c"),
        idempotency_key="same-request",
    )
    assert idempotent_second == idempotent_first


def test_invalid_plan_is_not_persisted(tmp_path: Path) -> None:
    invalid = RecursiveUIPlanner().plan(
        {
            "id": "dense-board",
            "responsibilities": {
                "controls": ["a", "b", "c", "d", "e", "f"],
                "states": ["loading", "error"],
            },
        },
        run_id="invalid-run",
    )

    assert not invalid.is_executable()
    with pytest.raises(ValueError):
        UICompilerArtifactStore(tmp_path / ".rumi" / "ui").save_plan(invalid)
    assert not (tmp_path / ".rumi" / "ui" / "runs" / "invalid-run").exists()


def test_failed_write_leaves_no_partial_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from domain.ui_compiler import artifact_store as artifact_store_module

    store = UICompilerArtifactStore(tmp_path / ".rumi" / "ui")
    plan = RecursiveUIPlanner().plan(_valid_inbox_tree(), run_id="failed-run")
    original = artifact_store_module._write_json

    def fail_on_report(path: Path, payload: dict) -> None:
        if path.name == "report.json":
            raise OSError("disk full")
        original(path, payload)

    monkeypatch.setattr(artifact_store_module, "_write_json", fail_on_report)

    with pytest.raises(OSError):
        store.save_plan(plan)
    assert not (tmp_path / ".rumi" / "ui" / "runs" / "failed-run").exists()


def test_concurrent_runs_do_not_mix_artifacts(tmp_path: Path) -> None:
    store = UICompilerArtifactStore(tmp_path / ".rumi" / "ui")

    def save(run_id: str) -> str:
        plan = RecursiveUIPlanner().plan(_valid_inbox_tree(), run_id=run_id)
        return store.save_plan(plan)["relativePath"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = sorted(pool.map(save, ["concurrent-a", "concurrent-b"]))

    assert paths == [".rumi/ui/runs/concurrent-a", ".rumi/ui/runs/concurrent-b"]
    for run_id in ("concurrent-a", "concurrent-b"):
        manifest = json.loads(
            (tmp_path / ".rumi" / "ui" / "runs" / run_id / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["runId"] == run_id
        assert manifest["status"] == "valid"
