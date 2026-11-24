"""Host-private request admission witness for one overlapping Saved chat source.

A witness changes bounded busy-gate scheduling only. Native approval, normal
Broker authority and all existing resource tickets remain mandatory.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
import time
from typing import Any

from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tobkiri_host.operation_cancellation import _NestedCancellationProof
from tobkiri_protocol.chat_message_v1 import message_arguments
from tobkiri_protocol.canonical import canonical_json
import json
import re

SAVED = ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved")
GUEST = ("conversation.saved-turn.v1", "saved_complete")
BROKER = ("tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke")
EXECUTE = (
    "tobkiri.service.chat.message.send.v1",
    "rumi_default_tools_pack.chat-message-send",
)
EFFECT = ("tobkiri.service.interactive-effect.v1", "interactive_effect.manage")
PATH = [
    (
        "tobkiri.service.tool.local.operation.v1",
        "rumi_default_tools_pack.chat-message-operation",
    ),
    (
        "tobkiri.service.tool.execute.v1",
        "rumi_tool_local_executor_pack.tool-local-execute",
    ),
]
CAPTURE = (
    "profile_id",
    "profile_revision",
    "activation_id",
    "activation_digest",
    "plan_digest",
    "profile_authority_digest",
    "security_epoch",
    "fencing_token",
)


@dataclass(frozen=True)
class ActiveChatCandidate:
    """Inert admission identity; never a receipt, approval or execution grant."""

    source_broker_envelope: Any
    target_conversation_ids: tuple[str, ...]
    delivery: str
    group_snapshot_json: bytes | None = None
    membership_json: bytes | None = None


def _pair(envelope: Any) -> tuple[str, str]:
    return envelope.contract_id, envelope.operation_id


def _owner(invocation: Any) -> tuple[str, str]:
    return (
        invocation.presentation_owner_principal_id,
        invocation.presentation_owner_session_id,
    )


def _active_children(proof: Any, owner: tuple[str, str]) -> list[Any]:
    """Snapshot enrolled, entered requests with actual still-running Futures."""
    if not isinstance(proof, _NestedCancellationProof):
        raise PermissionError("active chat source proof is unavailable")
    # Deliberately private to the existing Host registry; no wire lookup or new
    # registry authority API is introduced by this narrow scheduling helper.
    with proof._registry._lock:
        children = [
            child
            for child in proof._children.values()
            if isinstance(child.future, Future)
            and child.future.running()
            and not child.completed
            and proof.matches_invocation(child.envelope, *owner)
        ]
        return [
            child
            for child in children
            if not child.independent_saved_branch or child.execution_entered
        ]


def _nearest_saved(invocation: Any, proof: Any, owner: tuple[str, str]) -> Any:
    scope = invocation.parent_invocation
    seen: set[int] = set()
    while scope is not None:
        if (
            not isinstance(scope, CapturedInvocationScopeV4)
            or id(scope) in seen
            or len(seen) >= 16
        ):
            raise PermissionError("active chat source ancestry is invalid")
        seen.add(id(scope))
        scope.assert_current()
        if _pair(scope.envelope) == SAVED:
            if not proof.matches_invocation(scope.envelope, *owner):
                raise PermissionError("active Saved source is unavailable")
            return scope.envelope
        scope = scope.parent
    raise PermissionError("active Saved source is unavailable")


def assert_active_chat_candidate(
    invocation: Any, held: Any, held_proof: Any, *, reference_client: Any = None
) -> ActiveChatCandidate:
    """Admit only an actual finite Saved source overlapping an actual live tree.

    Callers retain ``held_proof`` at the original Broker entry using
    nested_cancellation_proof_for with the actual owner. This helper never
    receives caller JSON target lists or a caller-supplied target scope.
    """
    token, candidate, arguments = _candidate_context(invocation)
    if arguments["target_kind"] == "group":
        return read_active_group_candidate(
            invocation, held, held_proof, reference_client=reference_client
        )
    _, active = _active_targets(invocation, held, held_proof, candidate)
    matched = {arguments["target_id"]} & active
    if not matched:
        raise PermissionError("finite candidate has no active target overlap")
    invocation.assert_current()
    held.assert_current()
    return ActiveChatCandidate(candidate, tuple(sorted(matched)), arguments["delivery"])


def _candidate_context(invocation: Any) -> tuple[Any, Any, dict]:
    from tobkiri_host.finite_chat_dispatch import _current

    token = _current.get()
    if token is None or token.root is None:
        raise PermissionError("finite candidate source is unavailable")
    invocation.assert_current()
    token.scope.assert_current()
    candidate = token.root
    if (
        _pair(candidate) != BROKER
        or candidate.payload.get("tool_id") != "chat_send_message"
    ):
        raise PermissionError("finite candidate tool changed")
    arguments = message_arguments(candidate.payload.get("arguments"))
    if candidate.payload.get("tool_call_id") != token.tool_call_id:
        raise PermissionError("finite candidate call changed")
    if not isinstance(token.source_proof, _NestedCancellationProof):
        raise PermissionError("finite candidate proof changed")
    if not token.source_proof.matches_invocation(invocation.envelope, *token.owner):
        raise PermissionError("finite candidate is not registered")
    candidate_children = _active_children(token.source_proof, token.owner)
    entered = {id(child.envelope) for child in candidate_children}
    if (
        not {id(candidate), id(invocation.envelope), id(token.scope.envelope)}
        <= entered
    ):
        raise PermissionError("finite candidate real Future is unavailable")
    from tobkiri_host.finite_chat_dispatch import _assert_saved_source_ancestry

    # Share the finite minting validator: ordinary root Saved or actual selected
    # Calendar JobBroker/adapter ancestry, never a caller-supplied task flag.
    _assert_saved_source_ancestry(token.scope, token.source_proof, token.owner)
    if _owner(invocation) != token.owner:
        raise PermissionError("finite candidate owner changed")
    # Only the exact source Broker or its direct chat effect path can use this
    # request-only slot. Incoming recipient tools cannot recursively claim it.
    if invocation.envelope is not candidate:
        if _pair(invocation.envelope) != EFFECT:
            raise PermissionError("finite candidate coordinator path changed")
        scope = invocation.parent_invocation
        for operation in PATH:
            if (
                not isinstance(scope, CapturedInvocationScopeV4)
                or _pair(scope.envelope) != operation
            ):
                raise PermissionError("finite candidate coordinator ancestry changed")
            scope.assert_current()
            scope = scope.parent
        if (
            not isinstance(scope, CapturedInvocationScopeV4)
            or scope.envelope is not candidate
        ):
            raise PermissionError("finite candidate Broker ancestry changed")
    return token, candidate, arguments


def _held_source_broker(held: Any, proof: Any, owner: tuple[str, str]) -> Any:
    """Normalize the held coordinator to its exact enrolled source Broker."""
    if _pair(held.envelope) == BROKER:
        return held.envelope
    if _pair(held.envelope) != EFFECT:
        raise PermissionError("held candidate source path is unavailable")
    scope = held.parent_invocation
    for operation in PATH:
        if (
            not isinstance(scope, CapturedInvocationScopeV4)
            or _pair(scope.envelope) != operation
        ):
            raise PermissionError("held coordinator source ancestry changed")
        scope.assert_current()
        scope = scope.parent
    if (
        not isinstance(scope, CapturedInvocationScopeV4)
        or _pair(scope.envelope) != BROKER
        or not proof.matches_invocation(scope.envelope, *owner)
    ):
        raise PermissionError("held coordinator source Broker changed")
    scope.assert_current()
    return scope.envelope


def _active_targets(
    invocation: Any, held: Any, held_proof: Any, candidate: Any
) -> tuple[list[Any], set[str]]:
    invocation.assert_current()
    held.assert_current()
    if not all(
        getattr(candidate.context, key) == getattr(held.envelope.context, key)
        for key in CAPTURE
    ):
        raise PermissionError("active chat source capture changed")
    held_owner = _owner(held)
    if not isinstance(
        held_proof, _NestedCancellationProof
    ) or not held_proof.matches_invocation(held.envelope, *held_owner):
        raise PermissionError("held active chat source is unavailable")
    children = _active_children(held_proof, held_owner)
    if not any(child.envelope is held.envelope for child in children):
        raise PermissionError("held active chat Future is unavailable")
    source_broker = _held_source_broker(held, held_proof, held_owner)
    if not any(child.envelope is source_broker for child in children):
        raise PermissionError("held source Broker Future is unavailable")
    active: set[str] = set()
    if source_broker.payload.get("tool_id") == "chat_send_message":
        # Registered independent Saved recipient Futures are the authority for
        # active membership. Prepared payloads alone cannot invent a target.
        for child in children:
            target = child.envelope
            if (
                _pair(target) == SAVED
                and child.independent_saved_branch
                and isinstance(child.execution_proof, _NestedCancellationProof)
                and child.execution_proof.matches_invocation(target, *held_owner)
            ):
                if (
                    target.deadline_monotonic > time.monotonic()
                    and not target.cancellation_requested.is_set()
                ):
                    identifier = target.payload.get("request", {}).get(
                        "conversation_id"
                    )
                    if isinstance(identifier, str) and identifier:
                        active.add(identifier)
    else:
        target = _nearest_saved(held, held_proof, held_owner)
        identifier = target.payload.get("request", {}).get("conversation_id")
        if isinstance(identifier, str) and identifier:
            active.add(identifier)
    return children, active


REFERENCE = "tobkiri.resource.chat.reference.v1"
REFERENCE_OPERATION = "rumi_conversation_store_pack.chat-reference-read"


def _membership(
    snapshot: Any, *, profile: str, group: str, source: str
) -> tuple[bytes, set[str]]:
    now = int(time.time() * 1000)
    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("snapshot_time")) is not int
        or type(snapshot.get("expires_at")) is not int
        or not snapshot["snapshot_time"] <= now < snapshot["expires_at"]
        or snapshot["expires_at"] - snapshot["snapshot_time"] > 600000
    ):
        raise PermissionError("candidate group snapshot expired")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("kind") != "tobkiri.chat.reference.snapshot.v1"
        or snapshot.get("profile_id") != profile
        or snapshot.get("truncated") is not False
        or snapshot.get("next_cursor") is not None
    ):
        raise PermissionError("candidate group reference snapshot is unavailable")
    refs = snapshot.get("references")
    if not isinstance(refs, list) or len(refs) != 1:
        raise PermissionError("candidate group reference identity changed")
    ref = refs[0]
    if (
        not isinstance(ref, dict)
        or (ref.get("kind"), ref.get("id")) != ("group", group)
        or ref.get("membership_complete") is not True
    ):
        raise PermissionError("candidate group membership is incomplete")
    ids = ref.get("conversation_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > 17
        or any(
            not isinstance(identifier, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", identifier) is None
            for identifier in ids
        )
        or ids != sorted(set(ids))
        or type(ref.get("member_count")) is not int
        or ref["member_count"] != len(ids)
    ):
        raise PermissionError("candidate group recipient set is invalid")
    digest = ref.get("snapshot_digest")
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
    ):
        raise PermissionError("candidate group membership digest is invalid")
    effective = set(ids) - {source}
    if not 1 <= len(effective) <= 16:
        raise PermissionError("candidate group exceeds send recipient limit")
    identity = {
        "profile_id": profile,
        "kind": "group",
        "id": group,
        "conversation_ids": ids,
        "snapshot_digest": digest,
        "member_count": len(ids),
        "membership_complete": True,
    }
    return canonical_json(identity), effective


def read_active_group_candidate(
    invocation: Any, held: Any, held_proof: Any, *, reference_client: Any = None
) -> ActiveChatCandidate:
    """Read one canonical group through a fresh signed ToolBroker read edge.

    Central caller reserves its one bounded request-only slot before this read.
    Returned immutable bytes are private admission data, never native authority.
    """
    token, candidate, arguments = _candidate_context(invocation)
    if invocation.envelope is not candidate or arguments["target_kind"] != "group":
        raise PermissionError("candidate group read caller is unavailable")
    _, active = _active_targets(invocation, held, held_proof, candidate)
    parent = token.scope.parent
    source = parent.public_payload()["request"]["conversation_id"]
    profile = candidate.context.profile_id
    client = (
        reference_client
        if reference_client is not None
        else invocation.contract_client(
            allowed_contract_ids=frozenset({REFERENCE}),
            consumer_pack_id="rumi_tool_broker_pack",
            include_credentials=False,
        )
    )
    invocation.assert_current()
    snapshot = client.invoke(
        REFERENCE,
        REFERENCE_OPERATION,
        {
            "profile_id": profile,
            "operation": "resolve",
            "references": [{"kind": "group", "id": arguments["target_id"]}],
        },
    )
    invocation.assert_current()
    held.assert_current()
    from tobkiri_host.finite_chat_dispatch import _assert_saved_source_ancestry

    _assert_saved_source_ancestry(token.scope, token.source_proof, token.owner)
    now = int(time.time() * 1000)
    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("snapshot_time")) is not int
        or type(snapshot.get("expires_at")) is not int
        or not snapshot["snapshot_time"] <= now < snapshot["expires_at"]
        or snapshot["expires_at"] - snapshot["snapshot_time"] > 600000
    ):
        raise PermissionError("candidate group snapshot expired")
    frozen, effective = _membership(
        snapshot, profile=profile, group=arguments["target_id"], source=source
    )
    # The signed owner read may block while an existing recipient completes or
    # the held delivery advances to a different target. Only current enrolled
    # running Futures/proofs may bind the admission witness after that read.
    _, active = _active_targets(invocation, held, held_proof, candidate)
    overlap = effective.intersection(active)
    if not overlap:
        raise PermissionError("finite candidate has no active target overlap")
    return ActiveChatCandidate(
        candidate,
        tuple(sorted(overlap)),
        arguments["delivery"],
        canonical_json(snapshot),
        frozen,
    )


def assert_admission_membership(
    request: Any, snapshot: Any, witness: ActiveChatCandidate
) -> None:
    """Fence normal native prepare/execute to the original resolved membership."""
    from tobkiri_host.finite_chat_dispatch import _current

    token = _current.get()
    if token is None or token.root is not witness.source_broker_envelope:
        raise PermissionError("candidate membership source changed")
    token.scope.assert_current()
    if (
        not token.source_proof.matches_invocation(token.root, *token.owner)
        or witness.membership_json is None
        or witness.group_snapshot_json is None
    ):
        raise PermissionError("candidate membership admission is unavailable")
    arguments = message_arguments(token.root.payload.get("arguments"))
    source_request = token.scope.parent.public_payload()["request"]
    if (
        not isinstance(request, dict)
        or request.get("profile_id") != token.root.context.profile_id
        or request.get("source_conversation_id") != source_request["conversation_id"]
        or request.get("source_turn_id") != source_request["turn_id"]
        or request.get("tool_call_id") != token.tool_call_id
        or any(
            request.get(key) != arguments[key]
            for key in ("target_kind", "target_id", "content", "delivery")
        )
    ):
        raise PermissionError("candidate membership request changed")
    original = json.loads(witness.group_snapshot_json)
    if int(time.time() * 1000) >= original["expires_at"]:
        raise PermissionError("candidate membership admission expired")
    current, _ = _membership(
        snapshot,
        profile=request["profile_id"],
        group=request["target_id"],
        source=request["source_conversation_id"],
    )
    if current != witness.membership_json:
        raise PermissionError(
            "candidate group membership changed; native reapproval required"
        )
    token.scope.assert_current()
