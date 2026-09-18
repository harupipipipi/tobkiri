"""Canonical identity projection for Host-bound chat approval continuations.

The approval-continuation routes emit finite packets, not a free-floating
legacy stream.  Every packet carries the exact identities the Host bound
into its one-shot handle — ``turn_id``/``operation_id`` equal to the
canonical saved turn (or ``""`` when the operation is not turn-bound), the
bound ``conversation_id``, and the approval ``request_id`` — plus a
``terminal`` receipt that is ``null`` while the continuation is unsettled
and a canonical ``completed``/``failed``/``cancelled`` receipt once it is.
"""

from __future__ import annotations

import re
from typing import Mapping

CHAT_APPROVAL_APPROVE_TARGET = (
    "defaults.chat.approval.approve",
    "tobkiri.action.chat.approval-continuation.v1",
    "chat_approval.approve",
    "rumi_host_authority_bridge_pack.host-authority.chat-approval-continuation",
    "rumi_host_authority_bridge_pack.host-authority.chat-approval-continuation",
)
CHAT_APPROVAL_RESUME_TARGET = (
    "defaults.chat.approval.resume",
    "tobkiri.action.chat.approval-continuation.v1",
    "chat_approval.resume",
    "rumi_host_authority_bridge_pack.host-authority.chat-approval-continuation",
    "rumi_host_authority_bridge_pack.host-authority.chat-approval-continuation",
)
CHAT_CONTINUATION_TARGETS = frozenset(
    {CHAT_APPROVAL_APPROVE_TARGET, CHAT_APPROVAL_RESUME_TARGET}
)

_STABLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_TERMINAL = frozenset({"completed", "failed", "cancelled"})


def normalize_chat_continuation(payload: Mapping[str, object]) -> dict[str, object]:
    """Validate the declared canonical turn binding for one continuation.

    ``turn_id`` is always declared by the presentation owner: it is the exact
    saved turn the continuation is asserted to belong to, or ``""`` when the
    operation is not turn-bound.  The Host binds the declared value into its
    one-shot handle, so a later phase presenting a different turn is
    rejected.
    """

    turn_id = payload.get("turn_id")
    if (
        not isinstance(turn_id, str)
        or len(turn_id) > 256
        or turn_id.strip() != turn_id
        or (turn_id and not _STABLE_ID.fullmatch(turn_id))
    ):
        raise ValueError("chat approval continuation turn identity is invalid")
    return dict(payload)


def present_chat_continuation(result: Mapping[str, object]) -> dict[str, object]:
    """Project a Host-bound continuation result as a canonical turn packet.

    The projection refuses packets whose identities are malformed, whose
    ``operation_id`` does not equal ``turn_id``, or whose terminal receipt
    disagrees with the packet identity or the canonical terminal set.
    """

    identity = _continuation_identity(result)
    terminal = result.get("terminal")
    if terminal is not None:
        if not isinstance(terminal, Mapping):
            raise ValueError("chat approval continuation terminal is invalid")
        terminal_identity = _continuation_identity(terminal)
        status = terminal.get("status")
        if terminal_identity != identity or status not in _TERMINAL:
            raise ValueError("chat approval continuation terminal is invalid")
        terminal = {
            **terminal_identity,
            "status": status,
            "result_reference": terminal.get("result_reference"),
            "error": terminal.get("error"),
        }
    return {**dict(result), **identity, "terminal": terminal}


def _continuation_identity(value: Mapping[str, object]) -> dict[str, str]:
    turn_id = value.get("turn_id")
    conversation_id = value.get("conversation_id")
    operation_id = value.get("operation_id")
    request_id = value.get("request_id")
    if (
        not isinstance(turn_id, str)
        or len(turn_id) > 256
        or turn_id.strip() != turn_id
        or (turn_id and not _STABLE_ID.fullmatch(turn_id))
        or operation_id != turn_id
        or not isinstance(conversation_id, str)
        or not _STABLE_ID.fullmatch(conversation_id)
        or not isinstance(request_id, str)
        or not _STABLE_ID.fullmatch(request_id)
    ):
        raise ValueError("chat approval continuation identity is invalid")
    return {
        "turn_id": turn_id,
        "conversation_id": conversation_id,
        "operation_id": operation_id,
        "request_id": request_id,
    }
