"""End-to-end voice Workflow proof over the real captured production path.

This file drives the strongest practical in-process chain available without
real credentials or network access:

    inline audio input
      -> ``run.advance`` on the captured Workflow Pack
      -> real interactive-approval reservation + signed v3 ui_operator decision
      -> real ``RequestBroker`` dispatch to the captured modality Host Provider
      -> real Model Registry contract resolution of ``model_profile_id``
      -> real provider-adapter op -> real one-shot Host credential transport
         (``_open_pinned_request`` replaced by a byte-verifying fake opener —
          the lease, endpoint binding, scope, deadline and redaction checks all
          still run; only the socket is faked)
      -> typed text Gateway op -> real ``run.observe`` bounded projection.

Nothing about the Workflow invoker, Provider callbacks, approvals, or
Broker dispatch is stubbed: every hop crosses the production captured
interfaces.  The fake opener asserts exact request URLs, the synthetic
Bearer credential, and returns canned wire responses by endpoint suffix.

This is an executable integration regression with synthetic owner-store setup
and a fake wire opener. It does not claim native acceptance, real model output,
or a UI registration ceremony. The Profile, approval, Broker and credential
scope boundaries remain production implementations.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from core_runtime import credential_transport
from core_runtime.host_contract import bind_host_contract
from tests.conformance_support.host_contract import host_contract_for_session
from core_runtime.authority.ui_operator import sign_ui_operator
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from ecosystem.rumi_credential_broker_pack.runtime.service import (
    CredentialBrokerService,
)
from ecosystem.rumi_model_registry_pack.runtime.service import (
    ModelRegistryService,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import (
    ProviderRegistry,
)
from tests.conformance_support.packaged_profile import (
    packaged_profile_bundle_root,
)
from tests.test_workflow_v4_pack_control_integration import (
    _capture_defaultspack_dispatch,
)
from tobkiri_host.credential_store import host_credential_store_factory
from tobkiri_host.ports import InteractiveApprovalDecisionCommand
from tobkiri_protocol.canonical import canonical_json

_SESSION_ID = "voice-e2e." + "a" * 32
_PROVIDER_INSTANCE = "connection/voice:main"
_ENDPOINT = "https://provider.example/v1"
_SECRET = "voice-e2e-test-secret"
_DECODED_CAP = 1024 * 1024
_OBSERVE_BUDGET = 3 * 1024 * 1024

_STT_CONTRACT = "tobkiri.service.ai.audio.transcribe.v1"
_TTS_CONTRACT = "tobkiri.service.ai.audio.speech.v1"
_TEXT_CONTRACT = "tobkiri.service.ai.text.generate.v1"
_WORKFLOW_CONTRACT = "tobkiri.workflow.v4"

_STT_BYTES = b"\xff\xfb" + b"voice-e2e-frames" * 64
_TTS_BYTES = b"\xff\xfb" + b"voice-e2e-spoken" * 64
_TRANSCRIPT = "hello small world"
_REPLY = "speak this sentence"


class _JsonResponse:
    """Deterministic opener response: a finite byte body, closed on exit."""

    def __init__(self, value: Any) -> None:
        self._body = (
            value if isinstance(value, bytes) else json.dumps(value).encode()
        )
        self.closed = False

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True
        return None

    def read(self, amount: int | None = None) -> bytes:
        return self._body[:amount]


def _inline_audio(raw: bytes) -> dict[str, Any]:
    return {
        "content_id": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "media_type": "audio/mpeg",
        "data_base64": base64.b64encode(raw).decode("ascii"),
        "byte_size": len(raw),
        "filename": "clip.mp3",
    }


def _seed_owner_state(user_data: Path, profile_id: str) -> None:
    """Seed owner-registered provider/model state into the real stores.

    Uses the same Store classes the Pack runtimes read per-invocation; no
    registry or credential seam is faked.  The credential is synthetic test
    material and only the opaque handle crosses the registry.
    """
    credential = CredentialBrokerService(
        user_data_root=user_data
    ).invoke(
        "create",
        {
            "secret_material": {"api_key": _SECRET},
            "profile_id": profile_id,
            "consumer_pack_id": "rumi_provider_adapters_pack",
            "provider_instance_id": _PROVIDER_INSTANCE,
            "scopes": [
                "ai.generate",
                "ai.audio.transcribe",
                "ai.audio.speech",
            ],
            "purpose": "provider.invoke",
        },
    )
    ProviderRegistry(profile_id, user_data_root=user_data).save(
        {
            "provider_instance_id": _PROVIDER_INSTANCE,
            "adapter_id": "openai",
            "display_name": "Voice e2e provider",
            "credential_handle": credential["handle"],
            "endpoint": _ENDPOINT,
            "enabled": True,
        },
        expected_revision=0,
    )
    models = ModelRegistryService(user_data_root=user_data)
    revision = 0
    for model_profile_id, model_id in (
        ("voice.stt", "whisper-1"),
        ("voice.text", "gpt-4o-mini"),
        ("voice.tts", "gpt-4o-mini-tts"),
    ):
        saved = models.invoke(
            "create",
            {
                "profile_id": profile_id,
                "expected_revision": revision,
                "record": {
                    "model_profile_id": model_profile_id,
                    "model_id": model_id,
                    "display_name": model_profile_id,
                    "metadata": {
                        "provider_connection_id": _PROVIDER_INSTANCE
                    },
                },
            },
        )
        revision = int(saved["store_revision"])


def _fake_opener(calls: list[dict[str, Any]]):
    """Return a deterministic opener for the real pinned request pipeline.

    The opener receives the fully constructed ``urllib.request.Request``
    after endpoint binding, credential injection, and header shaping, so the
    test still proves wire format, exact URL, and Authorization scoping; the
    response body is canned per endpoint.
    """

    def open_request(
        request: Any,
        *,
        timeout: float,
        deadline: float | None = None,
        cancellation: Any = None,
        clock: Any = None,
        authority_check: Any = None,
    ) -> _JsonResponse:
        del timeout, deadline, cancellation, clock
        assert authority_check is None or authority_check()
        url = request.full_url
        headers = {key.lower(): value for key, value in request.header_items()}
        calls.append(
            {
                "url": url,
                "method": request.get_method(),
                "headers": headers,
                "body": bytes(request.data or b""),
            }
        )
        assert headers.get("authorization") == f"Bearer {_SECRET}"
        if url == _ENDPOINT + "/audio/transcriptions":
            return _JsonResponse(
                {
                    "text": _TRANSCRIPT,
                    "language": "en",
                    "segments": [
                        {"start": 0.0, "end": 1.5, "text": _TRANSCRIPT}
                    ],
                }
            )
        if url == _ENDPOINT + "/chat/completions":
            return _JsonResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": _REPLY,
                            }
                        }
                    ],
                    "usage": {"total_tokens": 12},
                }
            )
        if url == _ENDPOINT + "/audio/speech":
            return _JsonResponse(_TTS_BYTES)
        raise AssertionError(f"unexpected egress target: {url}")

    return open_request


def _invoke(session: Any, operation_id: str, payload: Mapping[str, Any]) -> Any:
    """Invoke a Workflow op as the authenticated panel session."""

    return session.invoke(
        _WORKFLOW_CONTRACT,
        operation_id,
        {**dict(payload), "_session_id": _SESSION_ID},
    )


def _approve(session: Any, reservation_id: str) -> None:
    """Decide one pending workflow approval through the real adapter."""

    control = session.authority_control
    assert control is not None
    status = control.interactive_approval_status(reservation_id)
    assert status.state == "pending"
    context = session.context_for(
        _WORKFLOW_CONTRACT, "run.step.resume", _SESSION_ID
    )
    with bind_host_contract(host_contract_for_session(
        session, values={"panel_bootstrap_secret": "synthetic-voice-e2e-bootstrap"},
    )):
        ui_operator = sign_ui_operator(
            reservation_id,
            decision="approve",
            request_snapshot_digest=status.request_snapshot_digest,
            typed_confirmation_digest=status.typed_confirmation_digest,
        )
        control.approve_interactive_approval(
            InteractiveApprovalDecisionCommand(
                context=context,
                request_id=reservation_id,
                actor_id="voice-e2e-owner",
                confirmation_text=str(
                    status.redacted_metadata.get("confirmation_phrase") or ""
                ),
                ui_operator=ui_operator,
            )
        )



def _run_step(session: Any, run_id: str, step_id: str) -> dict[str, Any]:
    """Execute one gated step: reservation -> signed approve -> resume."""

    _invoke(session, "run.advance", {"run_id": run_id})
    view = _invoke(session, "run.get", {"run_id": run_id})
    attempt = next(item for item in view["attempts"] if item["step_id"] == step_id)
    if attempt["state"] == "waiting_approval":
        _approve(session, str(attempt["authority_reservation_id"]))
        attempt = _invoke(
            session, "run.step.resume", {"run_id": run_id, "step_id": step_id}
        )
    return dict(attempt)


def _session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Capture the real production dispatch session for the default Profile."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    active = capture_default_profile(
        confirmation=prepare_default_profile_confirmation()
    )
    profile_id = str(active.resolved.profile["profile_id"])
    _seed_owner_state(user_data, profile_id)
    authority = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    return _capture_defaultspack_dispatch(
        active,
        bundle_root=packaged_profile_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=authority,
        credential_store_factory=host_credential_store_factory,
    )


def test_audio_palette_schemas_are_finite() -> None:
    """The generated audio contracts must type the Flow ports + selector.

    ``run.step.execute`` input validation and the editor's typed ports both
    read the canonical catalog; ``{"type": "object"}`` silently admits any
    node shape. Finite schemas are required before any real dispatch.
    """

    catalog = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "pack_v4_catalog.v1.json"
        ).read_text(encoding="utf-8")
    )
    pack = next(
        item
        for item in catalog["packs"]
        if item["pack_id"] == "rumi_ai_modality_pack"
    )
    contracts = {
        item["contract_id"]: item for item in pack["provided_contracts"]
    }
    transcribe_in = contracts[_STT_CONTRACT]["schemas"]["input"]
    speech_in = contracts[_TTS_CONTRACT]["schemas"]["input"]
    for schema in (transcribe_in, speech_in):
        assert schema.get("type") == "object", schema
        properties = schema.get("properties") or {}
        selector = properties.get("model_profile_id") or {}
        assert selector.get("x-tobkiri-selector") == "model-profile"
        assert selector.get("type") == "string"
        assert "model_profile_id" in (schema.get("required") or ())
        assert schema.get("additionalProperties") is False
    audio_props = (transcribe_in.get("properties") or {}).get("audio") or {}
    assert audio_props.get("type") == "object"
    speech_props = speech_in.get("properties") or {}
    assert speech_props.get("input", {}).get("type") == "string"
    assert speech_props.get("voice", {}).get("type") == "string"


def test_voice_chain_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Published 3-step voice run rides capture, approvals, Broker, observe.

    Captures the exact Profile and executes every hop without real egress.
    """

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        credential_transport, "_open_pinned_request", _fake_opener(calls)
    )
    session = _session(tmp_path, monkeypatch)
    try:
        palette = _invoke(session, "operation.palette", {})
        by_contract = {
            item["contract_id"]: item for item in palette["operations"]
        }
        targets = (
            by_contract[_STT_CONTRACT],
            by_contract[_TEXT_CONTRACT],
            by_contract[_TTS_CONTRACT],
        )

        def step(
            step_id: str,
            target: Mapping[str, Any],
            step_input: Mapping[str, Any],
            depends_on: list[str],
        ) -> dict[str, Any]:
            request = {
                "contract_id": target["contract_id"],
                "contract_revision_digest": target[
                    "contract_revision_digest"
                ],
                "operation_id": target["operation_id"],
                "function_principal_id": target["function_principal_id"],
                "input": dict(step_input),
            }
            return {
                "id": step_id,
                "request": request,
                "depends_on": depends_on,
                "retry": {"max_attempts": 1, "backoff_ms": 0},
            }

        document = {
            "workflow_api_version": "io.tobkiri.workflow.v4",
            "name": "voice e2e",
            "max_concurrency": 1,
            "steps": [
                step(
                    "stt",
                    targets[0],
                    {
                        "model_profile_id": "voice.stt",
                        "audio": "${inputs.audio}",
                    },
                    [],
                ),
                step(
                    "llm",
                    targets[1],
                    {
                        "model_profile_id": "voice.text",
                        "text": "${steps.stt.output.text}",
                    },
                    ["stt"],
                ),
                step(
                    "tts",
                    targets[2],
                    {
                        "model_profile_id": "voice.tts",
                        "input": "${steps.llm.output.text}",
                        "voice": "${inputs.voice}",
                    },
                    ["llm"],
                ),
            ],
        }
        validated = _invoke(
            session, "definition.validate", {"document": document}
        )
        assert validated.get("errors", []) == [], validated
        created = _invoke(
            session,
            "definition.create",
            {"definition_id": "workflow.voice-e2e", "document": document},
        )
        _invoke(
            session,
            "definition.publish",
            {"definition_id": "workflow.voice-e2e", "if_match": created["etag"]},
        )
        _invoke(
            session,
            "run.create",
            {
                "definition_id": "workflow.voice-e2e",
                "run_id": "voice-run-1",
                "inputs": {
                    "audio": _inline_audio(_STT_BYTES),
                    "voice": "alloy",
                },
            },
        )

        stt = _run_step(session, "voice-run-1", "stt")
        assert stt["state"] == "succeeded", stt
        assert stt["outcome"]["text"] == _TRANSCRIPT
        llm = _run_step(session, "voice-run-1", "llm")
        assert llm["state"] == "succeeded", llm
        assert llm["outcome"]["text"] == _REPLY
        tts = _run_step(session, "voice-run-1", "tts")
        assert tts["state"] == "succeeded", tts
        content = tts["outcome"]["content"]
        assert content["content_id"] == (
            "sha256:" + hashlib.sha256(_TTS_BYTES).hexdigest()
        )
        assert content["media_type"] == "audio/mpeg"
        assert base64.b64decode(content["data_base64"]) == _TTS_BYTES

        observed = _invoke(session, "run.observe", {"run_id": "voice-run-1"})
        assert observed["run"]["state"] == "succeeded"
        assert "inputs" not in observed["run"]
        assert observed["step_count"] == 3
        assert all(
            "request" not in attempt for attempt in observed["attempts"]
        )
        assert len(canonical_json(observed)) < _OBSERVE_BUDGET
        assert _SECRET not in json.dumps(observed)
        assert "credential_handle" not in json.dumps(observed)

        urls = [call["url"] for call in calls]
        assert urls == [
            _ENDPOINT + "/audio/transcriptions",
            _ENDPOINT + "/chat/completions",
            _ENDPOINT + "/audio/speech",
        ]
        multipart = calls[0]["body"]
        assert b"multipart" in calls[0]["headers"]["content-type"].encode()
        assert _STT_BYTES in multipart
        assert b'filename="clip.mp3"' in multipart
        assert b"whisper-1" in multipart
        speech_request = json.loads(calls[2]["body"].decode())
        assert speech_request["model"] == "gpt-4o-mini-tts"
        assert speech_request["voice"] == "alloy"
        assert speech_request["input"] == _REPLY
    finally:
        session.close()
