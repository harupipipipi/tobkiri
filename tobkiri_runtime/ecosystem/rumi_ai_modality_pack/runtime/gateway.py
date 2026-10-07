"""Dispatch non-chat modalities through selected typed provider contracts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import re
from typing import Any, Callable, Mapping

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

_EMBEDDING_PROVIDER = "tobkiri.service.ai.provider.embedding.v1"
_IMAGE_PROVIDER = "tobkiri.service.ai.provider.image.v1"
_TRANSCRIBE_PROVIDER = "tobkiri.service.ai.provider.audio.transcribe.v1"
_SPEECH_PROVIDER = "tobkiri.service.ai.provider.audio.speech.v1"

# Registered model selection resolves through the captured current-Profile
# Model Registry, never through caller-supplied provider identities.
_MODEL_PROFILE_CONTRACT = "tobkiri.resource.ai.model.profile.v1"
_MODEL_PROFILE_OPERATION = (
    "rumi_model_registry_pack.model-profile-resource.generate"
)
_MODEL_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")

_MODALITY_SPEECH_CONTRACT = "tobkiri.service.ai.audio.speech.v1"
_MODALITY_TRANSCRIBE_CONTRACT = "tobkiri.service.ai.audio.transcribe.v1"
_MODALITY_EMBEDDING_CONTRACT = "tobkiri.service.ai.embedding.v1"
_MODALITY_IMAGE_CONTRACT = "tobkiri.service.ai.image.v1"

_SPEECH_FUNCTION_ID = "rumi_ai_modality_pack.ai-modality.speech"
_TRANSCRIBE_FUNCTION_ID = "rumi_ai_modality_pack.ai-modality.transcribe"
_EMBEDDING_FUNCTION_ID = "rumi_ai_modality_pack.ai-modality.embedding"
_IMAGE_FUNCTION_ID = "rumi_ai_modality_pack.ai-modality.image"

_CONTENT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
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
_DENIED_REQUEST_KEYS = frozenset(
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
# Audio requests additionally reject caller routing/authority fields: the
# registered model profile supplies the provider binding and provider model
# ID, and the captured Host envelope alone owns deadline authority.
_AUDIO_DENIED_REQUEST_KEYS = _DENIED_REQUEST_KEYS | frozenset(
    {
        "model_id",
        "model_reference",
        "identifier",
        "profile_id",
        "provider_id",
        "provider_instance_id",
        "provider_connection_id",
        "deadline",
        "requirements",
        "allow_failover",
    }
)
# Inline audio is journaled as base64 inside the canonical envelope, so the
# decoded cap must fit MAX_CANONICAL_JSON_BYTES (4 MiB) after ~4/3 base64
# expansion plus all sibling fields and run-document duplication (inputs +
# attempt request + outcomes share one journaled document).  1 MiB decoded
# yields ~1.4 MiB encoded per direction and keeps the worst combined
# document at ~2.9 MiB; larger audio honestly requires a real scoped
# artifact transport, never a path/URL fallback.
_MAX_AUDIO_CONTENT_BYTES = 1024 * 1024
_MAX_TEXT_INPUT_CHARS = 8 * 1024
_MAX_MODEL_ID_CHARS = 512
_MAX_MODEL_PROFILE_ID_CHARS = 256
_MAX_LANGUAGE_CHARS = 64
_MAX_VOICE_CHARS = 128
_MAX_TRANSCRIPT_TEXT_CHARS = 64 * 1024
_MAX_TRANSCRIPT_SEGMENTS = 512
_MAX_SEGMENT_TEXT_CHARS = 4096
_MAX_SEGMENT_SPEAKER_CHARS = 128
_MAX_SEGMENT_MILLISECONDS = 86_400_000


def create_embedding_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the provider-neutral embedding gateway."""
    return _operation(client, _EMBEDDING_PROVIDER, "embed", _embedding)


def create_image_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the provider-neutral image gateway."""
    return _operation(client, _IMAGE_PROVIDER, "generate", _image)


def create_audio_transcribe_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the provider-neutral transcription gateway."""
    return _operation(
        client,
        _TRANSCRIBE_PROVIDER,
        "transcribe",
        _transcript,
        prepare=_transcribe_request,
        resolve=_resolve_model_route,
    )


def create_audio_speech_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the provider-neutral speech synthesis gateway."""
    return _operation(
        client,
        _SPEECH_PROVIDER,
        "synthesize",
        _speech,
        prepare=_speech_request,
        resolve=_resolve_model_route,
    )


def _operation(
    client: GlobalContractClient,
    contract_id: str,
    expected_operation: str,
    normalizer: Callable[[Any], dict[str, Any]],
    *,
    prepare: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
    resolve: (
        Callable[[GlobalContractClient, dict[str, Any]], dict[str, Any]]
        | None
    ) = None,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {expected_operation, "invoke"}:
            raise ValueError(f"unknown modality operation: {name}")
        # Validation and caller-key denial run before provider selection so a
        # denied routing field can never influence the provider match.
        if prepare is not None:
            request = prepare(payload)
        else:
            request = dict(payload)
        providers = client.providers(contract_id)
        preferred = str(request.pop("provider_instance_id", "") or "").strip()
        if preferred:
            matches = [
                item
                for item in providers
                if item.get("provider_instance_id") == preferred
            ]
        else:
            matches = list(providers) if len(providers) == 1 else []
        if len(matches) != 1:
            raise GlobalContractInvocationError(
                "missing_provider",
                f"select exactly one provider for {contract_id}",
            )
        provider_id = str(matches[0].get("provider_instance_id") or "")
        provider_operation = matches[0].get("operation_id")
        if not isinstance(provider_operation, str) or not provider_operation.strip():
            raise GlobalContractInvocationError(
                "missing_provider", "selected provider has no captured operation identity"
            )
        if resolve is not None:
            request = resolve(client, request)
        value = client.invoke(
            contract_id,
            provider_operation,
            request,
            provider_instance_id=provider_id,
        )
        result = normalizer(value)
        return {"status": "ok", "provider_instance_id": provider_id, **result}

    return operation


def _transcribe_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Bound one explicit transcription request to validated inline content."""
    _reject_denied_keys(payload, _AUDIO_DENIED_REQUEST_KEYS)
    request = dict(payload)
    request["model_profile_id"] = _model_profile_id(request)
    request["parameters"] = _parameters(request.get("parameters"))
    request["audio"] = _audio_inline_content(request.get("audio"))
    language = request.get("language")
    if language is not None:
        if (
            not isinstance(language, str)
            or not language.strip()
            or len(language) > _MAX_LANGUAGE_CHARS
            or any(char.isspace() for char in language.strip())
        ):
            raise _invalid_request("audio language is invalid")
        request["language"] = language.strip()
    return request


def _speech_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Bound one explicit speech synthesis request to validated text input."""
    _reject_denied_keys(payload, _AUDIO_DENIED_REQUEST_KEYS)
    request = dict(payload)
    request["model_profile_id"] = _model_profile_id(request)
    request["parameters"] = _parameters(request.get("parameters"))
    text = request.get("input")
    if text is None:
        text = request.get("text")
    if (
        not isinstance(text, str)
        or not text
        or len(text) > _MAX_TEXT_INPUT_CHARS
    ):
        raise _invalid_request("speech input text is invalid")
    request["input"] = text
    request.pop("text", None)
    voice = request.get("voice")
    if voice is not None:
        if (
            not isinstance(voice, str)
            or not voice.strip()
            or len(voice) > _MAX_VOICE_CHARS
        ):
            raise _invalid_request("speech voice is invalid")
        request["voice"] = voice.strip()
    return request


def _model_profile_id(request: Mapping[str, Any]) -> str:
    """Require one registered Model Registry profile identifier.

    ``x-tobkiri-selector: model-profile``: the caller supplies the registered
    model *profile* ID (the shared picker's value), never a raw provider
    model ID, provider instance, or execution ``profile_id``.
    """
    value = request.get("model_profile_id")
    if not isinstance(value, str):
        raise _invalid_request("a registered model_profile_id is required")
    identifier = value.strip()
    if (
        not identifier
        or len(identifier) > _MAX_MODEL_PROFILE_ID_CHARS
        or _MODEL_PROFILE_ID.fullmatch(identifier) is None
    ):
        raise _invalid_request("model_profile_id is not a registered reference")
    return identifier


def _parameters(value: Any) -> dict[str, Any]:
    """Project caller parameters to canonical-safe scalars only.

    Canonical Host journaling cannot carry floats, so fractional options are
    expressed as exact decimal strings (or integers) and converted for the
    wire downstream.  Raw floats are rejected rather than normalized.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _invalid_request("audio parameters are invalid")
    for item in value.values():
        if isinstance(item, (float, bytes, bytearray)) or isinstance(
            item, (Mapping, list, tuple)
        ):
            raise _invalid_request(
                "audio parameters must be canonical-safe scalars"
            )
    return dict(value)


def _resolve_model_route(
    client: GlobalContractClient,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the required model profile into provider routing fields.

    Both the provider binding (``metadata.provider_connection_id``) and the
    provider model ID (``model_id``) come from the registered profile record;
    disabled or missing profiles fail closed.
    """
    identifier = request["model_profile_id"]
    try:
        resolved = client.invoke(
            _MODEL_PROFILE_CONTRACT,
            _MODEL_PROFILE_OPERATION,
            {"identifier": identifier},
        )
    except (
        GlobalContractInvocationError,
        KeyError,
        PermissionError,
        ValueError,
        TypeError,
    ) as exc:
        raise GlobalContractInvocationError(
            "unresolved_profile", "model profile is unavailable"
        ) from exc
    profile = resolved.get("profile") if isinstance(resolved, Mapping) else None
    if not isinstance(profile, Mapping) or profile.get("enabled") is not True:
        raise GlobalContractInvocationError(
            "unresolved_profile", "model profile is unavailable"
        )
    model_id = profile.get("model_id")
    if (
        not isinstance(model_id, str)
        or not model_id.strip()
        or len(model_id.strip()) > _MAX_MODEL_ID_CHARS
    ):
        raise GlobalContractInvocationError(
            "unresolved_profile", "model profile model_id is invalid"
        )
    metadata = profile.get("metadata")
    connection_id = (
        metadata.get("provider_connection_id")
        if isinstance(metadata, Mapping)
        else None
    )
    # Match the Model Registry's effective binding for rich legacy profiles.
    # A supplied non-empty metadata binding always wins; malformed bindings
    # are rejected rather than silently redirected to a different provider.
    if not connection_id:
        requirements = profile.get("requirements")
        connection_id = (
            requirements.get("preferred_provider_instance_id")
            if isinstance(requirements, Mapping) else None
        )
    if (
        not isinstance(connection_id, str)
        or not connection_id
        or len(connection_id) > 256
    ):
        raise GlobalContractInvocationError(
            "unresolved_profile", "model profile provider binding is invalid"
        )
    request["model_id"] = model_id.strip()
    request["provider_connection_id"] = connection_id
    request["model_profile_id"] = str(
        resolved.get("resolved_profile_id") or identifier
    )
    return request


def _audio_inline_content(value: Any) -> dict[str, Any]:
    """Validate one bounded inline audio upload.

    Inline content is a caller-declared byte string whose ``content_id``
    self-hash is verified against the decoded bytes.  It is *validated
    inline content*, not a stored artifact reference: no ownership,
    persistence, or locator authority is implied.  A path, URI, URL, or
    any other dereferenceable locator is never accepted, so the gateway
    cannot be turned into an arbitrary read primitive.
    """
    if not isinstance(value, Mapping):
        raise _invalid_request("inline audio content is required")
    unknown = set(value) - {
        "content_id",
        "media_type",
        "data_base64",
        "byte_size",
        "filename",
    }
    if unknown or _DENIED_REQUEST_KEYS.intersection(value):
        raise GlobalContractInvocationError(
            "denied", "audio accepts only bounded inline content"
        )
    content_id = value.get("content_id")
    if not isinstance(content_id, str) or not _CONTENT_ID.match(content_id):
        raise _invalid_request("audio content_id is invalid")
    media_type = value.get("media_type")
    if not isinstance(media_type, str) or media_type.lower() not in _AUDIO_MEDIA_TYPES:
        raise _invalid_request("audio media_type is not supported")
    encoded = value.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        raise _invalid_request("audio data_base64 is required")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid_request("audio data_base64 is invalid") from None
    if not decoded or len(decoded) > _MAX_AUDIO_CONTENT_BYTES:
        raise _invalid_request("audio content exceeds the byte bound")
    declared_size = value.get("byte_size")
    if (
        declared_size is not None
        and (type(declared_size) is not int or declared_size != len(decoded))
    ):
        raise _invalid_request("audio byte_size does not match its bytes")
    if content_id != "sha256:" + hashlib.sha256(decoded).hexdigest():
        raise _invalid_request("audio content_id does not match its bytes")
    content: dict[str, Any] = {
        "content_id": content_id,
        "media_type": media_type.lower(),
        "data_base64": encoded,
        "byte_size": len(decoded),
    }
    filename = value.get("filename")
    if filename is not None:
        filename = _filename(filename)
        if filename is not None:
            content["filename"] = filename
    return content


def _filename(value: Any) -> str | None:
    if not isinstance(value, str):
        raise _invalid_request("audio filename is invalid")
    name = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if (
        not name
        or name in {".", ".."}
        or len(name) > 255
        or any(ord(char) < 32 for char in name)
    ):
        raise _invalid_request("audio filename is invalid")
    return name


def _audio_content_result(value: Any) -> dict[str, Any]:
    """Validate one provider-returned bounded inline audio result."""
    try:
        return _audio_inline_content(value)
    except GlobalContractInvocationError as exc:
        if exc.code == "invalid_request":
            raise _invalid() from exc
        raise


def _reject_denied_keys(
    payload: Mapping[str, Any],
    denied_keys: frozenset[str] = _DENIED_REQUEST_KEYS,
) -> None:
    denied = sorted(denied_keys.intersection(payload))
    if denied:
        raise GlobalContractInvocationError(
            "denied", f"request fields are not permitted: {', '.join(denied)}"
        )


def _embedding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid()
    vectors = value.get("vectors")
    if not isinstance(vectors, list) or not vectors:
        vector = value.get("vector")
        vectors = [vector] if isinstance(vector, list) else []
    normalized = []
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise _invalid()
        numbers = []
        for item in vector:
            try:
                number = float(item)
            except (TypeError, ValueError):
                raise _invalid() from None
            if not math.isfinite(number):
                raise _invalid()
            numbers.append(number)
        normalized.append(numbers)
    if not normalized:
        raise _invalid()
    return {"vectors": normalized, "usage": dict(value.get("usage") or {})}


def _image(value: Any) -> dict[str, Any]:
    artifacts = value.get("artifacts") if isinstance(value, Mapping) else None
    if not isinstance(artifacts, list) or not all(
        isinstance(item, Mapping) and item.get("artifact_id")
        for item in artifacts
    ):
        raise _invalid()
    return {"artifacts": [dict(item) for item in artifacts]}


def _transcript(value: Any) -> dict[str, Any]:
    """Project a provider transcript to the bounded public result shape.

    Only allowlisted canonical-safe fields cross the journal boundary: floats
    (provider seconds offsets) arrive upstream as integer ``*_ms`` fields and
    anything else is dropped rather than forwarded unprojected.
    """
    text = value.get("text") if isinstance(value, Mapping) else None
    if not isinstance(text, str) or len(text) > _MAX_TRANSCRIPT_TEXT_CHARS:
        raise _invalid()
    language = value.get("language")
    if language is not None and (
        not isinstance(language, str)
        or not language
        or len(language) > _MAX_LANGUAGE_CHARS
    ):
        raise _invalid()
    segments = value.get("segments")
    if segments is None:
        segments = []
    if not isinstance(segments, list) or len(segments) > _MAX_TRANSCRIPT_SEGMENTS:
        raise _invalid()
    projected = [_transcript_segment(item) for item in segments]
    return {
        "text": text,
        "language": language,
        "segments": projected,
    }


def _transcript_segment(item: Any) -> dict[str, Any]:
    """Project one provider segment to bounded ints/strings only."""
    if not isinstance(item, Mapping):
        raise _invalid()
    segment: dict[str, Any] = {}
    text = item.get("text")
    if isinstance(text, str) and text and len(text) <= _MAX_SEGMENT_TEXT_CHARS:
        segment["text"] = text
    for key in ("start_ms", "end_ms"):
        number = item.get(key)
        if type(number) is int and 0 <= number <= _MAX_SEGMENT_MILLISECONDS:
            segment[key] = number
    speaker = item.get("speaker")
    if (
        isinstance(speaker, str)
        and speaker.strip()
        and len(speaker.strip()) <= _MAX_SEGMENT_SPEAKER_CHARS
    ):
        segment["speaker"] = speaker.strip()
    return segment


def _speech(value: Any) -> dict[str, Any]:
    content = value.get("content") if isinstance(value, Mapping) else None
    if not isinstance(content, Mapping):
        raise _invalid()
    return {"content": _audio_content_result(content)}


def _invalid() -> GlobalContractInvocationError:
    return GlobalContractInvocationError(
        "invalid_response", "modality provider returned an invalid result"
    )


def _invalid_request(message: str) -> GlobalContractInvocationError:
    return GlobalContractInvocationError("invalid_request", message)


class ModalityGatewayHostFactoryV4:
    """Capture one exact modality gateway Function behind the Host Broker."""

    def __init__(
        self,
        function_id: str,
        *,
        contract_id: str,
        operation_id: str,
        operation_name: str,
        provider_contract_id: str,
        operation_factory: Any,
        extra_allowed_contract_ids: frozenset[str] = frozenset(),
    ) -> None:
        self.function_id = function_id
        self._contract_id = contract_id
        self._operation_id = operation_id
        self._operation_name = operation_name
        self._allowed_contract_ids = frozenset(
            {provider_contract_id, *extra_allowed_contract_ids}
        )
        self._operation_factory = operation_factory

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind only one Plan-pinned modality Function and its provider edge."""

        if not context.provider_bindings or any(
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != self._contract_id
            or binding.operation.operation_id != self._operation_id
            for binding in context.provider_bindings
        ):
            raise PermissionError("modality gateway provider bindings are incomplete")

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if operation_id != self._operation_id:
                raise PermissionError("modality gateway operation identity is invalid")
            invocation.assert_current()
            # The gateway only fans out to one declared provider contract; it
            # never touches credential material or the Host transport itself.
            client = invocation.contract_client(
                allowed_contract_ids=self._allowed_contract_ids,
                consumer_pack_id="rumi_ai_modality_pack",
                include_credentials=False,
            )
            result = self._operation_factory(client)(self._operation_name, payload)
            invocation.assert_current()
            return result

        contributions: list[HostProviderContributionV4] = []
        for binding in context.provider_bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                raise PermissionError("modality gateway domain binding is unavailable")
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
    _EMBEDDING_FUNCTION_ID: ModalityGatewayHostFactoryV4(
        _EMBEDDING_FUNCTION_ID,
        contract_id=_MODALITY_EMBEDDING_CONTRACT,
        operation_id="rumi_ai_modality_pack.ai-embedding",
        operation_name="embed",
        provider_contract_id=_EMBEDDING_PROVIDER,
        operation_factory=create_embedding_operation,
    ),
    _IMAGE_FUNCTION_ID: ModalityGatewayHostFactoryV4(
        _IMAGE_FUNCTION_ID,
        contract_id=_MODALITY_IMAGE_CONTRACT,
        operation_id="rumi_ai_modality_pack.ai-image",
        operation_name="generate",
        provider_contract_id=_IMAGE_PROVIDER,
        operation_factory=create_image_operation,
    ),
    _TRANSCRIBE_FUNCTION_ID: ModalityGatewayHostFactoryV4(
        _TRANSCRIBE_FUNCTION_ID,
        contract_id=_MODALITY_TRANSCRIBE_CONTRACT,
        operation_id="rumi_ai_modality_pack.ai-transcribe",
        operation_name="transcribe",
        provider_contract_id=_TRANSCRIBE_PROVIDER,
        operation_factory=create_audio_transcribe_operation,
        extra_allowed_contract_ids=frozenset({_MODEL_PROFILE_CONTRACT}),
    ),
    _SPEECH_FUNCTION_ID: ModalityGatewayHostFactoryV4(
        _SPEECH_FUNCTION_ID,
        contract_id=_MODALITY_SPEECH_CONTRACT,
        operation_id="rumi_ai_modality_pack.ai-speech",
        operation_name="synthesize",
        provider_contract_id=_SPEECH_PROVIDER,
        operation_factory=create_audio_speech_operation,
        extra_allowed_contract_ids=frozenset({_MODEL_PROFILE_CONTRACT}),
    ),
}
