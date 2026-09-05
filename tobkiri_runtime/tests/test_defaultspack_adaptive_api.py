from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.contract


def _approved_tool_context(**values):
    from domain.tool_policy.internal_context import mark_tool_server_approval_context

    return mark_tool_server_approval_context(dict(values))


def test_adaptive_dispatch_compile_apply_and_activity(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from domain.adaptive.service import AdaptiveRuntimeService, dispatch

    compiled = dispatch(
        "onboarding_compile",
        {"profile_id": "coding", "answers": {"profile_id": "coding", "preset_id": "maximum_local_autonomy"}},
        {},
    )
    assert compiled["status"] == "ok"
    plan = compiled["data"]["plan"]

    unapproved = dispatch("onboarding_apply", {"profile_id": "coding", "plan": plan}, {})
    assert unapproved["status"] == "error"
    assert unapproved["code"] == "APPROVAL_REQUIRED"

    approved_ctx = {"_tool_server_approved": True}
    applied = dispatch("onboarding_apply", {"profile_id": "coding", "plan": plan}, approved_ctx)
    assert applied["status"] == "ok"
    assert applied["data"]["applied"] is True

    frozen = dispatch("freeze_set", {"profile_id": "coding", "frozen": True, "reason": "test"}, {})
    assert frozen["status"] == "ok"
    snapshot = AdaptiveRuntimeService(profile_id="coding").activity_snapshot()
    assert snapshot["freeze"]["frozen"] is True
    assert snapshot["events"]
    blocked = dispatch("lease_acquire", {"profile_id": "coding", "resource": "src/App.tsx"}, {})
    assert blocked["status"] == "error"
    assert blocked["code"] == "ADAPTIVE_FROZEN"


def test_adaptive_apply_requires_plan_and_undo_restores_active_profile(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from core_runtime.operating_profile import OperatingProfilePlanStore
    from domain.adaptive.service import dispatch

    missing = dispatch(
        "onboarding_apply",
        {"profile_id": "coding", "answers": {"profile_id": "coding", "preset": "max_local_autonomy"}},
        {},
    )
    assert missing["status"] == "error"
    assert missing["code"] == "INVALID_INPUT"

    initial = dispatch(
        "onboarding_compile",
        {"profile_id": "coding", "answers": {"profile_id": "coding", "preset": "discussion_only"}},
        {},
    )
    assert initial["status"] == "ok"
    approved_ctx = {"_tool_server_approved": True}
    assert dispatch("onboarding_apply", {"profile_id": "coding", "plan": initial["data"]["plan"]}, approved_ctx)["status"] == "ok"

    target = dispatch(
        "onboarding_compile",
        {"profile_id": "coding", "answers": {"profile_id": "coding", "preset": "max_local_autonomy"}},
        {},
    )
    assert target["status"] == "ok"
    assert dispatch("onboarding_apply", {"profile_id": "coding", "plan": target["data"]["plan"]}, approved_ctx)["status"] == "ok"

    undone = dispatch("onboarding_undo", {"profile_id": "coding"}, {})
    assert undone["status"] == "ok"
    restored = OperatingProfilePlanStore().load_active_profile("coding")
    assert restored is not None
    assert restored.preset_id == "discussion_only"


def test_onboarding_undo_requires_approval_when_it_would_widen_policy(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from core_runtime.operating_profile import OperatingProfilePlanStore
    from domain.adaptive.service import dispatch

    approved_ctx = {"_tool_server_approved": True}

    discussion = dispatch(
        "onboarding_compile",
        {"profile_id": "coding", "answers": {"profile_id": "coding", "preset": "discussion_only"}},
        {},
    )
    assert discussion["status"] == "ok"
    assert dispatch("onboarding_apply", {"profile_id": "coding", "plan": discussion["data"]["plan"]}, approved_ctx)["status"] == "ok"

    max_local = dispatch(
        "onboarding_compile",
        {"profile_id": "coding", "answers": {"profile_id": "coding", "preset": "max_local_autonomy"}},
        {},
    )
    assert max_local["status"] == "ok"
    assert dispatch("onboarding_apply", {"profile_id": "coding", "plan": max_local["data"]["plan"]}, approved_ctx)["status"] == "ok"

    narrow = dispatch(
        "onboarding_compile",
        {"profile_id": "coding", "answers": {"profile_id": "coding", "preset": "discussion_only"}},
        {},
    )
    assert narrow["status"] == "ok"
    assert dispatch("onboarding_apply", {"profile_id": "coding", "plan": narrow["data"]["plan"]}, approved_ctx)["status"] == "ok"

    rejected = dispatch("onboarding_undo", {"profile_id": "coding"}, {})
    assert rejected["status"] == "error"
    assert rejected["code"] == "APPROVAL_REQUIRED"
    active = OperatingProfilePlanStore().load_active_profile("coding")
    assert active is not None
    assert active.preset_id == "discussion_only"

    undone = dispatch("onboarding_undo", {"profile_id": "coding"}, approved_ctx)
    assert undone["status"] == "ok"
    restored = OperatingProfilePlanStore().load_active_profile("coding")
    assert restored is not None
    assert restored.preset_id == "max_local_autonomy"


def test_operating_profile_preview_forwards_route_id_and_answers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from domain.adaptive.service import dispatch

    preview = dispatch(
        "operating_profiles_preview",
        {
            "id": "route-profile",
            "answers": {
                "profile_id": "body-profile",
                "preset": "balanced_local",
                "actions": {"local_write": "allow"},
            },
        },
        {},
    )

    assert preview["status"] == "ok"
    profile = preview["data"]["profile"]
    assert profile["profile_id"] == "route-profile"
    assert profile["answers"]["profile_id"] == "route-profile"
    assert profile["policy"]["local_write"] == "allow"


def test_operating_profile_activate_honors_route_id_without_body_profile(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from domain.adaptive.service import dispatch

    active = SimpleNamespace(
        activation={"activation_id": "activation:defaults-test"},
        resolved=SimpleNamespace(
            profile={"profile_id": "defaults"},
            plan={"plan_digest": "sha256:" + "1" * 64},
        ),
    )
    monkeypatch.setattr(
        "core_runtime.bootstrap.profile_capture.capture_active_profile",
        lambda: active,
    )

    result = dispatch("operating_profiles_activate", {"id": "route-profile"}, {})

    assert result["status"] == "error"
    assert result["code"] == "PROFILE_NOT_FOUND"


def test_adaptive_freeze_blocks_real_tool_and_public_function_dispatch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from domain.adaptive.service import dispatch
    from domain.function_runtime.dispatcher import run_defaultspack_function
    from domain.tool.executor import ToolExecutor

    frozen = dispatch("freeze_set", {"profile_id": "coding", "frozen": True, "reason": "incident"}, {})
    assert frozen["status"] == "ok"

    class Registry:
        def get(self, name):
            return {
                "tool_id": name,
                "name": name,
                "execution": {"type": "local"},
                "metadata": {"category": "external_message"},
            }

    def must_not_run(*args, **kwargs):  # pragma: no cover - proves the guard is preflight
        raise AssertionError("tool execution should be blocked before local execution")

    executor = ToolExecutor()
    executor._registry = Registry()
    monkeypatch.setattr(executor, "_execute_local", must_not_run)
    tool_result = executor.execute(
        "external_send",
        {"payload": {"text": "hello"}},
        {"profile_id": "coding", "_tool_server_approved": True, "profile_policy": {"yolo_mode": True}},
    )

    assert tool_result["is_error"] is True
    assert tool_result["adaptive_policy"]["code"] == "ADAPTIVE_FROZEN"

    browser_result = run_defaultspack_function(
        "browser_open_url",
        {"url": "https://example.invalid", "approved": True},
        {"profile_id": "coding", "_tool_server_approved": True},
    )
    assert browser_result["status"] == "ok"
    assert browser_result["data"]["adaptive_policy"]["code"] == "ADAPTIVE_FROZEN"

    terminal_result = run_defaultspack_function(
        "coding_terminal_exec",
        {"command": "echo should-not-run", "approved": True},
        {"profile_id": "coding", "_tool_server_approved": True},
    )
    assert terminal_result["status"] == "error"
    assert terminal_result["error"]["code"] == "ADAPTIVE_FROZEN"


def test_active_operating_profile_denies_tool_and_public_function_dispatch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    defaultspack_capability_plan_context,
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from core_runtime.operating_profile import OperatingProfilePlanStore, compile_operating_profile
    from domain.function_runtime.dispatcher import run_defaultspack_function
    from domain.tool.executor import ToolExecutor

    store = OperatingProfilePlanStore()
    profile = compile_operating_profile({"profile_id": "coding", "preset": "discussion_only"})
    store.apply_plan(store.create_plan("coding", profile, reason="test deny"))

    class Registry:
        def get(self, name):
            return {
                "tool_id": name,
                "name": name,
                "execution": {"type": "local"},
                "metadata": {"category": "shell"},
            }

    def must_not_run(*args, **kwargs):  # pragma: no cover - proves the guard is preflight
        raise AssertionError("tool execution should be blocked by active operating profile")

    executor = ToolExecutor()
    executor._registry = Registry()
    monkeypatch.setattr(executor, "_execute_local", must_not_run)
    capability_context = defaultspack_capability_plan_context(
        "coding_terminal_exec",
        _tool_definitions={"coding_terminal_exec": {"schema": {}}},
    )
    tool_result = executor.execute(
        "coding_terminal_exec",
        {"command": "echo denied"},
        {
            **capability_context,
            "profile_id": "coding",
            "_tool_server_approved": True,
        },
    )
    assert tool_result["is_error"] is True
    assert tool_result["adaptive_policy"]["code"] == "ADAPTIVE_PROFILE_DENIED"

    function_result = run_defaultspack_function(
        "coding_terminal_exec",
        {"command": "echo denied", "approved": True},
        {"profile_id": "coding", "_tool_server_approved": True},
    )
    assert function_result["status"] == "error"
    assert function_result["error"]["code"] == "ADAPTIVE_PROFILE_DENIED"


def test_adaptive_guard_splits_git_commit_push_and_merge_actions() -> None:
    from domain.adaptive.guard import action_for_function, action_for_tool

    assert action_for_function("coding_git_commit") == "git_commit"
    assert action_for_function("coding_git_push") == "git_push"
    assert action_for_function("coding_git_merge") == "git_merge"
    assert action_for_tool("git_commit", {}, {}) == "git_commit"
    assert action_for_tool("git_push", {}, {}) == "git_push"
    assert action_for_tool("git_merge", {}, {}) == "git_merge"


def test_adaptive_generated_functions_register_into_shared_registry() -> None:
    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
        assert_retired_module_absent,
    )
    from tests.v4_batch_support import assert_legacy_registry_fails_closed

    assert_retired_module_absent("core_runtime.function_registry")
    assert_retired_module_absent("domain.function_runtime.bridge")
    assert_legacy_registry_fails_closed()
    assert_profile_resolver_requires_authority_snapshot()


def test_adaptive_function_route_defaults_ignore_client_operation_override(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from domain.function_runtime.dispatcher import run_defaultspack_function

    result = run_defaultspack_function(
        "adaptive_onboarding_compile",
        {
            "operation": "activity_snapshot",
            "profile_id": "coding",
            "answers": {"profile_id": "coding", "preset_id": "maximum_local_autonomy"},
        },
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["compiled"] is True
    assert "activity" not in result["data"]


def test_adaptive_frontend_fixture_tracks_backend_route_contracts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from domain.adaptive.service import dispatch

    fixture_path = DEFAULTSPACK_ROOT / "webapp" / "src" / "adaptive" / "adaptiveBackend.fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    status_keys = {
        "profile_id",
        "configured",
        "current",
        "operating_profile",
        "last_history_entry",
        "freeze",
        "prepared_actions",
        "memory_conflicts",
        "events",
        "event_outbox",
        "event_subscriptions",
    }
    activity_keys = {
        "profile_id",
        "created_at",
        "onboarding_configured",
        "freeze",
        "prepared_actions",
        "events",
        "event_delivery_summary",
        "event_outbox",
        "event_subscriptions",
        "memory_conflicts",
        "memory_conflict_summary",
        "leases",
        "automations",
        "automation_templates",
        "automation_simulation",
    }

    assert status_keys <= set(fixture["onboarding_status"])
    assert activity_keys <= set(fixture["activity_center"])

    live_status = dispatch("onboarding_status", {"profile_id": "fixture-contract"}, {})
    assert live_status["status"] == "ok"
    assert status_keys <= set(live_status["data"])

    live_activity = dispatch("activity_snapshot", {"profile_id": "fixture-contract"}, {})
    assert live_activity["status"] == "ok"
    assert activity_keys <= set(live_activity["data"])


def test_context_file_read_search_and_evidence_are_bounded(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user_data"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH", str(tmp_path / "workspaces.json"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "app.py").write_text("alpha\nbeta target\ngamma target\n", encoding="utf-8")
    (workspace / "long.txt").write_text(
        "".join(f"prefix-{index:03d}-{'x' * 80}\n" for index in range(1, 40)),
        encoding="utf-8",
    )
    (workspace / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    from domain.coding.workspace_store import WorkspaceStore
    from domain.adaptive.service import dispatch

    WorkspaceStore().create(workspace, workspace_id="ws1", trusted=True)

    read = dispatch(
        "context_file_read",
        {"workspace_id": "ws1", "path": "app.py", "start_line": 2, "max_lines": 1},
        {},
    )
    assert read["status"] == "ok"
    assert read["data"]["line_count"] == 1
    assert read["data"]["lines"][0]["line"] == 2

    rear_read = dispatch(
        "context_file_read",
        {"workspace_id": "ws1", "path": "long.txt", "start_line": 20, "max_lines": 1, "max_bytes": 120},
        {},
    )
    assert rear_read["status"] == "ok"
    assert rear_read["data"]["line_count"] == 1
    assert rear_read["data"]["lines"][0]["line"] == 20
    assert rear_read["data"]["lines"][0]["text"].startswith("prefix-020-")

    search = dispatch("context_code_search", {"workspace_id": "ws1", "query": "target", "max_matches": 1}, {})
    assert search["status"] == "ok"
    assert search["data"]["count"] == 1
    assert search["data"]["truncated"] is True

    evidence = dispatch(
        "context_evidence",
        {"workspace_id": "ws1", "items": [{"path": "app.py", "start_line": 1, "max_lines": 2}]},
        {},
    )
    assert evidence["status"] == "ok"
    assert evidence["data"]["bundle_id"].startswith("ev_")

    untrusted_root = dispatch("context_file_read", {"root": str(tmp_path), "path": "repo/app.py"}, {})
    assert untrusted_root["status"] == "error"
    assert untrusted_root["code"] == "WORKSPACE_UNTRUSTED"

    absolute_path = dispatch("context_file_read", {"workspace_id": "ws1", "path": str(workspace / "app.py")}, {})
    assert absolute_path["status"] == "error"
    assert absolute_path["code"] == "PATH_OUTSIDE_WORKSPACE"

    secret_path = dispatch("context_file_read", {"workspace_id": "ws1", "path": ".env"}, {})
    assert secret_path["status"] == "error"
    assert secret_path["code"] == "PATH_RESTRICTED"

    def fail_rglob(self: Path, pattern: str):
        raise AssertionError(f"unbounded rglob must not be used: {self} {pattern}")

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    bounded_map = dispatch("context_repository_map", {"workspace_id": "ws1", "max_entries": 1}, {})
    assert bounded_map["status"] == "ok"
    assert bounded_map["data"]["truncated"] is True
    bounded_search = dispatch("context_code_search", {"workspace_id": "ws1", "query": "target", "max_matches": 1}, {})
    assert bounded_search["status"] == "ok"
    assert bounded_search["data"]["count"] == 1

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leaked.txt").write_text("outside-symlink-secret-needle\n", encoding="utf-8")
    try:
        (workspace / "linked-outside").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is not available: {exc}")
    symlink_map = dispatch("context_repository_map", {"workspace_id": "ws1", "max_entries": 20}, {})
    assert symlink_map["status"] == "ok"
    assert all("linked-outside" not in item["path"] for item in symlink_map["data"]["entries"])
    symlink_search = dispatch(
        "context_code_search",
        {"workspace_id": "ws1", "query": "outside-symlink-secret-needle", "max_matches": 5},
        {},
    )
    assert symlink_search["status"] == "ok"
    assert symlink_search["data"]["count"] == 0


def test_prepared_actions_redact_secret_and_lease_roundtrip(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from domain.adaptive.service import dispatch

    prepared = dispatch(
        "prepared_action_prepare",
        {"profile_id": "coding", "operation": "webhook.create", "arguments": {"shared_secret": "raw"}},
        {},
    )
    assert prepared["status"] == "ok"
    action = prepared["data"]["prepared_action"]
    assert action["display_args"]["shared_secret"] == "[REDACTED]"

    lease = dispatch("lease_acquire", {"profile_id": "coding", "resource": "src/App.tsx", "owner": "agent"}, {})
    assert lease["status"] == "ok"
    wrong_holder = dispatch(
        "lease_release",
        {"profile_id": "coding", "id": lease["data"]["lease"]["id"], "holder": "other-agent"},
        {},
    )
    assert wrong_holder["status"] == "error"
    assert wrong_holder["code"] == "LEASE_HELD"
    released = dispatch("lease_release", {"profile_id": "coding", "id": lease["data"]["lease"]["id"]}, {})
    assert released["status"] == "ok"
    assert released["data"]["lease"]["status"] == "released"


def test_adaptive_leases_gate_coding_file_and_worktree_mutations(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests._coding_contract_fixture import bind_verified_coding_contracts

    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user_data"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH", str(tmp_path / "workspaces.json"))

    from blocks.coding.file_write import run as file_write_run
    from blocks.coding.git_commit import run as git_commit_run
    from domain.adaptive.service import dispatch
    from domain.adaptive.storage import AdaptiveStore
    from domain.coding.workspace_store import WorkspaceStore

    workspace = tmp_path / "repo"
    workspace.mkdir()
    WorkspaceStore().create(workspace, workspace_id="ws1", trusted=True)
    bind_verified_coding_contracts(monkeypatch, workspace, workspace_id="ws1")

    lease = dispatch(
        "lease_acquire",
        {
            "profile_id": "coding",
            "workspace_id": "ws1",
            "resource": "src/App.tsx",
            "holder": "agent-a",
            "ttl_seconds": 60,
        },
        {},
    )
    assert lease["status"] == "ok"
    assert lease["data"]["lease"]["workspace_id"] == "ws1"

    blocked = file_write_run(
        {"profile_id": "coding", "workspace_id": "ws1", "path": "src/App.tsx", "content": "blocked"},
        _approved_tool_context(profile_id="coding", principal_id="agent-b"),
    )
    assert blocked["status"] == "error"
    assert blocked["error"]["code"] == "ADAPTIVE_LEASE_HELD"
    assert not (workspace / "src" / "App.tsx").exists()
    (workspace / "src").mkdir()
    (workspace / "src" / "App.tsx").write_text("", encoding="utf-8")

    allowed = file_write_run(
        {"profile_id": "coding", "workspace_id": "ws1", "path": "src/App.tsx", "content": "ok"},
        _approved_tool_context(profile_id="coding", principal_id="agent-a"),
    )
    assert allowed["status"] == "ok", allowed
    assert (workspace / "src" / "App.tsx").read_text(encoding="utf-8") == "ok"

    blocked_commit = git_commit_run(
        {"profile_id": "coding", "workspace_id": "ws1", "message": "try locked file", "paths": ["src/App.tsx"]},
        _approved_tool_context(profile_id="coding", principal_id="agent-b"),
    )
    assert blocked_commit["status"] == "error"
    assert blocked_commit["error"]["code"] == "ADAPTIVE_LEASE_HELD"

    store = AdaptiveStore("coding")

    def expire_src_app(state):
        leases = state.get("leases") if isinstance(state, dict) and isinstance(state.get("leases"), list) else []
        for item in leases:
            if item.get("key") == "src/App.tsx":
                item["expires_at"] = 1
        return {"version": 1, "leases": leases}

    store.update_json("orchestration/leases.json", {"version": 1, "leases": []}, expire_src_app)
    after_expiry = file_write_run(
        {"profile_id": "coding", "workspace_id": "ws1", "path": "src/App.tsx", "content": "agent b ok"},
        _approved_tool_context(profile_id="coding", principal_id="agent-b"),
    )
    assert after_expiry["status"] == "ok"

    worktree_lease = dispatch(
        "lease_acquire",
        {"profile_id": "coding", "workspace_id": "ws1", "resource": ".", "holder": "agent-a", "ttl_seconds": 60},
        {},
    )
    assert worktree_lease["status"] == "ok"
    blocked_by_worktree = file_write_run(
        {"profile_id": "coding", "workspace_id": "ws1", "path": "docs/notes.txt", "content": "blocked"},
        _approved_tool_context(profile_id="coding", principal_id="agent-b"),
    )
    assert blocked_by_worktree["status"] == "error"
    assert blocked_by_worktree["error"]["code"] == "ADAPTIVE_LEASE_HELD"
    assert not (workspace / "docs" / "notes.txt").exists()


def test_coding_mutation_requests_approval_before_resolving_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blocks.coding.file_write import run as file_write_run

    def unexpected_resolution(*_args, **_kwargs):
        raise AssertionError("workspace resolution must follow approval")

    monkeypatch.setattr(
        "blocks.coding.file_write.canonical_mutation_guard",
        unexpected_resolution,
    )
    result = file_write_run(
        {
            "workspace_id": "not-yet-resolved",
            "path": "src/App.tsx",
            "content": "pending approval",
        },
        {},
    )

    assert result["status"] == "ok"
    assert result["data"]["approval_required"] is True


def test_adaptive_pack_skill_automation_and_event_state_are_not_placeholders(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from domain.adaptive.service import dispatch
    from domain.adaptive.storage import AdaptiveStore

    recommendations = dispatch(
        "pack_recommendations_list",
        {
            "profile_id": "coding",
            "answers": {
                "profile_id": "coding",
                "use_cases": {"coding": True, "automation": True},
                "actions": {"terminal": "ask", "browser_control": "ask"},
            },
        },
        {},
    )
    assert recommendations["status"] == "ok"
    assert "degraded" not in recommendations["data"]
    # Legacy Setup Pack recommendation authority is retired. Pack discovery and
    # selection now come from the finite v4 Host Pack Control catalog, so this
    # compatibility endpoint must not synthesize recommendations from mutable
    # setup state or legacy ecosystem manifests.
    assert recommendations["data"]["recommendations"] == []
    assert recommendations["data"]["pack_recommendations"] == []
    assert recommendations["data"]["count"] == 0
    assert recommendations["data"]["local_only"] is True

    prepared = dispatch(
        "prepared_action_prepare",
        {
            "profile_id": "coding",
            "action_type": "automation.update",
            "arguments": {"automationId": "automation_daily_context", "patch": {"enabled": True}},
        },
        {},
    )
    assert prepared["status"] == "ok"
    action_id = prepared["data"]["prepared_action"]["action_id"]
    committed = dispatch("prepared_action_commit", {"profile_id": "coding", "action_id": action_id}, {})
    assert committed["status"] == "ok"
    assert committed["data"]["executed"] is True
    assert committed["data"]["execution_result"]["enabled"] is True
    duplicate_commit = dispatch("prepared_action_commit", {"profile_id": "coding", "action_id": action_id}, {})
    assert duplicate_commit["status"] == "ok"
    assert duplicate_commit["data"]["duplicate"] is True
    assert duplicate_commit["data"]["execution_result"]["enabled"] is True
    activity = dispatch("activity_snapshot", {"profile_id": "coding"}, {})
    assert activity["status"] == "ok"
    automation = next(item for item in activity["data"]["automations"] if item["id"] == "automation_daily_context")
    assert automation["enabled"] is True

    from domain.function_runtime.dispatcher import run_defaultspack_function

    updated = run_defaultspack_function(
        "adaptive_automation_update",
        {
            "profile_id": "coding",
            "automation_id": "automation_daily_context",
            "patch": {"enabled": False},
        },
        {},
    )
    assert updated["status"] == "ok"
    assert updated["data"]["automation"]["enabled"] is False

    missing_evidence = dispatch(
        "skill_candidate_promote",
        {
            "profile_id": "coding",
            "candidate_id": "bad_candidate",
            "candidate": {"candidate_id": "bad_candidate", "title": "Missing evidence"},
        },
        {},
    )
    assert missing_evidence["status"] == "error"
    assert missing_evidence["code"] == "INVALID_INPUT"

    failure_event = dispatch(
        "event_append",
        {
            "profile_id": "coding",
            "event_type": "adaptive.skill.failure",
            "payload": {"status": "failed", "case": "install failed"},
        },
        {},
    )
    success_event = dispatch(
        "event_append",
        {
            "profile_id": "coding",
            "event_type": "adaptive.skill.verified_success",
            "payload": {"status": "success", "verified": True, "replay_verified": True},
        },
        {},
    )
    replay_event = dispatch(
        "event_append",
        {
            "profile_id": "coding",
            "event_type": "adaptive.skill.replay_verified",
            "payload": {"status": "verified", "verified": True},
        },
        {},
    )
    assert failure_event["status"] == success_event["status"] == replay_event["status"] == "ok"

    store = AdaptiveStore("coding")
    store.write_json(
        "skills/candidates.json",
        {
            "version": 1,
            "candidates": [
                {
                    "candidate_id": "cand_success_pair",
                    "title": "Retry stable install after cache repair",
                    "evidence": {
                        "failure_event_id": failure_event["data"]["event"]["event_id"],
                        "success_event_id": success_event["data"]["event"]["event_id"],
                        "replay_event_id": replay_event["data"]["event"]["event_id"],
                    },
                }
            ],
        },
    )
    promoted = dispatch("skill_candidate_promote", {"profile_id": "coding", "candidate_id": "cand_success_pair"}, {})
    assert promoted["status"] == "ok"
    assert promoted["data"]["promoted"] is True
    assert promoted["data"]["skill"]["status"] == "canary"
    assert promoted["data"]["skill"]["canary_state"] == "pending"
    rolled_back = dispatch("skill_candidate_rollback", {"profile_id": "coding", "candidate_id": "cand_success_pair"}, {})
    assert rolled_back["status"] == "ok"
    assert rolled_back["data"]["rolled_back"] is True
    assert rolled_back["data"]["skill"]["status"] == "rolled_back"

    event = dispatch(
        "event_append",
        {"profile_id": "coding", "event_type": "adaptive.test", "idempotency_key": "idem-1", "payload": {"ok": True}},
        {},
    )
    duplicate = dispatch(
        "event_append",
        {"profile_id": "coding", "event_type": "adaptive.test", "idempotency_key": "idem-1", "payload": {"ok": True}},
        {},
    )
    assert event["status"] == "ok"
    assert duplicate["status"] == "ok"
    assert duplicate["data"]["duplicate"] is True
    assert duplicate["data"]["event"]["event_id"] == event["data"]["event"]["event_id"]


def test_adaptive_event_append_idempotency_is_atomic(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from concurrent.futures import ThreadPoolExecutor

    from domain.adaptive.service import dispatch
    from domain.adaptive.storage import AdaptiveStore

    payload = {"profile_id": "coding", "event_type": "adaptive.concurrent", "idempotency_key": "same-key"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: dispatch("event_append", payload, {}), range(8)))

    assert all(result["status"] == "ok" for result in results), results
    event_ids = {result["data"]["event"]["event_id"] for result in results}
    assert len(event_ids) == 1
    rows = [
        row
        for row in AdaptiveStore("coding").read_jsonl("events/events.jsonl")
        if row.get("idempotency_key") == "same-key"
    ]
    assert len(rows) == 1
    replay = dispatch("event_replay", {"profile_id": "coding", "limit": 10}, {})
    assert replay["status"] == "ok"
    assert replay["data"]["next_cursor"]


def test_adaptive_event_delivery_outbox_subscription_and_continuation_lifecycle(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from domain.adaptive.service import dispatch
    from domain.function_runtime.dispatcher import run_defaultspack_function

    subscription = dispatch(
        "event_subscribe",
        {"profile_id": "coding", "subscriber_id": "worker-a", "event_type": "adaptive.test"},
        {},
    )
    assert subscription["status"] == "ok"
    assert subscription["data"]["subscription"]["status"] == "active"
    subscriptions = dispatch("event_subscription_list", {"profile_id": "coding", "subscriber_id": "worker-a"}, {})
    assert subscriptions["status"] == "ok"
    assert subscriptions["data"]["subscriptions"][0]["subscriber_id"] == "worker-a"

    event = dispatch(
        "event_append",
        {"profile_id": "coding", "event_type": "adaptive.test", "payload": {"ok": True}},
        {},
    )
    assert event["status"] == "ok"
    event_id = event["data"]["event"]["event_id"]
    denied_ack = run_defaultspack_function(
        "adaptive_event_ack",
        {"profile_id": "coding", "event_id": event_id, "subscriber_id": "worker-b"},
        {},
    )
    assert denied_ack["status"] == "error"
    assert denied_ack["code"] == "SUBSCRIPTION_REQUIRED"
    acked = run_defaultspack_function(
        "adaptive_event_ack",
        {"profile_id": "coding", "event_id": event_id, "subscriber_id": "worker-a"},
        {},
    )
    assert acked["status"] == "ok"
    listed = dispatch("event_list", {"profile_id": "coding", "event_type": "adaptive.test"}, {})
    delivered = next(item for item in listed["data"]["events"] if item["event_id"] == event_id)
    assert delivered["ack_state"] == "acked"
    assert delivered["delivery_status"] == "delivered"

    retry = dispatch("event_retry", {"profile_id": "coding", "event_id": event_id}, {})
    assert retry["status"] == "ok"
    assert retry["data"]["event"]["delivery_status"] == "retry_pending"
    dead_letter = dispatch("event_dlq", {"profile_id": "coding", "event_id": event_id, "reason": "test failure"}, {})
    assert dead_letter["status"] == "ok"
    assert dead_letter["data"]["event"]["delivery_status"] == "dead_letter"

    prepared = dispatch(
        "prepared_action_prepare",
        {"profile_id": "coding", "action_type": "external.audit", "arguments": {"target": "local"}},
        {},
    )
    assert prepared["status"] == "ok"
    committed = dispatch(
        "prepared_action_commit",
        {"profile_id": "coding", "action_id": prepared["data"]["prepared_action"]["action_id"]},
        {},
    )
    assert committed["status"] == "ok"
    assert committed["data"]["execution_status"] == "queued"
    outbox_id = committed["data"]["outbox_id"]
    duplicate_commit = dispatch(
        "prepared_action_commit",
        {"profile_id": "coding", "action_id": prepared["data"]["prepared_action"]["action_id"]},
        {},
    )
    assert duplicate_commit["status"] == "ok"
    assert duplicate_commit["data"]["duplicate"] is True
    assert duplicate_commit["data"]["outbox_id"] == outbox_id
    outbox = dispatch("event_outbox", {"profile_id": "coding"}, {})
    assert outbox["status"] == "ok"
    assert sum(1 for item in outbox["data"]["outbox"] if item["outbox_id"] == outbox_id and item["status"] == "pending") == 1

    replay = dispatch("event_replay", {"profile_id": "coding", "limit": 20}, {})
    continuation_event = next(
        item
        for item in replay["data"]["events"]
        if isinstance(item.get("continuation"), dict) and item["continuation"].get("outbox_id") == outbox_id
    )
    raw_resume = dispatch("continuation_resume", {"profile_id": "coding", "outbox_id": outbox_id}, {})
    assert raw_resume["status"] == "error"
    assert raw_resume["code"] == "INVALID_INPUT"

    mismatched_resume = dispatch(
        "continuation_resume",
        {"profile_id": "coding", "event_id": continuation_event["event_id"], "outbox_id": "other-outbox"},
        {},
    )
    assert mismatched_resume["status"] == "error"
    assert mismatched_resume["code"] == "INVALID_INPUT"

    resumed = dispatch("continuation_resume", {"profile_id": "coding", "event_id": continuation_event["event_id"]}, {})
    assert resumed["status"] == "ok"
    assert resumed["data"]["resume_mode"] == "state_only"
    assert resumed["data"]["outbox_item"]["status"] == "completed"
    duplicate = dispatch("continuation_resume", {"profile_id": "coding", "event_id": continuation_event["event_id"]}, {})
    assert duplicate["status"] == "ok"
    assert duplicate["data"]["duplicate"] is True
    assert duplicate["data"]["resume_mode"] == "state_only"
    activity = dispatch("activity_snapshot", {"profile_id": "coding"}, {})
    assert activity["status"] == "ok"
    assert activity["data"]["event_delivery_summary"]["resumed"] == 1


def test_adaptive_public_route_uses_function_id_operation_not_client_operation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    from domain.function_runtime.dispatcher import run_defaultspack_function

    prepared = run_defaultspack_function(
        "adaptive_prepared_action_prepare",
        {
            "operation": "automation.update",
            "arguments": {"automationId": "automation_daily_context", "patch": {"enabled": True}},
        },
        {},
    )
    assert prepared["status"] == "ok"
    assert prepared["data"]["prepared_action"]["operation"] == "automation.update"
