from __future__ import annotations

import json
import sys
import base64
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_debug_status_rejects_fake_broker_with_same_bearer_token():
    import hashlib

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    key = Ed25519PrivateKey.generate()
    instance_nonce = "real-launcher-instance"

    class SignedLauncherBroker(BaseHTTPRequestHandler):
        def _signed_response(self):
            request_nonce = self.headers["X-Rumi-Launcher-Response-Nonce"]
            payload = json.dumps(
                {
                    "ok": True,
                    "status": {"state": "active"},
                    "verified": True,
                    "consumed": True,
                }
            ).encode()
            payload_hash = hashlib.sha256(payload).hexdigest()
            signed = (
                "tobkiri-launcher-response-v1\n"
                f"{instance_nonce}\n{request_nonce}\n{self.command}\n{self.path}\n"
                f"200\n{payload_hash}"
            ).encode()
            body = json.dumps({
                "ok": True,
                "status": {"state": "active"},
                "verified": True,
                "consumed": True,
                "_launcher_attestation": {
                    "version": 1,
                    "algorithm": "Ed25519",
                    "instance_nonce": instance_nonce,
                    "request_nonce": request_nonce,
                    "method": self.command,
                    "path": self.path,
                    "status": 200,
                    "payload_sha256": payload_hash,
                    "payload": base64.urlsafe_b64encode(payload).decode().rstrip("="),
                    "signature": base64.urlsafe_b64encode(
                        key.sign(signed)
                    ).decode().rstrip("="),
                },
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _signed_response
        do_POST = _signed_response

        def log_message(self, *_args):
            pass

    class FakeBroker(BaseHTTPRequestHandler):
        def _fake_response(self):
            body = json.dumps(
                {
                    "ok": True,
                    "status": {"state": "active"},
                    "verified": True,
                    "consumed": True,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _fake_response
        do_POST = _fake_response

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SignedLauncherBroker)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    public_key = base64.urlsafe_b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode().rstrip("=")
    client = ViewerBrokerClient(
        url=f"http://127.0.0.1:{port}",
        token="stolen-token",
        attestation_public_key=public_key,
        instance_nonce=instance_nonce,
    )
    try:
        assert client.debug_approval_status()["status"]["state"] == "active"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    fake = ThreadingHTTPServer(("127.0.0.1", port), FakeBroker)
    fake_thread = threading.Thread(target=fake.serve_forever, daemon=True)
    fake_thread.start()
    try:
        with pytest.raises(RuntimeError, match="attestation is missing"):
            client.debug_approval_status()
        with pytest.raises(RuntimeError, match="attestation is missing"):
            client.verify_debug_cli_operator({}, expected_decision="approve")
        with pytest.raises(RuntimeError, match="attestation is missing"):
            client.consume_debug_execution(
                request_id="request-1",
                lease_epoch=1,
                execution_jti="execution-1",
            )
    finally:
        fake.shutdown()
        fake.server_close()
        fake_thread.join(timeout=2)


def test_viewer_broker_client_reads_env_url_and_token(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from core_runtime.host_contract import bind_host_contract

    monkeypatch.setenv("RUMI_VIEWER_HOST_BROKER_URL", "http://ambient.invalid:8770")
    monkeypatch.setenv("RUMI_VIEWER_HOST_BROKER_TOKEN", "ambient-token")

    with bind_host_contract(
        {
            "schema_version": "tobkiri.host-contract.v1",
            "profile_id": "default",
            "values": {
                "viewer_broker_url": "http://127.0.0.1:8770",
                "viewer_broker_token": "secret-token",
            },
        }
    ):
        client = ViewerBrokerClient.from_environment()

    assert client.available() is True
    assert client.url == "http://127.0.0.1:8770"
    assert client.token == "secret-token"


def test_viewer_broker_client_reads_connection_json(tmp_path, monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    connection = tmp_path / "host_broker" / "connection.json"
    connection.parent.mkdir(parents=True, exist_ok=True)
    connection.write_text(
        json.dumps(
            {
                "version": 1,
                "host": "127.0.0.1",
                "port": 8771,
                "url": "http://127.0.0.1:8771",
                "token": "file-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("RUMI_VIEWER_HOST_BROKER_URL", raising=False)
    monkeypatch.delenv("RUMI_VIEWER_HOST_BROKER_TOKEN", raising=False)
    monkeypatch.setenv("RUMI_VIEWER_HOST_BROKER_CONNECTION", str(connection))

    client = ViewerBrokerClient.from_environment()

    assert client.available() is True
    assert client.url == "http://127.0.0.1:8771"
    assert client.token == "file-token"


def test_viewer_broker_client_fails_closed_on_configured_port_mismatch(tmp_path, monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    connection = tmp_path / "connection.json"
    connection.write_text(
        json.dumps(
            {
                "version": 1,
                "host": "127.0.0.1",
                "port": 8770,
                "url": "http://127.0.0.1:8770",
                "token": "wrong-broker-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("RUMI_VIEWER_HOST_BROKER_URL", raising=False)
    monkeypatch.delenv("RUMI_VIEWER_HOST_BROKER_TOKEN", raising=False)
    monkeypatch.setenv("RUMI_VIEWER_HOST_BROKER_CONNECTION", str(connection))
    monkeypatch.setenv("RUMI_VIEWER_BROKER_PORT", "8771")

    client = ViewerBrokerClient.from_environment()

    assert client.available() is False


def test_viewer_broker_client_rejects_invalid_port_and_non_loopback_url(tmp_path, monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    monkeypatch.setenv("RUMI_VIEWER_HOST_BROKER_URL", "http://192.0.2.1:8771")
    monkeypatch.setenv("RUMI_VIEWER_HOST_BROKER_TOKEN", "token")
    monkeypatch.setenv("RUMI_VIEWER_BROKER_PORT", "not-a-port")
    assert ViewerBrokerClient.from_environment().available() is False

    monkeypatch.setenv("RUMI_VIEWER_BROKER_PORT", "8771")
    assert ViewerBrokerClient.from_environment().available() is False


def test_viewer_broker_client_does_not_fallback_when_explicit_url_pair_is_incomplete(
    tmp_path, monkeypatch
):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient
    from core_runtime.host_contract import bind_host_contract

    connection = tmp_path / "connection.json"
    connection.write_text(
        json.dumps(
            {
                "version": 1,
                "host": "127.0.0.1",
                "port": 8770,
                "url": "http://127.0.0.1:8770",
                "token": "file-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_VIEWER_HOST_BROKER_URL", "http://ambient.invalid:8771")
    monkeypatch.setenv("RUMI_VIEWER_HOST_BROKER_TOKEN", "ambient-token")
    monkeypatch.setenv("RUMI_VIEWER_HOST_BROKER_CONNECTION", str(connection))

    with bind_host_contract(
        {
            "schema_version": "tobkiri.host-contract.v1",
            "profile_id": "default",
            "values": {"viewer_broker_url": "http://127.0.0.1:8771"},
        }
    ):
        assert ViewerBrokerClient.from_environment().available() is False


def test_viewer_broker_client_waits_longer_than_helper_timeout(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge import viewer_broker_client
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(viewer_broker_client.urllib.request, "urlopen", fake_urlopen)

    ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret-token").permissions()

    assert seen["timeout"] == viewer_broker_client.VIEWER_BROKER_REQUEST_TIMEOUT_SECONDS
    assert seen["timeout"] > viewer_broker_client.VIEWER_BROKER_HELPER_TIMEOUT_SECONDS


def test_viewer_broker_client_execute_intent_posts_payload(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    captured: dict[str, object] = {}

    def fake_request(self, method, path, payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "intent_id": "intent_1"}

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret-token")

    result = client.execute_intent({"intent": "open", "target": "settings"})

    assert result == {"ok": True, "intent_id": "intent_1"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/host/intent/execute"
    assert captured["payload"] == {"intent": "open", "target": "settings"}


def test_viewer_broker_client_start_stream_posts_payload(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    captured: dict[str, object] = {}

    def fake_request(self, method, path, payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "stream_id": "stream_1"}

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret-token")

    result = client.start_stream({"topic": "desktop"})

    assert result == {"ok": True, "stream_id": "stream_1"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/host/stream/start"
    assert captured["payload"] == {"topic": "desktop"}


def test_viewer_broker_client_stop_stream_posts_payload_with_stream_id(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    captured: dict[str, object] = {}

    def fake_request(self, method, path, payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret-token")
    payload = {"reason": "done"}

    result = client.stop_stream("stream 1/2", payload)

    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/host/stream/stop"
    assert captured["payload"] == {"reason": "done", "stream_id": "stream 1/2"}
    assert payload == {"reason": "done"}


def test_viewer_broker_client_stream_events_gets_encoded_stream_id(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    captured: dict[str, object] = {}

    def fake_request(self, method, path, payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "events": []}

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret-token")

    result = client.stream_events("stream 1/2")

    assert result == {"ok": True, "events": []}
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/host/stream/events/stream%201%2F2"
    assert captured["payload"] is None


def test_viewer_broker_client_drops_haze_sequence_from_helper_approval_args(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    captured: dict[str, object] = {}

    def fake_request(self, method, path, payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True, "result": {"action": "computer.show_app"}}

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret-token")

    client.run_computer(
        "computer.show_app",
        {
            "action": "computer.show_app",
            "payload": {"app": "Vivaldi", "computer_use_haze_sequence_id": "run_1"},
            "approval_token": "tok",
            "computer_use_haze_sequence_id": "run_1",
        },
        context={"conversation_id": "conv_1"},
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/host/computer/run"
    assert captured["payload"]["args"] == {"app": "Vivaldi"}
    assert captured["payload"]["approval_token"] == "tok"


def test_computer_router_routes_darwin_computer_calls_to_viewer(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge import computer_router

    class FakeClient:
        def available(self):
            return True

        def run_computer(self, function_id, args, context=None, artifact_root=None):
            return {"action": function_id, "routed": True, "payload": dict(args), "context": dict(context or {})}

    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(computer_router.ViewerBrokerClient, "from_environment", classmethod(lambda cls: FakeClient()))

    result = computer_router.run_computer_action(
        "computer.click",
        {"x": 10, "approval_token": "tok"},
        {"conversation_id": "conv_1"},
    )

    assert result["routed"] is True
    assert result["action"] == "computer.click"
    assert result["context"]["conversation_id"] == "conv_1"


def test_computer_router_uses_context_token_for_viewer_approval(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge import computer_router

    class FakeClient:
        def available(self):
            return True

        def run_computer(self, function_id, args, context=None, artifact_root=None):
            return {"action": function_id, "approval_token_seen": args.get("approval_token")}

    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(computer_router.ViewerBrokerClient, "from_environment", classmethod(lambda cls: FakeClient()))

    result = computer_router.run_computer_action(
        "computer.click",
        {"x": 10},
        {"tool_approval_tokens": {"computer.click": "tok_ctx"}},
        tool_name="computer_use",
    )

    assert result["approval_token_seen"] == "tok_ctx"


def test_computer_router_adds_text_input_guidance_to_viewer_observations(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge import computer_router

    class FakeClient:
        def available(self):
            return True

        def run_computer(self, function_id, args, context=None, artifact_root=None):
            return {"action": function_id, "ok": True, "recommended_next_actions": ["custom.next"]}

    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(computer_router.ViewerBrokerClient, "from_environment", classmethod(lambda cls: FakeClient()))

    for action in ("computer.screenshot", "computer.observe"):
        result = computer_router.run_computer_action(
            action,
            {},
            {"conversation_id": "conv_1"},
            tool_name="browser_computer",
        )

        assert result["action"] == action
        assert result["recommended_next_actions"][0] == "custom.next"
        assert "computer.type" in result["recommended_next_actions"]
        assert "computer.key" in result["recommended_next_actions"]
        assert "normal approval gates still apply" in result["input_guidance"]


def test_computer_router_does_not_add_text_guidance_to_viewer_approval_result(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge import computer_router

    class FakeClient:
        def available(self):
            return True

        def run_computer(self, function_id, args, context=None, artifact_root=None):
            return {
                "action": function_id,
                "requires_approval": True,
                "approval_request_id": "apr_viewer",
                "recommended_next_actions": ["approve_request"],
            }

    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(computer_router.ViewerBrokerClient, "from_environment", classmethod(lambda cls: FakeClient()))

    result = computer_router.run_computer_action("computer.screenshot", {}, {"conversation_id": "conv_1"})

    assert result["requires_approval"] is True
    assert result["recommended_next_actions"] == ["approve_request"]
    assert "input_guidance" not in result


def test_computer_router_skips_viewer_for_internal_host_execution(tmp_path, monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge import computer_router

    captured: dict[str, object] = {}

    def fake_run(action, payload, *, source_function_id):
        captured["action"] = action
        captured["payload"] = dict(payload)
        captured["source_function_id"] = source_function_id
        return {"action": action, "local": True}

    monkeypatch.setenv("RUMI_COMPUTER_HOST_INTERNAL", "1")
    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(computer_router, "run_host_contract_action", fake_run)

    def unexpected_viewer_call(cls):
        del cls
        raise AssertionError("internal host execution must not use Viewer")

    monkeypatch.setattr(
        computer_router.ViewerBrokerClient,
        "from_environment",
        classmethod(unexpected_viewer_call),
    )

    result = computer_router.run_computer_action(
        "computer.type",
        {"text": "hello"},
        {"conversation_id": "conv_1"},
        artifact_root=tmp_path,
        yolo_mode=True,
    )

    assert result["local"] is True
    assert captured["action"] == "computer.type"
    assert captured["payload"] == {
        "text": "hello",
        "artifact_root": str(tmp_path),
        "yolo_mode": True,
    }
    assert captured["source_function_id"] == "defaultspack.domain.host_bridge.computer_router"


def test_computer_router_returns_recovery_when_viewer_is_unavailable(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge import computer_router

    class FakeClient:
        def available(self):
            return False

    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(computer_router.ViewerBrokerClient, "from_environment", classmethod(lambda cls: FakeClient()))

    result = computer_router.run_computer_action("computer.screenshot", {}, {})

    assert result["is_error"] is True
    assert result["permission_subject"] == "Rumi Viewer"
    assert "Open Rumi Viewer" in result["recovery"]["note"]


def test_computer_router_returns_recovery_when_viewer_connection_is_stale(monkeypatch, tmp_path):
    from ecosystem.defaultspack.domain.host_bridge import computer_router

    class FakeClient:
        def available(self):
            return True

        def run_computer(self, function_id, args, context=None, artifact_root=None):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(computer_router.ViewerBrokerClient, "from_environment", classmethod(lambda cls: FakeClient()))

    result = computer_router.run_computer_action("computer.screenshot", {}, {}, artifact_root=tmp_path)

    assert result["is_error"] is True
    assert "unavailable" in result["reason"]
    assert result["permission_subject"] == "Rumi Viewer"


def test_viewer_broker_client_includes_artifact_root_but_not_pack_chat_store(tmp_path, monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    captured: dict[str, object] = {}
    chat_store_path = tmp_path / "chat" / "conversations.json"

    def fake_request(self, method, path, payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = dict(payload or {})
        return {"ok": True, "result": {"action": "computer.screenshot"}}

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(chat_store_path))
    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret-token")

    client.run_computer(
        "computer.screenshot",
        {},
        context={"conversation_id": "conv_1", "pack_id": "pack_1"},
        artifact_root=tmp_path,
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/host/computer/run"
    assert captured["payload"]["artifact_root"] == str(tmp_path)
    assert "chat_store_path" not in captured["payload"]
    assert captured["payload"]["pack_id"] == "pack_1"


def test_viewer_broker_client_unwraps_controller_shaped_approval_args(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    captured: dict[str, object] = {}

    def fake_request(self, method, path, payload=None):
        captured["method"] = method
        captured["path"] = path
        captured["payload"] = dict(payload or {})
        return {"ok": True, "result": {"action": "computer.windows"}}

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret-token")

    client.run_computer(
        "computer.windows",
        {
            "action": "computer.windows",
            "payload": {},
            "approval_token": "approval-token",
        },
        context={"conversation_id": "conv_1", "pack_id": "pack_1"},
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/api/host/computer/run"
    assert captured["payload"]["approval_token"] == "approval-token"
    assert captured["payload"]["args"] == {}
    assert captured["payload"]["conversation_id"] == "conv_1"


def test_viewer_broker_client_preserves_approval_payload_from_broker(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    def fake_request(self, method, path, payload=None):
        return {
            "ok": False,
            "audit_id": "host-audit-1",
            "result": {
                "action": "computer.click",
                "requires_approval": True,
                "approval_token": "tok",
            },
            "error": {"code": "APPROVAL_REQUIRED", "message": "Approval required."},
        }

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    client = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret-token")

    result = client.run_computer("computer.click", {"x": 10})

    assert result["requires_approval"] is True
    assert result["approval_token"] == "tok"
    assert result["error_code"] == "APPROVAL_REQUIRED"
    assert result["host_audit_id"] == "host-audit-1"


def test_viewer_broker_client_preserves_only_safe_type_failure_diagnostics(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    def fake_request(self, method, path, payload=None):
        return {
            "ok": False,
            "audit_id": "host-audit-type",
            "diagnostics": {
                "error_code": "TYPE_VERIFICATION_UNAVAILABLE",
                "input_dispatched": False,
                "dispatched_units": 0,
                "failure_stage": "initial_target_verification",
                "text": "private",
                "approval_token": "token",
                "pid": 123,
                "window_title": "private",
                "nested": {"raw_args": "private"},
            },
            "error": {"code": "TYPE_COMPLETION_NOT_VERIFIED", "message": "Typing failed."},
        }

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    result = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
        "computer.type", {"text": "private"}
    )

    assert result["diagnostics"] == {
        "error_code": "TYPE_VERIFICATION_UNAVAILABLE",
        "input_dispatched": False,
        "dispatched_units": 0,
        "failure_stage": "initial_target_verification",
    }


def test_viewer_broker_client_preserves_only_safe_posted_delivery_facts(monkeypatch):
    from ecosystem.defaultspack.domain.host_bridge.viewer_broker_client import ViewerBrokerClient

    def fake_request(self, method, path, payload=None):
        return {
            "ok": False,
            "audit_id": "host-audit-posted",
            "result": {
                "action": "computer.type",
                "executed": True,
                "delivered": True,
                "background": True,
                "foreground": False,
                "driver": "mac_accessibility",
                "can_parallel_user_work": True,
                "requires_foreground": False,
                "uses_physical_input": False,
                "completion_verified": False,
                "outcome": "posted_unverified",
                "verification_required": "screenshot",
                "ax_candidate": {
                    "driver_registered": True,
                    "driver_available": True,
                    "background_type_capable": True,
                    "pyobjc_ax_import_available": False,
                    "ax_process_trusted": False,
                    "ax_set_value_unsafe_app": False,
                    "target_app_present": True,
                    "target_bundle_present": True,
                    "target_pid_present": True,
                    "target_window_present": True,
                    "attempted": False,
                    "result_code": "AX_IMPORT_UNAVAILABLE",
                    "raw_target": "private AX target",
                },
                "text": "private text",
                "url": "https://example.invalid/?secret=query",
                "window_title": "private title",
                "pid": 123,
                "window_id": 456,
                "approval_token": "private token",
                "raw_error": "private error",
            },
            "error": {"code": "TYPE_COMPLETION_NOT_VERIFIED", "message": "private raw broker error"},
        }

    monkeypatch.setattr(ViewerBrokerClient, "_request", fake_request)
    result = ViewerBrokerClient(url="http://127.0.0.1:8770", token="secret").run_computer(
        "computer.type", {"text": "private request", "approval_token": "approval"}
    )

    assert result["executed"] is True
    assert result["delivered"] is True
    assert result["background"] is True
    assert result["driver"] == "mac_accessibility"
    assert result["completion_verified"] is False
    assert result["outcome"] == "posted_unverified"
    assert result["verification_required"] == "screenshot"
    assert result["ax_candidate"]["result_code"] == "AX_IMPORT_UNAVAILABLE"
    assert result["ax_candidate"]["pyobjc_ax_import_available"] is False
    assert "raw_target" not in result["ax_candidate"]
    serialized = str(result)
    for private in (
        "private text",
        "secret=query",
        "private title",
        "123",
        "456",
        "private token",
        "private error",
        "private request",
        "private raw broker error",
        "private AX target",
    ):
        assert private not in serialized


def test_computer_router_wraps_viewer_approval_into_request_id(monkeypatch):
    from domain.safety import approval
    from ecosystem.defaultspack.domain.host_bridge import computer_router

    approval.reset_approval_state_for_tests()

    class FakeClient:
        def available(self):
            return True

        def run_computer(self, function_id, args, context=None, artifact_root=None):
            return {
                "action": function_id,
                "requires_approval": True,
                "approval_token": "viewer_tok",
                "approval_hint": "approval required",
            }

    monkeypatch.setattr(computer_router.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(computer_router.ViewerBrokerClient, "from_environment", classmethod(lambda cls: FakeClient()))

    result = computer_router.run_computer_action(
        "computer.click",
        {"x": 10, "y": 20},
        {"conversation_id": "conv_1", "owner_pack": "pack_1"},
        tool_name="computer_use",
    )

    assert result["requires_approval"] is True
    assert result["approval_required"] is True
    assert result["tool_name"] == "computer_use"
    assert result["operation"] == "computer.click"
    assert result["payload"] == {"x": 10, "y": 20}
    assert str(result["approval_request_id"]).startswith("apr_")
    assert result["message"] == "approval required"
    assert result["user_prompt"] == "承認してください"

    decision = approval.approve(result["approval_request_id"])
    encoded = decision["token"].split(".", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))
    assert payload["operation"] == "computer.click"
    assert payload["function_id"] == "computer.click"
    assert payload["pack_id"] == "pack_1"
    assert payload["conversation_id"] == "conv_1"


def test_tool_executor_routes_local_computer_tools_through_router(monkeypatch):
    from domain.tool.executor import ToolExecutor
    from ecosystem.defaultspack.domain.host_bridge import computer_router

    captured: dict[str, object] = {}

    def fake_router(action, payload, context=None, *, tool_name="computer_use", artifact_root=None, yolo_mode=False):
        captured["action"] = action
        captured["payload"] = dict(payload)
        captured["context"] = dict(context or {})
        captured["tool_name"] = tool_name
        return {"action": action, "ok": True}

    monkeypatch.setattr(computer_router, "run_computer_action", fake_router)

    result = ToolExecutor()._execute_local(
        "computer_use",
        {"action": "click", "x": 10, "y": 20},
        {"conversation_id": "conv_1"},
    )

    assert result["is_error"] is False
    assert captured["action"] == "computer.click"
    assert captured["payload"]["x"] == 10
    assert captured["payload"]["y"] == 20
    assert captured["tool_name"] == "computer_use"
    assert captured["context"]["conversation_id"] == "conv_1"


def test_computer_host_helper_accepts_workspace_artifact_root(monkeypatch, tmp_path):
    from core_runtime.host_broker import computer_host_helper

    chat_store_path = tmp_path / "chat" / "conversations.json"
    artifact_root = chat_store_path.parent / "conversations" / "conv-1" / "workspace" / "tools" / "computer"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(chat_store_path))

    result = computer_host_helper._validated_artifact_root(str(artifact_root))

    assert result == artifact_root.resolve()


def test_computer_host_helper_rejects_non_workspace_artifact_root(tmp_path):
    from core_runtime.host_broker import computer_host_helper

    invalid_root = tmp_path / "rogue" / "computer"

    try:
        computer_host_helper._validated_artifact_root(str(invalid_root))
    except ValueError as exc:
        assert "artifact_root" in str(exc)
    else:  # pragma: no cover - safety net for explicit failure messaging
        raise AssertionError("expected artifact_root validation to fail")


def test_defaultspack_browser_computer_block_uses_router(monkeypatch):
    import ecosystem.defaultspack.blocks.tool.browser_computer as browser_computer_block

    captured: dict[str, object] = {}

    def fake_router(action, payload, context=None, *, tool_name="computer_use", artifact_root=None, yolo_mode=False):
        captured["action"] = action
        captured["payload"] = dict(payload)
        captured["context"] = dict(context or {})
        captured["tool_name"] = tool_name
        captured["artifact_root"] = artifact_root
        captured["yolo_mode"] = yolo_mode
        return {"action": action, "ok": True}

    monkeypatch.setattr(browser_computer_block, "run_computer_action", fake_router)

    result = browser_computer_block.run(
        {"action": "computer.observe", "payload": {"detail": "full"}},
        {"conversation_workspace_dir": "/tmp/work", "yolo_mode": "true"},
    )

    assert result["status"] == "ok"
    assert captured["action"] == "computer.observe"
    assert captured["payload"]["detail"] == "full"
    assert captured["context"]["conversation_workspace_dir"] == "/tmp/work"
    assert captured["tool_name"] == "browser_computer"
    assert captured["yolo_mode"] is True
