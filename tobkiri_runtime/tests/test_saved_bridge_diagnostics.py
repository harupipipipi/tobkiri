"""Captured saved callbacks retain safe outcomes and useful Host diagnostics."""

import logging
from pathlib import Path

import pytest

from core_runtime.authority.v4 import AuthorityStore, FunctionPrincipal
from core_runtime.bootstrap import bridge_diagnostics
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
    profile_capture_scope,
)
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.saved_bridge import READINESS
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import create_runtime_surface_services
from ecosystem.defaultspack.runtime import saved_conversation as saved
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from tests.test_live_production_v4_dispatch import (
    _CapturedBackend,
    _bundle_root,
    _credential_store_factory,
    _digest,
)
from tests.test_saved_bridge_callbacks import _frame
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.runtime import V4DispatchSession


@pytest.mark.parametrize(
    ("failure", "phase", "error_type"),
    [
        ("dispatch", "provider_dispatch", "PermissionError"),
        ("shape", "result_shape", "TypeError"),
        ("projection", "result_projection", "TypeError"),
        ("encoding", "result_encoding", "CanonicalizationError"),
        ("logger", "provider_dispatch", "PermissionError"),
    ],
)
def test_captured_saved_bridge_failure_is_correlated_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    phase: str,
    error_type: str,
) -> None:
    """Real capture/owner/callback path; provider and VM are explicit adapters."""
    user_data = tmp_path / "saved-diagnostics"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    binding = next(
        item
        for item in active.resolved.plan["bindings"]
        if item["contract_id"] == "conversation.saved-turn.v1"
    )
    principal = FunctionPrincipal.from_dict(binding["function_principal"])
    backend = _CapturedBackend(_digest("saved-diagnostics"))
    backend.target_executable_digest = principal.function_implementation_digest
    envelopes = []
    bridge_outcomes = []

    def guest(envelope):
        envelopes.append(envelope)
        callback, preflight = backend.saved_callbacks
        preflight(envelope)
        intent = saved.start(envelope.payload["request"])
        for _ in range(3):
            outcome = callback(envelope, _frame(intent, envelope.context.request_id))
            bridge_outcomes.append(outcome)
            intent = saved.resume(intent["state"], outcome)
        return ProviderOutcome(intent)

    monkeypatch.setattr(backend, "invoke", guest)
    original = V4DispatchSession.invoke

    def invoke(self, contract_id, operation_id, payload, **kwargs):
        if (contract_id, operation_id) == READINESS:
            return {
                "ready": payload.get("delivery_mode") != "incremental",
                "model_profile_id": "model-profile-1",
            }
        if (contract_id, operation_id) == saved.TARGETS[2]:
            if failure in {"dispatch", "logger"}:
                raise PermissionError("secret-path/prompt/provider-token")
            if failure == "shape":
                return ["secret-response"]
            # Strict ABI rejects floating point output after the AI projector.
            return {"status": "ok", "output": 1.5}
        return original(self, contract_id, operation_id, payload, **kwargs)

    monkeypatch.setattr(V4DispatchSession, "invoke", invoke)
    if failure == "projection":
        from core_runtime.bootstrap import saved_bridge

        def broken_projector(value):
            raise TypeError("secret-provider-response")

        monkeypatch.setattr(saved_bridge, "project_saved_ai_result", broken_projector)
    if failure == "logger":

        def broken_handler(*args, **kwargs):
            raise RuntimeError("secret-handler-failure")

        monkeypatch.setattr(bridge_diagnostics._LOGGER, "warning", broken_handler)
    session = capture_production_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=AuthorityStore(user_data / "authority/v4.sqlite3"),
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        backends=BackendRegistry((backend,)),
        target_backend_digests={principal.principal_id: backend.status.backend_digest},
        credential_store_factory=_credential_store_factory,
    )
    store = ConversationStore("defaults", user_data_root=user_data)
    store.create(
        {"id": "conversation-1", "model_reference": "model-profile-1"}, expected_revision=0
    )
    try:
        with caplog.at_level(logging.WARNING), profile_capture_scope():
            result = session.invoke(
                "conversation.saved-turn.v1",
                "saved_complete",
                {
                    "_session_id": "session.panel.saved-diagnostics",
                    "request": {
                        "turn_id": "turn-1",
                        "conversation_id": "conversation-1",
                        "conversation_revision": 1,
                        "content": "Hello",
                    },
                },
            )
        assert result["status"] == "error"
        assert result["error"]["code"] == "HOST_ACTION_FAILED"
        assert bridge_outcomes[-1] == {
            "status": "error",
            "error": {
                "code": "PROVIDER_UNAVAILABLE",
                "message": "The verified AI capability is unavailable.",
            },
        }
        assert [item["role"] for item in store.get("conversation-1")["messages"]] == ["user"]
        if failure != "logger":
            record = next(
                item for item in caplog.records if item.name == bridge_diagnostics.__name__
            )
            assert f"request={envelopes[0].context.request_id}" in record.getMessage()
            assert f"phase={phase}" in record.getMessage()
            assert f"error_types={error_type}" in record.getMessage()
            assert f"contract={saved.TARGETS[2][0]}" in record.getMessage()
            assert f"operation={saved.TARGETS[2][1]}" in record.getMessage()
            assert record.exc_info is None
        assert "secret" not in caplog.text
        assert "Hello" not in caplog.text
    finally:
        session.close()
