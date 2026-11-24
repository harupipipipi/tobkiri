"""Calendar completion uses exact captured ancestry and real durable receipts."""

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import pytest
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tests.test_operation_cancellation import _envelope
from tests.test_conversation_completion_confirmation import _context
from tests.test_saved_turn_coordinator import _Session, _run
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.conversation_lifecycle import completion_source

RUNTIME = Path(__file__).resolve().parents[1]
PACKAGE = "ecosystem.rumi_conversation_store_pack.runtime"
SPEC = importlib.util.spec_from_file_location(
    PACKAGE + ".calendar_completion_source",
    RUNTIME
    / "ecosystem/rumi_conversation_store_pack/runtime/calendar_completion_source.py",
)
proof = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = proof
SPEC.loader.exec_module(proof)
SPEC = importlib.util.spec_from_file_location(
    "calendar_completion_host_test",
    RUNTIME / "ecosystem/rumi_conversation_store_pack/runtime/completion_host.py",
)
completion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(completion)


class CalendarSession(_Session):
    def __init__(self, root):
        super().__init__(root)
        self.ai_outcome["value"]["finish_reason"] = "stop"
        self.job = {
            "profile_id": "defaults",
            "operation": "dispatch",
            "action_id": "chat.saved",
            "schedule_id": "schedule",
            "idempotency_key": "occurrence",
            "lease_id": "lease",
            "payload": {
                "profile_id": "defaults",
                "conversation_id": "conversation-1",
                "message": self.initial["request"]["content"],
            },
        }
        self.turn = "calendar:" + canonical_digest(
            ["defaults", "schedule", "occurrence"]
        ).removeprefix("sha256:")
        self.initial["request"]["turn_id"] = self.turn
        self.turns.reserve_calendar_preparation(
            occurrence_key="occurrence",
            source=self.job["payload"],
            turn_id=self.turn,
            conversation_id="conversation-1",
            conversation_revision=1,
        )
        self.turns.bind_calendar_preparation(
            occurrence_key="occurrence",
            source=self.job["payload"],
            payload=self.initial,
        )
        self.turns.bind_saved_input(self.initial, self.initial)
        context = _context(root)
        self.calendar_binding = NS(
            function=NS(function_id=proof.CALENDAR_FUNCTION),
            principal_ref=NS(value="calendar-owner"),
            artifact=NS(pack_id="rumi_turn_runtime_pack"),
            operation=NS(
                contract_id=proof.ADAPTER[0],
                operation_id=proof.CALENDAR_FUNCTION,
                contract_version="2.0.0",
            ),
        )
        context.catalog_bindings += (self.calendar_binding,)
        self.context = context
        self.confirm = (
            completion.ConversationCompletionHostFactoryV4()
            .capture(context)
            .contributions[0]
            .invoke
        )
        self.fail_confirmation = False
        self.read_calls = 0

    def invocation(self, *, altered=None, scope_guard=lambda: None):
        seed = _envelope()
        context = replace(seed.context, profile_id="defaults")
        broker = replace(
            seed,
            context=context,
            contract_id=proof.BROKER[0],
            operation_id=proof.BROKER[1],
            payload={
                "profile_id": "defaults",
                "operation": "dispatch",
                "idempotency_key": "occurrence",
            },
        )
        parent = replace(
            seed,
            context=context,
            target_principal=OpaqueAuthorityRef("calendar-owner"),
            contract_id=proof.ADAPTER[0],
            contract_version="2.0.0",
            operation_id=proof.CALENDAR_FUNCTION,
            payload={**self.job, **(altered or {})},
        )
        actual = replace(
            seed,
            context=replace(context, caller_principal=parent.target_principal),
            contract_id=completion.CONTRACT_ID,
            operation_id=completion.OPERATION_ID,
        )
        scope = CapturedInvocationScopeV4(
            parent, scope_guard, CapturedInvocationScopeV4(broker, lambda: None)
        )

        def bind(**kwargs):
            assert kwargs["allowed_contract_ids"] == frozenset(
                {completion.TURN_CONTRACT}
            )

            def read(contract, operation, payload):
                self.read_calls += 1
                return self.turns.get(payload["turn_id"])

            return NS(invoke=read)

        return NS(
            envelope=actual,
            parent_invocation=scope,
            assert_current=lambda: None,
            contract_client=bind,
        )

    def invoke(self, contract, operation, payload, **kwargs):
        if contract == completion.CONTRACT_ID:
            if self.fail_confirmation:
                raise TimeoutError("lost confirmation")
            return self.confirm(operation, payload, self.invocation())
        return super().invoke(contract, operation, payload, **kwargs)

    def confirm_now(self, invocation):
        return self.confirm(
            completion.OPERATION_ID,
            {"profile_id": "defaults", "operation": "confirm", "turn_id": self.turn},
            invocation,
        )


def test_genuine_calendar_completion_promotes_actual_owner_receipt(tmp_path):
    session = CalendarSession(tmp_path)
    result = _run(session.turns, session)
    assert result["status"] == "completed"
    lifecycle = completion_source(session.conversations.get("conversation-1"))
    assert (
        lifecycle["state"] == "completed"
        and lifecycle["completion_message_id"] is not None
    )
    assert len(session.conversations.get("conversation-1")["messages"]) == 2


@pytest.mark.parametrize(
    "change",
    ["payload", "missing_fingerprint", "wrong_adapter", "wrong_version", "expired"],
)
def test_wrong_calendar_source_never_confirms_append_candidate(tmp_path, change):
    session = CalendarSession(tmp_path)
    session.fail_confirmation = True
    assert _run(session.turns, session)["status"] == "reconciliation_required"
    invocation = session.invocation()
    if change == "payload":
        invocation = session.invocation(
            altered={"payload": {**session.job["payload"], "message": "forged"}}
        )
    elif change == "missing_fingerprint":
        original = invocation.contract_client

        def bind(**kwargs):
            client = original(**kwargs)
            invoke = client.invoke
            client.invoke = lambda *args: {
                key: value
                for key, value in invoke(*args).items()
                if key != "calendar_preparation"
            }
            return client

        invocation.contract_client = bind
    elif change == "wrong_adapter":
        parent = invocation.parent_invocation
        invocation.parent_invocation = replace(
            parent, envelope=replace(parent.envelope, operation_id="other-job-adapter")
        )
    elif change == "wrong_version":
        parent = invocation.parent_invocation
        invocation.parent_invocation = replace(
            parent, envelope=replace(parent.envelope, contract_version="1.0.0")
        )
    else:

        def expired():
            raise PermissionError("scope expired")

        invocation = session.invocation(scope_guard=expired)
    before = session.conversations.path.read_bytes()
    with pytest.raises(PermissionError):
        session.confirm_now(invocation)
    assert session.conversations.path.read_bytes() == before
    assert (
        completion_source(session.conversations.get("conversation-1"))["state"]
        == "running"
    )


def test_normal_saved_caller_confirmation_remains_unchanged(tmp_path):
    from tests.test_conversation_completion_confirmation import _CapturedSession

    session = _CapturedSession(tmp_path)
    session.confirm = (
        completion.ConversationCompletionHostFactoryV4()
        .capture(_context(tmp_path))
        .contributions[0]
        .invoke
    )
    assert _run(session.turns, session)["status"] == "completed"
    assert (
        completion_source(session.conversations.get("conversation-1"))["state"]
        == "completed"
    )
