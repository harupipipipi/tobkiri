"""Regression coverage for durable, non-auto-executing deferred steers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from ecosystem.rumi_agent_state_store_pack.runtime.store import (  # noqa: E402
    AgentStateConflict,
    AgentStateStore,
    _arguments,
)


def _registration(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "expected_revision": 0,
        "deferred_steer_id": "steer-1",
        "idempotency_key": "register-request-1",
        "title": "Investigate persistence",
        "instruction": "After this execution, investigate the ChatStore failure.",
        "reason": "The current run exposed a separate restart regression.",
        "scope_type": "conversation",
        "scope_id": "conversation-1",
        "checkpoint": "after_execution",
        "source": "ai",
        "source_id": "run-1",
        "actor_id": "agent-1",
        "related_references": [{"kind": "run", "id": "run-1"}],
        "dedupe_key": "chat-store-restart",
    }
    payload.update(overrides)
    return _arguments("deferred.register", payload)


def test_deferred_steer_survives_restart_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    """Persist one steer and return the stable ID on a duplicate delivery."""

    store = AgentStateStore("default", root=tmp_path)
    created = store.apply("deferred.register", _registration())

    restarted = AgentStateStore("default", root=tmp_path)
    snapshot = restarted.list_deferred(scope_type="conversation", scope_id="conversation-1")
    duplicate = restarted.apply(
        "deferred.register",
        _registration(expected_revision=0, deferred_steer_id="different-id"),
    )

    assert created["deferred_steer"]["id"] == "steer-1"
    assert created["deferred_steer"]["status"] == "queued"
    assert created["deferred_steer"]["application_reference"] is None
    assert snapshot["deferred_steers"][0]["instruction"].startswith("After")
    assert duplicate["deduplicated"] is True
    assert duplicate["deferred_steer"]["id"] == "steer-1"
    assert duplicate["revision"] == 1


def test_deferred_steer_retry_rejects_changed_payload(tmp_path: Path) -> None:
    """Do not let an idempotency key silently authorize different content."""

    store = AgentStateStore("default", root=tmp_path)
    store.apply("deferred.register", _registration())

    with pytest.raises(AgentStateConflict, match="payload does not match"):
        store.apply(
            "deferred.register",
            _registration(instruction="A different hidden instruction"),
        )


def test_deferred_steer_checkpoint_edit_apply_and_complete(tmp_path: Path) -> None:
    """Keep queued work inert until a checkpoint or explicit normal-path apply."""

    store = AgentStateStore("default", root=tmp_path)
    created = store.apply("deferred.register", _registration())["deferred_steer"]
    ready = store.apply(
        "deferred.checkpoint",
        _arguments(
            "deferred.checkpoint",
            {
                "expected_revision": 1,
                "checkpoint": "after_execution",
                "scope_type": "conversation",
                "scope_id": "conversation-1",
            },
        ),
    )
    assert ready["ready_count"] == 1
    ready_record = ready["deferred_steers"][0]
    assert created["status"] == "queued"
    assert ready_record["status"] == "ready"

    updated = store.apply(
        "deferred.update",
        _arguments(
            "deferred.update",
            {
                "expected_revision": 2,
                "deferred_steer_id": "steer-1",
                "expected_steer_revision": ready_record["revision"],
                "updates": {"title": "Investigate durable persistence"},
            },
        ),
    )["deferred_steer"]
    applied = store.apply(
        "deferred.apply",
        _arguments(
            "deferred.apply",
            {
                "expected_revision": 3,
                "deferred_steer_id": "steer-1",
                "expected_steer_revision": updated["revision"],
                "application_reference": {
                    "kind": "conversation_instruction",
                    "conversation_id": "conversation-1",
                    "message_id": "message-1",
                },
            },
        ),
    )["deferred_steer"]
    completed = store.apply(
        "deferred.complete",
        _arguments(
            "deferred.complete",
            {
                "expected_revision": 4,
                "deferred_steer_id": "steer-1",
                "expected_steer_revision": applied["revision"],
            },
        ),
    )["deferred_steer"]

    assert updated["title"] == "Investigate durable persistence"
    assert applied["status"] == "applied"
    assert applied["application_reference"]["message_id"] == "message-1"
    assert completed["status"] == "completed"
    assert [event["name"] for event in completed["events"]] == [
        "deferred.queued",
        "deferred.ready",
        "deferred.updated",
        "deferred.applied",
        "deferred.completed",
    ]


def test_deferred_steer_validates_closed_values_and_typed_references(
    tmp_path: Path,
) -> None:
    """Reject unknown scopes and reference payloads that could smuggle context."""

    store = AgentStateStore("default", root=tmp_path)
    with pytest.raises(ValueError, match="scope is invalid"):
        store.apply("deferred.register", _registration(scope_type="global_everywhere"))
    with pytest.raises(ValueError, match="reference is invalid"):
        store.apply(
            "deferred.register",
            _registration(
                related_references=[
                    {
                        "kind": "tool_result",
                        "id": "tool-1",
                        "raw_secret": "must-not-be-stored",
                    }
                ]
            ),
        )


def test_deferred_steer_stale_record_revision_fails_closed(tmp_path: Path) -> None:
    """Reject UI updates based on an obsolete steer snapshot."""

    store = AgentStateStore("default", root=tmp_path)
    store.apply("deferred.register", _registration())
    with pytest.raises(AgentStateConflict, match="steer revision is stale"):
        store.apply(
            "deferred.dismiss",
            _arguments(
                "deferred.dismiss",
                {
                    "expected_revision": 1,
                    "deferred_steer_id": "steer-1",
                    "expected_steer_revision": 0,
                    "reason": "obsolete action",
                },
            ),
        )


def test_deferred_steer_dismiss_wins_application_race(tmp_path: Path) -> None:
    """Do not resurrect a steer when a stale apply races with cancellation."""

    store = AgentStateStore("default", root=tmp_path)
    created = store.apply("deferred.register", _registration())["deferred_steer"]
    dismissed = store.apply(
        "deferred.dismiss",
        _arguments(
            "deferred.dismiss",
            {
                "expected_revision": 1,
                "deferred_steer_id": "steer-1",
                "expected_steer_revision": created["revision"],
                "reason": "user cancelled before application",
            },
        ),
    )["deferred_steer"]

    with pytest.raises(AgentStateConflict, match="steer revision is stale"):
        store.apply(
            "deferred.apply",
            _arguments(
                "deferred.apply",
                {
                    "expected_revision": 2,
                    "deferred_steer_id": "steer-1",
                    "expected_steer_revision": created["revision"],
                    "application_reference": {
                        "kind": "conversation_instruction",
                        "conversation_id": "conversation-1",
                        "message_id": "message-too-late",
                    },
                },
            ),
        )
    assert dismissed["status"] == "dismissed"


def test_deferred_steer_capacity_is_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reject registration when the profile-owned active queue reaches its cap."""

    from ecosystem.rumi_agent_state_store_pack.runtime import store as store_module

    monkeypatch.setattr(store_module, "_MAX_ACTIVE_DEFERRED_STEERS", 1)
    store = AgentStateStore("default", root=tmp_path)
    store.apply("deferred.register", _registration())

    with pytest.raises(AgentStateConflict, match="capacity reached"):
        store.apply(
            "deferred.register",
            _registration(
                expected_revision=1,
                deferred_steer_id="steer-2",
                idempotency_key="register-request-2",
                dedupe_key="second-follow-up",
            ),
        )


def test_failed_application_remains_visible_and_can_retry(tmp_path: Path) -> None:
    """Persist a visible failure before a later normal-path application retry."""

    store = AgentStateStore("default", root=tmp_path)
    created = store.apply("deferred.register", _registration())["deferred_steer"]
    failed = store.apply(
        "deferred.fail",
        _arguments(
            "deferred.fail",
            {
                "expected_revision": 1,
                "deferred_steer_id": "steer-1",
                "expected_steer_revision": created["revision"],
                "error": "normal conversation message creation failed",
            },
        ),
    )["deferred_steer"]
    recovered = store.apply(
        "deferred.apply",
        _arguments(
            "deferred.apply",
            {
                "expected_revision": 2,
                "deferred_steer_id": "steer-1",
                "expected_steer_revision": failed["revision"],
                "application_reference": {
                    "kind": "conversation_instruction",
                    "conversation_id": "conversation-1",
                    "message_id": "message-retry-1",
                },
            },
        ),
    )["deferred_steer"]

    visible = store.list_deferred(scope_type="conversation", scope_id="conversation-1")[
        "deferred_steers"
    ]
    assert failed["status"] == "failed"
    assert failed["error"] == "normal conversation message creation failed"
    assert recovered["status"] == "applied"
    assert visible[0]["id"] == "steer-1"
    assert visible[0]["provenance"]["source_id"] == "run-1"


def test_conversation_route_registers_visible_non_auto_executing_deferred_steer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expose the canonical store through the existing structured steer action."""

    from blocks.conversation.steer import run
    from domain.chat import deferred_steer as facade_module

    store = AgentStateStore("default", root=tmp_path)
    authority_scopes: list[dict[str, object]] = []

    def invoke(contract: str, operation: str, payload: dict[str, object]):
        if contract == facade_module.AUTHORITY:
            authority_scopes.append(payload)
            return {"authorized": True, "receipt": "receipt-1"}
        if contract == facade_module.RESOURCE and operation == "deferred.list":
            statuses = payload.get("statuses")
            return store.list_deferred(
                scope_type=str(payload.get("scope_type") or ""),
                scope_id=str(payload.get("scope_id") or ""),
                statuses=set(statuses) if isinstance(statuses, list) else None,
            )
        if contract == facade_module.ACTION:
            return store.apply(operation, _arguments(operation, payload))
        raise AssertionError(f"unexpected contract call: {contract}/{operation}")

    monkeypatch.setattr(facade_module, "_profile_id", lambda: "default")
    monkeypatch.setattr(facade_module, "_invoke", invoke)

    registered = run(
        {
            "action": "register_deferred",
            "deferred_steer_id": "route-steer-1",
            "idempotency_key": "route-request-1",
            "title": "Investigate later",
            "instruction": "Finish the current task, then inspect persistence.",
            "reason": "A separate failure was observed.",
            "scope_type": "conversation",
            "scope_id": "conversation-1",
            "checkpoint": "after_execution",
            "source": "ai",
        },
        {"principal_id": "agent-1", "run_id": "run-1"},
    )

    assert registered["status"] == "ok"
    assert registered["data"]["id"] == "route-steer-1"
    assert registered["data"]["deferred"] is True
    assert registered["data"]["visible"] is True
    assert registered["data"]["auto_send"] is False
    assert registered["data"]["confirmation"] == "Deferred steer registered"
    assert authority_scopes[0]["authority"] == "agent.state.manage"
    assert authority_scopes[0]["approval_required"] is False
