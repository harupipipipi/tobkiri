"""Separate side history, immutable lineage, saved-effect and race regressions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from ecosystem.defaultspack.runtime import saved_conversation as saved
from ecosystem.rumi_conversation_store_pack.runtime.store import (
    ConversationConflict,
)
from ecosystem.tobkiri_side_chat_pack.runtime.side_chat import (
    CONVERSATION,
    MANAGE,
    TURN,
    EVENTS,
    SAVED,
    STOP,
    RECONCILE,
    SideChat,
)
from tests.test_conversation_lifecycle_saved_boundary import _exchange
from tests.test_saved_bridge_callbacks import _frame
from tobkiri_protocol.conversation_context import (
    LINK_VERSION,
    context_binding,
    context_link,
    inherited_context,
    resolve_linked_conversation,
    resolve_request_context,
)
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input


class PublicClient:
    """Actual owner and producer/Host, with explicit turn transport adapters."""

    def __init__(self, tmp_path: Path) -> None:
        self.exchange = _exchange(
            tmp_path,
            {
                "status": "ok",
                "output": "Side answer",
                "finish_reason": "stop",
            },
        )
        self.store = self.exchange.store
        self.calls: list[tuple[Any, Any]] = []
        self.turns: dict[str, Any] = {}
        self.before_create: Any = None

    def invoke(self, contract: str, operation: str, payload: Any) -> Any:
        target = (contract, operation)
        self.calls.append((target, deepcopy(payload)))
        if target == CONVERSATION:
            assert payload["profile_id"] == "defaults"
            if payload["operation"] == "list":
                return self.store.snapshot()
            return {"conversation": self.store.get(payload["conversation_id"])}
        if target == MANAGE:
            if self.before_create is not None:
                callback, self.before_create = self.before_create, None
                callback()
            return self.store.create(
                payload["conversation"],
                expected_revision=payload["expected_revision"],
            )
        if target == TURN:
            if payload["operation"] == "list":
                return {
                    "turns": [
                        value
                        for value in self.turns.values()
                        if value["conversation_id"] == payload["conversation_id"]
                    ]
                }
            return self.turns.get(payload["turn_id"])
        if target == SAVED:
            exchange = self.exchange
            exchange.outer.payload = payload
            exchange.intent = saved.start(payload["request"])
            for _ in range(4):
                exchange.step()
            exchange.host.finish(exchange.intent)
            record = {
                "id": payload["request"]["turn_id"],
                "conversation_id": payload["request"]["conversation_id"],
                "status": "completed",
                "revision": 1,
                "events": [],
            }
            self.turns[record["id"]] = record
            return {"status": "completed", "turn": record}
        if target == EVENTS:
            return self.turns[payload["turn_id"]]
        if target == STOP:
            return {
                "status": "cancellation_requested",
                "turn_id": payload["turn_id"],
                "stopped": False,
            }
        assert target == RECONCILE
        return {"status": "existing", "turn": self.turns[payload["turn_id"]]}


@pytest.fixture
def service(tmp_path: Path) -> tuple[SideChat, PublicClient]:
    client = PublicClient(tmp_path)
    return SideChat(client, "defaults"), client


def _child_record(parent: Any, identifier: str = "child") -> Any:
    return {
        "id": identifier,
        "parent_conversation_id": parent["id"],
        "context_link": {
            "version": LINK_VERSION,
            "parent_conversation_id": parent["id"],
            "slot": "side-chat",
            "created_from_parent_revision": parent["conversation_revision"],
        },
    }


def test_ensure_creates_single_hidden_child_without_parent_history(
    service: Any,
) -> None:
    side, client = service
    assert side.get("conversation-1")["status"] == "missing"
    result = side.ensure("conversation-1", 1)
    parent = client.store.get("conversation-1")
    child = client.store.get(result["conversation_id"])
    assert parent["conversation_revision"] == 2
    assert child["model_reference"] == ""
    assert child["metadata"] == {"conversation_channel": "side", "is_hidden": True}
    assert result["thread"]["conversation"]["model_reference"] == "model-profile-1"
    assert child["messages"] == []
    again = side.ensure("conversation-1", 2)
    assert again["conversation_id"] == child["id"]
    assert len(client.store.snapshot()["conversations"]) == 2


def test_owner_slot_uniqueness_and_parent_revision_cas(service: Any) -> None:
    side, client = service
    parent = client.store.get("conversation-1")
    client.store.create(_child_record(parent), expected_revision=1)
    with pytest.raises(ConversationConflict):
        client.store.create(_child_record(parent, "other"), expected_revision=2)
    fresh = client.store.get("conversation-1")
    with pytest.raises(ConversationConflict, match="slot"):
        client.store.create(_child_record(fresh, "other"), expected_revision=2)
    assert len(client.store.snapshot()["conversations"]) == 2
    with pytest.raises(ValueError, match="revision"):
        side.ensure("conversation-1", 1)


@pytest.mark.parametrize(
    "patch",
    [
        {"parent_conversation_id": None},
        {"context_link": None},
        {"context_link": {"version": "forged"}},
    ],
)
def test_context_link_cannot_be_relinked_or_removed(service: Any, patch: Any) -> None:
    side, client = service
    child = side.ensure("conversation-1", 1)
    before = client.store.path.read_bytes()
    with pytest.raises(ValueError, match="immutable"):
        client.store.update(
            child["conversation_id"], patch, expected_conversation_revision=1
        )
    assert client.store.path.read_bytes() == before


def test_parent_delete_retains_child_lineage_and_history_but_no_execution(
    service: Any,
) -> None:
    side, client = service
    child = side.ensure("conversation-1", 1)
    client.store.delete("conversation-1", expected_conversation_revision=2)
    stored = client.store.get(child["conversation_id"])
    assert context_link(stored)["parent_conversation_id"] == "conversation-1"
    assert side.get("conversation-1")["status"] == "unavailable"
    with pytest.raises(LookupError):
        side.send(
            {
                "conversation_id": stored["id"],
                "expected_child_revision": 1,
                "expected_parent_revision": 2,
                "turn_id": "side-turn",
                "content": "Hi",
            }
        )
    assert not any(target == SAVED for target, _ in client.calls)


def test_fresh_binding_inherits_context_references_and_never_approval_flags(
    service: Any,
) -> None:
    _, client = service
    parent = client.store.get("conversation-1")
    parent["metadata"] = {
        "workspace_id": "workspace",
        "workspace_root": "/workspace",
        "approval_policy_reference": "policy",
        "approved": True,
        "approval_mode": "unrestricted",
    }
    child = _child_record(parent)
    child.update({"metadata": {}, "model_reference": "", "messages": []})
    binding = context_binding(parent, "defaults")
    resolved = resolve_linked_conversation(child, parent, binding, "defaults")
    assert resolved["model_reference"] == parent["model_reference"]
    assert resolved["metadata"]["workspace_root"] == "/workspace"
    assert "approved" not in resolved["metadata"]
    assert "approval_mode" not in inherited_context(parent)["metadata"]
    assert resolved["messages"] == []


@pytest.mark.parametrize("change", ["revision", "model", "workspace", "profile"])
def test_stale_or_different_context_binding_is_unavailable(
    service: Any, change: str
) -> None:
    side, client = service
    result = side.ensure("conversation-1", 1)
    parent = client.store.get("conversation-1")
    child = client.store.get(result["conversation_id"])
    binding = result["context_binding"]
    if change == "profile":
        binding = {**binding, "profile_id": "another"}
    elif change == "revision":
        parent["conversation_revision"] += 1
    elif change == "model":
        parent["model_reference"] = "another-model"
    else:
        parent["metadata"]["workspace_id"] = "another-workspace"
    with pytest.raises(ValueError, match="stale"):
        resolve_linked_conversation(child, parent, binding, "defaults")


@pytest.mark.parametrize(
    "patch",
    [
        {"model_reference": "other-model"},
        {"agent_id": "agent"},
        {"metadata": {"workspace_root": "/outside"}},
        {"metadata": {"tool_selection": {"mode": "auto"}}},
    ],
)
def test_child_cannot_override_parent_execution_context(
    service: Any, patch: Any
) -> None:
    side, client = service
    result = side.ensure("conversation-1", 1)
    parent = client.store.get("conversation-1")
    child = client.store.get(result["conversation_id"])
    child.update(patch)
    with pytest.raises(ValueError, match="override"):
        resolve_linked_conversation(
            child, parent, result["context_binding"], "defaults"
        )


@pytest.mark.parametrize(
    "parent_patch",
    [
        {"metadata": {"workspace_id": "workspace"}},
        {"agent_id": "agent"},
        {"conversation_kind": "coding"},
        {"metadata": {"shared_read_only": True}},
    ],
)
def test_unresolved_workspace_agent_or_restricted_parent_truthfully_unavailable(
    service: Any,
    parent_patch: Any,
) -> None:
    side, client = service
    client.store.update(
        "conversation-1", parent_patch, expected_conversation_revision=1
    )
    assert side.get("conversation-1")["status"] == "unavailable"
    with pytest.raises(ValueError, match="resolution"):
        side.ensure("conversation-1", 2)
    assert len(client.store.snapshot()["conversations"]) == 1


def test_saved_turn_uses_parent_model_and_only_separate_child_history(
    service: Any,
) -> None:
    side, client = service
    client.store.append_message(
        "conversation-1",
        {"id": "main-user", "role": "user", "content": "Main secret"},
        expected_conversation_revision=1,
    )
    result = side.ensure("conversation-1", 2)
    outcome = side.send(
        {
            "conversation_id": result["conversation_id"],
            "expected_child_revision": 1,
            "expected_parent_revision": 3,
            "turn_id": "side-turn",
            "content": "どう？",
        }
    )
    assert outcome["turn"]["status"] == "completed"
    assert [
        message["content"] for message in client.store.get("conversation-1")["messages"]
    ] == ["Main secret"]
    child = client.store.get(result["conversation_id"])
    assert [message["content"] for message in child["messages"]] == [
        "どう？",
        "Side answer",
    ]
    assert child["lifecycle"]["state"] == "completed"
    ai = [
        payload
        for target, payload in client.exchange.calls
        if target == saved.TARGETS[2]
    ][-1]
    assert ai["model_reference"] == "model-profile-1"
    assert ai["messages"] == [{"role": "user", "content": "どう？"}]
    send = [payload for target, payload in client.calls if target == SAVED][-1]
    assert send["request"]["context_binding"] == result["context_binding"]


@pytest.mark.parametrize("stage", ["user", "ai", "assistant"])
def test_parent_revision_change_at_each_resume_rejects_next_effect(
    service: Any,
    stage: str,
) -> None:
    side, client = service
    result = side.ensure("conversation-1", 1)
    exchange = client.exchange
    exchange.outer.payload = {
        "request": {
            "turn_id": "side-turn",
            "conversation_id": result["conversation_id"],
            "conversation_revision": 1,
            "content": "Side request",
            "context_binding": result["context_binding"],
        }
    }
    intent = saved.start(exchange.outer.payload["request"])
    steps = {"user": 1, "ai": 2, "assistant": 3}[stage]
    for _ in range(steps):
        intent = saved.resume(
            intent["state"], exchange.callback(exchange.outer, _frame(intent))
        )
    parent = client.store.get("conversation-1")
    client.store.update(
        "conversation-1",
        {"title": "Changed"},
        expected_conversation_revision=parent["conversation_revision"],
    )
    before = client.store.path.read_bytes()
    with pytest.raises(AuthorityDenied, match="stale"):
        exchange.callback(exchange.outer, _frame(intent))
    assert client.store.path.read_bytes() == before


def test_linked_turn_cannot_expand_parent_tool_selection(service: Any) -> None:
    side, client = service
    result = side.ensure("conversation-1", 1)
    child = client.store.get(result["conversation_id"])
    parent = client.store.get("conversation-1")
    with pytest.raises(ValueError, match="expand"):
        resolve_request_context(
            child,
            {
                "context_binding": result["context_binding"],
                "tool_selection": {"mode": "auto"},
            },
            "defaults",
            lambda _: parent,
        )


def test_cross_conversation_stop_events_reconcile_never_dispatch(service: Any) -> None:
    side, client = service
    result = side.ensure("conversation-1", 1)
    client.turns["main-turn"] = {"id": "main-turn", "conversation_id": "conversation-1"}
    for action in ("events", "stop", "reconcile"):
        with pytest.raises(ValueError, match="does not belong"):
            side.turn(action, result["conversation_id"], "main-turn")
    assert not any(target in (EVENTS, STOP, RECONCILE) for target, _ in client.calls)


def test_retry_stale_send_reconciles_without_duplicate_message_or_ai(
    service: Any,
) -> None:
    side, client = service
    result = side.ensure("conversation-1", 1)
    payload = {
        "conversation_id": result["conversation_id"],
        "expected_child_revision": 1,
        "expected_parent_revision": 2,
        "turn_id": "side-turn",
        "content": "Hi",
    }
    side.send(payload)
    count = len([target for target, _ in client.calls if target == SAVED])
    with pytest.raises(ValueError, match="child.*revision"):
        side.send(payload)
    assert len([target for target, _ in client.calls if target == SAVED]) == count
    assert (
        side.turn("reconcile", result["conversation_id"], "side-turn")["status"]
        == "existing"
    )
    assert len(client.store.get(result["conversation_id"])["messages"]) == 2


def test_missing_dependency_never_fakes_available_child(service: Any) -> None:
    side, _ = service
    side.client = SimpleNamespace(
        invoke=lambda *args: (_ for _ in ()).throw(LookupError())
    )
    assert side.get("conversation-1")["status"] == "unavailable"


def test_context_binding_schema_and_guest_validate_the_same_strict_input(
    service: Any,
) -> None:
    side, _ = service
    result = side.ensure("conversation-1", 1)
    initial = {
        "request": {
            "turn_id": "side-turn",
            "conversation_id": result["conversation_id"],
            "conversation_revision": 1,
            "content": "Hi",
            "context_binding": result["context_binding"],
        }
    }
    assert validate_saved_conversation_input(initial) == initial
    assert (
        saved.start(initial["request"])["state"]["request"]["context_binding"]
        == result["context_binding"]
    )
    from tobkiri_protocol.validation import validate_document

    validate_document(initial, "saved_conversation_input_v1.schema.json")
    initial["request"]["context_binding"]["approved"] = True
    with pytest.raises(ValueError):
        validate_saved_conversation_input(initial)
    with pytest.raises(ValueError):
        saved.start(initial["request"])


def test_concurrent_ensure_recovers_only_exact_owner_slot_winner(service: Any) -> None:
    side, client = service
    parent = client.store.get("conversation-1")
    client.before_create = lambda: client.store.create(
        _child_record(parent, "winner"),
        expected_revision=1,
    )
    result = side.ensure("conversation-1", 1)
    assert result["conversation_id"] == "winner"
    assert len(client.store.snapshot()["conversations"]) == 2


@pytest.mark.parametrize("kind", ["resource", "manage", "turn"])
def test_capture_exact_profile_version_and_restricted_client(
    service: Any, kind: str
) -> None:
    from ecosystem.tobkiri_side_chat_pack.runtime.host import (
        BINDINGS,
        HOST_PROVIDER_FACTORY,
        PACK_ID,
    )

    side, client = service
    child = side.ensure("conversation-1", 1)
    contract, suffix = BINDINGS[kind]
    operation = f"{PACK_ID}.{suffix}"
    function = f"{PACK_ID}.{kind}"
    context = SimpleNamespace(
        profile_id="defaults",
        provider_bindings=(
            SimpleNamespace(
                function=SimpleNamespace(
                    function_id=function, implementation_digest="implementation"
                ),
                operation=SimpleNamespace(
                    contract_id=contract,
                    operation_id=operation,
                    contract_version="1.0.0",
                ),
                principal_ref=SimpleNamespace(value="principal"),
                artifact=SimpleNamespace(digest="artifact"),
            ),
        ),
        domain_ids={(contract, operation, "principal"): "domain"},
    )
    claims = []
    checks = []
    invocation = SimpleNamespace(
        assert_current=lambda: checks.append("current"),
        contract_client=lambda **kwargs: claims.append(kwargs) or client,
    )
    captured = HOST_PROVIDER_FACTORY[function].capture(context)
    invoke = captured.contributions[0].invoke
    payload = {
        "resource": {"operation": "get", "parent_conversation_id": "conversation-1"},
        "manage": {
            "operation": "ensure",
            "parent_conversation_id": "conversation-1",
            "expected_parent_revision": 2,
        },
        "turn": {
            "operation": "send",
            "conversation_id": child["conversation_id"],
            "turn_id": "side-turn",
            "expected_child_revision": 1,
            "expected_parent_revision": 2,
            "content": "Hi",
        },
    }[kind]
    invoke(operation, {"profile_id": "defaults", **payload}, invocation)
    assert checks == ["current"]
    assert claims[0]["include_credentials"] is False
    assert claims[0]["consumer_pack_id"] == PACK_ID
    if kind == "resource":
        assert claims[0]["allowed_contract_ids"] == frozenset(
            {CONVERSATION[0], TURN[0], EVENTS[0]}
        )
    for extra in (
        {"approved": True},
        {"model_reference": "forged"},
        {"workspace_root": "/outside"},
    ):
        with pytest.raises(PermissionError, match="fields"):
            invoke(
                operation, {"profile_id": "defaults", **payload, **extra}, invocation
            )
    with pytest.raises(PermissionError, match="Profile"):
        invoke(operation, {"profile_id": "other", **payload}, invocation)
    context.provider_bindings[0].operation.contract_version = "2.0.0"
    with pytest.raises(PermissionError, match="binding"):
        HOST_PROVIDER_FACTORY[function].capture(context)


def test_buffered_event_and_cancel_receipts_remain_truthful(service: Any) -> None:
    side, client = service
    result = side.ensure("conversation-1", 1)
    client.turns["side-turn"] = {
        "id": "side-turn",
        "conversation_id": result["conversation_id"],
        "status": "running",
        "events": [],
    }
    events = side.turn("events", result["conversation_id"], "side-turn")
    assert events["events"] == []
    assert events["status"] == "running"
    assert side.turn("stop", result["conversation_id"], "side-turn") == {
        "status": "cancellation_requested",
        "turn_id": "side-turn",
        "stopped": False,
    }
