"""Cross-feature policy and credential lifetime regression coverage."""

from types import SimpleNamespace

import pytest

from core_runtime import credential_transport as transport
from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_provider_adapters_pack.runtime import adapter
from ecosystem.rumi_provider_registry_pack.runtime.model_access import VERSION
from ecosystem.rumi_provider_registry_pack.runtime.provider_filters import CAPABILITY_REVISION


@pytest.mark.parametrize("kind", ["transcribe", "speech"])
@pytest.mark.parametrize("case", ["empty", "other", "native", "override"])
def test_audio_checks_connection_policy_before_credentials(monkeypatch, kind, case):
    policy = {"version": VERSION, "mode": "explicit", "model_ids": ["whisper-1"]}
    parameters = {}
    expected = "denied"
    if case == "empty":
        policy["model_ids"] = []
    elif case == "other":
        policy["model_ids"] = ["other"]
    elif case == "native":
        policy["native_filters"] = {
            "revision": CAPABILITY_REVISION, "discovery": {},
            "routing": {"only": ["example"]},
        }
        expected = "incompatible"
    else:
        parameters = {"model": "other"}
        expected = "invalid_request"
    connection = {
        "provider_instance_id": "provider.review-a", "adapter_id": "openai-compatible",
        "endpoint": "https://openrouter.ai/api/v1", "enabled": True,
        "metadata": {"catalog_provider_id": "openrouter"}, "model_access": policy,
    }
    monkeypatch.setattr(adapter, "_connection", lambda *_args: connection)

    def forbidden(*_args, **_kwargs):
        pytest.fail("denied audio must not select credentials or reach transport")

    monkeypatch.setattr(adapter, "_credential_handle", forbidden)
    monkeypatch.setattr(adapter, "_openai_audio_transcribe", forbidden)
    monkeypatch.setattr(adapter, "_openai_audio_speech", forbidden)
    with pytest.raises(GlobalContractInvocationError) as failure:
        adapter._audio_operation(object(), kind=kind)("invoke", {
            "model_id": "review-a/whisper-1", "parameters": parameters,
        })
    assert failure.value.code == expected


@pytest.mark.parametrize("kind", ["transcribe", "speech"])
def test_audio_policy_uses_same_canonical_model_as_wire(monkeypatch, kind):
    connection = {
        "provider_instance_id": "provider.review-a", "adapter_id": "openai-compatible",
        "model_access": {"version": VERSION, "mode": "explicit", "model_ids": ["whisper-1"]},
    }
    monkeypatch.setattr(adapter, "_connection", lambda *_args: connection)
    monkeypatch.setattr(adapter, "_credential_handle", lambda *_args, **_kwargs: "opaque:ok")

    def completed(_client, request, _connection, _handle, _scope, model_id):
        assert model_id == "whisper-1"
        assert request["parameters"] == {"response_format": "json"}
        return {"checked": True}

    monkeypatch.setattr(adapter, "_openai_audio_transcribe", completed)
    monkeypatch.setattr(adapter, "_openai_audio_speech", completed)
    assert adapter._audio_operation(object(), kind=kind)("invoke", {
        "model_id": "review-a/whisper-1", "parameters": {"response_format": "json"},
    }) == {"checked": True}


@pytest.mark.parametrize("method", ["post_multipart", "post_binary"])
def test_audio_envelope_transport_preserves_parent_guard(monkeypatch, method):
    calls = []

    def revoked():
        calls.append("guard")
        raise PermissionError("parent no longer owns request")

    def capture(_envelope, **kwargs):
        assert kwargs["parent_guard"] is revoked
        kwargs["parent_guard"]()
        pytest.fail("revoked parent cannot construct an audio transport")

    monkeypatch.setattr(transport.HostBoundCredentialTransport, "from_authorized_envelope", capture)
    outer = transport.AuthorizedEnvelopeCredentialTransport(
        envelope=SimpleNamespace(), provider_principal=None, store=None,
        authority_store=None, current_security_epoch=lambda: 1,
        credential_key_version="1", consumer_pack_id="fixture", parent_guard=revoked,
    )
    args = {
        "endpoint": "https://provider.example/v1/audio", "headers": {},
        "credential_handle": "credential:fixture", "provider_instance_id": "provider.fixture",
        "credential_scope": "ai.audio.speech", "credential_scheme": "bearer",
        "deadline": 9_999_999_999.0,
    }
    if method == "post_binary":
        args["body"] = {"input": "hello"}
    else:
        args.update(fields={}, file_field="file", filename="clip.mp3",
                    content_type="audio/mpeg", content=b"audio")
    with pytest.raises(PermissionError, match="parent no longer owns"):
        getattr(outer, method)(**args)
    assert calls == ["guard"]
