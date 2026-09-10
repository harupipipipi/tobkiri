"""Root guest orchestration for the fixed saved-turn ABI, without Host authority."""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
import time
from typing import Any, Callable

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input
from tobkiri_protocol.saved_tools import MAX_SAVED_TOOL_HOPS
from .saved_turn_plan import SavedTurnPlan, TOOL

from .continuation_chain import ChainIdentity, ContinuationChains
from .continuation_session import ContinuationSession

# Root-owned plan; never import it from the executing Pack or accept it in input.
TARGETS = (
    ("tobkiri.resource.conversation.v1", "rumi_conversation_store_pack.conversation-resource"),
    ("tobkiri.action.message.manage.v1", "rumi_conversation_store_pack.message-manage"),
    ("tobkiri.service.ai.generate.v1", "rumi_ai_gateway_pack.ai-gateway.generate"),
    ("tobkiri.action.message.manage.v1", "rumi_conversation_store_pack.message-manage"),
)
PROTOCOL = "io.tobkiri.packvm.bridge.v2"
INVOKE_RESULT = "tobkiri.packvm.invoke.result.v1"
Execute = Callable[
    [dict[str, Any], dict[str, Any], float, Callable[[], None]], dict[str, Any]
]


@dataclass
class _Turn:
    request: dict[str, Any]
    session: ContinuationSession
    deadline: float
    binding_digest: str
    tool_plan: SavedTurnPlan | None = None
    pending_digest: str | None = None
    busy: bool = True
    terminal: bool = False


class SavedGuestTurns:
    """Retain pending/in-flight/terminal requests through their original expiry.

    The authenticated agent supplies launch bindings and an execution callback
    that verifies the artifact and starts a fresh sandbox for every ABI step.
    No lock is held across execution, so cancellation can fence an active step.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._chains = ContinuationChains(clock=clock, max_hops=MAX_SAVED_TOOL_HOPS)
        self._turns: dict[tuple[str, str], _Turn] = {}
        self._cancelled: dict[tuple[str, str], float] = {}
        self._fence = 0.0
        self._lock = threading.RLock()

    def begin(
        self, request: dict[str, Any], binding_digest: str, execute: Execute,
    ) -> dict[str, Any]:
        """Reserve the identity before the initial sandbox computation."""
        request = strict_loads(canonical_json(request), max_bytes=1024 * 1024)
        request["payload"] = validate_saved_conversation_input(request["payload"])
        key = (request["target_domain"], request["request_id"])
        with self._lock:
            self._purge()
            now = self._clock()
            if now < self._fence or key in self._cancelled or key in self._turns:
                raise ValueError("saved guest request identity is unavailable")
            if len(self._turns) >= 64:
                raise ValueError("saved guest request capacity is exhausted")
            deadline = now + 60
            identity = ChainIdentity(key[0], key[1], binding_digest, deadline)
            plan = SavedTurnPlan(request["payload"]["request"])
            tool_plan = plan if plan.enabled else None
            session = ContinuationSession(
                identity, tuple(dict.fromkeys((*TARGETS, TOOL))) if tool_plan else TARGETS,
                chains=self._chains,
                target_selector=(lambda: plan.target) if tool_plan else None,
            )
            turn = _Turn(request, session, deadline, binding_digest, tool_plan=tool_plan)
            self._turns[key] = turn
        try:
            output = execute(request, request["payload"], deadline, lambda: self._guard(turn))
            with self._lock:
                self._check(turn)
                frame = turn.session.start(canonical_json(output), nonce=secrets.token_hex(24))
                return self._pending(turn, frame)
        except BaseException:
            self._fail(turn)
            raise

    def resume(
        self, domain_id: str, request_id: str, result: object, execute: Execute,
    ) -> dict[str, Any]:
        """Consume a bound Host result once, then execute one fresh sandbox step."""
        with self._lock:
            self._purge()
            turn = self._turns.get((domain_id, request_id))
            if turn is None or turn.busy or turn.terminal:
                raise ValueError("saved guest continuation is unavailable")
            turn.busy = True
        try:
            with self._lock:
                self._check(turn)
                expected = self._binding(turn, "host-result")
                expected["bridge_request_digest"] = turn.pending_digest
                if not isinstance(result, dict) or set(result) != set(expected) | {"bridge_result"} or any(
                    result[key] != value for key, value in expected.items()
                ) or type(result.get("version")) is not int:
                    raise ValueError("saved guest Host result binding is invalid")
                permit = turn.session.receive(canonical_json(result["bridge_result"]))
                arguments = turn.session.resume_arguments(permit)
                turn.pending_digest = None
                if turn.tool_plan is not None:
                    turn.tool_plan.receive(arguments["outcome"])
            output = execute(turn.request, arguments, turn.deadline, lambda: self._guard(turn))
            with self._lock:
                self._check(turn)
                if output.get("kind") == "tobkiri.packvm.continuation.intent.v2":
                    frame = turn.session.advance(
                        permit, canonical_json(output), nonce=secrets.token_hex(24),
                    )
                    return self._pending(turn, frame)
                if set(output) != {"kind", "outcome"} or output["kind"] != INVOKE_RESULT:
                    raise ValueError("saved guest terminal envelope is invalid")
                turn.session.finish(permit, canonical_json(output["outcome"]))
                turn.terminal = True
                return output
        except BaseException:
            self._fail(turn)
            raise

    def contains(self, domain_id: str, request_id: str) -> bool:
        """Route retained identities, including tombstones, without reviving them."""
        with self._lock:
            self._purge()
            return (domain_id, request_id) in self._turns

    def cancel(self, domain_id: str, request_id: str) -> bool:
        """Fence registration or continuation; the caller also stops the process."""
        with self._lock:
            self._purge()
            key = (domain_id, request_id)
            turn = self._turns.get(key)
            cancelled = turn is not None and not turn.terminal
            if turn is not None:
                self._fail(turn)
            expiry = self._clock() + 60
            if key not in self._cancelled and len(self._cancelled) >= 64:
                self._fence = max(self._fence, expiry)
            else:
                self._cancelled[key] = expiry
            return cancelled

    def _pending(self, turn: _Turn, frame: bytes) -> dict[str, Any]:
        if turn.tool_plan is not None:
            turn.tool_plan.check(strict_loads(frame)["payload"])
        turn.pending_digest = canonical_digest(strict_loads(frame))
        turn.busy = False
        wrapper = self._binding(turn, "host-request")
        wrapper.update(
            deadline_monotonic=turn.request["deadline_monotonic"],
            bridge_request=strict_loads(frame), bridge_request_digest=turn.pending_digest,
        )
        return {"state": "pending", "host_bridge_request": wrapper}

    @staticmethod
    def _binding(turn: _Turn, kind: str) -> dict[str, Any]:
        return {
            "kind": f"tobkiri.packvm.bridge.{kind}.v2", "protocol": PROTOCOL, "version": 2,
            "request_id": turn.request["request_id"],
            "target_domain": turn.request["target_domain"],
            "guest_artifact_identity": turn.request["guest_artifact_identity"],
            "request_digest": turn.request["request_digest"],
            "binding_digest": turn.binding_digest,
        }

    def _check(self, turn: _Turn) -> None:
        if turn.terminal or self._clock() >= turn.deadline:
            raise ValueError("saved guest request is cancelled or expired")

    def _guard(self, turn: _Turn) -> None:
        with self._lock:
            self._check(turn)

    def _fail(self, turn: _Turn) -> None:
        with self._lock:
            turn.terminal = True
            turn.pending_digest = None
            turn.session.cancel()

    def _purge(self) -> None:
        now = self._clock()
        self._turns = {key: turn for key, turn in self._turns.items() if turn.deadline > now}
        self._cancelled = {key: expiry for key, expiry in self._cancelled.items() if expiry > now}
