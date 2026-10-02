"""Versioned finite ports and values for a captured Voice session.

These declarations are source-only. An admitted Host implementation must own
actual media completion and cancellation; request JSON cannot supply the port.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Protocol, Sequence

PORT_API = "io.tobkiri.voice-session.port.v1"
CAPTURE = "tobkiri.action.media.capture.v1"
TRANSCRIBE = "tobkiri.service.ai.audio.transcribe.v1"
SAVED = "tobkiri.action.turn.saved.v1"
TURN = "tobkiri.resource.turn.v1"
CONVERSATION = "tobkiri.resource.conversation.v1"
SPEECH = "tobkiri.service.ai.audio.speech.v1"
OUTPUT = "tobkiri.action.media.output.v1"
STOP = "tobkiri.action.turn.stop.v1"
REQUIRED_CONTRACTS = (CAPTURE, TRANSCRIBE, SAVED, TURN, CONVERSATION, SPEECH, OUTPUT, STOP)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VERSION = re.compile(r"1\.[0-9]+\.[0-9]+\Z")
_LANGUAGE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,2}\Z")
_MAX_TEXT_BYTES = 16_384


@dataclass(frozen=True)
class VoiceScope:
    """Immutable conversation and activation identity captured by the Host."""

    profile_id: str
    conversation_id: str
    conversation_revision: int
    plan_digest: str

    def __post_init__(self) -> None:
        """Reject malformed identities before resolving any dependency."""
        if (
            not _identifier(self.profile_id)
            or not _identifier(self.conversation_id)
            or type(self.conversation_revision) is not int
            or self.conversation_revision < 1
            or not _digest(self.plan_digest)
        ):
            raise ValueError("voice capture scope is invalid")


@dataclass(frozen=True)
class VoiceRoute:
    """One exact public provider binding from the captured activation."""

    contract_id: str
    contract_version: str
    operation_id: str
    provider_instance_id: str


@dataclass(frozen=True)
class VoiceCall:
    """A call identity, retained locally and never accepted as continuation JSON."""

    scope: VoiceScope
    session_id: str
    generation: int
    request_id: str
    route: VoiceRoute


@dataclass(frozen=True)
class VoiceMediaCompletion:
    """Host-observed media outcome delivered only by the captured port.

    A HostIntent, client approval flag, or raw provider response cannot be used
    in place of this outcome. The Host adapter must verify the actual broker
    operation before constructing it, including the request's exact identity.
    """

    request_id: str
    status: str
    artifact_id: str | None = None


class VoiceSessionPort(Protocol):
    """Finite adapter ABI; implementations retain Host authority and drain handles."""

    api_version: str
    profile_id: str
    plan_digest: str
    media_completion_available: bool
    cancellation_available: bool

    def assert_current(self, scope: VoiceScope) -> None:
        """Reject stale activation, navigation, cancellation, or expired authority."""

    def providers(self, contract_id: str) -> Sequence[VoiceRoute]:
        """Return only bindings from this captured activation."""

    async def invoke(self, call: VoiceCall, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Invoke the exact public route under its normal policy and approval path."""

    async def complete_media(
        self, call: VoiceCall, intent: Mapping[str, Any]
    ) -> VoiceMediaCompletion:
        """Observe the matching broker operation; never grant permission here."""

    async def cancel_calls(self, scope: VoiceScope, calls: Sequence[VoiceCall]) -> bool:
        """Stop capture/playback/transport and return true only after verified drain."""


class _VoiceFailure(Exception):
    """Internal finite failure without provider or authority details."""

    def __init__(self, code: str) -> None:
        self.code = code


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _valid_route(value: Any, contract_id: str) -> bool:
    return (
        isinstance(value, VoiceRoute)
        and value.contract_id == contract_id
        and _VERSION.fullmatch(value.contract_version) is not None
        and _identifier(value.operation_id)
        and _identifier(value.provider_instance_id)
    )


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _VoiceFailure("no_speech")
    if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise _VoiceFailure("voice_text_limit")
    return value.strip()
