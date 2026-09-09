"""Real-server proof for the production frontend-to-Broker contract path."""

from __future__ import annotations

import http.client
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator, Mapping
from urllib.parse import quote

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding as FrontendContractBinding,
    HTTPContractTarget as FrontendContractTarget,
)
from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
    load_frontend_contract_bindings,
)
from ecosystem.defaultspack.defaultspack.http_contract_composition import (
    defaultspack_capability_binding,
    defaultspack_capability_snapshot,
    defaultspack_capability_snapshot_mapping,
)
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
    defaultspack_runtime_capture_inputs,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from ecosystem.defaultspack.domain.runtime_v4 import ActivationStore, BundledCatalog
from ecosystem.rumi_shell_policy_pack.runtime import policy as shell_policy
from tests.conformance_support.command_protocol_activation import (
    COMMAND_PROTOCOL_HTTP_CASES,
    file_snapshot,
)
from tobkiri_host.backends import (
    REQUIRED_PRODUCTION_GATES,
    BackendRegistry,
    BackendStatus,
)
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.models import ExecutionKind, OpaqueAuthorityRef, RuntimeEvidence
from tobkiri_protocol.canonical import canonical_digest
from tests.conformance_support.host_contract import host_contract


pytestmark = pytest.mark.contract


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_MUTATION_TIMEOUT_SECONDS = 10
EVENTUAL_RECONCILIATION_TIMEOUT_SECONDS = 30
BUNDLE_ROOT = RUNTIME_ROOT / "ecosystem" / "defaultspack" / "v4"
MAP_PATH = (
    RUNTIME_ROOT / "ecosystem" / "defaultspack" / "defaultspack" / "frontend_contract_map.v4.json"
)


class _ShellPolicyPackVmBackend:
    """Test supervisor for the one production-admitted shell policy PackVM ABI.

    The test owns the supervisor transport only.  It still asks production
    capture to bind the sealed artifact and target-domain resolvers, then
    calls the staged shell-policy entrypoint with the exact catalog operation.
    That makes a nested terminal prepare prove the actual PackVM policy edge
    without adding a product fallback for non-macOS test runs.
    """

    _PACK_ID = "rumi_shell_policy_pack"
    _FUNCTION_ID = "rumi_shell_policy_pack.shell-policy.inspect"
    _CONTRACT_ID = "tobkiri.service.shell.inspect.v1"
    _OPERATION_ID = "rumi_shell_policy_pack.shell-inspect"
    _execute_abi = staticmethod(shell_policy.tobkiri_packvm_invoke)

    def __init__(self) -> None:
        self.status = BackendStatus(
            backend_id="tobkiri.python-pack-v4",
            execution_kind=ExecutionKind.PACK_VM,
            platform="any",
            backend_digest=canonical_digest({"backend": "test-shell-policy-packvm", "abi": 1}),
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )
        self._artifact_resolver = None
        self._target_domain_resolver = None
        self._target_domain_id: str | None = None
        self._saved_callback = None
        self._saved_preflight = None

    def bind_saved_capability_bridge(self, callback, preflight) -> None:
        """Accept capture wiring without widening this adapter's supported ABI."""
        assert self._saved_callback is None and self._saved_preflight is None
        assert callable(callback) and callable(preflight)
        self._saved_callback = callback
        self._saved_preflight = preflight

    def supports(self, binding: object) -> bool:
        """Admit only the exact shell-policy Function pinned by the Plan."""

        artifact = getattr(binding, "artifact", None)
        function = getattr(binding, "function", None)
        operation = getattr(binding, "operation", None)
        return bool(
            getattr(artifact, "pack_id", None) == self._PACK_ID
            and getattr(function, "function_id", None) == self._FUNCTION_ID
            and getattr(operation, "contract_id", None) == self._CONTRACT_ID
            and getattr(operation, "operation_id", None) == self._OPERATION_ID
        )

    def bind_artifact_resolver(self, resolver: object) -> None:
        """Accept the production-captured resolver exactly once."""

        assert self._artifact_resolver is None
        assert callable(resolver)
        self._artifact_resolver = resolver

    def bind_target_domain_resolver(self, resolver: object) -> None:
        """Accept the production authority domain resolver exactly once."""

        assert self._target_domain_resolver is None
        assert callable(resolver)
        self._target_domain_resolver = resolver

    def materialize(self, binding: object, reservation_id: str) -> RuntimeEvidence:
        """Materialize the verified policy artifact in its exact Host domain."""

        if not reservation_id or not self.supports(binding):
            raise BackendUnavailableError("test PackVM binding is unavailable")
        if self._artifact_resolver is None or self._target_domain_resolver is None:
            raise BackendUnavailableError("test PackVM capture is unavailable")
        artifact = self._artifact_resolver(binding)
        implementation_digest = getattr(
            getattr(binding, "function", None), "implementation_digest", None
        )
        if (
            getattr(artifact, "artifact_digest", None)
            != getattr(getattr(binding, "artifact", None), "digest", None)
            or getattr(artifact, "implementation_digest", None) != implementation_digest
        ):
            raise BackendUnavailableError("test PackVM artifact changed")
        domain_id = self._target_domain_resolver(binding)
        if not isinstance(domain_id, str) or not domain_id:
            raise BackendUnavailableError("test PackVM domain is unavailable")
        self._target_domain_id = domain_id
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(domain_id),
            executable_digest=str(implementation_digest),
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )

    def invoke(self, request: object) -> ProviderOutcome:
        """Execute just the real sealed shell-policy ABI over this test transport."""

        if (
            not isinstance(request, RequestEnvelope)
            or request.target_domain.value != self._target_domain_id
            or request.contract_id != self._CONTRACT_ID
            or request.operation_id != self._OPERATION_ID
        ):
            raise BackendUnavailableError("test PackVM envelope is invalid")
        return ProviderOutcome(
            self._execute_abi(
                request.operation_id,
                dict(request.payload),
            )
        )

    def cancel(self, request_id: str) -> None:
        """Accept the Broker's authenticated cancellation identity."""

        if not request_id:
            raise BackendUnavailableError("test PackVM cancellation is invalid")

    def terminate(self, domain_id: str) -> None:
        """Fence only the domain that production capture issued to this backend."""

        if domain_id != self._target_domain_id:
            raise BackendUnavailableError("test PackVM domain is invalid")


class _PresentationPackVmBackend(_ShellPolicyPackVmBackend):
    """Test transport for the exact application-owned presentation ABI."""

    from ecosystem.defaultspack.runtime.application_presentation import (
        tobkiri_packvm_invoke as _presentation_abi,
    )

    _PACK_ID = "defaultspack"
    _FUNCTION_ID = "defaultspack.application-presentation"
    _CONTRACT_ID = "tobkiri.resource.application.presentation.v1"
    _OPERATION_ID = "defaultspack.presentation.read"
    _execute_abi = staticmethod(_presentation_abi)


class _SavedPackVmBackend(_ShellPolicyPackVmBackend):
    """Explicit guest adapter; Host callbacks and conversation owner stay real."""

    _PACK_ID = "defaultspack"
    _FUNCTION_ID = "defaultspack.conversation.saved"
    _CONTRACT_ID = "conversation.saved-turn.v1"
    _OPERATION_ID = "saved_complete"

    def invoke(self, request: object) -> ProviderOutcome:
        from ecosystem.defaultspack.runtime import saved_conversation
        from tests.test_saved_bridge_callbacks import _frame

        assert isinstance(request, RequestEnvelope)
        assert request.target_domain.value == self._target_domain_id
        assert request.contract_id == self._CONTRACT_ID
        assert request.operation_id == self._OPERATION_ID
        self._saved_preflight(request)
        intent = saved_conversation.start(request.payload["request"])
        for _ in range(4):
            outcome = self._saved_callback(request, _frame(intent, request.context.request_id))
            intent = saved_conversation.resume(intent["state"], outcome)
        return ProviderOutcome(intent)


def _contract(method: str, target: str) -> str:
    return "/api/contracts/defaultspack/" + quote(f"{method.upper()} {target}", safe="")


def _request(
    server: PackAPIServer,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_seconds: float = EVENTUAL_RECONCILIATION_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, object], list[tuple[str, str]]]:
    # Bound real-server integration calls without imposing a product deadline
    # on synchronous integrity validation and runtime recapture.
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.port,
        timeout=timeout_seconds,
    )
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = dict(headers or {})
    if encoded is not None:
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    response_headers = response.getheaders()
    connection.close()
    return response.status, payload, response_headers


def _authenticate(server: PackAPIServer) -> tuple[str, str, str]:
    origin = f"http://127.0.0.1:{server.port}"
    status, bootstrap, _headers = _request(
        server,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": "desktop-bootstrap"},
    )
    assert status == 200, bootstrap
    status, exchange, headers = _request(
        server,
        "POST",
        "/api/panel/auth/exchange",
        body={"code": bootstrap["data"]["code"]},
        headers={"Origin": origin},
    )
    assert status == 200
    cookie = next(value for key, value in headers if key.lower() == "set-cookie")
    return cookie.split(";", 1)[0], str(exchange["data"]["csrf_token"]), origin


def _captured_production_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    packvm_backends: BackendRegistry | None = None,
    credential_store_factory=None,
) -> Iterator[tuple[PackAPIServer, object, AuthorityStore]]:
    """Start one production capture, optionally with a test PackVM supervisor."""

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    contract_path = user_data / "host_contract.json"
    contract_path.write_text(
        json.dumps(
            host_contract(
                profile_id=str(active.resolved.profile["profile_id"]),
                profile_revision=str(active.resolved.plan["profile_revision"]),
                activation_id=str(active.activation["activation_id"]),
                plan_digest=str(active.resolved.plan["plan_digest"]),
                values={"panel_bootstrap_secret": "desktop-bootstrap"},
            )
        ),
        encoding="utf-8",
    )
    contract_path.chmod(0o600)
    monkeypatch.setenv("TOBKIRI_HOST_CONTRACT_PATH", str(contract_path))
    authority = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    bundle_root = packaged_profile_bundle_root()

    def runtime_capture_inputs(current: object | None = None):
        """Bind refreshes to this test's explicit packaged Profile bundle."""

        return replace(
            defaultspack_runtime_capture_inputs(current),
            bundle_root=bundle_root,
            ecosystem_root=RUNTIME_ROOT / "ecosystem",
        )

    catalog = BundledCatalog.load(bundle_root)
    bindings = load_frontend_contract_bindings(
        MAP_PATH,
        catalog.packs["runtime.tauri.application.default"],
    )
    session = capture_production_dispatch(
        active,
        bundle_root=bundle_root,
        ecosystem_root=RUNTIME_ROOT / "ecosystem",
        authority_store=authority,
        backends=packvm_backends,
        credential_store_factory=credential_store_factory,
        http_contract_bindings=bindings,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        capability_binding_snapshot_factory=defaultspack_capability_snapshot_mapping,
        capability_binding_selector=defaultspack_capability_binding,
    )
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        dispatch_session=session,
        contract_bindings=bindings,
        runtime_capture_factory=runtime_capture_inputs,
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(),
    )

    def publish_current_host_contract() -> None:
        """Model the Launcher writer at an explicit runtime refresh boundary."""

        current = profile_capture.capture_active_profile()
        contract_path.write_text(
            json.dumps(
                host_contract(
                    profile_id=str(current.resolved.profile["profile_id"]),
                    profile_revision=str(current.resolved.plan["profile_revision"]),
                    activation_id=str(current.activation["activation_id"]),
                    plan_digest=str(current.resolved.plan["plan_digest"]),
                    values={"panel_bootstrap_secret": "desktop-bootstrap"},
                )
            ),
            encoding="utf-8",
        )
        contract_path.chmod(0o600)

    original_refresh = server._refresh_runtime_capture

    def refresh(session: object | None = None) -> None:
        publish_current_host_contract()
        original_refresh(
            session,  # type: ignore[arg-type]
            lifecycle_generation=server._lifecycle_generation,
        )

    monkeypatch.setattr(server, "_refresh_runtime_capture", refresh)
    server.start()
    try:
        yield server, session, authority
    finally:
        server.stop()
        session.broker.close()
        authority.close()


@pytest.fixture
def production_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Capture the unmodified production HTTP server fixture."""

    yield from _captured_production_server(tmp_path, monkeypatch)


@pytest.fixture
def command_vertical_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Capture production HTTP with only the sealed policy PackVM test port."""

    yield from _captured_production_server(
        tmp_path,
        monkeypatch,
        packvm_backends=BackendRegistry((_ShellPolicyPackVmBackend(),)),
    )


@pytest.fixture
def settings_vertical_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep production Broker checks while supplying the sealed presentation ABI."""
    yield from _captured_production_server(
        tmp_path, monkeypatch,
        packvm_backends=BackendRegistry((_PresentationPackVmBackend(),)),
    )


@pytest.mark.parametrize("completion", ["normal", "reply_lost", "stop_after_commit"])
def test_saved_send_http_preserves_authority_and_durable_idempotency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, completion: str,
) -> None:
    """Real HTTP/Broker/owners; guest execution, AI and readiness are adapters."""
    from core_runtime.bootstrap.saved_bridge import READINESS
    from ecosystem.defaultspack.runtime.saved_conversation import TARGETS
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
    from tobkiri_host.runtime import V4DispatchSession

    original = V4DispatchSession.invoke
    ai_calls = []
    stop_receipts = []
    lose_owner_reply = completion != "normal"

    def invoke(self, contract_id, operation_id, payload, **kwargs):
        if (contract_id, operation_id) == READINESS:
            return {"ready": True, "model_profile_id": "model-profile-1"}
        if (contract_id, operation_id) == TARGETS[2]:
            ai_calls.append(payload)
            return {"status": "ok", "output": "Hi"}
        result = original(self, contract_id, operation_id, payload, **kwargs)
        if (
            lose_owner_reply and (contract_id, operation_id) == TARGETS[3]
            and payload.get("operation") == "append_saved"
            and payload["message"]["role"] == "assistant"
        ):
            if completion == "stop_after_commit":
                # The owner has committed, but its reply has not reached the
                # coordinator. Stop must not erase that durable outcome.
                stop_status, stopped, _ = _request(
                    server, "POST", _contract("POST", "/api/chat/turn/stop"),
                    body={"turn_id": "turn-1"},
                    headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
                )
                stop_receipts.append((stop_status, stopped))
            raise RuntimeError("owner reply lost after commit")
        return result

    monkeypatch.setattr(V4DispatchSession, "invoke", invoke)
    servers = _captured_production_server(
        tmp_path, monkeypatch, packvm_backends=BackendRegistry((_SavedPackVmBackend(),)),
    )
    server, _session, _authority = next(servers)
    try:
        store = ConversationStore("defaults", user_data_root=tmp_path / "user-data")
        store.create({"id": "conversation-1", "model_reference": "model-profile-1"},
                     expected_revision=0)
        before = store.path.read_bytes()
        route = _contract("POST", "/api/chat/turn")
        body = {"request": {"turn_id": "turn-1", "conversation_id": "conversation-1",
                            "conversation_revision": 1, "content": "Hello"}}
        status, payload, _ = _request(server, "POST", route, body=body)
        assert status in {401, 403}, payload
        cookie, csrf, origin = _authenticate(server)
        headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}
        for invalid in (
            {**body, "approved": True}, {**body, "profile_id": "other"},
            {**body, "state": {}}, {"request": {**body["request"], "outcome": {}}},
            {"request": {**body["request"], "content": "あ" * 22000}},
            {"request": {**body["request"], "conversation_revision": True}},
        ):
            headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
            status, payload, _ = _request(server, "POST", route, body=invalid, headers=headers)
            assert status == 400, payload
        assert store.path.read_bytes() == before
        assert not ai_calls
        assert not list((tmp_path / "user-data").rglob("turns.sqlite3"))
        reconcile_route = _contract("POST", "/api/chat/turn/reconcile")
        for invalid in (
            {"turn_id": "turn-1", "approved": True},
            {"turn_id": "turn-1", "profile_id": "other"},
            {"turn_id": "turn-1", "result_reference": {}}, body,
        ):
            headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
            rejected_status, rejected, _ = _request(
                server, "POST", reconcile_route, body=invalid, headers=headers,
            )
            assert rejected_status == 400, rejected
        headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
        missing_status, _, _ = _request(
            server, "POST", reconcile_route, body={"turn_id": "turn-1"}, headers=headers,
        )
        assert missing_status != 200
        assert not list((tmp_path / "user-data").rglob("turns.sqlite3"))
        assert store.path.read_bytes() == before and not ai_calls
        headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
        status, payload, _ = _request(server, "POST", route, body=body, headers=headers)
        if completion == "stop_after_commit":
            assert len(stop_receipts) == 1
            stop_status, stopped = stop_receipts[0]
            assert stop_status == 200, stopped
            assert stopped["data"] == {
                "status": "cancellation_requested", "turn_id": "turn-1", "stopped": False,
            }
        else:
            assert status == 200, payload
            assert payload["data"]["status"] == (
                "reconciliation_required" if lose_owner_reply else "completed"
            ), payload
        headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
        status, repeated, _ = _request(
            server, "POST", reconcile_route if lose_owner_reply else route,
            body={"turn_id": "turn-1"} if lose_owner_reply else body, headers=headers,
        )
        assert status == 200, repeated
        if lose_owner_reply:
            assert repeated["data"]["status"] == "completed", repeated
        else:
            assert repeated["data"] == {"status": "existing", "turn": payload["data"]["turn"]}
        completed_turn = repeated["data"]["turn"]
        reference = completed_turn["result_reference"]
        assert reference["conversation_revision"] == 3
        assert len(ai_calls) == 1
        assert [message["content"] for message in store.get("conversation-1")["messages"]] == ["Hello", "Hi"]
        headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
        status, snapshot, _ = _request(
            server, "GET",
            _contract("GET", "/api/chat/conversation") + "?conversation_id=conversation-1",
            headers=headers,
        )
        assert status == 200, snapshot
        assert snapshot["data"]["conversation_revision"] == 3
        assert [message["id"] for message in snapshot["data"]["messages"]] == [
            reference["user_message_id"], reference["assistant_message_id"],
        ]
        assert all(
            message["conversation_id"] == "conversation-1"
            and message["metadata"]["turn_id"] == "turn-1"
            for message in snapshot["data"]["messages"]
        )
        ledger = next((tmp_path / "user-data").rglob("turns.sqlite3"))
        ledger_before = ledger.read_bytes()
        for query in ("turn_id=turn-1&profile_id=other", "turn_id=turn-1&operation=begin_saved"):
            headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
            status, rejected, _ = _request(
                server, "GET", _contract("GET", "/api/chat/turn") + "?" + query,
                headers=headers,
            )
            assert status == 400, rejected
        headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
        status, observed, _ = _request(
            server, "GET", _contract("GET", "/api/chat/turn") + "?turn_id=turn-1",
            headers=headers,
        )
        assert status == 200, observed
        assert observed["data"] == completed_turn
        assert ledger.read_bytes() == ledger_before
        conversation_before = store.path.read_bytes()
        headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
        status, late_stop, _ = _request(
            server, "POST", _contract("POST", "/api/chat/turn/stop"),
            body={"turn_id": "turn-1"}, headers=headers,
        )
        assert status != 200, late_stop
        assert store.path.read_bytes() == conversation_before
        assert ledger.read_bytes() == ledger_before
        assert len(ai_calls) == 1
    finally:
        servers.close()


def test_saved_http_rejects_owned_context_before_writes_but_allows_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise owned context rejection through real HTTP, Broker and stores."""
    from core_runtime.bootstrap.saved_bridge import READINESS
    from ecosystem.defaultspack.runtime.saved_conversation import TARGETS
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
    from tobkiri_host.runtime import V4DispatchSession

    original = V4DispatchSession.invoke
    reads, ai_calls = [], []

    def invoke(self, contract_id, operation_id, payload, **kwargs):
        if (contract_id, operation_id) == READINESS:
            return {"ready": True, "model_profile_id": "model-profile-1"}
        if (contract_id, operation_id) == TARGETS[2]:
            ai_calls.append(payload)
            return {"status": "ok", "output": "Hi"}
        result = original(self, contract_id, operation_id, payload, **kwargs)
        if (contract_id, operation_id) == TARGETS[0] and payload.get("operation") == "get":
            reads.append(payload["conversation_id"])
        return result

    monkeypatch.setattr(V4DispatchSession, "invoke", invoke)
    servers = _captured_production_server(
        tmp_path, monkeypatch, packvm_backends=BackendRegistry((_SavedPackVmBackend(),)),
    )
    server, _session, _authority = next(servers)
    try:
        store = ConversationStore("defaults", user_data_root=tmp_path / "user-data")
        cookie, csrf, origin = _authenticate(server)
        contexts = (
            {"metadata": {"workspaceId": "workspace-1"}},
            {"metadata": {"shared_read_only": True}},
            {"conversation_kind": "operations_company"},
            {"model_reference": None},
            {"model_reference": "   "},
            {"title": "stale revision"},
            {"metadata": {"icon": "chat"}},
        )
        for index, context in enumerate(contexts):
            conversation_id = f"context-{index}"
            store.create({
                "id": conversation_id, "model_reference": "model-profile-1", **context,
            }, expected_revision=index)
            before = store.path.read_bytes()
            status, payload, _ = _request(
                server, "POST", _contract("POST", "/api/chat/turn"),
                body={"request": {
                    "turn_id": f"turn-{index}", "conversation_id": conversation_id,
                    "conversation_revision": 2 if index == 5 else 1, "content": "Hello",
                }},
                headers={"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf,
                         "X-Tobkiri-Request-ID": str(uuid.uuid4())},
            )
            assert conversation_id in reads  # Not a missing route/auth rejection.
            if index < len(contexts) - 1:
                assert status != 200, payload
                assert store.path.read_bytes() == before
                assert not ai_calls
                assert not list((tmp_path / "user-data").rglob("turns.sqlite3"))
            else:
                assert status == 200, payload
                assert len(ai_calls) == 1
                assert [item["content"] for item in store.get(conversation_id)["messages"]] == [
                    "Hello", "Hi",
                ]
    finally:
        servers.close()


def test_saved_stop_http_signals_only_the_original_owner(tmp_path, monkeypatch) -> None:
    """Real HTTP/Host/Broker cancellation; the AI and guest remain explicit adapters."""
    from core_runtime.bootstrap.saved_bridge import READINESS
    from ecosystem.defaultspack.runtime.saved_conversation import TARGETS
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
    from tobkiri_host.runtime import V4DispatchSession

    entered, observed = threading.Event(), threading.Event()
    signals = []
    original = V4DispatchSession.invoke

    def invoke(self, contract_id, operation_id, payload, **kwargs):
        if (contract_id, operation_id) == READINESS:
            return {"ready": True, "model_profile_id": "model-profile-1"}
        if (contract_id, operation_id) == TARGETS[2]:
            signal = kwargs["parent_cancellation"]
            signals.append(signal)
            entered.set()
            if signal.wait(8):
                observed.set()
            raise RuntimeError("test AI stopped without a result")
        return original(self, contract_id, operation_id, payload, **kwargs)

    monkeypatch.setattr(V4DispatchSession, "invoke", invoke)
    servers = _captured_production_server(
        tmp_path, monkeypatch, packvm_backends=BackendRegistry((_SavedPackVmBackend(),)),
    )
    server, _session, _authority = next(servers)
    try:
        store = ConversationStore("defaults", user_data_root=tmp_path / "user-data")
        store.create({"id": "conversation-1", "model_reference": "model-profile-1"}, expected_revision=0)
        cookie, csrf, origin = _authenticate(server)
        foreign_cookie, foreign_csrf, _ = _authenticate(server)

        def post(path, body, *, foreign=False):
            return _request(server, "POST", _contract("POST", path), body=body, headers={
                "Cookie": foreign_cookie if foreign else cookie, "Origin": origin,
                "X-Rumi-CSRF": foreign_csrf if foreign else csrf,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            })

        before = store.path.read_bytes()
        status, absent, _ = post("/api/chat/turn/stop", {"turn_id": "turn-stop-1"})
        assert status != 200, absent
        assert store.path.read_bytes() == before
        assert not list((tmp_path / "user-data").rglob("turns.sqlite3"))
        body = {"request": {
            "turn_id": "turn-stop-1", "conversation_id": "conversation-1",
            "conversation_revision": 1, "content": "Hello",
        }}
        with ThreadPoolExecutor(max_workers=1) as pool:
            sent = pool.submit(post, "/api/chat/turn", body)
            try:
                assert entered.wait(8), "saved execution did not reach the AI adapter"
                status, denied, _ = post("/api/chat/turn/stop", {"turn_id": "turn-stop-1"}, foreign=True)
                assert status != 200, denied
                assert not signals[0].is_set()
                status, invalid, _ = post("/api/chat/turn/stop", {"turn_id": "turn-stop-1", "approved": True})
                assert status == 400, invalid
                status, receipt, _ = post("/api/chat/turn/stop", {"turn_id": "turn-stop-1"})
                assert status == 200, receipt
                assert receipt["data"] == {
                    "status": "cancellation_requested", "turn_id": "turn-stop-1", "stopped": False,
                }
                assert observed.wait(2)
                sent.result(timeout=5)
                assert len(signals) == 1
            finally:
                for signal in signals:
                    signal.set()
        # No assistant result was produced. Reconciliation and duplicate send
        # cannot invent one, replay the Provider, or append the user twice.
        after_stop = store.path.read_bytes()
        for path, payload in (
            ("/api/chat/turn/reconcile", {"turn_id": "turn-stop-1"}),
            ("/api/chat/turn", body),
        ):
            status, pending, _ = post(path, payload)
            assert status == 200, pending
            assert pending["data"]["turn"]["status"] in {"running", "waiting"}
            assert pending["data"]["turn"].get("result_reference") is None
        assert store.path.read_bytes() == after_stop
        assert [message["content"] for message in store.get("conversation-1")["messages"]] == ["Hello"]
        assert len(signals) == 1
        previous_capture = server._dispatch_session
        status, restarted, _ = post("/api/pack-control/restart", {})
        assert status == 200, restarted
        assert server._dispatch_session is not previous_capture
        cookie, csrf, origin = _authenticate(server)
        status, lost_handle, _ = post("/api/chat/turn/stop", {"turn_id": "turn-stop-1"})
        assert status != 200, lost_handle
        # A fresh capture is not permission to claim the durable turn again.
        for path, payload in (
            ("/api/chat/turn/reconcile", {"turn_id": "turn-stop-1"}),
            ("/api/chat/turn", body),
        ):
            status, recovered, _ = post(path, payload)
            assert status == 200, recovered
            assert recovered["data"]["turn"]["status"] in {"running", "waiting"}
            assert recovered["data"]["turn"].get("result_reference") is None
        assert store.path.read_bytes() == after_stop
        assert len(signals) == 1
    finally:
        servers.close()


def test_preferences_write_uses_captured_owner_and_preserves_private_state(
    settings_vertical_server, tmp_path: Path,
) -> None:
    """Authenticated display patches cross the real Broker, not a local writer."""
    server, _session, _authority = settings_vertical_server
    storage = tmp_path / "user-data/defaultspack/shared/frontend_settings.json"
    storage.parent.mkdir(parents=True, exist_ok=True)
    original = {
        "general": {"composer_placeholder": "Before", "private": "owner-only"},
        "models": {"google_api_key": "private-test-value"},
        "_settings_revision": 7,
    }
    storage.write_text(json.dumps(original), encoding="utf-8")
    before = storage.read_bytes()
    cookie, csrf, origin = _authenticate(server)
    headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}
    route = _contract("PUT", "/api/ui/settings")
    body = {"changes": {"general": {"composer_placeholder": "After"}}, "expected_revision": 7}
    status, payload, _ = _request(server, "PUT", route, body=body)
    assert status in {401, 403}, payload
    for extra in ({"profile_id": "other"}, {"approved": True}, {"path": str(storage)}):
        headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
        status, payload, _ = _request(server, "PUT", route, body={**body, **extra}, headers=headers)
        assert status == 400, payload
    assert storage.read_bytes() == before
    headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
    status, payload, _ = _request(server, "PUT", route, body=body, headers=headers)
    assert status == 200, payload
    assert payload["data"] == {"values": body["changes"], "document_revision": 8}
    persisted = json.loads(storage.read_text(encoding="utf-8"))
    assert persisted["general"] == {"composer_placeholder": "After", "private": "owner-only"}
    assert persisted["models"] == original["models"]
    after = storage.read_bytes()
    for invalid in (
        body,
        {"changes": {"models": {"google_api_key": "replacement"}}, "expected_revision": 8},
    ):
        headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
        status, payload, _ = _request(server, "PUT", route, body=invalid, headers=headers)
        assert status != 200, payload
        assert storage.read_bytes() == after


def test_settings_reads_saved_values_and_models_through_real_broker(
    settings_vertical_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings use captured state and a nested model contract, without writes."""
    from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry

    root = tmp_path / "user-data"
    registry = ModelRegistry("defaults", user_data_root=root)
    registry.save(
        {"model_profile_id": "settings-model", "display_name": "Settings model",
         "model_id": "test-model", "credential_handle": "opaque:test-secret"},
        expected_revision=0,
    )
    path = root / "defaultspack" / "shared" / "frontend_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "general": {"composer_placeholder": "Saved placeholder"},
        "_settings_revision": 7,
        "models": {"google_api_key": "hidden-test-secret"},
        "apis": {"api_keys": [{"value": "hidden-test-secret"}]},
        "_mutation_receipts": {"hidden-test-secret": {}},
    }), encoding="utf-8")
    before = path.read_bytes()
    server, _session, _authority = settings_vertical_server
    observed: list[RequestEnvelope] = []
    original_dispatch = _session.broker._dispatch

    def observe_dispatch(backend, envelope, *args, **kwargs):
        observed.append(envelope)
        return original_dispatch(backend, envelope, *args, **kwargs)

    monkeypatch.setattr(_session.broker, "_dispatch", observe_dispatch)
    cookie, _csrf, _origin = _authenticate(server)
    for suffix in ("", "?full=true"):
        observed.clear()
        status, payload, _ = _request(
            server, "GET", _contract("GET", f"/api/ui/settings{suffix}"),
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 200, payload
        assert len(observed) == 3
        outer, *nested = observed
        assert outer.operation_id == "tobkiri_ui_settings_pack.settings-read"
        assert {item.operation_id for item in nested} == {
            "rumi_model_registry_pack.model-profile-resource",
            "defaultspack.presentation.read",
        }
        assert len({item.context.request_id for item in observed}) == 3
        assert all(
            item.cancellation_requested is outer.cancellation_requested
            and item.deadline_monotonic <= outer.deadline_monotonic
            for item in nested
        )
        data = payload["data"]
        assert data["values"]["general"]["composer_placeholder"] == "Saved placeholder"
        assert data["document_revision"] == 7
        fields = {field["id"]: field for section in data["sections"]
                  if section["id"] == "models" for field in section["fields"]}
        assert fields["preferred_model"]["options"] == [
            {"value": "settings-model", "label": "Settings model"}
        ]
        assert "hidden-test-secret" not in json.dumps(payload)
        assert "opaque:test-secret" not in json.dumps(payload)
    status, payload, _ = _request(
        server, "GET", _contract("GET", "/api/ui/full-catalog"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, payload
    catalog = payload["data"]
    assert catalog["app"]["name"] == "Tobkiri"
    assert catalog["settings"]["document_revision"] == 7
    assert {region["id"] for region in catalog["shell"]["layout"]["regions"]} == {
        "title_bar", "history", "chat_header", "chat_messages", "composer",
        "activity_preview", "right_sidebar", "settings_modal",
    }
    assert catalog["settings"]["values"]["general"]["composer_placeholder"] == "Saved placeholder"
    assert catalog["sidebar"]["items"]
    assert catalog["chat_rendering"]["renderers"]
    assert "hidden-test-secret" not in json.dumps(catalog)
    status, host_payload, _ = _request(
        server, "GET", _contract("GET", "/api/ui/catalog"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, host_payload
    assert host_payload["data"]["dynamic_host"]["profile_id"] == "defaults"
    status, command_payload, _ = _request(
        server, "GET", _contract("GET", "/api/command-protocol/v1/catalog"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, command_payload
    command_catalog = command_payload["data"]
    assert command_catalog["kind"] == "ResolvedCommandCatalog"
    assert command_catalog["rollout"]["legacy_execution_enabled"] is False
    declarations = json.loads((
        Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
        / "commands" / "default_commands.json"
    ).read_text(encoding="utf-8"))
    assert {item["identity"]["id"] for item in command_catalog["commands"]} == {
        item["id"] for item in declarations
    }
    available = {item["identity"]["id"] for item in command_catalog["commands"]
                 if item["availability"]["status"] == "available"}
    assert available == {"terminal", "commit", "push", "patch", "restore"}
    assert all(item["authorization"]["approval_required"]
               for item in command_catalog["commands"]
               if item["identity"]["id"] in available)
    for query in ("profile_id=other", "approved=true", "operation=invoke"):
        status, payload, _ = _request(
            server, "GET", _contract("GET", f"/api/command-protocol/v1/catalog?{query}"),
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 400, payload
    for query in ("profile_id=other", "full=true", "operation=write"):
        status, payload, _ = _request(
            server, "GET", _contract("GET", f"/api/ui/full-catalog?{query}"),
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 400, payload
    for query in ("profile_id=other", "operation=write", "approved=true", "full=false"):
        status, payload, _ = _request(
            server, "GET", _contract("GET", f"/api/ui/settings?{query}"),
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 400, payload
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


def test_history_list_reads_real_captured_store_without_mutation(
    production_server, tmp_path: Path,
) -> None:
    """The full UI reads its own stored history through the real Broker."""
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    store = ConversationStore("defaults", user_data_root=tmp_path / "user-data")
    store.create({"id": "history-1", "title": "History entry"}, expected_revision=0)
    before = store.path.read_bytes()
    server, _session, _authority = production_server
    cookie, _csrf, _origin = _authenticate(server)
    status, payload, _headers = _request(
        server, "GET", _contract("GET", "/api/chat/conversations"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, payload
    assert payload["data"] == {
        "conversations": [{**item, "model": item["model_reference"]} for item in store.snapshot()["conversations"]], "total": 1,
        "store_revision": 1,
    }
    for query in ("profile_id=other", "operation=delete", "approved=true"):
        status, payload, _headers = _request(
            server, "GET", _contract("GET", f"/api/chat/conversations?{query}"),
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 400, payload
    assert store.path.read_bytes() == before


def test_conversation_create_uses_real_broker_and_rejects_replay(
    production_server, tmp_path: Path,
) -> None:
    """Only the captured, authenticated create writes once at its snapshot."""
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    store = ConversationStore("defaults", user_data_root=tmp_path / "user-data")
    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}
    path = _contract("POST", "/api/chat/conversations")
    body = {"id": str(uuid.uuid4()), "expected_revision": 0, "model": "owned-model"}
    for injected in ({"profile_id": "other"}, {"approved": True}, {"operation": "delete"}):
        headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
        status, payload, _ = _request(server, "POST", path, body={**body, **injected}, headers=headers)
        assert status == 400, payload
    status, payload, _ = _request(server, "POST", path, body=body)
    assert status in {401, 403}, payload
    assert not store.path.exists()
    headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
    status, payload, _ = _request(server, "POST", path, body=body, headers=headers)
    assert status == 200, payload
    assert payload["data"]["id"] == body["id"]
    assert payload["data"]["model"] == "owned-model"
    assert store.snapshot()["revision"] == 1
    before = store.path.read_bytes()
    headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
    status, payload, _ = _request(server, "POST", path, body=body, headers=headers)
    assert status != 200, payload
    assert store.path.read_bytes() == before
    assert not ConversationStore("other", user_data_root=tmp_path / "user-data").path.exists()
    identifier = body["id"]
    headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
    status, payload, _ = _request(
        server, "GET", _contract("GET", f"/api/chat/conversation?conversation_id={identifier}"),
        headers=headers,
    )
    assert status == 200, payload
    assert payload["data"]["conversation_revision"] == 1
    assert payload["data"]["model"] == "owned-model"

    def mutate(method: str, revision: int, **extra: object) -> tuple[int, dict]:
        headers["X-Tobkiri-Request-ID"] = str(uuid.uuid4())
        status, payload, _ = _request(
            server, method, _contract(method, "/api/chat/conversation"),
            body={"conversation_id": identifier, "expected_conversation_revision": revision, **extra},
            headers=headers,
        )
        return status, payload

    for updates in ({"id": "replace"}, {"messages": []}, {"conversation_revision": 9}, {"is_starred": "true"}):
        status, payload = mutate("PUT", 1, updates=updates)
        assert status == 400, payload
        assert store.path.read_bytes() == before
    status, payload = mutate("PUT", 1, updates={"title": "Updated", "model": "new-model"})
    assert status == 200, payload
    assert payload["data"]["title"] == "Updated"
    assert payload["data"]["model"] == "new-model"
    assert payload["data"]["conversation_revision"] == 2
    before = store.path.read_bytes()
    for method, extra in (("PUT", {"updates": {"title": "Stale"}}), ("DELETE", {})):
        status, payload = mutate(method, 1, **extra)
        assert status != 200, payload
        assert store.path.read_bytes() == before
    status, payload = mutate("DELETE", 2)
    assert status == 200, payload
    assert payload["data"] == {"deleted": True}
    assert store.get(identifier) is None
    before = store.path.read_bytes()
    status, payload = mutate("DELETE", 2)
    assert status != 200, payload
    assert store.path.read_bytes() == before


def test_model_profile_list_uses_real_registry_and_rejects_client_profile(
    production_server, tmp_path: Path,
) -> None:
    """Read persisted model identities through authenticated production HTTP."""
    from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry

    registry = ModelRegistry("defaults", user_data_root=tmp_path / "user-data")
    registry.save(
        {
            "model_profile_id": "ui-model",
            "display_name": "UI model",
            "model_id": "test-model",
            "credential_handle": "opaque:test-only",
        },
        expected_revision=0,
    )
    server, _session, _authority = production_server
    cookie, _csrf, _origin = _authenticate(server)
    status, payload, _headers = _request(
        server, "GET", _contract("GET", "/api/ai/profiles"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, payload
    assert payload["data"] == {
        "profiles": [{
            "profile_id": "ui-model",
            "display_name": "UI model",
            "model_id": "test-model",
        }],
        "count": 1,
        "registry_revision": 1,
    }
    status, payload, _headers = _request(
        server, "GET", _contract("GET", "/api/ai/profiles?profile_id=other"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 400, payload
    assert payload["data"]["code"] == "invalid_contract_payload"


def test_model_profile_save_http_rejects_authority_and_stale_revision(
    production_server, tmp_path: Path,
) -> None:
    """Save a selectable model through the signed Defaults edge and real owner."""
    from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry

    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    registry = ModelRegistry("defaults", user_data_root=tmp_path / "user-data")
    payload = {
        "model_profile_id": "daily", "model_id": "provider-model",
        "provider_instance_id": "provider.fixture", "display_name": "Daily",
        "expected_revision": 0,
    }

    def post(body):
        return _request(server, "POST", _contract("POST", "/api/ai/profiles"), body=body, headers={
            "Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        })

    for extra in ({"profile_id": "other"}, {"approved": True}, {"credential_handle": "credential:forged"}, {"expected_revision": True}):
        status, result, _ = post({**payload, **extra})
        assert status == 400, result
        assert not registry.path.exists()
    status, result, _ = post(payload)
    assert status == 200, result
    assert result["data"] == {
        "registry_revision": 1, "count": 1,
        "profiles": [{"profile_id": "daily", "model_id": "provider-model", "display_name": "Daily", "provider_id": "provider.fixture", "route_configured": True}],
    }
    stored = registry.resolve("daily")["profile"]
    assert stored["requirements"] == {}
    assert stored["metadata"] == {"provider_connection_id": "provider.fixture"}
    before = registry.path.read_bytes()
    status, result, _ = post(payload)
    assert status != 200, result
    assert registry.path.read_bytes() == before


def test_external_session_cannot_borrow_a_provider_only_edge(production_server) -> None:
    """A unique nested edge is not an implicit Shell capability."""
    from core_runtime.authority.v4 import AuthorityDenied

    _server, session, _authority = production_server
    with pytest.raises(AuthorityDenied, match="captured Shell caller edge"):
        session.context_for(
            "tobkiri.service.ai.generate.v1",
            "rumi_ai_gateway_pack.ai-gateway.generate",
            "external-panel-session",
        )


def test_command_protocol_paths_are_inert_in_captured_production_http(
    production_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unpublished Command Protocol aliases cannot reach any mutable boundary."""

    server, session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    audit_count = len(authority.audit_events())
    journal = server._operation_journal
    assert journal is not None
    settings_path = (
        Path(os.environ["RUMI_USER_DATA"])
        / "defaultspack"
        / "shared"
        / "frontend_settings.json"
    )
    event_store_path = settings_path.with_name("command_invocation_events.sqlite3")
    offline_queue_path = settings_path.with_name("command_offline_queue.sqlite3")
    state_paths = (
        journal.path,
        event_store_path,
        offline_queue_path,
        settings_path,
    )
    state_before = {path: file_snapshot(path) for path in state_paths}
    assert state_before[journal.path] is None
    assert state_before[event_store_path] is None
    assert state_before[offline_queue_path] is None

    broker_invocations: list[str] = []
    broker_submissions: list[str] = []
    journal_writes: list[str] = []

    def unexpected_broker_invocation(*_args, **_kwargs) -> dict[str, object]:
        broker_invocations.append("invoke")
        return {"state": "error", "code": "TEST_BROKER_BLOCKED"}

    def unexpected_broker_submission(*_args, **_kwargs):
        broker_submissions.append("submit")
        raise AssertionError("Command Protocol reached Broker submission")

    monkeypatch.setattr(session.broker, "invoke", unexpected_broker_invocation)
    monkeypatch.setattr(
        session.broker._executor,
        "submit",
        unexpected_broker_submission,
    )

    for method_name in ("renew_session", "begin_operation", "finish_operation"):
        monkeypatch.setattr(
            journal,
            method_name,
            lambda *_args, _name=method_name, **_kwargs: journal_writes.append(_name),
        )

    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    for method, path, body in COMMAND_PROTOCOL_HTTP_CASES:
        status, payload, _ = _request(
            server,
            method,
            path,
            body=body,
            headers={
                **headers,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 404, (path, payload)
        assert payload["success"] is False
        assert payload["error"] == "Not found"

    assert broker_invocations == []
    assert broker_submissions == []
    assert journal_writes == []
    assert {path: file_snapshot(path) for path in state_paths} == state_before
    assert file_snapshot(journal.path) is None
    assert file_snapshot(event_store_path) is None
    assert file_snapshot(offline_queue_path) is None
    assert len(authority.audit_events()) == audit_count
    assert session.broker._executor._work_queue.empty()


def test_provider_configuration_http_requires_approval_and_saves_once(
    production_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP/Broker/approval/credential and registry owners; no network or real key."""
    from core_runtime.authority.ui_operator import sign_ui_operator
    from tobkiri_host.runtime import V4DispatchSession
    from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry

    server, _session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}

    def post(path: str, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
        status, result, _ = _request(
            server, "POST", _contract("POST", path), body=body,
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        return status, result

    root = tmp_path / "user-data"
    registry = ProviderRegistry("defaults", user_data_root=root)
    secret = "fixture-secret-provider-configuration"
    failures = []
    original_invoke = V4DispatchSession.invoke

    def observed_invoke(self, contract_id, operation_id, payload, **kwargs):
        try:
            return original_invoke(self, contract_id, operation_id, payload, **kwargs)
        except Exception as error:
            chain = []
            current = error
            while current is not None:
                chain.append(f"{type(current).__name__}: {current}")
                current = current.__cause__
            failures.append((operation_id, chain))
            raise

    monkeypatch.setattr(V4DispatchSession, "invoke", observed_invoke)
    request = {
        "phase": "prepare", "effect_kind": "provider_configure",
        "correlation_id": str(uuid.uuid4()),
        "request": {
            "connection_name": "fixture", "protocol": "openai-compatible",
            "endpoint": "https://provider.example/v1", "key_value": secret,
        },
    }
    path = "/api/ai/provider-key"
    status, rejected = post(path, {**request, "effect_kind": "shell_execute"})
    assert status == 400, rejected
    assert not registry.path.exists()
    status, prepared = post(path, request)
    assert status == 200, "\n".join(
        f"{operation}: {' -> '.join(chain)}" for operation, chain in failures
    ) or str(prepared)
    effect = prepared["data"]
    assert effect["state"] == "approval_pending"
    assert secret not in json.dumps(prepared)
    assert not registry.path.exists()
    assert not (root / "credentials/material-store/credentials.store.json").exists()
    lookup = {
        "phase": "lookup", "effect_kind": "provider_configure",
        "correlation_id": request["correlation_id"],
    }
    for _ in range(2):
        status, receipt = post(path, lookup)
        assert status == 200, receipt
        assert receipt["data"] == effect
        assert secret not in json.dumps(receipt)
    assert not registry.path.exists()
    assert not (root / "credentials/material-store/credentials.store.json").exists()
    status, missing = post(path, {**lookup, "correlation_id": str(uuid.uuid4())})
    assert status != 200, missing
    status, invalid = post(path, {**lookup, "request": request["request"]})
    assert status == 400, invalid
    other_cookie, other_csrf, other_origin = _authenticate(server)
    status, foreign, _ = _request(
        server, "POST", _contract("POST", path), body=lookup,
        headers={
            "Cookie": other_cookie, "Origin": other_origin,
            "X-Rumi-CSRF": other_csrf, "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert status != 200, foreign
    assert effect["effect_id"] not in json.dumps(foreign)
    status, denied = post(path, {"phase": "resume", "effect_id": effect["effect_id"]})
    assert status != 200 or denied["data"]["state"] != "succeeded"
    assert not registry.path.exists()
    approval_id = effect["approval_request_id"]
    status, approval = post("/api/interactive-approval/v1/get", {"request_id": approval_id})
    assert status == 200, approval
    data = approval["data"]
    status, approved = post("/api/interactive-approval/v1/approve", {
        "request_id": approval_id, "confirmation_text": "EXECUTE",
        "ui_operator": sign_ui_operator(
            approval_id, nonce="provider-configuration-approval", decision="approve",
            request_snapshot_digest=data["request_snapshot_digest"],
            typed_confirmation_digest=data["typed_confirmation_digest"],
        ),
    })
    assert status == 200, approved
    for _ in range(2):
        status, result = post(path, {"phase": "resume", "effect_id": effect["effect_id"]})
        assert status == 200, result
        assert result["data"]["state"] == "succeeded", result
        assert secret not in json.dumps(result)
        snapshot = registry.snapshot()
        assert snapshot["revision"] == 1
        assert snapshot["providers"][0]["credential_handle"].startswith("credential:")
    assert secret not in registry.path.read_text()
    stored = root / "credentials/material-store/credentials.store.json"
    assert secret not in stored.read_text()
    assert len(json.loads(stored.read_text())["credentials"]) == 1
    assert secret not in json.dumps(authority.audit_events(), default=str)


def test_saved_settings_reach_host_credential_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real settings/approval/owners/Gateway/Broker; guest and HTTPS are doubles."""
    import io
    from core_runtime import credential_transport
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
    from tobkiri_host.credential_store import host_credential_store_factory
    from tobkiri_host.runtime import V4DispatchSession

    requests = []

    def open_provider(request, *, timeout):
        assert timeout > 0
        assert request.full_url == "https://provider.example/v1/chat/completions"
        assert request.get_header("Authorization") == (
            "Bearer fixture-secret-provider-configuration"
        )
        body = json.loads(request.data)
        assert body["model"] == "organization/raw-model"
        assert body["messages"] == [{"role": "user", "content": "Hello"}]
        requests.append(body)
        return io.BytesIO(json.dumps({
            "choices": [{"message": {"content": "Host transport reply"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode())

    monkeypatch.setattr(credential_transport, "_open_pinned_request", open_provider)
    servers = _captured_production_server(
        tmp_path, monkeypatch,
        packvm_backends=BackendRegistry((_SavedPackVmBackend(),)),
        credential_store_factory=host_credential_store_factory,
    )
    fixture = next(servers)
    server, _session, _authority = fixture
    try:
        test_provider_configuration_http_requires_approval_and_saves_once(
            fixture, tmp_path, monkeypatch,
        )
        failures = []
        original_invoke = V4DispatchSession.invoke

        def observe(self, *args, **kwargs):
            try:
                value = original_invoke(self, *args, **kwargs)
                return value
            except Exception as error:
                chain = []
                while error is not None:
                    chain.append(f"{type(error).__name__}: {error}")
                    error = error.__cause__
                failures.append(chain)
                raise

        monkeypatch.setattr(V4DispatchSession, "invoke", observe)
        cookie, csrf, origin = _authenticate(server)

        def post(path, body):
            return _request(server, "POST", _contract("POST", path), body=body,
                            headers={"Cookie": cookie, "Origin": origin,
                                     "X-Rumi-CSRF": csrf,
                                     "X-Tobkiri-Request-ID": str(uuid.uuid4())})

        status, result, _ = post("/api/ai/profiles", {
            "model_profile_id": "daily", "model_id": "organization/raw-model",
            "provider_instance_id": "provider.fixture", "display_name": "Daily",
            "expected_revision": 0,
        })
        assert status == 200, result
        store = ConversationStore("defaults", user_data_root=tmp_path / "user-data")
        store.create({"id": "chat", "model_reference": "daily"}, expected_revision=0)
        status, result, _ = post("/api/chat/turn", {"request": {
            "turn_id": "turn", "conversation_id": "chat",
            "conversation_revision": 1, "content": "Hello",
        }})
        assert status == 200, result
        assert result["data"]["status"] == "completed", (result, failures)
        assert len(requests) == 1
        assert [item["content"] for item in store.get("chat")["messages"]] == [
            "Hello", "Host transport reply",
        ]
    finally:
        servers.close()


def test_all_high_risk_commands_http_require_host_approval_and_run_once(
    command_vertical_server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise every Command ref through HTTP, Host approval, and one resume.

    This is intentionally one mounted repository.  The PackVM test transport
    is limited to the real shell-policy ABI; every command effect still uses
    the production adapter, Host coordinator, signed UI operator, and its
    captured Host Provider.
    """

    from core_runtime.authority.ui_operator import sign_ui_operator
    from ecosystem.rumi_workspace_mount_pack.runtime.mounts import WorkspaceMountStore

    server, session, _authority = command_vertical_server
    workspace = tmp_path / "workspace"
    remote = tmp_path / "vertical-remote.git"
    workspace.mkdir()
    subprocess.run(("git", "init", "-q", str(workspace)), check=True)
    subprocess.run(("git", "init", "--bare", "-q", str(remote)), check=True)
    for key, value in (
        ("user.email", "test@example.com"),
        ("user.name", "Tobkiri Test"),
    ):
        subprocess.run(("git", "-C", str(workspace), "config", key, value), check=True)
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    (workspace / "restore.txt").write_text("restore seed\n", encoding="utf-8")
    (workspace / "patch.txt").write_text("patch before\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(workspace), "add", "."), check=True)
    subprocess.run(("git", "-C", str(workspace), "commit", "-qm", "seed"), check=True)
    branch = subprocess.run(
        ("git", "-C", str(workspace), "symbolic-ref", "--short", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    approved_remote_url = "https://push.example.invalid/tobkiri/vertical.git"
    subprocess.run(
        ("git", "-C", str(workspace), "remote", "add", "origin", approved_remote_url),
        check=True,
    )

    mounts = WorkspaceMountStore(
        "defaults",
        user_data_root=Path(os.environ["TOBKIRI_USER_DATA"]),
    )
    mounted = mounts.mount("vertical", str(workspace), expected_revision=0)
    mounts.select("vertical", expected_revision=mounted["revision"])

    cookie, csrf, origin = _authenticate(server)
    headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}

    def post(path: str, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
        status, response, _ = _request(
            server,
            "POST",
            _contract("POST", path),
            body=body,
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        return status, response

    def git_output(*args: str, check: bool = True) -> str:
        return subprocess.run(
            ("git", "-C", str(workspace), *args),
            check=check,
            capture_output=True,
            text=True,
        ).stdout

    # The product plan retains a safe HTTPS remote and performs its normal
    # URL validation, source/remote CAS, and lease construction.  Only the
    # final, already revalidated transport is redirected to an isolated bare
    # repository, so the vertical test never contacts the network.
    publish_contribution = next(
        contribution
        for backend in session.broker._backends.registered
        for contribution in getattr(backend, "_contributions", {}).values()
        if contribution.operation_id == "rumi_git_publish_pack.git-push"
    )
    provider_globals = publish_contribution.invoke.__globals__
    original_git = provider_globals["_git"]
    git_executable = provider_globals["_git_executable"]

    def local_final_push(
        repository: Path,
        args: list[str],
        *,
        timeout: int = 30,
        hardened: bool = False,
    ) -> str:
        if not args or args[0] != "push":
            return original_git(repository, args, timeout=timeout, hardened=hardened)
        assert hardened is True
        assert args[-2] == approved_remote_url
        assert args[-1].endswith(f":refs/heads/{branch}")
        completed = subprocess.run(
            (
                git_executable(),
                "-C",
                str(repository),
                "-c",
                "protocol.file.allow=always",
                *args[:-2],
                str(remote),
                args[-1],
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (completed.stdout + completed.stderr)[:256_000]
        if completed.returncode != 0:
            raise RuntimeError(output.strip() or "test local Git push failed")
        return output

    # Host extensions are loaded from verified bytes under a digest-scoped
    # module name.  Patch that exact captured Provider transport rather than
    # an ordinary import which production dispatch never calls.
    monkeypatch.setitem(provider_globals, "_git", local_final_push)

    def approve(approval_request_id: str, nonce: str) -> None:
        status, approval = post(
            "/api/interactive-approval/v1/get",
            {"request_id": approval_request_id},
        )
        assert status == 200, approval
        approval_data = approval["data"]
        assert approval_data["typed_confirmation_required"] is True
        status, approved = post(
            "/api/interactive-approval/v1/approve",
            {
                "request_id": approval_request_id,
                "confirmation_text": "EXECUTE",
                "ui_operator": sign_ui_operator(
                    approval_request_id,
                    nonce=nonce,
                    decision="approve",
                    request_snapshot_digest=approval_data["request_snapshot_digest"],
                    typed_confirmation_digest=approval_data["typed_confirmation_digest"],
                ),
            },
        )
        assert status == 200, approved
        assert approved["data"]["state"] == "approved"

    def exercise(
        command_ref: str,
        arguments: Mapping[str, object],
        before_effect: Callable[[], object],
        after_effect: Callable[[], object],
    ) -> None:
        """Prove a command has no pre-approval effect and one final effect."""

        invocation_id = f"vertical-{command_ref}"
        request = {
            "phase": "prepare",
            "invocation_id": invocation_id,
            "command_ref": command_ref,
            "arguments": dict(arguments),
            "presentation": {"title": "Untrusted copy", "summary": "Run command"},
        }
        expected_before = before_effect()

        for suffix, forbidden in enumerate(
            (
                {**request, "approved": True},
                {
                    **request,
                    "arguments": {**dict(arguments), "authority_receipt": "forged"},
                },
            ),
            start=1,
        ):
            # The adapter reserves durable state before it calls the Host
            # coordinator.  A rejected authority field can therefore leave a
            # conservative tombstone, which must never share the real
            # invocation's idempotency key.
            forbidden = {
                **forbidden,
                "invocation_id": f"{invocation_id}-forged-{suffix}",
            }
            status, rejected = post("/api/command-protocol/v1/high-risk", forbidden)
            assert status >= 400, rejected
            assert rejected["success"] is False
            assert before_effect() == expected_before

        status, pending = post("/api/command-protocol/v1/high-risk", request)
        assert status == 200, pending
        assert pending["data"]["state"] == "approval_pending"
        assert before_effect() == expected_before

        approve(str(pending["data"]["approval_request_id"]), invocation_id)
        status, completed = post(
            "/api/command-protocol/v1/high-risk",
            {"phase": "resume", "invocation_id": invocation_id},
        )
        assert status == 200, completed
        assert completed["data"]["state"] == "succeeded"
        expected_after = after_effect()
        assert expected_after != expected_before

        status, replay = post(
            "/api/command-protocol/v1/high-risk",
            {"phase": "resume", "invocation_id": invocation_id},
        )
        assert status == 200, replay
        assert replay["data"] == completed["data"]
        assert after_effect() == expected_after

    def terminal_before() -> tuple[int, str]:
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(workspace),
                "config",
                "--local",
                "--get-all",
                "tobkiri.vertical.terminal",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode, completed.stdout

    exercise(
        "terminal",
        {
            "command": [
                "git",
                "config",
                "--local",
                "--add",
                "tobkiri.vertical.terminal",
                "ran",
            ],
            "cwd": ".",
        },
        terminal_before,
        lambda: (0, git_output("config", "--local", "--get-all", "tobkiri.vertical.terminal")),
    )

    (workspace / "commit.txt").write_text("commit effect\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(workspace), "add", "commit.txt"), check=True)
    exercise(
        "commit",
        {"workspace_id": "vertical", "message": "vertical command commit"},
        lambda: git_output("rev-parse", "HEAD").strip(),
        lambda: git_output("rev-parse", "HEAD").strip(),
    )

    (workspace / "restore.txt").write_text("restore changed\n", encoding="utf-8")
    exercise(
        "restore",
        {
            "workspace_id": "vertical",
            "paths": ["restore.txt"],
            "source": "HEAD",
        },
        lambda: (workspace / "restore.txt").read_text(encoding="utf-8"),
        lambda: (workspace / "restore.txt").read_text(encoding="utf-8"),
    )

    patch = """diff --git a/patch.txt b/patch.txt
index ba35db0..e311d74 100644
--- a/patch.txt
+++ b/patch.txt
@@ -1 +1 @@
-patch before
+patch after
"""
    exercise(
        "patch",
        {"workspace_id": "vertical", "patch": patch},
        lambda: (workspace / "patch.txt").read_text(encoding="utf-8"),
        lambda: (workspace / "patch.txt").read_text(encoding="utf-8"),
    )

    remote_ref = f"refs/heads/{branch}"
    exercise(
        "push",
        {
            "workspace_id": "vertical",
            "remote": "origin",
            "branch": branch,
            "force_with_lease": False,
        },
        lambda: subprocess.run(
            ("git", "--git-dir", str(remote), "rev-parse", "--verify", remote_ref),
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        lambda: subprocess.run(
            ("git", "--git-dir", str(remote), "rev-parse", "--verify", remote_ref),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
    )


def test_named_profile_registry_crud_http_preserves_active_pointer_and_history(
    production_server,
) -> None:
    """Exercise the authenticated Profile registry through its real HTTP surface."""

    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    read_headers = {
        "Cookie": cookie,
        "X-Tobkiri-Request-ID": str(uuid.uuid4()),
    }
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
        "X-Tobkiri-Request-ID": str(uuid.uuid4()),
    }

    status, initial, _ = _request(
        server,
        "GET",
        "/api/v4/profiles",
        headers=read_headers,
    )
    assert status == 200, initial
    initial_registry = initial["data"]
    assert initial_registry["active_profile_id"] == "defaults"
    assert initial_registry["active_profile_revision"]
    generation = initial_registry["generation"]

    def mutate(action: str, body: dict[str, object]) -> dict[str, object]:
        status_code, payload, _ = _request(
            server,
            "POST",
            f"/api/v4/profiles/{action}",
            body=body,
            headers={
                **mutation_headers,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status_code == 200, payload
        return payload["data"]

    created = mutate(
        "create",
        {
            "profile_id": "profile-a",
            "display_name": "Profile A",
            "source_profile_id": "defaults",
            "expected_store_generation": generation,
        },
    )
    created_profile = next(
        profile
        for profile in created["profiles"]
        if profile["profile_id"] == "profile-a"
    )
    assert created_profile["profile_revision"]
    assert created_profile["parent_revision"] is None
    assert created["active_profile_id"] == initial_registry["active_profile_id"]
    assert created["active_profile_revision"] == initial_registry["active_profile_revision"]

    updated = mutate(
        "update",
        {
            "profile_id": "profile-a",
            "display_name": "Profile A updated",
            "expected_profile_revision": created_profile["profile_revision"],
            "expected_store_generation": created["generation"],
        },
    )
    updated_profile = next(
        profile
        for profile in updated["profiles"]
        if profile["profile_id"] == "profile-a"
    )
    assert updated_profile["profile_revision"] != created_profile["profile_revision"]
    assert updated_profile["parent_revision"] == created_profile["profile_revision"]
    assert updated["active_profile_id"] == "defaults"
    assert updated["active_profile_revision"] == initial_registry["active_profile_revision"]

    duplicated = mutate(
        "duplicate",
        {
            "profile_id": "profile-a",
            "new_profile_id": "profile-b",
            "display_name": "Profile B",
            "expected_profile_revision": updated_profile["profile_revision"],
            "expected_store_generation": updated["generation"],
        },
    )
    duplicated_profile = next(
        profile
        for profile in duplicated["profiles"]
        if profile["profile_id"] == "profile-b"
    )
    assert duplicated_profile["profile_revision"]
    assert duplicated_profile["parent_revision"] is None
    assert duplicated["active_profile_id"] == "defaults"

    deleted = mutate(
        "delete",
        {
            "profile_id": "profile-b",
            "expected_profile_revision": duplicated_profile["profile_revision"],
            "expected_store_generation": duplicated["generation"],
        },
    )
    assert all(profile["profile_id"] != "profile-b" for profile in deleted["profiles"])
    assert deleted["changed_profile"]["profile_id"] == "profile-b"
    assert deleted["changed_profile"]["tombstone"] is True
    assert deleted["action"] == "delete"
    assert deleted["active_profile_id"] == "defaults"
    assert deleted["active_profile_revision"] == initial_registry["active_profile_revision"]

    stale_status, stale, _ = _request(
        server,
        "POST",
        "/api/v4/profiles/update",
        body={
            "profile_id": "profile-a",
            "display_name": "stale",
            "expected_profile_revision": created_profile["profile_revision"],
            "expected_store_generation": deleted["generation"],
        },
        headers={
            **mutation_headers,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert stale_status == 409
    assert stale["success"] is False


def test_home_and_pack_workflow_use_only_real_broker_contracts(
    production_server,
) -> None:
    server, session, authority = production_server
    authority_path = authority.path
    cookie, csrf, origin = _authenticate(server)
    read_headers = {
        "Cookie": cookie,
        "X-Tobkiri-Request-ID": str(uuid.uuid4()),
    }
    before = len(authority.audit_events())
    status, dashboard, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/home/dashboard"),
        headers=read_headers,
    )
    assert status == 200
    assert dashboard["data"]["kernel"]["status"] == "running"
    events = authority.audit_events()
    assert len(events) == before + 4
    assert [event["event_state"] for event in events[-3:]] == [
        "reserved",
        "dispatched",
        "committed",
    ]

    status, catalog, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/pack-control/catalog"),
        headers={**read_headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200
    assert catalog["data"]["profile_id"] == "defaults"

    status, ui_catalog, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/ui/catalog"),
        headers={**read_headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200
    dynamic_host = ui_catalog["data"]["dynamic_host"]
    assert dynamic_host["profile_revision"] != dynamic_host["plan_hash"]
    assert dynamic_host["activation_id"] == session.activation_id
    status_contribution = next(
        item for item in dynamic_host["contributions"] if item["label"] == "pack.status"
    )
    assert status_contribution["resolved_profile_id"] == session.profile_id
    assert status_contribution["resolved_profile_revision"] == session.profile_revision
    assert status_contribution["resolved_activation_id"] == session.activation_id
    assert status_contribution["resolved_plan_hash"] == session.plan_digest

    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }

    audit_before_capability = len(authority.audit_events())
    status, capability_result, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/ui/capability/invoke"),
        body={
            "request_id": str(uuid.uuid4()),
            "expires_at": time.time() + 30,
            "profile_id": dynamic_host["profile_id"],
            "profile_revision": dynamic_host["profile_revision"],
            "activation_id": dynamic_host["activation_id"],
            "plan_hash": dynamic_host["plan_hash"],
            "catalog_hash": dynamic_host["catalog_hash"],
            "contribution_id": status_contribution["contribution_id"],
            "owner_pack_id": status_contribution["owner_pack_id"],
            "contract_id": status_contribution["action_contract"],
            "payload": {"pack_id": "defaultspack"},
        },
        headers={
            **mutation_headers,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert status == 200, capability_result
    assert capability_result["data"]["pack_id"] == "defaultspack"
    assert len(authority.audit_events()) == audit_before_capability + 3

    def post(
        target: str,
        body: dict[str, object],
        *,
        request_id: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        # This workflow proves durable state and route correctness. Allow the
        # real server to finish its complete integrity validation; dedicated
        # tests cover unknown mutation outcomes and eventual reconciliation.
        status_code, payload, _ = _request(
            server,
            "POST",
            _contract("POST", target),
            body=body,
            headers={
                **mutation_headers,
                "X-Tobkiri-Request-ID": request_id or str(uuid.uuid4()),
            },
            timeout_seconds=EVENTUAL_RECONCILIATION_TIMEOUT_SECONDS,
        )
        return status_code, payload

    # Use the production optional-Pack lifecycle fixture.  Picking the first
    # optional catalog row is not sufficient: declarative content Packs are
    # intentionally installable but have no runtime Function to enable.
    target_pack = "tobkiri_workflow_pack"
    target_row = next(
        item for item in catalog["data"]["packs"] if item["pack_id"] == target_pack
    )
    assert target_row["required"] is False
    assert post("/api/pack-control/install", {"pack_id": target_pack})[0] == 200
    never_approved_request = str(uuid.uuid4())
    denied_status, denied = post(
        "/api/pack-control/approval-revoke",
        {"pack_id": target_pack},
        request_id=never_approved_request,
    )
    replay_status, replayed_denial = post(
        "/api/pack-control/approval-revoke",
        {"pack_id": target_pack},
        request_id=never_approved_request,
    )
    assert denied_status == replay_status == 403
    assert denied == replayed_denial
    assert denied["data"]["code"] == "UNAPPROVED"
    assert denied["data"]["retryable"] is False
    candidate_status, candidate = post(
        "/api/pack-control/approval-candidate", {"pack_id": target_pack}
    )
    assert candidate_status == 200
    assert (
        post(
            "/api/pack-control/approval-approve",
            {
                "pack_id": target_pack,
                "candidate_id": candidate["data"]["candidate_id"],
            },
        )[0]
        == 200
    )
    enable_status, enabled = post("/api/pack-control/enable", {"pack_id": target_pack})
    assert enable_status == 200, enabled
    assert enabled["data"]["enabled"] is True
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    assert post("/api/pack-control/restart", {})[0] == 200
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    status, refreshed_catalog, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/ui/catalog"),
        headers={
            "Cookie": cookie,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert status == 200, refreshed_catalog
    refreshed_host = refreshed_catalog["data"]["dynamic_host"]
    refreshed_status = next(
        item for item in refreshed_host["contributions"] if item["label"] == "pack.status"
    )
    status, persisted = post(
        "/api/ui/capability/invoke",
        {
            "request_id": str(uuid.uuid4()),
            "expires_at": time.time() + 30,
            "profile_id": refreshed_host["profile_id"],
            "profile_revision": refreshed_host["profile_revision"],
            "activation_id": refreshed_host["activation_id"],
            "plan_hash": refreshed_host["plan_hash"],
            "catalog_hash": refreshed_host["catalog_hash"],
            "contribution_id": refreshed_status["contribution_id"],
            "owner_pack_id": refreshed_status["owner_pack_id"],
            "contract_id": refreshed_status["action_contract"],
            "payload": {"pack_id": target_pack},
        },
    )
    assert status == 200, persisted
    assert persisted["data"]["enabled"] is True
    assert post("/api/pack-control/disable", {"pack_id": target_pack})[0] == 200
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    revoke_status, revoked = post("/api/pack-control/approval-revoke", {"pack_id": target_pack})
    assert revoke_status == 200, revoked
    assert revoked["data"]["approved"] is False
    assert revoked["data"]["approval_status"] == "revoked"
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    assert post("/api/pack-control/restart", {})[0] == 200
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    catalog_status, after_revoke, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/pack-control/catalog"),
        headers={
            "Cookie": cookie,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert catalog_status == 200, after_revoke
    revoked_pack = next(
        item for item in after_revoke["data"]["packs"] if item["pack_id"] == target_pack
    )
    assert revoked_pack["approved"] is False
    assert revoked_pack["enabled"] is False
    assert revoked_pack["approval_reason"] == "approval_revoked"
    enable_status, denied = post("/api/pack-control/enable", {"pack_id": target_pack})
    assert enable_status == 409
    assert denied["data"]["code"] == "STALE_REVISION"

    # A revoked approval cannot be reused, but a fresh normal ceremony must
    # restore this optional Pack without duplicating the persisted selection.
    candidate_status, candidate = post(
        "/api/pack-control/approval-candidate", {"pack_id": target_pack}
    )
    assert candidate_status == 200, candidate
    approve_status, approved = post(
        "/api/pack-control/approval-approve",
        {"pack_id": target_pack, "candidate_id": candidate["data"]["candidate_id"]},
    )
    assert approve_status == 200, approved
    enable_status, reenabled = post("/api/pack-control/enable", {"pack_id": target_pack})
    assert enable_status == 200, reenabled
    assert reenabled["data"]["enabled"] is True
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}
    assert post("/api/pack-control/restart", {})[0] == 200
    cookie, csrf, origin = _authenticate(server)
    # Restart invalidates the projection. Reconcile only a bounded read timeout;
    # never replay restart/approval or hide any other response failure.
    read_deadline = time.monotonic() + EVENTUAL_RECONCILIATION_TIMEOUT_SECONDS
    while True:
        status, selection, _ = _request(
            server, "GET", _contract("GET", "/api/runtime-surface/profile"),
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
            timeout_seconds=max(0.1, read_deadline - time.monotonic()),
        )
        if status != 504 or selection.get("data", {}).get("code") != "TIMEOUT":
            break
        if time.monotonic() >= read_deadline:
            break
        time.sleep(0.1)
    assert status == 200, selection
    selected_ids = [
        item["pack_id"] for item in selection["data"]["data"]["profile_document"]["packs"]
    ]
    assert selected_ids.count(target_pack) == 1
    assert len(selected_ids) == len(set(selected_ids))

    with AuthorityStore(authority_path) as current_authority:
        assert any(
            event["event_type"] == "pack_approval_revoked" and event["event_state"] == "committed"
            for event in current_authority.audit_events()
        )

    with AuthorityStore(authority_path) as current_authority:
        audit_before_legacy = len(current_authority.audit_events())
    status, retired, _ = _request(
        server,
        "GET",
        "/api/panel/dashboard",
        headers={"Cookie": cookie},
    )
    assert status == 410
    assert retired["data"]["state"] == "legacy_api_retired"
    with AuthorityStore(authority_path) as current_authority:
        assert len(current_authority.audit_events()) == audit_before_legacy


@pytest.mark.parametrize("refresh_error", [RuntimeError, TypeError])
def test_pack_enable_keeps_journal_success_when_runtime_refresh_fails(
    production_server,
    monkeypatch: pytest.MonkeyPatch,
    refresh_error: type[Exception],
) -> None:
    """A committed Pack enable is not rewritten by a failed runtime refresh."""

    server, session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }

    def post(
        target: str,
        body: Mapping[str, object],
        *,
        request_id: str | None = None,
    ) -> tuple[str, int, dict[str, object]]:
        current_request_id = request_id or str(uuid.uuid4())
        status, payload, _ = _request(
            server,
            "POST",
            _contract("POST", target),
            body=body,
            headers={
                **headers,
                "X-Tobkiri-Request-ID": current_request_id,
            },
        )
        return current_request_id, status, payload

    pack_id = "tobkiri_workflow_pack"
    _, status, installed = post("/api/pack-control/install", {"pack_id": pack_id})
    assert status == 200, installed
    _, status, candidate = post(
        "/api/pack-control/approval-candidate",
        {"pack_id": pack_id},
    )
    assert status == 200, candidate
    _, status, approved = post(
        "/api/pack-control/approval-approve",
        {
            "pack_id": pack_id,
            "candidate_id": candidate["data"]["candidate_id"],
        },
    )
    assert status == 200, approved

    handler = server.handler_class
    assert handler is not None
    binding = handler._current_panel_auth_binding()
    assert binding is not None
    raw_session_id = cookie.split("=", 1)[1]
    verified_session = server._panel_auth_manager.verify_session(
        raw_session_id,
        binding,
    )
    assert verified_session is not None
    refresh_attempts = 0

    def fail_refresh(_session: object | None = None) -> None:
        nonlocal refresh_attempts
        refresh_attempts += 1
        raise refresh_error("stale Host contract")

    monkeypatch.setattr(handler, "_runtime_refresh", staticmethod(fail_refresh))

    dispatch_calls = 0
    original_broker_invoke = session.broker.invoke

    def counted_broker_invoke(*args: object, **kwargs: object) -> object:
        nonlocal dispatch_calls
        dispatch_calls += 1
        return original_broker_invoke(*args, **kwargs)

    monkeypatch.setattr(session.broker, "invoke", counted_broker_invoke)
    request_id = str(uuid.uuid4())
    _, status, enabled = post(
        "/api/pack-control/enable",
        {"pack_id": pack_id},
        request_id=request_id,
    )
    assert status == 200, enabled
    assert enabled["data"]["enabled"] is True
    assert refresh_attempts == 1
    assert dispatch_calls == 1

    journal = server._operation_journal
    assert journal is not None
    operation = journal.operation_status(
        request_id,
        session_id=str(verified_session["session_id"]),
    )
    assert operation["state"] == "succeeded"
    assert operation["result"]["enabled"] is True


def test_revoke_denials_respond_before_logging_and_release_for_retry(
    production_server,
) -> None:
    """Known denials remain bounded under logging delay and concurrent retry."""

    server, session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    mutation_headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }

    def revoke(request_id: str) -> tuple[int, dict[str, object]]:
        status, payload, _headers = _request(
            server,
            "POST",
            _contract("POST", "/api/pack-control/approval-revoke"),
            body={"pack_id": "rumi_git_read_pack"},
            headers={
                **mutation_headers,
                "X-Tobkiri-Request-ID": request_id,
            },
        )
        return status, payload

    install_status, install_payload, _headers = _request(
        server,
        "POST",
        _contract("POST", "/api/pack-control/install"),
        body={"pack_id": "rumi_git_read_pack"},
        headers={
            **mutation_headers,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert install_status == 200, install_payload

    log_entered = threading.Event()
    initial_denials_logged = threading.Event()
    initial_access_logged = threading.Event()
    all_access_logged = threading.Event()
    release_log = threading.Event()
    denial_log_count = 0
    initial_access_log_count = 0
    access_log_count = 0
    log_count_lock = threading.Lock()
    delay_access_logs = threading.Event()

    class DelayedReplayAccessLog(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            nonlocal access_log_count, denial_log_count, initial_access_log_count
            message = record.getMessage()
            if message.startswith("Contract dispatch denied"):
                assert message.endswith(
                    "tobkiri.host.pack-control.v4/approval.revoke: UNAPPROVED"
                )
                with log_count_lock:
                    denial_log_count += 1
                    if denial_log_count == len(request_ids):
                        initial_denials_logged.set()
            elif message.startswith("API:"):
                assert '"POST /api/contracts/defaultspack/' in message
                status_and_length = message.rsplit(" ", 2)
                assert status_and_length[-2] == "403"
                assert int(status_and_length[-1]) > 0
                if not delay_access_logs.is_set():
                    with log_count_lock:
                        initial_access_log_count += 1
                        if initial_access_log_count == len(request_ids):
                            initial_access_logged.set()
                    return
                with log_count_lock:
                    access_log_count += 1
                    if access_log_count == len(request_ids):
                        all_access_logged.set()
                log_entered.set()
                release_log.wait()

    delayed_log = DelayedReplayAccessLog()
    api_logger = logging.getLogger("core_runtime.pack_api_server")
    original_log_level = api_logger.level
    api_logger.setLevel(logging.INFO)
    api_logger.addHandler(delayed_log)
    request_ids = [str(uuid.uuid4()) for _index in range(8)]
    initial = [revoke(request_id) for request_id in request_ids]
    assert all(status == 403 for status, _payload in initial)
    assert all(
        payload["data"]["code"] == "UNAPPROVED"
        for _status, payload in initial
    )
    assert all(
        payload["data"]["retryable"] is False
        for _status, payload in initial
    )
    assert initial_denials_logged.wait(timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS)
    assert denial_log_count == len(request_ids)
    # ``_request`` returns after it receives the complete response body; the
    # server deliberately writes its access entry later, after closing that
    # response.  Wait for that post-response boundary instead of assuming the
    # final handler has already reached ``finish`` on a loaded CI worker.
    assert initial_access_logged.wait(timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS)
    assert initial_access_log_count == len(request_ids)
    audit_after_initial = len(authority.audit_events())
    delay_access_logs.set()

    executor = ThreadPoolExecutor(max_workers=len(request_ids))
    try:
        responses = [executor.submit(revoke, request_id) for request_id in request_ids]
        assert log_entered.wait(timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS)
        completed, pending = wait(
            responses,
            timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS,
        )
        assert not pending
        assert len(completed) == len(request_ids)
        replayed = [response.result() for response in responses]
        assert replayed == initial
        # Every replay client received its complete denial body while the
        # first access log still held this Handler's serialization lock.
        assert not release_log.is_set()
        assert not all_access_logged.is_set()
        assert server.server is not None
        assert server.server._active_requests > 0
        # Exact terminal replay bypasses fresh mutation admission and adds no
        # audit side effects while handlers remain blocked after close.
        assert len(authority.audit_events()) == audit_after_initial
    finally:
        release_log.set()
        executor.shutdown(wait=True, cancel_futures=True)
        if server.server is not None:
            assert server.server.wait_for_request_drain(
                FRONTEND_MUTATION_TIMEOUT_SECONDS
            )
        api_logger.removeHandler(delayed_log)
        api_logger.setLevel(original_log_level)
        delayed_log.close()

    assert denial_log_count == len(request_ids)
    assert all_access_logged.wait(timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS)
    assert access_log_count == len(request_ids)
    assert server.server is not None
    assert server.server.wait_for_request_drain(FRONTEND_MUTATION_TIMEOUT_SECONDS)
    assert server.server._active_requests == 0
    retry_status, retry_payload = revoke(str(uuid.uuid4()))
    assert retry_status == 403
    assert retry_payload["data"]["code"] == "UNAPPROVED"
    assert len(authority.audit_events()) > audit_after_initial
    assert session.broker._executor._work_queue.empty()
    assert not session.broker._closed


def test_runtime_surface_reads_use_the_canonical_broker_contract(
    production_server,
) -> None:
    server, _session, authority = production_server
    cookie, _csrf, _origin = _authenticate(server)
    headers = {
        "Cookie": cookie,
        "X-Tobkiri-Request-ID": str(uuid.uuid4()),
    }

    targets = {
        "profile": "/api/runtime-surface/profile",
        "settings": "/api/runtime-surface/settings",
        "packs": "/api/runtime-surface/topology/packs",
        "contracts": "/api/runtime-surface/topology/contracts",
        "operations": "/api/runtime-surface/topology/operations",
        "principals": "/api/runtime-surface/topology/principals",
    }
    responses = {}
    for surface, target in targets.items():
        status, payload, _ = _request(
            server,
            "GET",
            _contract("GET", target),
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 200, payload
        envelope = payload["data"]
        assert envelope["runtime_surface_api_version"] == ("io.tobkiri.launcher.runtime-surface.v4")
        assert envelope["surface"] == surface
        assert envelope["state"] == "ready"
        assert envelope["catalog_revision"].startswith("sha256:")
        assert all(
            set(record) == {"digest", "source_ref"} for record in envelope["records"].values()
        )
        responses[surface] = envelope

    assert responses["profile"]["data"]["profile"]["profile_id"] == "defaults"
    verified = [
        item
        for item in responses["operations"]["data"]["operations"]
        if item["schema"].get("input_schema")
    ]
    assert verified
    assert all(item["route"]["function_id"] for item in verified)
    assert any(event["event_state"] == "committed" for event in authority.audit_events())


def test_authoritative_profile_catalog_selection_completes_real_http_ceremony(
    production_server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.conformance_support.packaged_profile import (
        packaged_profile_bundle_root,
    )

    server, _session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    status, registry_response, _ = _request(
        server,
        "GET",
        "/api/v4/profiles",
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, registry_response
    registry = registry_response["data"]
    defaults = next(item for item in registry["profiles"] if item["profile_id"] == "defaults")
    for profile_id, display_name in (("alpha", "Alpha"), ("beta", "Beta")):
        status, registry_response, _ = _request(
            server,
            "POST",
            "/api/v4/profiles/duplicate",
            body={
                "profile_id": "defaults",
                "new_profile_id": profile_id,
                "display_name": display_name,
                "expected_profile_revision": defaults["profile_revision"],
                "expected_store_generation": registry["generation"],
            },
            headers={
                "Cookie": cookie,
                "Origin": origin,
                "X-Rumi-CSRF": csrf,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 200, registry_response
        registry = registry_response["data"]
    status, catalog_response, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profiles"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, catalog_response
    catalog = catalog_response["data"]["data"]
    assert {item["profile_id"] for item in catalog["profiles"]} >= {
        "defaults",
        "alpha",
        "beta",
    }
    assert catalog["selection"] == {
        "state": "active_execution",
        "selected_profile_id": "defaults",
        "execution_profile_id": "defaults",
    }
    selected = next(
        item for item in catalog["profiles"] if item["profile_id"] == "alpha"
    )
    assert selected["active"] is False
    assert selected["lifecycle_state"] == "available"
    status, profile_response, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, profile_response
    active = profile_response["data"]
    desired = [
        item["pack_id"]
        for item in selected["pack_closure"]
        if item["role"] not in {"base", "shell", "application", "dependency"}
    ]
    headers = {"Cookie": cookie, "Origin": origin, "X-Rumi-CSRF": csrf}

    def post(path: str, body: Mapping[str, object]):
        return _request(
            server,
            "POST",
            _contract("POST", path),
            body=body,
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )

    status, resolved, _ = post(
        "/api/runtime-surface/profile-change/resolve",
        {
            "profile_id": selected["profile_id"],
            "expected_profile_revision": active["profile_revision"],
            "expected_plan_digest": active["plan_digest"],
            "desired_pack_ids": desired,
            "profile_definition_digest": selected["definition"]["digest"],
            "profile_catalog_digest": catalog["catalog_digest"],
            "bundle_lock_digest": catalog["bundle_lock_digest"],
        },
    )
    assert status == 200, resolved
    assert resolved["data"]["state"] == "resolved", resolved
    status, reviewed, _ = post(
        "/api/runtime-surface/profile-change/review",
        {
            "candidate_id": resolved["data"]["candidate_id"],
            "candidate_digest": resolved["data"]["candidate_digest"],
        },
    )
    assert status == 200, reviewed
    status, approved, _ = post(
        "/api/runtime-surface/profile-change/approve",
        {
            "candidate_id": reviewed["data"]["candidate_id"],
            "candidate_digest": reviewed["data"]["candidate_digest"],
        },
    )
    assert status == 200, approved
    approval = approved["data"]["authority_approval"]
    assert approval["decision"] == "approved"
    assert authority.get_approval(approval["approval_id"]) is not None
    handler = server.handler_class
    assert handler is not None
    refresh = handler._runtime_refresh
    assert refresh is not None
    monkeypatch.setattr(handler, "_runtime_refresh", staticmethod(lambda _session: None))
    status, activated, _ = post(
        "/api/runtime-surface/profile-change/activate",
        {
            "approval_id": approval["approval_id"],
            "approval_digest": approved["data"]["approval_digest"],
        },
    )
    assert status == 200, activated
    assert activated["data"]["state"] == "active"
    contract_path = Path(os.environ["TOBKIRI_HOST_CONTRACT_PATH"])
    contract_path.write_text(
        json.dumps(
            host_contract(
                profile_id=str(activated["data"]["profile_id"]),
                profile_revision=str(
                    resolved["data"]["review"]["resolved_plan"]["profile_revision"]
                ),
                activation_id=str(activated["data"]["activation_id"]),
                plan_digest=str(activated["data"]["plan_digest"]),
                values={"panel_bootstrap_secret": "desktop-bootstrap"},
            )
        ),
        encoding="utf-8",
    )
    contract_path.chmod(0o600)
    refresh(None)
    cookie, _csrf, _origin = _authenticate(server)

    status, refreshed_profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, refreshed_profile
    refreshed = refreshed_profile["data"]
    refreshed_identity = (
        refreshed["profile_id"],
        refreshed["profile_revision"],
        refreshed["data"]["activation_record"]["activation_id"],
        refreshed["plan_digest"],
    )
    assert refreshed_identity == (
        activated["data"]["profile_id"],
        refreshed["profile_revision"],
        activated["data"]["activation_id"],
        activated["data"]["plan_digest"],
    )

    # A process restart must bind the registry to the Authority path, even if
    # an ambient environment override points at a different Host root.
    restart_active = profile_capture.capture_active_profile()
    authority_path = authority.path.resolve()
    authority_user_data = authority_path.parent.parent
    wrong_user_data = tmp_path / "wrong-user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(wrong_user_data))
    restart_authority = AuthorityStore(authority_path)
    restart_bindings = tuple(server._contract_routes.values())
    restarted_session = capture_production_dispatch(
        restart_active,
        bundle_root=packaged_profile_bundle_root(),
        ecosystem_root=RUNTIME_ROOT / "ecosystem",
        authority_store=restart_authority,
        http_contract_bindings=restart_bindings,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        capability_binding_snapshot_factory=defaultspack_capability_snapshot_mapping,
        capability_binding_selector=defaultspack_capability_binding,
    )
    assert (
        restarted_session.profile_id,
        restart_active.resolved.plan["profile_revision"],
        restarted_session.plan_digest,
    ) == (
        refreshed_identity[0],
        refreshed_identity[1],
        refreshed_identity[3],
    )
    restarted_session.close()

    # Re-open the HTTP boundary with the freshly captured session and verify
    # that the same identity is exposed after restart.
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(authority_user_data))
    server.stop()
    restarted_authority = AuthorityStore(authority_path)
    restarted_session = capture_production_dispatch(
        restart_active,
        bundle_root=packaged_profile_bundle_root(),
        ecosystem_root=RUNTIME_ROOT / "ecosystem",
        authority_store=restarted_authority,
        http_contract_bindings=restart_bindings,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        capability_binding_snapshot_factory=defaultspack_capability_snapshot_mapping,
        capability_binding_selector=defaultspack_capability_binding,
    )
    restarted_server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        dispatch_session=restarted_session,
        contract_bindings=restart_bindings,
        runtime_capture_factory=defaultspack_runtime_capture_inputs,
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(),
    )
    try:
        restarted_server.start()
        restart_cookie, _restart_csrf, _restart_origin = _authenticate(restarted_server)
        status, restarted_profile, _ = _request(
            restarted_server,
            "GET",
            _contract("GET", "/api/runtime-surface/profile"),
            headers={
                "Cookie": restart_cookie,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 200, restarted_profile
        restarted = restarted_profile["data"]
        assert (
            restarted["profile_id"],
            restarted["profile_revision"],
            restarted["data"]["activation_record"]["activation_id"],
            restarted["plan_digest"],
        ) == refreshed_identity
    finally:
        restarted_server.stop()
        restarted_session.close()


def test_runtime_surface_operation_identity_invokes_exact_capability_binding(
    production_server,
) -> None:
    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    status, payload, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/topology/operations"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, payload
    envelope = payload["data"]
    status_operations = [
        item for item in envelope["data"]["operations"] if item["operation_id"] == "pack.status"
    ]
    assert any(item["invokable"] is True for item in status_operations), json.dumps(
        status_operations, indent=2
    )
    operation = next(item for item in status_operations if item["invokable"] is True)
    base = {
        "request_id": str(uuid.uuid4()),
        "expires_at": time.time() + 30,
        "profile_id": envelope["profile_id"],
        "profile_revision": envelope["profile_revision"],
        "activation_id": operation["activation_id"],
        "plan_hash": envelope["plan_digest"],
        "catalog_hash": operation["invocation_catalog_hash"],
        "contribution_id": operation["invocation_contribution_id"],
        "owner_pack_id": operation["invocation_owner_pack_id"],
        "contract_id": operation["contract_id"],
        "payload": {"pack_id": "defaultspack"},
    }
    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }

    def invoke(body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
        code, response, _ = _request(
            server,
            "POST",
            _contract("POST", "/api/ui/capability/invoke"),
            body=body,
            headers={**headers, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        return code, response

    code, response = invoke(base)
    assert code == 200, response
    assert response["data"]["pack_id"] == "defaultspack"

    denied_requests = (
        {**base, "request_id": str(uuid.uuid4()), "catalog_hash": "sha256:" + "0" * 64},
        {**base, "request_id": str(uuid.uuid4()), "contribution_id": "pack.forged.operation"},
        {**base, "request_id": str(uuid.uuid4()), "expires_at": time.time() - 1},
        {**base, "request_id": str(uuid.uuid4()), "owner_pack_id": "forged-pack"},
    )
    for denied in denied_requests:
        denied_code, denied_response = invoke(denied)
        assert denied_code == 404
        assert denied_response["success"] is False


def test_profile_ceremony_uses_four_canonical_broker_operations(
    production_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }

    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, profile
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]

    def post(
        target: str,
        body: Mapping[str, object],
        *,
        request_id: str | None = None,
    ):
        return _request(
            server,
            "POST",
            _contract("POST", target),
            body=body,
            headers={
                **headers,
                "X-Tobkiri-Request-ID": request_id or str(uuid.uuid4()),
            },
        )

    status, resolved, _ = post(
        "/api/runtime-surface/profile-change/resolve",
        {
            "profile_id": "defaults",
            "expected_profile_revision": envelope["profile_revision"],
            "expected_plan_digest": envelope["plan_digest"],
            "desired_pack_ids": desired,
        },
    )
    assert status == 200, resolved
    status, reviewed, _ = post(
        "/api/runtime-surface/profile-change/review",
        {
            "candidate_id": resolved["data"]["candidate_id"],
            "candidate_digest": resolved["data"]["candidate_digest"],
        },
    )
    assert status == 200, reviewed
    status, approved, _ = post(
        "/api/runtime-surface/profile-change/approve",
        {
            "candidate_id": reviewed["data"]["candidate_id"],
            "candidate_digest": reviewed["data"]["candidate_digest"],
        },
    )
    assert status == 200, approved
    receipt = approved["data"]["authority_approval"]
    assert approved["data"]["approval_id"] == receipt["approval_id"]
    assert authority.get_approval(receipt["approval_id"]) is not None
    approval_audit = next(
        event
        for event in reversed(authority.audit_events())
        if event["event_type"] == "authority_records_committed"
    )
    assert approval_audit["payload"]["records"] == [
        {
            "record_type": "approval",
            "record_id": approved["data"]["approval_id"],
            "record_digest": approved["data"]["approval_digest"],
        }
    ]
    read_worker_capture_loads: list[int] = []
    original_load = ActivationStore.load_active_snapshot

    def counted_load(store):
        if threading.current_thread().name.startswith("tobkiri-runtime-read"):
            read_worker_capture_loads.append(threading.get_ident())
        return original_load(store)

    monkeypatch.setattr(
        ActivationStore,
        "load_active_snapshot",
        counted_load,
    )
    assert server.handler_class is not None
    monkeypatch.setattr(
        server.handler_class,
        "_runtime_refresh",
        staticmethod(lambda _session: None),
    )
    activation_request_id = str(uuid.uuid4())
    activation_body = {
        "approval_id": approved["data"]["approval_id"],
        "approval_digest": approved["data"]["approval_digest"],
    }
    status, activated, _ = post(
        "/api/runtime-surface/profile-change/activate",
        activation_body,
        request_id=activation_request_id,
    )
    assert status == 200, activated
    assert activated["data"]["state"] == "active"
    assert activated["data"]["authoritative_snapshot"]["state"] == "ready"
    # The session's direct active loader still performs its independent
    # authority check.  No additional capture_default_profile store read is
    # allowed in the worker after the mutation recapture populated the scope.
    assert read_worker_capture_loads == []
    journal = server._operation_journal
    assert journal is not None
    replay_mutating_calls: list[str] = []

    def unexpected_replay_renew(*_args, **_kwargs) -> None:
        replay_mutating_calls.append("renew_session")

    def unexpected_replay_begin(*_args, **_kwargs):
        replay_mutating_calls.append("begin_operation")
        return {}, False

    monkeypatch.setattr(journal, "renew_session", unexpected_replay_renew)
    monkeypatch.setattr(journal, "begin_operation", unexpected_replay_begin)
    status, replayed, _ = post(
        "/api/runtime-surface/profile-change/activate",
        activation_body,
        request_id=activation_request_id,
    )
    assert status == 401, replayed
    assert replayed["error"] == "Unauthorized"
    assert replay_mutating_calls == []


def test_mutation_status_reconciles_lost_response_and_exact_approval_retry(
    production_server,
) -> None:
    server, _session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
    }
    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, profile
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]

    def post(target: str, body: Mapping[str, object], request_id: str):
        return _request(
            server,
            "POST",
            _contract("POST", target),
            body=body,
            headers={**headers, "X-Tobkiri-Request-ID": request_id},
        )

    status, resolved, _ = post(
        "/api/runtime-surface/profile-change/resolve",
        {
            "profile_id": "defaults",
            "expected_profile_revision": envelope["profile_revision"],
            "expected_plan_digest": envelope["plan_digest"],
            "desired_pack_ids": desired,
        },
        str(uuid.uuid4()),
    )
    assert status == 200, resolved
    status, reviewed, _ = post(
        "/api/runtime-surface/profile-change/review",
        {
            "candidate_id": resolved["data"]["candidate_id"],
            "candidate_digest": resolved["data"]["candidate_digest"],
        },
        str(uuid.uuid4()),
    )
    assert status == 200, reviewed
    approve_body = {
        "candidate_id": reviewed["data"]["candidate_id"],
        "candidate_digest": reviewed["data"]["candidate_digest"],
    }
    request_id = str(uuid.uuid4())
    lost_response = http.client.HTTPConnection(
        "127.0.0.1",
        server.port,
        timeout=FRONTEND_MUTATION_TIMEOUT_SECONDS,
    )
    lost_response.request(
        "POST",
        _contract("POST", "/api/runtime-surface/profile-change/approve"),
        body=json.dumps(approve_body).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json",
            "X-Tobkiri-Request-ID": request_id,
        },
    )
    lost_response.close()

    status_path = _contract("GET", "/api/runtime-surface/operation-status")
    deadline = time.monotonic() + EVENTUAL_RECONCILIATION_TIMEOUT_SECONDS
    while True:
        status, reconciled, _ = _request(
            server,
            "GET",
            f"{status_path}?request_id={request_id}",
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        if status == 200:
            reconciliation_state = reconciled["data"]["state"]
            assert reconciliation_state in {"pending", "succeeded"}, reconciled
            if reconciliation_state == "succeeded":
                break
        else:
            assert status == 409, reconciled
        assert time.monotonic() < deadline, reconciled
        time.sleep(0.02)
    assert status == 200, reconciled
    assert reconciled["data"]["state"] == "succeeded"
    assert reconciled["data"]["request_id"] == request_id
    assert reconciled["data"]["result_digest"].startswith("sha256:")
    approved = {"data": reconciled["data"]["result"]}
    assert reconciled["data"]["record_refs"] == [
        {
            "kind": "approval",
            "id": approved["data"]["approval_id"],
            "digest": approved["data"]["approval_digest"],
        }
    ]

    server.stop()
    server.start()
    status, after_restart, _ = _request(
        server,
        "GET",
        f"{status_path}?request_id={request_id}",
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, after_restart
    assert after_restart["data"] == reconciled["data"]

    other_cookie, _other_csrf, _other_origin = _authenticate(server)
    status, cross_session, _ = _request(
        server,
        "GET",
        f"{status_path}?request_id={request_id}",
        headers={
            "Cookie": other_cookie,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert status == 409, cross_session

    status, same_request, _ = post(
        "/api/runtime-surface/profile-change/approve",
        approve_body,
        request_id,
    )
    assert status == 200, same_request
    assert same_request["data"] == approved["data"]
    status, different_request, _ = post(
        "/api/runtime-surface/profile-change/approve",
        approve_body,
        str(uuid.uuid4()),
    )
    assert status == 200, different_request
    assert different_request["data"]["approval_id"] == approved["data"]["approval_id"]
    assert different_request["data"]["approval_digest"] == approved["data"]["approval_digest"]
    assert different_request["data"]["authority_approval"] == approved["data"]["authority_approval"]

    commits = [
        event
        for event in authority.audit_events()
        if event["event_type"] == "authority_records_committed"
        and any(
            item.get("record_id") == approved["data"]["approval_id"]
            for item in event["payload"].get("records", [])
        )
    ]
    assert len(commits) == 1

    for unknown_id in (
        "00000000-0000-4000-8000-000000000000",
        request_id + "-tampered",
    ):
        status, rejected, _ = _request(
            server,
            "GET",
            f"{status_path}?request_id={unknown_id}",
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )
        assert status == 409, rejected


def test_contract_replay_unknown_and_stale_capture_fail_closed(
    production_server,
) -> None:
    server, session, authority = production_server
    cookie, _csrf, _origin = _authenticate(server)
    request_id = str(uuid.uuid4())
    path = _contract("GET", "/api/home/dashboard")
    headers = {"Cookie": cookie, "X-Tobkiri-Request-ID": request_id}
    first = _request(server, "GET", path, headers=headers)
    assert first[0] == 200, first[1]
    audit_after_first = len(authority.audit_events())
    assert _request(server, "GET", path, headers=headers)[0] == 409
    assert len(authority.audit_events()) == audit_after_first

    traversal = "/api/contracts/defaultspack/GET%20%2Fapi%2F..%2Fsecrets"
    assert (
        _request(
            server,
            "GET",
            traversal,
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )[0]
        == 400
    )
    assert len(authority.audit_events()) == audit_after_first

    assert (
        _request(
            server,
            "GET",
            "/api/ui/catalog",
            headers={"Cookie": cookie},
        )[0]
        == 404
    )
    assert len(authority.audit_events()) == audit_after_first

    unknown = _contract("GET", "/api/pack-control/not-selected")
    assert (
        _request(
            server,
            "GET",
            unknown,
            headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
        )[0]
        == 404
    )
    assert len(authority.audit_events()) == audit_after_first

    authority.advance_security_epoch("test stale frontend capture")
    stale_server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="other"),
        dispatch_session=session,
        contract_bindings=tuple(server._contract_routes.values()),
    )
    with pytest.raises(Exception, match="stale|epoch"):
        stale_server.start()
    assert stale_server.server is None


def test_stale_fresh_mutation_has_no_journal_admission_side_effects(
    production_server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _session, authority = production_server
    cookie, csrf, origin = _authenticate(server)
    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, profile
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]
    journal = server._operation_journal
    assert journal is not None
    assert not journal.path.exists()
    mutating_calls: list[str] = []
    lookup_calls: list[str] = []
    original_lookup = journal.lookup_operation

    def counted_lookup(**kwargs):
        lookup_calls.append(str(kwargs["request_id"]))
        return original_lookup(**kwargs)

    def unexpected_renew(*_args, **_kwargs) -> None:
        mutating_calls.append("renew_session")

    def unexpected_begin(*_args, **_kwargs):
        mutating_calls.append("begin_operation")
        return {}, False

    monkeypatch.setattr(journal, "lookup_operation", counted_lookup)
    monkeypatch.setattr(journal, "renew_session", unexpected_renew)
    monkeypatch.setattr(journal, "begin_operation", unexpected_begin)
    authority.advance_security_epoch("reject stale fresh mutation")

    status, rejected, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/runtime-surface/profile-change/resolve"),
        body={
            "profile_id": "defaults",
            "expected_profile_revision": envelope["profile_revision"],
            "expected_plan_digest": envelope["plan_digest"],
            "desired_pack_ids": desired,
        },
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )

    assert status == 401, rejected
    assert rejected["error"] == "Unauthorized"
    assert lookup_calls == []
    assert mutating_calls == []
    assert not journal.path.exists()


def test_replayed_mutation_without_record_is_filesystem_immutable(
    production_server,
) -> None:
    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    request_id = str(uuid.uuid4())
    journal = server._operation_journal.path
    assert not journal.exists()
    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": request_id},
    )
    assert status == 200
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]

    status, rejected, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/runtime-surface/profile-change/resolve"),
        body={
            "profile_id": "defaults",
            "expected_profile_revision": envelope["profile_revision"],
            "expected_plan_digest": envelope["plan_digest"],
            "desired_pack_ids": desired,
        },
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
            "X-Tobkiri-Request-ID": request_id,
        },
    )

    assert status == 409, rejected
    assert not journal.exists()


def test_corrupt_reconciliation_journal_maps_to_typed_503_without_detail(
    production_server,
) -> None:
    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    journal = server._operation_journal.path
    journal.parent.mkdir(parents=True)
    journal.write_bytes(b"not a sqlite database")
    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]

    status, rejected, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/runtime-surface/profile-change/resolve"),
        body={
            "profile_id": "defaults",
            "expected_profile_revision": envelope["profile_revision"],
            "expected_plan_digest": envelope["plan_digest"],
            "desired_pack_ids": desired,
        },
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )

    assert status == 503
    assert rejected["data"]["code"] == "operation_reconciliation_unavailable"
    assert rejected["error"] == "Control operation reconciliation is unavailable"
    assert "sqlite" not in json.dumps(rejected).lower()


def test_reconciliation_binding_conflict_maps_to_typed_409_without_detail(
    production_server,
) -> None:
    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    status, profile, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profile"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200
    envelope = profile["data"]
    desired = [
        item["pack_id"]
        for item in envelope["data"]["profile_document"]["packs"]
        if item.get("role") != "application"
    ]
    assert len(desired) > 1
    request_id = str(uuid.uuid4())
    headers = {
        "Cookie": cookie,
        "Origin": origin,
        "X-Rumi-CSRF": csrf,
        "X-Tobkiri-Request-ID": request_id,
    }
    path = _contract("POST", "/api/runtime-surface/profile-change/resolve")
    body = {
        "profile_id": "defaults",
        "expected_profile_revision": envelope["profile_revision"],
        "expected_plan_digest": envelope["plan_digest"],
        "desired_pack_ids": desired,
    }
    assert _request(server, "POST", path, body=body, headers=headers)[0] == 200
    tampered = {**body, "desired_pack_ids": list(reversed(desired))}

    status, rejected, _ = _request(
        server,
        "POST",
        path,
        body=tampered,
        headers=headers,
    )

    assert status == 409
    assert rejected["data"]["code"] == "operation_reconciliation_mismatch"
    assert rejected["error"] == "Control operation conflicts with durable state"
    assert "digest" not in json.dumps(rejected).lower()


def test_contract_server_rejects_missing_or_wrong_capture_before_bind(
    production_server,
) -> None:
    server, session, _authority = production_server
    bindings = tuple(server._contract_routes.values())
    missing = PackAPIServer(port=0, contract_bindings=bindings)
    with pytest.raises(RuntimeError, match="captured v4 session"):
        missing.start()
    assert missing.server is None

    route = bindings[0]
    target = route.targets[0]
    wrong_target = FrontendContractTarget(
        contribution_id=target.contribution_id,
        contract_id=target.contract_id,
        operation_id=target.operation_id,
        provider_id="unselected.provider",
        function_id="unselected.provider",
        allowed_payload_keys=target.allowed_payload_keys,
    )
    wrong_binding = FrontendContractBinding(
        method=route.method,
        path=route.path,
        presentation=route.presentation,
        targets=(wrong_target,),
    )
    wrong = PackAPIServer(
        port=0,
        dispatch_session=session,
        contract_bindings=(wrong_binding,),
    )
    with pytest.raises(RuntimeError, match="Provider identity"):
        wrong.start()
    assert wrong.server is None


def test_contract_server_rejects_empty_and_wrong_backend_registry_before_bind(
    production_server,
) -> None:
    server, session, _authority = production_server
    bindings = tuple(server._contract_routes.values())
    selected_backends = session.broker._backends

    session.broker._backends = BackendRegistry(())
    empty = PackAPIServer(
        port=0,
        dispatch_session=session,
        contract_bindings=bindings,
    )
    with pytest.raises(BackendUnavailableError, match="not installed"):
        empty.start()
    assert empty.server is None

    original_statuses = [
        (backend, backend.status) for backend in selected_backends.registered
    ]
    try:
        for backend, original_status in original_statuses:
            backend.status = type(original_status)(
                backend_id=original_status.backend_id,
                execution_kind=original_status.execution_kind,
                platform=original_status.platform,
                backend_digest="sha256:" + "0" * 64,
                production_enabled=True,
                conformance_only=False,
                satisfied_gates=original_status.satisfied_gates,
            )
        session.broker._backends = BackendRegistry(
            backend for backend, _status in original_statuses
        )
        wrong = PackAPIServer(
            port=0,
            dispatch_session=session,
            contract_bindings=bindings,
        )
        with pytest.raises(RuntimeError, match="metadata is stale or wrong"):
            wrong.start()
        assert wrong.server is None
    finally:
        for backend, original_status in original_statuses:
            backend.status = original_status
        session.broker._backends = selected_backends

    exact = PackAPIServer(
        port=0,
        dispatch_session=session,
        contract_bindings=bindings,
    )
    with pytest.raises(
        BackendUnavailableError,
        match="authenticated PackVM supervisor",
    ):
        exact._validate_contract_runtime()
    assert exact.server is None


def test_selected_desktop_entrypoint_has_no_compatibility_server_authority() -> None:
    desktop = (
        RUNTIME_ROOT / "ecosystem" / "defaultspack" / "defaultspack" / "desktop_app.py"
    ).read_text(encoding="utf-8")
    assert "DefaultsHttpServer" not in desktop
    assert "transport.http" not in desktop
    assert "build_fallback_http_routes" not in desktop
