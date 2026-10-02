"""Production HTTP proof for the saved-turn readiness surface and persistence.

Real HTTP -> Broker -> captured owners. Guest execution, AI completion and
route readiness stay explicit adapters, exactly as in
``test_production_frontend_contract_http``.  These cases close the
code-level half of the real-conversation acceptance:

- model unset, Provider missing and credentials missing reach the UI as
  distinct typed states rather than one opaque failure;
- a completed saved turn, its conversation and its selected model survive
  an ordinary runtime recapture (restart) without any re-execution;
- owner load failures surface as errors instead of an empty success.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.pack_api_server import PackAPIServer
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.runtime import V4DispatchSession
from tobkiri_host.errors import BackendUnavailableError

from tests.test_production_frontend_contract_http import (
    _SavedPackVmBackend,
    _PresentationPackVmBackend,
    _authenticate,
    _captured_production_server,
    _contract,
    _request,
)


pytestmark = pytest.mark.contract


def _saved_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[PackAPIServer, object, AuthorityStore]]:
    return _captured_production_server(
        tmp_path,
        monkeypatch,
        packvm_backends=BackendRegistry((_SavedPackVmBackend(),)),
    )


def _post(
    server: PackAPIServer,
    headers: dict[str, str],
    path: str,
    body: dict[str, object],
) -> tuple[int, dict[str, object], list[tuple[str, str]]]:
    headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
    return _request(
        server, "POST", _contract("POST", path), body=body, headers=headers
    )


def _get(
    server: PackAPIServer, headers: dict[str, str], path: str
) -> tuple[int, dict[str, object], list[tuple[str, str]]]:
    headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
    return _request(server, "GET", _contract("GET", path), headers=headers)


def _turn_body(turn_id: str = "turn-1") -> dict[str, object]:
    return {
        "request": {
            "turn_id": turn_id,
            "conversation_id": "conversation-1",
            "conversation_revision": 1,
            "content": "Hello",
        }
    }


@pytest.mark.parametrize("readiness", ["not_ready", "provider_missing"])
def test_saved_send_readiness_denied_surfaces_distinct_waiting_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    readiness: str,
) -> None:
    """A denied route readiness is an explicit non-terminal state, not a send.

    The Host cannot prove zero effects happened once a dispatch failed, so
    the turn stays ``waiting`` for reconcile instead of being fabricated as
    failed.  What the UI receives is still strictly distinct from both a
    completed turn and the immediate INVALID_REQUEST rejection an unset
    model produces: no user/assistant write, no AI call, a durable waiting
    record and a readable unconfirmed phase.
    """
    from core_runtime.bootstrap.saved_bridge import READINESS
    from ecosystem.defaultspack.runtime.saved_conversation import TARGETS
    from ecosystem.rumi_conversation_store_pack.runtime.store import (
        ConversationStore,
    )

    original = V4DispatchSession.invoke
    ai_calls = []

    def invoke(self, contract_id, operation_id, payload, **kwargs):
        if (contract_id, operation_id) == READINESS:
            if readiness == "provider_missing":
                raise BackendUnavailableError(
                    "test provider adapter reports no executable provider"
                )
            return {"ready": False, "model_profile_id": "model-profile-1"}
        if (contract_id, operation_id) == TARGETS[2]:
            ai_calls.append(payload)
            return {"status": "ok", "output": "Hi"}
        return original(self, contract_id, operation_id, payload, **kwargs)

    monkeypatch.setattr(V4DispatchSession, "invoke", invoke)
    servers = _saved_server(tmp_path, monkeypatch)
    server, _session, _authority = next(servers)
    try:
        store = ConversationStore("defaults", user_data_root=tmp_path / "user-data")
        store.create(
            {"id": "conversation-1", "model_reference": "model-profile-1"},
            expected_revision=0,
        )
        before = store.path.read_bytes()
        cookie, csrf, origin = _authenticate(server)
        headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}
        status, payload, _ = _post(
            server, headers, "/api/chat/turn", _turn_body()
        )
        assert status == 200, payload
        assert payload["data"]["status"] == "reconciliation_required"
        waiting = payload["data"]["turn"]
        assert waiting["status"] == "waiting"
        assert waiting.get("result_reference") is None
        # Readiness denial happens before any write: no user message, no AI
        # dispatch, and no fabricated outcome may appear anywhere.
        assert store.get("conversation-1")["messages"] == []
        assert store.path.read_bytes() == before
        assert ai_calls == []
        assert "no executable provider" not in json.dumps(payload)
        ledger = next((tmp_path / "user-data").rglob("turns.sqlite3"))
        ledger_bytes = ledger.read_bytes()

        status, observed, _ = _get(server, headers, "/api/chat/turn?turn_id=turn-1")
        assert status == 200, observed
        assert observed["data"] == waiting

        status, events, _ = _get(
            server,
            headers,
            "/api/chat/turn/events?turn_id=turn-1&conversation_id=conversation-1",
        )
        assert status == 200, events
        assert events["data"]["status"] == "waiting"
        assert events["data"]["terminal"] is None
        assert events["data"]["events"][-1]["details"] == {
            "phase": "reconciliation_required",
            "reason": "saved_execution_outcome_unconfirmed",
        }

        # Reconcile and re-send neither invent a result nor repeat effects.
        status, reconciled, _ = _post(
            server, headers, "/api/chat/turn/reconcile", {"turn_id": "turn-1"}
        )
        assert status == 200, reconciled
        assert reconciled["data"]["status"] == "existing"
        assert reconciled["data"]["turn"]["status"] == "waiting"
        assert reconciled["data"]["turn"].get("result_reference") is None

        status, repeated, _ = _post(server, headers, "/api/chat/turn", _turn_body())
        assert status == 200, repeated
        assert repeated["data"]["status"] == "existing"
        assert repeated["data"]["turn"]["status"] == "waiting"

        assert store.get("conversation-1")["messages"] == []
        assert store.path.read_bytes() == before
        assert ai_calls == []
        assert ledger.read_bytes() == ledger_bytes
    finally:
        servers.close()


def test_saved_send_model_unset_is_typed_rejection_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model unset is a deterministic pre-write rejection, distinct from
    Provider/credential gaps that surface as a waiting turn."""
    from core_runtime.bootstrap.saved_bridge import READINESS
    from ecosystem.defaultspack.runtime.saved_conversation import TARGETS
    from ecosystem.rumi_conversation_store_pack.runtime.store import (
        ConversationStore,
    )

    original = V4DispatchSession.invoke
    ai_calls = []
    readiness_calls = []

    def invoke(self, contract_id, operation_id, payload, **kwargs):
        if (contract_id, operation_id) == READINESS:
            readiness_calls.append(payload)
            return {"ready": True, "model_profile_id": "model-profile-1"}
        if (contract_id, operation_id) == TARGETS[2]:
            ai_calls.append(payload)
            return {"status": "ok", "output": "Hi"}
        return original(self, contract_id, operation_id, payload, **kwargs)

    monkeypatch.setattr(V4DispatchSession, "invoke", invoke)
    servers = _saved_server(tmp_path, monkeypatch)
    server, _session, _authority = next(servers)
    try:
        store = ConversationStore("defaults", user_data_root=tmp_path / "user-data")
        store.create(
            {"id": "conversation-1", "model_reference": None},
            expected_revision=0,
        )
        before = store.path.read_bytes()
        cookie, csrf, origin = _authenticate(server)
        headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}
        status, payload, _ = _post(
            server, headers, "/api/chat/turn", _turn_body()
        )
        assert status == 400, payload
        assert payload["data"]["state"] == "error"
        assert payload["data"]["code"] == "INVALID_REQUEST"
        assert payload["data"]["retryable"] is False
        assert store.get("conversation-1")["messages"] == []
        assert store.path.read_bytes() == before
        assert ai_calls == []
        assert readiness_calls == []
        # No durable turn was claimed: nothing to reconcile or replay.
        assert not list((tmp_path / "user-data").rglob("turns.sqlite3"))
        status, missing, _ = _get(server, headers, "/api/chat/turn?turn_id=turn-1")
        assert status != 200, missing
    finally:
        servers.close()


def test_saved_turn_and_conversation_survive_runtime_recapture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart persistence: completed turn, transcript and model selection
    reopen through a fresh captured session without any re-execution."""
    from core_runtime.bootstrap.saved_bridge import READINESS
    from ecosystem.defaultspack.runtime.saved_conversation import TARGETS
    from ecosystem.rumi_conversation_store_pack.runtime.store import (
        ConversationStore,
    )
    from ecosystem.rumi_turn_runtime_pack.runtime.durable import (
        DurableTurnRuntime,
    )

    original = V4DispatchSession.invoke
    ai_calls = []

    def invoke(self, contract_id, operation_id, payload, **kwargs):
        if (contract_id, operation_id) == READINESS:
            return {"ready": True, "model_profile_id": "model-profile-1"}
        if (contract_id, operation_id) == TARGETS[2]:
            ai_calls.append(payload)
            return {"status": "ok", "output": "Hi"}
        return original(self, contract_id, operation_id, payload, **kwargs)

    monkeypatch.setattr(V4DispatchSession, "invoke", invoke)
    servers = _saved_server(tmp_path, monkeypatch)
    server, _session, _authority = next(servers)
    try:
        store = ConversationStore("defaults", user_data_root=tmp_path / "user-data")
        store.create(
            {"id": "conversation-1", "model_reference": "model-profile-1"},
            expected_revision=0,
        )
        cookie, csrf, origin = _authenticate(server)
        headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}
        status, payload, _ = _post(
            server, headers, "/api/chat/turn", _turn_body()
        )
        assert status == 200, payload
        assert payload["data"]["status"] == "completed"
        completed_turn = payload["data"]["turn"]
        reference = completed_turn["result_reference"]
        assert reference["conversation_revision"] == 3

        # Ordinary restart: the Host recaptures a new dispatch session over
        # the same user-data root, replacing every in-memory handle.
        previous_capture = server._dispatch_session
        status, restarted, _ = _post(
            server, headers, "/api/pack-control/restart", {}
        )
        assert status == 200, restarted
        assert server._dispatch_session is not previous_capture
        cookie, csrf, origin = _authenticate(server)
        headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}

        # Reopened conversation: both messages, their stable IDs, turn
        # metadata and the selected model are still bound to the record.
        status, snapshot, _ = _get(
            server, headers, "/api/chat/conversation?conversation_id=conversation-1"
        )
        assert status == 200, snapshot
        conversation = snapshot["data"]
        assert conversation["conversation_revision"] == 3
        assert conversation["model"] == "model-profile-1"
        assert [message["id"] for message in conversation["messages"]] == [
            reference["user_message_id"],
            reference["assistant_message_id"],
        ]
        assert [message["role"] for message in conversation["messages"]] == [
            "user",
            "assistant",
        ]
        assert [message["content"] for message in conversation["messages"]] == [
            "Hello",
            "Hi",
        ]
        assert all(
            message["metadata"]["turn_id"] == "turn-1"
            for message in conversation["messages"]
        )

        # The durable turn record survives as a terminal receipt.
        status, observed, _ = _get(server, headers, "/api/chat/turn?turn_id=turn-1")
        assert status == 200, observed
        assert observed["data"]["status"] == "completed"
        assert observed["data"]["result_reference"] == reference

        # A repeat of the same send is a read, never a second execution.
        status, repeated, _ = _post(server, headers, "/api/chat/turn", _turn_body())
        assert status == 200, repeated
        assert repeated["data"] == {"status": "existing", "turn": completed_turn}
        assert len(ai_calls) == 1

        # Owner reopen over the same data root agrees with the served state:
        # user and assistant each persisted exactly once.
        reopened = ConversationStore(
            "defaults", user_data_root=tmp_path / "user-data"
        ).get("conversation-1")
        assert [message["role"] for message in reopened["messages"]] == [
            "user",
            "assistant",
        ]
        assert reopened["model_reference"] == "model-profile-1"
        ledger = DurableTurnRuntime("defaults", user_data_root=tmp_path / "user-data")
        assert ledger.get("turn-1")["status"] == "completed"
        assert len(ai_calls) == 1
    finally:
        servers.close()


def test_connection_status_distinguishes_missing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The UI can tell Provider absent, credentials missing and configured
    apart through the typed connection snapshot."""
    from ecosystem.rumi_provider_registry_pack.runtime.registry import (
        ProviderRegistry,
    )

    servers = _captured_production_server(
        tmp_path,
        monkeypatch,
        packvm_backends=BackendRegistry((_PresentationPackVmBackend(),)),
    )
    server, _session, _authority = next(servers)
    try:
        registry = ProviderRegistry("defaults", user_data_root=tmp_path / "user-data")
        cookie, _csrf, _origin = _authenticate(server)
        headers = {"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())}

        # No registered Provider at all: an explicit empty list, not an error.
        status, payload, _ = _get(server, headers, "/api/connections/status")
        assert status == 200, payload
        assert payload["data"] == {"revision": 0, "providers": []}

        registry.save(
            {
                "provider_instance_id": "connection/openrouter:main",
                "adapter_id": "openrouter",
                "display_name": "OpenRouter main",
                "endpoint": "https://openrouter.ai/api/v1",
                "enabled": True,
            },
            expected_revision=0,
        )
        registry.save(
            {
                "provider_instance_id": "connection/openai:main",
                "adapter_id": "openai",
                "display_name": "OpenAI main",
                "endpoint": "https://api.openai.com",
                "credential_handle": "opaque:connection-secret",
                "enabled": True,
            },
            expected_revision=1,
        )
        registry.save(
            {
                "provider_instance_id": "connection/ollama:local",
                "adapter_id": "ollama",
                "display_name": "Ollama local",
                "endpoint": "http://127.0.0.1:11434",
                "enabled": False,
            },
            expected_revision=2,
        )

        status, payload, _ = _get(server, headers, "/api/connections/status")
        assert status == 200, payload
        providers = {
            item["provider_instance_id"]: item
            for item in payload["data"]["providers"]
        }
        assert providers["connection/openrouter:main"]["credential_status"] == "missing"
        assert providers["connection/openrouter:main"]["enabled"] is True
        assert providers["connection/openrouter:main"]["reachability"] == "unknown"
        assert providers["connection/openai:main"]["credential_status"] == "configured"
        assert providers["connection/ollama:local"]["enabled"] is False
        assert providers["connection/ollama:local"]["credential_status"] == "missing"
        assert "connection-secret" not in json.dumps(payload)
    finally:
        servers.close()


def test_owner_load_failures_surface_instead_of_empty_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt owner store must surface as an HTTP error, never as an
    empty conversation list or an empty workspace list."""
    from ecosystem.rumi_conversation_store_pack.runtime.store import (
        ConversationStore,
    )
    from ecosystem.rumi_workspace_mount_pack.runtime.mounts import (
        WorkspaceMountStore,
    )

    servers = _captured_production_server(tmp_path, monkeypatch)
    server, _session, _authority = next(servers)
    try:
        store = ConversationStore("defaults", user_data_root=tmp_path / "user-data")
        store.create({"id": "conversation-1"}, expected_revision=0)
        mounts = WorkspaceMountStore("defaults", user_data_root=tmp_path / "user-data")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        mounts.mount("workspace-1", str(workspace), expected_revision=0)
        cookie, _csrf, _origin = _authenticate(server)
        headers = {"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())}
        for route in (
            "/api/chat/conversations",
            "/api/coding/workspaces",
        ):
            status, payload, _ = _get(server, headers, route)
            assert status == 200, payload

        # Corrupt both owner files after they were proven readable.
        store.path.write_bytes(b"{ not json")
        mounts.path.write_bytes(b"{ not json")

        status, payload, _ = _get(server, headers, "/api/chat/conversations")
        assert status != 200, payload
        assert payload["success"] is False
        assert payload["data"]["state"] == "error"

        status, payload, _ = _get(server, headers, "/api/coding/workspaces")
        assert status != 200, payload
        assert payload["success"] is False
        assert payload["data"]["state"] == "error"
    finally:
        servers.close()
