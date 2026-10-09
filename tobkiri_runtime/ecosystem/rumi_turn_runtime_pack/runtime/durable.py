"""Explicitly rooted SQLite persistence for the existing turn lifecycle owner.

The captured Host provider uses this store. It never starts or restarts AI
execution. Restored nonterminal records require explicit reconciliation by the
future execution coordinator.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Mapping

from core_runtime.profile_workspace import validate_profile_id
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict, TurnRuntime
from tobkiri_protocol.saved_workflow_admission import SavedWorkflowAdmission
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input

_MAX_RECORD_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ACTIONS = {
    "transition": {"status", "details"},
    "steer": {"guidance", "guidance_id"},
    "handoff": {"target"},
    "consume_guidance": {"guidance_ids"},
    "cancel_guidance": {"guidance_id"},
}


def _saved_claim_available(record: Mapping[str, Any]) -> bool:
    """Do not admit new execution after the owner has recorded a stop request."""
    return record["status"] == "queued" and not any(
        event.get("name") == "turn.cancellation_requested"
        for event in record.get("events", [])
    )


class DurableTurnRuntime:
    """Retain turn/request identities with bounded reads and atomic updates."""

    def __init__(self, profile_id: str, *, user_data_root: Path, max_turns: int = 10000) -> None:
        """Capture an explicit owner root without opening or creating a database."""
        self.profile_id = validate_profile_id(profile_id)
        if type(max_turns) is not int or max_turns < 1:
            raise ValueError("turn capacity must be an exact positive integer")
        self.max_turns = max_turns
        if not Path(user_data_root).is_absolute():
            raise ValueError("turn owner root must be absolute")
        self.path = (
            Path(user_data_root)
            / "packs"
            / "rumi_turn_runtime_pack"
            / "profiles"
            / self.profile_id
            / "turns.sqlite3"
        )

    def recover_calendar_preparation(self, **arguments: Any) -> dict[str, Any] | None:
        """Recover the immutable Calendar occurrence before any preparation."""
        from .calendar_preparation import recover_preparation
        return recover_preparation(self, **arguments)

    def reserve_calendar_preparation(self, **arguments: Any) -> dict[str, Any]:
        """Reserve target idle admission before Calendar model preparation."""
        from .calendar_preparation import reserve_preparation
        return reserve_preparation(self, **arguments)

    def bind_calendar_preparation(self, **arguments: Any) -> dict[str, Any]:
        """Bind one immutable post-preparation normal saved source envelope."""
        from .calendar_preparation import bind_preparation
        return bind_preparation(self, **arguments)

    def release_calendar_preparation(self, **arguments: Any) -> dict[str, Any]:
        """Release only the exact scheduler reservation before execution."""
        from .calendar_preparation import release_preparation
        return release_preparation(self, **arguments)

    def admit_delivery(self, delivery: Mapping[str, Any]) -> dict[str, Any]:
        """Admit one approved delivery atomically alongside normal saved turns."""
        from ecosystem.rumi_turn_runtime_pack.runtime.delivery import admit_delivery
        return admit_delivery(self, delivery)

    def delivery_receipt(self, delivery_id: str) -> dict[str, Any] | None:
        """Read one durable admission receipt, without starting target execution."""
        if not self.path.exists():
            return None
        self._check_path()
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_deliveries'"
            ).fetchone() is None:
                return None
            row = connection.execute(
                "SELECT body FROM message_deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            connection.close()

    def cancel_delivery(self, delivery_id: str) -> dict[str, Any]:
        """Cancel unstarted input or queued guidance; never claim a live stop."""
        from ecosystem.rumi_turn_runtime_pack.runtime.delivery import cancel_delivery
        return cancel_delivery(self, delivery_id)

    def begin_saved(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Bind validated saved input without starting or retrying execution."""
        return self.begin(self._saved_begin_payload(payload))

    def _saved_begin_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        initial = validate_saved_conversation_input(payload)
        request = initial["request"]
        identity = canonical_digest({
            "profile_id": self.profile_id, "turn_id": request["turn_id"],
        }).removeprefix("sha256:")
        return {
            "turn_id": request["turn_id"],
            "request_id": "saved-turn." + identity,
            "conversation_id": request["conversation_id"],
            "conversation_revision": request["conversation_revision"],
            "input_digest": canonical_digest(initial),
        }

    def saved_input(self, source: Mapping[str, Any]) -> dict[str, Any] | None:
        """Recover exact source-bound accepted context without preparing another batch."""
        initial = validate_saved_conversation_input(source)
        if not self.path.exists():
            return None
        self._check_path()
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='saved_inputs'"
            ).fetchone() is None:
                return None
            row = connection.execute(
                "SELECT source_digest, accepted_digest, context FROM saved_inputs WHERE id=?",
                (initial["request"]["turn_id"],),
            ).fetchone()
            return self._saved_input(initial, row) if row is not None else None
        finally:
            connection.close()

    def bind_saved_input(
        self, source: Mapping[str, Any], accepted: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Atomically retain one owner-projected context for the exact original input."""
        initial = validate_saved_conversation_input(source)
        captured = validate_saved_conversation_input(accepted)
        original_request = dict(initial["request"])
        accepted_request = dict(captured["request"])
        if "resolved_chat_references" in original_request:
            raise PermissionError("source cannot claim resolved chat references")
        original_request.pop("task_context", None)
        context = {
            "kind": "tobkiri.saved.input-projection.v1",
            "task_context": accepted_request.pop("task_context", None),
            "resolved_chat_references": accepted_request.pop("resolved_chat_references", None),
            "target_settings": {key: original_request[key] for key in (
                "tool_selection",
            ) if key in original_request},
        }
        if canonical_digest(original_request) != canonical_digest(accepted_request):
            raise PermissionError("context capture cannot change saved user input")
        body = json.dumps(context, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise ValueError("saved context exceeds size limit")
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT source_digest, accepted_digest, context FROM saved_inputs WHERE id=?",
                (initial["request"]["turn_id"],),
            ).fetchone()
            if row is not None:
                return self._saved_input(initial, row)
            turn = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id=?",
                (initial["request"]["turn_id"],),
            ).fetchone()
            if turn is not None and self._record(turn).get("input_digest") != canonical_digest(captured):
                record = self._record(turn)
                if ((record.get("delivery_provenance") or record.get("calendar_preparation"))
                        and record["status"] == "queued"
                        and record.get("input_digest") == canonical_digest(initial)):
                    # Only the immutable admitted source may receive the owner's
                    # optional task context before the first saved execution.
                    record["input_digest"] = canonical_digest(captured)
                    self._save(connection, record)
                else:
                    raise TurnConflict("turn input identity was rebound")
            if connection.execute("SELECT COUNT(*) FROM saved_inputs").fetchone()[0] >= self.max_turns:
                raise TurnConflict("saved input capacity is exhausted")
            connection.execute(
                "INSERT INTO saved_inputs(id, source_digest, accepted_digest, context) VALUES (?, ?, ?, ?)",
                (initial["request"]["turn_id"], canonical_digest(initial), canonical_digest(captured), body),
            )
            connection.commit()
            return captured
        finally:
            connection.close()

    def _saved_input(self, source: Mapping[str, Any], row: tuple[str, str, str]) -> dict[str, Any]:
        source_digest, accepted_digest, body = row
        if canonical_digest(source) != source_digest:
            raise TurnConflict("saved source input identity was rebound")
        if len(body.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise ValueError("saved context exceeds size limit")
        captured = validate_saved_conversation_input(source)
        captured["request"].pop("task_context", None)
        captured["request"].pop("resolved_chat_references", None)
        context = json.loads(body)
        if isinstance(context, dict) and context.get("kind") == "tobkiri.saved.input-projection.v1":
            if set(context) not in (
                {"kind", "task_context", "resolved_chat_references"},
                {"kind", "task_context", "resolved_chat_references", "target_settings"},
            ):
                raise ValueError("saved projection fields are invalid")
            if "target_settings" in context:
                expected_settings = {key: captured["request"][key] for key in (
                    "tool_selection",
                ) if key in captured["request"]}
                if context["target_settings"] != expected_settings:
                    raise ValueError("saved target settings identity is invalid")
            for field in ("task_context", "resolved_chat_references"):
                if context[field] is not None:
                    captured["request"][field] = context[field]
        elif context is not None:
            # Old owner rows retain their original task-context-only shape.
            captured["request"]["task_context"] = context
        captured = validate_saved_conversation_input(captured)
        if canonical_digest(captured) != accepted_digest:
            raise ValueError("saved accepted input identity is invalid")
        return captured

    def delivery_state(self, conversation_id: str, *, conversation_revision: int | None = None) -> dict[str, Any]:
        """Read exact conversation admission state without truncated pagination."""
        if not isinstance(conversation_id, str) or not _ID.fullmatch(conversation_id):
            raise ValueError("delivery state conversation is invalid")
        from .delivery import target_delivery_state
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            return target_delivery_state(self, connection, conversation_id, conversation_revision)
        finally:
            connection.close()

    def delivery_target_settings(self, conversation_id: str) -> dict[str, Any]:
        """Read finite settings proved by a completed ordinary owner receipt."""
        from .delivery import latest_target_settings

        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            return latest_target_settings(self, connection, conversation_id)
        finally:
            connection.close()

    def claim_saved(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically acquire one queued saved turn without granting authority.

        Guidance may update the queued revision before this transaction starts.
        Preserve that guidance while reading and claiming the latest owner state
        under the same SQLite lock. Only a queued record can win. Lost claim
        replies still reconcile the running record, never claim execution again.
        """
        self.begin_saved(payload)
        request = validate_saved_conversation_input(payload)["request"]
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id=?",
                (request["turn_id"],),
            ).fetchone()
            if row is None:
                raise TurnConflict("saved turn disappeared before claim")
            current = self._record(row)
            if current.get("input_digest") != canonical_digest(
                validate_saved_conversation_input(payload)
            ):
                raise TurnConflict("turn input identity was rebound")
            if not _saved_claim_available(current):
                return {"claimed": False, "turn": current}
            from ecosystem.rumi_turn_runtime_pack.runtime.delivery import (
                assert_conversation_idle,
            )
            assert_conversation_idle(
                self, connection, current["conversation_id"], current["id"]
            )
            message_ids = {
                role: "message:" + canonical_digest(
                    [request["conversation_id"], request["turn_id"], role]
                ).removeprefix("sha256:")
                for role in ("user", "assistant")
            }
            runtime = self._restore(row)
            record = runtime.transition(
                current["id"], "running", expected_revision=current["revision"],
                details={
                    "phase": "saved_execution_claimed",
                    "user_message_id": message_ids["user"],
                    "assistant_message_id": message_ids["assistant"],
                },
            )
            self._save(connection, record)
            connection.commit()
            return {"claimed": True, "turn": record}
        finally:
            connection.close()

    def claim_saved_workflow(
        self, payload: Mapping[str, Any], admission: SavedWorkflowAdmission,
    ) -> dict[str, Any]:
        """Atomically bind a captured graph identity while winning the turn claim.

        This is an owner-internal method, not a public lifecycle operation.
        It does not create a run, grant authority or replay an existing claim.
        """
        if not isinstance(admission, SavedWorkflowAdmission):
            raise PermissionError("saved Workflow admission requires captured owner data")
        admission.bind_input(self.profile_id, payload)
        return self._claim_saved_workflow(payload, admission)

    def _claim_saved_workflow(
        self, payload: Mapping[str, Any], admission: SavedWorkflowAdmission,
    ) -> dict[str, Any]:
        record = self.begin_saved(payload)
        if not _saved_claim_available(record):
            return {"claimed": False, "turn": record}
        initial = validate_saved_conversation_input(payload)
        request = initial["request"]
        message_ids = {
            role: "message:" + canonical_digest(
                [request["conversation_id"], request["turn_id"], role]
            ).removeprefix("sha256:")
            for role in ("user", "assistant")
        }
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?", (record["id"],)
            ).fetchone()
            if row is None:
                raise TurnConflict("saved turn disappeared before claim")
            current = self._record(row)
            if current.get("input_digest") != canonical_digest(initial):
                raise TurnConflict("turn input identity was rebound")
            if not _saved_claim_available(current):
                return {"claimed": False, "turn": current}
            if "saved_workflow" in current:
                raise TurnConflict("queued saved turn already has an execution binding")
            from ecosystem.rumi_turn_runtime_pack.runtime.delivery import (
                assert_conversation_idle,
            )
            assert_conversation_idle(
                self, connection, current["conversation_id"], current["id"]
            )
            runtime = self._restore(row)
            claimed = runtime.transition(
                current["id"], expected_revision=current["revision"], status="running",
                details={"phase": "saved_execution_claimed",
                         "user_message_id": message_ids["user"],
                         "assistant_message_id": message_ids["assistant"]},
            )
            claimed["saved_workflow"] = admission.to_mapping()
            self._save(connection, claimed)
            connection.commit()
            return {"claimed": True, "turn": claimed}
        finally:
            connection.close()

    def begin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Begin or recover the same persisted request without re-executing it."""
        allowed = {
            "profile_id",
            "turn_id",
            "request_id",
            "conversation_id",
            "conversation_revision",
            "input_digest",
        }
        if set(payload) - allowed or (
            "profile_id" in payload and payload["profile_id"] != self.profile_id
        ):
            raise PermissionError("turn begin does not match captured Profile")
        for field in ("turn_id", "request_id", "conversation_id"):
            if not isinstance(payload.get(field), str) or not _ID.fullmatch(payload[field]):
                raise ValueError("durable begin requires explicit stable identities")
        if "input_digest" in payload:
            _input_digest(payload["input_digest"])
        bound = {**payload, "profile_id": self.profile_id}
        # Validate before creating a new database, including the exact revision.
        candidate = TurnRuntime().begin(bound)
        if "input_digest" in payload:
            candidate["input_digest"] = payload["input_digest"]
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE request_id = ?",
                (candidate["request_id"],),
            ).fetchone()
            if row is not None:
                if self._record(row).get("input_digest") != candidate.get("input_digest"):
                    raise TurnConflict("turn input identity was rebound")
                runtime = self._restore(row)
                return runtime.begin(bound)
            if (
                connection.execute(
                    "SELECT 1 FROM turns WHERE id = ?", (candidate["id"],)
                ).fetchone()
                is not None
            ):
                raise TurnConflict("turn already exists")
            from ecosystem.rumi_turn_runtime_pack.runtime.delivery import assert_conversation_idle
            assert_conversation_idle(self, connection, candidate["conversation_id"], candidate["id"])
            if connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0] >= self.max_turns:
                raise TurnConflict("durable turn capacity is exhausted")
            self._save(connection, candidate)
            connection.commit()
            return candidate
        finally:
            connection.close()

    def get(self, turn_id: str) -> dict[str, Any] | None:
        """Read one persisted record; absent stores are not created by reads."""
        if not self.path.exists():
            return None
        self._check_path()
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            return self._record(row) if row is not None else None
        finally:
            connection.close()

    def list(self, *, limit: int = 100, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """Read a bounded newest-first page without loading the entire history."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("turn list limit is invalid")
        if conversation_id is not None and (
            not isinstance(conversation_id, str) or not _ID.fullmatch(conversation_id)
        ):
            raise ValueError("turn conversation filter is invalid")
        if not self.path.exists():
            return []
        self._check_path()
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE (? IS NULL OR json_extract(body, '$.conversation_id') = ?) ORDER BY updated_at DESC, id LIMIT ?",
                (conversation_id, conversation_id, limit),
            ).fetchall()
            return [self._record(row) for row in rows]
        finally:
            connection.close()

    def mutate(
        self, action: str, turn_id: str, *, expected_revision: int,
        reject_saved_transition: bool = False, **arguments: Any
    ) -> dict[str, Any]:
        """Apply only existing lifecycle methods under one SQLite write lock."""
        if type(reject_saved_transition) is not bool:
            raise ValueError("turn mutation boundary is invalid")
        if action not in _ACTIONS or set(arguments) - _ACTIONS[action]:
            raise ValueError("durable turn mutation fields are invalid")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("turn revision must be an exact positive integer")
        if not self.path.exists():
            raise KeyError("turn is unknown")
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if row is None:
                raise KeyError("turn is unknown")
            if reject_saved_transition and action == "transition":
                existing = self._record(row)
                if existing.get("input_digest") and existing["request_id"].startswith("saved-turn."):
                    # The captured management adapter fixes this flag, never
                    # caller payload. Check under the write lock, so creation
                    # or pruning cannot race a separate preflight read.
                    raise PermissionError("saved turn status is coordinator-owned")
            existing = self._record(row)
            if existing.get("calendar_preparation") and existing["status"] == "queued" and (action == "steer" or (action == "transition" and not existing.get("input_digest"))):
                raise TurnConflict("Calendar preparation is coordinator-owned")
            if action == "steer" and existing["status"] == "queued" and existing.get("delivery_provenance"):
                raise TurnConflict("delivery reservation cannot receive guidance before claim")
            runtime = self._restore(row)
            if action == "transition" and arguments.get("status") == "running":
                from ecosystem.rumi_turn_runtime_pack.runtime.delivery import assert_conversation_idle
                record = self._record(row)
                assert_conversation_idle(self, connection, record["conversation_id"], turn_id)
            result = getattr(runtime, action)(
                turn_id, expected_revision=expected_revision, **arguments
            )
            record = runtime.get(turn_id)
            if record is None:
                raise ValueError("turn mutation lost its record")
            self._save(connection, record)
            connection.commit()
            return result
        finally:
            connection.close()

    def reconcile_saved(
        self, turn_id: str, *, expected_revision: int, input_digest: str,
        result_reference: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Commit coordinator-verified owner evidence without starting execution.

        This method is intentionally absent from the public lifecycle adapter.
        The coordinator must obtain the reference through its captured reader.
        """
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("saved receipt revision is invalid")
        return self.settle_saved_from_receipt(
            turn_id,
            expected_revision=expected_revision,
            input_digest=input_digest,
            result_reference=result_reference,
        )

    def settle_saved_from_receipt(
        self, turn_id: str, *, input_digest: str,
        result_reference: Mapping[str, Any],
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Settle exact owner output unless cancellation reached a terminal fence.

        A cancellation request records intent; it is not proof that the guest or
        Provider stopped before committing output.  Therefore an exact owner
        receipt may truthfully win while the turn remains ``running`` or
        ``waiting``.  A verified drain first moves the turn to ``cancelled``,
        which this method never overwrites.  Reader reconciliation additionally
        supplies its captured revision so an unrelated concurrent mutation must
        be observed again rather than silently accepted here.
        """

        _input_digest(input_digest)
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1
        ):
            raise ValueError("saved receipt revision is invalid")
        if not self.path.exists():
            raise KeyError("turn is unknown")
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?", (turn_id,),
            ).fetchone()
            if row is None:
                raise KeyError("turn is unknown")
            record = self._record(row)
            reference = _saved_result_reference(record, result_reference)
            if (
                record.get("input_digest") == input_digest
                and record["request_id"].startswith("saved-turn.")
                and record["status"] == "completed"
                and record.get("result_reference") == reference
            ):
                connection.rollback()
                return record
            if (
                record.get("input_digest") != input_digest
                or not record["request_id"].startswith("saved-turn.")
                or record["status"] not in {"running", "waiting"}
                or (
                    expected_revision is not None
                    and record["revision"] != expected_revision
                )
                or not _has_only_unconfirmed_cancellation(record)
            ):
                raise TurnConflict("saved receipt cannot settle current turn")
            runtime = self._restore(row)
            current = runtime._turns[turn_id]
            if any(
                event.get("name") == "turn.cancellation_requested"
                for event in current["events"]
            ):
                _cancel_queued_guidance_after_unconfirmed_stop(current)
            result = runtime.transition(
                turn_id,
                "completed",
                expected_revision=current["revision"],
                details={"result_reference": reference},
                reconciled_saved=True,
            )
            self._save(connection, result)
            connection.commit()
            return result
        finally:
            connection.close()

    def reserve_guidance_followup(
        self,
        parent_turn_id: str,
        guidance_id: str,
        source_turn_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Atomically bind queued guidance to one deterministic saved child."""

        begin = self._saved_begin_payload(payload)
        request = validate_saved_conversation_input(payload)["request"]
        if not self.path.exists():
            raise KeyError("turn is unknown")
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            parent_row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?",
                (parent_turn_id,),
            ).fetchone()
            source_row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?",
                (source_turn_id,),
            ).fetchone()
            if parent_row is None or source_row is None:
                raise KeyError("guidance source is unavailable")
            parent = self._record(parent_row)
            source = self._record(source_row)
            reference = source.get("result_reference")
            if (
                parent["status"] != "completed"
                or source["status"] != "completed"
                or not isinstance(reference, Mapping)
                or request["conversation_id"] != parent["conversation_id"]
                or request["conversation_id"] != source["conversation_id"]
                or request["conversation_revision"]
                != reference.get("conversation_revision")
            ):
                raise TurnConflict("guidance source is not acknowledged")
            item = next(
                (
                    value for value in parent["guidance"]
                    if value.get("id") == guidance_id
                ),
                None,
            )
            if item is None:
                raise KeyError("guidance is unavailable")
            child_row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?",
                (begin["turn_id"],),
            ).fetchone()
            if item.get("status") in {"consumed", "sent", "failed"}:
                if item.get("followup_turn_id") != begin["turn_id"] or child_row is None:
                    raise TurnConflict("guidance followup identity changed")
                child = self._record(child_row)
                if (
                    child.get("input_digest") != begin["input_digest"]
                    or child.get("guidance_parent_turn_id") != parent_turn_id
                    or child.get("guidance_id") != guidance_id
                    or child.get("guidance_source_turn_id") != source_turn_id
                ):
                    raise TurnConflict("guidance followup input changed")
                connection.rollback()
                return child
            value = item.get("value")
            if (
                item.get("status") != "queued"
                or not isinstance(value, Mapping)
                or value.get("auto_send") is not True
            ):
                raise TurnConflict("guidance is not eligible for followup")
            if child_row is None:
                rebound = connection.execute(
                    "SELECT id, request_id, body FROM turns WHERE request_id = ?",
                    (begin["request_id"],),
                ).fetchone()
                if rebound is not None or connection.execute(
                    "SELECT COUNT(*) FROM turns"
                ).fetchone()[0] >= self.max_turns:
                    raise TurnConflict("guidance followup cannot be reserved")
                child = TurnRuntime().begin(
                    {**begin, "profile_id": self.profile_id}
                )
                child["input_digest"] = begin["input_digest"]
                child["guidance_parent_turn_id"] = parent_turn_id
                child["guidance_id"] = guidance_id
                child["guidance_source_turn_id"] = source_turn_id
                self._save(connection, child)
            else:
                child = self._record(child_row)
                if (
                    child.get("input_digest") != begin["input_digest"]
                    or child.get("guidance_parent_turn_id") != parent_turn_id
                    or child.get("guidance_id") != guidance_id
                    or child.get("guidance_source_turn_id") != source_turn_id
                ):
                    raise TurnConflict("guidance followup input changed")
            item["status"] = "consumed"
            item["consumed_at"] = _now_ms()
            item["followup_turn_id"] = child["id"]
            item["followup_source_turn_id"] = source_turn_id
            item["followup_input"] = validate_saved_conversation_input(payload)
            parent["revision"] += 1
            parent["updated_at"] = _now_ms()
            parent["events"].append({
                "sequence": len(parent["events"]),
                "name": "turn.guidance_consumed",
                "at": parent["updated_at"],
                "details": {
                    "guidance_ids": [guidance_id],
                    "followup_turn_id": child["id"],
                },
            })
            self._save(connection, parent)
            connection.commit()
            return child
        finally:
            connection.close()

    def confirm_guidance_followup(
        self, parent_turn_id: str, guidance_id: str, child_turn_id: str,
    ) -> dict[str, Any]:
        """Mark one reserved guidance item from its terminal child receipt."""

        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = [
                connection.execute(
                    "SELECT id, request_id, body FROM turns WHERE id = ?", (value,),
                ).fetchone()
                for value in (parent_turn_id, child_turn_id)
            ]
            if any(row is None for row in rows):
                raise KeyError("guidance followup is unavailable")
            parent, child = (self._record(row) for row in rows)
            item = next(
                (value for value in parent["guidance"] if value.get("id") == guidance_id),
                None,
            )
            if item is None or item.get("followup_turn_id") != child_turn_id:
                raise TurnConflict("guidance followup identity changed")
            if child["status"] not in {"completed", "failed", "cancelled"}:
                connection.rollback()
                return parent
            expected = "sent" if child["status"] == "completed" else "failed"
            if item.get("status") == expected:
                connection.rollback()
                return parent
            if item.get("status") != "consumed":
                raise TurnConflict("guidance followup status changed")
            item["status"] = expected
            item["updated_at"] = _now_ms()
            parent["revision"] += 1
            parent["updated_at"] = item["updated_at"]
            self._save(connection, parent)
            connection.commit()
            return parent
        finally:
            connection.close()

    def active_guidance_followup(self, parent_turn_id: str) -> str | None:
        """Return the sole nonterminal descendant in a bounded guidance chain."""

        frontier = [parent_turn_id]
        visited: set[str] = set()
        active: list[str] = []
        for _depth in range(9):
            next_frontier: list[str] = []
            for owner_id in frontier:
                if owner_id in visited:
                    raise TurnConflict("guidance followup cycle detected")
                visited.add(owner_id)
                owner = self.get(owner_id)
                if owner is None or owner.get("status") != "completed":
                    continue
                for item in owner.get("guidance", []):
                    child_id = item.get("followup_turn_id")
                    if not isinstance(child_id, str):
                        continue
                    child = self.get(child_id)
                    if child is None:
                        continue
                    if (
                        child.get("guidance_parent_turn_id") != owner_id
                        or child.get("guidance_id") != item.get("id")
                        or child.get("guidance_source_turn_id")
                        != item.get("followup_source_turn_id")
                    ):
                        raise TurnConflict("guidance followup identity changed")
                    if child.get("status") in {"queued", "running", "waiting"}:
                        active.append(child_id)
                    elif child.get("status") == "completed":
                        next_frontier.append(child_id)
            if not next_frontier:
                break
            frontier = next_frontier
        else:
            raise TurnConflict("guidance followup depth exceeded")
        if len(active) > 1:
            raise TurnConflict("guidance has multiple active followups")
        return active[0] if active else None

    def recover_guidance(
        self,
        root_turn_id: str,
        guidance_id: str,
        guidance: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Recover one stable guidance mutation across its bounded turn chain."""

        frontier = [root_turn_id]
        visited: set[str] = set()
        recovered: dict[str, Any] | None = None
        for _depth in range(9):
            next_frontier: list[str] = []
            for owner_id in frontier:
                if owner_id in visited:
                    raise TurnConflict("guidance followup cycle detected")
                visited.add(owner_id)
                owner = self.get(owner_id)
                if owner is None:
                    continue
                for item in owner.get("guidance", []):
                    if item.get("id") == guidance_id:
                        if item.get("value") != dict(guidance) or recovered is not None:
                            raise TurnConflict("guidance identity rebound")
                        recovered = owner
                    child_id = item.get("followup_turn_id")
                    if not isinstance(child_id, str):
                        continue
                    child = self.get(child_id)
                    if (
                        child is None
                        or child.get("guidance_parent_turn_id") != owner_id
                        or child.get("guidance_id") != item.get("id")
                        or child.get("guidance_source_turn_id")
                        != item.get("followup_source_turn_id")
                    ):
                        raise TurnConflict("guidance followup identity changed")
                    next_frontier.append(child_id)
            if not next_frontier:
                return recovered
            frontier = next_frontier
        raise TurnConflict("guidance followup depth exceeded")

    def prepare_guidance_stop(
        self,
        parent_turn_id: str,
        *,
        expected_active_turn_id: str | None,
    ) -> dict[str, Any] | None:
        """Fence one active descendant and cancel queued chain guidance."""

        if not self.path.exists():
            raise KeyError("turn is unknown")
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            def load(turn_id: str) -> dict[str, Any] | None:
                row = connection.execute(
                    "SELECT id, request_id, body FROM turns WHERE id = ?",
                    (turn_id,),
                ).fetchone()
                return None if row is None else self._record(row)

            root = load(parent_turn_id)
            if root is None:
                raise KeyError("turn is unknown")
            if root.get("status") != "completed":
                connection.rollback()
                return None
            frontier = [root]
            visited: set[str] = set()
            owners: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
            active: list[dict[str, Any]] = []
            for _depth in range(9):
                next_frontier: list[dict[str, Any]] = []
                for owner in frontier:
                    owner_id = owner["id"]
                    if owner_id in visited:
                        raise TurnConflict("guidance followup cycle detected")
                    visited.add(owner_id)
                    queued = [
                        item
                        for item in owner.get("guidance", [])
                        if item.get("status") == "queued"
                        and isinstance(item.get("value"), Mapping)
                        and item["value"].get("auto_send") is True
                    ]
                    if queued:
                        owners.append((owner, queued))
                    for item in owner.get("guidance", []):
                        child_id = item.get("followup_turn_id")
                        if not isinstance(child_id, str):
                            continue
                        child = load(child_id)
                        if (
                            child is None
                            or child.get("guidance_parent_turn_id") != owner_id
                            or child.get("guidance_id") != item.get("id")
                            or child.get("guidance_source_turn_id")
                            != item.get("followup_source_turn_id")
                        ):
                            raise TurnConflict("guidance followup identity changed")
                        if child.get("status") in {"queued", "running", "waiting"}:
                            active.append(child)
                        if child.get("status") == "completed":
                            next_frontier.append(child)
                if not next_frontier:
                    break
                frontier = next_frontier
            else:
                raise TurnConflict("guidance followup depth exceeded")
            if len(active) > 1:
                raise TurnConflict("guidance has multiple active followups")
            active_turn_id = active[0]["id"] if active else None
            if active_turn_id != expected_active_turn_id:
                raise TurnConflict("guidance active followup changed")
            if not owners and not active:
                connection.rollback()
                return None
            changed_at = _now_ms()
            for owner, queued in owners:
                for item in queued:
                    item["status"] = "failed"
                    item["updated_at"] = changed_at
                    item["failure_reason"] = "cancelled_before_dispatch"
                owner["revision"] += 1
                owner["updated_at"] = changed_at
                owner["events"].append({
                    "sequence": len(owner["events"]),
                    "name": "turn.guidance_cancelled",
                    "at": changed_at,
                    "details": {"guidance_ids": [item["id"] for item in queued]},
                })
                self._save(connection, owner)
            if active:
                active_record = active[0]
                active_row = connection.execute(
                    "SELECT id, request_id, body FROM turns WHERE id = ?",
                    (active_record["id"],),
                ).fetchone()
                if active_row is None:
                    raise TurnConflict("guidance active followup disappeared")
                runtime = self._restore(active_row)
                fenced = runtime.request_cancellation(active_record["id"])
                self._save(connection, fenced)
            connection.commit()
            return {
                "root": load(parent_turn_id),
                "active_turn_id": active_turn_id,
                "cancelled_guidance": sum(len(items) for _, items in owners),
            }
        finally:
            connection.close()

    def cancel_queued_guidance(self, parent_turn_id: str) -> dict[str, Any] | None:
        """Cancel queued guidance when no descendant execution is active."""

        prepared = self.prepare_guidance_stop(
            parent_turn_id,
            expected_active_turn_id=self.active_guidance_followup(parent_turn_id),
        )
        if prepared is None or prepared["active_turn_id"] is not None:
            return None
        return prepared["root"]

    def request_saved_cancellation(self, turn_id: str) -> dict[str, Any]:
        """Persist a stop request without treating wrapper exit as termination."""

        return self._saved_cancellation_update(turn_id, confirm=False)

    def confirm_saved_cancellation(self, turn_id: str) -> dict[str, Any]:
        """Persist a cancelled terminal after Host verified nested drain."""

        return self._saved_cancellation_update(turn_id, confirm=True)

    def _saved_cancellation_update(
        self, turn_id: str, *, confirm: bool
    ) -> dict[str, Any]:
        if not isinstance(turn_id, str) or not _ID.fullmatch(turn_id):
            raise ValueError("saved cancellation requires a stable turn ID")
        if not self.path.exists():
            raise KeyError("turn is unknown")
        connection = self._connect_write()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, request_id, body FROM turns WHERE id = ?", (turn_id,),
            ).fetchone()
            if row is None:
                raise KeyError("turn is unknown")
            existing = self._record(row)
            if (
                not existing.get("input_digest")
                or not existing["request_id"].startswith("saved-turn.")
            ):
                raise PermissionError("only saved turns can be cancelled")
            runtime = self._restore(row)
            result = (
                runtime.confirm_cancellation(turn_id)
                if confirm
                else runtime.request_cancellation(turn_id)
            )
            self._save(connection, result)
            connection.commit()
            return result
        finally:
            connection.close()

    def _connect_write(self) -> sqlite3.Connection:
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            self.path.chmod(0o600)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, updated_at INTEGER NOT NULL, body TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS saved_inputs (id TEXT PRIMARY KEY, source_digest TEXT NOT NULL, accepted_digest TEXT NOT NULL, context TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS message_deliveries (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL)"
            )
            connection.execute("CREATE TABLE IF NOT EXISTS message_interrupts (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS calendar_preparations (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            return connection
        except Exception:
            connection.close()
            raise

    def _check_path(self) -> None:
        # Defense against accidentally redirected owner files. The captured
        # application-data tree must remain protected from hostile local writes;
        # this check is not a substitute for filesystem permissions.
        if any(path.is_symlink() for path in (self.path, *self.path.parents)):
            raise PermissionError("turn owner path must not contain symlinks")

    def _record(self, row: tuple[str, str, str]) -> dict[str, Any]:
        turn_id, request_id, body = row
        if len(body.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise ValueError("turn record exceeds size limit")
        record = json.loads(body)
        if not isinstance(record, dict) or record.get("profile_id") != self.profile_id:
            raise ValueError("persisted turn Profile is invalid")
        if record.get("id") != turn_id or record.get("request_id") != request_id:
            raise ValueError("persisted turn identity does not match its index")
        if "input_digest" in record:
            _input_digest(record["input_digest"])
        if "saved_workflow" in record:
            value = record["saved_workflow"]
            if not isinstance(value, Mapping):
                raise ValueError("persisted saved Workflow admission is invalid")
            admission = SavedWorkflowAdmission.from_mapping(value)
            if (admission.profile_id != self.profile_id or admission.turn_id != turn_id
                    or admission.conversation_id != record.get("conversation_id")
                    or admission.input_digest != record.get("input_digest")):
                raise ValueError("persisted saved Workflow admission was rebound")
        for field in ("id", "request_id", "conversation_id"):
            value = record.get(field)
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError("persisted turn identity is invalid")
        for field in ("revision", "conversation_revision", "created_at", "updated_at"):
            value = record.get(field)
            if type(value) is not int or value < 1:
                raise ValueError("persisted turn revision or timestamp is invalid")
        if record.get("status") not in {
            "queued",
            "running",
            "waiting",
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError("persisted turn status is invalid")
        for field in ("guidance", "events"):
            values = record.get(field)
            if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
                raise ValueError("persisted turn sequence is invalid")
        return record

    def _restore(self, row: tuple[str, str, str]) -> TurnRuntime:
        record = self._record(row)
        runtime = TurnRuntime()
        # Same-owner restoration of one record; legacy global runtime pools and
        # terminal pruning are deliberately not the durable identity index.
        runtime._turns[record["id"]] = record
        runtime._request_ids[record["request_id"]] = record["id"]
        return runtime

    def _save(self, connection: sqlite3.Connection, record: Mapping[str, Any]) -> None:
        body = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > _MAX_RECORD_BYTES:
            raise ValueError("turn record exceeds size limit")
        connection.execute(
            "INSERT INTO turns(id, request_id, updated_at, body) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, body=excluded.body",
            (record["id"], record["request_id"], record["updated_at"], body),
        )


def _input_digest(value: object) -> None:
    """Validate an input identity, never an execution or approval credential."""
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("turn input digest is invalid")


def _saved_result_reference(
    record: Mapping[str, Any], value: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate the content-free identity committed by the conversation owner."""

    if not isinstance(value, Mapping):
        raise TurnConflict("saved receipt result identity is invalid")
    reference = dict(value)
    expected_fields = {
        "conversation_id",
        "conversation_revision",
        "user_message_id",
        "assistant_message_id",
        "outcome_digest",
    }
    message_ids = {
        role: "message:"
        + canonical_digest(
            [record["conversation_id"], record["id"], role]
        ).removeprefix("sha256:")
        for role in ("user", "assistant")
    }
    if (
        set(reference) != expected_fields
        or reference.get("conversation_id") != record["conversation_id"]
        or type(reference.get("conversation_revision")) is not int
        or reference["conversation_revision"]
        != record["conversation_revision"] + 2
        or reference.get("user_message_id") != message_ids["user"]
        or reference.get("assistant_message_id") != message_ids["assistant"]
        or not isinstance(reference.get("outcome_digest"), str)
        or _DIGEST.fullmatch(reference["outcome_digest"]) is None
    ):
        raise TurnConflict("saved receipt result identity is invalid")
    return reference


def _has_only_unconfirmed_cancellation(record: Mapping[str, Any]) -> bool:
    """Accept no cancellation or one exact, still-unconfirmed stop intent."""

    events = record.get("events")
    if not isinstance(events, list):
        return False
    requested = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("name") == "turn.cancellation_requested"
    ]
    if not requested:
        return not any(event.get("name") == "turn.cancelled" for event in events)
    if len(requested) != 1 or any(
        event.get("name") == "turn.cancelled" for event in events
    ):
        return False
    index, event = requested[0]
    return (
        index == len(events) - 1
        and event.get("sequence") == index
        and event.get("details") == {"phase": "nested_cancellation_requested"}
    )


def _cancel_queued_guidance_after_unconfirmed_stop(
    record: dict[str, Any],
) -> None:
    """Fence queued descendants when an already-committed result wins a stop."""

    queued = [
        item
        for item in record["guidance"]
        if item.get("status") == "queued"
        and isinstance(item.get("value"), Mapping)
        and item["value"].get("auto_send") is True
    ]
    if not queued:
        return
    changed_at = _now_ms()
    for item in queued:
        item["status"] = "failed"
        item["updated_at"] = changed_at
        item["failure_reason"] = "cancelled_before_dispatch"
    record["revision"] += 1
    record["updated_at"] = changed_at
    record["events"].append(
        {
            "sequence": len(record["events"]),
            "name": "turn.guidance_cancelled",
            "at": changed_at,
            "details": {"guidance_ids": [item["id"] for item in queued]},
        }
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
