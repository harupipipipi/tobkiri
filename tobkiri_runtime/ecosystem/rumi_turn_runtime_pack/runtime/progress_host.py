"""Public progress owner validates only Broker-preserved execution provenance."""

from __future__ import annotations

from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    HostProviderCaptureContextV4,
    HostProviderInvocationContextV4,
)
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from ecosystem.rumi_turn_runtime_pack.runtime.progress import TurnProgressJournal
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import SAVED_CONVERSATION_CONTRACT
from tobkiri_protocol.turn_progress_v1 import (
    AI_STREAM,
    MAX_EVENTS,
    PROVIDER_STREAM,
    payload_digest,
    validate_begin,
)

CONVERSATION = "tobkiri.resource.conversation.v1"
CONVERSATION_OPERATION = "rumi_conversation_store_pack.conversation-resource"


def progress_operation(
    context: HostProviderCaptureContextV4,
    store: DurableTurnRuntime,
    *,
    resource: bool,
    payload: Mapping[str, Any],
    invocation: HostProviderInvocationContextV4,
) -> Mapping[str, Any]:
    """Publish under exact captured producers; expose only authenticated owner reads."""
    invocation.assert_current()
    values = {key: value for key, value in payload.items() if key != "_session_id"}
    owner = canonical_digest(
        {
            "principal": invocation.presentation_owner_principal_id,
            "session": invocation.presentation_owner_session_id,
        }
    )
    capture = canonical_digest(
        {
            "profile_id": context.profile_id,
            "plan_digest": context.plan_digest,
            "security_epoch": context.security_epoch,
        }
    )
    store._check_path()
    journal = TurnProgressJournal(store.path.with_name("progress.sqlite3"))
    client = invocation.contract_client(
        allowed_contract_ids=frozenset({CONVERSATION}),
        consumer_pack_id="rumi_turn_runtime_pack",
        include_credentials=False,
    )

    def branch(binding: Mapping[str, Any]) -> None:
        value = client.invoke(
            CONVERSATION,
            CONVERSATION_OPERATION,
            {
                "profile_id": context.profile_id,
                "operation": "get",
                "conversation_id": binding["conversation_id"],
            },
        )
        conversation = value.get("conversation", value)
        if not isinstance(conversation, Mapping) or (
            conversation.get("conversation_revision") != binding["conversation_revision"]
            or conversation.get("current_node_id") != binding["parent_id"]
        ):
            raise PermissionError("live progress selected branch changed")
        invocation.assert_current()

    if resource:
        if set(values) - {"progress_id"} != {"turn_id", "conversation_id", "cursor"}:
            raise ValueError("progress read fields are invalid")
        identity = journal.find(values["turn_id"], owner=owner, capture=capture)
        cursor = values["cursor"]
        if type(cursor) is not int or not 0 <= cursor <= MAX_EVENTS:
            raise ValueError("progress cursor is invalid")
        if "progress_id" in values:
            if not isinstance(values["progress_id"], str):
                raise ValueError("progress identity is invalid")
            if values["progress_id"] != identity:
                cursor = 0
        page = journal.read(identity, owner=owner, capture=capture, cursor=cursor)
        if page["binding"]["conversation_id"] != values["conversation_id"]:
            raise PermissionError("progress conversation does not match")
        branch(page["binding"])
        turn = store.get(values["turn_id"])
        page["canonical_turn_status"] = turn.get("status") if turn else None
        return page

    parent = invocation.parent_invocation
    if not isinstance(parent, CapturedInvocationScopeV4):
        raise PermissionError("progress requires captured parent execution")
    parent.assert_current()
    envelope = parent.envelope
    if (
        envelope.context.profile_id != context.profile_id
        or envelope.context.plan_digest != context.plan_digest
        or envelope.context.security_epoch != context.security_epoch
        or envelope.contract_version != "1.0.0"
        or envelope.target_principal != invocation.envelope.context.caller_principal
    ):
        raise PermissionError("progress parent capture does not match")
    action = values.pop("phase", None)
    parent_payload = parent.public_payload()
    if action == "ready":
        if values or envelope.contract_id != SAVED_CONVERSATION_CONTRACT:
            raise PermissionError("progress readiness requires captured saved execution")
        request = parent_payload.get("request")
        turn = store.get(request.get("turn_id", "")) if isinstance(request, Mapping) else None
        return {
            "ready": bool(
                turn
                and turn.get("status") == "running"
                and turn.get("input_digest") == canonical_digest(parent_payload)
            )
        }
    if action == "begin":
        if envelope.contract_id != SAVED_CONVERSATION_CONTRACT:
            raise PermissionError("progress begin requires saved execution")
        binding = validate_begin(values)
        initial = parent_payload.get("request")
        turn = store.get(binding["turn_id"])
        if (
            not isinstance(initial, Mapping)
            or not turn
            or (
                turn.get("status") != "running"
                or turn.get("input_digest") != binding["input_digest"]
                or canonical_digest(parent_payload) != binding["input_digest"]
                or initial.get("turn_id") != binding["turn_id"]
                or initial.get("conversation_id") != binding["conversation_id"]
                or envelope.context.request_id != binding["request_id"]
            )
        ):
            raise PermissionError("progress saved input does not match")
        branch(binding)
        return {"progress_id": journal.begin(binding, owner=owner, capture=capture)}
    if action in {"tool_bind", "tool_publish"}:
        if (
            envelope.contract_id != "tobkiri.service.tool.invoke.v1"
            or envelope.operation_id != "rumi_tool_broker_pack.tool-invoke"
        ):
            raise PermissionError("tool progress requires captured tool broker")
        matches = []
        for binding in context.catalog_bindings:
            if (
                binding.operation.contract_id == envelope.contract_id
                and binding.operation.operation_id == envelope.operation_id
                and binding.operation.contract_version == "1.0.0"
                and binding.principal_ref == envelope.target_principal
                and binding.function.function_id == "rumi_tool_broker_pack.tool-broker.invoke"
                and binding not in matches
            ):
                # Several captured caller edges may pin the same full binding.
                matches.append(binding)
        if len(matches) != 1:
            raise PermissionError("tool progress producer is not captured")
        expected = {"progress_id"} if action == "tool_bind" else {"progress_id", "cursor", "event"}
        if set(values) != expected or parent_payload.get("progress_id") != values["progress_id"]:
            raise PermissionError("tool progress request changed")
        original = dict(parent_payload)
        original.pop("progress_id")
        digest = payload_digest(original)
        producer = envelope.target_principal.value
        execution = canonical_digest(
            {
                "request_id": envelope.context.request_id,
                "request_digest": envelope.request_digest,
                "principal": producer,
                "domain": envelope.target_domain.value,
            }
        )
        page = journal.read(values["progress_id"], owner=owner, capture=capture, cursor=0)
        branch(page["binding"])
        if action == "tool_bind":
            journal.bind(
                values["progress_id"],
                owner=owner,
                capture=capture,
                ai_input_digest=digest,
                producer=producer,
                producer_input_digest=digest,
            )
            journal.claim(
                values["progress_id"],
                owner=owner,
                capture=capture,
                producer=producer,
                producer_input_digest=digest,
                execution=execution,
            )
            return {"bound": True}
        event = values["event"]
        if not isinstance(event, Mapping) or any(
            event.get(key) != original.get(key) for key in ("tool_id", "tool_call_id")
        ):
            raise PermissionError("tool progress identity changed")
        if values["cursor"] == 1:
            if dict(event) != {
                "type": "tool_started",
                **{key: original[key] for key in ("tool_id", "tool_call_id", "arguments")},
            }:
                raise PermissionError("tool progress arguments changed")
        elif values["cursor"] != 2 or event.get("type") != "tool_completed":
            raise PermissionError("tool progress lifecycle is invalid")
        journal.publish(
            values["progress_id"],
            owner=owner,
            capture=capture,
            producer=producer,
            producer_input_digest=digest,
            cursor=values["cursor"],
            event=event,
            execution=execution,
        )
        return {"cursor": values["cursor"]}
    if action == "bind":
        if set(values) != {"progress_id", "producer", "producer_input_digest"} or (
            envelope.contract_id != AI_STREAM[0] or envelope.operation_id != AI_STREAM[1]
        ):
            raise PermissionError("progress bind requires captured AI stream")
        matches = [
            binding
            for binding in context.catalog_bindings
            if binding.operation.contract_id == PROVIDER_STREAM
            and binding.operation.contract_version == "1.0.0"
            and binding.principal_ref.value == values["producer"]
        ]
        if len(matches) != 1:
            raise PermissionError("progress producer is not captured")
        ai_input = dict(parent_payload)
        progress_id = ai_input.pop("progress_id", None)
        if progress_id != values["progress_id"]:
            raise PermissionError("progress AI request changed")
        journal.bind(
            values["progress_id"],
            owner=owner,
            capture=capture,
            ai_input_digest=payload_digest(ai_input),
            producer=values["producer"],
            producer_input_digest=values["producer_input_digest"],
        )
        return {"bound": True}
    if action in {"publish", "claim"}:
        expected = {"progress_id", "cursor", "event"} if action == "publish" else {"progress_id"}
        if set(values) != expected or (envelope.contract_id != PROVIDER_STREAM):
            raise PermissionError("progress publish requires captured stream producer")
        page = journal.read(values["progress_id"], owner=owner, capture=capture, cursor=0)
        branch(page["binding"])
        execution = canonical_digest(
            {
                "request_id": envelope.context.request_id,
                "request_digest": envelope.request_digest,
                "principal": envelope.target_principal.value,
                "domain": envelope.target_domain.value,
            }
        )
        if action == "claim":
            journal.claim(
                values["progress_id"],
                owner=owner,
                capture=capture,
                producer=envelope.target_principal.value,
                producer_input_digest=payload_digest(parent_payload),
                execution=execution,
            )
            return {"claimed": True}
        if not isinstance(values["event"], Mapping) or values["event"].get("type") not in {
            "text_delta",
            "thinking_delta",
            "finish",
        }:
            raise PermissionError("AI producer cannot publish tool progress")
        journal.publish(
            values["progress_id"],
            owner=owner,
            capture=capture,
            producer=envelope.target_principal.value,
            producer_input_digest=payload_digest(parent_payload),
            cursor=values["cursor"],
            event=values["event"],
            execution=execution,
        )
        return {"cursor": values["cursor"]}
    raise ValueError("progress phase is invalid")
