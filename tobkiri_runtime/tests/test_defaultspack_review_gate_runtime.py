from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PACK_ROOT = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
sys.path.insert(0, str(PACK_ROOT))

from core_runtime.operating_profile import (  # noqa: E402
    AgentExecutionMode,
    AuthorityReviewResult,
    FinalizationAction,
    ReviewGateRequest,
    ReviewVerdict,
    OperatingProfilePlanStore,
    compile_operating_profile,
)
from core_runtime.profile_workspace import ProfileWorkspaceManager  # noqa: E402
from domain.agent.review_gate_runtime import enforce_finalization_review  # noqa: E402
from domain.tool_policy.internal_context import (  # noqa: E402
    mark_tool_server_approval_context,
    mark_trusted_review_gate_context,
)


class _PlanStore:
    def __init__(self, profile: Any) -> None:
        self.profile = profile

    def load_active_profile(self, profile_id: str) -> Any:
        assert profile_id == "coding_agent"
        return self.profile


class _RunStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def add_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.events.append((run_id, event_type, payload))


class _Authority:
    def __init__(self, *, approved: bool) -> None:
        self.approved = approved
        self.requests: list[ReviewGateRequest] = []

    def consume_review(
        self,
        request: ReviewGateRequest,
    ) -> AuthorityReviewResult | None:
        self.requests.append(request)
        if not self.approved:
            return None
        return AuthorityReviewResult(
            authority_record_id="authority-record-1",
            binding_digest=request.binding_digest,
            review_id="review-1",
            reviewer_profile="reviewer_agent",
            reviewer_principal_id="profile:reviewer_agent",
            reviewer_run_id="reviewer-run-1",
            reviewer_model="local/reviewer",
            verdict=ReviewVerdict.APPROVED,
            reviewed_artifacts=(request.context.artifact_digest,),
        )


def _profile():
    return compile_operating_profile(
        {
            "profile_id": "coding_agent",
            "preset": "balanced_local",
            "review_topology": {
                "mode": "blocking",
                "reviewer_profile": "reviewer_agent",
                "require_separate_run": True,
                "applies_to": ["commit", "push", "delivery"],
                "store_findings": True,
            },
        }
    )


def _context(mode: AgentExecutionMode) -> dict[str, Any]:
    context = {
        "conversation_id": "conversation-1",
        "run_id": "actor-run-1",
        "profile_id": "coding_agent",
        "principal_id": "profile:coding_agent",
    }
    return mark_trusted_review_gate_context(
        context,
        execution_mode=mode.value,
        mode_context_id=f"{mode.value}:context-1",
    )


@pytest.mark.parametrize("mode", list(AgentExecutionMode))
def test_runtime_blocks_each_agent_mode_and_persists_retry_request(mode):
    authority = _Authority(approved=False)
    run_store = _RunStore()

    enforcement = enforce_finalization_review(
        FinalizationAction.COMMIT,
        {"expected_head": "abc123", "message": "ship"},
        _context(mode),
        plan_store=_PlanStore(_profile()),
        authority=authority,
        run_store=run_store,
    )

    assert enforcement is not None
    assert enforcement.blocked is True
    assert enforcement.decision.reason == "authority_review_missing"
    assert authority.requests[0].context.execution_mode is mode
    assert authority.requests[0].context.artifact_revision == "abc123"
    assert run_store.events[0][0:2] == (
        "actor-run-1",
        "review_gate_resolved",
    )
    assert (
        run_store.events[0][2]["request"]["binding_digest"]
        == authority.requests[0].binding_digest
    )


def test_runtime_consumes_approved_review_and_untrusted_callers_cannot_forge_mode():
    authority = _Authority(approved=True)
    plan_store = _PlanStore(_profile())
    artifact = {"provider": "slack", "text_sha256": "a" * 64}

    untrusted = enforce_finalization_review(
        FinalizationAction.DELIVERY,
        artifact,
        {
            "_review_gate_execution_mode": "fusion_agent",
            "_review_gate_mode_context_id": "forged",
            "conversation_id": "conversation-1",
            "run_id": "actor-run-1",
            "profile_id": "coding_agent",
        },
        plan_store=plan_store,
        authority=authority,
    )
    approved = enforce_finalization_review(
        FinalizationAction.DELIVERY,
        artifact,
        _context(AgentExecutionMode.FUSION_AGENT),
        plan_store=plan_store,
        authority=authority,
        run_store=_RunStore(),
    )

    assert untrusted is None
    assert approved is not None
    assert approved.blocked is False
    assert approved.decision.review_satisfied is True
    assert approved.decision.reason == "review_approved"


def test_runtime_reloads_activated_review_settings_after_restart(tmp_path):
    manager = ProfileWorkspaceManager(tmp_path)
    first_store = OperatingProfilePlanStore(manager)
    profile = _profile()
    plan = first_store.create_plan(profile.profile_id, profile)
    first_store.apply_plan(plan)

    restarted_store = OperatingProfilePlanStore(ProfileWorkspaceManager(tmp_path))
    enforcement = enforce_finalization_review(
        FinalizationAction.COMMIT,
        {"expected_head": "restart-head"},
        _context(AgentExecutionMode.MODE_AGENT),
        plan_store=restarted_store,
        authority=_Authority(approved=False),
        run_store=_RunStore(),
    )

    assert enforcement is not None
    assert enforcement.blocked is True
    assert enforcement.decision.request.policy.reviewer_profile == "reviewer_agent"


def test_runtime_does_not_resolve_artifact_without_an_active_profile():
    resolved: list[bool] = []

    enforcement = enforce_finalization_review(
        FinalizationAction.COMMIT,
        lambda: resolved.append(True) or {"expected_head": "unexpected"},
        _context(AgentExecutionMode.MODE_AGENT),
        plan_store=_PlanStore(None),
    )

    assert enforcement is None
    assert resolved == []


@pytest.mark.parametrize(
    ("action", "snapshot_name"),
    [
        (FinalizationAction.COMMIT, "git_snapshot"),
        (FinalizationAction.PUSH, "git_publish_snapshot"),
    ],
)
def test_git_review_artifact_uses_server_snapshot_and_forwards_it_to_effect(
    monkeypatch,
    action,
    snapshot_name,
):
    from domain.coding import contract_adapter
    from domain.tool.executor import _bind_finalization_artifact

    snapshot = {
        "expected_head": "server-head",
        "expected_tree": "server-tree",
        "expected_status_hash": "server-status",
        "expected_mount_revision": 9,
    }
    if action is FinalizationAction.PUSH:
        snapshot.update(
            {
                "remote": "origin",
                "branch": "main",
                "expected_remote_url_hash": "server-remote",
            }
        )
    monkeypatch.setattr(
        contract_adapter,
        snapshot_name,
        lambda *_args, **_kwargs: dict(snapshot),
    )
    arguments = {
        "workspace_id": "workspace-1",
        "expected_head": "caller-spoof",
    }

    artifact = _bind_finalization_artifact(action, arguments)

    assert artifact["expected_head"] == "server-head"
    assert arguments["expected_head"] == "server-head"
    if action is FinalizationAction.PUSH:
        assert artifact["expected_remote_url_hash"] == "server-remote"


def test_external_delivery_stops_before_provider_when_review_is_missing(
    monkeypatch,
):
    from domain.agent import review_gate_runtime
    from domain.external import send_tool
    from domain.tool.executor import ToolExecutor

    authority = _Authority(approved=False)
    enforcement = enforce_finalization_review(
        FinalizationAction.DELIVERY,
        {"provider": "slack", "text": "ship"},
        _context(AgentExecutionMode.TEAM_AGENT),
        plan_store=_PlanStore(_profile()),
        authority=authority,
        run_store=_RunStore(),
    )
    assert enforcement is not None

    monkeypatch.setattr(
        review_gate_runtime,
        "enforce_finalization_review",
        lambda *_args, **_kwargs: enforcement,
    )

    class _MustNotSend:
        def send_channel_message(self, *_args, **_kwargs):
            raise AssertionError("provider effect ran before the review gate")

    monkeypatch.setattr(send_tool, "SlackResponseAdapter", _MustNotSend)
    context = mark_tool_server_approval_context(
        _context(AgentExecutionMode.TEAM_AGENT)
    )
    result = ToolExecutor()._execute_handler(
        {
            "tool_id": "external_send",
            "name": "external_send",
            "requires_approval": True,
            "execution": {
                "type": "local",
                "handler": "domain.external.send_tool:external_send_tool",
            },
        },
        {"provider": "slack", "channel_id": "C1", "text": "ship"},
        context,
    )

    assert result["is_error"] is True
    assert result["error_type"] == "review_required"
    assert result["review_gate"]["request"]["context"]["action"] == "delivery"


def test_git_capability_stops_after_approval_consumption_and_before_effect(
    monkeypatch,
):
    from domain.agent import review_gate_runtime
    from domain.tool.executor import ToolExecutor

    authority = _Authority(approved=False)
    enforcement = enforce_finalization_review(
        FinalizationAction.PUSH,
        {"branch": "main", "expected_remote_url_hash": "b" * 64},
        _context(AgentExecutionMode.MODE_AGENT),
        plan_store=_PlanStore(_profile()),
        authority=authority,
        run_store=_RunStore(),
    )
    assert enforcement is not None
    monkeypatch.setattr(
        review_gate_runtime,
        "enforce_finalization_review",
        lambda *_args, **_kwargs: enforcement,
    )

    class _MustNotExecute:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("capability effect ran before the review gate")

    executor = ToolExecutor()
    monkeypatch.setattr(executor, "_capability_executor", lambda _context: _MustNotExecute())
    monkeypatch.setattr(
        executor,
        "_prepare_deferred_tool_approval",
        lambda *_args, **_kwargs: None,
    )
    consumed: list[bool] = []
    monkeypatch.setattr(
        executor,
        "_consume_deferred_tool_approval",
        lambda _context: consumed.append(True) and None,
    )
    result = executor._execute_capability_request(
        {
            "tool_id": "coding_git_push",
            "name": "coding_git_push",
            "execution": {"type": "rumi_function"},
        },
        {
            "type": "function.call",
            "qualified_name": "defaultspack:coding_git_push",
            "args": {"branch": "main"},
        },
        _context(AgentExecutionMode.MODE_AGENT),
    )

    assert consumed == [True]
    assert result["is_error"] is True
    assert result["error_type"] == "review_required"
    assert result["review_gate"]["request"]["context"]["action"] == "push"
