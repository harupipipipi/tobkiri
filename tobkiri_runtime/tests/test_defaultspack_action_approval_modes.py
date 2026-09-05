from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_agent_reviewer_uses_isolated_history_and_redacts_secrets(
    monkeypatch,
    tmp_path,
) -> None:
    from domain.tool.approval_reviewer import review_tool_action

    def fake_complete(input_data, context):
        assert input_data["tools"] == []
        assert context["approval_reviewer"] is True
        return {
            "status": "ok",
            "data": {
                "content": json.dumps(
                    {"decision": "approve", "reason": "Scoped update."}
                )
            },
        }

    monkeypatch.setattr("blocks.ai.complete.run", fake_complete)
    parent_history = tmp_path / "conversation" / "history.json"
    result = review_tool_action(
        "file.update",
        {
            "tool_id": "file.update",
            "trusted": True,
            "risk": "medium",
            "requires_approval": True,
            "effects": [{"class": "update"}],
        },
        {"path": "README.md", "api_key": "must-not-leak"},
        {
            "conversation_id": "parent",
            "history_json_path": str(parent_history),
            "model": "stub/reviewer",
            "profile_policy": {"action_approval_mode": "agent"},
        },
    )

    reviewer_history = (
        parent_history.parent / "approval-reviewer" / "history.json"
    )
    assert result["decision"] == "approve"
    assert result["history_json_path"] == str(reviewer_history)
    assert reviewer_history != parent_history
    raw_history = reviewer_history.read_text(encoding="utf-8")
    assert "must-not-leak" not in raw_history
    assert "[redacted]" in raw_history


def test_agent_reviewer_escalates_destructive_action_without_model_call(
    monkeypatch,
    tmp_path,
) -> None:
    from domain.tool.approval_reviewer import review_tool_action

    def unexpected_call(*args, **kwargs):
        raise AssertionError("destructive action must hit the hard minimum")

    monkeypatch.setattr("blocks.ai.complete.run", unexpected_call)
    result = review_tool_action(
        "file.delete",
        {
            "tool_id": "file.delete",
            "trusted": True,
            "risk": "high",
            "effects": [{"class": "delete"}],
        },
        {"path": "important.txt"},
        {
            "conversation_id": "parent",
            "history_json_path": str(tmp_path / "history.json"),
            "model": "stub/reviewer",
            "profile_policy": {"action_approval_mode": "agent"},
        },
    )

    assert result["decision"] == "escalate"
    assert result["source"] == "hard_minimum"


def test_local_ui_full_access_policy_is_server_canonicalized() -> None:
    from domain.chat.run_request import _sanitize_untrusted_chat_tool_policy

    policy, ignored = _sanitize_untrusted_chat_tool_policy(
        {
            "action_approval_mode": "full",
            "full_access": True,
            "yolo_mode": True,
        },
        trusted_local_ui=True,
    )

    assert ignored == []
    assert policy["action_approval_mode"] == "full"
    assert policy["full_access"] is True
    assert policy["allow_file_write"] is True
    assert policy["allow_network"] is True
    assert policy["allow_shell"] is True
    assert policy["write_actions_require_approval"] is False
    assert policy["yolo_mode"] is True


def test_remote_full_access_policy_remains_untrusted() -> None:
    from domain.chat.run_request import _sanitize_untrusted_chat_tool_policy

    policy, ignored = _sanitize_untrusted_chat_tool_policy(
        {
            "action_approval_mode": "full",
            "full_access": True,
            "yolo_mode": True,
        }
    )

    assert policy == {}
    assert set(ignored) == {
        "action_approval_mode",
        "full_access",
        "yolo_mode",
    }


def test_agent_mode_strips_legacy_yolo_bypass() -> None:
    from domain.chat.run_request import _sanitize_untrusted_chat_tool_policy

    policy, _ = _sanitize_untrusted_chat_tool_policy(
        {
            "action_approval_mode": "agent",
            "full_access": True,
            "yolo_mode": True,
        },
        trusted_local_ui=True,
    )

    assert policy == {"action_approval_mode": "agent"}


def test_full_access_auto_approves_trusted_ordinary_write() -> None:
    from domain.capability.policy import EffectPolicyEngine

    decision = EffectPolicyEngine().resolve(
        {
            "tool_id": "file.update",
            "trusted": True,
            "risk": "medium",
            "effects": [{"class": "update"}],
        },
        {"capabilities": {"approval": {"actions": {"update": "confirm"}}}},
        full_access=True,
    )[0]

    assert decision.mode == "auto"
    assert decision.hard_minimum == "auto"
    assert decision.source == "full_access"


def test_workspace_fallback_requires_authenticated_local_full_access(
    tmp_path,
) -> None:
    from domain.tool.executor import _trusted_full_access_workspace_binding

    base = {
        "workspace_root": str(tmp_path),
        "profile_policy": {
            "action_approval_mode": "full",
            "full_access": True,
        },
    }
    assert (
        _trusted_full_access_workspace_binding(base, "workspace-1") is None
    )

    binding = _trusted_full_access_workspace_binding(
        {
            **base,
            "_defaultspack_local_ui_authenticated": True,
        },
        "workspace-1",
    )

    assert binding is not None
    assert binding["workspace_id"] == "workspace-1"
    assert binding["canonical_root"] == str(tmp_path.resolve())
    assert binding["access"] == "read_only"
    assert binding["binding_source"] == "authenticated_local_ui_fallback"
