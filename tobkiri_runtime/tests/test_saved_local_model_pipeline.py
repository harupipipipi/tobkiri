"""Hermetic saved-turn proof through the real local-model production pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typing_extensions import Self

from core_runtime import local_model_transport
from core_runtime.authority.v4 import AuthorityStore, FunctionPrincipal
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
    profile_capture_scope,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from tests.test_live_production_v4_dispatch import (
    _bundle_root,
    _deny_production_test_internet,  # noqa: F401
)
from tests.test_production_frontend_contract_http import _SavedPackVmBackend
from tobkiri_host.backends import BackendRegistry
from tobkiri_protocol.secure_persistence import SecureDirectory


def test_saved_local_model_turns_complete_and_replay_without_duplicate_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep coordinator, preflight, gateway, registries and transport genuine.

    The existing test guest executes the saved ABI and production-bound Host
    callbacks. Only its VM process transport and local HTTP connection are
    adapters; neither readiness nor AI dispatch is replaced with a success.
    """
    user_data = tmp_path / "saved-local-production"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    active = capture_default_profile(
        confirmation=prepare_default_profile_confirmation()
    )
    provider_id = "provider.saved-local"
    model_profile_id = "profile.saved-local"
    model_id = "lfm-local"
    endpoint = "http://127.0.0.1:18080/v1"
    ProviderRegistry("defaults", user_data_root=user_data).save(
        {
            "provider_instance_id": provider_id,
            "adapter_id": "local-openai-compatible",
            "endpoint": endpoint,
            "credential_handle": None,
            "enabled": True,
        },
        expected_revision=0,
    )
    SecureDirectory(user_data / "host_local_models").write_bytes_atomic(
        "allowlist.json",
        json.dumps(
            {
                "version": "tobkiri.host.local-models.v1",
                "registrations": [
                    {
                        "profile_id": "defaults",
                        "provider_instance_id": provider_id,
                        "endpoint": endpoint,
                        "model_ids": [model_id],
                    }
                ],
            }
        ).encode("utf-8"),
    )
    ModelRegistry("defaults", user_data_root=user_data).save(
        {
            "model_profile_id": model_profile_id,
            "model_id": model_id,
            "metadata": {"provider_connection_id": provider_id},
        },
        expected_revision=0,
    )
    conversations = ConversationStore("defaults", user_data_root=user_data)
    conversations.create(
        {"id": "conversation-local", "model_reference": model_profile_id},
        expected_revision=0,
    )
    turns = DurableTurnRuntime("defaults", user_data_root=user_data)
    observed: list[dict[str, Any]] = []
    closed: list[bool] = []

    class FakeSocket:
        def shutdown(self, _how: int) -> None:
            pass

    class FakeResponse:
        status = 200

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def read(self, amount: int) -> bytes:
            assert amount == 4 * 1024 * 1024 + 1
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": "saved-local-ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {},
                }
            ).encode("utf-8")

    class FakeConnection:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            assert (host, port) == ("127.0.0.1", 18080)
            assert 0 < timeout <= 60
            self.sock: FakeSocket | None = None

        def connect(self) -> None:
            self.sock = FakeSocket()

        def request(
            self,
            method: str,
            path: str,
            payload: bytes,
            headers: dict[str, str],
        ) -> None:
            assert (method, path) == ("POST", "/v1/chat/completions")
            assert set(headers) == {"Content-Type", "Accept", "Connection"}
            observed.append(json.loads(payload))

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            closed.append(True)
            self.sock = None

    monkeypatch.setattr(
        local_model_transport.http.client, "HTTPConnection", FakeConnection
    )
    backend = _SavedPackVmBackend()
    binding = next(
        item
        for item in active.resolved.plan["bindings"]
        if (item["contract_id"], item["operation_id"])
        == ("conversation.saved-turn.v1", "saved_complete")
    )
    principal = FunctionPrincipal.from_dict(binding["function_principal"])
    session = capture_production_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=AuthorityStore(user_data / "authority" / "v4.sqlite3"),
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        backends=BackendRegistry((backend,)),
        target_backend_digests={
            principal.principal_id: backend.status.backend_digest
        },
    )
    try:
        for index, content in enumerate(
            ("Hello locally", [{"type": "text", "text": "Warm local turn"}])
        ):
            initial = {
                "_session_id": "session.panel.saved-local",
                "request": {
                    "turn_id": f"turn-local-{index}",
                    "conversation_id": "conversation-local",
                    "conversation_revision": 1 + 2 * index,
                    "content": content,
                    "tool_selection": {"mode": "none"},
                },
            }
            # Every frontend submission gets its own capture scope, including
            # duplicate submissions. Nested requests inherit the same guards.
            with profile_capture_scope():
                result = session.invoke(
                    "tobkiri.action.turn.saved.v1",
                    "rumi_turn_runtime_pack.turn-saved",
                    initial,
                )
            assert result["status"] == "completed", {
                "result": result,
                "local_http_requests": len(observed),
                "conversation": conversations.get("conversation-local"),
            }
            assert result["turn"]["status"] == "completed"
            assert turns.get(f"turn-local-{index}") == result["turn"]
            receipt = conversations.saved_receipt(f"turn-local-{index}")
            assert receipt is not None
            assert receipt["result_reference"] == result["turn"]["result_reference"]
            assert receipt["result_reference"]["conversation_revision"] == 3 + 2 * index
            with profile_capture_scope():
                repeated = session.invoke(
                    "tobkiri.action.turn.saved.v1",
                    "rumi_turn_runtime_pack.turn-saved",
                    initial,
                )
            assert repeated == {"status": "existing", "turn": result["turn"]}
            assert len(observed) == len(closed) == index + 1
    finally:
        session.close()

    assert observed == [
        {
            "model": model_id,
            "stream": False,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": "Hello locally"}],
        },
        {
            "model": model_id,
            "stream": False,
            "max_tokens": 512,
            "messages": [
                {"role": "user", "content": "Hello locally"},
                {"role": "assistant", "content": "saved-local-ok"},
                {"role": "user", "content": "Warm local turn"},
            ],
        },
    ]
    conversation = conversations.get("conversation-local")
    assert conversation is not None
    assert conversation["conversation_revision"] == 5
    assert [(item["role"], item["content"]) for item in conversation["messages"]] == [
        ("user", "Hello locally"),
        ("assistant", "saved-local-ok"),
        ("user", [{"type": "text", "text": "Warm local turn"}]),
        ("assistant", "saved-local-ok"),
    ]
