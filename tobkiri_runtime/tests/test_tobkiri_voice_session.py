"""Source acceptance for captured voice ports, without devices or credentials."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from ecosystem.tobkiri_voice_agent_pack.voice import session as voice
from tobkiri_protocol.canonical import canonical_digest


class Port:
    """Trusted adapter fixture; records exact public calls without executing them."""

    api_version = voice.PORT_API
    profile_id = "defaults"
    plan_digest = "sha256:" + "a" * 64
    media_completion_available = True
    cancellation_available = True

    def __init__(self) -> None:
        self.valid = True
        self.calls: list[tuple[voice.VoiceCall, dict[str, Any]]] = []
        self.drains: list[Sequence[voice.VoiceCall]] = []
        self.routes = {
            contract: (voice.VoiceRoute(contract, "1.0.0", f"operation.{index}", "provider.one"),)
            for index, contract in enumerate(voice.REQUIRED_CONTRACTS)
        }
        self.block_contract: str | None = None
        self.block_media_completion: str | None = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.ignore_cancel = False
        self.text = "Hello"
        self.media_status = "captured"
        self.completion_wrong_request = False
        self.raw_media_response = False
        self.raw_error = False
        self.drain_confirmed = True
        self.stop_confirmed = True
        self.turn_status = "completed"
        self.turn: dict[str, Any] = {}
        self.conversation: dict[str, Any] = {}
        self.reply = "SAY: Hello there ASK: Anything else?"
        self.corrupt: str | None = None

    def assert_current(self, scope: voice.VoiceScope) -> None:
        """Reject changed captured identities before any source call."""
        assert self.valid and scope.profile_id == self.profile_id
        assert scope.plan_digest == self.plan_digest

    def providers(self, contract_id: str) -> Sequence[voice.VoiceRoute]:
        """Return finite fixture routes."""
        return self.routes.get(contract_id, ())

    async def invoke(self, call: voice.VoiceCall, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Deliver one simulated public reply, never an actual Pack execution."""
        self.calls.append((call, json.loads(json.dumps(payload))))
        contract = call.route.contract_id
        if contract == self.block_contract:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not self.ignore_cancel:
                    raise
                await self.release.wait()
        if self.raw_error:
            raise RuntimeError("Authorization: private-provider-key /private/user/path")
        if contract in {voice.CAPTURE, voice.OUTPUT}:
            if self.raw_media_response:
                return {"status": "ok", "approved": True, "artifact_id": "audio.fake"}
            return {
                "type": "host_intent",
                "version": 1,
                "operation": payload["operation"],
                "args": payload["arguments"],
            }
        if contract == voice.TRANSCRIBE:
            return {"status": "ok", "text": self.text}
        if contract == voice.SAVED:
            self._saved_result(payload["request"])
            return {"status": self.turn_status, "turn": self.turn}
        if contract == voice.TURN:
            return self.turn
        if contract == voice.CONVERSATION:
            return {"conversation": self.conversation}
        if contract == voice.SPEECH:
            return {"status": "ok", "artifact": {"artifact_id": "audio.speech"}}
        if contract == voice.STOP:
            return {
                "status": (
                    "stopped_confirmed" if self.stop_confirmed else "cancellation_requested"
                ),
                "stopped": self.stop_confirmed,
                "turn_id": payload["turn_id"],
            }
        raise AssertionError("unexpected contract")

    async def complete_media(
        self, call: voice.VoiceCall, intent: Mapping[str, Any]
    ) -> voice.VoiceMediaCompletion:
        """Produce a fixture Host completion, distinct from the intent itself."""
        assert intent["type"] == "host_intent"
        if call.route.contract_id == self.block_media_completion:
            self.started.set()
            await self.release.wait()
        status = self.media_status if call.route.contract_id == voice.CAPTURE else "played"
        return voice.VoiceMediaCompletion(
            "wrong.request" if self.completion_wrong_request else call.request_id,
            status,
            "audio.capture" if status == "captured" else None,
        )

    async def cancel_calls(self, scope: voice.VoiceScope, calls: Sequence[voice.VoiceCall]) -> bool:
        """Record drain requests without converting an unconfirmed stop to success."""
        assert scope.profile_id == self.profile_id
        self.drains.append(calls)
        return self.drain_confirmed

    def _saved_result(self, request: Mapping[str, Any]) -> None:
        turn_id = request["turn_id"]
        conversation_id = request["conversation_id"]
        user_id, assistant_id = (
            "message:" + canonical_digest([conversation_id, turn_id, role]).removeprefix("sha256:")
            for role in ("user", "assistant")
        )
        revision = request["conversation_revision"] + 2
        reference = {
            "conversation_id": conversation_id,
            "conversation_revision": revision,
            "user_message_id": user_id,
            "assistant_message_id": assistant_id,
            "outcome_digest": "sha256:" + "b" * 64,
        }
        self.turn = {
            "id": turn_id,
            "conversation_id": conversation_id,
            "conversation_revision": request["conversation_revision"],
            "request_id": "saved-turn."
            + canonical_digest({"profile_id": self.profile_id, "turn_id": turn_id}).removeprefix(
                "sha256:"
            ),
            "input_digest": canonical_digest({"request": request}),
            "revision": 3,
            "status": self.turn_status,
            "result_reference": reference,
        }
        message = {
            "id": assistant_id,
            "parent_id": user_id,
            "role": "assistant",
            "status": "complete",
            "metadata": {"turn_id": turn_id},
            "content": self.reply,
        }
        self.conversation = {
            "id": conversation_id,
            "conversation_revision": revision,
            "model_reference": "conversation.owner/model",
            "messages": [message],
        }
        if self.corrupt == "conversation":
            reference["conversation_id"] = "other.conversation"
        elif self.corrupt == "revision":
            reference["conversation_revision"] = revision - 1
        elif self.corrupt == "digest":
            reference["outcome_digest"] = "not-a-receipt"
        elif self.corrupt == "role":
            message["role"] = "tool"
        elif self.corrupt == "message_status":
            message["status"] = "running"
        elif self.corrupt == "message_id":
            reference["assistant_message_id"] = message["id"] = "wrong.message"
        elif self.corrupt == "request_id":
            self.turn["request_id"] = "other.request"
        elif self.corrupt == "input_digest":
            self.turn["input_digest"] = "sha256:" + "c" * 64


def make_session(port: Port, **kwargs: Any) -> voice.VoiceSession:
    """Create an exact captured fixture scope."""
    return voice.VoiceSession(
        voice.VoiceScope("defaults", "conversation.one", 5, port.plan_digest),
        port,
        **kwargs,
    )


def contracts(port: Port) -> list[str]:
    """List actual source port calls in execution order."""
    return [call.route.contract_id for call, _ in port.calls]


@pytest.mark.parametrize("fault", ["missing", "ambiguous", "version", "adapter", "scope"])
def test_unavailable_dependencies_never_invoke_or_fallback(fault: str) -> None:
    port = Port()
    if fault == "missing":
        port.routes[voice.TRANSCRIBE] = ()
    elif fault == "ambiguous":
        port.routes[voice.TRANSCRIBE] *= 2
    elif fault == "version":
        port.routes[voice.TRANSCRIBE] = (
            replace(port.routes[voice.TRANSCRIBE][0], contract_version="2.0.0"),
        )
    elif fault == "adapter":
        port.media_completion_available = False
    else:
        port.profile_id = "other.profile"
    session = make_session(port)
    assert session.snapshot()["phase"] == "unavailable"
    assert port.calls == []


def test_capture_and_cumulative_transcript_review_do_not_write_or_dispatch() -> None:
    async def scenario() -> None:
        port = Port()
        selection = {"mode": "manual", "include": ["subagent"]}
        session = make_session(port, language="ja-JP", tool_selection=selection)
        selection["include"].append("unexpected.tool")
        assert (await session.capture())["phase"] == "review"
        assert session.transcript == "Hello"
        session.edit_transcript("Edited")
        port.text = "Hello again"
        await session.capture()
        assert session.transcript == "Hello again"
        assert contracts(port) == [voice.CAPTURE, voice.TRANSCRIBE] * 2
        assert port.calls[0][1]["arguments"] == {"duration_ms": 45_000}
        assert port.calls[1][1] == {"artifact_id": "audio.capture", "language": "ja-JP"}
        await session.commit_turn()
        saved = next(
            payload for call, payload in port.calls if call.route.contract_id == voice.SAVED
        )
        assert saved["request"]["content"] == "Hello again"
        assert saved["request"]["tool_selection"] == {"mode": "manual", "include": ["subagent"]}
        assert "model_reference" not in saved["request"]
        assert "approved" not in json.dumps([payload for _, payload in port.calls])

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,code", [("silence", "no_speech"), ("denied", "media_permission_denied")]
)
def test_silence_and_permission_denial_never_call_stt(status: str, code: str) -> None:
    async def scenario() -> None:
        port = Port()
        port.media_status = status
        result = await make_session(port).capture()
        assert result["phase"] == "error"
        assert result["error"]["code"] == code
        assert contracts(port) == [voice.CAPTURE]

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["raw_response", "wrong_completion"])
def test_host_intent_and_client_approval_are_not_actual_media_completion(fault: str) -> None:
    async def scenario() -> None:
        port = Port()
        port.raw_media_response = fault == "raw_response"
        port.completion_wrong_request = fault == "wrong_completion"
        result = await make_session(port).capture()
        assert result["phase"] == "error"
        assert contracts(port) == [voice.CAPTURE]
        assert port.drains

    asyncio.run(scenario())


def test_late_capture_after_cancel_is_ignored_and_never_transcribed() -> None:
    async def scenario() -> None:
        port = Port()
        port.block_contract = voice.CAPTURE
        port.ignore_cancel = True
        session = make_session(port)
        work = asyncio.create_task(session.capture())
        await asyncio.wait_for(port.started.wait(), timeout=1)
        assert (await session.cancel())["phase"] == "cancelled"
        port.release.set()
        await work
        assert session.transcript == ""
        assert contracts(port) == [voice.CAPTURE]
        assert port.drains[0][0].generation < session.generation

    asyncio.run(scenario())


def test_scope_change_during_stt_drains_capture_and_rejects_late_text() -> None:
    async def scenario() -> None:
        port = Port()
        port.block_contract = voice.TRANSCRIBE
        session = make_session(port)
        work = asyncio.create_task(session.capture())
        await asyncio.wait_for(port.started.wait(), timeout=1)
        port.valid = False
        port.release.set()
        await work
        assert session.transcript == ""
        assert port.drains
        assert session.phase == "cancelled"

    asyncio.run(scenario())


def test_waiting_turn_reconciles_only_and_never_resends_or_speaks() -> None:
    async def scenario() -> None:
        port = Port()
        port.turn_status = "waiting"
        session = make_session(port)
        await session.capture()
        assert (await session.commit_turn())["phase"] == "waiting"
        with pytest.raises(ValueError):
            await session.commit_turn()
        with pytest.raises(ValueError):
            await session.capture()
        await session.reconcile_turn()
        assert contracts(port) == [voice.CAPTURE, voice.TRANSCRIBE, voice.SAVED, voice.TURN]
        assert session.reply == ""

    asyncio.run(scenario())


def test_authoritative_completed_answer_gates_bounded_synthesis_and_output() -> None:
    async def scenario() -> None:
        port = Port()
        port.reply = " ".join(f"SAY: segment{i}" for i in range(10)) + " CONTINUE"
        session = make_session(port)
        await session.capture()
        assert (await session.commit_turn())["phase"] == "reply_ready"
        assert contracts(port) == [voice.CAPTURE, voice.TRANSCRIBE, voice.SAVED, voice.CONVERSATION]
        await session.play_reply()
        assert contracts(port).count(voice.SPEECH) == 6
        assert contracts(port).count(voice.OUTPUT) == 6
        assert contracts(port).count(voice.CAPTURE) == 1
        assert session.phase == "reply_ready"
        assert session.conversation_revision == 7

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "corrupt",
    [
        "conversation",
        "revision",
        "digest",
        "role",
        "message_status",
        "message_id",
        "request_id",
        "input_digest",
    ],
)
def test_invalid_owner_receipt_or_answer_never_becomes_spoken_output(corrupt: str) -> None:
    async def scenario() -> None:
        port = Port()
        port.corrupt = corrupt
        session = make_session(port)
        await session.capture()
        await session.commit_turn()
        assert session.phase == "waiting"
        assert session.reply == ""
        assert voice.SPEECH not in contracts(port)
        assert voice.OUTPUT not in contracts(port)

    asyncio.run(scenario())


def test_interrupt_playback_requires_drain_and_stops_remaining_segments() -> None:
    async def scenario() -> None:
        port = Port()
        session = make_session(port)
        await session.capture()
        await session.commit_turn()
        port.block_media_completion = voice.OUTPUT
        port.drain_confirmed = False
        work = asyncio.create_task(session.play_reply())
        await asyncio.wait_for(port.started.wait(), timeout=1)
        assert session.phase == "awaiting_approval"
        result = await session.interrupt()
        await work
        assert result["phase"] == "cancelling"
        assert contracts(port).count(voice.SPEECH) == 1
        assert contracts(port).count(voice.OUTPUT) == 1
        assert port.drains[0][0].route.contract_id == voice.OUTPUT

    asyncio.run(scenario())


def test_interrupt_during_synthesis_invalidates_late_tts_and_preserves_history() -> None:
    async def scenario() -> None:
        port = Port()
        session = make_session(port)
        await session.capture()
        await session.commit_turn()
        original = json.dumps(port.conversation)
        port.block_contract = voice.SPEECH
        port.ignore_cancel = True
        work = asyncio.create_task(session.play_reply())
        await asyncio.wait_for(port.started.wait(), timeout=1)
        assert (await session.interrupt())["phase"] == "cancelled"
        port.release.set()
        await work
        assert voice.OUTPUT not in contracts(port)
        assert json.dumps(port.conversation) == original
        assert voice.STOP not in contracts(port)

    asyncio.run(scenario())


@pytest.mark.parametrize("confirmed", [True, False])
def test_cancel_running_turn_requires_authenticated_stop_confirmation(confirmed: bool) -> None:
    async def scenario() -> None:
        port = Port()
        port.block_contract = voice.SAVED
        port.ignore_cancel = True
        port.stop_confirmed = confirmed
        session = make_session(port)
        await session.capture()
        work = asyncio.create_task(session.commit_turn())
        await asyncio.wait_for(port.started.wait(), timeout=1)
        result = await session.cancel()
        assert result["phase"] == ("cancelled" if confirmed else "cancelling")
        stop = next(payload for call, payload in port.calls if call.route.contract_id == voice.STOP)
        assert stop == {"turn_id": session.turn_id}
        port.release.set()
        await work
        assert session.reply == ""
        assert voice.CONVERSATION not in contracts(port)

    asyncio.run(scenario())


def test_network_failure_is_sanitized_and_unconfirmed_drain_blocks_restart() -> None:
    async def scenario() -> None:
        port = Port()
        port.raw_error = True
        port.drain_confirmed = False
        session = make_session(port)
        result = await session.capture()
        assert result["phase"] == "cancelling"
        assert "private-provider-key" not in json.dumps(result)
        assert "/private" not in json.dumps(result)
        with pytest.raises(ValueError):
            await session.capture()

    asyncio.run(scenario())


def test_timeout_remains_finite_and_never_makes_stt_from_unfinished_capture() -> None:
    async def scenario() -> None:
        port = Port()
        port.block_contract = voice.CAPTURE
        session = make_session(port, operation_timeout=0.01)
        result = await session.capture()
        assert result["phase"] == "error"
        assert result["error"]["code"] == "voice_operation_timeout"
        assert contracts(port) == [voice.CAPTURE]
        assert port.drains

    asyncio.run(scenario())


def test_cancelling_task_also_requests_host_drain() -> None:
    async def scenario() -> None:
        port = Port()
        port.block_contract = voice.CAPTURE
        session = make_session(port)
        work = asyncio.create_task(session.capture())
        await asyncio.wait_for(port.started.wait(), timeout=1)
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert session.phase == "cancelled"
        assert port.drains

    asyncio.run(scenario())


def test_concurrent_capture_cannot_open_a_second_device_operation() -> None:
    async def scenario() -> None:
        port = Port()
        port.block_contract = voice.CAPTURE
        session = make_session(port)
        work = asyncio.create_task(session.capture())
        await asyncio.wait_for(port.started.wait(), timeout=1)
        with pytest.raises(ValueError):
            await session.capture()
        await session.cancel()
        await work
        assert contracts(port) == [voice.CAPTURE]

    asyncio.run(scenario())


def test_call_and_turn_budgets_reject_before_additional_effects() -> None:
    async def scenario() -> None:
        port = Port()
        session = make_session(port)
        session._calls = 20
        result = await session.capture()
        assert result["error"]["code"] == "voice_call_limit"
        assert port.calls == []
        session._calls = 0
        await session.capture()
        session._turns = 4
        result = await session.commit_turn()
        assert result["error"]["code"] == "voice_turn_limit"
        assert voice.SAVED not in contracts(port)

    asyncio.run(scenario())


def test_session_metadata_declares_only_public_staged_dependency_ports() -> None:
    path = Path(voice.__file__).with_name("session.ports.v1.json")
    metadata = json.loads(path.read_text())
    assert metadata["schema"] == voice.PORT_API
    assert metadata["status"] == "staged_adapter_unavailable"
    assert {item["contract_id"] for item in metadata["requirements"]} == set(
        voice.REQUIRED_CONTRACTS
    )
    assert all(item["version_range"] == ">=1.0.0,<2.0.0" for item in metadata["requirements"])
    assert metadata["media_completion"]["host_intent_is_completion"] is False
