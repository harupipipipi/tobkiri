"""Authenticated tool progress producers and stage-aware read cursors."""

from types import SimpleNamespace

import pytest

from ecosystem.rumi_turn_runtime_pack.runtime import progress_host
from ecosystem.rumi_turn_runtime_pack.runtime.progress import TurnProgressJournal
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.turn_progress_v1 import payload_digest


@pytest.fixture
def owner(tmp_path, monkeypatch):
    payload = {"tool_id": "file.read", "tool_call_id": "call", "arguments": {"path": "x"}}
    producer = SimpleNamespace(value="tool-broker")
    parent_context = SimpleNamespace(
        profile_id="profile", plan_digest="plan", security_epoch=1, request_id="request"
    )
    envelope = SimpleNamespace(
        context=parent_context,
        contract_version="1.0.0",
        contract_id="tobkiri.service.tool.invoke.v1",
        operation_id="rumi_tool_broker_pack.tool-invoke",
        target_principal=producer,
        target_domain=SimpleNamespace(value="domain"),
        request_digest="captured-input",
    )

    class Parent:
        def assert_current(self):
            pass

        def public_payload(self):
            return {**payload, "progress_id": identity}

    parent = Parent()
    parent.envelope = envelope
    monkeypatch.setattr(progress_host, "CapturedInvocationScopeV4", Parent)
    catalog = SimpleNamespace(
        operation=SimpleNamespace(
            contract_id=envelope.contract_id,
            operation_id=envelope.operation_id,
            contract_version="1.0.0",
        ),
        principal_ref=producer,
        function=SimpleNamespace(function_id="rumi_tool_broker_pack.tool-broker.invoke"),
    )
    context = SimpleNamespace(
        profile_id="profile", plan_digest="plan", security_epoch=1, catalog_bindings=(catalog,)
    )

    def conversation_read(contract, operation, request):
        assert contract == progress_host.CONVERSATION
        assert operation == progress_host.CONVERSATION_OPERATION
        assert request == {
            "profile_id": context.profile_id,
            "operation": "get",
            "conversation_id": "conversation",
        }
        return {"conversation": {"conversation_revision": 2, "current_node_id": "user"}}

    client = SimpleNamespace(invoke=conversation_read)
    invocation = SimpleNamespace(
        assert_current=lambda: None,
        parent_invocation=parent,
        presentation_owner_principal_id="owner",
        presentation_owner_session_id="session",
        envelope=SimpleNamespace(context=SimpleNamespace(caller_principal=producer)),
        contract_client=lambda **kwargs: client,
    )
    store = SimpleNamespace(
        path=tmp_path / "turn.sqlite3",
        _check_path=lambda: None,
        get=lambda _: {"status": "running"},
    )
    journal = TurnProgressJournal(tmp_path / "progress.sqlite3")
    binding = {
        "turn_id": "turn",
        "conversation_id": "conversation",
        "conversation_revision": 2,
        "parent_id": "user",
        "input_digest": canonical_digest({}),
        "request_id": "request",
        "ai_input_digest": payload_digest(payload),
    }
    identity = journal.begin(
        binding,
        owner=canonical_digest({"principal": "owner", "session": "session"}),
        capture=canonical_digest(
            {"profile_id": "profile", "plan_digest": "plan", "security_epoch": 1}
        ),
    )

    def operation(phase, **fields):
        return progress_host.progress_operation(
            context,
            store,
            resource=False,
            payload={"phase": phase, "progress_id": identity, **fields},
            invocation=invocation,
        )

    return operation, payload, envelope, identity, invocation, context, store


def test_owner_accepts_only_exact_arguments_and_one_captured_tool_execution(owner):
    operation, payload, _, _, _, _, _ = owner
    assert operation("tool_bind") == {"bound": True}
    with pytest.raises(PermissionError):
        operation("tool_bind")
    with pytest.raises(PermissionError, match="arguments changed"):
        operation(
            "tool_publish",
            cursor=1,
            event={"type": "tool_started", **payload, "arguments": {"path": "other"}},
        )
    operation("tool_publish", cursor=1, event={"type": "tool_started", **payload})
    operation(
        "tool_publish",
        cursor=2,
        event={
            "type": "tool_completed",
            "tool_id": "file.read",
            "tool_call_id": "call",
            "status": "success",
            "content": '{"status":"success","result":1,"error":null}',
        },
    )


def test_foreign_producer_cannot_claim_tool_progress(owner):
    operation, _, envelope, _, _, _, _ = owner
    envelope.operation_id = "foreign.operation"
    with pytest.raises(PermissionError, match="captured tool broker"):
        operation("tool_bind")


def test_authenticated_stage_change_resets_cursor_but_same_stage_never_rewinds(owner):
    operation, payload, _, identity, invocation, context, store = owner
    operation("tool_bind")
    operation("tool_publish", cursor=1, event={"type": "tool_started", **payload})

    def read(prior, cursor):
        return progress_host.progress_operation(
            context,
            store,
            resource=True,
            payload={
                "turn_id": "turn",
                "conversation_id": "conversation",
                "cursor": cursor,
                "progress_id": prior,
            },
            invocation=invocation,
        )

    assert len(read("previous-stage", 4)["events"]) == 1
    assert read(identity, 1)["events"] == []
    with pytest.raises(ValueError, match="ahead"):
        read(identity, 4)
    with pytest.raises(ValueError, match="cursor"):
        read("previous-stage", -1)


def test_identical_captured_edges_preserve_one_tool_producer(owner):
    """Repeated edges to an identical captured binding remain one producer."""
    operation, payload, _, _, _, context, _ = owner
    context.catalog_bindings = context.catalog_bindings * 2
    assert operation("tool_bind") == {"bound": True}
    assert operation("tool_publish", cursor=1, event={"type": "tool_started", **payload}) == {
        "cursor": 1
    }
    assert operation(
        "tool_publish",
        cursor=2,
        event={
            "type": "tool_completed",
            "tool_id": payload["tool_id"],
            "tool_call_id": payload["tool_call_id"],
            "status": "success",
            "content": '{"status":"success","result":null,"error":null}',
        },
    ) == {"cursor": 2}


def test_distinct_matching_captured_bindings_are_ambiguous(owner):
    """A different complete route cannot collapse into the same producer."""
    operation, _, _, _, _, context, _ = owner
    original = context.catalog_bindings[0]
    different = SimpleNamespace(**vars(original), route="different-route")
    context.catalog_bindings = (original, different)
    with pytest.raises(PermissionError, match="producer is not captured"):
        operation("tool_bind")
