"""Finite voice sessions over captured public contract ports.

The port is supplied by an admitted Host adapter, never by request JSON. This
source controller owns no microphone, speaker, provider transport, credentials,
conversation store, or tool executor. Its media completion and cancellation
port still need an admitted implementation before this Pack can be enabled.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Awaitable, Mapping, TypeVar

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input
from tobkiri_protocol.saved_tools import validate_tool_selection

from .ports import (
    CAPTURE,
    CONVERSATION,
    OUTPUT,
    PORT_API,
    REQUIRED_CONTRACTS,
    SAVED,
    SPEECH,
    STOP,
    TRANSCRIBE,
    TURN,
    VoiceCall,
    VoiceMediaCompletion,
    VoiceRoute,
    VoiceScope,
    VoiceSessionPort,
    _LANGUAGE,
    _VoiceFailure,
    _identifier,
    _required_text,
    _valid_route,
)
from .protocol import speech_plan
from .receipts import _completed_reply, _valid_reference

_MAX_CALLS = 20
_MAX_TURNS = 4
_MAX_LIFETIME = 600.0
_ERROR_MESSAGE = "Voice operation did not complete. Check the conversation or retry."
T = TypeVar("T")


class _StaleVoiceOperation(Exception):
    """Internal control flow for a cancelled or changed captured session."""


class VoiceSession:
    """Run reviewed voice turns with bounded public calls and scoped cancellation."""

    def __init__(
        self,
        scope: VoiceScope,
        port: VoiceSessionPort,
        *,
        language: str | None = None,
        operation_timeout: float = 60.0,
        tool_selection: dict[str, Any] | None = None,
    ) -> None:
        """Capture routes once; missing, ambiguous, or incompatible ports stay closed."""
        if language is not None and not _LANGUAGE.fullmatch(language):
            raise ValueError("voice language is invalid")
        if (
            isinstance(operation_timeout, bool)
            or not isinstance(operation_timeout, (int, float))
            or not 0 < operation_timeout <= 300
        ):
            raise ValueError("voice operation deadline is invalid")
        self.scope = scope
        self.port = port
        self.language = language
        self.operation_timeout = float(operation_timeout)
        # Discovery preferences are captured locally and validated by the public
        # saved-turn protocol. They never supply approval or execution authority.
        self._tool_selection = validate_tool_selection(
            tool_selection if tool_selection is not None else {"mode": "none"}
        )
        self.session_id = f"voice.{secrets.token_hex(12)}"
        self.generation = 0
        self.phase = "idle"
        self.transcript = ""
        self.reply = ""
        self.error_code: str | None = None
        self.turn_id: str | None = None
        self.conversation_revision = scope.conversation_revision
        self._turns = 0
        self._calls = 0
        self._pending: dict[str, VoiceCall] = {}
        self._active_tasks: set[asyncio.Future[Any]] = set()
        self._routes: dict[str, VoiceRoute] = {}
        self._turn_unconfirmed = False
        self._turn_revision = 0
        self._saved_input_digest: str | None = None
        self._started: float | None = None
        self._busy = False
        try:
            if (
                port.api_version != PORT_API
                or port.profile_id != scope.profile_id
                or port.plan_digest != scope.plan_digest
                or not port.media_completion_available
                or not port.cancellation_available
            ):
                raise _VoiceFailure("voice_adapter_unavailable")
            port.assert_current(scope)
            for contract_id in REQUIRED_CONTRACTS:
                routes = tuple(port.providers(contract_id))
                if len(routes) != 1 or not _valid_route(routes[0], contract_id):
                    raise _VoiceFailure("voice_dependency_unavailable")
                self._routes[contract_id] = routes[0]
        except Exception:
            self.phase = "unavailable"
            self.error_code = "voice_dependency_unavailable"

    def snapshot(self) -> dict[str, Any]:
        """Return finite display state without raw media, credentials, or error bodies."""
        return {
            "schema": "io.tobkiri.voice-session.snapshot.v1",
            "session_id": self.session_id,
            "profile_id": self.scope.profile_id,
            "conversation_id": self.scope.conversation_id,
            "generation": self.generation,
            "phase": self.phase,
            "transcript": self.transcript,
            "reply": self.reply,
            "turn_id": self.turn_id,
            "error": (
                {"code": self.error_code, "message": _ERROR_MESSAGE} if self.error_code else None
            ),
        }

    async def capture(self) -> dict[str, Any]:
        """Request a finite approved capture and replace cumulative STT at review."""
        self._enter({"idle", "review", "reply_ready", "error"})
        generation = self.generation
        try:
            self.phase = "capturing"
            outcome = await self._media(
                CAPTURE,
                {
                    "operation": "host.microphone.capture",
                    "arguments": {"duration_ms": 45_000},
                    "context": {"conversation_id": self.scope.conversation_id},
                },
                generation,
            )
            if outcome.status == "silence":
                raise _VoiceFailure("no_speech")
            if outcome.status != "captured" or not _identifier(outcome.artifact_id):
                raise _VoiceFailure("capture_not_completed")
            self.phase = "transcribing"
            result = await self._invoke(
                TRANSCRIBE,
                {"artifact_id": outcome.artifact_id, "language": self.language},
                generation,
            )
            text = _required_text(result.get("text"))
            # A cumulative STT response replaces its predecessor; it is never
            # appended to the composer draft or previous cumulative transcript.
            self.transcript = text
            self.reply = ""
            self.error_code = None
            self.phase = "review"
        except _StaleVoiceOperation:
            await self._drain_stale()
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception as error:
            await self._fail(error, generation)
        finally:
            self._busy = False
        return self.snapshot()

    def edit_transcript(self, text: str) -> None:
        """Edit only the separate reviewed transcript, before any saved dispatch."""
        self._assert_current(self.generation)
        if self.phase != "review" or self._busy:
            raise ValueError("voice transcript is not editable")
        self.transcript = _required_text(text)

    async def commit_turn(self) -> dict[str, Any]:
        """Dispatch a reviewed utterance once through the saved turn owner."""
        self._enter({"review"})
        generation = self.generation
        try:
            if self._turns >= _MAX_TURNS:
                raise _VoiceFailure("voice_turn_limit")
            content = _required_text(self.transcript)
            self._turns += 1
            self.turn_id = f"{self.session_id}.turn.{self._turns}"
            self._turn_unconfirmed = True
            self.phase = "dispatching"
            payload = validate_saved_conversation_input(
                {
                    "request": {
                        "turn_id": self.turn_id,
                        "conversation_id": self.scope.conversation_id,
                        "conversation_revision": self.conversation_revision,
                        "content": content,
                        "tool_selection": self._tool_selection,
                    }
                }
            )
            self._saved_input_digest = canonical_digest(payload)
            self._turn_revision = 0
            result = await self._invoke(SAVED, payload, generation)
            await self._observe_turn(result.get("turn"), generation)
        except _StaleVoiceOperation:
            await self._drain_stale()
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception as error:
            await self._fail(error, generation)
        finally:
            self._busy = False
        return self.snapshot()

    async def reconcile_turn(self) -> dict[str, Any]:
        """Read an existing turn only; waiting or lost replies never trigger re-send."""
        self._enter({"waiting"})
        generation = self.generation
        try:
            result = await self._invoke(
                TURN,
                {
                    "profile_id": self.scope.profile_id,
                    "operation": "get",
                    "turn_id": self.turn_id,
                },
                generation,
            )
            await self._observe_turn(result, generation)
        except _StaleVoiceOperation:
            await self._drain_stale()
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception as error:
            await self._fail(error, generation)
        finally:
            self._busy = False
        return self.snapshot()

    async def play_reply(self) -> dict[str, Any]:
        """Synthesize and play only a confirmed answer, with finite directive bounds."""
        self._enter({"reply_ready"})
        generation = self.generation
        try:
            plan = speech_plan(self.reply)
            for segment in plan["segments"]:
                delay = float(segment["delay"])
                if delay:
                    await self._bounded(asyncio.sleep(delay), generation, timeout=delay + 1)
                self.phase = "synthesizing"
                speech = await self._invoke(SPEECH, {"text": segment["text"]}, generation)
                artifact = speech.get("artifact")
                artifact_id = artifact.get("artifact_id") if isinstance(artifact, Mapping) else None
                if not _identifier(artifact_id):
                    raise _VoiceFailure("speech_result_invalid")
                self.phase = "playing"
                outcome = await self._media(
                    OUTPUT,
                    {
                        "operation": "host.audio.output",
                        "arguments": {"artifact_id": artifact_id},
                        "context": {"conversation_id": self.scope.conversation_id},
                    },
                    generation,
                )
                if outcome.status != "played":
                    raise _VoiceFailure("playback_not_completed")
            self.phase = "finished" if plan["finish"] else "reply_ready"
            # CONTINUE is data, never an automatic microphone/model/tool loop.
        except _StaleVoiceOperation:
            await self._drain_stale()
        except asyncio.CancelledError:
            await self.cancel()
            raise
        except Exception as error:
            await self._fail(error, generation)
        finally:
            self._busy = False
        return self.snapshot()

    async def cancel(self) -> dict[str, Any]:
        """Invalidate all late results and request authenticated drain and turn stop."""
        self.generation += 1
        generation = self.generation
        self.phase = "cancelling"
        calls = tuple(self._pending.values())
        for task in tuple(self._active_tasks):
            task.cancel()
        drained = False
        try:
            drained = await self._bounded(
                self.port.cancel_calls(self.scope, calls), generation, check_scope=False
            )
            if drained is not True:
                drained = False
            if self._turn_unconfirmed and self.turn_id:
                stop = await self._invoke(STOP, {"turn_id": self.turn_id}, generation)
                drained = drained and (
                    stop.get("status") == "stopped_confirmed"
                    and stop.get("turn_id") == self.turn_id
                    and stop.get("stopped") is True
                )
        except Exception:
            drained = False
        if generation == self.generation:
            self.phase = "cancelled" if drained else "cancelling"
            self.error_code = None if drained else "voice_drain_unconfirmed"
            if drained:
                self._pending.clear()
                self._turn_unconfirmed = False
        return self.snapshot()

    async def interrupt(self) -> dict[str, Any]:
        """Stop current work without editing history or granting a new capture."""
        return await self.cancel()

    def _enter(self, phases: set[str]) -> None:
        self._assert_current(self.generation)
        if self._busy or self.phase not in phases or self._turn_unconfirmed:
            if self.phase == "waiting" and phases == {"waiting"} and not self._busy:
                self._busy = True
                return
            raise ValueError("voice session operation is unavailable")
        self._busy = True
        if self._started is None:
            self._started = time.monotonic()

    def _assert_current(self, generation: int, *, check_scope: bool = True) -> None:
        if generation != self.generation:
            raise _StaleVoiceOperation()
        if check_scope:
            try:
                self.port.assert_current(self.scope)
            except Exception:
                self.generation += 1
                self.phase = "cancelling"
                self.error_code = "voice_scope_changed"
                raise _StaleVoiceOperation() from None
        if (
            check_scope
            and self._started is not None
            and (time.monotonic() - self._started >= _MAX_LIFETIME)
        ):
            raise _VoiceFailure("voice_session_expired")

    async def _bounded(
        self,
        operation: Awaitable[T],
        generation: int,
        *,
        check_scope: bool = True,
        timeout: float | None = None,
    ) -> T:
        task = asyncio.ensure_future(operation)
        self._active_tasks.add(task)
        try:
            self._assert_current(generation, check_scope=check_scope)
            remaining = (
                _MAX_LIFETIME - (time.monotonic() - self._started)
                if check_scope and self._started is not None
                else self.operation_timeout
            )
            done, _ = await asyncio.wait(
                {task}, timeout=min(timeout or self.operation_timeout, remaining)
            )
            self._assert_current(generation, check_scope=check_scope)
            if not done:
                raise _VoiceFailure("voice_operation_timeout")
            return task.result()
        finally:
            self._active_tasks.discard(task)
            if not task.done():
                task.cancel()
            # Do not wait indefinitely for a provider which ignores cancellation.
            # The Host drain port must confirm stop before this session can close.
            task.add_done_callback(_consume_task_result)

    def _call(self, contract_id: str, generation: int) -> VoiceCall:
        self._assert_current(generation)
        if self._calls >= _MAX_CALLS and contract_id != STOP:
            raise _VoiceFailure("voice_call_limit")
        self._calls += 1
        call = VoiceCall(
            self.scope,
            self.session_id,
            generation,
            f"{self.session_id}.call.{self._calls}",
            self._routes[contract_id],
        )
        self._pending[call.request_id] = call
        return call

    async def _invoke(
        self, contract_id: str, payload: Mapping[str, Any], generation: int
    ) -> Mapping[str, Any]:
        call = self._call(contract_id, generation)
        result = await self._bounded(self.port.invoke(call, payload), generation)
        if not isinstance(result, Mapping):
            raise _VoiceFailure("voice_response_invalid")
        self._pending.pop(call.request_id, None)
        return result

    async def _media(
        self, contract_id: str, payload: Mapping[str, Any], generation: int
    ) -> VoiceMediaCompletion:
        call = self._call(contract_id, generation)
        result = await self._bounded(self.port.invoke(call, payload), generation)
        if (
            not isinstance(result, Mapping)
            or result.get("type") != "host_intent"
            or type(result.get("version")) is not int
            or result["version"] != 1
        ):
            raise _VoiceFailure("media_authority_intent_required")
        if result.get("operation") != payload["operation"]:
            raise _VoiceFailure("media_authority_intent_invalid")
        self.phase = "awaiting_approval"
        outcome = await self._bounded(self.port.complete_media(call, result), generation)
        if not isinstance(outcome, VoiceMediaCompletion) or outcome.request_id != call.request_id:
            raise _VoiceFailure("media_completion_invalid")
        self._pending.pop(call.request_id, None)
        if outcome.status == "denied":
            raise _VoiceFailure("media_permission_denied")
        return outcome

    async def _observe_turn(self, value: Any, generation: int) -> None:
        if (
            not isinstance(value, Mapping)
            or value.get("id") != self.turn_id
            or value.get("conversation_id") != self.scope.conversation_id
            or value.get("conversation_revision") != self.conversation_revision
            or type(value.get("conversation_revision")) is not int
            or value.get("request_id")
            != "saved-turn."
            + canonical_digest(
                {"profile_id": self.scope.profile_id, "turn_id": self.turn_id}
            ).removeprefix("sha256:")
            or value.get("input_digest") != self._saved_input_digest
            or type(value.get("revision")) is not int
            or value["revision"] < max(1, self._turn_revision)
        ):
            raise _VoiceFailure("voice_turn_invalid")
        self._turn_revision = value["revision"]
        status = value.get("status")
        if status in {"running", "waiting", "waiting_approval", "awaiting_user"}:
            self.phase = "waiting"
            return
        if status in {"failed", "cancelled"}:
            self._turn_unconfirmed = False
            self.phase = "cancelled" if status == "cancelled" else "error"
            self.error_code = "voice_turn_failed" if status == "failed" else None
            return
        reference = value.get("result_reference")
        if status != "completed" or not _valid_reference(
            reference, self.scope.conversation_id, self.conversation_revision
        ):
            raise _VoiceFailure("voice_turn_unconfirmed")
        conversation = await self._invoke(
            CONVERSATION,
            {
                "profile_id": self.scope.profile_id,
                "operation": "get",
                "conversation_id": self.scope.conversation_id,
            },
            generation,
        )
        self.reply = _completed_reply(conversation, reference, self.turn_id)
        self.conversation_revision = reference["conversation_revision"]
        self._turn_unconfirmed = False
        self.error_code = None
        self.phase = "reply_ready"

    async def _fail(self, error: Exception, generation: int) -> None:
        if generation != self.generation:
            return
        self.error_code = (
            error.code if isinstance(error, _VoiceFailure) else "voice_operation_failed"
        )
        if self._pending:
            try:
                drained = await self._bounded(
                    self.port.cancel_calls(self.scope, tuple(self._pending.values())),
                    generation,
                    check_scope=False,
                )
            except Exception:
                drained = False
            if generation != self.generation:
                return
            if drained is not True:
                self.phase = "cancelling"
                self.error_code = "voice_drain_unconfirmed"
                return
            self._pending.clear()
        # A saved turn may have committed before the transport failed. It stays
        # reconcilable, and cannot be re-sent or spoken until the owner confirms.
        self.phase = "waiting" if self._turn_unconfirmed else "error"

    async def _drain_stale(self) -> None:
        if self.error_code == "voice_scope_changed":
            await self.cancel()


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()
