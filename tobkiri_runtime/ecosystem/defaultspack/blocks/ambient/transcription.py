from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from blocks._common import error, ok
from domain.ambient.materialization import materialize_ambient_event_attachments
from domain.ambient.transcription import transcribe_ambient_audio


MAX_AUDIO_DATA_URL_CHARS = 36 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024
_AUDIO_DATA_KEYS = frozenset(
    {
        "audio_data_url",
        "audioDataUrl",
        "audio",
        "data_url",
        "dataUrl",
    }
)
_MIME_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")


def _failure(message: str, code: str) -> dict[str, Any]:
    """Return a stable validation envelope without invoking transcription."""
    return error(message, code)


def _normalized_audio_mime(value: Any) -> str:
    """Return a validated base audio media type, excluding parameters."""
    mime_type = str(value or "").strip().lower().split(";", 1)[0].strip()
    if not _MIME_TYPE_RE.fullmatch(mime_type) or not mime_type.startswith("audio/"):
        return ""
    return mime_type


def _decode_audio_data_url(value: str) -> tuple[str, int] | tuple[None, None]:
    """Validate an audio-only base64 data URL and return its decoded size."""
    if not value.startswith("data:") or "," not in value:
        return None, None
    header, encoded = value.split(",", 1)
    header_parts = header[5:].split(";")
    if len(header_parts) < 2 or header_parts[-1].strip().lower() != "base64":
        return None, None
    mime_type = _normalized_audio_mime(header_parts[0])
    if not mime_type or not encoded:
        return None, None
    try:
        decoded_size = len(base64.b64decode(encoded, validate=True))
    except (ValueError, binascii.Error):
        return None, None
    return mime_type, decoded_size


def _payload_has_nested_media(payload: dict[str, Any]) -> bool:
    """Reject attachment/media side channels outside the one composer recording.

    This endpoint is intentionally a one-recording operation.  Accepting
    arbitrary nested attachments would allow extra (and potentially oversized)
    media to reach the generic ambient materializer without this route's byte
    accounting.
    """

    def visit(value: Any, path: tuple[str, ...]) -> bool:
        if isinstance(value, dict):
            for raw_key, child in value.items():
                key = str(raw_key)
                child_path = path + (key,)
                if key == "attachments" and child:
                    return True
                if key in _AUDIO_DATA_KEYS:
                    # The single top-level canonical field is validated
                    # separately.  Aliases and nested media are not accepted.
                    if child_path != ("audio_data_url",):
                        return True
                if visit(child, child_path):
                    return True
            return False
        if isinstance(value, list):
            return any(visit(child, path + ("[]",)) for child in value)
        return False

    return visit(payload, ())


def _validated_transcription_payload(
    input_data: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Validate and minimize the one-audio transcription request contract."""
    payload = dict(input_data if isinstance(input_data, dict) else {})
    audio_data_url = payload.get("audio_data_url")
    if not isinstance(audio_data_url, str) or not audio_data_url.strip():
        return None, _failure("audio_data_url is required", "AUDIO_PAYLOAD_MISSING")
    audio_data_url = audio_data_url.strip()
    if len(audio_data_url) > MAX_AUDIO_DATA_URL_CHARS:
        return None, _failure(
            "recorded audio is too large to transcribe", "AUDIO_PAYLOAD_TOO_LARGE"
        )
    if _payload_has_nested_media(payload):
        return None, _failure(
            "composer transcription accepts exactly one top-level audio recording",
            "AUDIO_PAYLOAD_INVALID",
        )
    mime_type, decoded_size = _decode_audio_data_url(audio_data_url)
    if not mime_type or decoded_size is None:
        return None, _failure(
            "audio_data_url must be a valid base64 audio data URL",
            "AUDIO_PAYLOAD_INVALID",
        )
    if decoded_size > MAX_AUDIO_BYTES:
        return None, _failure(
            "recorded audio is too large to transcribe", "AUDIO_PAYLOAD_TOO_LARGE"
        )

    declared_mime = payload.get("audio_mime_type")
    if declared_mime not in (None, ""):
        normalized_declared_mime = _normalized_audio_mime(declared_mime)
        if not normalized_declared_mime or normalized_declared_mime != mime_type:
            return None, _failure(
                "audio_mime_type must match the audio data URL media type",
                "AUDIO_PAYLOAD_INVALID",
            )
    declared_size = payload.get("audio_size")
    if declared_size not in (None, ""):
        try:
            if int(declared_size) < 0:
                raise ValueError
        except (TypeError, ValueError):
            return None, _failure("audio_size must be a non-negative integer", "AUDIO_PAYLOAD_INVALID")

    # Do not forward arbitrary event metadata or attachments into the ambient
    # path.  It remains a transient, dispatch-free single-media operation.
    params = payload.get("params")
    safe_payload: dict[str, Any] = {
        "audio_data_url": audio_data_url,
        "audio_mime_type": mime_type,
        "audio_size": decoded_size,
        "audio_name": str(payload.get("audio_name") or "composer-recording.webm")[:255],
        "model": str(payload.get("model") or "")[:512],
        "profile_id": str(payload.get("profile_id") or "")[:512],
        "params": dict(params) if isinstance(params, dict) else {},
    }
    return safe_payload, None


def run(input_data: dict[str, Any], context: dict[str, Any] | None = None):
    """Transcribe one transient composer recording without ambient dispatch.

    The ambient event endpoint is intentionally protected because it can dispatch
    agent actions.  Composer transcription only needs the narrower media
    operation, so it has a separate same-origin API route and never persists the
    supplied audio.
    """

    del context
    payload, validation_error = _validated_transcription_payload(input_data)
    if validation_error is not None:
        return validation_error
    assert payload is not None

    attachments = materialize_ambient_event_attachments(
        payload,
        event_id=str(payload.get("event_id") or "composer_audio_transcription"),
    )
    params = dict(payload.get("params") if isinstance(payload.get("params"), dict) else {})
    target_model_ref = str(
        payload.get("model")
        or payload.get("profile_id")
        or params.get("model")
        or params.get("profile_id")
        or ""
    ).strip()
    result = transcribe_ambient_audio(
        attachments,
        payload=payload,
        params=params,
        routing={},
        target_model_ref=target_model_ref,
        # This endpoint is an explicit transcription request. Even if the chat
        # model accepts audio, local STT remains a valid fallback here.
        target_supports_audio=False,
    )
    return ok(
        {
            "transcript": str(result.get("text") or "").strip(),
            "transcription": result,
        }
    )
