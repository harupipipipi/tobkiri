"""Owner-atomic message admission; receipts never confer execution authority."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict, TurnRuntime
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input

DELIVERY_CONTRACT = "tobkiri.action.chat.message.delivery.v1"
DELIVERY_OPERATION = "rumi_turn_runtime_pack.chat-message-deliver"
SAVED_CONTRACT = "tobkiri.action.turn.saved.v1"
SAVED_OPERATION = "rumi_turn_runtime_pack.turn-saved"
RESOURCE_CONTRACT = "tobkiri.resource.conversation.v1"
RESOURCE_OPERATION = "rumi_conversation_store_pack.conversation-resource"
DELIVERY_CONTRACTS = frozenset({SAVED_CONTRACT, RESOURCE_CONTRACT})
MAX_RECIPIENTS = 16
MAX_TEXT_BYTES = 20 * 1024
ACTIVE = frozenset({"queued", "running", "waiting"})


def _copy(value: Any) -> Any:
    return strict_loads(canonical_json(value))


def _message_id(conversation: str, turn: str, role: str) -> str:
    return "message:" + canonical_digest([conversation, turn, role])[7:]


def _rows(store: Any, connection: Any, conversation: str) -> list[dict[str, Any]]:
    return [
        store._record(row)
        for row in connection.execute(
            "SELECT id, request_id, body FROM turns ORDER BY updated_at, id"
        )
        if store._record(row)["conversation_id"] == conversation
    ]


def assert_conversation_idle(
    store: Any, connection: Any, conversation: str, turn_id: str
) -> None:
    """Fence normal UI starts against another active owner turn."""
    if any(
        record["id"] != turn_id and record["status"] in ACTIVE
        for record in _rows(store, connection, conversation)
    ):
        raise TurnConflict("conversation already has an active turn")


TARGET_SETTING_FIELDS = frozenset({"tool_selection"})


def latest_target_settings(
    store: Any, connection: Any, conversation: str
) -> dict[str, Any]:
    """Use only same-conversation ordinary canonical completed settings."""
    records = sorted(
        _rows(store, connection, conversation),
        key=lambda item: (
            (
                item.get("result_reference", {}).get("conversation_revision", -1)
                if isinstance(item.get("result_reference"), Mapping)
                else -1
            ),
            item["id"],
        ),
        reverse=True,
    )
    for record in records:
        if (
            not record["request_id"].startswith("saved-turn.")
            or record["status"] != "completed"
            or turn_has_delivery_provenance(store, connection, record)
        ):
            continue
        reference = record.get("result_reference")
        if not isinstance(reference, Mapping) or (
            reference.get("conversation_id") != conversation
            or reference.get("user_message_id")
            != _message_id(conversation, record["id"], "user")
            or reference.get("assistant_message_id")
            != _message_id(conversation, record["id"], "assistant")
        ):
            continue
        row = connection.execute(
            "SELECT accepted_digest, context FROM saved_inputs WHERE id=?",
            (record["id"],),
        ).fetchone()
        if row is None or row[0] != record.get("input_digest"):
            return {}
        context = json.loads(row[1])
        if (
            not isinstance(context, dict)
            or context.get("kind") != "tobkiri.saved.input-projection.v1"
        ):
            return {}
        settings = context.get("target_settings", {})
        if not isinstance(settings, dict) or not set(settings) <= TARGET_SETTING_FIELDS:
            raise ValueError("saved target settings are invalid")
        validated = validate_saved_conversation_input(
            {
                "request": {
                    "turn_id": record["id"],
                    "conversation_id": conversation,
                    "conversation_revision": reference["conversation_revision"],
                    "content": "Target settings validation",
                    **settings,
                }
            }
        )
        selection = validated["request"].get("tool_selection")
        if (
            isinstance(selection, dict)
            and selection.get("scope", "turn") == "conversation"
        ):
            return {"tool_selection": selection}
        return {}
    return {}


def assert_delivery_drain_capacity(
    store: Any, connection: Any, current: Mapping[str, Any]
) -> None:
    """Reject deliveries the existing bounded guidance drain cannot consume."""
    depth = 0
    ancestor = current
    while ancestor.get("guidance_parent_turn_id"):
        depth += 1
        if depth >= 8:
            raise TurnConflict("delivery guidance depth is exhausted")
        row = connection.execute(
            "SELECT id, request_id, body FROM turns WHERE id=?",
            (ancestor["guidance_parent_turn_id"],),
        ).fetchone()
        if row is None:
            raise TurnConflict("delivery guidance ancestry is unavailable")
        ancestor = store._record(row)
    queued = sum(
        1
        for item in current.get("guidance", [])
        if item.get("status") == "queued"
        and item.get("value", {}).get("auto_send") is True
    )
    if queued >= 8 - depth:
        raise TurnConflict("delivery guidance drain capacity is exhausted")


class InterruptUnsupported(PermissionError):
    """A chat sender has no authenticated target execution stop ownership."""


def target_delivery_state(
    store: Any,
    connection: Any,
    conversation: str,
    conversation_revision: int | None = None,
) -> dict[str, Any]:
    """Read exact active owner state; a finite list page cannot establish idle."""
    active = [
        record
        for record in _rows(store, connection, conversation)
        if record["status"] in ACTIVE
    ]
    if len(active) > 1:
        raise TurnConflict("conversation has multiple active turns")
    record = active[0] if active else None
    return {
        "conversation_id": conversation,
        "conversation_revision": conversation_revision,
        "turn_id": record["id"] if record else None,
        "request_id": record["request_id"] if record else None,
        "revision": record["revision"] if record else None,
        "status": record["status"] if record else "idle",
    }


def admit_delivery(store: Any, delivery: Mapping[str, Any]) -> dict[str, Any]:
    """Reserve idle input or append active guidance under one owner write lock.

    Only a Host-authenticated helper constructs this finite delivery mapping.
    This method provides scheduling and provenance, never approval or dispatch.
    """
    fields = {
        "delivery_id",
        "source",
        "target_conversation_id",
        "conversation_revision",
        "content",
        "recipient_snapshot",
    }
    if set(delivery) - {"delivery", "target_state"} != fields:
        raise ValueError("delivery fields are invalid")
    delivery_mode = delivery.get("delivery", "steer")
    if delivery_mode not in {"steer", "interrupt"}:
        raise ValueError("message delivery mode is invalid")
    if delivery_mode == "interrupt":
        state = delivery.get("target_state")
        if (
            not isinstance(state, Mapping)
            or set(state)
            != {
                "conversation_id",
                "conversation_revision",
                "turn_id",
                "request_id",
                "revision",
                "status",
            }
            or state["conversation_id"] != delivery["target_conversation_id"]
        ):
            raise ValueError("interrupt requires an exact prepared target state")
        if state["status"] == "idle" and any(
            state[key] is not None for key in ("turn_id", "request_id", "revision")
        ):
            raise ValueError("idle interrupt target state is invalid")
    elif "target_state" in delivery:
        raise ValueError("steer does not require a prepared target state")
    source = delivery["source"]
    if (
        not isinstance(source, Mapping)
        or set(source) != {"profile_id", "conversation_id", "turn_id", "tool_call_id"}
        or source["profile_id"] != store.profile_id
    ):
        raise PermissionError("delivery source does not match owner")
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import _ID

    for value in [
        delivery["delivery_id"],
        delivery["target_conversation_id"],
        source["profile_id"],
        source["conversation_id"],
        source["turn_id"],
    ]:
        if not isinstance(value, str) or _ID.fullmatch(value) is None:
            raise ValueError("delivery identity is invalid")
    tool_call_id = source["tool_call_id"]
    if not isinstance(tool_call_id, str) or not tool_call_id or len(tool_call_id) > 256:
        raise ValueError("delivery tool call identity is invalid")
    target = delivery["target_conversation_id"]
    if target == source["conversation_id"]:
        raise PermissionError("message delivery cannot target its source")
    content = delivery["content"]
    snapshot = delivery["recipient_snapshot"]
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content.encode("utf-8")) > MAX_TEXT_BYTES
        or not isinstance(snapshot, list)
        or not 1 <= len(snapshot) <= MAX_RECIPIENTS
        or any(not isinstance(item, str) for item in snapshot)
        or len(set(snapshot)) != len(snapshot)
        or target not in snapshot
        or source["conversation_id"] in snapshot
        or any(
            not isinstance(item, str) or _ID.fullmatch(item) is None
            for item in snapshot
        )
        or type(delivery["conversation_revision"]) is not int
        or delivery["conversation_revision"] < 1
    ):
        raise ValueError("delivery content or recipient snapshot is invalid")
    fingerprint = canonical_digest(
        {
            key: value
            for key, value in delivery.items()
            if key != "conversation_revision"
        }
    )
    delivery_id = delivery["delivery_id"]
    identity = canonical_digest([store.profile_id, delivery_id, target])[7:]
    own_turn = "delivery:" + identity
    guidance_id = "delivery-guidance:" + identity
    connection = store._connect_write()
    try:
        connection.execute("BEGIN IMMEDIATE")
        known = connection.execute(
            "SELECT fingerprint, body FROM message_deliveries WHERE id=?",
            (delivery_id,),
        ).fetchone()
        if known is not None:
            if known[0] != fingerprint:
                raise TurnConflict("delivery identity was rebound")
            return _copy(json.loads(known[1]))
        if (
            connection.execute("SELECT COUNT(*) FROM message_deliveries").fetchone()[0]
            >= store.max_turns
        ):
            raise TurnConflict("delivery capacity is exhausted")
        source_deliveries = {
            identifier
            for identifier, body in connection.execute(
                "SELECT id,body FROM message_deliveries"
            )
            if json.loads(body)["provenance"]["source"]["turn_id"] == source["turn_id"]
        }
        source_deliveries.update(
            identifier
            for identifier, body in connection.execute(
                "SELECT id,body FROM message_interrupts"
            )
            if json.loads(body).get("source", {}).get("turn_id") == source["turn_id"]
        )
        if len(source_deliveries - {delivery_id}) >= MAX_RECIPIENTS:
            raise TurnConflict("source turn delivery budget is exhausted")
        origin = connection.execute(
            "SELECT id, request_id, body FROM turns WHERE id=?", (source["turn_id"],)
        ).fetchone()
        if origin is None:
            raise PermissionError("delivery source turn is unavailable")
        origin_record = store._record(origin)
        if (
            origin_record["conversation_id"] != source["conversation_id"]
            or origin_record["status"] != "running"
        ):
            raise PermissionError("delivery source is not the active AI turn")
        if turn_has_delivery_provenance(store, connection, origin_record):
            raise PermissionError("incoming message turns cannot cascade delivery")
        active = [
            record
            for record in _rows(store, connection, target)
            if record["status"] in ACTIVE
        ]
        if len(active) > 1:
            raise TurnConflict("conversation has multiple active turns")
        if delivery_mode == "interrupt":
            state = delivery["target_state"]
            if state["status"] == "idle":
                if target_delivery_state(
                    store, connection, target, delivery["conversation_revision"]
                ) != dict(state):
                    raise TurnConflict("prepared interrupt target changed")
            else:
                attempt = connection.execute(
                    "SELECT body FROM message_interrupts WHERE id=?", (delivery_id,)
                ).fetchone()
                if (
                    attempt is None
                    or json.loads(attempt[0]).get("status") != "stopped"
                    or json.loads(attempt[0]).get("fingerprint") != fingerprint
                ):
                    raise InterruptUnsupported(
                        "interrupt requires authenticated verified target stop"
                    )
                if active:
                    raise TurnConflict("interrupt target acquired a new active run")
        provenance = {
            "kind": "chat_message_delivery",
            "delivery_id": delivery_id,
            "source": _copy(dict(source)),
            "loop_depth": 1,
        }
        if active:
            current = active[0]
            if current["status"] == "queued" and current.get("delivery_provenance"):
                raise TurnConflict(
                    "delivery reservation cannot receive guidance before claim"
                )
            assert_delivery_drain_capacity(store, connection, current)
            runtime = TurnRuntime()
            runtime._turns[current["id"]] = current
            runtime._request_ids[current["request_id"]] = current["id"]
            guidance = {
                "prompt": content,
                "target_type": "conversation",
                "target_id": target,
                "conversation_id": target,
                "visible": True,
                "auto_send": True,
                "metadata": {"delivery_provenance": provenance},
            }
            record = runtime.steer(
                current["id"],
                guidance,
                expected_revision=current["revision"],
                guidance_id=guidance_id,
            )
            store._save(connection, record)
            parent_id = current["id"]
            target_turn = (
                "steer:"
                + canonical_digest(
                    {"parent_turn_id": parent_id, "guidance_id": guidance_id}
                )[7:]
            )
            mode = "queued"
            initial = None
        else:
            initial = validate_saved_conversation_input(
                {
                    "request": {
                        "turn_id": own_turn,
                        "conversation_id": target,
                        "conversation_revision": delivery["conversation_revision"],
                        "content": content,
                        **latest_target_settings(store, connection, target),
                    }
                }
            )
            record = TurnRuntime().begin(
                {**store._saved_begin_payload(initial), "profile_id": store.profile_id}
            )
            record["input_digest"] = canonical_digest(initial)
            record["delivery_provenance"] = provenance
            if (
                connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
                >= store.max_turns
            ):
                raise TurnConflict("durable turn capacity is exhausted")
            store._save(connection, record)
            parent_id = None
            target_turn = own_turn
            mode = "reserved"
        receipt = {
            "delivery": delivery_mode,
            "interrupted_turn_id": (
                delivery["target_state"]["turn_id"]
                if delivery_mode == "interrupt"
                else None
            ),
            "interrupt_status": (
                (
                    "stopped_confirmed"
                    if delivery["target_state"]["status"] != "idle"
                    else "idle_noop"
                )
                if delivery_mode == "interrupt"
                else None
            ),
            "delivery_id": delivery_id,
            "profile_id": store.profile_id,
            "target_conversation_id": target,
            "target_turn_id": target_turn,
            "user_message_id": _message_id(target, target_turn, "user"),
            "assistant_message_id": _message_id(target, target_turn, "assistant"),
            "guidance_id": guidance_id if active else None,
            "parent_turn_id": parent_id,
            "status": mode,
            "initial": initial,
            "provenance": provenance,
        }
        connection.execute(
            "INSERT INTO message_deliveries(id, fingerprint, body) VALUES (?,?,?)",
            (delivery_id, fingerprint, canonical_json(receipt).decode()),
        )
        connection.commit()
        return _copy(receipt)
    finally:
        connection.close()


def turn_has_delivery_provenance(
    store: Any, connection: Any, turn: Mapping[str, Any]
) -> bool:
    """Fence message-origin turns and their bounded owner followup ancestry."""
    delivered_ids = {
        json.loads(body)["target_turn_id"]
        for (body,) in connection.execute("SELECT body FROM message_deliveries")
    }
    pending = [dict(turn)]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        identity = current["id"]
        if identity in visited:
            continue
        if len(visited) >= 32:
            raise PermissionError("delivery provenance ancestry exceeds budget")
        visited.add(identity)
        if current.get("delivery_provenance") or identity in delivered_ids:
            return True
        parent_id = current.get("guidance_parent_turn_id")
        for ancestor_id in {parent_id, current.get("guidance_source_turn_id")} - {None}:
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id=?", (ancestor_id,)
            ).fetchone()
            if row is None:
                raise PermissionError("delivery provenance ancestor is unavailable")
            ancestor = store._record(row)
            if ancestor["conversation_id"] != turn["conversation_id"]:
                raise PermissionError("delivery provenance conversation changed")
            if ancestor_id == parent_id and any(
                item.get("id") == current.get("guidance_id")
                and item.get("value", {}).get("metadata", {}).get("delivery_provenance")
                for item in ancestor.get("guidance", [])
            ):
                return True
            pending.append(ancestor)
    return False


def public_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Return accepted identities without treating scheduled messages as saved."""
    return {
        "delivery": "steer",
        "interrupted_turn_id": None,
        "interrupt_status": None,
        **{
            key: _copy(value)
            for key, value in receipt.items()
            if key not in {"initial", "provenance"}
        },
        "user_message_saved": False,
        "assistant_message_saved": False,
    }


def matches_interrupt_state(
    current: Mapping[str, Any], prepared: Mapping[str, Any]
) -> bool:
    """Fence immutable run identity while allowing monotonic progress revisions."""
    fields = {
        "conversation_id",
        "conversation_revision",
        "turn_id",
        "request_id",
        "status",
    }
    return all(current.get(key) == prepared.get(key) for key in fields) and (
        type(current.get("revision")) is int
        and type(prepared.get("revision")) is int
        and current["revision"] >= prepared["revision"]
    )


def begin_interrupt(store: Any, delivery: Mapping[str, Any]) -> dict[str, Any]:
    """Reserve a single exact stop attempt; retries never signal twice."""
    fingerprint = canonical_digest(
        {
            key: value
            for key, value in delivery.items()
            if key != "conversation_revision"
        }
    )
    connection = store._connect_write()
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT body FROM message_interrupts WHERE id=?", (delivery["delivery_id"],)
        ).fetchone()
        if prior:
            result = json.loads(prior[0])
            if result["fingerprint"] != fingerprint:
                raise TurnConflict("interrupt attempt identity was rebound")
            return {**result, "claimed": False}
        state = dict(delivery["target_state"])
        if not matches_interrupt_state(
            target_delivery_state(
                store,
                connection,
                delivery["target_conversation_id"],
                state["conversation_revision"],
            ),
            state,
        ):
            raise TurnConflict("prepared interrupt target changed")
        row = connection.execute(
            "SELECT id,request_id,body FROM turns WHERE id=?", (state["turn_id"],)
        ).fetchone()
        record = store._record(row)
        if any(item.get("status") == "queued" for item in record.get("guidance", [])):
            raise TurnConflict(
                "interrupt target has separately accepted queued guidance"
            )
        if delivery["target_conversation_id"] == delivery["source"]["conversation_id"]:
            raise PermissionError("interrupt cannot target its source")
        if (
            connection.execute("SELECT COUNT(*) FROM message_interrupts").fetchone()[0]
            >= store.max_turns
        ):
            raise TurnConflict("interrupt owner capacity is exhausted")
        source = store.get(delivery["source"]["turn_id"])
        if (
            source is None
            or source["status"] != "running"
            or source["conversation_id"] != delivery["source"]["conversation_id"]
        ):
            raise PermissionError("interrupt source is not executing")
        accepted_ids = {
            identity
            for identity, body in connection.execute(
                "SELECT id,body FROM message_deliveries"
            )
            if json.loads(body).get("provenance", {}).get("source", {}).get("turn_id")
            == source["id"]
        }
        accepted_ids.update(
            identity
            for identity, body in connection.execute(
                "SELECT id,body FROM message_interrupts"
            )
            if json.loads(body).get("source", {}).get("turn_id") == source["id"]
        )
        if len(accepted_ids) >= MAX_RECIPIENTS:
            raise TurnConflict("source message delivery budget is exhausted")
        if turn_has_delivery_provenance(store, connection, source):
            raise PermissionError("received message cannot cascade interrupt")
        result = {
            "fingerprint": fingerprint,
            "state": state,
            "status": "reserved",
            "source": dict(delivery["source"]),
        }
        connection.execute(
            "INSERT INTO message_interrupts VALUES (?,?)",
            (delivery["delivery_id"], canonical_json(result).decode()),
        )
        connection.commit()
        return {**result, "claimed": True}
    finally:
        connection.close()


def request_interrupt(store: Any, delivery: Mapping[str, Any]) -> None:
    """Version-fence one canonical target immediately before Host signaling."""
    state = delivery["target_state"]
    connection = store._connect_write()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if not matches_interrupt_state(
            target_delivery_state(
                store,
                connection,
                state["conversation_id"],
                state["conversation_revision"],
            ),
            state,
        ):
            raise TurnConflict("prepared interrupt target changed before signal")
        row = connection.execute(
            "SELECT id,request_id,body FROM turns WHERE id=?", (state["turn_id"],)
        ).fetchone()
        record = store._record(row)
        if not record.get("input_digest") or not record["request_id"].startswith(
            "saved-turn."
        ):
            raise PermissionError("interrupt requires canonical saved target")
        if any(item.get("status") == "queued" for item in record.get("guidance", [])):
            raise TurnConflict("interrupt target guidance changed before signal")
        source_row = connection.execute(
            "SELECT id,request_id,body FROM turns WHERE id=?",
            (delivery["source"]["turn_id"],),
        ).fetchone()
        if source_row is None:
            raise PermissionError("interrupt source is unavailable before signal")
        source = store._record(source_row)
        if (
            source["status"] != "running"
            or source["conversation_id"] != delivery["source"]["conversation_id"]
            or turn_has_delivery_provenance(store, connection, source)
        ):
            raise PermissionError("interrupt source authority changed before signal")
        updated = store._restore(row).request_cancellation(record["id"])
        store._save(connection, updated)
        connection.commit()
    finally:
        connection.close()


def confirm_interrupt(store: Any, delivery: Mapping[str, Any]) -> dict[str, Any]:
    """Retain an exact verified stop acknowledgement before admitting message."""
    confirmed = store.confirm_saved_cancellation(delivery["target_state"]["turn_id"])
    if confirmed["status"] != "cancelled":
        raise TurnConflict("interrupt target stop is unconfirmed")
    connection = store._connect_write()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT body FROM message_interrupts WHERE id=?", (delivery["delivery_id"],)
        ).fetchone()
        if row is None:
            raise TurnConflict("interrupt attempt is unavailable")
        record = json.loads(row[0])
        record["status"] = "stopped"
        record["stopped_turn_id"] = confirmed["id"]
        connection.execute(
            "UPDATE message_interrupts SET body=? WHERE id=?",
            (canonical_json(record).decode(), delivery["delivery_id"]),
        )
        connection.commit()
        return record
    finally:
        connection.close()


def dispatch_delivery(
    store: Any,
    delivery: Mapping[str, Any],
    *,
    client: Any,
    guard: Callable[[], None],
    interrupt: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Use a live captured client synchronously; never detach target execution."""
    guard()
    if (
        delivery.get("delivery") == "interrupt"
        and delivery["target_state"]["status"] != "idle"
        and store.delivery_receipt(delivery["delivery_id"]) is None
    ):
        if interrupt is None:
            raise InterruptUnsupported(
                "authenticated target interrupt port is unavailable"
            )
        attempt = begin_interrupt(store, delivery)
        if attempt["status"] != "stopped":
            if not attempt["claimed"]:
                raise TurnConflict("interrupt outcome requires reconciliation")
            interrupt(delivery)
            guard()
            confirm_interrupt(store, delivery)
    if store.delivery_receipt(delivery["delivery_id"]) is not None:
        # Replays first validate the original delivery identity and retain its
        # receipt even when the target was deleted after acceptance.
        receipt = store.admit_delivery(delivery)
    else:
        response = client.invoke(
            RESOURCE_CONTRACT,
            RESOURCE_OPERATION,
            {
                "profile_id": store.profile_id,
                "operation": "get",
                "conversation_id": delivery["target_conversation_id"],
            },
        )
        conversation = (
            response.get("conversation") if isinstance(response, Mapping) else None
        )
        if (
            not isinstance(conversation, Mapping)
            or conversation.get("id") != delivery["target_conversation_id"]
        ):
            raise PermissionError("delivery target conversation is unavailable")
        # Approval binds recipients/content. Revision is scheduling state and
        # comes from the authoritative target owner, never caller preferences.
        normalized = {
            **dict(delivery),
            "conversation_revision": conversation.get("conversation_revision"),
        }
        guard()
        receipt = store.admit_delivery(normalized)
    result = public_receipt(receipt)
    if receipt["status"] in {"queued", "cancelled"}:
        child = store.get(receipt["target_turn_id"])
        if child is None and receipt.get("guidance_id"):
            parent = store.get(receipt["parent_turn_id"])
            item = next(
                (
                    entry
                    for entry in (parent or {}).get("guidance", [])
                    if entry.get("id") == receipt["guidance_id"]
                ),
                None,
            )
            if item is not None and item.get("status") == "cancelled":
                result["status"] = "cancelled"
        if child is not None:
            result["status"] = child["status"]
            reference = child.get("result_reference")
            if isinstance(reference, Mapping):
                result["user_message_saved"] = (
                    reference.get("user_message_id") == result["user_message_id"]
                )
                result["assistant_message_saved"] = (
                    reference.get("assistant_message_id")
                    == result["assistant_message_id"]
                )
        return result
    # A replay asks the canonical saved owner to reconcile; it does not claim
    # ownership of a previously running invocation or invent cancellation.
    try:
        guard()
        outcome = client.invoke(SAVED_CONTRACT, SAVED_OPERATION, receipt["initial"])
        guard()
    except Exception:
        # The owner can prove queued work never won the execution claim.
        # Release only that exact reservation; running work remains ambiguous.
        cancelled = store.cancel_delivery(delivery["delivery_id"])
        result["status"] = (
            "cancelled"
            if cancelled["status"] == "cancelled"
            else "reconciliation_required"
        )
        return result
    turn = outcome.get("turn")
    if not isinstance(turn, Mapping) or turn.get("id") != receipt["target_turn_id"]:
        result["status"] = "reconciliation_required"
        return result
    result["status"] = turn.get("status", "reconciliation_required")
    reference = turn.get("result_reference")
    if isinstance(reference, Mapping):
        result["user_message_saved"] = (
            reference.get("user_message_id") == result["user_message_id"]
        )
        result["assistant_message_saved"] = (
            reference.get("assistant_message_id") == result["assistant_message_id"]
        )
    return result


def cancel_delivery(store: Any, delivery_id: str) -> dict[str, Any]:
    """Cancel only work proven unstarted; active effects need canonical stop."""
    connection = store._connect_write()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT fingerprint, body FROM message_deliveries WHERE id=?",
            (delivery_id,),
        ).fetchone()
        if row is None:
            raise KeyError("delivery is unknown")
        receipt = json.loads(row[1])
        target = connection.execute(
            "SELECT id,request_id,body FROM turns WHERE id=?",
            (receipt["target_turn_id"],),
        ).fetchone()
        if target is not None:
            record = store._record(target)
            if record["status"] == "queued":
                runtime = store._restore(target)
                if any(item.get("status") == "queued" for item in record["guidance"]):
                    return {**public_receipt(receipt), "status": "stop_required"}
                record = runtime.transition(
                    record["id"], "cancelled", expected_revision=record["revision"]
                )
                store._save(connection, record)
            elif record["status"] != "cancelled":
                return {
                    **public_receipt(receipt),
                    "status": (
                        "stop_required"
                        if record["status"] in ACTIVE
                        else record["status"]
                    ),
                }
        elif receipt["guidance_id"]:
            parent = connection.execute(
                "SELECT id,request_id,body FROM turns WHERE id=?",
                (receipt["parent_turn_id"],),
            ).fetchone()
            if parent is None:
                raise TurnConflict("delivery guidance owner is unavailable")
            runtime = store._restore(parent)
            record = store._record(parent)
            item = next(
                (
                    entry
                    for entry in record["guidance"]
                    if entry["id"] == receipt["guidance_id"]
                ),
                None,
            )
            if item is None:
                raise TurnConflict("delivery guidance is unavailable")
            if item["status"] == "queued":
                outcome = runtime.cancel_guidance(
                    record["id"],
                    receipt["guidance_id"],
                    expected_revision=record["revision"],
                )
                store._save(connection, outcome["turn"])
            elif item["status"] != "cancelled":
                return {**public_receipt(receipt), "status": "stop_required"}
        receipt["status"] = "cancelled"
        connection.execute(
            "UPDATE message_deliveries SET body=? WHERE id=?",
            (canonical_json(receipt).decode(), delivery_id),
        )
        connection.commit()
        return public_receipt(receipt)
    finally:
        connection.close()


def deliver_message(
    store: Any,
    payload: Mapping[str, Any],
    *,
    client: Any,
    guard: Callable[[], None],
    interrupt: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Deliver one already-approved immutable snapshot with partial receipts."""
    from tobkiri_protocol.chat_message_v1 import validate_execute_payload

    if set(payload) != {"request", "plan"}:
        raise ValueError("message delivery payload is invalid")
    bound = validate_execute_payload(payload["request"], payload["plan"])
    request, plan = bound["request"], bound["plan"]
    if request["profile_id"] != store.profile_id:
        raise PermissionError("message delivery Profile changed")
    source = {
        "profile_id": store.profile_id,
        "conversation_id": request["source_conversation_id"],
        "turn_id": request["source_turn_id"],
        "tool_call_id": request["tool_call_id"],
    }
    receipts = []
    for recipient in plan["recipient_ids"]:
        delivery_id = (
            "message-delivery:"
            + canonical_digest(
                [
                    store.profile_id,
                    request["source_turn_id"],
                    request["tool_call_id"],
                    recipient,
                ]
            )[7:]
        )
        delivery = {
            "delivery_id": delivery_id,
            "source": source,
            "target_conversation_id": recipient,
            "conversation_revision": 1,
            "content": request["content"],
            "recipient_snapshot": plan["recipient_ids"],
        }
        if request.get("delivery", "steer") == "interrupt":
            delivery["delivery"] = "interrupt"
            delivery["target_state"] = next(
                state
                for state in plan["target_states"]
                if state["conversation_id"] == recipient
            )
        try:
            guard()
            receipt = dispatch_delivery(
                store, delivery, client=client, guard=guard, interrupt=interrupt
            )
        except (PermissionError, ValueError, RuntimeError, KeyError, OSError) as error:
            known = store.delivery_receipt(delivery_id)
            receipt = (
                public_receipt(known)
                if known
                else {
                    "delivery_id": delivery_id,
                    "profile_id": store.profile_id,
                    "target_conversation_id": recipient,
                    "delivery": request.get("delivery", "steer"),
                    "interrupted_turn_id": delivery.get("target_state", {}).get(
                        "turn_id"
                    ),
                    "interrupt_status": None,
                    "target_turn_id": None,
                    "user_message_id": None,
                    "assistant_message_id": None,
                    "guidance_id": None,
                    "parent_turn_id": None,
                    "user_message_saved": False,
                    "assistant_message_saved": False,
                }
            )
            receipt["status"] = "reconciliation_required" if known else "not_started"
            if isinstance(error, InterruptUnsupported):
                receipt["status"] = "unsupported"
                receipt["reason"] = "target_stop_owner_unavailable"
            elif request.get("delivery") == "interrupt":
                connection = store._connect_write()
                try:
                    pending = connection.execute(
                        "SELECT body FROM message_interrupts WHERE id=?", (delivery_id,)
                    ).fetchone()
                finally:
                    connection.close()
                if pending is not None:
                    attempt = json.loads(pending[0])
                    receipt.update(
                        {
                            "status": "reconciliation_required",
                            "delivery": "interrupt",
                            "interrupted_turn_id": attempt["state"]["turn_id"],
                            "interrupt_status": "stop_unconfirmed",
                            "reason": "target_stop_unconfirmed",
                        }
                    )
                elif isinstance(error, TurnConflict):
                    receipt["reason"] = (
                        "target_guidance_pending"
                        if "guidance" in str(error)
                        else "target_state_changed"
                    )
                else:
                    receipt["reason"] = "delivery_not_started"
        receipts.append(receipt)
    statuses = {receipt["status"] for receipt in receipts}
    status = (
        "completed"
        if statuses == {"completed"}
        else (
            "queued"
            if statuses <= {"queued", "running", "waiting", "reserved"}
            else (
                "reconciliation_required"
                if statuses == {"reconciliation_required"}
                else "partial"
            )
        )
    )
    return {
        "version": "tobkiri.chat.message-delivery.v1",
        "plan_digest": plan["plan_digest"],
        "status": status,
        "deliveries": receipts,
    }


def delivery_host_factory() -> Any:
    """Publish one operation requiring the actual approved execute parent."""
    from core_runtime.host_provider_function_v4 import SingleOperationHostFactoryV4
    from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4

    def bind(capture: Any) -> Any:
        from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime

        if capture.user_data_root is None or not capture.profile_id:
            raise PermissionError("message delivery capture is incomplete")
        store = DurableTurnRuntime(
            capture.profile_id, user_data_root=capture.user_data_root
        )

        def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
            from core_runtime.owned_chat_message_approval_v4 import (
                assert_chat_message_request_live,
            )
            from tobkiri_protocol.chat_message_v1 import validate_execute_payload

            invocation.assert_current()
            if set(payload) != {"request", "plan"}:
                raise ValueError("message delivery payload is invalid")
            bound = validate_execute_payload(payload["request"], payload["plan"])
            parent = invocation.parent_invocation
            if not isinstance(parent, CapturedInvocationScopeV4):
                raise PermissionError(
                    "message delivery requires approved execute ancestry"
                )
            parent.assert_current()
            envelope = parent.envelope
            if (
                envelope.contract_id != "tobkiri.service.chat.message.send.v1"
                or envelope.operation_id != "rumi_default_tools_pack.chat-message-send"
                or envelope.contract_version != "1.0.0"
                or envelope.context.profile_id != capture.profile_id
                or envelope.context.plan_digest != capture.plan_digest
                or envelope.context.security_epoch != capture.security_epoch
                or envelope.target_principal
                != invocation.envelope.context.caller_principal
                or parent.public_payload() != bound
            ):
                raise PermissionError("message delivery execute parent changed")
            assert_chat_message_request_live(bound["request"], envelope.context)
            client = invocation.contract_client(
                allowed_contract_ids=DELIVERY_CONTRACTS,
                consumer_pack_id="rumi_turn_runtime_pack",
                include_credentials=False,
            )

            port = getattr(invocation, "approved_interrupt", None)

            def interrupt_target(delivery: Mapping[str, Any]) -> None:
                if port is None:
                    raise InterruptUnsupported(
                        "authenticated target interrupt port is unavailable"
                    )
                observation = port.request(
                    delivery["target_state"],
                    before_signal=lambda: request_interrupt(store, delivery),
                )
                invocation.assert_current()
                deadline = getattr(invocation.envelope, "deadline_monotonic", None)
                if type(deadline) not in {
                    int,
                    float,
                } or not observation.wait_for_verified_drain(float(deadline)):
                    raise TurnConflict("interrupt target drain is unconfirmed")
                invocation.assert_current()

            return deliver_message(
                store,
                bound,
                client=client,
                guard=invocation.assert_current,
                interrupt=interrupt_target if port is not None else None,
            )

        return invoke

    return SingleOperationHostFactoryV4(
        function_id="rumi_turn_runtime_pack.chat-message-deliver",
        contract_id=DELIVERY_CONTRACT,
        operation_id=DELIVERY_OPERATION,
        bind=bind,
    )
