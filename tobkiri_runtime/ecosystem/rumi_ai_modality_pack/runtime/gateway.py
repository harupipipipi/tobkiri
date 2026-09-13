"""Dispatch non-chat modalities through selected typed provider contracts."""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping

from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)

_EMBEDDING_PROVIDER = "rumi.service.ai.provider.embedding.v1"
_IMAGE_PROVIDER = "rumi.service.ai.provider.image.v1"
_TRANSCRIBE_PROVIDER = "rumi.service.ai.provider.audio.transcribe.v1"
_SPEECH_PROVIDER = "rumi.service.ai.provider.audio.speech.v1"


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
    return _operation(client, _TRANSCRIBE_PROVIDER, "transcribe", _transcript)


def create_audio_speech_operation(
    client: GlobalContractClient,
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    """Create the provider-neutral speech synthesis gateway."""
    return _operation(client, _SPEECH_PROVIDER, "synthesize", _speech)


def _operation(
    client: GlobalContractClient,
    contract_id: str,
    expected_operation: str,
    normalizer: Callable[[Any], dict[str, Any]],
) -> Callable[[str, Mapping[str, Any]], dict[str, Any]]:
    def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if name not in {expected_operation, "invoke"}:
            raise ValueError(f"unknown modality operation: {name}")
        providers = client.providers(contract_id)
        preferred = str(payload.get("provider_instance_id") or "").strip()
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
        request = dict(payload)
        request.pop("provider_instance_id", None)
        value = client.invoke(
            contract_id,
            expected_operation,
            request,
            provider_instance_id=provider_id,
        )
        result = normalizer(value)
        return {"status": "ok", "provider_instance_id": provider_id, **result}

    return operation


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
    text = value.get("text") if isinstance(value, Mapping) else None
    if not isinstance(text, str):
        raise _invalid()
    return {
        "text": text,
        "language": value.get("language"),
        "segments": list(value.get("segments") or []),
    }


def _speech(value: Any) -> dict[str, Any]:
    artifact = value.get("artifact") if isinstance(value, Mapping) else None
    if not isinstance(artifact, Mapping) or not artifact.get("artifact_id"):
        raise _invalid()
    return {"artifact": dict(artifact)}


def _invalid() -> GlobalContractInvocationError:
    return GlobalContractInvocationError(
        "invalid_response", "modality provider returned an invalid result"
    )

