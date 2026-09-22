"""Real-server proof for the production frontend-to-Broker contract path."""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Mapping
from urllib.parse import quote

import pytest

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.frontend_contract_routes import load_frontend_contract_bindings
from core_runtime.frontend_contract_routes import (
    FrontendContractBinding,
    FrontendContractTarget,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.errors import BackendUnavailableError


pytestmark = pytest.mark.contract


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_MUTATION_TIMEOUT_SECONDS = 10
EVENTUAL_RECONCILIATION_TIMEOUT_SECONDS = 30
BUNDLE_ROOT = RUNTIME_ROOT / "ecosystem" / "defaultspack" / "v4"
MAP_PATH = (
    RUNTIME_ROOT / "ecosystem" / "defaultspack" / "defaultspack" / "frontend_contract_map.v4.json"
)


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


@pytest.fixture
def production_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    authority = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    bundle_root = packaged_profile_bundle_root()
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
        frontend_contract_bindings=bindings,
    )
    server = PackAPIServer(
        port=0,
        panel_auth_manager=PanelAuthManager(bootstrap_secret="desktop-bootstrap"),
        dispatch_session=session,
        contract_bindings=bindings,
    )
    server.start()
    try:
        yield server, session, authority
    finally:
        server.stop()
        session.broker.close()
        authority.close()


def test_home_and_pack_workflow_use_only_real_broker_contracts(
    production_server,
) -> None:
    server, _session, authority = production_server
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
    status_contribution = next(
        item for item in dynamic_host["contributions"] if item["label"] == "pack.status"
    )

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

    target_pack = "rumi_git_read_pack"
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
    assert post("/api/pack-control/restart", {})[0] == 200
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
    revoke_status, revoked = post("/api/pack-control/approval-revoke", {"pack_id": target_pack})
    assert revoke_status == 200, revoked
    assert revoked["data"]["approved"] is False
    assert revoked["data"]["approval_status"] == "revoked"
    assert post("/api/pack-control/restart", {})[0] == 200
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
            elif message.startswith("API:"):
                assert '"POST /api/contracts/defaultspack/' in message
                assert 'HTTP/1.1" 403 292' in message
                if not delay_access_logs.is_set():
                    with log_count_lock:
                        initial_access_log_count += 1
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
    assert denial_log_count == len(request_ids)
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
) -> None:
    server, _session, _authority = production_server
    cookie, csrf, origin = _authenticate(server)
    status, catalog_response, _ = _request(
        server,
        "GET",
        _contract("GET", "/api/runtime-surface/profiles"),
        headers={"Cookie": cookie, "X-Tobkiri-Request-ID": str(uuid.uuid4())},
    )
    assert status == 200, catalog_response
    catalog = catalog_response["data"]["data"]
    selected = next(item for item in catalog["profiles"] if item["active"])
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
    status, activated, _ = post(
        "/api/runtime-surface/profile-change/activate",
        {
            "approval_id": approved["data"]["approval_id"],
            "approval_digest": approved["data"]["approval_digest"],
        },
    )
    assert status == 200, activated
    assert activated["data"]["state"] == "active"


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
    original_load = profile_capture.ActivationStore.load_active_snapshot

    def counted_load(store):
        if threading.current_thread().name.startswith("tobkiri-runtime-read"):
            read_worker_capture_loads.append(threading.get_ident())
        return original_load(store)

    monkeypatch.setattr(
        profile_capture.ActivationStore,
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
    assert status == 200, replayed
    assert replayed["data"] == activated["data"]
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

    assert status == 503, rejected
    assert rejected["data"]["code"] == "API_FAILURE"
    assert len(lookup_calls) == 1
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
    exact._validate_contract_runtime()
    assert exact.server is None


def test_selected_desktop_entrypoint_has_no_compatibility_server_authority() -> None:
    desktop = (
        RUNTIME_ROOT / "ecosystem" / "defaultspack" / "defaultspack" / "desktop_app.py"
    ).read_text(encoding="utf-8")
    assert "DefaultsHttpServer" not in desktop
    assert "transport.http" not in desktop
    assert "build_fallback_http_routes" not in desktop
