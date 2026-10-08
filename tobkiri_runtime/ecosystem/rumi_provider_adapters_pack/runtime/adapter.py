"""Execute registry-selected provider protocols with scoped credentials."""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import math
import re
import threading
import time
from typing import Any, Callable, Mapping
import urllib.parse

from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from core_runtime.http_request_lifetime import HttpRequestLifetime
from ecosystem.rumi_provider_registry_pack.runtime.model_access import (
    compile_connection_parameters,
)
from ecosystem.rumi_provider_registry_pack.runtime.local_endpoint import (
    local_openai_endpoint,
)
from core_runtime.local_model_authority import LocalModelRequest
from ecosystem.rumi_provider_adapters_pack.runtime.streaming import stream_request
from tobkiri_protocol.turn_progress_v1 import ACTION as PROGRESS_CONTRACT

REGISTRY_CONTRACT = "tobkiri.resource.ai.provider.registry.v1"
REGISTRY_GENERATE_OPERATION = (
    "rumi_provider_registry_pack.provider-registry-resource.generate"
)
REGISTRY_STREAM_OPERATION = (
    "rumi_provider_registry_pack.provider-registry-resource.stream"
)
REGISTRY_OPERATION = REGISTRY_GENERATE_OPERATION
DEFAULT_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "RumiAI/1.0",
}
_MAX_LOCAL_REQUEST_BYTES = 1024 * 1024
_MAX_LOCAL_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_LOCAL_TIMEOUT_SECONDS = 30.0

_AUDIO_ADAPTERS = frozenset({"openai", "openai-compatible"})
_AUDIO_DENY_KEYS = frozenset(
    {
        "path",
        "uri",
        "url",
        "file",
        "endpoint",
        "credential",
        "credential_handle",
        "api_key",
        "token",
        "secret",
    }
)
# The provider-facing audio request additionally rejects caller authority and
# routing fields: profile_id is execution-Profile authority, provider pinning
# arrives via the resolved model profile's connection binding, and deadline
# authority belongs to the captured Host envelope alone.
_AUDIO_REQUEST_DENY_KEYS = (
    _AUDIO_DENY_KEYS - {"credential_handle"}
) | {"profile_id", "provider_instance_id", "deadline"}
_AUDIO_MEDIA_TYPES = frozenset(
    {
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/mp4",
        "audio/m4a",
        "audio/aac",
        "audio/flac",
        "audio/ogg",
        "audio/webm",
        "audio/l16",
    }
)
_AUDIO_CONTENT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
# Decoded inline audio must fit the canonical journal envelope after base64
# expansion and run-document duplication; see the modality gateway for the
# shared bound rationale (MAX_CANONICAL_JSON_BYTES = 4 MiB).
_MAX_AUDIO_CONTENT_BYTES = 1024 * 1024
_MAX_AUDIO_MODEL_CHARS = 512
_MAX_AUDIO_LANGUAGE_CHARS = 64
_MAX_AUDIO_VOICE_CHARS = 128
_MAX_AUDIO_PROMPT_CHARS = 4096
_MAX_AUDIO_FILENAME_CHARS = 255
_MAX_SPEECH_INPUT_CHARS = 4096
_MAX_SPEECH_INSTRUCTIONS_CHARS = 4096
_MAX_TRANSCRIPT_TEXT_CHARS = 64 * 1024
_MAX_TRANSCRIPT_SEGMENTS = 512
_MAX_SEGMENT_TEXT_CHARS = 4096
_MAX_SEGMENT_SPEAKER_CHARS = 128
_MAX_SEGMENT_MILLISECONDS = 86_400_000
_TRANSCRIBE_PARAMS = frozenset(
    {"prompt", "temperature", "response_format"}
)
_TRANSCRIBE_RESPONSE_FORMATS = frozenset(
    {"json", "verbose_json", "diarized_json"}
)
_SPEECH_PARAMS = frozenset({"speed", "instructions", "response_format"})
_SPEECH_FORMAT_MEDIA_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/l16",
}
_AUDIO_FILENAME_SUFFIX = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mp4": ".mp4",
    "audio/m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
}
_AUDIO_JSON_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "RumiAI/1.0",
}


def create_generate_operation(client: GlobalContractClient):
    """Create a non-streaming provider execution operation."""
    return _operation(client, streaming=False)


def create_stream_operation(client: GlobalContractClient):
    """Create a streaming provider execution operation."""
    return _operation(client, streaming=True)


def create_embedding_operation(client: GlobalContractClient):
    """Create an OpenAI-compatible embedding provider operation."""
    return _modality_operation(client, kind="embedding")


def create_image_operation(client: GlobalContractClient):
    """Create an OpenAI-compatible image provider operation."""
    return _modality_operation(client, kind="image")


def create_audio_transcribe_operation(client: GlobalContractClient):
    """Create an OpenAI-compatible audio transcription provider operation."""
    return _audio_operation(client, kind="transcribe")


def create_audio_speech_operation(client: GlobalContractClient):
    """Create an OpenAI-compatible speech synthesis provider operation."""
    return _audio_operation(client, kind="speech")


def _operation(
    client: GlobalContractClient,
    *,
    streaming: bool,
    cancellation: threading.Event | None = None,
    deadline: float | None = None,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"invoke", "stream" if streaming else "generate"}
        if name not in allowed:
            raise ValueError(f"unknown provider adapter operation: {name}")
        request = dict(payload)
        connection = _connection(client, request, streaming=streaming)
        request = _apply_model_access(request, connection)
        adapter_id = str(connection.get("adapter_id") or "")
        credential_handle = _credential_handle(
            request,
            connection,
            scope="ai.stream" if streaming else "ai.generate",
        )
        adapter = _adapter(
            adapter_id,
            provider_id=str(request.get("provider_id") or ""),
        )
        # Lifetime authority comes only from the captured Host invocation.
        # Payload fields never supply a cancellation Event or monotonic deadline.
        local_lifetime = (
            {"cancellation": cancellation, "deadline": deadline}
            if adapter is _local_openai_compatible else {}
        )
        return adapter(
            client,
            request,
            connection,
            credential_handle,
            "ai.stream" if streaming else "ai.generate",
            streaming,
            **local_lifetime,
        )

    return operation


def _modality_operation(client: GlobalContractClient, *, kind: str):
    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        expected = "embed" if kind == "embedding" else "generate"
        if name not in {expected, "invoke"}:
            raise ValueError(f"unknown provider modality operation: {name}")
        request = dict(payload)
        connection = _connection(client, request)
        request = _apply_model_access(request, connection)
        if "provider" in request["parameters"]:
            raise GlobalContractInvocationError(
                "incompatible", "native routing is verified for chat completions only",
            )
        credential_handle = _credential_handle(
            request,
            connection,
            scope=f"ai.{kind}",
        )
        adapter_id = str(connection.get("adapter_id") or "")
        if adapter_id not in {"openai", "openai-compatible"}:
            raise GlobalContractInvocationError(
                "incompatible", "provider modality protocol is unavailable"
            )
        if credential_handle is None:
            raise GlobalContractInvocationError(
                "not_configured", "hosted modality credential is not configured"
            )
        if kind == "embedding":
            return _openai_embedding(client, request, connection, credential_handle, "ai.embedding")
        return _openai_image(client, request, connection, credential_handle, "ai.image")

    return operation


def _apply_model_access(
    request: Mapping[str, Any], connection: Mapping[str, Any],
    *, model_id: str | None = None,
) -> dict[str, Any]:
    """Compile the current Host-bound policy before credentials or transport."""
    try:
        parameters = compile_connection_parameters(
            connection, model_id if model_id is not None else _provider_model_id(request),
            request.get("parameters"),
        )
    except PermissionError as exc:
        raise GlobalContractInvocationError("denied", str(exc)) from exc
    except ValueError as exc:
        raise GlobalContractInvocationError("invalid_request", str(exc)) from exc
    return {**request, "parameters": parameters}


def _audio_operation(client: GlobalContractClient, *, kind: str):
    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        expected = "transcribe" if kind == "transcribe" else "synthesize"
        if name not in {expected, "invoke"}:
            raise ValueError(f"unknown provider audio operation: {name}")
        request = dict(payload)
        denied = sorted(_AUDIO_REQUEST_DENY_KEYS.intersection(request))
        if denied:
            message = ", ".join(denied)
            raise GlobalContractInvocationError(
                "denied", f"audio request fields are not permitted: {message}"
            )
        connection = _connection(client, request)
        model_id = _audio_model_id(request, connection)
        request = _apply_model_access(request, connection, model_id=model_id)
        if "provider" in request["parameters"]:
            raise GlobalContractInvocationError(
                "incompatible", "native routing is verified for chat completions only",
            )
        credential_handle = _credential_handle(
            request,
            connection,
            scope=f"ai.audio.{kind}",
        )
        adapter_id = str(connection.get("adapter_id") or "")
        if adapter_id not in _AUDIO_ADAPTERS:
            raise GlobalContractInvocationError(
                "incompatible", "provider audio protocol is unavailable"
            )
        if credential_handle is None:
            raise GlobalContractInvocationError(
                "not_configured", "hosted audio credential is not configured"
            )
        if kind == "transcribe":
            return _openai_audio_transcribe(
                client,
                request,
                connection,
                credential_handle,
                "ai.audio.transcribe",
                model_id,
            )
        return _openai_audio_speech(
            client,
            request,
            connection,
            credential_handle,
            "ai.audio.speech",
            model_id,
        )

    return operation


def _audio_model_id(
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
) -> str:
    """Require one registered provider model ID from the resolved record."""
    model_id = request.get("model_id")
    if (
        not isinstance(model_id, str)
        or not model_id.strip()
        or len(model_id.strip()) > _MAX_AUDIO_MODEL_CHARS
    ):
        raise GlobalContractInvocationError(
            "invalid_request", "a registered model_id is required"
        )
    # The connection instance supplies the provider prefix context; callers
    # never name the provider.
    instance = str(connection.get("provider_instance_id") or "")
    provider_id = (
        instance[len("provider."):]
        if instance.startswith("provider.")
        else ""
    )
    return _provider_model_id(
        {**request, "model_id": model_id.strip(), "provider_id": provider_id}
    )


def _audio_inline_content(value: Any) -> dict[str, Any]:
    """Validate one bounded inline audio upload for the wire.

    Inline content is a caller-declared byte string whose ``content_id``
    self-hash is verified against the decoded bytes.  It is *validated
    inline content*, not a stored artifact reference: no ownership,
    persistence, or locator authority is implied, and paths, URIs, and
    URLs are never dereferenceable inputs.
    """
    if not isinstance(value, Mapping):
        raise GlobalContractInvocationError(
            "invalid_request", "inline audio content is required"
        )
    unknown = set(value) - {
        "content_id",
        "media_type",
        "data_base64",
        "byte_size",
        "filename",
    }
    if unknown or _AUDIO_DENY_KEYS.intersection(value):
        raise GlobalContractInvocationError(
            "denied", "audio accepts only bounded inline content"
        )
    content_id = value.get("content_id")
    if not isinstance(content_id, str) or not _AUDIO_CONTENT_ID.match(
        content_id
    ):
        raise GlobalContractInvocationError(
            "invalid_request", "audio content_id is invalid"
        )
    media_type = str(value.get("media_type") or "").lower()
    if media_type not in _AUDIO_MEDIA_TYPES:
        raise GlobalContractInvocationError(
            "invalid_request", "audio media_type is not supported"
        )
    encoded = value.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        raise GlobalContractInvocationError(
            "invalid_request", "audio data_base64 is required"
        )
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise GlobalContractInvocationError(
            "invalid_request", "audio data_base64 is invalid"
        ) from None
    if not decoded or len(decoded) > _MAX_AUDIO_CONTENT_BYTES:
        raise GlobalContractInvocationError(
            "invalid_request", "audio content exceeds the byte bound"
        )
    declared_size = value.get("byte_size")
    if (
        declared_size is not None
        and (type(declared_size) is not int or declared_size != len(decoded))
    ):
        raise GlobalContractInvocationError(
            "invalid_request", "audio byte_size does not match its bytes"
        )
    if content_id != "sha256:" + hashlib.sha256(decoded).hexdigest():
        raise GlobalContractInvocationError(
            "invalid_request", "audio content_id does not match its bytes"
        )
    filename = value.get("filename")
    name = "audio" + _AUDIO_FILENAME_SUFFIX.get(media_type, ".bin")
    if filename is not None:
        if not isinstance(filename, str):
            raise GlobalContractInvocationError(
                "invalid_request", "audio filename is invalid"
            )
        candidate = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
        if (
            not candidate
            or candidate in {".", ".."}
            or len(candidate) > _MAX_AUDIO_FILENAME_CHARS
            or '"' in candidate
            or any(ord(char) < 32 for char in candidate)
        ):
            raise GlobalContractInvocationError(
                "invalid_request", "audio filename is invalid"
            )
        name = candidate
    return {
        "content_id": content_id,
        "media_type": media_type,
        "filename": name,
        "content": decoded,
    }


def _audio_transcribe_fields(parameters: Any) -> dict[str, str]:
    """Project allowlisted scalar parameters into multipart form fields."""
    if parameters is None:
        return {}
    if not isinstance(parameters, Mapping):
        raise GlobalContractInvocationError(
            "invalid_request", "audio parameters are invalid"
        )
    unknown = sorted(set(parameters) - _TRANSCRIBE_PARAMS)
    if unknown:
        raise GlobalContractInvocationError(
            "invalid_request",
            f"audio parameters are not supported: {', '.join(unknown)}",
        )
    fields: dict[str, str] = {}
    prompt = parameters.get("prompt")
    if prompt is not None:
        if (
            not isinstance(prompt, str)
            or not prompt
            or len(prompt) > _MAX_AUDIO_PROMPT_CHARS
        ):
            raise GlobalContractInvocationError(
                "invalid_request", "audio prompt is invalid"
            )
        fields["prompt"] = prompt
    temperature = parameters.get("temperature")
    if temperature is not None:
        fields["temperature"] = _audio_decimal(
            "temperature", temperature, minimum=0.0, maximum=1.0
        )
    response_format = parameters.get("response_format")
    if response_format is not None:
        if (
            not isinstance(response_format, str)
            or response_format not in _TRANSCRIBE_RESPONSE_FORMATS
        ):
            raise GlobalContractInvocationError(
                "invalid_request", "audio response_format is invalid"
            )
        fields["response_format"] = response_format
    return fields


def _audio_speech_parameters(parameters: Any) -> dict[str, Any]:
    """Project allowlisted scalar parameters into the speech JSON body."""
    if parameters is None:
        return {}
    if not isinstance(parameters, Mapping):
        raise GlobalContractInvocationError(
            "invalid_request", "speech parameters are invalid"
        )
    unknown = sorted(set(parameters) - _SPEECH_PARAMS)
    if unknown:
        raise GlobalContractInvocationError(
            "invalid_request",
            f"speech parameters are not supported: {', '.join(unknown)}",
        )
    body: dict[str, Any] = {}
    speed = parameters.get("speed")
    if speed is not None:
        body["speed"] = float(
            _audio_decimal("speed", speed, minimum=0.25, maximum=4.0)
        )
    instructions = parameters.get("instructions")
    if instructions is not None:
        if (
            not isinstance(instructions, str)
            or not instructions
            or len(instructions) > _MAX_SPEECH_INSTRUCTIONS_CHARS
        ):
            raise GlobalContractInvocationError(
                "invalid_request", "speech instructions are invalid"
            )
        body["instructions"] = instructions
    response_format = parameters.get("response_format")
    if response_format is not None:
        if (
            not isinstance(response_format, str)
            or response_format not in _SPEECH_FORMAT_MEDIA_TYPES
        ):
            raise GlobalContractInvocationError(
                "invalid_request", "speech response_format is invalid"
            )
        body["response_format"] = response_format
    return body


def _audio_decimal(
    name: str,
    value: Any,
    *,
    minimum: float,
    maximum: float,
) -> str:
    """Normalize one fractional option to an exact decimal string.

    Canonical journaling cannot carry floats, so callers express fractional
    options as decimal strings (or integers); the wire body converts that
    canonical representation back into a JSON number.
    """
    if isinstance(value, bool):
        raise GlobalContractInvocationError(
            "invalid_request", f"audio {name} is invalid"
        )
    if isinstance(value, int):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if (
            not text
            or len(text) > 8
            or re.fullmatch(r"[0-9]+(\.[0-9]+)?", text) is None
        ):
            raise GlobalContractInvocationError(
                "invalid_request", f"audio {name} is invalid"
            )
        number = float(text)
    else:
        raise GlobalContractInvocationError(
            "invalid_request", f"audio {name} is invalid"
        )
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise GlobalContractInvocationError(
            "invalid_request", f"audio {name} is out of range"
        )
    return f"{number:g}"


def _audio_multipart_post(
    client: GlobalContractClient,
    *,
    endpoint: str,
    fields: Mapping[str, str],
    filename: str,
    content_type: str,
    content: bytes,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
) -> dict[str, Any]:
    """POST one structured multipart form through the Host credential lease."""
    # No caller deadline: public audio requests deny the field.  The bounded
    # local budget is the absolute wall-clock limit the transport compares
    # against the captured Host envelope (`min(remaining, host_remaining)`).
    deadline = time.time() + _MAX_LOCAL_TIMEOUT_SECONDS
    try:
        value = client.post_multipart_with_credential(
            endpoint=endpoint,
            headers=_AUDIO_JSON_HEADERS,
            fields=fields,
            file_field="file",
            filename=filename,
            content_type=content_type,
            content=content,
            credential_handle=credential_handle,
            provider_instance_id=str(connection["provider_instance_id"]),
            credential_scope=credential_scope,
            credential_scheme="bearer",
            deadline=deadline,
        )
    except (OSError, PermissionError, RuntimeError, ValueError) as exc:
        raise GlobalContractInvocationError(
            "provider_unavailable", type(exc).__name__
        ) from None
    if not isinstance(value, dict):
        raise GlobalContractInvocationError(
            "invalid_response", "provider returned a non-object response"
        )
    return value


def _audio_binary_post(
    client: GlobalContractClient,
    *,
    endpoint: str,
    body: Mapping[str, Any],
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
) -> bytes:
    """POST one bounded JSON request returning opaque audio bytes."""
    deadline = time.time() + _MAX_LOCAL_TIMEOUT_SECONDS
    try:
        value = client.post_binary_with_credential(
            endpoint=endpoint,
            headers=dict(DEFAULT_JSON_HEADERS),
            body=body,
            credential_handle=credential_handle,
            provider_instance_id=str(connection["provider_instance_id"]),
            credential_scope=credential_scope,
            credential_scheme="bearer",
            deadline=deadline,
        )
    except (OSError, PermissionError, RuntimeError, ValueError) as exc:
        raise GlobalContractInvocationError(
            "provider_unavailable", type(exc).__name__
        ) from None
    if not isinstance(value, bytes):
        raise GlobalContractInvocationError(
            "invalid_response", "provider returned a non-binary response"
        )
    return value


def _openai_audio_transcribe(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
    model_id: str,
) -> dict[str, Any]:
    """POST one real multipart transcription to the connection endpoint."""
    inline = _audio_inline_content(request.get("audio"))
    fields = {"model": model_id}
    language = request.get("language")
    if language is not None:
        if (
            not isinstance(language, str)
            or not language.strip()
            or len(language) > _MAX_AUDIO_LANGUAGE_CHARS
            or any(char.isspace() for char in language.strip())
        ):
            raise GlobalContractInvocationError(
                "invalid_request", "audio language is invalid"
            )
        fields["language"] = language.strip()
    fields.update(_audio_transcribe_fields(request.get("parameters")))
    fields.setdefault("response_format", "json")
    value = _audio_multipart_post(
        client,
        endpoint=_endpoint(connection, "/audio/transcriptions"),
        fields=fields,
        filename=inline["filename"],
        content_type=inline["media_type"],
        content=inline["content"],
        request=request,
        connection=connection,
        credential_handle=credential_handle,
        credential_scope=credential_scope,
    )
    text = value.get("text")
    if not isinstance(text, str) or len(text) > _MAX_TRANSCRIPT_TEXT_CHARS:
        raise GlobalContractInvocationError(
            "invalid_response", "audio provider returned an invalid result"
        )
    language = value.get("language")
    if language is not None and (
        not isinstance(language, str)
        or not language
        or len(language) > _MAX_AUDIO_LANGUAGE_CHARS
    ):
        raise GlobalContractInvocationError(
            "invalid_response", "audio provider returned an invalid result"
        )
    return {
        "text": text,
        "language": language,
        "segments": _transcript_segments(value.get("segments")),
    }


def _transcript_segments(value: Any) -> list[dict[str, Any]]:
    """Project verbose provider segments to canonical-safe millisecond ints.

    Provider segment offsets arrive as fractional seconds; only integer
    millisecond fields cross the canonical journal boundary.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_TRANSCRIPT_SEGMENTS:
        raise GlobalContractInvocationError(
            "invalid_response", "audio provider returned an invalid result"
        )
    segments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise GlobalContractInvocationError(
                "invalid_response", "audio provider returned an invalid result"
            )
        segment: dict[str, Any] = {}
        text = item.get("text")
        if (
            isinstance(text, str)
            and text
            and len(text) <= _MAX_SEGMENT_TEXT_CHARS
        ):
            segment["text"] = text
        for source, key in (("start", "start_ms"), ("end", "end_ms")):
            offset = item.get(source)
            if isinstance(offset, bool):
                continue
            if isinstance(offset, (int, float)) and math.isfinite(offset):
                millis = round(float(offset) * 1000)
                if 0 <= millis <= _MAX_SEGMENT_MILLISECONDS:
                    segment[key] = millis
        speaker = item.get("speaker")
        if (
            isinstance(speaker, str)
            and speaker.strip()
            and len(speaker.strip()) <= _MAX_SEGMENT_SPEAKER_CHARS
        ):
            segment["speaker"] = speaker.strip()
        segments.append(segment)
    return segments


def _openai_audio_speech(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
    model_id: str,
) -> dict[str, Any]:
    """POST one real JSON speech request and bind the binary audio reply."""
    text = request.get("input")
    if text is None:
        text = request.get("text")
    if (
        not isinstance(text, str)
        or not text
        or len(text) > _MAX_SPEECH_INPUT_CHARS
    ):
        raise GlobalContractInvocationError(
            "invalid_request", "speech input text is invalid"
        )
    voice = request.get("voice")
    if (
        not isinstance(voice, str)
        or not voice.strip()
        or len(voice) > _MAX_AUDIO_VOICE_CHARS
    ):
        raise GlobalContractInvocationError(
            "invalid_request", "a speech voice is required"
        )
    parameters = _audio_speech_parameters(request.get("parameters"))
    response_format = str(parameters.pop("response_format", "mp3"))
    body = {
        "model": model_id,
        "input": text,
        "voice": voice.strip(),
        "response_format": response_format,
        **parameters,
    }
    audio = _audio_binary_post(
        client,
        endpoint=_endpoint(connection, "/audio/speech"),
        body=body,
        request=request,
        connection=connection,
        credential_handle=credential_handle,
        credential_scope=credential_scope,
    )
    return {
        "content": _speech_result_content(
            audio,
            media_type=_SPEECH_FORMAT_MEDIA_TYPES[response_format],
        )
    }


def _speech_result_content(value: Any, *, media_type: str) -> dict[str, Any]:
    """Bind provider audio bytes to validated inline content.

    The ``content_id`` is recomputed Host-side from the returned bytes, so a
    provider can never supply a locator or a digest the caller must trust.
    """
    if (
        not isinstance(value, (bytes, bytearray))
        or not value
        or len(value) > _MAX_AUDIO_CONTENT_BYTES
    ):
        raise GlobalContractInvocationError(
            "invalid_response", "audio provider returned an invalid result"
        )
    content = bytes(value)
    return {
        "content_id": "sha256:" + hashlib.sha256(content).hexdigest(),
        "media_type": media_type,
        "data_base64": base64.b64encode(content).decode("ascii"),
        "byte_size": len(content),
    }


def _connection(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    *,
    streaming: bool = False,
) -> dict[str, Any]:
    connection_id = request.get("provider_connection_id")
    if request.get("endpoint") is not None:
        raise GlobalContractInvocationError(
            "denied", "provider endpoint is bound by the Host provider registry"
        )
    if connection_id is None:
        # Catalog-driven requests retain the legacy provider-slug route.
        # Explicit saved connection IDs are opaque registry identities and
        # never pass through this prefixing compatibility path.
        provider_id = str(request.get("provider_id") or "").strip()
        if not provider_id:
            raise GlobalContractInvocationError(
                "invalid_request", "provider_id is required"
            )
        expected = f"provider.{provider_id}"
    elif not isinstance(connection_id, str) or not connection_id:
        raise GlobalContractInvocationError(
            "invalid_request", "provider_connection_id is invalid"
        )
    else:
        expected = connection_id
    registry_payload = {}
    profile_id = str(request.get("profile_id") or "").strip()
    if profile_id:
        registry_payload["profile_id"] = profile_id
    result = client.invoke(
        REGISTRY_CONTRACT,
        REGISTRY_STREAM_OPERATION if streaming else REGISTRY_GENERATE_OPERATION,
        registry_payload,
    )
    providers = result.get("providers") if isinstance(result, Mapping) else None
    providers = providers if isinstance(providers, list) else []
    matches = [
        dict(item)
        for item in providers
        if isinstance(item, Mapping)
        and str(item.get("provider_instance_id") or "") == expected
        and item.get("enabled") is True
    ]
    if len(matches) != 1:
        raise GlobalContractInvocationError(
            "not_configured", "provider connection is not configured"
        )
    return matches[0]


def _credential_handle(
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    *,
    scope: str,
) -> str | None:
    del scope
    supplied_handle = request.get("credential_handle")
    handle = connection.get("credential_handle")
    if supplied_handle is not None:
        raise GlobalContractInvocationError(
            "denied", "credential handle is bound by the Host provider registry"
        )
    if connection.get("adapter_id") == "local-openai-compatible":
        if handle is not None:
            raise GlobalContractInvocationError(
                "denied", "local provider credentials are not permitted"
            )
        try:
            local_openai_endpoint(connection.get("endpoint"))
        except ValueError:
            raise GlobalContractInvocationError(
                "denied", "local provider endpoint is invalid"
            ) from None
        return None
    if handle is None:
        raise GlobalContractInvocationError(
            "not_configured", "provider credential is not configured"
        )
    if not str(handle).startswith(("credential:", "opaque:")):
        raise GlobalContractInvocationError(
            "denied", "provider adapter accepts only opaque credentials"
        )
    endpoint = urllib.parse.urlsplit(str(connection.get("endpoint") or ""))
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
    ):
        raise GlobalContractInvocationError(
            "denied", "credentialed provider endpoint requires HTTPS"
        )
    return str(handle)


def _adapter(
    adapter_id: str,
    *,
    provider_id: str = "",
) -> Callable[..., dict[str, Any]]:
    if adapter_id == "llm":
        adapter_id = "anthropic" if provider_id == "anthropic" else "openai-compatible"
    adapters = {
        "openai-compatible": _openai_compatible,
        "openai": _openai_compatible,
        "openrouter": _openai_compatible,
        "local-openai-compatible": _local_openai_compatible,
        "anthropic": _anthropic,
    }
    try:
        return adapters[adapter_id]
    except KeyError:
        raise GlobalContractInvocationError(
            "incompatible", "provider adapter protocol is unavailable"
        ) from None


def _openai_compatible(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
    streaming: bool,
) -> dict[str, Any]:
    endpoint = _endpoint(connection, "/chat/completions")
    body = {
        "model": _provider_model_id(request),
        "messages": list(request.get("messages") or []),
        **dict(request.get("parameters") or {}),
        "stream": streaming,
    }
    tools = request.get("tools")
    if isinstance(tools, list) and tools:
        body["tools"] = tools
    headers = dict(DEFAULT_JSON_HEADERS)
    if streaming:
        return stream_request(
            client, request, connection, body=body, endpoint=endpoint, headers=headers,
            credential_handle=credential_handle, credential_scope=credential_scope,
        )
    value = _post(
        client,
        endpoint,
        headers,
        body,
        request,
        connection=connection,
        credential_handle=credential_handle,
        credential_scope=credential_scope,
        credential_scheme="bearer",
    )
    choices = value.get("choices") if isinstance(value, Mapping) else None
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, Mapping) else {}
    content = message.get("content") if isinstance(message, Mapping) else ""
    result: dict[str, Any] = {
        "output": content if content is not None else "",
        "tool_intents": (
            list(message.get("tool_calls") or []) if isinstance(message, Mapping) else []
        ),
        "usage": dict(value.get("usage") or {}),
        "finish_reason": (first.get("finish_reason") if isinstance(first, Mapping) else None),
    }
    return result


def _local_openai_compatible(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str | None,
    credential_scope: str,
    streaming: bool,
    *,
    cancellation: threading.Event | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Call one registry-bound loopback OpenAI chat endpoint without a secret."""
    del client, credential_scope
    if credential_handle is not None:
        raise GlobalContractInvocationError(
            "denied", "local provider credentials are not permitted"
        )
    endpoint = local_openai_endpoint(connection.get("endpoint"))
    body = {
        "model": _provider_model_id(request),
        "messages": list(request.get("messages") or []),
        "stream": False,
        **dict(request.get("parameters") or {}),
    }
    tools = request.get("tools")
    if isinstance(tools, list) and tools:
        body["tools"] = tools
    value = _local_post(
        endpoint, body, request, cancellation=cancellation, deadline=deadline,
    )
    choices = value.get("choices") if isinstance(value, Mapping) else None
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, Mapping) else {}
    content = message.get("content") if isinstance(message, Mapping) else ""
    result: dict[str, Any] = {
        "output": content if content is not None else "",
        "tool_intents": (
            list(message.get("tool_calls") or [])
            if isinstance(message, Mapping)
            else []
        ),
        "usage": dict(value.get("usage") or {}),
        "finish_reason": (
            first.get("finish_reason") if isinstance(first, Mapping) else None
        ),
    }
    return _stream_result(result) if streaming else result


def _local_post(
    endpoint: str,
    body: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    cancellation: threading.Event | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Send bounded JSON to the finite local chat route without proxy use."""
    parsed = urllib.parse.urlsplit(local_openai_endpoint(endpoint))
    hostname = parsed.hostname
    if hostname is None:
        raise GlobalContractInvocationError("invalid_request", "local provider host is invalid")
    encoded = json.dumps(
        body, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_LOCAL_REQUEST_BYTES:
        raise GlobalContractInvocationError(
            "invalid_request", "local provider request is too large"
        )
    timeout = _local_timeout(request)
    lifetime: HttpRequestLifetime | None = None
    connection: http.client.HTTPConnection | None = None
    response: http.client.HTTPResponse | None = None
    try:
        lifetime = HttpRequestLifetime(
            timeout=timeout, deadline=deadline, cancellation=cancellation,
        )
        connection = http.client.HTTPConnection(
            hostname, parsed.port, timeout=lifetime.remaining(),
        )
        connection.connect()
        if connection.sock is None:
            raise OSError("local provider connection socket is unavailable")
        # HTTP/1.0 and Connection: close can clear connection.sock while the
        # response still owns a blocked buffered reader. Retain the actual TCP
        # socket; the watchdog only shuts it down, and this caller closes IO.
        owned_socket = connection.sock
        lifetime.attach(owned_socket)
        owned_socket.settimeout(lifetime.remaining())
        connection.auto_open = 0
        lifetime.check()
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=encoded,
            headers={**DEFAULT_JSON_HEADERS, "Content-Length": str(len(encoded))},
        )
        owned_socket.settimeout(lifetime.remaining())
        response = connection.getresponse()
        lifetime.check()
        owned_socket.settimeout(lifetime.remaining())
        if response.status != 200:
            raise GlobalContractInvocationError(
                "provider_unavailable", "local provider returned an error"
            )
        raw = response.read(_MAX_LOCAL_RESPONSE_BYTES + 1)
        lifetime.check()
        if len(raw) > _MAX_LOCAL_RESPONSE_BYTES:
            raise GlobalContractInvocationError(
                "invalid_response", "local provider response is too large"
            )
    except GlobalContractInvocationError:
        raise
    except (OSError, TimeoutError, http.client.HTTPException):
        raise GlobalContractInvocationError(
            "provider_unavailable", "local provider is unavailable"
        ) from None
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                if lifetime is not None:
                    lifetime.close()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GlobalContractInvocationError(
            "invalid_response", "local provider returned invalid JSON"
        ) from None
    if not isinstance(value, dict):
        raise GlobalContractInvocationError(
            "invalid_response", "local provider returned a non-object response"
        )
    return value


def _local_timeout(request: Mapping[str, Any]) -> float:
    """Return the initial bounded wall-clock budget for the entire request."""
    deadline = request.get("deadline")
    if deadline is None:
        return _MAX_LOCAL_TIMEOUT_SECONDS
    if (
        not isinstance(deadline, (int, float))
        or isinstance(deadline, bool)
        or not math.isfinite(float(deadline))
    ):
        raise GlobalContractInvocationError(
            "invalid_request", "provider deadline is invalid"
        )
    timeout = min(_MAX_LOCAL_TIMEOUT_SECONDS, float(deadline) - time.time())
    if timeout <= 0:
        raise GlobalContractInvocationError(
            "provider_unavailable", "provider deadline expired"
        )
    return timeout


def _anthropic(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
    streaming: bool,
) -> dict[str, Any]:
    endpoint = _endpoint(connection, "/messages")
    parameters = dict(request.get("parameters") or {})
    body = {
        "model": _provider_model_id(request),
        "messages": list(request.get("messages") or []),
        "max_tokens": int(parameters.pop("max_tokens", 1024)),
        **parameters,
        "stream": streaming,
    }
    headers = {
        **DEFAULT_JSON_HEADERS,
        "anthropic-version": "2023-06-01",
    }
    if streaming:
        return stream_request(
            client, request, connection, body=body, endpoint=endpoint, headers=headers,
            credential_handle=credential_handle, credential_scope=credential_scope,
            scheme="anthropic", protocol="anthropic",
        )
    value = _post(
        client,
        endpoint,
        headers,
        body,
        request,
        connection=connection,
        credential_handle=credential_handle,
        credential_scope=credential_scope,
        credential_scheme="anthropic",
    )
    blocks = value.get("content") if isinstance(value, Mapping) else None
    blocks = blocks if isinstance(blocks, list) else []
    text = "".join(
        str(item.get("text") or "")
        for item in blocks
        if isinstance(item, Mapping) and item.get("type") == "text"
    )
    result: dict[str, Any] = {
        "output": text,
        "tool_intents": [],
        "usage": dict(value.get("usage") or {}),
        "finish_reason": value.get("stop_reason"),
    }
    return result


def _openai_embedding(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
) -> dict[str, Any]:
    value = _post(
        client,
        _endpoint(connection, "/embeddings"),
        dict(DEFAULT_JSON_HEADERS),
        {"model": _provider_model_id(request), "input": request.get("input")},
        request,
        connection=connection,
        credential_handle=credential_handle,
        credential_scope=credential_scope,
        credential_scheme="bearer",
    )
    data = value.get("data")
    data = data if isinstance(data, list) else []
    vectors = [list(item.get("embedding") or []) for item in data if isinstance(item, Mapping)]
    return {"vectors": vectors, "usage": dict(value.get("usage") or {})}


def _openai_image(
    client: GlobalContractClient,
    request: Mapping[str, Any],
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
) -> dict[str, Any]:
    body = {
        "model": _provider_model_id(request),
        "prompt": request.get("prompt"),
        **dict(request.get("parameters") or {}),
    }
    value = _post(
        client,
        _endpoint(connection, "/images/generations"),
        dict(DEFAULT_JSON_HEADERS),
        body,
        request,
        connection=connection,
        credential_handle=credential_handle,
        credential_scope=credential_scope,
        credential_scheme="bearer",
    )
    data = value.get("data")
    artifacts = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, Mapping):
            continue
        material = str(item.get("url") or item.get("b64_json") or "")
        if not material:
            continue
        artifacts.append(
            {
                "artifact_id": "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
                "uri": item.get("url"),
                "base64": item.get("b64_json"),
                "revised_prompt": item.get("revised_prompt"),
            }
        )
    return {"artifacts": artifacts}


def _provider_model_id(request: Mapping[str, Any]) -> str:
    model_id = str(request.get("model_id") or "")
    provider_id = str(request.get("provider_id") or "")
    prefix = f"{provider_id}/"
    if provider_id and model_id.startswith(prefix):
        return model_id[len(prefix) :]
    return model_id


def _stream_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Project one completed local response into the compatibility event shape.

    Local OpenAI-compatible endpoints are intentionally finite HTTP requests.
    This helper does not publish progress: hosted providers publish only actual
    received stream frames through ``stream_request``.
    """
    events: list[dict[str, Any]] = []
    output = str(result.get("output") or "")
    if output:
        events.append({"type": "text_delta", "delta": output})
    for intent in result.get("tool_intents") or []:
        if isinstance(intent, Mapping):
            events.append(
                {
                    "type": "tool_intent_delta",
                    "tool_intent": dict(intent),
                }
            )
    events.extend(
        [
            {"type": "usage", "usage": dict(result.get("usage") or {})},
            {"type": "finish", "finish_reason": result.get("finish_reason")},
        ]
    )
    return {"events": events}


def _endpoint(connection: Mapping[str, Any], suffix: str) -> str:
    endpoint = str(connection.get("endpoint") or "").rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        raise GlobalContractInvocationError("not_configured", "provider endpoint is not configured")
    return endpoint + suffix


def _post(
    client: GlobalContractClient,
    endpoint: str,
    headers: Mapping[str, str],
    body: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    connection: Mapping[str, Any],
    credential_handle: str,
    credential_scope: str,
    credential_scheme: str,
) -> dict[str, Any]:
    deadline = float(request.get("deadline") or 0)
    try:
        value = client.post_json_with_credential(
            endpoint=endpoint,
            headers=headers,
            body=body,
            credential_handle=credential_handle,
            provider_instance_id=str(connection["provider_instance_id"]),
            credential_scope=credential_scope,
            credential_scheme=credential_scheme,
            deadline=deadline,
        )
    except (OSError, PermissionError, RuntimeError, ValueError) as exc:
        raise GlobalContractInvocationError("provider_unavailable", type(exc).__name__) from None
    if not isinstance(value, dict):
        raise GlobalContractInvocationError(
            "invalid_response", "provider returned a non-object response"
        )
    return value


_PROVIDER_OPERATIONS = {
    "rumi_provider_adapters_pack.provider.compatibility.audio_speech": (
        "synthesize",
        create_audio_speech_operation,
    ),
    "rumi_provider_adapters_pack.provider.compatibility.audio_transcribe": (
        "transcribe",
        create_audio_transcribe_operation,
    ),
    "rumi_provider_adapters_pack.provider.compatibility.embedding": (
        "embed",
        create_embedding_operation,
    ),
    "rumi_provider_adapters_pack.provider.compatibility.generate": (
        "generate",
        create_generate_operation,
    ),
    "rumi_provider_adapters_pack.provider.compatibility.image": (
        "generate",
        create_image_operation,
    ),
    "rumi_provider_adapters_pack.provider.compatibility.stream": (
        "stream",
        create_stream_operation,
    ),
}


class ProviderAdapterHostFactoryV4:
    """Capture one adapter Function behind authenticated Host capabilities."""

    def __init__(self, function_id: str) -> None:
        self.function_id = function_id
        operation_factory = _PROVIDER_OPERATIONS[function_id][1]
        self.local_model_request = (
            LocalModelRequest(
                "tobkiri.service.ai.provider.generate.v1",
                REGISTRY_GENERATE_OPERATION,
            )
            if operation_factory is create_generate_operation
            else LocalModelRequest(
                "tobkiri.service.ai.provider.stream.v1",
                REGISTRY_STREAM_OPERATION,
            )
            if operation_factory is create_stream_operation
            else None
        )

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind adapter execution to its exact resolved operation."""
        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            for binding in context.provider_bindings
        ):
            raise PermissionError("provider adapter bindings are incomplete")
        operation_name, operation_factory = _PROVIDER_OPERATIONS[self.function_id]

        def invoke(
            _operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            client = invocation.contract_client(
                allowed_contract_ids=frozenset(
                    {REGISTRY_CONTRACT, PROGRESS_CONTRACT}
                    if operation_factory is create_stream_operation else {REGISTRY_CONTRACT}
                ),
                consumer_pack_id="rumi_provider_adapters_pack",
            )
            if operation_factory in {
                create_generate_operation, create_stream_operation,
            }:
                operation = _operation(
                    client,
                    streaming=operation_factory is create_stream_operation,
                    cancellation=invocation.envelope.cancellation_requested,
                    deadline=invocation.envelope.deadline_monotonic,
                )
            else:
                operation = operation_factory(client)
            result = operation(operation_name, payload)
            invocation.assert_current()
            return result

        contributions = []
        for binding in context.provider_bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                raise PermissionError("provider adapter domain binding is unavailable")
            contributions.append(
                HostProviderContributionV4(
                    contract_id=binding.operation.contract_id,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), lambda: None)


HOST_PROVIDER_FACTORY = {
    function_id: ProviderAdapterHostFactoryV4(function_id)
    for function_id in _PROVIDER_OPERATIONS
}
