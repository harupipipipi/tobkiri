"""Bounded media capture/output HostIntents for the Viewer broker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final


CAPTURE_LIMITS_MS: Final[dict[str, int]] = {
    "host.screen.capture": 0,
    "host.microphone.capture": 300_000,
    "host.audio.capture": 300_000,
    "host.camera.capture": 300_000,
}
OUTPUT_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"host.audio.output", "host.speech.synthesize"}
)
_FORBIDDEN_ARGUMENTS: Final[frozenset[str]] = frozenset(
    {"approved", "approval_token", "authority_token", "viewer_host_approved", "yolo_mode"}
)


@dataclass(frozen=True)
class MediaHostService:
    """Build media HostIntents without opening devices or persisting payloads."""

    access: str
    operations: frozenset[str]

    def invoke(
        self,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a bounded caller-bound HostIntent or typed denial."""

        normalized_operation = str(operation or "").strip()
        if normalized_operation not in self.operations:
            return _denied(
                "operation_outside_media_contract",
                operation=normalized_operation,
                access=self.access,
            )
        normalized_arguments = dict(arguments or {})
        forbidden = sorted(_FORBIDDEN_ARGUMENTS.intersection(normalized_arguments))
        if forbidden:
            return _denied(
                "client_authority_material_forbidden",
                forbidden_arguments=forbidden,
            )
        if normalized_operation in CAPTURE_LIMITS_MS:
            limit = CAPTURE_LIMITS_MS[normalized_operation]
            duration = normalized_arguments.get("duration_ms", 30_000 if limit else 0)
            if isinstance(duration, bool) or not isinstance(duration, int):
                return _denied("invalid_capture_duration")
            if duration < 0 or (limit and duration > limit):
                return _denied("capture_duration_out_of_range", max_duration_ms=limit)
            if limit:
                normalized_arguments["duration_ms"] = duration
        caller_context = dict(context or {})
        normalized_arguments.pop("_contract_consumer_pack_id", None)
        normalized_arguments.pop(
            "_contract_consumer_function_id",
            normalized_arguments.pop("_source_function_id", ""),
        )
        delegated_screen_capture = normalized_operation == "host.screen.capture"
        return {
            "type": "host_intent",
            "version": 1,
            "operation": (
                "host.intent.execute" if delegated_screen_capture else normalized_operation
            ),
            "args": normalized_arguments,
            "stream": {"enabled": False},
            "reason": str(caller_context.get("reason") or "").strip(),
            "caller": {
                "pack_id": "",
                "function_id": "",
            },
            "conversation_id": str(
                caller_context.get("conversation_id") or ""
            ).strip(),
            "host_function_id": (
                "computer.screenshot"
                if delegated_screen_capture
                else f"media.{self.access}"
            ),
        }


def create_media_capture(_context: dict[str, Any] | None = None) -> MediaHostService:
    """Create the screen, microphone, audio, and camera capture provider."""

    return MediaHostService(
        access="capture",
        operations=frozenset(CAPTURE_LIMITS_MS),
    )


def create_media_output(_context: dict[str, Any] | None = None) -> MediaHostService:
    """Create the audio and synthesized-speech output provider."""

    return MediaHostService(access="output", operations=OUTPUT_OPERATIONS)


def _denied(error_type: str, **details: Any) -> dict[str, Any]:
    return {
        "status": "denied",
        "success": False,
        "error_type": error_type,
        **details,
    }

