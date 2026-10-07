"""Focused specifications for the canonical audio runtime path (v4).

Covered surface:

* ``HostBoundCredentialTransport.post_multipart``/``post_binary`` — bounded
  multipart transcription requests and bounded binary speech responses
  through the same single-use credential lease (no general HTTP API).
* ``rumi_provider_adapters_pack`` audio operations — real OpenAI-compatible
  ``/audio/transcriptions`` (multipart) and ``/audio/speech`` (JSON in,
  binary out) wire formats behind the registry-bound connection.
* ``rumi_ai_modality_pack`` gateways — explicit registered model IDs and
  validated inline audio content (digest-checked, bounded), never a
  dereferenceable path/URL.
* Captured Host Provider factories — exact binding validation, captured
  authority ordering, and the allowed-contract sets.

All transport tests run against deterministic fake openers; no network, no
credential material, and no external AI calls are involved.
"""

from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from core_runtime import credential_transport as transport_module
from core_runtime.credential_transport import (
    AuthorizedEnvelopeCredentialTransport,
    CredentialTransportDenied,
    HostBoundCredentialTransport,
    _encode_multipart_form_data,
)
from core_runtime.global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
    HostCredentialTransportError,
)
from ecosystem.rumi_ai_modality_pack.runtime.gateway import (
    HOST_PROVIDER_FACTORY as MODALITY_HOST_PROVIDER_FACTORY,
    ModalityGatewayHostFactoryV4,
    create_audio_speech_operation,
    create_audio_transcribe_operation,
)
from ecosystem.rumi_provider_adapters_pack.runtime.adapter import (
    _PROVIDER_OPERATIONS,
    HOST_PROVIDER_FACTORY,
    ProviderAdapterHostFactoryV4,
    create_audio_speech_operation as create_adapter_speech_operation,
    create_audio_transcribe_operation as create_adapter_transcribe_operation,
)
from ecosystem.rumi_credential_broker_pack.runtime.service import (
    CredentialBrokerService,
)
from tests.test_credential_broker_pack import _dispatched_envelope
from tobkiri_protocol.canonical import canonical_json, canonical_digest
from tobkiri_host.broker import PreparedInvocationSnapshot, _snapshot_mapping

_TRANSCRIBE_CONTRACT = "tobkiri.service.ai.audio.transcribe.v1"
_SPEECH_CONTRACT = "tobkiri.service.ai.audio.speech.v1"
_PROVIDER_TRANSCRIBE = "tobkiri.service.ai.provider.audio.transcribe.v1"
_PROVIDER_SPEECH = "tobkiri.service.ai.provider.audio.speech.v1"
_MODEL_PROFILE_CONTRACT = "tobkiri.resource.ai.model.profile.v1"
_MODEL_PROFILE_OPERATION = (
    "rumi_model_registry_pack.model-profile-resource.generate"
)
_ADAPTER_TRANSCRIBE_FN = (
    "rumi_provider_adapters_pack.provider.compatibility.audio_transcribe"
)
_ADAPTER_SPEECH_FN = (
    "rumi_provider_adapters_pack.provider.compatibility.audio_speech"
)

_AUDIO_BYTES = b"\xff\xfb\x90\x44fake-mp3-frames"
_AUDIO_ID = "sha256:" + hashlib.sha256(_AUDIO_BYTES).hexdigest()
_AUDIO_B64 = base64.b64encode(_AUDIO_BYTES).decode("ascii")


class _Response:
    def __init__(self, value: dict[str, Any]) -> None:
        self._value = value

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int | None = None) -> bytes:
        return json.dumps(self._value).encode("utf-8")[:amount]


class _BytesResponse:
    def __init__(self, value: bytes) -> None:
        self._value = value

    def __enter__(self) -> "_BytesResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int | None = None) -> bytes:
        return self._value[:amount]


def _inline_audio(**overrides: Any) -> dict[str, Any]:
    content = {
        "content_id": _AUDIO_ID,
        "media_type": "audio/mpeg",
        "data_base64": _AUDIO_B64,
    }
    content.update(overrides)
    return content


def _audio_transport(tmp_path, *, scopes: list[str]):
    """Build one real Host-bound transport lease for audio-scoped tests."""
    service = CredentialBrokerService(user_data_root=tmp_path / "credential")
    created = service.invoke(
        "create",
        {
            "secret_material": {"api_key": "audio-test-secret"},
            "profile_id": "profile-1",
            "consumer_pack_id": "rumi_provider_adapters_pack",
            "provider_instance_id": "provider.review-a",
            "scopes": scopes,
        },
    )
    authority, envelope = _dispatched_envelope(tmp_path / "dispatch")
    transport = HostBoundCredentialTransport.from_authorized_envelope(
        envelope,
        provider_principal=authority.target,
        store=service.store,
        authority_store=authority.store,
        credential_handle=created["handle"],
        credential_key_version=created["key_version"],
        provider_instance_id="provider.review-a",
        credential_scope="ai.audio.transcribe",
        credential_purpose="provider.invoke",
        endpoint_origin="https://provider.example",
        current_security_epoch=lambda: authority.store.security_epoch,
        consumer_pack_id="rumi_provider_adapters_pack",
    )
    arguments = {
        "endpoint": "https://provider.example/v1/audio/transcriptions",
        "headers": {},
        "fields": {"model": "whisper-1", "response_format": "json"},
        "file_field": "file",
        "filename": "clip.mp3",
        "content_type": "audio/mpeg",
        "content": _AUDIO_BYTES,
        "credential_handle": created["handle"],
        "provider_instance_id": "provider.review-a",
        "credential_scope": "ai.audio.transcribe",
        "credential_scheme": "bearer",
        "deadline": 9_999_999_999.0,
    }
    return transport, arguments


# ---------------------------------------------------------------------------
# Multipart encoder: exact byte-level contract
# ---------------------------------------------------------------------------


def test_multipart_encoding_is_exact_and_deterministic() -> None:
    body, content_type = _encode_multipart_form_data(
        {"model": "whisper-1", "language": "en"},
        file_field="file",
        filename="clip.mp3",
        content_type="audio/mpeg",
        content=_AUDIO_BYTES,
    )
    body_again, _ = _encode_multipart_form_data(
        {"language": "en", "model": "whisper-1"},
        file_field="file",
        filename="clip.mp3",
        content_type="audio/mpeg",
        content=_AUDIO_BYTES,
    )
    assert body == body_again
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1]
    delimiter = ("--" + boundary).encode("ascii")
    expected = b"".join(
        [
            delimiter
            + b'\r\nContent-Disposition: form-data; name="language"'
            + b"\r\n\r\nen\r\n",
            delimiter
            + b'\r\nContent-Disposition: form-data; name="model"'
            + b"\r\n\r\nwhisper-1\r\n",
            delimiter
            + b'\r\nContent-Disposition: form-data; name="file";'
            + b' filename="clip.mp3"\r\nContent-Type: audio/mpeg\r\n\r\n'
            + _AUDIO_BYTES
            + b"\r\n",
            delimiter + b"--\r\n",
        ]
    )
    assert body == expected


def test_multipart_rejects_malformed_fields_and_locators() -> None:
    with pytest.raises(CredentialTransportDenied):
        _encode_multipart_form_data(
            {"model": "ok", "evil\r\nfield": "x"},
            file_field="file",
            filename="clip.mp3",
            content_type="audio/mpeg",
            content=_AUDIO_BYTES,
        )
    body, _ = _encode_multipart_form_data(
        {"model": "ok"},
        file_field="file",
        filename="../escape.mp3",
        content_type="audio/mpeg",
        content=_AUDIO_BYTES,
    )
    # Dereferenceable filenames collapse to a basename label only.
    assert b'filename="escape.mp3"' in body
    assert b'filename="../' not in body
    with pytest.raises(CredentialTransportDenied):
        _encode_multipart_form_data(
            {"model": "ok"},
            file_field="file",
            filename='x"quote.mp3',
            content_type="audio/mpeg",
            content=_AUDIO_BYTES,
        )
    with pytest.raises(CredentialTransportDenied):
        _encode_multipart_form_data(
            {"model": "ok"},
            file_field="file",
            filename="clip.mp3",
            content_type="text/html",
            content=_AUDIO_BYTES,
        )
    with pytest.raises(CredentialTransportDenied):
        _encode_multipart_form_data(
            {"model": "ok"},
            file_field="file",
            filename="clip.mp3",
            content_type="audio/mpeg",
            content=b"",
        )
    with pytest.raises(CredentialTransportDenied):
        _encode_multipart_form_data(
            {f"field_{index}": "x" for index in range(17)},
            file_field="file",
            filename="clip.mp3",
            content_type="audio/mpeg",
            content=_AUDIO_BYTES,
        )
    with pytest.raises(CredentialTransportDenied):
        _encode_multipart_form_data(
            {"file": "forged-name"},
            file_field="file",
            filename="clip.mp3",
            content_type="audio/mpeg",
            content=_AUDIO_BYTES,
        )


# ---------------------------------------------------------------------------
# HostBoundCredentialTransport audio primitives
# ---------------------------------------------------------------------------


def test_post_multipart_sends_exact_wire_bytes(tmp_path, monkeypatch) -> None:
    transport, arguments = _audio_transport(
        tmp_path, scopes=["ai.audio.transcribe"]
    )
    captured: dict[str, Any] = {}

    def open_request(request, **_kwargs):
        captured["url"] = request.full_url
        captured["content_type"] = request.headers.get("Content-type")
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = request.data
        return _Response({"text": "hello", "language": "en"})

    monkeypatch.setattr(transport, "_opener", open_request)
    result = transport.post_multipart(**arguments)

    assert result == {"text": "hello", "language": "en"}
    assert captured["url"] == (
        "https://provider.example/v1/audio/transcriptions"
    )
    assert captured["content_type"].startswith(
        "multipart/form-data; boundary=TobkiriFormBoundary"
    )
    assert captured["authorization"] == "Bearer audio-test-secret"
    body = captured["body"]
    assert b'name="model"\r\n\r\nwhisper-1' in body
    assert b'name="response_format"\r\n\r\njson' in body
    assert b'name="file"; filename="clip.mp3"' in body
    assert b"Content-Type: audio/mpeg\r\n\r\n" + _AUDIO_BYTES in body
    # The body is reproducible byte-for-byte by the declared encoder.
    expected_body, _ = _encode_multipart_form_data(
        arguments["fields"],
        file_field="file",
        filename="clip.mp3",
        content_type="audio/mpeg",
        content=_AUDIO_BYTES,
    )
    assert body == expected_body


def test_post_multipart_lease_is_single_use(tmp_path, monkeypatch) -> None:
    transport, arguments = _audio_transport(
        tmp_path, scopes=["ai.audio.transcribe"]
    )
    monkeypatch.setattr(
        transport, "_opener", lambda *_args, **_kwargs: _Response({"text": "x"})
    )
    assert transport.post_multipart(**arguments) == {"text": "x"}

    with pytest.raises(CredentialTransportDenied) as denied:
        transport.post_multipart(**arguments)

    # Single-use denial is one fixed material-independent code.
    assert denied.value.code == "binding_invalid"


def test_post_binary_returns_bounded_audio_bytes(tmp_path, monkeypatch) -> None:
    transport, arguments = _audio_transport(
        tmp_path, scopes=["ai.audio.transcribe"]
    )
    captured: dict[str, Any] = {}
    audio = b"\x49\x44\x33id3-tagged-mp3-bytes"

    def open_request(request, **_kwargs):
        captured["body"] = request.data
        captured["content_type"] = request.headers.get("Content-type")
        return _BytesResponse(audio)

    monkeypatch.setattr(transport, "_opener", open_request)
    result = transport.post_binary(
        endpoint="https://provider.example/v1/audio/speech",
        headers={},
        body={"model": "tts-1", "input": "hello", "voice": "alloy"},
        credential_handle=arguments["credential_handle"],
        provider_instance_id="provider.review-a",
        credential_scope="ai.audio.transcribe",
        credential_scheme="bearer",
        deadline=9_999_999_999.0,
    )

    assert result == audio
    assert captured["content_type"] == "application/json"
    assert json.loads(captured["body"].decode("utf-8")) == {
        "model": "tts-1",
        "input": "hello",
        "voice": "alloy",
    }


def test_post_binary_denies_responses_disclosing_secret_material(
    tmp_path, monkeypatch
) -> None:
    transport, arguments = _audio_transport(
        tmp_path, scopes=["ai.audio.transcribe"]
    )
    monkeypatch.setattr(
        transport,
        "_opener",
        lambda *_args, **_kwargs: _BytesResponse(
            b"audio-prefix audio-test-secret audio-suffix"
        ),
    )
    with pytest.raises(CredentialTransportDenied) as denied:
        transport.post_binary(
            endpoint="https://provider.example/v1/audio/speech",
            headers={},
            body={"model": "tts-1", "input": "hi", "voice": "alloy"},
            credential_handle=arguments["credential_handle"],
            provider_instance_id="provider.review-a",
            credential_scope="ai.audio.transcribe",
            credential_scheme="bearer",
            deadline=9_999_999_999.0,
        )

    assert denied.value.code == "response_invalid"


def test_audio_scope_absent_fails_closed(tmp_path, monkeypatch) -> None:
    """A credential stored without the audio scope can never serve it."""
    transport, arguments = _audio_transport(tmp_path, scopes=["ai.generate"])
    opened = []
    monkeypatch.setattr(
        transport,
        "_opener",
        lambda *_args, **_kwargs: opened.append(True) or _Response({}),
    )

    with pytest.raises(CredentialTransportDenied) as denied:
        transport.post_multipart(**arguments)

    assert denied.value.code == "store_failure"
    assert opened == []


def test_envelope_transport_multipart_consumes_the_single_use_lease(
    tmp_path, monkeypatch
) -> None:
    service = CredentialBrokerService(user_data_root=tmp_path / "credential")
    created = service.invoke(
        "create",
        {
            "secret_material": {"api_key": "audio-test-secret"},
            "profile_id": "profile-1",
            "consumer_pack_id": "rumi_provider_adapters_pack",
            "provider_instance_id": "provider.review-a",
            "scopes": ["ai.audio.speech"],
        },
    )
    authority, envelope = _dispatched_envelope(tmp_path / "dispatch")
    outer = AuthorizedEnvelopeCredentialTransport(
        envelope=envelope,
        provider_principal=authority.target,
        store=service.store,
        authority_store=authority.store,
        current_security_epoch=lambda: authority.store.security_epoch,
        credential_key_version=created["key_version"],
        consumer_pack_id="rumi_provider_adapters_pack",
    )
    monkeypatch.setattr(
        transport_module,
        "_open_pinned_request",
        lambda *_args, **_kwargs: _BytesResponse(b"mp3-payload"),
    )
    result = outer.post_binary(
        endpoint="https://provider.example/v1/audio/speech",
        headers={},
        body={"model": "tts-1", "input": "hi", "voice": "alloy"},
        credential_handle=created["handle"],
        provider_instance_id="provider.review-a",
        credential_scope="ai.audio.speech",
        credential_scheme="bearer",
        deadline=9_999_999_999.0,
    )
    assert result == b"mp3-payload"

    with pytest.raises(CredentialTransportDenied) as denied:
        outer.post_binary(
            endpoint="https://provider.example/v1/audio/speech",
            headers={},
            body={"model": "tts-1", "input": "hi", "voice": "alloy"},
            credential_handle=created["handle"],
            provider_instance_id="provider.review-a",
            credential_scope="ai.audio.speech",
            credential_scheme="bearer",
            deadline=9_999_999_999.0,
        )
    assert denied.value.code == "binding_invalid"


# ---------------------------------------------------------------------------
# GlobalContractClient audio surface (fail-closed dispatch)
# ---------------------------------------------------------------------------


def test_client_audio_calls_fail_closed_without_host_transport() -> None:
    """A missing Host transport capability never resolves credential work."""
    client = _client_without_transport()

    with pytest.raises(PermissionError):
        client.post_multipart_with_credential(
            endpoint="https://provider.example/v1/audio/transcriptions",
            headers={},
            fields={"model": "whisper-1"},
            file_field="file",
            filename="clip.mp3",
            content_type="audio/mpeg",
            content=_AUDIO_BYTES,
            credential_handle="credential:x",
            provider_instance_id="provider.review-a",
            credential_scope="ai.audio.transcribe",
            credential_scheme="bearer",
            deadline=9_999_999_999.0,
        )
    with pytest.raises(PermissionError):
        client.post_binary_with_credential(
            endpoint="https://provider.example/v1/audio/speech",
            headers={},
            body={},
            credential_handle="credential:x",
            provider_instance_id="provider.review-a",
            credential_scope="ai.audio.speech",
            credential_scheme="bearer",
            deadline=9_999_999_999.0,
        )


def _client_without_transport() -> GlobalContractClient:
    return GlobalContractClient(
        session=None,
        allowed_contract_ids=frozenset(),
        consumer_pack_id="test",
        host_credential_transport=None,
    )


def test_client_wraps_audio_transport_failures() -> None:
    """Any transport error surfaces as one fixed host-transport failure."""
    def _raise(**_kwargs):
        raise PermissionError("store_failure")

    transport = SimpleNamespace(post_multipart=_raise, post_binary=_raise)
    client = GlobalContractClient(
        session=None,
        allowed_contract_ids=frozenset(),
        consumer_pack_id="test",
        host_credential_transport=transport,
    )
    with pytest.raises(HostCredentialTransportError):
        client.post_multipart_with_credential(
            endpoint="https://provider.example/v1/audio/transcriptions",
            headers={},
            fields={"model": "whisper-1"},
            file_field="file",
            filename="clip.mp3",
            content_type="audio/mpeg",
            content=_AUDIO_BYTES,
            credential_handle="credential:x",
            provider_instance_id="provider.review-a",
            credential_scope="ai.audio.transcribe",
            credential_scheme="bearer",
            deadline=9_999_999_999.0,
        )
    with pytest.raises(HostCredentialTransportError):
        client.post_binary_with_credential(
            endpoint="https://provider.example/v1/audio/speech",
            headers={},
            body={"model": "tts-1", "input": "hi", "voice": "alloy"},
            credential_handle="credential:x",
            provider_instance_id="provider.review-a",
            credential_scope="ai.audio.speech",
            credential_scheme="bearer",
            deadline=9_999_999_999.0,
        )


# ---------------------------------------------------------------------------
# Provider adapter audio operations (real wire formats, fake transport)
# ---------------------------------------------------------------------------


def _adapter_connection_client(response: Any, *, binary: bool):
    class FakeHostClient:
        def __init__(self) -> None:
            self.multipart: dict[str, Any] = {}
            self.binary_call: dict[str, Any] = {}

        def invoke(self, contract_id, operation, payload):
            assert contract_id == "tobkiri.resource.ai.provider.registry.v1"
            return {
                "providers": [
                    {
                        "provider_instance_id": "provider.review-a",
                        "adapter_id": "openai-compatible",
                        "credential_handle": "credential:opaque-review-a",
                        "endpoint": "https://provider.example/v1",
                        "enabled": True,
                    }
                ]
            }

        def post_multipart_with_credential(self, **kwargs):
            self.multipart = dict(kwargs)
            return response

        def post_binary_with_credential(self, **kwargs):
            self.binary_call = dict(kwargs)
            return response

    return FakeHostClient()


def test_adapter_transcribe_posts_real_multipart_contract() -> None:
    client = _adapter_connection_client(
        {"text": "hello world", "language": "en"}, binary=False
    )
    operation = create_adapter_transcribe_operation(client)

    result = operation(
        "transcribe",
        {
            "provider_connection_id": "provider.review-a",
            "model_id": "review-a/whisper-1",
            "model_profile_id": "profile.review-stt",
            "audio": _inline_audio(filename="clip.mp3"),
            "language": "en",
            "parameters": {"prompt": "meeting notes", "temperature": "0.2"},
        },
    )

    assert result == {
        "text": "hello world",
        "language": "en",
        "segments": [],
    }
    call = client.multipart
    assert call["endpoint"] == (
        "https://provider.example/v1/audio/transcriptions"
    )
    assert call["fields"] == {
        "model": "whisper-1",
        "language": "en",
        "prompt": "meeting notes",
        "temperature": "0.2",
        "response_format": "json",
    }
    assert call["file_field"] == "file"
    assert call["filename"] == "clip.mp3"
    assert call["content_type"] == "audio/mpeg"
    assert call["content"] == _AUDIO_BYTES
    assert call["credential_handle"] == "credential:opaque-review-a"
    assert call["provider_instance_id"] == "provider.review-a"
    assert call["credential_scope"] == "ai.audio.transcribe"
    assert call["credential_scheme"] == "bearer"


def test_adapter_speech_posts_json_and_returns_validated_inline_content(
) -> None:
    audio = b"\xff\xfb\x90\x44speech-bytes"
    client = _adapter_connection_client(audio, binary=True)
    operation = create_adapter_speech_operation(client)

    result = operation(
        "synthesize",
        {
            "provider_connection_id": "provider.review-a",
            "model_id": "review-a/tts-1",
            "model_profile_id": "profile.review-tts",
            "input": "say hello",
            "voice": "alloy",
            "parameters": {"speed": "1.25", "response_format": "mp3"},
        },
    )

    expected_id = "sha256:" + hashlib.sha256(audio).hexdigest()
    assert result == {
        "content": {
            "content_id": expected_id,
            "media_type": "audio/mpeg",
            "data_base64": base64.b64encode(audio).decode("ascii"),
            "byte_size": len(audio),
        }
    }
    call = client.binary_call
    assert call["endpoint"] == "https://provider.example/v1/audio/speech"
    # The wire body carries a JSON number; the journaled request carried a
    # canonical-safe decimal string.
    assert call["body"] == {
        "model": "tts-1",
        "input": "say hello",
        "voice": "alloy",
        "response_format": "mp3",
        "speed": 1.25,
    }
    assert call["credential_handle"] == "credential:opaque-review-a"
    assert call["provider_instance_id"] == "provider.review-a"
    assert call["credential_scope"] == "ai.audio.speech"


def test_adapter_transcribe_projects_verbose_segments_to_milliseconds(
) -> None:
    """Provider float-second offsets become canonical integer milliseconds."""
    client = _adapter_connection_client(
        {
            "text": "hi",
            "language": "en",
            "segments": [
                {
                    "id": 0,
                    "text": "hi",
                    "start": 0.0,
                    "end": 1.25,
                    "speaker": "speaker-1",
                    # Non-allowlisted provider fields are dropped, not relayed.
                    "confidence": 0.9,
                    "words": [{"w": 0.5}],
                },
                {"text": "bye", "start": 1.3, "end": 2.0},
                {"start": 3.0},
            ],
            "usage": {"seconds": 2.0},
        },
        binary=False,
    )
    operation = create_adapter_transcribe_operation(client)

    result = operation(
        "transcribe",
        {
            "provider_connection_id": "provider.review-a",
            "model_id": "review-a/whisper-1",
            "audio": _inline_audio(),
            "parameters": {"response_format": "verbose_json"},
        },
    )

    # usage is never relayed; offsets are integer milliseconds.
    assert result == {
        "text": "hi",
        "language": "en",
        "segments": [
            {
                "text": "hi",
                "start_ms": 0,
                "end_ms": 1250,
                "speaker": "speaker-1",
            },
            {"text": "bye", "start_ms": 1300, "end_ms": 2000},
            {"start_ms": 3000},
        ],
    }
    canonical_json(result)
    canonical_digest(result)


@pytest.mark.parametrize(
    "payload",
    [
        {"provider_connection_id": "provider.review-a",
         "model_id": "review-a/tts-1", "input": "hi"},
        {"provider_connection_id": "provider.review-a",
         "model_id": "review-a/tts-1", "input": "hi", "voice": " "},
        {"provider_connection_id": "provider.review-a",
         "model_id": "review-a/tts-1", "input": "x" * 5000,
         "voice": "alloy"},
        {"provider_connection_id": "provider.review-a",
         "model_id": "review-a/tts-1", "input": "hi", "voice": "alloy",
         "parameters": {"response_format": "exe"}},
        {"provider_connection_id": "provider.review-a",
         "model_id": "review-a/tts-1", "input": "hi", "voice": "alloy",
         "parameters": {"speed": 1.25}},
        {"provider_connection_id": "provider.review-a",
         "model_id": "review-a/tts-1", "input": "hi", "voice": "alloy",
         "parameters": {"speed": "12.5"}},
        {"provider_connection_id": "provider.review-a",
         "model_id": "review-a/tts-1", "input": "hi", "voice": "alloy",
         "parameters": {"speed": "0.1"}},
    ],
    ids=[
        "missing-voice",
        "blank-voice",
        "overlong-input",
        "unknown-response-format",
        "raw-float-speed",
        "out-of-range-speed",
        "below-range-speed",
    ],
)
def test_adapter_speech_rejects_invalid_requests(payload) -> None:
    client = _adapter_connection_client(b"audio", binary=True)
    operation = create_adapter_speech_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation("synthesize", payload)

    assert error.value.code == "invalid_request"
    assert client.binary_call == {}


@pytest.mark.parametrize("denied_key", ["path", "uri", "url", "endpoint"])
def test_adapter_audio_rejects_dereferenceable_inputs(denied_key) -> None:
    client = _adapter_connection_client(
        {"text": "never"}, binary=False
    )
    operation = create_adapter_transcribe_operation(client)
    payload = {
        "provider_connection_id": "provider.review-a",
        "model_id": "review-a/whisper-1",
        "audio": _inline_audio(),
        denied_key: "/etc/passwd",
    }

    with pytest.raises(GlobalContractInvocationError) as error:
        operation("transcribe", payload)

    assert error.value.code == "denied"
    assert client.multipart == {}


@pytest.mark.parametrize(
    "denied_key",
    ["profile_id", "provider_instance_id", "deadline"],
)
def test_adapter_audio_rejects_caller_authority_fields(denied_key) -> None:
    """profile_id/provider pinning/deadline belong to resolved authority."""
    client = _adapter_connection_client({"text": "never"}, binary=False)
    operation = create_adapter_transcribe_operation(client)
    payload = {
        "provider_connection_id": "provider.review-a",
        "model_id": "review-a/whisper-1",
        "audio": _inline_audio(),
        denied_key: "caller-supplied",
    }

    with pytest.raises(GlobalContractInvocationError) as error:
        operation("transcribe", payload)

    assert error.value.code == "denied"
    assert client.multipart == {}


def test_adapter_audio_rejects_supplied_credential_handle() -> None:
    client = _adapter_connection_client({"text": "never"}, binary=False)
    operation = create_adapter_transcribe_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {
                "provider_connection_id": "provider.review-a",
                "model_id": "review-a/whisper-1",
                "audio": _inline_audio(),
                "credential_handle": "credential:forged",
            },
        )

    assert error.value.code == "denied"


def test_adapter_transcribe_requires_registered_model_id() -> None:
    client = _adapter_connection_client({"text": "never"}, binary=False)
    operation = create_adapter_transcribe_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {
                "provider_connection_id": "provider.review-a",
                "audio": _inline_audio(),
            },
        )

    assert error.value.code == "invalid_request"
    assert client.multipart == {}


def test_adapter_audio_rejects_unsupported_connection_adapter() -> None:
    class FakeHostClient:
        def invoke(self, *_args, **_kwargs):
            return {
                "providers": [
                    {
                        "provider_instance_id": "provider.review-a",
                        "adapter_id": "anthropic",
                        "credential_handle": "credential:opaque-review-a",
                        "endpoint": "https://provider.example/v1",
                        "enabled": True,
                    }
                ]
            }

        def post_multipart_with_credential(self, **_kwargs):
            raise AssertionError("unsupported adapter must not post")

    operation = create_adapter_transcribe_operation(FakeHostClient())
    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {
                "provider_connection_id": "provider.review-a",
                "model_id": "review-a/whisper-1",
                "audio": _inline_audio(),
            },
        )

    assert error.value.code == "incompatible"


def test_adapter_audio_transport_failures_are_provider_unavailable() -> None:
    class FakeHostClient:
        def invoke(self, *_args, **_kwargs):
            return {
                "providers": [
                    {
                        "provider_instance_id": "provider.review-a",
                        "adapter_id": "openai-compatible",
                        "credential_handle": "credential:opaque-review-a",
                        "endpoint": "https://provider.example/v1",
                        "enabled": True,
                    }
                ]
            }

        def post_multipart_with_credential(self, **_kwargs):
            raise HostCredentialTransportError

    operation = create_adapter_transcribe_operation(FakeHostClient())
    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {
                "provider_connection_id": "provider.review-a",
                "model_id": "review-a/whisper-1",
                "audio": _inline_audio(),
            },
        )

    assert error.value.code == "provider_unavailable"


def test_adapter_audio_operations_are_registered() -> None:
    assert _PROVIDER_OPERATIONS[_ADAPTER_TRANSCRIBE_FN] == (
        "transcribe",
        create_adapter_transcribe_operation,
    )
    assert _PROVIDER_OPERATIONS[_ADAPTER_SPEECH_FN] == (
        "synthesize",
        create_adapter_speech_operation,
    )
    assert set(HOST_PROVIDER_FACTORY) == set(_PROVIDER_OPERATIONS)


# ---------------------------------------------------------------------------
# Modality gateway: explicit model IDs + validated inline content
# ---------------------------------------------------------------------------


_STT_PROFILE = {
    "model_profile_id": "profile.review-stt",
    "display_name": "Review STT",
    "model_id": "review-a/whisper-1",
    "requirements": {},
    "parameters": {},
    "enabled": True,
    "metadata": {"provider_connection_id": "provider.review-a"},
}
_TTS_PROFILE = {
    "model_profile_id": "profile.review-tts",
    "display_name": "Review TTS",
    "model_id": "review-a/tts-1",
    "requirements": {},
    "parameters": {},
    "enabled": True,
    "metadata": {"provider_connection_id": "provider.review-a"},
}


def _modality_client(
    providers: list[dict[str, Any]],
    response: Mapping[str, Any],
    *,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
):
    """Fake Host client covering the Model Registry and provider contracts."""
    if profiles is None:
        profiles = {
            "profile.review-stt": _STT_PROFILE,
            "profile.review-tts": _TTS_PROFILE,
        }

    class FakeHostClient:
        def __init__(self) -> None:
            self.invoked: dict[str, Any] = {}
            self.registry_request: dict[str, Any] | None = None

        def providers(self, contract_id):
            # Captured metadata uses canonical operation IDs, not logical verbs.
            operation_id = (
                "rumi_provider_adapters_pack.provider-audio-transcribe"
                if contract_id == _PROVIDER_TRANSCRIBE
                else "rumi_provider_adapters_pack.provider-audio-speech"
            )
            return [{"operation_id": operation_id, **item} for item in providers]

        def invoke(self, contract_id, operation, request, **kwargs):
            if contract_id == _MODEL_PROFILE_CONTRACT:
                assert operation == _MODEL_PROFILE_OPERATION
                self.registry_request = dict(request)
                profile = profiles.get(str(request.get("identifier") or ""))
                if profile is None:
                    raise KeyError("model profile is unknown")
                return {
                    "requested_id": request["identifier"],
                    "resolved_profile_id": profile["model_profile_id"],
                    "aliased": (
                        request["identifier"] != profile["model_profile_id"]
                    ),
                    "profile": dict(profile),
                    "store_revision": 3,
                }
            self.invoked = {
                "contract_id": contract_id,
                "operation": operation,
                "request": dict(request),
                "kwargs": dict(kwargs),
            }
            return dict(response)

    return FakeHostClient()


def test_modality_transcribe_gateway_delegates_validated_inline_content(
) -> None:
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}], {"text": "ok"}
    )
    operation = create_audio_transcribe_operation(client)

    result = operation(
        "transcribe",
        {
            "model_profile_id": "profile.review-stt",
            "audio": _inline_audio(filename="clip.mp3"),
            "language": "en",
        },
    )

    assert result["status"] == "ok"
    assert result["text"] == "ok"
    assert client.registry_request == {"identifier": "profile.review-stt"}
    call = client.invoked
    assert call["contract_id"] == _PROVIDER_TRANSCRIBE
    assert call["operation"] == "rumi_provider_adapters_pack.provider-audio-transcribe"
    assert call["kwargs"]["provider_instance_id"] == "p.audio.transcribe"
    request = call["request"]
    # Provider binding and provider model ID come from the resolved record.
    assert request["model_id"] == "review-a/whisper-1"
    assert request["provider_connection_id"] == "provider.review-a"
    assert request["model_profile_id"] == "profile.review-stt"
    assert request["audio"] == {
        "content_id": _AUDIO_ID,
        "media_type": "audio/mpeg",
        "data_base64": _AUDIO_B64,
        "byte_size": len(_AUDIO_BYTES),
        "filename": "clip.mp3",
    }
    assert "provider_instance_id" not in request


def test_modality_speech_gateway_returns_validated_inline_content() -> None:
    audio = b"provider-synthesized-mp3"
    response_content = {
        "content_id": "sha256:" + hashlib.sha256(audio).hexdigest(),
        "media_type": "audio/mpeg",
        "data_base64": base64.b64encode(audio).decode("ascii"),
        "byte_size": len(audio),
    }
    client = _modality_client(
        [{"provider_instance_id": "p.audio.speech"}],
        {"content": response_content},
    )
    operation = create_audio_speech_operation(client)

    result = operation(
        "synthesize",
        {
            "model_profile_id": "profile.review-tts",
            "input": "say hello",
            "voice": "alloy",
        },
    )

    assert result["status"] == "ok"
    assert result["content"] == response_content
    call = client.invoked
    assert call["contract_id"] == _PROVIDER_SPEECH
    assert call["operation"] == "rumi_provider_adapters_pack.provider-audio-speech"
    assert call["request"]["model_id"] == "review-a/tts-1"
    assert call["request"]["provider_connection_id"] == "provider.review-a"
    assert call["request"]["input"] == "say hello"
    assert call["request"]["voice"] == "alloy"


@pytest.mark.parametrize(
    "denied_key",
    [
        "path",
        "uri",
        "url",
        "endpoint",
        "credential",
        # Routing/authority fields: the resolved model profile owns these.
        "model_id",
        "provider_id",
        "provider_instance_id",
        "provider_connection_id",
        "profile_id",
        "deadline",
        "model_reference",
        "identifier",
        "requirements",
        "allow_failover",
    ],
)
def test_modality_audio_gateways_reject_locator_inputs(denied_key) -> None:
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}], {"text": "x"}
    )
    operation = create_audio_transcribe_operation(client)
    payload = {
        "model_profile_id": "profile.review-stt",
        "audio": _inline_audio(),
        denied_key: "caller-supplied",
    }

    with pytest.raises(GlobalContractInvocationError) as error:
        operation("transcribe", payload)

    assert error.value.code == "denied"
    assert client.invoked == {}


@pytest.mark.parametrize(
    "audio, code",
    [
        ({}, "invalid_request"),
        (_inline_audio(content_id="sha256:" + "0" * 64), "invalid_request"),
        (_inline_audio(media_type="application/pdf"), "invalid_request"),
        (_inline_audio(byte_size=1), "invalid_request"),
        (_inline_audio(data_base64="!!!"), "invalid_request"),
    ],
    ids=[
        "missing-content",
        "self-hash-mismatch",
        "unsupported-media-type",
        "byte-size-mismatch",
        "invalid-base64",
    ],
)
def test_modality_transcribe_rejects_invalid_inline_content(audio, code) -> None:
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}], {"text": "x"}
    )
    operation = create_audio_transcribe_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {"model_profile_id": "profile.review-stt", "audio": audio},
        )

    assert error.value.code == code
    assert client.invoked == {}


def test_modality_audio_requires_exactly_one_registered_adapter() -> None:
    """Two registered audio adapters cannot be disambiguated by the caller."""
    providers = [
        {"provider_instance_id": "p.audio.one"},
        {"provider_instance_id": "p.audio.two"},
    ]
    client = _modality_client(providers, {"text": "x"})
    operation = create_audio_transcribe_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {
                "model_profile_id": "profile.review-stt",
                "audio": _inline_audio(),
            },
        )
    assert error.value.code == "missing_provider"
    # A caller-side provider pin is denied, never silently honored.
    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {
                "model_profile_id": "profile.review-stt",
                "audio": _inline_audio(),
                "provider_instance_id": "p.audio.two",
            },
        )
    assert error.value.code == "denied"
    assert client.invoked == {}


def test_modality_audio_requires_registered_model_profile() -> None:
    """Missing model_profile_id or an unknown/disabled profile fail closed."""
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}], {"text": "x"}
    )
    operation = create_audio_transcribe_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation("transcribe", {"audio": _inline_audio()})
    assert error.value.code == "invalid_request"
    assert client.invoked == {}

    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {"model_profile_id": "profile.missing", "audio": _inline_audio()},
        )
    assert error.value.code == "unresolved_profile"
    assert client.invoked == {}


@pytest.mark.parametrize(
    "profile",
    [
        {**_STT_PROFILE, "enabled": False},
        {**_STT_PROFILE, "metadata": {}},
        {**_STT_PROFILE, "model_id": ""},
        {**_STT_PROFILE, "model_id": "x" * 600},
    ],
    ids=[
        "disabled-profile",
        "missing-provider-binding",
        "missing-model-id",
        "overlong-model-id",
    ],
)
def test_modality_audio_rejects_unusable_profile(profile) -> None:
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}],
        {"text": "x"},
        profiles={"profile.review-stt": profile},
    )
    operation = create_audio_transcribe_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {"model_profile_id": "profile.review-stt", "audio": _inline_audio()},
        )

    assert error.value.code == "unresolved_profile"
    assert client.invoked == {}


def test_modality_audio_uses_resolved_profile_id_for_alias() -> None:
    """An alias resolves once; the provider request records the target ID."""
    alias_client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}],
        {"text": "x"},
        profiles={"profile.alias": _STT_PROFILE},
    )
    operation = create_audio_transcribe_operation(alias_client)

    result = operation(
        "transcribe",
        {"model_profile_id": "profile.alias", "audio": _inline_audio()},
    )

    assert result["status"] == "ok"
    assert alias_client.registry_request == {"identifier": "profile.alias"}
    request = alias_client.invoked["request"]
    assert request["model_profile_id"] == "profile.review-stt"


def test_modality_audio_result_survives_canonical_journal_boundaries() -> None:
    """The public result is journaled canonical JSON: no floats, bounded."""
    audio = b"provider-synthesized-mp3"
    response_content = {
        "content_id": "sha256:" + hashlib.sha256(audio).hexdigest(),
        "media_type": "audio/mpeg",
        "data_base64": base64.b64encode(audio).decode("ascii"),
        "byte_size": len(audio),
    }
    client = _modality_client(
        [{"provider_instance_id": "p.audio.speech"}],
        {"content": response_content},
    )
    operation = create_audio_speech_operation(client)
    payload = {
        "model_profile_id": "profile.review-tts",
        "input": "say hello",
        "voice": "alloy",
        "parameters": {"speed": "1.25"},
    }

    result = operation("synthesize", payload)

    # Broker snapshot + canonical digest boundaries accept the full exchange.
    _snapshot_mapping(dict(payload), "snapshot payload")
    canonical_json(result)
    canonical_digest(result)
    provider_request = client.invoked["request"]
    _snapshot_mapping(provider_request, "snapshot payload")
    canonical_json(provider_request)


def _sized_inline_audio(byte_count: int, **overrides: Any) -> dict[str, Any]:
    raw = bytes(byte_count)
    content = {
        "content_id": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "media_type": "audio/mpeg",
        "data_base64": base64.b64encode(raw).decode("ascii"),
        "byte_size": byte_count,
    }
    content.update(overrides)
    return content


_CANONICAL_LIMIT = 4 * 1024 * 1024
_AUDIO_DECODED_LIMIT = 1024 * 1024


def _outer_envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a journaled payload in the real durable outer canonical document.

    ``PreparedInvocationSnapshot`` is what actually persists: its constructor
    re-canonicalizes ``normalized_payload`` and ``to_dict()`` is the stored
    JSON, so encoding it checks the whole outer envelope rather than the
    inner payload alone.
    """
    return PreparedInvocationSnapshot(
        contract_id=_PROVIDER_TRANSCRIBE,
        contract_version="1.0.0",
        operation_id="rumi_provider_adapters_pack.provider-audio-transcribe",
        normalized_payload=dict(payload),
        request_digest="sha256:" + "1" * 64,
        timeout_ms=120_000,
        idempotency_key=None,
        binding_fingerprint={},
        context_fingerprint={},
        allow_lossy_adapters=False,
    ).to_dict()


def test_stt_input_at_decoded_cap_survives_real_canonical_bounds() -> None:
    """One MiB decoded is the largest inline audio that journals canonically."""
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}], {"text": "x" * 65536}
    )
    operation = create_audio_transcribe_operation(client)
    payload = {
        "model_profile_id": "profile.review-stt",
        "audio": _sized_inline_audio(_AUDIO_DECODED_LIMIT, filename="clip.mp3"),
        "language": "en",
        "parameters": {"prompt": "p" * 4096, "temperature": "0.9"},
    }

    result = operation("transcribe", payload)

    request = client.invoked["request"]
    # The full journaled exchange — caller request, provider request and
    # result — must each fit MAX_CANONICAL_JSON_BYTES (4 MiB), inside the
    # real outer canonical envelope, not just as inner dicts.
    for document in (payload, request, result):
        encoded = canonical_json(document)
        _snapshot_mapping(document, "snapshot payload")
        assert len(encoded) < _CANONICAL_LIMIT
        canonical_digest(document)
        outer = _outer_envelope(document)
        assert len(canonical_json(outer)) < _CANONICAL_LIMIT
        PreparedInvocationSnapshot.from_dict(
            json.loads(json.dumps(outer))
        )


def test_stt_input_above_decoded_cap_fails_closed() -> None:
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}], {"text": "x"}
    )
    operation = create_audio_transcribe_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {
                "model_profile_id": "profile.review-stt",
                "audio": _sized_inline_audio(_AUDIO_DECODED_LIMIT + 1),
            },
        )

    assert error.value.code == "invalid_request"
    assert client.invoked == {}


def test_tts_output_at_decoded_cap_survives_real_canonical_bounds() -> None:
    """Provider binary at the cap produces a journalable inline result."""
    raw = bytes(_AUDIO_DECODED_LIMIT)
    content = {
        "content_id": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "media_type": "audio/mpeg",
        "data_base64": base64.b64encode(raw).decode("ascii"),
        "byte_size": len(raw),
    }
    client = _modality_client(
        [{"provider_instance_id": "p.audio.speech"}], {"content": content}
    )
    operation = create_audio_speech_operation(client)
    payload = {
        "model_profile_id": "profile.review-tts",
        "input": "say hello",
        "voice": "alloy",
        "parameters": {"instructions": "i" * 4096},
    }

    result = operation("synthesize", payload)
    assert result["content"]["byte_size"] == _AUDIO_DECODED_LIMIT

    # run.get duplicates inputs + attempt request + outcomes in one document:
    # the duplicated doc (worst observed envelope shape) must stay under 4 MiB.
    duplicated = {
        "inputs": payload,
        "attempt": {"request": client.invoked["request"]},
        "outcomes": [result],
    }
    encoded = canonical_json(duplicated)
    assert len(encoded) < _CANONICAL_LIMIT
    _snapshot_mapping(duplicated, "snapshot payload")
    # The same budget holds inside the real outer canonical document.
    outer = _outer_envelope(result)
    assert len(canonical_json(outer)) < _CANONICAL_LIMIT
    PreparedInvocationSnapshot.from_dict(json.loads(json.dumps(outer)))


def test_tts_output_above_decoded_cap_is_invalid_response() -> None:
    raw = bytes(_AUDIO_DECODED_LIMIT + 1)
    content = {
        "content_id": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "media_type": "audio/mpeg",
        "data_base64": base64.b64encode(raw).decode("ascii"),
        "byte_size": len(raw),
    }
    client = _modality_client(
        [{"provider_instance_id": "p.audio.speech"}], {"content": content}
    )
    operation = create_audio_speech_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "synthesize",
            {
                "model_profile_id": "profile.review-tts",
                "input": "say hello",
                "voice": "alloy",
            },
        )

    assert error.value.code == "invalid_response"


def test_adapter_rejects_binary_result_above_decoded_cap() -> None:
    raw = bytes(_AUDIO_DECODED_LIMIT + 1)
    client = _adapter_connection_client(raw, binary=True)
    operation = create_adapter_speech_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "synthesize",
            {
                "provider_connection_id": "provider.review-a",
                "model_id": "review-a/tts-1",
                "input": "hi",
                "voice": "alloy",
            },
        )

    assert error.value.code == "invalid_response"


def test_modality_transcribe_result_projects_bounded_segments() -> None:
    """Verbose segments surface as bounded canonical ints/strings only."""
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}],
        {
            "text": "hi",
            "language": "en",
            "segments": [
                {
                    "text": "hi",
                    "start_ms": 0,
                    "end_ms": 1250,
                    "speaker": "speaker-1",
                    # Non-allowlisted provider fields are dropped, not relayed.
                    "confidence": 0.91,
                    "words": [{"w": 0.5}],
                },
                {"text": "bye", "start": 0.5},
            ],
        },
    )
    operation = create_audio_transcribe_operation(client)

    result = operation(
        "transcribe",
        {"model_profile_id": "profile.review-stt", "audio": _inline_audio()},
    )

    assert result["segments"] == [
        {
            "text": "hi",
            "start_ms": 0,
            "end_ms": 1250,
            "speaker": "speaker-1",
        },
        {"text": "bye"},
    ]
    canonical_json(result)


@pytest.mark.parametrize(
    "parameters",
    [
        {"temperature": 0.2},
        {"speed": 1.0},
        {"nested": {"x": 1}},
        {"blob": b"bytes"},
    ],
    ids=["raw-float", "raw-float-speed", "nested-mapping", "bytes-value"],
)
def test_modality_audio_rejects_non_canonical_parameters(parameters) -> None:
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}], {"text": "x"}
    )
    operation = create_audio_transcribe_operation(client)

    with pytest.raises(GlobalContractInvocationError) as error:
        operation(
            "transcribe",
            {
                "model_profile_id": "profile.review-stt",
                "audio": _inline_audio(),
                "parameters": parameters,
            },
        )

    assert error.value.code == "invalid_request"
    assert client.invoked == {}


# ---------------------------------------------------------------------------
# Captured Host Provider factories (contract tests)
# ---------------------------------------------------------------------------


def _modality_binding(
    function_id: str, contract_id: str, operation_id: str
) -> SimpleNamespace:
    return SimpleNamespace(
        function=SimpleNamespace(
            function_id=function_id,
            implementation_digest="sha256:implementation",
        ),
        operation=SimpleNamespace(
            contract_id=contract_id,
            contract_version="1.0.0",
            operation_id=operation_id,
        ),
        principal_ref=SimpleNamespace(value="fixture.principal"),
        artifact=SimpleNamespace(digest="sha256:artifact"),
    )


def test_modality_gateway_captured_factory_validates_exact_bindings() -> None:
    function_id = "rumi_ai_modality_pack.ai-modality.transcribe"
    factory = MODALITY_HOST_PROVIDER_FACTORY[function_id]
    assert isinstance(factory, ModalityGatewayHostFactoryV4)

    with pytest.raises(PermissionError):
        factory.capture(
            SimpleNamespace(provider_bindings=(), domain_ids={})
        )

    binding = _modality_binding(
        "rumi_ai_modality_pack.ai-modality.speech",
        _TRANSCRIBE_CONTRACT,
        "rumi_ai_modality_pack.ai-transcribe",
    )
    with pytest.raises(PermissionError):
        factory.capture(
            SimpleNamespace(provider_bindings=(binding,), domain_ids={})
        )

    binding = _modality_binding(
        function_id, _TRANSCRIBE_CONTRACT, "rumi_ai_modality_pack.ai-transcribe"
    )
    captured = factory.capture(
        SimpleNamespace(
            provider_bindings=(binding,),
            domain_ids={
                (
                    _TRANSCRIBE_CONTRACT,
                    "rumi_ai_modality_pack.ai-transcribe",
                    "fixture.principal",
                ): "fixture.domain",
            },
        )
    )
    assert len(captured.contributions) == 1
    contribution = captured.contributions[0]
    assert contribution.contract_id == _TRANSCRIBE_CONTRACT
    assert contribution.operation_id == "rumi_ai_modality_pack.ai-transcribe"


def test_modality_gateway_captured_invoke_is_authority_scoped() -> None:
    function_id = "rumi_ai_modality_pack.ai-modality.transcribe"
    factory = MODALITY_HOST_PROVIDER_FACTORY[function_id]
    binding = _modality_binding(
        function_id, _TRANSCRIBE_CONTRACT, "rumi_ai_modality_pack.ai-transcribe"
    )
    captured = factory.capture(
        SimpleNamespace(
            provider_bindings=(binding,),
            domain_ids={
                (
                    _TRANSCRIBE_CONTRACT,
                    "rumi_ai_modality_pack.ai-transcribe",
                    "fixture.principal",
                ): "fixture.domain",
            },
        )
    )
    contribution = captured.contributions[0]
    calls: list[Any] = []
    inner_client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}], {"text": "ok"}
    )

    class Invocation:
        def assert_current(self) -> None:
            calls.append("assert_current")

        def contract_client(self, **kwargs: Any) -> object:
            calls.append(("contract_client", dict(kwargs)))
            return inner_client

    result = contribution.invoke(
        "rumi_ai_modality_pack.ai-transcribe",
        {
            "model_profile_id": "profile.review-stt",
            "audio": _inline_audio(),
        },
        Invocation(),
    )

    assert result["status"] == "ok"
    assert calls[0] == "assert_current"
    assert calls[1] == (
        "contract_client",
        {
            "allowed_contract_ids": frozenset(
                {_PROVIDER_TRANSCRIBE, _MODEL_PROFILE_CONTRACT}
            ),
            "consumer_pack_id": "rumi_ai_modality_pack",
            "include_credentials": False,
        },
    )
    assert calls[-1] == "assert_current"

    with pytest.raises(PermissionError):
        contribution.invoke(
            "rumi_ai_modality_pack.ai-speech", {}, Invocation()
        )


def test_adapter_factory_captures_audio_operations() -> None:
    factory = HOST_PROVIDER_FACTORY[_ADAPTER_TRANSCRIBE_FN]
    assert isinstance(factory, ProviderAdapterHostFactoryV4)
    binding = _modality_binding(
        _ADAPTER_TRANSCRIBE_FN,
        _PROVIDER_TRANSCRIBE,
        "rumi_provider_adapters_pack.provider-audio-transcribe",
    )
    captured = factory.capture(
        SimpleNamespace(
            provider_bindings=(binding,),
            domain_ids={
                (
                    _PROVIDER_TRANSCRIBE,
                    "rumi_provider_adapters_pack.provider-audio-transcribe",
                    "fixture.principal",
                ): "fixture.domain",
            },
        )
    )
    assert captured.contributions[0].contract_id == _PROVIDER_TRANSCRIBE
    assert (
        captured.contributions[0].operation_id
        == "rumi_provider_adapters_pack.provider-audio-transcribe"
    )


def test_adapter_captured_invoke_calls_audio_operation() -> None:
    factory = HOST_PROVIDER_FACTORY[_ADAPTER_TRANSCRIBE_FN]
    binding = _modality_binding(
        _ADAPTER_TRANSCRIBE_FN,
        _PROVIDER_TRANSCRIBE,
        "rumi_provider_adapters_pack.provider-audio-transcribe",
    )
    captured = factory.capture(
        SimpleNamespace(
            provider_bindings=(binding,),
            domain_ids={
                (
                    _PROVIDER_TRANSCRIBE,
                    "rumi_provider_adapters_pack.provider-audio-transcribe",
                    "fixture.principal",
                ): "fixture.domain",
            },
        )
    )
    calls: list[Any] = []
    inner_client = _adapter_connection_client(
        {"text": "through-factory"}, binary=False
    )

    class Invocation:
        envelope = SimpleNamespace(
            cancellation_requested=None, deadline_monotonic=None
        )

        def assert_current(self) -> None:
            calls.append("assert_current")

        def contract_client(self, **kwargs: Any) -> object:
            calls.append(("contract_client", dict(kwargs)))
            return inner_client

    result = captured.contributions[0].invoke(
        "rumi_provider_adapters_pack.provider-audio-transcribe",
        {
            "provider_connection_id": "provider.review-a",
            "model_id": "review-a/whisper-1",
            "model_profile_id": "profile.review-stt",
            "audio": _inline_audio(),
        },
        Invocation(),
    )

    assert result["text"] == "through-factory"
    assert calls[0] == "assert_current"
    assert calls[1] == (
        "contract_client",
        {
            "allowed_contract_ids": frozenset(
                {"tobkiri.resource.ai.provider.registry.v1"}
            ),
            "consumer_pack_id": "rumi_provider_adapters_pack",
        },
    )
    assert calls[-1] == "assert_current"


def test_audio_uses_the_registered_requirements_provider_binding() -> None:
    profile = {**_STT_PROFILE, "metadata": {},
               "requirements": {"preferred_provider_instance_id": "provider.review-a"}}
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe"}], {"text": "x"},
        profiles={"profile.review-stt": profile},
    )
    result = create_audio_transcribe_operation(client)(
        "transcribe", {"model_profile_id": "profile.review-stt", "audio": _inline_audio()},
    )
    assert result["status"] == "ok"
    assert client.invoked["request"]["provider_connection_id"] == "provider.review-a"


def test_modality_rejects_provider_without_exact_operation_identity() -> None:
    client = _modality_client(
        [{"provider_instance_id": "p.audio.transcribe", "operation_id": None}],
        {"text": "not invoked"},
    )
    with pytest.raises(GlobalContractInvocationError, match="operation identity"):
        create_audio_transcribe_operation(client)("transcribe", {
            "model_profile_id": "profile.review-stt", "audio": _inline_audio(),
        })
    assert client.invoked == {}
