"""Source ancestry, sealed recipients and actual receipt tests."""

import importlib.util
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS

import pytest


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[1]
protocol = _load(
    "tobkiri_protocol.chat_message_v1", ROOT / "tobkiri_protocol/chat_message_v1.py"
)
owned = _load(
    "core_runtime.owned_chat_message_approval_v4",
    ROOT / "core_runtime/owned_chat_message_approval_v4.py",
)
tool = _load(
    "ecosystem.rumi_default_tools_pack.runtime.chat_messages",
    ROOT / "ecosystem/rumi_default_tools_pack/runtime/chat_messages.py",
)


def context():
    return NS(
        **{
            key: "capture"
            for key in (
                "profile_revision",
                "activation_id",
                "activation_digest",
                "plan_digest",
                "profile_authority_digest",
                "security_epoch",
                "fencing_token",
            )
        },
        profile_id="profile",
        caller_principal=NS(value="owner"),
        caller_session_id="session",
    )


def ancestry():
    ctx = context()

    def scope(operation, parent=None, payload=None):
        return NS(
            envelope=NS(
                contract_id=operation[0],
                operation_id=operation[1],
                context=ctx,
                payload=payload or {},
            ),
            parent=parent,
            assert_current=lambda: None,
        )

    root = scope(
        (owned.SAVED_CANONICAL, "rumi_turn_runtime_pack.turn-saved"),
        payload={"request": {"conversation_id": "source", "turn_id": "turn"}},
    )
    broker = scope(owned.BROKER, root)
    executor = scope(owned.EXECUTOR, broker)
    invocation = NS(
        envelope=scope(owned.LOCAL).envelope,
        parent_invocation=executor,
        assert_current=lambda: None,
        presentation_owner_principal_id="owner",
        presentation_owner_session_id="session",
    )
    return invocation, root


def request(invocation):
    return owned.register_chat_message_tool_request(
        {
            "target_kind": "chat",
            "target_id": "target",
            "content": "Hello",
            "delivery": "steer",
            "source_conversation_id": "source",
            "source_turn_id": "turn",
            "profile_id": "profile",
            "tool_call_id": "call",
        },
        invocation,
    )


def snapshot(ids=None):
    return {
        "kind": "tobkiri.chat.reference.snapshot.v1",
        "profile_id": "profile",
        "store_revision": 1,
        "project_revision": 1,
        "snapshot_time": int(time.time() * 1000) - 1000,
        "expires_at": int(time.time() * 1000) + 90000,
        "next_cursor": None,
        "truncated": False,
        "references": [
            {
                "kind": "chat",
                "id": "target",
                "label": "Target",
                "conversation_ids": ids or ["target"],
                "snapshot_digest": "digest",
                "member_count": len(ids or ["target"]),
                "membership_complete": True,
            }
        ],
    }


def test_tool_arguments_reject_claimed_source_and_approval():
    base = {"target_kind": "chat", "target_id": "target", "content": "Hello"}
    for key in ("source_conversation_id", "profile_id", "approved", "approval_token"):
        with pytest.raises(ValueError):
            protocol.message_arguments({**base, key: "fake"})


def test_source_and_content_are_bound_to_live_saved_ancestry():
    invocation, root = ancestry()
    req = request(invocation)
    try:
        owned.assert_chat_message_request_live(req, context())
        with pytest.raises(PermissionError):
            owned.assert_chat_message_request_live(
                {**req, "content": "changed"}, context()
            )
        root.envelope.payload["request"]["conversation_id"] = "other"
        # Source must be revalidated on each use, not only at registration.
        with pytest.raises(PermissionError):
            owned.assert_chat_message_request_live(req, context())
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_execute_rejects_changed_membership_without_delivery():
    invocation, _ = ancestry()
    req = request(invocation)
    plan = protocol.create_plan(req, snapshot())
    observed = []
    invocation.contract_client = lambda **kwargs: NS(
        invoke=lambda *args: observed.append(args) or snapshot(["different"])
    )
    try:
        with pytest.raises(PermissionError, match="membership"):
            tool._bind_execute(NS())({"request": req, "plan": plan}, invocation)
        assert all(call[0] == tool.REFERENCE for call in observed)
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_execute_retains_actual_partial_receipt():
    invocation, _ = ancestry()
    req = request(invocation)
    plan = protocol.create_plan(req, snapshot())
    receipt = {
        "version": "tobkiri.chat.message-delivery.v1",
        "plan_digest": plan["plan_digest"],
        "status": "partial",
        "deliveries": [{"target_conversation_id": "target", "status": "queued"}],
    }
    invocation.contract_client = lambda **kwargs: NS(
        invoke=lambda contract, op, payload: (
            snapshot() if contract == tool.REFERENCE else receipt
        )
    )
    try:
        owned.bind_chat_message_effect(
            "receipt-effect", req, context(), lambda: None, plan=plan
        )
        execute = NS(**vars(invocation))
        execute.envelope = NS(**vars(invocation.envelope))
        execute.envelope.contract_id = tool.CONTRACT
        execute.envelope.operation_id = tool.EXECUTE
        execute.envelope.payload = {"request": req, "plan": plan}
        assert (
            tool._bind_execute(NS())({"request": req, "plan": plan}, execute) == receipt
        )
        assert owned.take_chat_message_receipt(req, context()) == receipt
        with pytest.raises(PermissionError):
            owned.take_chat_message_receipt(req, context())
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_cancel_drains_effect_and_source_self_send_fails():
    invocation, _ = ancestry()
    req = request(invocation)
    cancelled = []
    owned.bind_chat_message_effect(
        "effect",
        req,
        context(),
        lambda: cancelled.append(True),
        plan=protocol.create_plan(req, snapshot()),
    )
    guard = owned.chat_message_effect_execution_guard("effect", context())
    with pytest.raises(PermissionError):
        protocol.create_plan(req, snapshot(["source"]))
    owned.close_chat_message_tool_request(req["invocation_key"])
    assert cancelled == [True]
    with pytest.raises(PermissionError):
        guard()


def test_native_denial_cancels_and_never_resumes(monkeypatch):
    invocation, _ = ancestry()
    calls = []

    def dispatch(contract, operation, payload):
        calls.append(payload["phase"])
        return {
            "effect_id": "effect",
            "approval_request_id": "approval",
            "state": "approval_pending" if payload["phase"] == "prepare" else "denied",
        }

    invocation.contract_client = lambda **kwargs: NS(invoke=dispatch)
    monkeypatch.setattr(
        tool, "open_chat_message_tool_approval", lambda *args, **kwargs: None
    )
    invoke = tool._bind_tool(
        NS(interactive_approval_port=object(), authority_approval_window_port=object())
    )
    with pytest.raises(PermissionError, match="not approved"):
        invoke(
            {
                "tool_id": "chat_send_message",
                "tool_call_id": "call",
                "arguments": {
                    "target_kind": "chat",
                    "target_id": "target",
                    "content": "Hello",
                },
            },
            invocation,
        )
    assert calls == ["prepare", "status", "cancel"]


def test_incoming_delivery_ancestry_cannot_send():
    invocation, root = ancestry()
    root.parent = NS(
        envelope=NS(
            contract_id=tool.DELIVERY,
            operation_id=tool.DELIVERY_OPERATION,
            context=context(),
            payload={},
        ),
        parent=None,
        assert_current=lambda: None,
    )
    with pytest.raises(PermissionError, match="nested incoming"):
        owned.authenticated_chat_message_tool_owner(invocation)


def test_ancestry_cycle_and_fake_presenter_fail():
    invocation, root = ancestry()
    invocation.presentation_owner_principal_id = "fake"
    with pytest.raises(PermissionError, match="owner changed"):
        owned.authenticated_chat_message_tool_owner(invocation)
    invocation.presentation_owner_principal_id = "owner"
    root.parent = root
    with pytest.raises(PermissionError, match="ancestry"):
        owned.authenticated_chat_message_tool_owner(invocation)


def test_maximum_content_and_recipients_present_every_character():
    invocation, _ = ancestry()
    req = request(invocation)
    try:
        req = {**req, "content": "x" * protocol.MAX_CONTENT_BYTES}
        ids = [f"target-{index:02d}-" + "z" * 246 for index in range(16)]
        plan = protocol.create_plan(req, snapshot(ids))
        metadata = protocol.approval_metadata(req, plan)
        assert len(metadata) <= 32
        assert all(len(value) <= 2048 for value in metadata.values())
        assert (
            "".join(
                metadata[key] for key in sorted(metadata) if key.startswith("message_")
            )
            == req["content"]
        )
        assert "".join(
            metadata[key] for key in sorted(metadata) if key.startswith("recipients_")
        ) == "\n".join(ids)
        with pytest.raises(PermissionError):
            protocol.approval_metadata({**req, "content": req["content"][:-1]}, plan)
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_read_tools_bind_captured_profile_and_reject_claims():
    reads = _load(
        "ecosystem.rumi_default_tools_pack.runtime.chat_reference_tools",
        ROOT / "ecosystem/rumi_default_tools_pack/runtime/chat_reference_tools.py",
    )
    invocation, _ = ancestry()
    calls = []
    invocation.contract_client = lambda **kwargs: NS(
        invoke=lambda *args: calls.append(args) or snapshot()
    )
    invoke = reads._bind(NS(profile_id="profile"))
    for tool_id, arguments in (
        ("chat_list_targets", {}),
        ("chat_resolve_target", {"kind": "chat", "id": "target"}),
    ):
        assert (
            invoke(
                {"tool_id": tool_id, "tool_call_id": "call", "arguments": arguments},
                invocation,
            )["profile_id"]
            == "profile"
        )
    assert calls[0][2] == {"profile_id": "profile", "operation": "list"}
    assert calls[1][2]["references"] == [{"kind": "chat", "id": "target"}]
    with pytest.raises(ValueError):
        invoke(
            {
                "tool_id": "chat_list_targets",
                "tool_call_id": "call",
                "arguments": {"profile_id": "other"},
            },
            invocation,
        )
    with pytest.raises(ValueError):
        invoke(
            {
                "tool_id": "chat_resolve_target",
                "tool_call_id": "call",
                "arguments": {"kind": "chat", "id": "target", "approved": True},
            },
            invocation,
        )


def test_snapshot_clocks_and_global_revisions_can_change():
    invocation, _ = ancestry()
    req = request(invocation)
    try:
        old = snapshot()
        plan = protocol.create_plan(req, old)
        current = {
            **old,
            "snapshot_time": old["snapshot_time"] + 500,
            "expires_at": old["expires_at"] + 500,
            "store_revision": 99,
            "project_revision": 100,
        }
        protocol.assert_current_recipients(req, plan, current)
        current["references"] = [{**old["references"][0], "label": "renamed"}]
        with pytest.raises(PermissionError, match="membership"):
            protocol.assert_current_recipients(req, plan, current)
        with pytest.raises(PermissionError, match="expired"):
            protocol.assert_current_recipients(req, plan, old, now=old["expires_at"])
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_group_source_is_skipped_but_full_membership_remains_bound():
    invocation, _ = ancestry()
    req = {**request(invocation), "target_kind": "group"}
    try:
        resolved = snapshot(["source", "target"])
        resolved["references"][0]["kind"] = "group"
        plan = protocol.create_plan(req, resolved)
        assert plan["recipient_ids"] == ["target"]
        assert plan["snapshot"]["references"][0]["conversation_ids"] == [
            "source",
            "target",
        ]
        metadata = protocol.approval_metadata(req, plan)
        assert metadata["recipients_01"] == "target"
        assert "source_excluded" in metadata
        resolved["references"][0]["conversation_ids"] = ["source"]
        resolved["references"][0]["member_count"] = 1
        with pytest.raises(ValueError, match="effective"):
            protocol.create_plan(req, resolved)
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_actual_identity_projection_is_stable_across_approval_delay():
    identity_path = (
        ROOT.parents[3]
        / "identity/work/tobkiri_runtime/ecosystem/rumi_conversation_store_pack/runtime/chat_reference.py"
    )
    canonical_identity = (
        ROOT / "ecosystem/rumi_conversation_store_pack/runtime/chat_reference.py"
    )
    if canonical_identity.exists():
        identity_path = canonical_identity
    identity = _load("chat_message_identity_projection_test", identity_path)
    invocation, _ = ancestry()
    req = request(invocation)
    conversations = {
        "profile_id": "profile",
        "revision": 1,
        "conversations": [{"id": "target", "title": "Target", "updated_at": 1000}],
    }
    projects = {"namespace": "defaultspack.projects.v1", "revision": 1, "projects": []}
    resolve = {
        "profile_id": "profile",
        "operation": "resolve",
        "references": [{"kind": "chat", "id": "target"}],
    }
    try:
        first = identity.project_references(
            resolve, conversations, projects, profile_id="profile", now_ms=1000
        )
        second = identity.project_references(
            resolve,
            {**conversations, "revision": 2},
            {**projects, "revision": 2},
            profile_id="profile",
            now_ms=2000,
        )
        plan = protocol.create_plan(req, first)
        protocol.assert_current_recipients(req, plan, second, now=2000)
        conversations["conversations"][0]["title"] = "Changed label"
        renamed = identity.project_references(
            resolve, conversations, projects, profile_id="profile", now_ms=2000
        )
        with pytest.raises(PermissionError, match="membership"):
            protocol.assert_current_recipients(req, plan, renamed, now=2000)
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_mode_default_unknown_and_tampering():
    arguments = {"target_kind": "chat", "target_id": "target", "content": "Hello"}
    assert protocol.message_arguments(arguments)["delivery"] == "steer"
    with pytest.raises(ValueError, match="mode"):
        protocol.message_arguments({**arguments, "delivery": "unexpected"})
    invocation, _ = ancestry()
    req = request(invocation)
    try:
        plan = protocol.create_plan(req, snapshot())
        with pytest.raises((ValueError, PermissionError)):
            protocol.validate_execute_payload({**req, "delivery": "interrupt"}, plan)
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_interrupt_fences_exact_target_before_any_delivery():
    invocation, _ = ancestry()
    req = {**request(invocation), "delivery": "interrupt"}
    state = {
        "conversation_id": "target",
        "conversation_revision": 3,
        "turn_id": "run-one",
        "request_id": "request-one",
        "revision": 2,
        "status": "running",
    }
    plan = protocol.create_plan(req, snapshot(), [state])
    seen = []

    def dispatch(contract, operation, payload):
        seen.append(contract)
        if contract == tool.REFERENCE:
            return snapshot()
        if contract == tool.TURN_RESOURCE:
            return {**state, "turn_id": "replacement-run", "revision": 3}
        raise AssertionError("changed target must not reach delivery")

    invocation.contract_client = lambda **kwargs: NS(invoke=dispatch)
    # Bound internal request registration must include the selected mode.
    owned.close_chat_message_tool_request(req["invocation_key"])
    req.pop("invocation_key")
    req = owned.register_chat_message_tool_request(req, invocation)
    plan = protocol.create_plan(req, snapshot(), [state])
    try:
        metadata = protocol.approval_metadata(req, plan)
        assert metadata["delivery"] == "interrupt"
        assert "run-one" in metadata["interrupt_runs_01"]
        with pytest.raises(PermissionError, match="execution changed"):
            tool._bind_execute(NS())({"request": req, "plan": plan}, invocation)
        assert tool.DELIVERY not in seen
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_progress_of_same_run_allowed_replacement_and_rollback_rejected():
    state = {
        "conversation_id": "target",
        "conversation_revision": 3,
        "turn_id": "run",
        "request_id": "request",
        "revision": 2,
        "status": "running",
    }
    protocol.assert_interrupt_targets([state], [{**state, "revision": 3}])
    for altered in (
        {"revision": 1},
        {"turn_id": "new"},
        {"request_id": "new"},
        {"status": "waiting"},
    ):
        with pytest.raises(PermissionError):
            protocol.assert_interrupt_targets([state], [{**state, **altered}])


def test_live_request_alone_does_not_authorize_interrupt():
    invocation, _ = ancestry()
    req = request(invocation)
    try:
        with pytest.raises(PermissionError):
            owned.bind_chat_message_execute_authority(
                req, protocol.create_plan(req, snapshot()), invocation
            )
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_interrupt_authority_requires_bound_actual_execute_envelope():
    invocation, _ = ancestry()
    req = request(invocation)
    owned.close_chat_message_tool_request(req["invocation_key"])
    req.pop("invocation_key")
    req["delivery"] = "interrupt"
    req = owned.register_chat_message_tool_request(req, invocation)
    state = {
        "conversation_id": "target",
        "conversation_revision": 3,
        "turn_id": "run",
        "request_id": "request",
        "revision": 2,
        "status": "running",
    }
    plan = protocol.create_plan(req, snapshot(), [state])
    execute = NS(**vars(invocation))
    execute.envelope = NS(**vars(invocation.envelope))
    execute.envelope.contract_id = tool.CONTRACT
    execute.envelope.operation_id = tool.EXECUTE
    execute.envelope.payload = {"request": req, "plan": plan}
    parent = NS(envelope=execute.envelope, assert_current=lambda: None)
    delivery = NS(**vars(invocation))
    delivery.parent_invocation = parent
    delivery.envelope = NS(**vars(execute.envelope))
    delivery.envelope.contract_id = tool.DELIVERY
    delivery.envelope.operation_id = tool.DELIVERY_OPERATION
    try:
        owned.bind_chat_message_effect(
            "interrupt-effect", req, context(), lambda: None, plan=plan
        )
        with pytest.raises(PermissionError, match="execution proof"):
            owned.assert_chat_message_interrupt_authority(delivery, state)
        owned.bind_chat_message_execute_authority(req, plan, execute)
        outer, presenter = owned.assert_chat_message_interrupt_authority(
            delivery, state
        )
        assert outer is execute.envelope and presenter == "owner"
        parent.envelope = NS(**vars(execute.envelope))
        with pytest.raises(PermissionError, match="execution proof"):
            owned.assert_chat_message_interrupt_authority(delivery, state)
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_prepare_uses_one_immutable_reference_and_turn_client():
    invocation, _ = ancestry()
    req = request(invocation)
    owned.close_chat_message_tool_request(req["invocation_key"])
    req.pop("invocation_key")
    req["delivery"] = "interrupt"
    req = owned.register_chat_message_tool_request(req, invocation)
    state = {
        "conversation_id": "target",
        "conversation_revision": 3,
        "turn_id": "run-one",
        "request_id": "request-one",
        "revision": 2,
        "status": "running",
    }
    bindings = []

    def bind(**kwargs):
        assert not bindings, "production binding cannot be narrowed or rebound"
        bindings.append(kwargs["allowed_contract_ids"])
        return NS(
            invoke=lambda contract, op, payload: (
                snapshot() if contract == tool.REFERENCE else state
            )
        )

    invocation.contract_client = bind
    try:
        plan = tool._bind_prepare(NS())(req, invocation)
        assert plan["target_states"] == [state]
        assert bindings == [frozenset({tool.REFERENCE, tool.TURN_RESOURCE})]
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_execute_uses_one_immutable_reference_turn_delivery_client(monkeypatch):
    invocation, _ = ancestry()
    req = request(invocation)
    plan = protocol.create_plan(req, snapshot())
    bindings = []
    receipt = {
        "version": "tobkiri.chat.message-delivery.v1",
        "plan_digest": plan["plan_digest"],
        "status": "completed",
        "deliveries": [{"target_conversation_id": "target"}],
    }

    def bind(**kwargs):
        assert not bindings, "production binding cannot be narrowed or rebound"
        bindings.append(kwargs["allowed_contract_ids"])
        return NS(
            invoke=lambda contract, op, payload: (
                snapshot() if contract == tool.REFERENCE else receipt
            )
        )

    invocation.contract_client = bind
    monkeypatch.setattr(tool, "bind_chat_message_execute_authority", lambda *args: None)
    try:
        assert (
            tool._bind_execute(NS())({"request": req, "plan": plan}, invocation)
            == receipt
        )
        assert bindings == [
            frozenset({tool.REFERENCE, tool.TURN_RESOURCE, tool.DELIVERY})
        ]
    finally:
        owned.close_chat_message_tool_request(req["invocation_key"])


@pytest.mark.parametrize("phase", ["prepare", "execute"])
@pytest.mark.parametrize("admission", ["matching", "changed", "inactive"])
def test_native_group_decisions_check_private_full_membership(
    monkeypatch, phase, admission
):
    """Private witness checks precede plans, target state and delivery work."""
    from tobkiri_host import active_chat_source, finite_chat_dispatch

    invocation, _ = ancestry()
    raw = request(invocation)
    owned.close_chat_message_tool_request(raw.pop("invocation_key"))
    raw.update(target_kind="group", target_id="group")
    req = owned.register_chat_message_tool_request(raw, invocation)
    fresh = snapshot(["source", "target"])
    fresh["references"][0].update(kind="group", id="group")
    plan = protocol.create_plan(req, fresh)
    witness = object()
    token = NS(active=admission != "inactive", candidate_witness=witness)
    calls = []

    def check(actual_request, actual_snapshot, actual_witness):
        assert actual_request == req
        assert actual_snapshot is fresh
        assert actual_witness is witness
        assert actual_snapshot["references"][0]["conversation_ids"] == [
            "source", "target"
        ]
        calls.append("membership")
        if admission == "changed":
            raise PermissionError("candidate group membership changed")

    monkeypatch.setattr(active_chat_source, "assert_admission_membership", check)
    marker = finite_chat_dispatch._current.set(token)
    receipt = {
        "version": "tobkiri.chat.message-delivery.v1",
        "plan_digest": plan["plan_digest"],
        "status": "completed",
        "deliveries": [{"target_conversation_id": "target"}],
    }

    def invoke(contract, operation, payload):
        calls.append(contract)
        return fresh if contract == tool.REFERENCE else receipt

    invocation.contract_client = lambda **kwargs: NS(invoke=invoke)
    monkeypatch.setattr(tool, "bind_chat_message_execute_authority", lambda *a: None)
    try:
        action = tool._bind_prepare(NS()) if phase == "prepare" else tool._bind_execute(NS())
        payload = req if phase == "prepare" else {"request": req, "plan": plan}
        if admission == "matching":
            result = action(payload, invocation)
            assert result["recipient_ids"] == ["target"] if phase == "prepare" else result == receipt
            assert calls[:2] == [tool.REFERENCE, "membership"]
        else:
            with pytest.raises(PermissionError):
                action(payload, invocation)
            assert tool.DELIVERY not in calls
            assert tool.TURN_RESOURCE not in calls
            if admission == "inactive":
                assert calls == [tool.REFERENCE]
    finally:
        finite_chat_dispatch._current.reset(marker)
        owned.close_chat_message_tool_request(req["invocation_key"])


def test_single_chat_candidate_does_not_require_group_witness(monkeypatch):
    """Single chat admission has no group snapshot to compare."""
    from tobkiri_host import active_chat_source, finite_chat_dispatch

    invocation, _ = ancestry()
    req = request(invocation)
    token = NS(active=True, candidate_witness=object())
    marker = finite_chat_dispatch._current.set(token)
    monkeypatch.setattr(
        active_chat_source,
        "assert_admission_membership",
        lambda *args: pytest.fail("single chat has no group membership witness"),
    )
    invocation.contract_client = lambda **kwargs: NS(invoke=lambda *a: snapshot())
    try:
        assert tool._bind_prepare(NS())(req, invocation)["recipient_ids"] == ["target"]
    finally:
        finite_chat_dispatch._current.reset(marker)
        owned.close_chat_message_tool_request(req["invocation_key"])
