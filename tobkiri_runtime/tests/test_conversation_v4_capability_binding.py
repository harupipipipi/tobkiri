"""HTTP proof for the static Pack v4 conversation capability binding."""

from __future__ import annotations

import hashlib
import http.client
import json
import time
import uuid
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

import pytest

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding as FrontendContractBinding,
    HTTPContractTarget as FrontendContractTarget,
)
from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
    load_frontend_contract_bindings,
)
from ecosystem.defaultspack.defaultspack.http_contract_composition import (
    defaultspack_capability_snapshot,
)
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from tests.conformance_support.host_contract import host_contract_for_session


pytestmark = pytest.mark.contract


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = (
    RUNTIME_ROOT
    / "ecosystem"
    / "defaultspack"
    / "defaultspack"
    / "frontend_contract_map.v4.json"
)
_CONVERSATION_ID = "defaults.conversation.complete"
_CONVERSATION_CONTRACT = "conversation.turn.v1"
_CONVERSATION_OPERATION = "complete"


def _contract(method: str, target: str) -> str:
    return "/api/contracts/defaultspack/" + quote(f"{method.upper()} {target}", safe="")


def _request(
    server: PackAPIServer,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object], list[tuple[str, str]]]:
    """Send one real loopback request to the finite Pack v4 boundary."""

    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
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
    """Create one authenticated panel session for capability requests."""

    origin = f"http://127.0.0.1:{server.port}"
    status, bootstrap, _ = _request(
        server,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": "conversation-test-bootstrap"},
    )
    assert status == 200, bootstrap
    status, exchange, headers = _request(
        server,
        "POST",
        "/api/panel/auth/exchange",
        body={"code": bootstrap["data"]["code"]},
        headers={"Origin": origin},
    )
    assert status == 200, exchange
    cookie = next(value for key, value in headers if key.lower() == "set-cookie")
    return cookie.split(";", 1)[0], str(exchange["data"]["csrf_token"]), origin


def _load_conversation_target() -> FrontendContractTarget:
    """Load the committed target through the map's digest-checked parser."""

    raw = MAP_PATH.read_bytes()
    bindings = load_frontend_contract_bindings(
        MAP_PATH,
        {
            "pack": {
                "id": "runtime.tauri.application.default",
                "kind": "application",
            },
            "artifacts": [
                {
                    "path": "defaultspack/frontend_contract_map.v4.json",
                    "kind": "asset",
                    "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                }
            ]
        },
    )
    binding = next(
        item
        for item in bindings
        if item.method == "POST" and item.path == "/api/ui/capability/invoke"
    )
    target = next(item for item in binding.targets if item.contribution_id == _CONVERSATION_ID)
    assert target.contract_id == _CONVERSATION_CONTRACT
    assert target.operation_id == _CONVERSATION_OPERATION
    assert target.provider_id == target.function_id == "defaultspack.conversation"
    assert target.allowed_payload_keys == frozenset({"messages"})
    return target


class _CapturedConversationSession:
    """Finite captured Broker fixture with controllable readiness evidence."""

    profile_id = "defaults"
    profile_revision = "sha256:" + "3" * 64
    plan_digest = "sha256:" + "1" * 64
    activation_id = "activation:conversation-test"
    security_epoch = 1

    def __init__(self, targets: tuple[FrontendContractTarget, ...]) -> None:
        self._providers: dict[str, tuple[Mapping[str, object], ...]] = {}
        self.ready_operations = {
            (target.contract_id, target.operation_id) for target in targets
        }
        self.broker_invocations: list[tuple[str, str, dict[str, object]]] = []
        for target in targets:
            self._providers[target.contract_id] = (
                {
                    "provider_id": target.provider_id,
                    "function_id": target.function_id,
                    "operation_id": target.operation_id,
                    "profile_id": self.profile_id,
                    "profile_revision": self.profile_revision,
                    "activation_id": self.activation_id,
                    "plan_digest": self.plan_digest,
                    "artifact_digest": "sha256:" + "2" * 64,
                },
            )

    def assert_current(self) -> None:
        """Model an unchanged capture for the request lifetime."""

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, object], ...]:
        """Return only the exact captured Provider identity."""

        return self._providers.get(contract_id, ())

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None:
        """Require the exact Provider to remain ready in this capture."""

        if (contract_id, operation_id) not in self.ready_operations:
            raise RuntimeError("captured Provider is unavailable")

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, object],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, object]:
        """Capture the exact Broker dispatch emitted by PackAPI."""

        del version_range
        self.broker_invocations.append((contract_id, operation_id, dict(payload)))
        if contract_id == _CONVERSATION_CONTRACT:
            return {
                "content": [{"type": "text", "text": "accepted"}],
                "tool_calls": [],
            }
        return {"profile_id": self.profile_id}


def test_conversation_capability_is_capture_gated_and_http_brokered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a verified capture can expose and invoke the Conversation route."""

    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    conversation = _load_conversation_target()
    catalog = FrontendContractTarget(
        contribution_id="defaults.pack.catalog",
        contract_id="tobkiri.host.pack-control.v4",
        operation_id="catalog.read",
        provider_id="tobkiri.host.pack-control",
        function_id="tobkiri.host.pack-control",
    )
    capability_binding = FrontendContractBinding(
        method="POST",
        path="/api/ui/capability/invoke",
        presentation="capability_result",
        targets=(conversation, catalog),
        application_id="runtime.tauri.application.default",
        route_namespace="defaultspack",
        profile_id=_CapturedConversationSession.profile_id,
        profile_revision=_CapturedConversationSession.profile_revision,
        activation_id=_CapturedConversationSession.activation_id,
        plan_digest=_CapturedConversationSession.plan_digest,
    )
    catalog_binding = FrontendContractBinding(
        method="GET",
        path="/api/ui/catalog",
        presentation="dynamic_pack_catalog",
        targets=(catalog,),
        application_id="runtime.tauri.application.default",
        route_namespace="defaultspack",
        profile_id=_CapturedConversationSession.profile_id,
        profile_revision=_CapturedConversationSession.profile_revision,
        activation_id=_CapturedConversationSession.activation_id,
        plan_digest=_CapturedConversationSession.plan_digest,
    )
    session = _CapturedConversationSession((conversation, catalog))
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(
            bootstrap_secret="conversation-test-bootstrap"
        ),
        dispatch_session=session,
        contract_bindings=(capability_binding, catalog_binding),
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(),
        host_contract=host_contract_for_session(session),
    )
    server.start()
    try:
        server.handler_class._capability_catalog_cache = {"packs": []}  # noqa: SLF001
        cookie, csrf, origin = _authenticate(server)
        read_headers = {
            "Cookie": cookie,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        }
        status, catalog_response, _ = _request(
            server,
            "GET",
            _contract("GET", "/api/ui/catalog"),
            headers=read_headers,
        )
        assert status == 200, catalog_response
        host = catalog_response["data"]["dynamic_host"]
        assert host["profile_revision"] == session.profile_revision
        assert host["profile_revision"] != host["plan_hash"]
        assert host["activation_id"] == session.activation_id
        contribution = next(
            item
            for item in host["contributions"]
            if item["contribution_id"] == _CONVERSATION_ID
        )
        assert contribution["kind"] == "route"
        assert contribution["mode"] == "declarative"
        assert contribution["route"] == "/chat"
        assert contribution["action_contract"] == _CONVERSATION_CONTRACT
        assert contribution["resolved_profile_id"] == session.profile_id
        assert contribution["resolved_profile_revision"] == session.profile_revision
        assert contribution["resolved_activation_id"] == session.activation_id
        assert contribution["resolved_plan_hash"] == session.plan_digest
        assert contribution["view"]["type"] == "conversation_v4"
        assert contribution["view"]["title"] == "Tobkiri Conversation"
        assert contribution["view"]["body"]

        capability_request = {
            "request_id": str(uuid.uuid4()),
            "expires_at": time.time() + 45,
            "profile_id": host["profile_id"],
            "profile_revision": host["profile_revision"],
            "activation_id": host["activation_id"],
            "plan_hash": host["plan_hash"],
            "catalog_hash": host["catalog_hash"],
            "contribution_id": contribution["contribution_id"],
            "owner_pack_id": contribution["owner_pack_id"],
            "contract_id": contribution["action_contract"],
            "payload": {"messages": [{"role": "user", "content": "hello"}]},
        }
        route = _contract("POST", "/api/ui/capability/invoke")
        mutation_headers = {
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
        }
        before = len(session.broker_invocations)
        unauthorized, _, _ = _request(
            server,
            "POST",
            route,
            body={**capability_request, "request_id": str(uuid.uuid4())},
        )
        assert unauthorized == 401
        assert len(session.broker_invocations) == before

        missing_identity, missing_identity_response, _ = _request(
            server,
            "POST",
            route,
            body={**capability_request, "request_id": str(uuid.uuid4())},
            headers=mutation_headers,
        )
        assert missing_identity == 409, missing_identity_response
        assert missing_identity_response["data"]["code"] == "invalid_request_identity"
        assert len(session.broker_invocations) == before

        rejected_payload, rejected_response, _ = _request(
            server,
            "POST",
            route,
            body={
                **capability_request,
                "request_id": str(uuid.uuid4()),
                "payload": {
                    "messages": [{"role": "user", "content": "hello"}],
                    "model": "not-exposed-by-this-surface",
                },
            },
            headers={
                **mutation_headers,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert rejected_payload == 400, rejected_response
        assert rejected_response["data"]["code"] == "invalid_contract_payload"
        assert len(session.broker_invocations) == before

        invoked, invoked_response, _ = _request(
            server,
            "POST",
            route,
            body={**capability_request, "request_id": str(uuid.uuid4())},
            headers={
                **mutation_headers,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert invoked == 200, invoked_response
        assert invoked_response["data"]["content"][0]["text"] == "accepted"
        contract_id, operation_id, payload = session.broker_invocations[-1]
        assert (contract_id, operation_id) == (
            _CONVERSATION_CONTRACT,
            _CONVERSATION_OPERATION,
        )
        assert payload["messages"] == [{"role": "user", "content": "hello"}]
        assert set(payload) == {"messages", "_session_id"}

        rotated_activation, rotated_response, _ = _request(
            server,
            "POST",
            route,
            body={
                **capability_request,
                "request_id": str(uuid.uuid4()),
                "activation_id": "activation:rotated",
            },
            headers={
                **mutation_headers,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert rotated_activation == 404, rotated_response
        assert len(session.broker_invocations) == before + 1

        missing_revision = dict(capability_request)
        del missing_revision["profile_revision"]
        missing_revision_status, missing_revision_response, _ = _request(
            server,
            "POST",
            route,
            body={**missing_revision, "request_id": str(uuid.uuid4())},
            headers={
                **mutation_headers,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert missing_revision_status == 404, missing_revision_response
        assert len(session.broker_invocations) == before + 1

        before_legacy = len(session.broker_invocations)
        legacy_status, _, _ = _request(
            server,
            "POST",
            "/api/conversation/complete",
            body={"messages": [{"role": "user", "content": "legacy"}]},
        )
        assert legacy_status == 404
        assert len(session.broker_invocations) == before_legacy

        session.ready_operations.remove((_CONVERSATION_CONTRACT, _CONVERSATION_OPERATION))
        status, unready_catalog, _ = _request(
            server,
            "GET",
            _contract("GET", "/api/ui/catalog"),
            headers={
                "Cookie": cookie,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 200, unready_catalog
        assert all(
            item["contribution_id"] != _CONVERSATION_ID
            for item in unready_catalog["data"]["dynamic_host"]["contributions"]
        )
    finally:
        server.stop()


def test_unready_conversation_is_omitted_without_blocking_packapi_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PackAPI starts fail-closed before the optional Provider is ready."""

    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    conversation = _load_conversation_target()
    catalog = FrontendContractTarget(
        contribution_id="defaults.pack.catalog",
        contract_id="tobkiri.host.pack-control.v4",
        operation_id="catalog.read",
        provider_id="tobkiri.host.pack-control",
        function_id="tobkiri.host.pack-control",
    )
    session = _CapturedConversationSession((conversation, catalog))
    session.ready_operations.remove(
        (_CONVERSATION_CONTRACT, _CONVERSATION_OPERATION)
    )
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(
            bootstrap_secret="conversation-test-bootstrap"
        ),
        dispatch_session=session,
        contract_bindings=(
            FrontendContractBinding(
                method="POST",
                path="/api/ui/capability/invoke",
                presentation="capability_result",
                targets=(conversation, catalog),
                application_id="runtime.tauri.application.default",
                route_namespace="defaultspack",
                profile_id=_CapturedConversationSession.profile_id,
                profile_revision=_CapturedConversationSession.profile_revision,
                activation_id=_CapturedConversationSession.activation_id,
                plan_digest=_CapturedConversationSession.plan_digest,
            ),
            FrontendContractBinding(
                method="GET",
                path="/api/ui/catalog",
                presentation="dynamic_pack_catalog",
                targets=(catalog,),
                application_id="runtime.tauri.application.default",
                route_namespace="defaultspack",
                profile_id=_CapturedConversationSession.profile_id,
                profile_revision=_CapturedConversationSession.profile_revision,
                activation_id=_CapturedConversationSession.activation_id,
                plan_digest=_CapturedConversationSession.plan_digest,
            ),
        ),
        capability_snapshot_factory=defaultspack_capability_snapshot,
        application_presentation=DefaultspackHTTPPresentation(),
        host_contract=host_contract_for_session(session),
    )

    server.start()
    try:
        server.handler_class._capability_catalog_cache = {"packs": []}  # noqa: SLF001
        cookie, _, _ = _authenticate(server)
        status, response, _ = _request(
            server,
            "GET",
            _contract("GET", "/api/ui/catalog"),
            headers={
                "Cookie": cookie,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert status == 200, response
        assert all(
            item["contribution_id"] != _CONVERSATION_ID
            for item in response["data"]["dynamic_host"]["contributions"]
        )
    finally:
        server.stop()
