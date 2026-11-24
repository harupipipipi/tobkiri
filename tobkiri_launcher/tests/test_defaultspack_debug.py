from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "defaultspack_debug.py"
SPEC = importlib.util.spec_from_file_location("defaultspack_debug", SCRIPT_PATH)
assert SPEC and SPEC.loader
debug = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(debug)


class FakeClient:
    def __init__(self) -> None:
        self.base_url = "http://127.0.0.1:8785"
        self.api_token = "local-api-token-which-must-never-print"
        self.browser_approval_token = "browser-approval-token-which-must-never-print"
        self.calls: list[tuple[str, str, dict]] = []
        self.stream_payloads: list[dict] = []
        self.stream_count = 0

    @property
    def secrets_to_hide(self) -> tuple[str, ...]:
        return (self.api_token, self.browser_approval_token)

    def get(self, path: str, *, query=None) -> dict:
        self.calls.append(("GET", path, {"query": query}))
        if path == "/api/health":
            return {"status": "healthy"}
        if path == "/api/desktop-system-info":
            return {
                "source": "viewer_broker",
                "reliable": True,
                "host_broker": {"available": True},
            }
        if path == "/api/ai/providers":
            return {
                "providers": [
                    {"provider_id": "cerebras", "registered": True},
                ]
            }
        if path == "/api/ai/models":
            assert query == {"provider": "cerebras"}
            return {"models": [{"qualified_model_id": "cerebras/gemma-4-31b"}]}
        if path == "/api/authority/requests":
            pending = []
            if self.stream_count == 1:
                pending = [
                    {
                        "request_id": "auth-1",
                        "status": "pending",
                        "permission_id": "model.invoke",
                        "conversation_id": "chat-1",
                        "allowed_scopes": ["once", "conversation"],
                        "resource": {
                            "provider_id": "cerebras",
                            "api_id": "legacy",
                            "model_id": "gemma-4-31b",
                            "model_ref": "cerebras/gemma-4-31b",
                            "domain": "api.cerebras.ai",
                            "stream": True,
                        },
                    }
                ]
            return {"pending": pending, "requests": pending}
        if path == "/api/coding/approvals":
            pending = []
            if self.stream_count == 2:
                pending = [
                    {
                        "request_id": "apr-1",
                        "status": "pending",
                        "operation": "computer.type",
                        "created_at": 1,
                        "details": {
                            "conversation_id": "chat-1",
                            "tool_name": "browser_computer",
                            "action": "computer.type",
                            "arguments": {
                                "action": "computer.type",
                                "payload": {"text": "youtube", "app": "Atlas"},
                            },
                        },
                    }
                ]
            return {"pending": pending, "requests": pending}
        raise AssertionError(path)

    def post(self, path: str, payload: dict, *, query=None, headers=None) -> dict:
        self.calls.append(
            (
                "POST",
                path,
                {"payload": payload, "query": query, "headers": headers},
            )
        )
        if path == "/api/chat/conversations":
            return {"id": "chat-1", "model": payload["model"]}
        if path == "/api/authority/browser-ui-operator":
            return {
                "request_id": "auth-1",
                "ui_operator": {
                    "version": 1,
                    "kind": "ui_operator",
                    "request_id": "auth-1",
                    "signature": "signed-not-a-secret-in-this-fixture",
                },
            }
        if path == "/api/authority/requests/auth-1/approve":
            return {
                "request_id": "auth-1",
                "approved": True,
                "scope": payload["scope"],
                "permission_id": "model.invoke",
                "token": "authority-replay-token-which-must-never-print",
                "related_approvals": [
                    {
                        "request_id": "auth-related",
                        "permission_id": "api_key.use",
                        "token": "related-authority-token-which-must-never-print",
                    }
                ],
            }
        if path == "/api/coding/approvals/approve":
            return {
                "request_id": "apr-1",
                "approved": True,
                "token": "runtime-replay-token-which-must-never-print",
            }
        raise AssertionError(path)

    def stream(self, path: str, payload: dict):
        self.calls.append(("STREAM", path, {"payload": payload}))
        self.stream_payloads.append(payload)
        self.stream_count += 1
        if self.stream_count == 1:
            yield {
                "type": "approval_requested",
                "authority": True,
                "request_id": "auth-1",
                "permission_id": "model.invoke",
            }
            yield {
                "type": "done",
                "message": {
                    "id": "message-1",
                    "finish_reason": "authority_approval_required",
                },
            }
            return
        if self.stream_count == 2:
            yield {
                "type": "approval_requested",
                "approval_request_id": "apr-1",
                "tool_name": "browser_computer",
                "tool_call_id": "call-1",
                "action": "computer.type",
                "operation": "computer.type",
                "payload": {"text": "youtube", "app": "Atlas"},
            }
            yield {
                "type": "done",
                "message": {"id": "message-2", "finish_reason": "approval_required"},
            }
            return
        yield {"type": "delta", "delta": "Task complete"}
        yield {
            "type": "done",
            "message": {"id": "message-3", "finish_reason": "stop"},
        }


def test_smoke_stops_at_authority_without_impersonating_user(tmp_path):
    client = FakeClient()
    output = io.StringIO()
    reporter = debug.SmokeReporter(output, secrets_to_hide=client.secrets_to_hide)
    artifact = {
        "chat_store": str(tmp_path / "chat" / "conversations.json"),
        "log_path": str(tmp_path / "defaultspack.log"),
        "run_dir": str(tmp_path),
    }
    runner = debug.ComputerUseSmokeRunner(
        client,
        artifact,
        prompt="ordinary prompt",
        max_turns=4,
        reporter=reporter,
        min_stream_interval_seconds=0,
    )

    with pytest.raises(
        debug.SmokeRunnerError, match="automatic Authority approval is disabled"
    ):
        runner.run()

    assert client.stream_payloads[0]["message"]["content"] == "ordinary prompt"
    assert client.stream_payloads[0]["tools"] == ["browser_computer"]
    assert client.stream_payloads[0]["params"]["tool_policy"] == {
        "action_approval_mode": "ask",
        "selected_tools": ["browser_computer"],
    }
    assert not any(
        path == "/api/authority/browser-ui-operator" or path.endswith("/approve")
        for _method, path, _details in client.calls
    )


def _owned_smoke_launch_fixture(tmp_path, monkeypatch):
    run_root = tmp_path / "run-root"
    latest_path = run_root / "latest.json"
    run_dir = run_root / "launch-owned"
    run_dir.mkdir(parents=True)
    api_token_path = run_dir / ".desktop_api_token"
    browser_token_path = run_dir / ".authority_browser_test_token"
    api_token_path.write_text("api-token-from-file", encoding="utf-8")
    browser_token_path.write_text("browser-token-from-file", encoding="utf-8")
    api_token_path.chmod(0o600)
    browser_token_path.chmod(0o600)
    artifact = {
        "schema": "rumi.defaultspack-debug-run.v1",
        "run_id": "launch-owned",
        "run_dir": str(run_dir),
        "manifest_path": str(run_dir / "manifest.json"),
        "pid": 7313,
        "process_start_marker": "owned-start-marker",
        "port": 18799,
        "token_file": str(api_token_path),
        "browser_approval_token_file": str(browser_token_path),
        "user_data": str(run_dir / "defaultspack_state" / "user_data"),
        "chat_store": str(run_dir / "defaultspack_state" / "chat" / "conversations.json"),
    }
    (run_dir / "manifest.json").write_text(json.dumps(artifact), encoding="utf-8")
    latest_path.write_text(json.dumps(artifact), encoding="utf-8")
    monkeypatch.setattr(debug, "RUN_ROOT", run_root)
    monkeypatch.setattr(debug, "LATEST_JSON", latest_path)
    monkeypatch.setattr(debug, "process_start_marker", lambda _pid: "owned-start-marker")
    monkeypatch.setattr(debug, "pid_is_running", lambda _pid: True)
    monkeypatch.setattr(debug, "lsof_listener", lambda _port: {"pid": "7313"})
    return artifact


def test_load_smoke_configuration_reads_only_owned_manifest_tokens(
    tmp_path, monkeypatch
):
    artifact = _owned_smoke_launch_fixture(tmp_path, monkeypatch)

    configuration = debug.load_smoke_configuration(artifact["port"])

    assert configuration["base_url"] == "http://127.0.0.1:18799"
    assert configuration["api_token"] == "api-token-from-file"
    assert configuration["browser_approval_token"] == ""
    assert "api_token" not in configuration["artifact"]
    assert "browser_approval_token" not in configuration["artifact"]


def test_load_smoke_configuration_rejects_foreign_port_before_sending_tokens(
    tmp_path, monkeypatch
):
    _owned_smoke_launch_fixture(tmp_path, monkeypatch)

    with pytest.raises(debug.SmokeRunnerError, match="does not match"):
        debug.load_smoke_configuration(18800)


def test_load_smoke_configuration_rejects_foreign_listener(tmp_path, monkeypatch):
    artifact = _owned_smoke_launch_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(debug, "lsof_listener", lambda _port: {"pid": "9999"})

    with pytest.raises(debug.SmokeRunnerError, match="active owned listener"):
        debug.load_smoke_configuration(artifact["port"])


def test_load_smoke_configuration_rejects_manifest_token_path_tampering(
    tmp_path, monkeypatch
):
    artifact = _owned_smoke_launch_fixture(tmp_path, monkeypatch)
    external = tmp_path / "foreign-token"
    external.write_text("foreign-secret-canary", encoding="utf-8")
    external.chmod(0o600)
    artifact["token_file"] = str(external)
    Path(artifact["manifest_path"]).write_text(json.dumps(artifact), encoding="utf-8")
    debug.LATEST_JSON.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(debug.SmokeRunnerError, match="invalid local API token-file"):
        debug.load_smoke_configuration(artifact["port"])


def test_reporter_redacts_nested_text_tokens_and_sensitive_url_query():
    output = io.StringIO()
    reporter = debug.SmokeReporter(output, secrets_to_hide=("known-secret",))

    reporter.emit(
        "stream",
        token="raw-token",
        action=debug._compact_action("computer.type Atlas youtube"),
        payload={
            "text": "youtube",
            "query": "youtube",
            "nested": {"value": "known-secret"},
        },
        url="https://example.test/watch?v=1&access_token=known-secret",
    )

    printed = output.getvalue()
    assert "raw-token" not in printed
    assert "youtube" not in printed
    assert "known-secret" not in printed
    event = json.loads(printed)
    assert event["token"] == "[redacted]"
    assert event["action"] == "computer.type"
    assert event["payload"]["text"] == "[redacted]"
    assert event["payload"]["query"] == "[redacted]"
    assert "v=1" in event["url"]
    assert "%5Bredacted%5D" in event["url"]


def test_compact_stream_event_reports_only_allowlisted_type_diagnostics():
    event = {
        "type": "tool_call_completed",
        "tool_name": "browser_computer",
        "result": {
            "data": {
                "widget": {
                    "error_code": "TYPE_COMPLETION_NOT_VERIFIED",
                    "diagnostics": {
                        "error_code": "TYPE_VERIFICATION_UNAVAILABLE",
                        "input_strategy": "none",
                        "completion_verified": False,
                        "input_dispatched": False,
                        "dispatched_units": 0,
                        "target_pid_stable": False,
                        "focused_element_stable": False,
                        "failure_stage": "initial_target_verification",
                        "direct_ax_attempted": False,
                        "mutation_observed": False,
                        "typed_text": "youtube",
                        "approval_token": "must-not-escape",
                        "pid": 123,
                        "window_title": "private-window",
                    },
                }
            }
        },
    }

    compact = debug._compact_stream_event(event, turn=8)

    assert compact is not None
    assert compact["type_diagnostics"] == {
        "error_code": "TYPE_VERIFICATION_UNAVAILABLE",
        "input_strategy": "none",
        "completion_verified": False,
        "input_dispatched": False,
        "dispatched_units": 0,
        "target_pid_stable": False,
        "focused_element_stable": False,
        "failure_stage": "initial_target_verification",
        "direct_ax_attempted": False,
        "mutation_observed": False,
    }
    rendered = json.dumps(compact, ensure_ascii=False)
    assert "youtube" not in rendered
    assert "must-not-escape" not in rendered
    assert "private-window" not in rendered


def test_compact_type_diagnostics_recognizes_top_level_fixed_error_code_only():
    assert debug._compact_type_diagnostics(
        {"error_code": "TYPE_COMPLETION_NOT_VERIFIED"}
    ) == {"error_code": "TYPE_COMPLETION_NOT_VERIFIED"}
    assert debug._compact_type_diagnostics(
        {"error_code": "PRIVATE_BODY_SHOULD_NOT_ESCAPE"}
    ) == {"error_code": "TYPE_ERROR"}
    assert debug._compact_type_diagnostics(
        {"error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND"}
    ) == {"error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND"}


def test_smoke_paces_only_actual_stream_calls_with_injectable_clock(
    tmp_path, monkeypatch
):
    class Clock:
        now = 100.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            sleeps.append(seconds)
            self.now += seconds

    client = FakeClient()
    output = io.StringIO()
    sleeps = []
    clock = Clock()
    runner = debug.ComputerUseSmokeRunner(
        client,
        {
            "chat_store": str(tmp_path / "chat" / "conversations.json"),
            "log_path": str(tmp_path / "defaultspack.log"),
            "run_dir": str(tmp_path),
        },
        prompt="ordinary prompt",
        max_turns=4,
        reporter=debug.SmokeReporter(output, secrets_to_hide=client.secrets_to_hide),
        min_stream_interval_seconds=35.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    monkeypatch.setattr(
        runner,
        "_approve_authority",
        lambda _request: debug._message_request("delegated authority resume"),
    )
    monkeypatch.setattr(
        runner,
        "_approve_runtime",
        lambda _request, _events: debug._message_request("delegated runtime resume"),
    )

    result = runner.run()

    assert result["ok"] is True
    assert sleeps == [35.0, 35.0]
    assert len([call for call in client.calls if call[0] == "STREAM"]) == 3
    pacing = [json.loads(line) for line in output.getvalue().splitlines() if "stream_pacing" in line]
    assert [event["event"] for event in pacing] == [
        "stream_pacing_ready",
        "stream_pacing_wait",
        "stream_pacing_ready",
        "stream_pacing_wait",
        "stream_pacing_ready",
    ]
    assert all("token" not in event and "payload" not in event for event in pacing)
    assert client.api_token not in output.getvalue()


def test_provider_preflight_fails_before_conversation_or_model_turn(tmp_path):
    class MissingProviderClient(FakeClient):
        def get(self, path: str, *, query=None) -> dict:
            if path == "/api/ai/providers":
                self.calls.append(("GET", path, {"query": query}))
                return {"providers": [{"provider_id": "cerebras", "registered": False}]}
            return super().get(path, query=query)

    client = MissingProviderClient()
    output = io.StringIO()
    runner = debug.ComputerUseSmokeRunner(
        client,
        {"chat_store": str(tmp_path / "chat" / "conversations.json")},
        prompt="ordinary prompt",
        max_turns=1,
        reporter=debug.SmokeReporter(output),
        min_stream_interval_seconds=0,
        provider_preflight=debug.isolated_smoke_provider_preflight(
            {"CEREBRAS_API_KEY": "preflight-canary-not-printed"}
        ),
    )

    with pytest.raises(debug.SmokeRunnerError, match="PROVIDER_NOT_REGISTERED"):
        runner.run()

    assert client.stream_count == 0
    assert not any(call[1] == "/api/chat/conversations" for call in client.calls)
    assert "preflight-canary-not-printed" not in output.getvalue()


def test_owned_child_log_redacts_provider_canary(tmp_path):
    canary = "canary-cerebras-log-secret-a73e2"

    class Process:
        stdout = io.StringIO(f"provider key={canary}\n")

    log_path = tmp_path / "child.log"
    tee = debug.ViewerLogTee(Process(), log_path, secrets_to_hide=(canary,), echo=False)
    tee.start()
    tee.join()

    assert canary not in log_path.read_text(encoding="utf-8")


def test_transient_post_tool_error_resumes_with_pacing_without_replay(tmp_path):
    class TransientClient(FakeClient):
        def get(self, path: str, *, query=None) -> dict:
            if path in {"/api/authority/requests", "/api/coding/approvals"}:
                return {"pending": [], "requests": []}
            return super().get(path, query=query)

        def stream(self, path: str, payload: dict):
            self.calls.append(("STREAM", path, {"payload": payload}))
            self.stream_payloads.append(payload)
            self.stream_count += 1
            if self.stream_count == 1:
                yield {"type": "tool_call_completed", "tool_name": "browser_computer"}
                yield {
                    "type": "done",
                    "message": {
                        "finish_reason": "ai_error_after_tool_use",
                        "metadata": {
                            "transient_ai_error": True,
                            "sanitized_error": "[Errno 60] Operation timed out",
                        },
                    },
                }
                return
            yield {"type": "done", "message": {"finish_reason": "stop"}}

    class Clock:
        now = 10.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            sleeps.append(seconds)
            self.now += seconds

    client = TransientClient()
    output = io.StringIO()
    sleeps = []
    clock = Clock()
    runner = debug.ComputerUseSmokeRunner(
        client,
        {"chat_store": str(tmp_path / "chat/store.json")},
        prompt="original task",
        max_turns=3,
        reporter=debug.SmokeReporter(output),
        max_transient_resumes=2,
        min_stream_interval_seconds=35.0,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    result = runner.run()

    assert result["ok"] is True
    assert sleeps == [35.0]
    assert client.stream_payloads[0]["message"]["content"] == "original task"
    continuation = client.stream_payloads[1]["message"]["content"]
    assert "current visually verified state" in continuation
    assert "do not repeat any completed action" in continuation
    assert "approval_followup" not in client.stream_payloads[1]["message"].get("metadata", {})
    recovery = [json.loads(line) for line in output.getvalue().splitlines() if "transient_ai_recovery" in line]
    assert recovery == [{"event": "transient_ai_recovery", "count": 1, "reason_class": "timeout"}]
    assert "Operation timed out" not in output.getvalue()


def test_transient_resume_is_bounded_and_rejects_non_transient_errors(tmp_path):
    def events(error, *, marked=False):
        return [
            {
                "type": "done",
                "message": {
                    "finish_reason": "ai_error_after_tool_use",
                    "metadata": {"transient_ai_error": marked, "sanitized_error": error},
                },
            }
        ]

    assert debug._transient_ai_error_class(events("temporarily unavailable")) == "temporary_provider"
    assert debug._transient_ai_error_class(events("authentication failed", marked=True)) is None
    assert debug._transient_ai_error_class(events("invalid response format", marked=True)) is None
    assert debug._transient_ai_error_class(events("provider failure")) is None

    direct_metadata = [
        {
            "type": "done",
            "metadata": {"transient_ai_error": True},
            "message": {"finish_reason": "ai_error_after_tool_use"},
        }
    ]
    assert debug._transient_ai_error_class(direct_metadata) == "marked_transient"


def test_runtime_approval_candidate_prefers_stream_payload_shape():
    request = {
        "request_id": "apr-1",
        "operation": "computer.type",
        "details": {
            "tool_name": "browser_computer",
            "arguments": {
                "action": "computer.type",
                "payload": {"text": "fallback"},
            },
        },
    }
    events = [
        {
            "type": "approval_requested",
            "approval_request_id": "apr-1",
            "tool_name": "browser_computer",
            "tool_call_id": "call-1",
            "action": "computer.type",
            "payload": {"text": "literal search text", "app": "Atlas"},
        }
    ]

    candidate = debug._runtime_approval_candidate(events, request)

    assert candidate == {
        "request_id": "apr-1",
        "tool_name": "browser_computer",
        "tool_call_id": "call-1",
        "operation": "computer.type",
        "action": "computer.type",
        "payload": {"text": "literal search text", "app": "Atlas"},
    }


def test_stale_connection_status_identifies_dead_connection_file(tmp_path):
    connection_path = tmp_path / "connection.json"
    connection_path.write_text("{}", encoding="utf-8")

    status = debug.stale_connection_status(
        connection_path,
        {
            "connection": {
                "path": str(connection_path),
                "pid": 12345,
                "pid_running": False,
                "port": 8770,
                "port_open": False,
            },
            "health": {"ok": False, "error": "connection refused"},
        },
    )

    assert status == {"stale": True, "reason": "connection file PID is no longer running"}


def test_viewer_log_tee_marks_wry_detached_panic_without_retaining_secret(tmp_path):
    class Process:
        stdout = None

    log_tee = debug.ViewerLogTee(Process(), tmp_path / "viewer.log")

    line = log_tee.consume_line(
        "thread panicked at " + debug.WRY_DETACHED_PANIC + " token=viewer-secret-value\n"
    )

    assert log_tee.wry_detached_panic is True
    assert debug.WRY_DETACHED_PANIC in line
    assert "viewer-secret-value" not in line
    assert "viewer-api-value" not in log_tee.consume_line('{"api_key":"viewer-api-value"}\n')


def test_viewer_build_environment_uses_explicit_debug_preflight_without_mutating_parent(monkeypatch):
    monkeypatch.setenv("RUMI_VIEWER_MIN_FREE_MB", "5120")

    env = debug.viewer_build_environment(4096, 8771)

    assert debug.DEFAULT_VIEWER_MIN_FREE_MB == 4096
    assert env["RUMI_VIEWER_MIN_FREE_MB"] == "4096"
    assert env["RUMI_VIEWER_BROKER_PORT"] == "8771"
    assert debug.os.environ["RUMI_VIEWER_MIN_FREE_MB"] == "5120"


def test_viewer_build_environment_sets_validated_debug_isolation_only_when_complete(tmp_path):
    root = tmp_path / "viewer_user_data"
    root.mkdir()

    env = debug.viewer_build_environment(
        4096,
        18770,
        connection_path=root / "host_broker" / "connection.json",
        instance_nonce="safe_nonce-1234567890",
        debug_instance_id="debug-123-safe",
        debug_user_data_root=root,
    )

    assert env[debug.VIEWER_DEBUG_INSTANCE_ID_ENV] == "debug-123-safe"
    assert env[debug.VIEWER_DEBUG_USER_DATA_ROOT_ENV] == str(root.resolve())
    assert env[debug.VIEWER_BROKER_CONNECTION_ENV].endswith("host_broker/connection.json")
    assert env[debug.VIEWER_BROKER_INSTANCE_NONCE_ENV] == "safe_nonce-1234567890"
    with pytest.raises(ValueError):
        debug.viewer_build_environment(4096, 18770, debug_instance_id="debug-123-safe")
    with pytest.raises(debug.SmokeRunnerError):
        debug.validate_debug_instance_id("not-debug")


def test_prepare_owned_viewer_debug_root_rejects_external_connection(tmp_path):
    supervisor = tmp_path / "run"
    expected = supervisor / "viewer_user_data" / "host_broker" / "connection.json"

    root, connection = debug.prepare_owned_viewer_debug_root(supervisor, expected)

    assert root == (supervisor / "viewer_user_data").resolve()
    assert connection == expected.resolve()
    assert root.stat().st_mode & 0o777 == 0o700
    with pytest.raises(debug.SmokeRunnerError):
        debug.prepare_owned_viewer_debug_root(supervisor, tmp_path / "external.json")


def test_configured_viewer_broker_port_is_strict_and_defaults(monkeypatch):
    monkeypatch.delenv("RUMI_VIEWER_BROKER_PORT", raising=False)
    assert debug.configured_viewer_broker_port() == 8770
    monkeypatch.setenv("RUMI_VIEWER_BROKER_PORT", "8771")
    assert debug.configured_viewer_broker_port() == 8771
    for invalid in ("0", " 8771", "8771 ", "65536", "localhost:8771"):
        monkeypatch.setenv("RUMI_VIEWER_BROKER_PORT", invalid)
        with pytest.raises(debug.SmokeRunnerError):
            debug.configured_viewer_broker_port()


def test_reserve_loopback_port_selects_distinct_owned_ports():
    first = debug.reserve_loopback_port(
        requested=None,
        excluded={debug.DEFAULT_VIEWER_BROKER_PORT, debug.DEFAULT_DEFAULTSPACK_HTTP_PORT},
        name="Defaultspack HTTP port",
    )
    second = debug.reserve_loopback_port(
        requested=None,
        excluded={
            debug.DEFAULT_VIEWER_BROKER_PORT,
            debug.DEFAULT_KERNEL_PORT,
            first.port,
        },
        name="kernel port",
    )
    try:
        assert first.port not in {debug.DEFAULT_VIEWER_BROKER_PORT, debug.DEFAULT_DEFAULTSPACK_HTTP_PORT}
        assert second.port not in {debug.DEFAULT_VIEWER_BROKER_PORT, debug.DEFAULT_KERNEL_PORT, first.port}
        assert debug.port_is_open(first.port)
        assert debug.port_is_open(second.port)
    finally:
        first.release()
        second.release()


def test_viewer_build_environment_requires_complete_defaultspack_debug_context(tmp_path):
    root = tmp_path / "viewer_user_data"
    root.mkdir()
    state_root = tmp_path / "defaultspack_state"

    env = debug.viewer_build_environment(
        4096,
        18770,
        connection_path=root / "host_broker" / "connection.json",
        instance_nonce="safe_nonce-1234567890",
        debug_instance_id="debug-123-safe",
        debug_user_data_root=root,
        defaultspack_run_id="debug-123-safe",
        defaultspack_state_root=state_root,
        defaultspack_http_port=18771,
        kernel_port=18772,
    )

    assert env[debug.DEFAULTSPACK_DEBUG_ISOLATION_ENV] == "1"
    assert env[debug.DEFAULTSPACK_DEBUG_RUN_ID_ENV] == "debug-123-safe"
    assert env[debug.DEFAULTSPACK_DEBUG_STATE_ROOT_ENV] == str(state_root.resolve())
    assert env[debug.DEFAULTSPACK_DEBUG_HTTP_PORT_ENV] == "18771"
    assert env[debug.DEFAULTSPACK_DEBUG_KERNEL_PORT_ENV] == "18772"
    assert env[debug.VIEWER_TRUSTED_CHAT_STORE_ENV] == str(state_root.resolve() / "chat" / "conversations.json")
    approval_secret_path = Path(env["RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH"])
    assert approval_secret_path == state_root.resolve() / "approval" / "approval_runtime_secret"
    assert approval_secret_path.stat().st_mode & 0o777 == 0o600
    assert approval_secret_path.read_text(encoding="utf-8").strip()
    with pytest.raises(ValueError):
        debug.viewer_build_environment(
            4096,
            18770,
            connection_path=root / "host_broker" / "connection.json",
            instance_nonce="safe_nonce-1234567890",
            debug_instance_id="debug-123-safe",
            debug_user_data_root=root,
            defaultspack_run_id="debug-123-safe",
            defaultspack_state_root=state_root,
            defaultspack_http_port=18771,
        )


def test_load_connection_rejects_mismatched_or_unauthenticated_broker(tmp_path, monkeypatch):
    path = tmp_path / "connection.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "host": "127.0.0.1",
                "port": 8770,
                "url": "http://127.0.0.1:8770",
                "token": "secret",
                "permission_subject": "Tobkiri Launcher",
                "pid": 12,
                "created_at": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(debug, "http_status", lambda url, **kwargs: {"ok": True})
    monkeypatch.setattr(debug, "pid_is_running", lambda pid: True)
    _, mismatch = debug.load_connection(path, expected_port=8771)
    assert mismatch["ok"] is False
    assert "mismatch" in mismatch["health"]["error"]

    calls = []

    def fake_status(url, **kwargs):
        calls.append((url, kwargs.get("token")))
        return {"ok": len(calls) == 1}

    monkeypatch.setattr(debug, "http_status", fake_status)
    _, unauthenticated = debug.load_connection(path, expected_port=8770)
    assert unauthenticated["ok"] is False
    assert calls[1][1] == "secret"


def test_defaultspack_python_prefers_launcher_managed_venv(tmp_path, monkeypatch):
    app_data = tmp_path / "dev.tobkiri.launcher"
    connection_path = app_data / "user_data" / "host_broker" / "connection.json"
    launcher_python = app_data / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")
    launcher_python.parent.mkdir(parents=True)
    launcher_python.write_text("", encoding="utf-8")
    launcher_python.chmod(0o700)
    monkeypatch.setattr(debug, "default_connection_path", lambda: connection_path)

    assert debug.defaultspack_python_executable() == launcher_python.absolute()


def test_viewer_smoke_composes_existing_launch_and_smoke_and_stops_only_owned_processes(
    tmp_path, monkeypatch
):
    class Process:
        def __init__(self) -> None:
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

        def kill(self):
            self.returncode = -9

    class LogTee:
        wry_detached_panic = False

        def join(self, timeout=1.0):
            return None

    viewer = Process()
    defaultspack = Process()
    supervisor_root = tmp_path / "runs"
    monkeypatch.setattr(debug, "RUN_ROOT", supervisor_root)
    monkeypatch.setattr(debug, "_has_live_pty", lambda: True)
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-child-env-only")
    monkeypatch.setattr(
        debug,
        "load_connection",
        lambda path, **kwargs: ({}, {"ok": False, "connection": {"path": str(path)}, "health": {}}),
    )
    captured_viewer_builds = []

    def fake_start_viewer_dev(path, **kwargs):
        captured_viewer_builds.append((path, kwargs))
        return viewer, LogTee()

    monkeypatch.setattr(debug, "start_viewer_dev", fake_start_viewer_dev)
    monkeypatch.setattr(
        debug,
        "wait_for_viewer_broker",
        lambda *args, **kwargs: {"ok": True, "connection": {"port": 18770, "token_present": True}},
    )
    captured_launch_args = []

    def fake_launch(args, include_process=False):
        captured_launch_args.append(args)
        return {
            "ok": True,
            "launch": {"port": 8766, "log_path": str(tmp_path / "defaultspack.log")},
            "_process": defaultspack if include_process else None,
        }

    monkeypatch.setattr(debug, "launch", fake_launch)
    captured_smoke_args = []

    def fake_smoke(args):
        captured_smoke_args.append(args)
        return {"ok": True, "turns": 1}

    monkeypatch.setattr(debug, "smoke_computer_use", fake_smoke)

    result = debug.viewer_smoke_computer_use(
        type(
            "Args",
            (),
            {
                "connection": None,
                "viewer_broker_port": 18770,
                "port": None,
                "user_data": None,
                "wait_seconds": 1.0,
                "viewer_wait_seconds": 1.0,
                "viewer_min_free_mb": 3072,
                "max_turns": 1,
                "min_stream_interval_seconds": 7.0,
                "max_transient_resumes": 1,
                "keep_running": False,
                "prompt": None,
            },
        )()
    )

    assert result["ok"] is True
    assert result["smoke"] == {"ok": True, "turns": 1}
    assert captured_viewer_builds[0][1]["min_free_mb"] == 3072
    assert captured_viewer_builds[0][1]["broker_port"] == 18770
    assert captured_viewer_builds[0][1]["connection_path"].is_relative_to(supervisor_root)
    assert captured_viewer_builds[0][1]["instance_nonce"]
    assert captured_viewer_builds[0][1]["debug_instance_id"].startswith("debug-")
    assert captured_viewer_builds[0][1]["debug_user_data_root"].is_relative_to(supervisor_root)
    assert captured_launch_args[0].viewer_broker_port == 18770
    assert captured_launch_args[0].port not in {
        18770,
        debug.DEFAULT_DEFAULTSPACK_HTTP_PORT,
        debug.DEFAULT_KERNEL_PORT,
    }
    assert captured_launch_args[0].defaultspack_debug_run_id.startswith("debug-")
    assert captured_launch_args[0].defaultspack_debug_state_root.is_relative_to(supervisor_root)
    assert captured_launch_args[0].defaultspack_kernel_port not in {
        18770,
        debug.DEFAULT_DEFAULTSPACK_HTTP_PORT,
        debug.DEFAULT_KERNEL_PORT,
        captured_launch_args[0].port,
    }
    assert captured_smoke_args[0].min_stream_interval_seconds == 7.0
    assert captured_smoke_args[0].max_transient_resumes == 1
    assert viewer.terminated is True
    assert defaultspack.terminated is True
    assert result["cleanup"]["viewer"]["label"] == "viewer"
    assert result["cleanup"]["defaultspack"]["label"] == "defaultspack"


def test_launch_propagates_validated_connection_port_to_child_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    connection_path = tmp_path / "connection.json"
    connection = {
        "version": 1,
        "host": "127.0.0.1",
        "port": 8771,
        "url": "http://127.0.0.1:8771",
        "token": "secret",
    }
    connection_path.write_text(json.dumps(connection), encoding="utf-8")
    run_root = tmp_path / "runs"
    monkeypatch.setattr(debug, "RUN_ROOT", run_root)
    monkeypatch.setattr(
        debug,
        "load_desktop_app",
        lambda: {"command": "python desktop_app.py", "env": {}},
    )
    monkeypatch.setattr(debug, "load_connection", lambda path, **kwargs: (connection, {"ok": True, "connection": {"port": 8771}}))
    monkeypatch.setattr(debug, "port_is_open", lambda port: False)
    monkeypatch.setattr(debug, "wait_for_owned_defaultspack_health", lambda port, process, timeout: True)
    monkeypatch.setattr(debug, "status", lambda args: {"ok": True})
    monkeypatch.setenv(debug.VIEWER_DEBUG_INSTANCE_ID_ENV, "debug-parent-value")
    monkeypatch.setenv(debug.VIEWER_DEBUG_USER_DATA_ROOT_ENV, str(tmp_path / "parent-root"))
    monkeypatch.setenv(debug.VIEWER_BROKER_INSTANCE_NONCE_ENV, "parent-nonce")
    captured = {}

    class Process:
        pid = 1234

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(debug.subprocess, "Popen", fake_popen)

    result = debug.launch(
        type(
            "Args",
            (),
            {
                "port": 8766,
                "connection": str(connection_path),
                "viewer_broker_port": 8771,
                "user_data": "relative-user-data",
                "wait_seconds": 1.0,
                "allow_no_broker": False,
            },
        )()
    )

    assert result["ok"] is True
    assert captured["env"]["RUMI_VIEWER_BROKER_PORT"] == "8771"
    assert captured["env"]["RUMI_VIEWER_HOST_BROKER_CONNECTION"] == str(connection_path)
    assert captured["env"]["TOBKIRI_USER_DATA"] == str(
        (tmp_path / "relative-user-data").resolve()
    )
    assert captured["env"]["RUMI_USER_DATA"] == str(
        (tmp_path / "relative-user-data").resolve()
    )
    direct_workspace = Path(captured["env"]["RUMI_DEFAULTSPACK_DIRECT_CONVERSATION_WORKSPACE"])
    assert direct_workspace == Path(captured["env"]["RUMI_DEFAULTSPACK_CHAT_STORE_PATH"]).parent / "conversations" / "direct-http" / "workspace"
    assert (direct_workspace / "tools" / "computer").is_dir()
    assert debug.VIEWER_DEBUG_INSTANCE_ID_ENV not in captured["env"]
    assert debug.VIEWER_DEBUG_USER_DATA_ROOT_ENV not in captured["env"]
    assert debug.VIEWER_BROKER_INSTANCE_NONCE_ENV not in captured["env"]


def test_launch_applies_debug_isolation_after_desktop_metadata(tmp_path, monkeypatch):
    connection_path = tmp_path / "connection.json"
    connection = {"port": 18770}
    connection_path.write_text(json.dumps(connection), encoding="utf-8")
    state_root = tmp_path / "viewer-smoke" / "defaultspack_state"
    monkeypatch.setattr(debug, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(
        debug,
        "load_desktop_app",
        lambda: {
            "command": "python desktop_app.py",
            "env": {"DEFAULTS_HTTP_PORT": "8766", "RUMI_DEFAULTSPACK_PORT": "8766"},
        },
    )
    monkeypatch.setattr(
        debug,
        "load_connection",
        lambda path, **kwargs: (connection, {"ok": True, "connection": {"port": 18770}}),
    )
    monkeypatch.setattr(debug, "port_is_open", lambda port: False)
    monkeypatch.setattr(debug, "wait_for_owned_defaultspack_health", lambda port, process, timeout: True)
    monkeypatch.setattr(debug, "status", lambda args: {"ok": True})
    captured = {}

    class Process:
        pid = 1234

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(debug.subprocess, "Popen", fake_popen)
    result = debug.launch(
        type(
            "Args",
            (),
            {
                "port": 18771,
                "connection": str(connection_path),
                "viewer_broker_port": 18770,
                "user_data": str(state_root / "user_data"),
                "wait_seconds": 1.0,
                "allow_no_broker": False,
                "defaultspack_debug_run_id": "debug-123-safe",
                "defaultspack_debug_nonce": "safe_nonce-1234567890",
                "defaultspack_debug_state_root": state_root,
                "defaultspack_kernel_port": 18772,
            },
        )()
    )

    assert result["ok"] is True
    env = captured["env"]
    assert env["DEFAULTS_HTTP_HOST"] == "127.0.0.1"
    assert env["DEFAULTS_HTTP_PORT"] == "18771"
    assert env["RUMI_DEFAULTSPACK_PORT"] == "18771"
    assert env["RUMI_PORT"] == "18772"
    assert env["TOBKIRI_USER_DATA"] == str(state_root / "user_data")
    assert env["RUMI_USER_DATA"] == str(state_root / "user_data")
    assert env[debug.DEFAULTSPACK_DEBUG_ISOLATION_ENV] == "1"
    assert env[debug.DEFAULTSPACK_REQUIRE_OWN_BIND_ENV] == "1"
    assert env["RUMI_DEFAULTSPACK_APPROVAL_DB_PATH"].startswith(str(state_root))
    assert env["RUMI_DEFAULTSPACK_SECRETS_DIR"].startswith(str(state_root))
    assert env["RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH"] == str(
        state_root.resolve() / "approval" / "approval_runtime_secret"
    )


def test_isolated_cerebras_provider_env_is_fixed_and_never_persisted(tmp_path):
    canary = "canary-cerebras-credential-must-not-persist-9f4ee4"
    parent = {
        "CEREBRAS_API_KEY": canary,
        "CEREBRAS_BASE_URL": "https://untrusted.invalid/v1",
        "CEREBRAS_UNRELATED_OVERRIDE": "not-forwarded",
    }
    state_root = tmp_path / "defaultspack_state"
    child_env = {"CEREBRAS_BASE_URL": parent["CEREBRAS_BASE_URL"]}

    preflight = debug.isolated_smoke_provider_preflight(parent)
    debug.apply_isolated_smoke_provider_env(
        child_env,
        parent_env=parent,
        require_credential=True,
    )
    debug.apply_defaultspack_debug_isolation(
        child_env,
        run_id="debug-123-safe",
        nonce="safe_nonce-1234567890",
        state_root=state_root,
        http_port=18771,
        kernel_port=18772,
    )

    assert preflight == {
        "provider_id": "cerebras",
        "model": "cerebras/gemma-4-31b",
        "credential_present": True,
        "credential_source": "inherited_env",
        "credential_persisted": False,
        "allow_custom_base_url": False,
    }
    assert child_env["CEREBRAS_API_KEY"] == canary
    assert "CEREBRAS_BASE_URL" not in child_env
    assert "CEREBRAS_UNRELATED_OVERRIDE" not in child_env
    assert child_env["RUMI_DEFAULTSPACK_ENABLE_CLOUD_PROVIDERS"] == "1"
    settings = json.loads((state_root / "frontend_settings.json").read_text(encoding="utf-8"))
    assert settings == {
        "models": {
            "preferred_model": "cerebras/gemma-4-31b",
            "thinking_level": "medium",
            "deepthink_enabled": False,
        }
    }
    assert (state_root / "frontend_settings.json").stat().st_mode & 0o777 == 0o600
    for path in state_root.rglob("*"):
        if path.is_file():
            assert canary.encode("utf-8") not in path.read_bytes()


def test_isolated_smoke_provider_requires_parent_cerebras_env_before_start(monkeypatch):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    preflight = debug.isolated_smoke_provider_preflight()

    assert preflight["credential_present"] is False
    assert preflight["credential_source"] == "inherited_env"
    child_env = {"CEREBRAS_BASE_URL": "https://untrusted.invalid/v1"}
    with pytest.raises(debug.SmokeRunnerError, match="PROVIDER_ENV_NOT_PRESENT"):
        debug.apply_isolated_smoke_provider_env(
            child_env,
            parent_env={},
            require_credential=True,
        )
    assert "CEREBRAS_BASE_URL" not in child_env


def test_mimo_profile_forwards_only_fixed_key_and_seeds_fixed_model(tmp_path):
    key = "zen-key-canary-that-must-stay-ephemeral"
    parent = {
        "OPENCODE_ZEN_API_KEY": key,
        "OPENCODE_ZEN_BASE_URL": "https://attacker.invalid/v1",
        "CEREBRAS_API_KEY": "unselected-provider-key",
        "CEREBRAS_BASE_URL": "https://also.invalid/v1",
        "OPENAI_API_KEY": "openai-canary",
        "OPENAI_COMPATIBLE_BASE_URL": "https://openai-compatible.invalid",
        "ANTHROPIC_API_KEY": "anthropic-canary",
        "GOOGLE_API_KEY": "google-canary",
        "GEMINI_API_KEY": "gemini-canary",
        "OPENCODE_GO_API_KEY": "go-canary",
        "RUMI_CLOUDFLARE_OAUTH_ACCESS_TOKEN": "cloudflare-canary",
        "PATH": "/safe/runtime/bin",
    }
    child = dict(parent)

    preflight = debug.isolated_smoke_provider_preflight(
        parent, profile_name=debug.MIMO_CHAT_PROFILE
    )
    debug.apply_isolated_smoke_provider_env(
        child,
        parent_env=parent,
        require_credential=True,
        profile_name=debug.MIMO_CHAT_PROFILE,
    )
    debug.seed_isolated_smoke_model_selection(
        tmp_path, profile_name=debug.MIMO_CHAT_PROFILE
    )

    assert preflight == {
        "provider_id": "opencode-zen",
        "model": "opencode-zen/mimo-v2.5-free",
        "credential_present": True,
        "credential_source": "inherited_env",
        "credential_persisted": False,
        "allow_custom_base_url": False,
    }
    assert debug._SMOKE_PROVIDER_PROFILES[debug.MIMO_CHAT_PROFILE] == {
        "provider_id": "opencode-zen",
        "model": "opencode-zen/mimo-v2.5-free",
        "credential_env": "OPENCODE_ZEN_API_KEY",
        "env_prefix": "OPENCODE_ZEN_",
        "api_id": "legacy",
        "endpoint_url": "https://opencode.ai/zen/v1/chat/completions",
        "endpoint_path": "/v1/chat/completions",
        "origin": "https://opencode.ai",
        "domain": "opencode.ai",
        "port": 443,
        "transport": "https",
    }
    assert child["OPENCODE_ZEN_API_KEY"] == key
    assert "OPENCODE_ZEN_BASE_URL" not in child
    assert "CEREBRAS_API_KEY" not in child
    assert "CEREBRAS_BASE_URL" not in child
    assert "OPENAI_API_KEY" not in child
    assert "OPENAI_COMPATIBLE_BASE_URL" not in child
    assert "ANTHROPIC_API_KEY" not in child
    assert "GOOGLE_API_KEY" not in child
    assert "GEMINI_API_KEY" not in child
    assert "OPENCODE_GO_API_KEY" not in child
    assert "RUMI_CLOUDFLARE_OAUTH_ACCESS_TOKEN" not in child
    assert child["PATH"] == "/safe/runtime/bin"
    settings = json.loads((tmp_path / "frontend_settings.json").read_text())
    assert settings["models"]["preferred_model"] == "opencode-zen/mimo-v2.5-free"
    assert key not in (tmp_path / "frontend_settings.json").read_text()


def test_isolated_smoke_strips_complete_canonical_provider_environment():
    selected_key = "selected-zen-key"
    # Code-owned mirror of every provider credential and endpoint env declared by
    # the bundled provider catalog, curated metadata, and provider manifests.  A
    # new catalog env must be added to the harness isolation contract explicitly.
    canonical_provider_env_names = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "AVIAN_API_KEY",
        "AVIAN_BASE_URL",
        "CEREBRAS_API_KEY",
        "CEREBRAS_BASE_URL",
        "DEEPINFRA_API_KEY",
        "DEEPINFRA_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "FIREWORKS_API_KEY",
        "FIREWORKS_BASE_URL",
        "FRIENDLI_API_KEY",
        "FRIENDLI_BASE_URL",
        "GEMINI_API_KEY",
        "GENSPARK_API_KEY",
        "GENSPARK_LLM_BASE_URL",
        "GITLAWB_OPENGATEWAY_API_KEY",
        "GITLAWB_OPENGATEWAY_BASE_URL",
        "GLM_API_KEY",
        "GLM_BASE_URL",
        "GOOGLE_API_KEY",
        "GOOGLE_BASE_URL",
        "GROQ_API_KEY",
        "GROQ_BASE_URL",
        "HYPERBOLIC_API_KEY",
        "HYPERBOLIC_BASE_URL",
        "INFERENCENET_API_KEY",
        "INFERENCENET_BASE_URL",
        "INFERENCE_NET_API_KEY",
        "INFERENCE_NET_BASE_URL",
        "LLAMACPP_API_KEY",
        "LLAMACPP_BASE_URL",
        "LMSTUDIO_API_KEY",
        "LMSTUDIO_BASE_URL",
        "LONGCAT_API_KEY",
        "LONGCAT_BASE_URL",
        "MIMO_API_KEY",
        "MISTRAL_API_KEY",
        "MISTRAL_BASE_URL",
        "MOONSHOT_API_KEY",
        "MOONSHOT_BASE_URL",
        "NEBIUS_API_KEY",
        "NEBIUS_BASE_URL",
        "NGC_API_KEY",
        "NVIDIA_API_KEY",
        "NVIDIA_BASE_URL",
        "NOVITA_API_KEY",
        "NOVITA_BASE_URL",
        "OLLAMA_API_KEY",
        "OLLAMA_BASE_URL",
        "OLLAMA_HOST",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_COMPATIBLE_API_KEY",
        "OPENAI_COMPATIBLE_BASE_URL",
        "OPENCODE_GO_API_KEY",
        "OPENCODE_GO_BASE_URL",
        "OPENCODE_ZEN_API_KEY",
        "OPENCODE_ZEN_BASE_URL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "PERPLEXITY_API_KEY",
        "PERPLEXITY_BASE_URL",
        "SAMBANOVA_API_KEY",
        "SAMBANOVA_BASE_URL",
        "TOGETHER_API_KEY",
        "TOGETHER_BASE_URL",
        "UPSTAGE_API_KEY",
        "UPSTAGE_BASE_URL",
        "VLLM_API_KEY",
        "VLLM_BASE_URL",
        "XAI_API_KEY",
        "XAI_BASE_URL",
        "XIAOMI_MIMO_CN_API_KEY",
        "XIAOMI_MIMO_CN_BASE_URL",
        "XIAOMI_MIMO_GLOBAL_API_KEY",
        "XIAOMI_MIMO_GLOBAL_BASE_URL",
        "XIAOMI_MIMO_TOKEN_PLAN_AMS_API_KEY",
        "XIAOMI_MIMO_TOKEN_PLAN_AMS_BASE_URL",
        "XIAOMI_MIMO_TOKEN_PLAN_API_KEY",
        "XIAOMI_MIMO_TOKEN_PLAN_CN_API_KEY",
        "XIAOMI_MIMO_TOKEN_PLAN_CN_BASE_URL",
        "XIAOMI_MIMO_TOKEN_PLAN_SGP_API_KEY",
        "XIAOMI_MIMO_TOKEN_PLAN_SGP_BASE_URL",
    }
    oauth_provider_env_names = {
        "CF_API_TOKEN",
        "CLOUDFLARE_API_TOKEN",
        "RUMIOAUTH_CLOUDFLARE_ACCESS_TOKEN",
        "RUMIOAUTH_CLOUDFLARE_CLIENT_CONFIG",
        "RUMIOAUTH_CLOUDFLARE_ID_TOKEN",
        "RUMIOAUTH_CLOUDFLARE_REFRESH_TOKEN",
        "RUMIOAUTH_GOOGLE_ACCESS_TOKEN",
        "RUMIOAUTH_GOOGLE_CLIENT_CONFIG",
        "RUMIOAUTH_GOOGLE_ID_TOKEN",
        "RUMIOAUTH_GOOGLE_REFRESH_TOKEN",
        "RUMI_CLOUDFLARE_OAUTH_ACCESS_TOKEN",
        "RUMI_CLOUDFLARE_OAUTH_REFRESH_TOKEN",
        "RUMI_CLOUDFLARE_SANDBOX_API_KEY",
    }
    provider_env_names = canonical_provider_env_names | oauth_provider_env_names
    parent = {name: f"secret-for-{name}" for name in provider_env_names}
    parent["OPENCODE_ZEN_API_KEY"] = selected_key
    child = dict(parent)
    child.update(
        {
            "HOME": "/safe/home",
            "LANG": "ja_JP.UTF-8",
            "PATH": "/safe/bin",
            "SSL_CERT_FILE": "/safe/cert.pem",
            "RUMI_VIEWER_DEBUG_INSTANCE_ID": "debug-safe",
        }
    )

    debug.apply_isolated_smoke_provider_env(
        child,
        parent_env=parent,
        require_credential=True,
        profile_name=debug.MIMO_CHAT_PROFILE,
    )

    assert child["OPENCODE_ZEN_API_KEY"] == selected_key
    assert not (provider_env_names - {"OPENCODE_ZEN_API_KEY"}) & child.keys()
    assert child["HOME"] == "/safe/home"
    assert child["LANG"] == "ja_JP.UTF-8"
    assert child["PATH"] == "/safe/bin"
    assert child["SSL_CERT_FILE"] == "/safe/cert.pem"
    assert child["RUMI_VIEWER_DEBUG_INSTANCE_ID"] == "debug-safe"


@pytest.mark.parametrize(
    "ambient_name",
    [
        "GENSPARK_FUTURE_ENDPOINT_OVERRIDE",
        "INFERENCE_NET_FUTURE_API_KEY",
        "INFERENCENET_FUTURE_API_KEY",
        "LMSTUDIO_FUTURE_BASE_URL",
        "XAI_FUTURE_ENDPOINT_OVERRIDE",
    ],
)
def test_isolated_smoke_strips_future_values_in_canonical_provider_namespaces(
    ambient_name,
):
    child = {
        "OPENCODE_ZEN_API_KEY": "ambient-selected-key",
        ambient_name: "ambient-provider-value",
        "PATH": "/safe/bin",
    }

    debug.apply_isolated_smoke_provider_env(
        child,
        parent_env={"OPENCODE_ZEN_API_KEY": "selected-key"},
        require_credential=True,
        profile_name=debug.MIMO_CHAT_PROFILE,
    )

    assert ambient_name not in child
    assert child == {
        "OPENCODE_ZEN_API_KEY": "selected-key",
        "PATH": "/safe/bin",
        "RUMI_DEFAULTSPACK_ENABLE_CLOUD_PROVIDERS": "1",
    }


def test_unknown_smoke_provider_profile_is_rejected_without_forwarding():
    child = {"OPENCODE_ZEN_API_KEY": "ambient"}
    with pytest.raises(
        debug.SmokeRunnerError, match="SMOKE_PROVIDER_PROFILE_NOT_ALLOWED"
    ):
        debug.apply_isolated_smoke_provider_env(
            child,
            parent_env={"ATTACKER_KEY": "secret"},
            require_credential=True,
            profile_name="attacker-profile",
        )
    assert child == {"OPENCODE_ZEN_API_KEY": "ambient"}


def test_mimo_provider_launch_uses_complete_owned_debug_isolation(
    tmp_path, monkeypatch
):
    run_root = tmp_path / "runs"
    monkeypatch.setattr(debug, "RUN_ROOT", run_root)
    monkeypatch.setattr(debug, "LATEST_JSON", run_root / "latest.json")
    monkeypatch.setattr(
        debug,
        "load_desktop_app",
        lambda: {"command": "python desktop_app.py", "env": {}},
    )
    monkeypatch.setattr(
        debug,
        "load_connection",
        lambda *_args, **_kwargs: ({}, {"ok": False, "connection": {}}),
    )
    monkeypatch.setattr(debug, "port_is_open", lambda _port: False)
    selected_ports = iter((18770, 18771))

    class Reservation:
        def __init__(self):
            self.port = next(selected_ports)

        def release(self):
            return None

    monkeypatch.setattr(
        debug, "reserve_loopback_port", lambda **_kwargs: Reservation()
    )
    captured = {}

    class Process:
        pid = 7314
        stdout = io.StringIO("")

        def poll(self):
            return None

    process = Process()

    def popen(_argv, **kwargs):
        captured["env"] = dict(kwargs["env"])
        return process

    monkeypatch.setattr(debug.subprocess, "Popen", popen)
    monkeypatch.setattr(
        debug, "wait_for_owned_defaultspack_health", lambda *_args: True
    )
    monkeypatch.setattr(debug, "process_start_marker", lambda _pid: "owned-start")
    monkeypatch.setattr(debug, "status", lambda _args: {"ok": True})
    parent = {
        "OPENCODE_ZEN_API_KEY": "ephemeral-zen-canary",
        "OPENCODE_ZEN_BASE_URL": "https://attacker.invalid",
        "OPENAI_API_KEY": "ambient-openai-canary",
        "ANTHROPIC_API_KEY": "ambient-anthropic-canary",
        "RUMI_API_TOKEN": "ambient-api-token-canary",
        "RUMI_PANEL_BOOTSTRAP_SECRET": "ambient-bootstrap-canary",
        "RUMI_AUTHORITY_BROWSER_TEST_TOKEN": "ambient-browser-token-canary",
        "PATH": os.environ.get("PATH", ""),
    }
    args = type(
        "Args",
        (),
        {
            "port": None,
            "connection": None,
            "viewer_broker_port": None,
            "user_data": None,
            "wait_seconds": 0.1,
            "allow_no_broker": True,
            "isolated_provider_parent_env": parent,
            "isolated_provider_profile": debug.MIMO_CHAT_PROFILE,
        },
    )()

    result = debug.launch(args, include_process=True)

    assert result["ok"] is True
    env = captured["env"]
    state_root = Path(env[debug.DEFAULTSPACK_DEBUG_STATE_ROOT_ENV])
    assert state_root.parent == Path(result["launch"]["run_dir"])
    assert state_root.name == "defaultspack_state"
    assert env[debug.DEFAULTSPACK_DEBUG_ISOLATION_ENV] == "1"
    assert env[debug.DEFAULTSPACK_REQUIRE_OWN_BIND_ENV] == "1"
    assert env[debug.DEFAULTSPACK_DEBUG_HTTP_PORT_ENV] == "18770"
    assert env[debug.DEFAULTSPACK_DEBUG_KERNEL_PORT_ENV] == "18771"
    assert env["TOBKIRI_USER_DATA"] == str(state_root / "user_data")
    assert env["RUMI_USER_DATA"] == str(state_root / "user_data")
    assert env["RUMI_DEFAULTSPACK_CHAT_STORE_PATH"] == str(
        state_root / "chat" / "conversations.json"
    )
    for key in (
        "RUMI_DEFAULTSPACK_APPROVAL_DB_PATH",
        "RUMI_DEFAULTSPACK_APPROVAL_SECRET_PATH",
        "RUMI_DEFAULTSPACK_AUDIT_PATH",
        "RUMI_DEFAULTSPACK_BROWSER_ARTIFACTS_PATH",
        "RUMI_DEFAULTSPACK_RUNTIME_CONFIG_PATH",
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        "RUMI_DEFAULTSPACK_SECRETS_DIR",
        "RUMI_DEFAULTSPACK_SCHEDULER_DIR",
    ):
        Path(env[key]).resolve().relative_to(state_root)
    settings = json.loads((state_root / "frontend_settings.json").read_text())
    assert settings["models"]["preferred_model"] == "opencode-zen/mimo-v2.5-free"
    assert env["OPENCODE_ZEN_API_KEY"] == "ephemeral-zen-canary"
    assert "OPENCODE_ZEN_BASE_URL" not in env
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["RUMI_API_TOKEN"] != "ambient-api-token-canary"
    assert env["RUMI_PANEL_BOOTSTRAP_SECRET"] != "ambient-bootstrap-canary"
    assert "RUMI_AUTHORITY_BROWSER_TEST_TOKEN" not in env
    assert "ephemeral-zen-canary" not in json.dumps(result["launch"])
    assert "ambient-api-token-canary" not in json.dumps(result["launch"])


class FakeMimoChatClient(FakeClient):
    def get(self, path: str, *, query=None) -> dict:
        if path == "/api/desktop-system-info":
            raise AssertionError("chat-only smoke must not require the Viewer broker")
        if path == "/api/ai/providers":
            return {"providers": [{"provider_id": "opencode-zen", "registered": True}]}
        if path == "/api/ai/models":
            assert query == {"provider": "opencode-zen"}
            return {"models": [{"qualified_model_id": "opencode-zen/mimo-v2.5-free"}]}
        if path == "/api/authority/requests":
            pending = []
            if self.stream_count == 1:
                pending = [
                    {
                        "request_id": "auth-mimo-1",
                        "status": "pending",
                        "permission_id": "model.invoke",
                        "conversation_id": "chat-1",
                        "allowed_scopes": ["once", "conversation"],
                        "config": {
                            "domains": ["request-config-must-not-be-used.invalid"],
                            "ports": [444],
                        },
                        "resource": {
                            "provider_id": "opencode-zen",
                            "api_id": "legacy",
                            "model_id": "mimo-v2.5-free",
                            "model_ref": "opencode-zen/mimo-v2.5-free",
                            "endpoint_url": "https://opencode.ai/zen/v1/chat/completions",
                            "endpoint_path": "/v1/chat/completions",
                            "domain": "opencode.ai",
                            "transport": "https",
                            "port": 443,
                            "stream": True,
                        },
                    }
                ]
            return {"pending": pending, "requests": pending}
        return super().get(path, query=query)

    def post(self, path: str, payload: dict, *, query=None, headers=None) -> dict:
        if path == "/api/chat/conversations":
            self.calls.append(
                ("POST", path, {"payload": payload, "query": query, "headers": headers})
            )
            assert payload["model"] == "opencode-zen/mimo-v2.5-free"
            return {"id": "chat-1", "model": payload["model"]}
        if path == "/api/authority/browser-ui-operator":
            self.calls.append(
                ("POST", path, {"payload": payload, "query": query, "headers": headers})
            )
            return {
                "ui_operator": {
                    "version": 1,
                    "kind": "ui_operator",
                    "request_id": "auth-mimo-1",
                    "signature": "fixture-signature",
                }
            }
        if path == "/api/authority/requests/auth-mimo-1/approve":
            self.calls.append(
                ("POST", path, {"payload": payload, "query": query, "headers": headers})
            )
            return {
                "request_id": "auth-mimo-1",
                "approved": True,
                "scope": payload["scope"],
                "permission_id": "model.invoke",
                "token": "mimo-authority-token-never-print",
            }
        return super().post(path, payload, query=query, headers=headers)

    def stream(self, path: str, payload: dict):
        self.calls.append(("STREAM", path, {"payload": payload}))
        self.stream_payloads.append(payload)
        self.stream_count += 1
        if self.stream_count == 1:
            yield {
                "type": "approval_requested",
                "authority": True,
                "request_id": "auth-mimo-1",
                "permission_id": "model.invoke",
            }
            yield {
                "type": "done",
                "message": {"finish_reason": "authority_approval_required"},
            }
            return
        yield {"type": "delta", "delta": "movie_project: {}"}
        yield {"type": "done", "message": {"finish_reason": "stop"}}


def test_chat_only_mimo_stops_for_delegated_authority_approval(tmp_path):
    client = FakeMimoChatClient()
    output = io.StringIO()
    reporter = debug.SmokeReporter(output, secrets_to_hide=client.secrets_to_hide)
    runner = debug.ChatOnlySmokeRunner(
        client,
        {"chat_store": str(tmp_path / "chat" / "conversations.json")},
        prompt="create a short movie project",
        max_turns=3,
        reporter=reporter,
        provider_preflight=debug.isolated_smoke_provider_preflight(
            {"OPENCODE_ZEN_API_KEY": "ephemeral-key"},
            profile_name=debug.MIMO_CHAT_PROFILE,
        ),
    )

    with pytest.raises(
        debug.SmokeRunnerError, match="automatic Authority approval is disabled"
    ):
        runner.run()

    assert "tools" not in client.stream_payloads[0]
    assert not any(path.endswith("/approve") for _method, path, _details in client.calls)


@pytest.mark.parametrize(
    ("permission_id", "resource_update", "request_config"),
    [
        ("network.egress", {"domain": "attacker.invalid"}, {}),
        ("api_key.use", {"port": 444}, {}),
        ("model.invoke", {"endpoint_url": "https://attacker.invalid/collect"}, {}),
        (
            "network.egress",
            {"endpoint_url": "https://opencode.ai:444/zen/v1/chat/completions"},
            {},
        ),
        (
            "api_key.use",
            {"endpoint_url": "https://opencode.ai/other/v1/chat/completions"},
            {},
        ),
        (
            "model.invoke",
            {"endpoint_url": "http://opencode.ai/zen/v1/chat/completions"},
            {},
        ),
        (
            "network.egress",
            {"endpoint_url": "https://opencode.ai:443/zen/v1/chat/completions"},
            {},
        ),
        ("api_key.use", {"endpoint_path": "/v1/responses"}, {}),
        ("model.invoke", {"transport": "http"}, {}),
        ("network.egress", {}, {"domains": ["attacker.invalid"], "ports": [444]}),
    ],
)
def test_chat_only_mimo_rejects_untrusted_authority_endpoint_before_approval(
    tmp_path, permission_id, resource_update, request_config
):
    client = FakeMimoChatClient()
    output = io.StringIO()
    credential = "sk-malicious-canary-that-must-never-print"
    reporter = debug.SmokeReporter(
        output, secrets_to_hide=(*client.secrets_to_hide, credential)
    )
    runner = debug.ChatOnlySmokeRunner(
        client,
        {"chat_store": str(tmp_path / "chat" / "conversations.json")},
        prompt="create a short movie project",
        max_turns=3,
        reporter=reporter,
        provider_preflight=debug.isolated_smoke_provider_preflight(
            {"OPENCODE_ZEN_API_KEY": credential},
            profile_name=debug.MIMO_CHAT_PROFILE,
        ),
    )
    client.stream_count = 1
    request = client.get("/api/authority/requests", query={"status": "pending"})[
        "pending"
    ][0]
    request["permission_id"] = permission_id
    request["resource"].update(resource_update)
    if request_config:
        request["config"] = request_config

    if resource_update:
        expected_error = "refusing unexpected"
    else:
        expected_error = "automatic Authority approval is disabled"
    with pytest.raises(debug.SmokeRunnerError, match=expected_error):
        runner._approve_authority(request)
    assert not any("/approve" in call[1] for call in client.calls)
    assert credential not in output.getvalue()


@pytest.mark.parametrize(
    "permission_id", ["model.invoke", "api_key.use", "network.egress"]
)
def test_chat_only_mimo_never_auto_approves_fixed_provider_permissions(
    tmp_path, permission_id
):
    client = FakeMimoChatClient()
    reporter = debug.SmokeReporter(io.StringIO(), secrets_to_hide=client.secrets_to_hide)
    runner = debug.ChatOnlySmokeRunner(
        client,
        {"chat_store": str(tmp_path / "chat" / "conversations.json")},
        prompt="fixed Mimo chat",
        max_turns=3,
        reporter=reporter,
        provider_preflight=debug.isolated_smoke_provider_preflight(
            {"OPENCODE_ZEN_API_KEY": "ephemeral-key"},
            profile_name=debug.MIMO_CHAT_PROFILE,
        ),
    )
    client.stream_count = 1
    request = client.get("/api/authority/requests", query={"status": "pending"})[
        "pending"
    ][0]
    request["permission_id"] = permission_id

    with pytest.raises(
        debug.SmokeRunnerError, match="automatic Authority approval is disabled"
    ):
        runner._approve_authority(request)

    assert not any("/approve" in call[1] for call in client.calls)


@contextlib.contextmanager
def _local_stream_server(responder):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(content_length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            responder(self)

        def log_message(self, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _capture_stream_workers(monkeypatch):
    workers = []
    original = debug._start_debug_stream_worker

    def start(request):
        process = original(request)
        workers.append(process)
        return process

    monkeypatch.setattr(debug, "_start_debug_stream_worker", start)
    return workers


def test_debug_stream_stops_after_first_terminal_event(monkeypatch):
    def respond(handler):
        handler.wfile.write(
            b'data: {"type":"done","message":{"finish_reason":"stop"}}\n\n'
            b'data: {"type":"done","message":{"finish_reason":"stop"}}\n\n'
        )
        handler.wfile.flush()

    workers = _capture_stream_workers(monkeypatch)
    with _local_stream_server(respond) as base_url:
        client = debug.DebugHttpClient(base_url, "api", "browser")
        events = list(client.stream("/stream", {"message": {"content": "test"}}))

    assert [event["type"] for event in events] == ["done"]
    assert len(workers) == 1
    assert workers[0].poll() is not None


def test_debug_stream_real_silent_http_is_bounded_and_reaps_child(monkeypatch):
    connected = threading.Event()

    def respond(_handler):
        connected.set()
        time.sleep(1)

    workers = _capture_stream_workers(monkeypatch)
    with _local_stream_server(respond) as base_url:
        client = debug.DebugHttpClient(
            base_url, "api-secret", "browser-secret", stream_timeout=0.1
        )
        started = time.monotonic()
        with pytest.raises(debug.DebugApiError, match="inactive for 0.1 seconds"):
            list(client.stream("/stream", {}))
        elapsed = time.monotonic() - started

    assert connected.is_set()
    assert elapsed < 0.8
    assert len(workers) == 1
    assert workers[0].poll() is not None


def test_debug_stream_heartbeats_extend_deadline_and_delta_is_incremental(monkeypatch):
    release_terminal = threading.Event()

    def respond(handler):
        for _ in range(3):
            handler.wfile.write(b": heartbeat\n\n")
            handler.wfile.flush()
            time.sleep(0.1)
        handler.wfile.write(b'data: {"type":"delta","delta":"ready"}\n\n')
        handler.wfile.flush()
        release_terminal.wait(timeout=1)
        try:
            handler.wfile.write(
                b'data: {"type":"done","message":{"finish_reason":"stop"}}\n\n'
            )
            handler.wfile.flush()
        except BrokenPipeError:
            pass

    workers = _capture_stream_workers(monkeypatch)
    with _local_stream_server(respond) as base_url:
        client = debug.DebugHttpClient(base_url, "api", "browser", stream_timeout=0.2)
        stream = client.stream("/stream", {})
        assert next(stream) == {"type": "delta", "delta": "ready"}
        assert workers[0].poll() is None
        release_terminal.set()
        assert [event["type"] for event in stream] == ["done"]

    assert workers[0].poll() is not None


def test_debug_stream_consumer_close_terminates_and_reaps_child(monkeypatch):
    def respond(handler):
        handler.wfile.write(b'data: {"type":"delta","delta":"first"}\n\n')
        handler.wfile.flush()
        time.sleep(1)

    workers = _capture_stream_workers(monkeypatch)
    with _local_stream_server(respond) as base_url:
        client = debug.DebugHttpClient(base_url, "api", "browser", stream_timeout=2)
        stream = client.stream("/stream", {})
        assert next(stream)["type"] == "delta"
        stream.close()

    assert len(workers) == 1
    assert workers[0].poll() is not None
    assert not any(
        thread.name == "defaultspack-debug-stream-reader" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_viewer_smoke_stops_before_start_when_parent_provider_env_is_missing(monkeypatch):
    monkeypatch.setattr(debug, "_has_live_pty", lambda: True)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    started = []
    monkeypatch.setattr(debug, "start_viewer_dev", lambda *args, **kwargs: started.append(True))

    result = debug.viewer_smoke_computer_use(
        type("Args", (), {"viewer_broker_port": 18770})()
    )

    assert result == {
        "ok": False,
        "error": "PROVIDER_ENV_NOT_PRESENT",
        "provider_preflight": {
            "provider_id": "cerebras",
            "model": "cerebras/gemma-4-31b",
            "credential_present": False,
            "credential_source": "inherited_env",
            "credential_persisted": False,
            "allow_custom_base_url": False,
        },
    }
    assert started == []


def test_launch_canary_never_reaches_isolated_artifacts(tmp_path, monkeypatch):
    canary = "canary-cerebras-artifact-secret-abc789"
    connection_path = tmp_path / "connection.json"
    connection = {"port": 18770}
    connection_path.write_text(json.dumps(connection), encoding="utf-8")
    state_root = tmp_path / "viewer-smoke" / "defaultspack_state"
    run_root = tmp_path / "runs"
    latest = run_root / "latest.json"
    monkeypatch.setattr(debug, "RUN_ROOT", run_root)
    monkeypatch.setattr(debug, "LATEST_JSON", latest)
    monkeypatch.setattr(
        debug,
        "load_desktop_app",
        lambda: {"command": "python desktop_app.py", "env": {}},
    )
    monkeypatch.setattr(
        debug,
        "load_connection",
        lambda path, **kwargs: (connection, {"ok": True, "connection": {"port": 18770}}),
    )
    monkeypatch.setattr(debug, "port_is_open", lambda port: False)
    monkeypatch.setattr(debug, "wait_for_owned_defaultspack_health", lambda *args: True)
    monkeypatch.setattr(debug, "status", lambda args: {"ok": True})
    captured = {}

    class Process:
        pid = 4321
        stdout = io.StringIO("")

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(debug.subprocess, "Popen", fake_popen)
    result = debug.launch(
        type(
            "Args",
            (),
            {
                "port": 18771,
                "connection": str(connection_path),
                "viewer_broker_port": 18770,
                "user_data": str(state_root / "user_data"),
                "wait_seconds": 1.0,
                "allow_no_broker": False,
                "defaultspack_debug_run_id": "debug-123-safe",
                "defaultspack_debug_nonce": "safe_nonce-1234567890",
                "defaultspack_debug_state_root": state_root,
                "defaultspack_kernel_port": 18772,
                "isolated_provider_parent_env": {
                    "CEREBRAS_API_KEY": canary,
                    "CEREBRAS_BASE_URL": "https://untrusted.invalid/v1",
                },
            },
        )(),
        include_process=True,
    )
    result.pop("_log_tee").join()

    assert result["ok"] is True
    assert captured["env"]["CEREBRAS_API_KEY"] == canary
    assert "CEREBRAS_BASE_URL" not in captured["env"]
    assert canary not in " ".join(captured["argv"])
    for path in run_root.rglob("*"):
        if path.is_file():
            assert canary.encode("utf-8") not in path.read_bytes()


def test_owned_defaultspack_readiness_rejects_foreign_listener(monkeypatch):
    class Process:
        pid = 100

        def poll(self):
            return None

    monkeypatch.setattr(debug, "lsof_listener", lambda port: {"pid": "101", "command": "python"})
    monkeypatch.setattr(debug, "http_status", lambda url: {"ok": True})
    monkeypatch.setattr(debug.time, "sleep", lambda seconds: None)
    assert debug.wait_for_owned_defaultspack_health(18771, Process(), 0.001) is False


def test_owned_defaultspack_readiness_accepts_matching_child(monkeypatch):
    class Process:
        pid = 100

        def poll(self):
            return None

    monkeypatch.setattr(debug, "lsof_listener", lambda port: {"pid": "100", "command": "python"})
    monkeypatch.setattr(debug, "http_status", lambda url: {"ok": True})
    assert debug.wait_for_owned_defaultspack_health(18771, Process(), 1.0) is True


def test_launch_readiness_failure_stops_owned_child_and_does_not_publish_latest(
    tmp_path, monkeypatch
):
    run_root = tmp_path / "runs"
    latest = run_root / "latest.json"
    monkeypatch.setattr(debug, "RUN_ROOT", run_root)
    monkeypatch.setattr(debug, "LATEST_JSON", latest)
    monkeypatch.setattr(
        debug,
        "load_desktop_app",
        lambda: {"command": "python desktop_app.py", "env": {}},
    )
    monkeypatch.setattr(
        debug,
        "load_connection",
        lambda *_args, **_kwargs: ({}, {"ok": False, "connection": {}}),
    )
    monkeypatch.setattr(debug, "port_is_open", lambda _port: False)
    monkeypatch.setattr(
        debug, "wait_for_owned_defaultspack_health", lambda *_args: False
    )
    monkeypatch.setattr(debug, "status", lambda _args: {"ok": False})

    class Process:
        pid = 4321
        stdout = io.StringIO("")

        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

    process = Process()
    monkeypatch.setattr(debug.subprocess, "Popen", lambda *_args, **_kwargs: process)

    result = debug.launch(
        type(
            "Args",
            (),
            {
                "port": 18771,
                "connection": None,
                "viewer_broker_port": None,
                "user_data": None,
                "wait_seconds": 0.01,
                "allow_no_broker": True,
            },
        )()
    )

    assert result["ok"] is False
    assert result["cleanup"]["stopped"] is True
    assert process.terminated is True
    assert latest.exists() is False
    assert Path(result["launch"]["manifest_path"]).is_file()


def test_unique_run_directories_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(debug, "RUN_ROOT", tmp_path / "runs")
    first_id, first = debug.create_unique_run_dir("launch")
    second_id, second = debug.create_unique_run_dir("launch")

    assert first_id != second_id
    assert first != second
    assert first.is_dir() and second.is_dir()
    assert first.parent == second.parent == tmp_path / "runs"


def test_owned_stop_refuses_manifest_that_does_not_match_run_root(tmp_path, monkeypatch):
    run_root = tmp_path / "runs"
    foreign_root = tmp_path / "foreign" / "launch-foreign"
    foreign_root.mkdir(parents=True)
    artifact = {
        "schema": "rumi.defaultspack-debug-run.v1",
        "run_id": "launch-foreign",
        "run_dir": str(foreign_root),
        "manifest_path": str(foreign_root / "manifest.json"),
        "pid": 123,
        "port": 18771,
    }
    (foreign_root / "manifest.json").write_text(json.dumps(artifact))
    monkeypatch.setattr(debug, "RUN_ROOT", run_root)
    monkeypatch.setattr(debug, "latest_run", lambda: artifact)
    killed = []
    monkeypatch.setattr(debug.os, "kill", lambda *args: killed.append(args))

    result = debug.stop_latest_owned_launch(type("Args", (), {})())

    assert result["ok"] is False
    assert result["stopped"] is False
    assert killed == []


def test_owned_stop_signals_only_manifest_pid_when_it_owns_listener(tmp_path, monkeypatch):
    run_root = tmp_path / "runs"
    run_dir = run_root / "launch-owned"
    run_dir.mkdir(parents=True)
    manifest = run_dir / "manifest.json"
    artifact = {
        "schema": "rumi.defaultspack-debug-run.v1",
        "run_id": "launch-owned",
        "run_dir": str(run_dir),
        "manifest_path": str(manifest),
        "pid": 321,
        "process_start_marker": "Mon Jul 20 12:00:00 2026",
        "port": 18771,
    }
    manifest.write_text(json.dumps(artifact))
    monkeypatch.setattr(debug, "RUN_ROOT", run_root)
    monkeypatch.setattr(debug, "latest_run", lambda: artifact)
    monkeypatch.setattr(debug, "lsof_listener", lambda _port: {"pid": "321"})
    monkeypatch.setattr(
        debug, "process_start_marker", lambda _pid: "Mon Jul 20 12:00:00 2026"
    )
    running = iter([True, False, False])
    monkeypatch.setattr(debug, "pid_is_running", lambda _pid: next(running))
    signals = []
    monkeypatch.setattr(debug.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    result = debug.stop_latest_owned_launch(type("Args", (), {})())

    assert result == {
        "ok": True,
        "stopped": True,
        "forced": False,
        "run_id": "launch-owned",
    }
    assert signals == [(321, debug.signal.SIGTERM)]


def test_keep_running_viewer_pair_persists_all_owned_identities(tmp_path, monkeypatch):
    class Process:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    run_root = tmp_path / "runs"
    supervisor_dir = run_root / "viewer-smoke-owned"
    launch_dir = run_root / "launch-owned"
    connection_path = (
        supervisor_dir / "viewer_user_data" / "host_broker" / "connection.json"
    )
    connection_path.parent.mkdir(parents=True)
    launch_dir.mkdir(parents=True)
    default_manifest = launch_dir / "manifest.json"
    defaultspack_launch = {
        "schema": "rumi.defaultspack-debug-run.v1",
        "run_id": "launch-owned",
        "run_dir": str(launch_dir),
        "manifest_path": str(default_manifest),
        "pid": 103,
        "process_start_marker": "default-start",
        "port": 18771,
    }
    default_manifest.write_text(json.dumps(defaultspack_launch), encoding="utf-8")
    connection_path.write_text(
        json.dumps(
            {
                "pid": 102,
                "port": 18770,
                "instance_nonce": "owned-nonce",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(debug, "RUN_ROOT", run_root)
    monkeypatch.setattr(debug, "LATEST_JSON", run_root / "latest.json")
    monkeypatch.setattr(
        debug,
        "process_start_marker",
        lambda pid: {
            101: "launcher-start",
            102: "broker-start",
            103: "default-start",
        }.get(pid, ""),
    )
    monkeypatch.setattr(
        debug,
        "process_group_id",
        lambda pid: {101: 501, 102: 501, 103: 503}.get(pid),
    )
    monkeypatch.setattr(
        debug,
        "lsof_listener",
        lambda port: {"pid": "102"} if port == 18770 else {"pid": "103"},
    )

    pair = debug.persist_keep_running_viewer_pair(
        supervisor_dir,
        viewer_process=Process(101),
        defaultspack_process=Process(103),
        defaultspack_launch=defaultspack_launch,
        connection_path=connection_path,
        broker_port=18770,
        instance_nonce="owned-nonce",
    )

    assert pair["viewer"] == {
        "connection_path": str(connection_path.resolve()),
        "instance_nonce": "owned-nonce",
        "broker_port": 18770,
        "broker_listener_pid": 102,
        "broker_pid": 102,
        "broker_start_marker": "broker-start",
        "broker_process_group": 501,
        "launch_pid": 101,
        "launch_start_marker": "launcher-start",
        "launch_process_group": 501,
    }
    assert pair["defaultspack"]["process_group"] == 503
    assert (supervisor_dir / "viewer-pair-manifest.json").is_file()
    latest = json.loads((run_root / "latest.json").read_text(encoding="utf-8"))
    assert latest["viewer_pair_manifest"] == pair["manifest_path"]
    assert latest["pid"] == 103


def test_owned_pair_stop_removes_owned_viewer_children_without_signaling_shared_group(
    tmp_path, monkeypatch
):
    run_root = tmp_path / "runs"
    supervisor_dir = run_root / "viewer-smoke-owned"
    launch_dir = run_root / "launch-owned"
    supervisor_dir.mkdir(parents=True)
    launch_dir.mkdir(parents=True)
    default_manifest = launch_dir / "manifest.json"
    defaultspack = {
        "schema": "rumi.defaultspack-debug-run.v1",
        "run_id": "launch-owned",
        "run_dir": str(launch_dir),
        "manifest_path": str(default_manifest),
        "pid": 103,
        "process_start_marker": "default-start",
        "port": 18771,
        "process_group": 503,
    }
    default_manifest.write_text(json.dumps(defaultspack), encoding="utf-8")
    connection_path = (
        supervisor_dir / "viewer_user_data" / "host_broker" / "connection.json"
    )
    connection_path.parent.mkdir(parents=True)
    connection_path.write_text("{}", encoding="utf-8")
    pair_manifest = supervisor_dir / "viewer-pair-manifest.json"
    pair = {
        "schema": "rumi.viewer-defaultspack-debug-pair.v1",
        "run_id": "viewer-smoke-owned",
        "run_dir": str(supervisor_dir),
        "manifest_path": str(pair_manifest),
        "viewer": {
            "connection_path": str(connection_path),
            "instance_nonce": "owned-nonce",
            "broker_port": 18770,
            "broker_listener_pid": 102,
            "broker_pid": 102,
            "broker_start_marker": "broker-start",
            "broker_process_group": 501,
            "launch_pid": 101,
            "launch_start_marker": "launcher-start",
            "launch_process_group": 501,
        },
        "defaultspack": defaultspack,
    }
    pair_manifest.write_text(json.dumps(pair), encoding="utf-8")
    latest = {**defaultspack, "viewer_pair_manifest": str(pair_manifest)}
    running = {101, 102, 103, 999}
    markers = {101: "launcher-start", 102: "broker-start", 103: "default-start"}
    groups = {101: 501, 102: 501, 103: 503, 999: 501}
    signals = []

    monkeypatch.setattr(debug, "RUN_ROOT", run_root)
    monkeypatch.setattr(debug, "latest_run", lambda: latest)
    monkeypatch.setattr(debug, "pid_is_running", lambda pid: pid in running)
    monkeypatch.setattr(debug, "process_start_marker", lambda pid: markers.get(pid, ""))
    monkeypatch.setattr(debug, "process_group_id", lambda pid: groups.get(pid))

    def fake_kill(pid, sig):
        signals.append((pid, sig))
        if sig == debug.signal.SIGTERM:
            running.discard(pid)

    monkeypatch.setattr(debug.os, "kill", fake_kill)

    result = debug.stop_latest_owned_launch(type("Args", (), {})())

    assert result["ok"] is True
    assert result["stopped"] is True
    assert [pid for pid, _sig in signals] == [102, 101, 103]
    assert 999 in running
    assert all(pid != 999 for pid, _sig in signals)
    assert all(item[1] == debug.signal.SIGTERM for item in signals)


def test_wait_for_viewer_broker_classifies_clean_exit_before_connection_publish(tmp_path):
    class Process:
        def poll(self):
            return 0

    class LogTee:
        wry_detached_panic = False

        def join(self, timeout=1.0):
            return None

    with pytest.raises(debug.SmokeRunnerError, match="duplicate_instance_or_pre_setup_exit"):
        debug.wait_for_viewer_broker(
            tmp_path / "connection.json",
            Process(),
            LogTee(),
            timeout=1.0,
            expected_port=18770,
            expected_instance_nonce="nonce",
            launched_at=1,
        )


def test_launch_fails_closed_when_parent_port_disagrees_with_connection(tmp_path, monkeypatch):
    connection_path = tmp_path / "connection.json"
    connection_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RUMI_VIEWER_BROKER_PORT", "8770")
    monkeypatch.setattr(debug, "load_desktop_app", lambda: {"command": "python desktop_app.py", "env": {}})
    captured = {}

    def fake_load(path, *, expected_port=None):
        captured["expected_port"] = expected_port
        return {}, {"ok": False, "health": {"error": "invalid connection file: localhost port mismatch"}}

    monkeypatch.setattr(debug, "load_connection", fake_load)

    result = debug.launch(
        type(
            "Args",
            (),
            {
                "port": 8766,
                "connection": str(connection_path),
                "user_data": None,
                "wait_seconds": 1.0,
                "allow_no_broker": False,
            },
        )()
    )

    assert result["ok"] is False
    assert captured["expected_port"] == 8770


def test_direct_sequence_uses_background_only_approval_replay_and_boolean_sentinels(
    tmp_path, monkeypatch
):
    source_root = tmp_path / "source-artifacts"
    source_root.mkdir()
    viewer_root = tmp_path / "viewer_user_data"
    audit_path = viewer_root / "host_broker" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True)
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug.time, "sleep", lambda _seconds: None)

    class DirectClient:
        def __init__(self):
            self.api_token = "direct-api-token-must-not-print"
            self.browser_approval_token = "direct-browser-token-must-not-print"
            self._secrets = {self.api_token, self.browser_approval_token}
            self.pending = {}
            self.counter = 0
            self.calls = []
            self.screenshot_counter = 0

        @property
        def secrets_to_hide(self):
            return tuple(self._secrets)

        def hide_secrets(self, *values):
            self._secrets.update(str(value) for value in values if str(value or ""))

        def _audit(self, action, *, ok, token_present, approval_result):
            with audit_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "audit_id": f"host-audit-{self.counter:08d}",
                            "ts": self.counter,
                            "function_id": action,
                            "allowed": True,
                            "result_ok": ok,
                            "approval_token_present": token_present,
                            "approval_result": approval_result,
                            "args_summary": {
                                "pid": 98765,
                                "window_id": "must-not-print-window-id",
                                "title": "private window title",
                            },
                        }
                    )
                    + "\n"
                )

        def post(self, path, payload, **_kwargs):
            self.calls.append((path, payload))
            if path == "/api/coding/approvals/approve":
                request_id = payload["approval_request_id"]
                assert request_id in self.pending
                return {"approved": True, "token": f"one-shot-secret-{request_id}"}
            assert path == "/api/tools/browser-computer"
            action = payload["action"]
            action_payload = payload["payload"]
            if action == "computer.context":
                assert action_payload == {"include_windows": False}
                return {
                    "widget": {
                        "action": action,
                        "active_window": {
                            "app": "Codex",
                            "pid": 98765,
                            "window_id": "must-not-print-window-id",
                            "title": "private window title",
                        },
                    }
                }
            if action == "computer.select_window":
                assert action_payload == {
                    "app": "ChatGPT Atlas",
                    "focus": False,
                    "require_exact_binding": True,
                }
                return {
                    "widget": {
                        "action": action,
                            "selected": True,
                            "selection_exact_binding_required": True,
                            "selection_exact_binding_present": True,
                            "selection_authoritative_permission_source": "swift_host",
                            "selection_authoritative_permission_outcome": "permissions_ok",
                            "selection_secondary_permission_outcome": "skipped_non_authoritative",
                            "target_window": {
                            "app": "ChatGPT Atlas",
                            "pid": 12345,
                            "window_id": 67890,
                            "x": -1200,
                            "y": 40,
                            "width": 1180,
                            "height": 760,
                            "title": "binding-title-canary-must-not-print",
                        },
                    }
                }
            if action == "computer.probe_text_control":
                assert action_payload == {
                    "app": "ChatGPT Atlas",
                    "window": {
                        "app": "ChatGPT Atlas",
                        "pid": 12345,
                        "window_id": 67890,
                        "x": -1200,
                        "y": 40,
                        "width": 1180,
                        "height": 760,
                    },
                    "target_control": "browser_address",
                    "background": True,
                    "focus": False,
                    "include_screenshot": False,
                }
                return {
                    "widget": {
                        "action": action,
                        "probe_completed": True,
                        "semantic_control_ready": True,
                        "semantic_control_resolved": True,
                        "semantic_control_role_allowed": True,
                        "semantic_control_settable": True,
                        "semantic_traversal_order": "breadth_first",
                        "semantic_discovery_stage": "ready",
                        "semantic_scan_scope": "exact_window_descendants",
                        "semantic_ownership_proof": "window_descendant",
                        "semantic_window_scan_complete": True,
                        "semantic_window_scan_truncated": False,
                        "semantic_window_depth_truncated": False,
                        "semantic_actionable_scan_complete": True,
                        "semantic_final_candidate_count": 1,
                    }
                }
            token = action_payload.get("approval_token")
            if not token:
                self.counter += 1
                request_id = f"apr-{self.counter}"
                self.pending[request_id] = (action, dict(action_payload))
                self._audit(action, ok=False, token_present=False, approval_result="missing_token")
                return {
                    "widget": {
                        "action": action,
                        "approval_required": True,
                        "requires_approval": True,
                        "approval_request_id": request_id,
                    }
                }
            request_id = token.removeprefix("one-shot-secret-")
            expected_action, expected_payload = self.pending.pop(request_id)
            assert expected_action == action
            assert {
                key: value for key, value in action_payload.items() if key != "approval_token"
            } == expected_payload
            self._audit(action, ok=True, token_present=True, approval_result="approved")
            if action == "computer.screenshot":
                self.screenshot_counter += 1
                screenshot = source_root / f"shot-{self.screenshot_counter}.png"
                screenshot.write_bytes(b"fake-png")
                return {
                    "widget": {
                        "action": action,
                        "executed": True,
                        "screenshot_path": str(screenshot),
                        "target_window": {
                            "pid": 12345,
                            "window_id": 67890,
                            "title": "private title",
                        },
                    }
                }
            return {
                "widget": {
                    "action": action,
                    "executed": True,
                    "delivered": True,
                    "completion_verified": True,
                    "background": True,
                    "driver": "mac_swift_host",
                    "uses_physical_input": False,
                    "requires_foreground": False,
                    "can_parallel_user_work": True,
                    "edge_haze": {
                        "attempted": True,
                        "started": True,
                        "sequence_id": "private-sequence",
                    },
                    "app": "ChatGPT Atlas",
                    "url": "https://private.example.test/private?q=secret",
                }
            }

    client = DirectClient()
    output = io.StringIO()
    reporter = debug.SmokeReporter(output, secrets_to_hide=client.secrets_to_hide)

    def delegated_approval(client, reporter, action, payload):
        requested = client.post(
            "/api/tools/browser-computer",
            {"action": action, "payload": dict(payload)},
        )
        request_id = debug._direct_approval_request_id(debug._direct_widget(requested))
        decision = client.post(
            "/api/coding/approvals/approve",
            {"approval_request_id": request_id},
        )
        token = decision["token"]
        client.hide_secrets(token)
        reporter.hide_secrets(token)
        replay_started_at = time.time()
        replay = client.post(
            "/api/tools/browser-computer",
            {
                "action": action,
                "payload": {**payload, "approval_token": token},
            },
        )
        return debug._direct_widget(replay), True, replay_started_at

    monkeypatch.setattr(debug, "_direct_approved_widget", delegated_approval)
    result = debug.direct_computer_use_sequence(
        client,
        run_dir=tmp_path / "run",
        viewer_user_data_root=viewer_root,
        direct_artifact_root=source_root,
        reporter=reporter,
        run_nonce="test-nonce",
    )

    assert result["ok"] is True
    assert result["provider_used"] is False
    assert result["chat_used"] is False
    assert result["model_used"] is False
    assert result["screenshot_evidence_captured"] is True
    assert result["effect_verified"] is False
    assert result["visual_inspection_required"] is True
    high_risk_requests = [
        body
        for path, body in client.calls
        if path == "/api/tools/browser-computer"
        and body["action"] in {"computer.type", "computer.screenshot"}
    ]
    for body in high_risk_requests:
        action = body["action"]
        clean_payload = {
            key: value for key, value in body["payload"].items() if key != "approval_token"
        }
        assert "approved" not in clean_payload
        assert "fallback" not in clean_payload
        assert "foreground" not in clean_payload
        if action == "computer.type":
            assert clean_payload["app"] == "ChatGPT Atlas"
            assert clean_payload["background"] is True
            assert clean_payload["focus"] is False
            assert clean_payload["include_screenshot"] is False
            assert clean_payload["target_control"] == "browser_address"
            assert clean_payload["window"] == {
                "app": "ChatGPT Atlas",
                "pid": 12345,
                "window_id": 67890,
                "x": -1200,
                "y": 40,
                "width": 1180,
                "height": 760,
            }
        if action == "computer.screenshot":
            assert clean_payload == {
                "app": "ChatGPT Atlas",
                "window": {
                    "app": "ChatGPT Atlas",
                    "pid": 12345,
                    "window_id": 67890,
                    "x": -1200,
                    "y": 40,
                    "width": 1180,
                    "height": 760,
                },
            }
    replay_tokens = [
        str(body["payload"].get("approval_token"))
        for body in high_risk_requests
        if body["payload"].get("approval_token")
    ]
    assert len(replay_tokens) == 2
    assert len(set(replay_tokens)) == 2
    printed = output.getvalue()
    for secret in client._secrets:
        assert secret not in printed
    assert "98765" not in printed
    assert "12345" not in printed
    assert "67890" not in printed
    assert "must-not-print-window-id" not in printed
    assert "private window title" not in printed
    assert "binding-title-canary-must-not-print" not in printed
    assert "private.example.test" not in printed
    assert "private-sequence" not in printed
    assert str(tmp_path) not in printed
    assert "rumi-background-delivery-test-nonce" not in printed
    copied = list((tmp_path / "run" / "evidence" / "screenshots").glob("*.png"))
    assert len(copied) == 1
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in copied)


def test_direct_harness_never_auto_approves_mutation():
    with pytest.raises(debug.SmokeRunnerError, match="automatic smoke approval is disabled"):
        debug._direct_approved_widget(
            object(),
            object(),
            "computer.type",
            {"text": "secret"},
        )


def test_direct_probe_only_ready_ax_text_area_contract_stops_before_approval_mutation_screenshot_and_host_audit(
    tmp_path, monkeypatch
):
    calls = []
    output = io.StringIO()

    def unapproved(_client, action, payload):
        calls.append((action, dict(payload)))
        if action == "computer.context":
            return {
                "action": action,
                "active_window": {"app": "Codex"},
            }
        if action == "computer.select_window":
            assert payload == {
                "app": "ChatGPT Atlas",
                "focus": False,
                "require_exact_binding": True,
            }
            return _exact_select_widget()
        assert action == "computer.probe_text_control"
        # Selector roles belong to the defaultspack/native boundary.  The
        # Viewer harness must retain its closed, nonforeground probe contract
        # as the browser-address selector gains AXTextArea support.
        assert payload == {
            "app": "ChatGPT Atlas",
            "window": {
                "app": "ChatGPT Atlas",
                "pid": 123,
                "window_id": 456,
                "x": -10,
                "y": 20,
                "width": 1000,
                "height": 700,
            },
            "target_control": "browser_address",
            "background": True,
            "focus": False,
            "include_screenshot": False,
        }
        return _ready_probe_widget()

    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", unapproved)
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail("probe-only must not request approval"),
    )
    monkeypatch.setattr(
        debug,
        "_validated_direct_host_audit",
        lambda *_args, **_kwargs: pytest.fail("probe-only must not read host audit"),
    )
    monkeypatch.setattr(
        debug,
        "_read_host_audit_since",
        lambda *_args, **_kwargs: pytest.fail("probe-only must not read host audit"),
    )
    monkeypatch.setattr(
        debug,
        "copy_direct_screenshot_artifacts",
        lambda *_args, **_kwargs: pytest.fail("probe-only must not capture screenshots"),
    )

    result = debug.direct_computer_use_sequence(
        object(),
        run_dir=tmp_path / "run",
        viewer_user_data_root=tmp_path / "viewer",
        direct_artifact_root=tmp_path / "artifacts",
        reporter=debug.SmokeReporter(output),
        run_nonce="probe-only",
        probe_only=True,
    )

    assert result == {
        "ok": True,
        "provider_used": False,
        "chat_used": False,
        "model_used": False,
        "probe_only": True,
        "probe_completed": True,
        "semantic_control_ready": True,
        "frontmost_non_atlas": True,
        "frontmost_unchanged": True,
        "screenshot_evidence_captured": False,
        "effect_verified": False,
        "visual_inspection_required": False,
        "steps": [],
        "host_audit_present": False,
        "host_audit": [],
    }
    assert {action for action, _payload in calls} == {
        "computer.context",
        "computer.select_window",
        "computer.probe_text_control",
    }
    assert not any(
        action in {"computer.type", "computer.screenshot"}
        for action, _payload in calls
    )
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[-1]["event"] == "direct_summary"
    assert records[-1]["probe_only"] is True
    assert records[-1]["steps"] == []


@pytest.mark.parametrize(
    "binding",
    [
        {"app": "ChatGPT", "pid": 1, "window_id": 2, "x": 0, "y": 0, "width": 10, "height": 10},
        {"app": "ChatGPT Atlas", "pid": 0, "window_id": 2, "x": 0, "y": 0, "width": 10, "height": 10},
        {"app": "ChatGPT Atlas", "pid": 1, "window_id": "private-id", "x": 0, "y": 0, "width": 10, "height": 10},
        {"app": "ChatGPT Atlas", "pid": 1, "window_id": 2, "x": 0, "y": 0, "width": 0, "height": 10},
    ],
)
def test_direct_atlas_binding_fails_closed_without_exact_safe_window(binding):
    with pytest.raises(debug.SmokeRunnerError, match="SCREENSHOT_TARGET_UNAVAILABLE"):
        debug._direct_atlas_window_binding(binding)


def test_direct_atlas_binding_excludes_title_and_unknown_values():
    binding = debug._direct_atlas_window_binding(
        {
            "app": "ChatGPT Atlas",
            "pid": 123,
            "window_id": 456,
            "x": -10,
            "y": 20,
            "width": 1000,
            "height": 700,
            "title": "private-canary",
            "unknown": "private-value",
        }
    )

    assert binding == {
        "app": "ChatGPT Atlas",
        "pid": 123,
        "window_id": 456,
        "x": -10,
        "y": 20,
        "width": 1000,
        "height": 700,
    }


def _exact_select_widget(**overrides):
    widget = {
        "action": "computer.select_window",
        "selected": True,
        "selection_selected": True,
        "selection_exact_binding_required": True,
        "selection_exact_binding_present": True,
        "selection_app_verified": True,
        "selection_pid_present": True,
        "selection_window_id_present": True,
        "selection_geometry_complete": True,
        "selection_geometry_integral": True,
        "selection_focus_requested": False,
        "selection_focus_attempted": False,
        "selection_failure_stage": "none",
        "selection_authoritative_permission_source": "swift_host",
        "selection_authoritative_permission_outcome": "permissions_ok",
        "selection_secondary_permission_outcome": "skipped_non_authoritative",
        "target_window": {
            "app": "ChatGPT Atlas",
            "pid": 123,
            "window_id": 456,
            "x": -10,
            "y": 20,
            "width": 1000,
            "height": 700,
        },
    }
    widget.update(overrides)
    return widget


def _eligible_selection_miss(**overrides):
    widget = {
        "action": "computer.select_window",
        "selected": False,
        "is_error": True,
        "error_code": "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED",
        "selection_selected": False,
        "selection_exact_binding_required": True,
        "selection_exact_binding_present": False,
        "selection_focus_requested": False,
        "selection_focus_attempted": False,
        "selection_activation_policy": "not_requested",
        "selection_failure_stage": "app_match",
        "selection_swift_helper_response_contract": "valid_success",
        "selection_swift_helper_contract_version_class": "expected",
        "selection_inventory_instrumentation_consistent": True,
        "selection_nsworkspace_target_process_present": True,
        "selection_later_source_target_match_present": False,
        "selection_inventory_diagnostic_stage": "complete",
        "selection_inventory_diagnostic_outcome": "process_present_no_window",
    }
    widget.update(overrides)
    return widget


def _ready_probe_widget(**overrides):
    widget = {
        "action": "computer.probe_text_control",
        "probe_completed": True,
        "semantic_control_ready": True,
        "semantic_control_resolved": True,
        "semantic_control_role_allowed": True,
        "semantic_control_settable": True,
        "semantic_traversal_order": "breadth_first",
        "semantic_discovery_stage": "ready",
        "semantic_scan_scope": "exact_window_descendants",
        "semantic_ownership_proof": "window_descendant",
        "semantic_window_scan_complete": True,
        "semantic_window_scan_truncated": False,
        "semantic_window_depth_truncated": False,
        "semantic_actionable_scan_complete": True,
        "semantic_final_candidate_count": 1,
    }
    widget.update(overrides)
    return widget


def test_direct_probe_requires_actionable_scan_complete_even_when_a_candidate_is_ready():
    with pytest.raises(debug.DirectProbeContractError) as raised:
        debug._direct_probe_contract(
            _ready_probe_widget(semantic_actionable_scan_complete=False)
        )

    assert raised.value.error_code == "PROBE_TRANSPORT_CONTRACT_INVALID"


def test_direct_probe_contract_requires_action_owned_complete_ready_result():
    facts = debug._direct_probe_contract(_ready_probe_widget())

    assert facts["probe_completed"] is True
    assert facts["semantic_control_ready"] is True
    assert facts["semantic_discovery_stage"] == "ready"

    nested = {
        "action": "computer.probe_text_control",
        "probe_completed": True,
        "semantic_control_ready": False,
        "semantic_discovery_stage": "role_absent",
        "result": _ready_probe_widget(),
    }
    with pytest.raises(debug.DirectProbeContractError) as raised:
        debug._direct_probe_contract(nested)
    assert raised.value.error_code == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"


def test_direct_probe_recovered_clean_uses_authoritative_final_ready_facts():
    facts = debug._direct_probe_contract(
        _ready_probe_widget(
            semantic_stale_recovery_eligible=True,
            semantic_stale_recovery_attempted=True,
            semantic_stale_recovery_window_rebound=True,
            semantic_stale_recovery_window_stable=True,
            semantic_stale_recovery_second_pass_complete=True,
            semantic_stale_recovery_succeeded=True,
            semantic_discovery_pass_count=2,
            semantic_stale_recovery_restart_count=1,
            semantic_first_pass_stale_count=1,
            semantic_second_pass_stale_count=0,
            semantic_first_pass_unknown_branch_count=1,
            semantic_second_pass_unknown_branch_count=0,
            semantic_first_pass_nodes_visited_count=31,
            semantic_second_pass_nodes_visited_count=47,
            semantic_second_pass_final_candidate_count=1,
            semantic_stale_recovery_outcome="recovered_clean",
        )
    )

    assert facts["semantic_stale_recovery_outcome"] == "recovered_clean"
    assert facts["semantic_stale_recovery_succeeded"] is True
    assert facts["semantic_first_pass_nodes_visited_count"] == 31
    assert facts["semantic_second_pass_nodes_visited_count"] == 47
    assert facts["semantic_discovery_stage"] == "ready"
    assert facts["semantic_window_scan_complete"] is True
    assert facts["semantic_final_candidate_count"] == 1
    assert facts["semantic_ownership_proof"] == "window_descendant"


def test_direct_probe_recovered_clean_ready_reaches_one_write_approval(
    tmp_path, monkeypatch
):
    approved_actions = []
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")

    def unapproved(_client, action, _payload):
        if action == "computer.select_window":
            return _exact_select_widget()
        assert action == "computer.probe_text_control"
        return _ready_probe_widget(
            semantic_stale_recovery_eligible=True,
            semantic_stale_recovery_attempted=True,
            semantic_stale_recovery_window_rebound=True,
            semantic_stale_recovery_window_stable=True,
            semantic_stale_recovery_second_pass_complete=True,
            semantic_stale_recovery_succeeded=True,
            semantic_discovery_pass_count=2,
            semantic_stale_recovery_restart_count=1,
            semantic_first_pass_stale_count=1,
            semantic_second_pass_stale_count=0,
            semantic_stale_recovery_outcome="recovered_clean",
        )

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", unapproved)

    class ApprovalReached(Exception):
        pass

    def approved(_client, _reporter, action, _payload):
        approved_actions.append(action)
        raise ApprovalReached

    monkeypatch.setattr(debug, "_direct_approved_widget", approved)

    with pytest.raises(ApprovalReached):
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(io.StringIO()),
            run_nonce="stale-recovered-clean",
        )

    assert approved_actions == ["computer.type"]


def test_direct_probe_second_pass_incomplete_is_content_free_and_stops_before_write_approval(
    tmp_path, monkeypatch
):
    output = io.StringIO()
    calls = []
    identities = iter(("codex", "codex", "codex", "codex"))
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: next(identities))

    def unapproved(_client, action, payload):
        calls.append((action, payload))
        if action == "computer.select_window":
            return _exact_select_widget()
        assert action == "computer.probe_text_control"
        return {
            "action": action,
            "probe_completed": True,
            "semantic_control_ready": False,
            "error_code": "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
            "semantic_discovery_stage": "scan_incomplete",
            "semantic_traversal_order": "breadth_first",
            "semantic_scan_scope": "exact_window_descendants",
            "semantic_window_nodes_visited_count": 255,
            "semantic_window_scan_complete": False,
            "semantic_window_scan_truncated": True,
            "semantic_window_depth_truncated": False,
            "semantic_children_failure_under_toolbar": True,
            "semantic_children_count_known": False,
            "semantic_children_unknown_branch_count": 1,
            "semantic_children_failure_class": "cannot_complete",
            "semantic_children_incomplete_branch_class": "container",
            "semantic_stale_recovery_eligible": True,
            "semantic_stale_recovery_attempted": True,
            "semantic_stale_recovery_window_rebound": True,
            "semantic_stale_recovery_window_stable": True,
            "semantic_stale_recovery_second_pass_complete": False,
            "semantic_stale_recovery_succeeded": False,
            "semantic_discovery_pass_count": 2,
            "semantic_stale_recovery_restart_count": 1,
            "semantic_first_pass_stale_count": 1,
            "semantic_second_pass_stale_count": 1,
            "semantic_first_pass_unknown_branch_count": 1,
            "semantic_second_pass_unknown_branch_count": 1,
            "semantic_first_pass_nodes_visited_count": 31,
            "semantic_second_pass_nodes_visited_count": 29,
            "semantic_second_pass_final_candidate_count": 0,
            "semantic_stale_recovery_outcome": "second_pass_incomplete",
            "semantic_unlisted_mutation_ready_count": 1,
            "title": "private-title-must-not-print",
            "pid": 987654,
        }

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", unapproved)
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail(
            "incomplete probe must stop before write approval"
        ),
    )

    with pytest.raises(debug.DirectProbeContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(output),
            run_nonce="probe-incomplete",
        )

    assert raised.value.error_code == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    assert "error_code" not in raised.value.facts
    assert [action for action, _payload in calls] == [
        "computer.select_window",
        "computer.probe_text_control",
    ]
    rendered = output.getvalue()
    assert "viewer_direct_probe_failed" in rendered
    assert "private-title-must-not-print" not in rendered
    assert "987654" not in rendered
    [probe_event] = [
        json.loads(line)
        for line in rendered.splitlines()
        if "viewer_direct_probe_failed" in line
    ]
    assert probe_event["error"] == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    assert probe_event["error_code"] == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    assert probe_event["semantic_children_failure_under_toolbar"] is True
    assert probe_event["semantic_children_unknown_branch_count"] == 1
    assert probe_event["semantic_children_failure_class"] == "cannot_complete"
    assert probe_event["semantic_stale_recovery_attempted"] is True
    assert probe_event["semantic_stale_recovery_succeeded"] is False
    assert probe_event["semantic_discovery_pass_count"] == 2
    assert probe_event["semantic_second_pass_stale_count"] == 1
    assert probe_event["semantic_stale_recovery_outcome"] == "second_pass_incomplete"

    supervisor_output = io.StringIO()
    debug.SmokeReporter(supervisor_output).emit(
        "viewer_direct_failed",
        ok=False,
        **debug._direct_failure_report(raised.value),
    )
    supervisor_event = json.loads(supervisor_output.getvalue())
    shared_fields = set(probe_event) - {"event", "ok"}
    assert {
        key: supervisor_event[key] for key in shared_fields
    } == {
        key: probe_event[key] for key in shared_fields
    }


def test_direct_probe_persistently_stale_subtree_stops_without_reobservation(
    tmp_path, monkeypatch
):
    output = io.StringIO()
    calls = []
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")

    def unapproved(_client, action, payload):
        calls.append((action, dict(payload)))
        if action == "computer.select_window":
            return _exact_select_widget()
        assert action == "computer.probe_text_control"
        return {
            "action": action,
            "probe_completed": True,
            "semantic_control_ready": False,
            "error_code": "TYPE_SEMANTIC_AX_SUBTREE_PERSISTENTLY_STALE",
            "semantic_discovery_stage": "scan_incomplete",
            "semantic_traversal_order": "breadth_first",
            "semantic_scan_scope": "exact_window_descendants",
            "semantic_window_scan_complete": False,
            "semantic_window_scan_truncated": True,
            "semantic_window_depth_truncated": False,
            "semantic_stale_recovery_final_scan_complete": False,
            "semantic_discovery_pass_count": 3,
            "semantic_stale_recovery_restart_count": 2,
            "semantic_stale_recovery_outcome": "final_pass_stale",
            "semantic_stale_reference_refresh_class": "same_stale_reference_returned",
            "semantic_stale_branch_comparison": "same_class_and_depth",
            "semantic_second_third_stale_reference_class": "same_parent_same_reference",
            "title": "private-title-must-not-print",
        }

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", unapproved)
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail("persistent stale probe must stop before write approval"),
    )

    with pytest.raises(debug.DirectProbeContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(output),
            run_nonce="persistent-stale-probe",
        )

    assert raised.value.error_code == "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
    assert raised.value.failure_stage == "contract_validation"
    assert raised.value.facts["semantic_control_ready"] is False
    assert [action for action, _payload in calls] == [
        "computer.select_window",
        "computer.probe_text_control",
    ]
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    [probe_event] = [
        record for record in records if record["event"] == "viewer_direct_probe_failed"
    ]
    assert probe_event["classification"] == "PROBE_PRECONDITION_FAILED"
    assert probe_event["failure_stage"] == "contract_validation"
    assert probe_event["semantic_control_ready"] is False
    assert probe_event["error_code"] == "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
    assert "private-title-must-not-print" not in output.getvalue()


def test_direct_probe_child_read_diagnostics_are_bounded_closed_facts():
    boolean_facts = {
        "semantic_children_failure_on_window_root": False,
        "semantic_children_failure_under_toolbar": True,
        "semantic_children_attribute_advertised": True,
        "semantic_children_count_known": True,
        "semantic_children_count_nonzero": False,
        "semantic_children_branch_proven_empty": True,
    }
    count_fields = (
        "semantic_children_read_success_count",
        "semantic_children_empty_count",
        "semantic_children_unsupported_count",
        "semantic_children_no_value_count",
        "semantic_children_cannot_complete_count",
        "semantic_children_invalid_element_count",
        "semantic_children_global_failure_count",
        "semantic_children_protocol_failure_count",
        "semantic_children_unknown_branch_count",
        "semantic_children_proven_empty_after_failure_count",
        "semantic_children_retry_attempted_count",
        "semantic_children_retry_recovered_count",
    )
    widget = {
        "action": "computer.probe_text_control",
        **boolean_facts,
        **{key: 10_000 for key in count_fields},
        "semantic_children_failure_class": "multiple",
        "semantic_children_incomplete_branch_class": "static_value",
        "semantic_children_ax_error_class": "illegal_argument",
        "semantic_children_structural_empty_proof": "count_zero",
        "semantic_stale_branch_scope": "selector_relevant_unknown",
        "semantic_stale_node_self_eligible": True,
        "semantic_stale_node_class": "text_control",
        "accessibility_trust_preflight": "granted",
        "semantic_children_private_role": "AXStaticText",
    }

    facts = debug._direct_probe_facts(widget)

    assert {key: facts[key] for key in boolean_facts} == boolean_facts
    assert {key: facts[key] for key in count_fields} == {
        key: 64 for key in count_fields
    }
    assert facts["semantic_children_failure_class"] == "multiple"
    assert facts["semantic_children_incomplete_branch_class"] == "static_value"
    assert facts["semantic_children_ax_error_class"] == "illegal_argument"
    assert facts["semantic_children_structural_empty_proof"] == "count_zero"
    assert facts["semantic_stale_branch_scope"] == "selector_relevant_unknown"
    assert facts["semantic_stale_node_self_eligible"] is True
    assert facts["semantic_stale_node_class"] == "text_control"
    assert facts["accessibility_trust_preflight"] == "granted"
    assert "semantic_children_private_role" not in facts


def test_direct_probe_navigation_order_fallback_diagnostics_are_bounded_closed_facts():
    widget = {
        "action": "computer.probe_text_control",
        "semantic_navigation_order_fallback_attempted_count": 999,
        "semantic_navigation_order_fallback_succeeded_count": 999,
        "semantic_navigation_order_recovered_invalid_count": 999,
        "semantic_navigation_order_page_read_count": 999,
        "semantic_navigation_order_fallback_outcome": "complete_children",
        "semantic_navigation_order_failure_class": "none",
        "semantic_navigation_order_ax_error_class": "invalid_element",
        "semantic_navigation_order_cardinality_class": "nine_to_64",
        "semantic_navigation_order_parent_proof": "all_direct",
        "semantic_navigation_order_count_stable": True,
        "semantic_navigation_order_complete": True,
        "semantic_navigation_order_raw_children": ["CANARY_AX_REFERENCE"],
    }

    facts = debug._direct_probe_facts(widget)

    assert facts["semantic_navigation_order_fallback_attempted_count"] == 8
    assert facts["semantic_navigation_order_fallback_succeeded_count"] == 8
    assert facts["semantic_navigation_order_recovered_invalid_count"] == 8
    assert facts["semantic_navigation_order_page_read_count"] == 16
    assert facts["semantic_navigation_order_fallback_outcome"] == "complete_children"
    assert facts["semantic_navigation_order_failure_class"] == "none"
    assert facts["semantic_navigation_order_ax_error_class"] == "invalid_element"
    assert facts["semantic_navigation_order_cardinality_class"] == "nine_to_64"
    assert facts["semantic_navigation_order_parent_proof"] == "all_direct"
    assert facts["semantic_navigation_order_count_stable"] is True
    assert facts["semantic_navigation_order_complete"] is True
    assert facts["semantic_counts_truncated"] is True
    assert "CANARY" not in str(facts)


def test_direct_probe_allowed_role_geometry_diagnostics_are_bounded_closed_facts():
    count_fields = (
        "semantic_allowed_ax_text_field_count",
        "semantic_allowed_ax_combo_box_count",
        "semantic_allowed_ax_text_area_count",
        "semantic_allowed_frame_inside_window_count",
        "semantic_allowed_region_x_match_count",
        "semantic_allowed_region_y_match_count",
    )
    widget = {
        "action": "computer.probe_text_control",
        **{key: 999 for key in count_fields},
        "semantic_allowed_role_class": "ax_text_area",
        "semantic_allowed_region_miss_axis": "y",
        "semantic_allowed_center_y_band": "upper_22_35",
        "semantic_allowed_width_band": "wide_40_80",
        "semantic_allowed_height_band": "shallow_0_15",
        "semantic_allowed_private_frame": {"x": 12345},
    }

    facts = debug._direct_probe_facts(widget)

    assert {key: facts[key] for key in count_fields} == {key: 8 for key in count_fields}
    assert facts["semantic_counts_truncated"] is True
    assert facts["semantic_allowed_role_class"] == "ax_text_area"
    assert facts["semantic_allowed_region_miss_axis"] == "y"
    assert facts["semantic_allowed_center_y_band"] == "upper_22_35"
    assert facts["semantic_allowed_width_band"] == "wide_40_80"
    assert facts["semantic_allowed_height_band"] == "shallow_0_15"
    assert "private" not in json.dumps(facts)
    assert "12345" not in json.dumps(facts)

    unknown = dict(widget)
    unknown.update({
        "semantic_allowed_ax_text_field_count": True,
        "semantic_allowed_role_class": "CANARY_ROLE",
        "semantic_allowed_region_miss_axis": "CANARY_AXIS",
        "semantic_allowed_center_y_band": 22,
    })
    invalid = debug._direct_probe_facts(unknown)
    assert "semantic_allowed_ax_text_field_count" not in invalid
    assert "semantic_allowed_role_class" not in invalid
    assert "semantic_allowed_region_miss_axis" not in invalid
    assert "semantic_allowed_center_y_band" not in invalid
    assert "CANARY" not in json.dumps(invalid)


def test_direct_probe_stale_recovery_diagnostics_are_bounded_closed_facts():
    boolean_facts = {
        "semantic_stale_recovery_eligible": True,
        "semantic_stale_recovery_attempted": True,
        "semantic_stale_recovery_window_rebound": True,
        "semantic_stale_recovery_window_stable": True,
        "semantic_stale_recovery_second_pass_complete": False,
        "semantic_stale_recovery_succeeded": False,
    }
    count_caps = {
        "semantic_discovery_pass_count": 3,
        "semantic_stale_recovery_restart_count": 2,
        "semantic_first_pass_stale_count": 64,
        "semantic_second_pass_stale_count": 64,
        "semantic_first_pass_unknown_branch_count": 64,
        "semantic_second_pass_unknown_branch_count": 64,
        "semantic_first_pass_nodes_visited_count": 255,
        "semantic_second_pass_nodes_visited_count": 255,
        "semantic_second_pass_final_candidate_count": 8,
    }
    widget = {
        "action": "computer.probe_text_control",
        **boolean_facts,
        **{key: 10_000 for key in count_caps},
        "semantic_stale_recovery_outcome": "second_pass_stale",
        "semantic_second_third_stale_reference_class": "CANARY_UNKNOWN_REFERENCE_CLASS",
        "semantic_stale_recovery_raw_element": "private AX identity",
    }

    facts = debug._direct_probe_facts(widget)

    assert {key: facts[key] for key in boolean_facts} == boolean_facts
    assert {key: facts[key] for key in count_caps} == count_caps
    assert facts["semantic_stale_recovery_outcome"] == "second_pass_stale"
    assert "semantic_second_third_stale_reference_class" not in facts
    assert "semantic_stale_recovery_raw_element" not in facts


def test_direct_probe_app_diagnostic_candidate_does_not_override_exact_role_absent():
    widget = {
        "action": "computer.probe_text_control",
        "probe_completed": True,
        "semantic_control_ready": False,
        "semantic_discovery_stage": "role_absent",
        "semantic_traversal_order": "breadth_first",
        "semantic_window_scan_complete": True,
        "semantic_window_scan_truncated": False,
        "semantic_window_depth_truncated": False,
        "semantic_window_allowed_role_count": 0,
        "semantic_app_diagnostic_stage": "scan_incomplete",
        "semantic_app_scan_complete": False,
        "semantic_app_scan_truncated": True,
        "semantic_unlisted_mutation_ready_count": 1,
        "semantic_unlisted_role_class": "unlisted_container",
    }

    with pytest.raises(debug.DirectProbeContractError) as raised:
        debug._direct_probe_contract(widget)

    assert raised.value.error_code == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    assert raised.value.facts["semantic_unlisted_role_class"] == "unlisted_container"
    assert raised.value.facts["semantic_app_diagnostic_stage"] == "scan_incomplete"


def test_direct_probe_secondary_app_diagnostics_are_bounded_content_free_facts():
    count_fields = (
        "semantic_unlisted_value_settable_count",
        "semantic_unlisted_selected_text_settable_count",
        "semantic_unlisted_selected_range_settable_count",
        "semantic_unlisted_focus_settable_count",
        "semantic_unlisted_attribute_capability_known_count",
        "semantic_unlisted_under_toolbar_count",
        "semantic_unlisted_related_allowed_role_count",
    )
    widget = {
        "action": "computer.probe_text_control",
        "semantic_actionable_counts_truncated": False,
        "semantic_app_diagnostic_counts_truncated": True,
        "semantic_unlisted_relation_scan_complete": True,
        **{key: 10_000 for key in count_fields},
        "semantic_app_diagnostic_stage": "scan_incomplete",
        "semantic_app_diagnostic_scope": "application_tree_owned",
        "semantic_app_diagnostic_ownership_proof": "multiple",
        "semantic_unlisted_relation_kind": "linked_relation",
        "semantic_unlisted_related_element": "private AX identity",
    }

    facts = debug._direct_probe_facts(widget)

    assert facts["semantic_actionable_counts_truncated"] is False
    assert facts["semantic_app_diagnostic_counts_truncated"] is True
    assert facts["semantic_unlisted_relation_scan_complete"] is True
    assert {key: facts[key] for key in count_fields} == {
        key: 64 for key in count_fields
    }
    assert facts["semantic_app_diagnostic_stage"] == "scan_incomplete"
    assert facts["semantic_app_diagnostic_scope"] == "application_tree_owned"
    assert facts["semantic_app_diagnostic_ownership_proof"] == "multiple"
    assert facts["semantic_unlisted_relation_kind"] == "linked_relation"
    assert "semantic_unlisted_related_element" not in facts


def test_direct_probe_exact_role_absent_overrides_legacy_incomplete_error_code():
    widget = {
        "action": "computer.probe_text_control",
        "probe_completed": True,
        "semantic_control_ready": False,
        "error_code": "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
        "semantic_discovery_stage": "role_absent",
        "semantic_traversal_order": "breadth_first",
        "semantic_scan_scope": "exact_window_descendants",
        "semantic_ownership_proof": "window_descendant",
        "semantic_window_scan_complete": True,
        "semantic_window_scan_truncated": False,
        "semantic_window_depth_truncated": False,
        "semantic_window_allowed_role_count": 0,
        "semantic_app_diagnostic_stage": "scan_incomplete",
        "semantic_app_scan_complete": False,
        "semantic_app_scan_truncated": True,
    }

    with pytest.raises(debug.DirectProbeContractError) as raised:
        debug._direct_probe_contract(widget)

    assert raised.value.error_code == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    assert raised.value.facts["semantic_discovery_stage"] == "role_absent"
    assert raised.value.facts["semantic_app_diagnostic_stage"] == "scan_incomplete"


def test_direct_probe_exposure_diagnostics_are_bounded_closed_facts():
    boolean_facts = {
        "semantic_exposure_probe_performed": True,
        "semantic_exposure_probe_complete": True,
        "semantic_exposure_probe_truncated": False,
        "semantic_alt_contents_advertised": True,
        "semantic_alt_visible_children_advertised": True,
        "semantic_alt_navigation_order_advertised": True,
        "semantic_alt_shared_text_advertised": True,
        "semantic_alt_focused_element_present": True,
        "semantic_alt_focused_element_exact_owned": True,
        "semantic_alt_focused_element_non_web": True,
        "semantic_alt_focused_element_allowed_role": True,
        "semantic_alt_search_predicate_advertised": True,
        "semantic_alt_text_marker_relation_advertised": True,
        "semantic_alt_allowed_role_found": True,
        "semantic_alt_full_eligibility_found": True,
    }
    count_caps = {
        "semantic_exposure_nodes_visited_count": 64,
        "semantic_exposure_edge_reads_count": 128,
        "semantic_exposure_edge_read_failure_count": 16,
        "semantic_exposure_exact_owned_count": 64,
        "semantic_exposure_non_web_count": 64,
        "semantic_exposure_allowed_role_count": 8,
        "semantic_exposure_full_eligibility_count": 8,
        "semantic_exposure_shared_text_relation_count": 8,
        "semantic_exposure_parameterized_capability_count": 8,
        "semantic_exposure_page_control_count": 8,
    }
    widget = {
        "action": "computer.probe_text_control",
        **boolean_facts,
        **{key: 10_000 for key in count_caps},
        "semantic_exposure_stage": "alternate_structural_role_found",
        "semantic_exposure_source": "multiple",
        "semantic_parameterized_capability_class": "multiple",
        "semantic_exposure_raw_attribute": "AXContents",
        "semantic_exposure_raw_role": "AXTextField",
        "semantic_exposure_raw_value": "private-value",
        "semantic_exposure_element_id": "private-id",
        "semantic_exposure_frame": {"x": 1, "y": 2},
    }

    facts = debug._direct_probe_facts(widget)

    assert {key: facts[key] for key in boolean_facts} == boolean_facts
    assert {key: facts[key] for key in count_caps} == count_caps
    assert facts["semantic_exposure_stage"] == "alternate_structural_role_found"
    assert facts["semantic_exposure_source"] == "multiple"
    assert facts["semantic_parameterized_capability_class"] == "multiple"
    rendered = json.dumps(facts)
    for canary in ("AXContents", "AXTextField", "private-value", "private-id", '"x"'):
        assert canary not in rendered


def test_direct_probe_exposure_incomplete_causes_are_bounded_closed_facts():
    boolean_facts = {
        "semantic_exposure_global_node_limit_hit": False,
        "semantic_exposure_global_read_limit_hit": True,
        "semantic_exposure_count_saturated": True,
    }
    count_caps = {
        "semantic_exposure_incomplete_cause_count": 8,
        "semantic_exposure_edge_fanout_truncated_count": 16,
        "semantic_exposure_depth_limit_new_target_count": 16,
        "semantic_exposure_depth_limit_queued_target_count": 16,
        "semantic_exposure_queue_remainder_count": 64,
        "semantic_exposure_payload_missing_count": 16,
        "semantic_exposure_payload_invalid_count": 16,
        "semantic_exposure_payload_mixed_count": 16,
        "semantic_exposure_attribute_inventory_unknown_count": 16,
        "semantic_exposure_parameterized_inventory_unknown_count": 5,
        "semantic_exposure_edge_incomplete_without_failure_count": 16,
        "semantic_exposure_node_ownership_rejected_count": 64,
        "semantic_exposure_edge_target_ownership_rejected_count": 64,
    }
    widget = {
        "action": "computer.probe_text_control",
        **boolean_facts,
        **{key: 10_000 for key in count_caps},
        "semantic_exposure_incomplete_cause": "multiple",
        "semantic_exposure_fanout_source": "navigation_order",
        "semantic_exposure_depth_limit_source": "shared_text",
        "semantic_exposure_focus_cardinality": "multiple",
        "semantic_exposure_count_saturation_class": "edge_fanout",
        "semantic_exposure_raw_attribute": "AXChildrenInNavigationOrder",
        "semantic_exposure_raw_role": "AXTextField",
        "semantic_exposure_raw_value": "private-value",
        "semantic_exposure_element_id": "private-id",
        "semantic_exposure_frame": {"x": 1, "y": 2},
    }

    facts = debug._direct_probe_facts(widget)

    assert {key: facts[key] for key in boolean_facts} == boolean_facts
    assert {key: facts[key] for key in count_caps} == count_caps
    assert facts["semantic_exposure_incomplete_cause"] == "multiple"
    assert facts["semantic_exposure_fanout_source"] == "navigation_order"
    assert facts["semantic_exposure_depth_limit_source"] == "shared_text"
    assert facts["semantic_exposure_focus_cardinality"] == "multiple"
    assert facts["semantic_exposure_count_saturation_class"] == "edge_fanout"
    rendered = json.dumps(facts)
    for canary in (
        "AXChildrenInNavigationOrder",
        "AXTextField",
        "private-value",
        "private-id",
        '"x"',
    ):
        assert canary not in rendered

    unknown = dict(widget)
    for key in (
        "semantic_exposure_incomplete_cause",
        "semantic_exposure_fanout_source",
        "semantic_exposure_depth_limit_source",
        "semantic_exposure_focus_cardinality",
        "semantic_exposure_count_saturation_class",
    ):
        unknown[key] = f"CANARY_{key}"
    unknown_facts = debug._direct_probe_facts(unknown)
    for key in (
        "semantic_exposure_incomplete_cause",
        "semantic_exposure_fanout_source",
        "semantic_exposure_depth_limit_source",
        "semantic_exposure_focus_cardinality",
        "semantic_exposure_count_saturation_class",
    ):
        assert key not in unknown_facts
    assert "CANARY" not in json.dumps(unknown_facts)


@pytest.mark.parametrize(
    "incomplete_cause",
    [
        "none",
        "edge_fanout",
        "depth_limit",
        "global_node_limit",
        "global_read_limit",
        "queue_remainder",
        "focus_cardinality",
        "payload_invalid",
        "attribute_inventory_unknown",
        "parameterized_inventory_unknown",
        "edge_incomplete_without_failure",
        "counter_saturation",
        "multiple",
    ],
)
def test_direct_probe_every_exposure_incomplete_cause_remains_not_found(
    incomplete_cause,
):
    widget = {
        "action": "computer.probe_text_control",
        "probe_completed": True,
        "semantic_control_ready": False,
        "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
        "semantic_discovery_stage": "role_absent",
        "semantic_traversal_order": "breadth_first",
        "semantic_scan_scope": "exact_window_descendants",
        "semantic_window_scan_complete": True,
        "semantic_window_scan_truncated": False,
        "semantic_window_depth_truncated": False,
        "semantic_window_allowed_role_count": 0,
        "semantic_exposure_probe_performed": True,
        "semantic_exposure_probe_complete": False,
        "semantic_exposure_probe_truncated": True,
        "semantic_exposure_stage": "incomplete",
        "semantic_exposure_incomplete_cause": incomplete_cause,
        "semantic_exposure_incomplete_cause_count": 0
        if incomplete_cause == "none"
        else 2
        if incomplete_cause == "multiple"
        else 1,
    }

    with pytest.raises(debug.DirectProbeContractError) as raised:
        debug._direct_probe_contract(widget)

    assert raised.value.error_code == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    assert raised.value.facts["semantic_exposure_incomplete_cause"] == incomplete_cause


@pytest.mark.parametrize(
    "exposure_stage",
    [
        "incomplete",
        "alternate_structural_role_found",
        "relationship_role_found",
        "focused_page_control",
        "capability_advertised_only",
        "only_unlisted_proxy",
        "complete_no_fixed_exposure",
    ],
)
def test_direct_probe_every_exposure_result_stays_not_found_before_approval_and_mutation(
    exposure_stage, tmp_path, monkeypatch
):
    output = io.StringIO()
    calls = []
    identities = iter(("codex", "codex", "codex", "codex"))
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: next(identities))

    def unapproved(_client, action, payload):
        calls.append((action, payload))
        if action == "computer.select_window":
            return _exact_select_widget()
        assert action == "computer.probe_text_control"
        return {
            "action": action,
            "probe_completed": True,
            "semantic_control_ready": False,
            "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
            "semantic_discovery_stage": "role_absent",
            "semantic_traversal_order": "breadth_first",
            "semantic_scan_scope": "exact_window_descendants",
            "semantic_window_scan_complete": True,
            "semantic_window_scan_truncated": False,
            "semantic_window_depth_truncated": False,
            "semantic_window_allowed_role_count": 0,
            "semantic_exposure_probe_performed": True,
            "semantic_exposure_probe_complete": exposure_stage != "incomplete",
            "semantic_exposure_probe_truncated": exposure_stage == "incomplete",
            "semantic_alt_allowed_role_found": exposure_stage
            in {"alternate_structural_role_found", "relationship_role_found"},
            "semantic_alt_full_eligibility_found": exposure_stage
            == "alternate_structural_role_found",
            "semantic_exposure_allowed_role_count": 1,
            "semantic_exposure_stage": exposure_stage,
            "semantic_exposure_source": "contents",
            "semantic_parameterized_capability_class": "none",
            "semantic_exposure_incomplete_cause": "edge_fanout"
            if exposure_stage == "incomplete"
            else "none",
            "semantic_exposure_incomplete_cause_count": 1
            if exposure_stage == "incomplete"
            else 0,
            "semantic_exposure_edge_fanout_truncated_count": 1
            if exposure_stage == "incomplete"
            else 0,
            "semantic_exposure_fanout_source": "navigation_order"
            if exposure_stage == "incomplete"
            else "none",
            "semantic_exposure_depth_limit_source": "none",
            "semantic_exposure_focus_cardinality": "none",
            "semantic_exposure_global_node_limit_hit": False,
            "semantic_exposure_global_read_limit_hit": False,
            "semantic_exposure_count_saturated": False,
            "semantic_exposure_count_saturation_class": "none",
            "semantic_exposure_raw_value": "private-canary",
        }

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", unapproved)
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail(
            "exposure diagnostics must stop before write approval or mutation"
        ),
    )

    with pytest.raises(debug.DirectProbeContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(output),
            run_nonce=f"exposure-{exposure_stage}",
        )

    assert raised.value.error_code == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    assert [action for action, _payload in calls] == [
        "computer.select_window",
        "computer.probe_text_control",
    ]
    [probe_event] = [
        json.loads(line)
        for line in output.getvalue().splitlines()
        if "viewer_direct_probe_failed" in line
    ]
    assert probe_event["error_code"] == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    assert probe_event["semantic_exposure_stage"] == exposure_stage
    assert probe_event["semantic_exposure_incomplete_cause"] == (
        "edge_fanout" if exposure_stage == "incomplete" else "none"
    )
    assert probe_event["semantic_exposure_fanout_source"] == (
        "navigation_order" if exposure_stage == "incomplete" else "none"
    )
    assert "private-canary" not in output.getvalue()

    supervisor_output = io.StringIO()
    debug.SmokeReporter(supervisor_output).emit(
        "viewer_direct_failed",
        ok=False,
        **debug._direct_failure_report(raised.value),
    )
    supervisor_event = json.loads(supervisor_output.getvalue())
    assert supervisor_event["error_code"] == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    assert supervisor_event["semantic_exposure_stage"] == exposure_stage
    assert supervisor_event["semantic_exposure_incomplete_cause"] == (
        "edge_fanout" if exposure_stage == "incomplete" else "none"
    )
    assert supervisor_event["semantic_exposure_fanout_source"] == (
        "navigation_order" if exposure_stage == "incomplete" else "none"
    )
    assert set(supervisor_event) <= {
        "event",
        "ok",
        "error",
        "error_code",
        "classification",
        "failure_stage",
        *debug._DIRECT_PROBE_BOOL_FIELDS,
        *debug._DIRECT_PROBE_COUNT_CAPS,
        *debug._DIRECT_PROBE_ENUM_FIELDS,
    }
    assert "private-canary" not in supervisor_output.getvalue()


def test_direct_select_contract_uses_only_action_owned_root_result():
    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug._direct_select_contract(
            {
                "action": "computer.select_window",
                "selected": False,
                "error_code": "SELECT_WINDOW_APP_NOT_FOUND",
                "selection_exact_binding_required": True,
                "result": _exact_select_widget(),
            }
        )

    assert raised.value.error_code == "SELECT_WINDOW_APP_NOT_FOUND"


def test_direct_select_contract_rejects_nested_target_window():
    widget = _exact_select_widget(target_window=None)
    widget["result"] = {"target_window": _exact_select_widget()["target_window"]}

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug._direct_select_contract(widget)

    assert raised.value.error_code == "SELECT_WINDOW_TRANSPORT_CONTRACT_INVALID"


def test_direct_select_contract_requires_action_owned_scope():
    widget = _exact_select_widget(action="computer.context")
    widget["result"] = _exact_select_widget()

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug._direct_select_contract(widget)

    assert raised.value.error_code == "SELECT_WINDOW_RESULT_SCOPE_INVALID"
    assert raised.value.failure_stage == "result_scope"


@pytest.mark.parametrize("missing_key", ["pid", "window_id", "x", "y", "width", "height"])
def test_direct_select_contract_maps_invalid_exact_component_to_transport_error(missing_key):
    widget = _exact_select_widget()
    widget["target_window"] = dict(widget["target_window"])
    widget["target_window"].pop(missing_key)

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug._direct_select_contract(widget)

    assert raised.value.error_code == "SELECT_WINDOW_TRANSPORT_CONTRACT_INVALID"


def test_direct_selection_failure_stops_before_approval_type_native_or_screenshot(
    tmp_path, monkeypatch
):
    select_calls = []
    mutation_calls = []
    output = io.StringIO()
    reporter = debug.SmokeReporter(output)
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")

    def unapproved(_client, action, payload):
        select_calls.append((action, payload))
        return {
            "action": "computer.select_window",
            "selected": False,
            "is_error": True,
            "error_code": "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
            "selection_exact_binding_required": True,
            "selection_exact_binding_present": False,
            "selection_matched_app": True,
            "selection_matched_window": True,
            "selection_failure_stage": "exact_binding",
            "target_window": {
                "title": "private-title-must-not-print",
                "pid": 987654,
                "window_id": 876543,
            },
        }

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", unapproved)

    def forbidden_mutation(*args, **kwargs):
        mutation_calls.append((args, kwargs))
        pytest.fail("selection failure must stop before approval or computer mutation")

    monkeypatch.setattr(debug, "_direct_approved_widget", forbidden_mutation)
    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=reporter,
            run_nonce="selection-failure",
        )

    assert raised.value.error_code == "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE"
    assert mutation_calls == []
    assert select_calls == [
        (
            "computer.select_window",
            {
                "app": "ChatGPT Atlas",
                "focus": False,
                "require_exact_binding": True,
            },
        )
    ]
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[0]["event"] == "viewer_direct_selection_observation"
    assert records[0]["selection_observation_index"] == 1
    assert records[0]["selection_observation_count"] == 1
    assert records[0]["selection_reobservation_attempted"] is False
    assert records[1] == {
        "event": "viewer_direct_selection_failed",
        "ok": False,
        "error_code": "SELECT_WINDOW_EXACT_BINDING_INCOMPLETE",
        "failure_stage": "contract_validation",
        "selection_exact_binding_required": True,
        "selection_exact_binding_present": False,
        "selection_matched_app": True,
        "selection_matched_window": True,
        "selection_failure_stage": "exact_binding",
        "selection_observation_count": 1,
        "selection_reobservation_eligible": False,
        "selection_reobservation_attempted": False,
        "selection_reobservation_recovered": False,
        "selection_reobservation_outcome": "not_eligible",
        "selection_permission_fact_stability": "unknown",
        "selection_permission_fact_change_count": 0,
        "selection_visibility_fact_stability": "unknown",
        "selection_visibility_fact_change_count": 0,
    }
    assert "private-title-must-not-print" not in output.getvalue()
    assert "987654" not in output.getvalue()
    assert "876543" not in output.getvalue()


def test_direct_selection_background_invariant_is_fixed_failure(tmp_path, monkeypatch):
    identities = iter(("codex", "terminal"))
    output = io.StringIO()
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: next(identities))
    monkeypatch.setattr(
        debug,
        "_direct_unapproved_read_widget",
        lambda *_args, **_kwargs: _exact_select_widget(),
    )
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail("background failure must stop before approval"),
    )

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(output),
            run_nonce="background-failure",
        )

    assert raised.value.error_code == "SELECT_WINDOW_BACKGROUND_INVARIANT_FAILED"
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[0]["event"] == "viewer_direct_selection_observation"
    record = records[1]
    assert record["failure_stage"] == "frontmost_validation"
    assert set(record) <= {
        "event",
        "ok",
        "error_code",
        "failure_stage",
        *debug._DIRECT_SELECT_BOOL_FIELDS,
        *debug._DIRECT_SELECT_COUNT_CAPS,
        *debug._DIRECT_SELECT_ENUM_FIELDS,
    }


def test_direct_selection_reobservation_recovers_after_fixed_delay_without_approval(
    tmp_path, monkeypatch
):
    calls = []
    sleeps = []
    output = io.StringIO()
    selection_results = iter((_eligible_selection_miss(), _exact_select_widget()))
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")
    monkeypatch.setattr(debug.time, "sleep", lambda seconds: sleeps.append(seconds))

    def unapproved(_client, action, payload):
        calls.append((action, dict(payload)))
        if action == "computer.select_window":
            return next(selection_results)
        assert action == "computer.probe_text_control"
        return {
            "action": action,
            "probe_completed": True,
            "semantic_control_ready": False,
            "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
            "semantic_discovery_stage": "role_absent",
            "semantic_traversal_order": "breadth_first",
            "semantic_scan_scope": "exact_window_descendants",
            "semantic_window_scan_complete": True,
            "semantic_window_scan_truncated": False,
            "semantic_window_depth_truncated": False,
            "semantic_window_allowed_role_count": 0,
        }

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", unapproved)
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail(
            "reobservation and probe must remain before approval or mutation"
        ),
    )

    with pytest.raises(debug.DirectProbeContractError):
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(output),
            run_nonce="reobservation-recovered",
        )

    assert sleeps == [0.1]
    assert [action for action, _payload in calls] == [
        "computer.select_window",
        "computer.select_window",
        "computer.probe_text_control",
    ]
    assert calls[0][1] == calls[1][1]
    observations = [
        json.loads(line)
        for line in output.getvalue().splitlines()
        if "viewer_direct_selection_observation" in line
    ]
    assert [item["selection_observation_index"] for item in observations] == [1, 2]
    assert "selection_observation_count" not in observations[0]
    assert observations[1]["selection_observation_count"] == 2
    assert observations[1]["selection_reobservation_eligible"] is True
    assert observations[1]["selection_reobservation_attempted"] is True
    assert observations[1]["selection_reobservation_recovered"] is True
    assert observations[1]["selection_reobservation_outcome"] == "recovered"


def test_direct_selection_two_misses_have_one_delay_and_no_third_read(
    tmp_path, monkeypatch
):
    calls = []
    sleeps = []
    output = io.StringIO()
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")
    monkeypatch.setattr(debug.time, "sleep", lambda seconds: sleeps.append(seconds))

    def unapproved(_client, action, payload):
        calls.append((action, dict(payload)))
        assert action == "computer.select_window"
        return _eligible_selection_miss()

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", unapproved)
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail("two misses must stop before approval"),
    )

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(output),
            run_nonce="reobservation-two-misses",
        )

    assert raised.value.error_code == "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED"
    assert sleeps == [0.1]
    assert len(calls) == 2
    assert calls[0] == calls[1]
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    observations = [
        item for item in records if item["event"] == "viewer_direct_selection_observation"
    ]
    assert [item["selection_observation_index"] for item in observations] == [1, 2]
    assert observations[1]["selection_reobservation_recovered"] is False
    assert observations[1]["selection_reobservation_outcome"] == "not_recovered"
    failure = records[-1]
    assert failure["event"] == "viewer_direct_selection_failed"
    assert failure["selection_observation_count"] == 2
    assert failure["selection_reobservation_attempted"] is True
    supervisor = debug._direct_failure_report(raised.value)
    assert supervisor["error_code"] == "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED"
    assert supervisor["selection_observation_count"] == 2
    assert supervisor["selection_reobservation_eligible"] is True
    assert supervisor["selection_reobservation_attempted"] is True
    assert supervisor["selection_reobservation_recovered"] is False
    assert supervisor["selection_reobservation_outcome"] == "not_recovered"


def test_direct_selection_reobservation_not_eligible_has_no_delay_or_second_read(
    tmp_path, monkeypatch
):
    calls = []
    sleeps = []
    output = io.StringIO()
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")
    monkeypatch.setattr(debug.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        debug,
        "_direct_unapproved_read_widget",
        lambda _client, action, payload: calls.append((action, dict(payload)))
        or _eligible_selection_miss(
            selection_nsworkspace_target_process_present=False,
            selection_later_source_target_match_present=False,
            selection_inventory_diagnostic_outcome="process_absent",
        ),
    )

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(output),
            run_nonce="reobservation-not-eligible",
        )

    assert raised.value.error_code == "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED"
    assert sleeps == []
    assert len(calls) == 1
    failure = json.loads(output.getvalue().splitlines()[-1])
    assert failure["selection_reobservation_eligible"] is False
    assert failure["selection_reobservation_attempted"] is False
    assert failure["selection_reobservation_outcome"] == "not_eligible"


def test_direct_selection_background_change_blocks_eligible_second_read(
    tmp_path, monkeypatch
):
    calls = []
    sleeps = []
    identities = iter(("codex", "terminal"))
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: next(identities))
    monkeypatch.setattr(debug.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        debug,
        "_direct_unapproved_read_widget",
        lambda _client, action, payload: calls.append((action, dict(payload)))
        or _eligible_selection_miss(),
    )

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(io.StringIO()),
            run_nonce="reobservation-background-change",
        )

    assert raised.value.error_code == "SELECT_WINDOW_BACKGROUND_INVARIANT_FAILED"
    assert sleeps == []
    assert len(calls) == 1


def test_direct_selection_second_read_must_independently_satisfy_exact_contract(
    tmp_path, monkeypatch
):
    calls = []
    results = iter(
        (
            _eligible_selection_miss(),
            _exact_select_widget(selection_exact_binding_present=False),
        )
    )
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")
    monkeypatch.setattr(debug.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        debug,
        "_direct_unapproved_read_widget",
        lambda _client, action, payload: calls.append((action, dict(payload)))
        or next(results),
    )

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(io.StringIO()),
            run_nonce="reobservation-invalid-exact",
        )

    assert raised.value.error_code == "SELECT_WINDOW_TRANSPORT_CONTRACT_INVALID"
    assert len(calls) == 2
    assert raised.value.facts["selection_reobservation_recovered"] is False
    assert raised.value.facts["selection_reobservation_outcome"] == "not_recovered"


def test_direct_selection_inventory_and_reobservation_facts_are_closed_bounded():
    widget = _eligible_selection_miss(
        selection_swift_window_total_count=10_000,
        selection_swift_target_name_match_count=10_000,
        selection_inventory_cause_count=10_000,
        selection_observation_count=10_000,
        selection_inventory_source_used="swift_host",
        selection_swift_helper_binary_class="isolated_compiled_current",
        selection_inventory_diagnostic_outcome="process_present_no_window",
        selection_reobservation_outcome="not_recovered",
        selection_reobservation_attempted=True,
        selection_window_owner_alias_matched=False,
        selection_raw_windows=[{"title": "private-canary"}],
        selection_raw_process_name="private-canary",
        selection_raw_bundle_id="private.canary",
        selection_raw_helper_path="/private/canary",
        pid=98765,
        window_id=87654,
    )

    facts = debug._direct_selection_facts(widget)

    assert facts["selection_swift_window_total_count"] == 64
    assert facts["selection_swift_target_name_match_count"] == 8
    assert facts["selection_inventory_cause_count"] == 4
    assert facts["selection_observation_count"] == 2
    assert facts["selection_inventory_source_used"] == "swift_host"
    assert facts["selection_swift_helper_binary_class"] == "isolated_compiled_current"
    assert facts["selection_inventory_diagnostic_outcome"] == "process_present_no_window"
    assert facts["selection_reobservation_outcome"] == "not_recovered"
    assert facts["selection_reobservation_attempted"] is True
    assert facts["selection_window_owner_alias_matched"] is False
    rendered = json.dumps(facts)
    assert "private-canary" not in rendered
    assert "98765" not in rendered
    assert "87654" not in rendered

    unknown = dict(widget)
    unknown.update(
        {
            "selection_inventory_source_used": "CANARY_SOURCE",
            "selection_swift_helper_binary_class": "CANARY_BINARY",
            "selection_inventory_diagnostic_outcome": "CANARY_OUTCOME",
            "selection_reobservation_outcome": "CANARY_REOBSERVATION",
        }
    )
    unknown_facts = debug._direct_selection_facts(unknown)
    for key in (
        "selection_inventory_source_used",
        "selection_swift_helper_binary_class",
        "selection_inventory_diagnostic_outcome",
        "selection_reobservation_outcome",
    ):
        assert key not in unknown_facts
    assert "CANARY" not in json.dumps(unknown_facts)


def test_direct_selection_selected_identity_facts_are_closed_and_non_authorizing():
    widget = _eligible_selection_miss(
        selection_selected_identity_contract_valid=True,
        selection_selected_identity_available=True,
        selection_selected_owner_alias_match=True,
        selection_selected_target_process_match=False,
        selection_selected_target_bundle_match=False,
        selection_selected_identity_class="owner_name_only",
        _rumi_owner_alias_match=True,
        selection_selected_identity_private_pid=98765,
    )

    facts = debug._direct_selection_facts(widget)

    assert facts["selection_selected_identity_contract_valid"] is True
    assert facts["selection_selected_identity_available"] is True
    assert facts["selection_selected_identity_class"] == "owner_name_only"
    assert "_rumi_" not in json.dumps(facts)
    assert "98765" not in json.dumps(facts)

    malformed = dict(widget)
    malformed.update({
        "selection_selected_identity_available": "true",
        "selection_selected_identity_class": "CANARY_IDENTITY",
    })
    invalid = debug._direct_selection_facts(malformed)
    assert "selection_selected_identity_available" not in invalid
    assert "selection_selected_identity_class" not in invalid
    assert "CANARY" not in json.dumps(invalid)


def test_direct_selection_permission_facts_are_closed_bounded_and_content_free():
    widget = _eligible_selection_miss(
        selection_permission_request_api_invoked=False,
        selection_swift_permission_check_colocated=True,
        selection_quartz_permission_check_colocated=True,
        selection_system_events_permission_check_colocated=True,
        selection_swift_all_windows_nonactionable=True,
        selection_quartz_all_windows_nonactionable=True,
        selection_swift_owner_name_present_count=10_000,
        selection_swift_raw_target_pid_match_count=10_000,
        selection_quartz_rejected_target_pid_mismatch_count=10_000,
        selection_permission_fact_change_count=10_000,
        selection_swift_execution_component="swift_helper",
        selection_quartz_execution_component="isolated_python_runtime",
        selection_system_events_execution_component="system_events_child",
        selection_swift_helper_signing_class="ad_hoc",
        selection_swift_helper_persistence_class="reused_current",
        selection_swift_helper_path_stability="same",
        selection_swift_helper_signature_stability="same",
        selection_codex_permission_comparison="not_observable",
        selection_swift_ax_trust="trusted",
        selection_quartz_ax_trust="not_trusted",
        selection_swift_ax_target_probe_outcome="success",
        selection_quartz_ax_target_probe_outcome="skipped_not_trusted",
        selection_system_events_automation_preflight="would_require_consent",
        selection_system_events_execution_outcome="not_authorized",
        selection_swift_screen_capture_preflight="granted",
        selection_quartz_screen_capture_preflight="denied",
        selection_swift_cg_on_screen_query_outcome="success_nonempty",
        selection_swift_cg_all_windows_query_outcome="success_nonempty",
        selection_quartz_cg_on_screen_query_outcome="success_empty",
        selection_quartz_cg_all_windows_query_outcome="success_nonempty",
        selection_permission_diagnostic_outcome="multiple",
        selection_authoritative_permission_source="swift_host",
        selection_authoritative_permission_outcome="permissions_ok",
        selection_secondary_permission_outcome="skipped_non_authoritative",
        selection_permission_fact_stability="changed",
        selection_raw_owner_name="private-canary",
        selection_raw_window_name="private-canary",
        selection_raw_bundle_id="private.canary",
        selection_raw_signing_identity="private-canary",
        selection_raw_path="/private/canary",
        selection_raw_tcc_error="private-canary",
    )

    facts = debug._direct_selection_facts(widget)

    assert facts["selection_swift_owner_name_present_count"] == 64
    assert facts["selection_swift_raw_target_pid_match_count"] == 8
    assert facts["selection_quartz_rejected_target_pid_mismatch_count"] == 64
    assert facts["selection_permission_fact_change_count"] == 4
    assert facts["selection_swift_execution_component"] == "swift_helper"
    assert facts["selection_swift_helper_signing_class"] == "ad_hoc"
    assert facts["selection_quartz_screen_capture_preflight"] == "denied"
    assert facts["selection_permission_diagnostic_outcome"] == "multiple"
    assert facts["selection_authoritative_permission_source"] == "swift_host"
    assert facts["selection_authoritative_permission_outcome"] == "permissions_ok"
    assert facts["selection_secondary_permission_outcome"] == "skipped_non_authoritative"
    assert facts["selection_permission_request_api_invoked"] is False
    rendered = json.dumps(facts)
    assert "private-canary" not in rendered
    assert "private.canary" not in rendered
    assert "/private/canary" not in rendered

    unknown = dict(widget)
    for key in (
        "selection_swift_execution_component",
        "selection_swift_helper_signing_class",
        "selection_swift_ax_trust",
        "selection_system_events_automation_preflight",
        "selection_quartz_screen_capture_preflight",
        "selection_swift_cg_all_windows_query_outcome",
        "selection_permission_diagnostic_outcome",
        "selection_authoritative_permission_source",
        "selection_authoritative_permission_outcome",
        "selection_secondary_permission_outcome",
        "selection_permission_fact_stability",
    ):
        unknown[key] = f"CANARY_{key}"
    unknown_facts = debug._direct_selection_facts(unknown)
    for key in (
        "selection_swift_execution_component",
        "selection_swift_helper_signing_class",
        "selection_swift_ax_trust",
        "selection_system_events_automation_preflight",
        "selection_quartz_screen_capture_preflight",
        "selection_swift_cg_all_windows_query_outcome",
        "selection_permission_diagnostic_outcome",
        "selection_authoritative_permission_source",
        "selection_authoritative_permission_outcome",
        "selection_secondary_permission_outcome",
        "selection_permission_fact_stability",
    ):
        assert key not in unknown_facts
    assert "CANARY" not in json.dumps(unknown_facts)


@pytest.mark.parametrize(
    ("facts", "expected_outcome"),
    [
        (
            {"selection_swift_ax_trust": "not_trusted"},
            "accessibility_denied",
        ),
        (
            {"selection_quartz_screen_capture_preflight": "denied"},
            "screen_capture_denied",
        ),
        (
            {"selection_system_events_automation_preflight": "would_require_consent"},
            "system_events_denied",
        ),
        (
            {"selection_swift_on_screen_omission_confirmed": True},
            "on_screen_filter_exclusion",
        ),
    ],
)
def test_direct_selection_permission_reducer_accepts_matching_closed_outcomes(
    facts, expected_outcome
):
    normalized, failure = debug._direct_permission_diagnostic_contract(
        {**facts, "selection_permission_diagnostic_outcome": expected_outcome},
        selected=False,
    )

    assert failure is None
    assert normalized["selection_permission_diagnostic_outcome"] == expected_outcome


@pytest.mark.parametrize(
    ("facts", "selected", "expected_authoritative_outcome", "expected_failure"),
    [
        (
            {
                "selection_permission_request_api_invoked": True,
                "selection_permission_diagnostic_outcome": "forbidden_action_required",
            },
            False,
            "forbidden_action_required",
            ("SELECT_WINDOW_PERMISSION_REQUEST_FORBIDDEN", "safety_policy_validation"),
        ),
        (
            {
                "selection_authoritative_permission_source": "swift_host",
                "selection_authoritative_permission_outcome": "permissions_ok",
                "selection_swift_cg_all_windows_query_outcome": "success_nonempty",
                "selection_swift_all_windows_nonactionable": False,
                "selection_permission_diagnostic_outcome": "on_screen_filter_exclusion",
            },
            True,
            "instrumentation_inconsistent",
            ("SELECT_WINDOW_RESULT_INVALID", "authoritative_diagnostic_validation"),
        ),
        (
            {
                "selection_authoritative_permission_source": "swift_host",
                "selection_authoritative_permission_outcome": "permissions_ok",
                "selection_permission_fact_stability": "changed",
                "selection_permission_fact_change_count": 0,
                "selection_permission_diagnostic_outcome": "instrumentation_inconsistent",
            },
            True,
            "instrumentation_inconsistent",
            ("SELECT_WINDOW_RESULT_INVALID", "authoritative_diagnostic_validation"),
        ),
    ],
)
def test_direct_selection_permission_contract_normalizes_inconsistency(
    facts, selected, expected_authoritative_outcome, expected_failure
):
    normalized, failure = debug._direct_permission_diagnostic_contract(
        facts,
        selected=selected,
    )

    assert failure == expected_failure
    assert (
        normalized["selection_authoritative_permission_outcome"]
        == expected_authoritative_outcome
    )


def test_direct_selection_permission_stability_is_closed_and_bounded():
    stable = debug._direct_permission_fact_stability(
        {
            "selection_authoritative_permission_source": "swift_host",
            "selection_authoritative_permission_outcome": "permissions_ok",
            "selection_secondary_permission_outcome": "skipped_non_authoritative",
        },
        {
            "selection_authoritative_permission_source": "swift_host",
            "selection_authoritative_permission_outcome": "permissions_ok",
            "selection_secondary_permission_outcome": "screen_capture_denied",
        },
    )
    changed = debug._direct_permission_fact_stability(
        {
            "selection_authoritative_permission_source": "swift_host",
            "selection_authoritative_permission_outcome": "permissions_ok",
        },
        {
            "selection_authoritative_permission_source": "quartz",
            "selection_authoritative_permission_outcome": "screen_capture_denied",
        },
    )
    unknown = debug._direct_permission_fact_stability({}, {})

    assert stable == {
        "selection_permission_fact_stability": "stable",
        "selection_permission_fact_change_count": 0,
    }
    assert changed == {
        "selection_permission_fact_stability": "changed",
        "selection_permission_fact_change_count": 2,
    }
    assert unknown == {
        "selection_permission_fact_stability": "unknown",
        "selection_permission_fact_change_count": 0,
    }


def test_direct_selection_visibility_facts_are_closed_bounded_and_content_free():
    widget = _eligible_selection_miss(
        selection_swift_visibility_probe_performed=True,
        selection_swift_visibility_probe_complete=True,
        selection_swift_visibility_probe_truncated=False,
        selection_swift_target_hidden_present=False,
        selection_swift_target_unhidden_present=True,
        selection_swift_target_ax_windows_read_complete=True,
        selection_swift_visibility_target_process_count=99,
        selection_swift_visibility_candidate_process_count=99,
        selection_swift_target_ax_window_count=99,
        selection_swift_ax_minimized_count=99,
        selection_swift_ax_nonminimized_count=99,
        selection_swift_ax_frame_valid_count=99,
        selection_swift_ax_display_intersection_count=99,
        selection_swift_ax_same_pid_cg_frame_match_count=99,
        selection_swift_ax_cross_pid_cg_frame_match_count=99,
        selection_swift_target_cg_offscreen_layer_zero_geometry_count=99,
        selection_swift_visibility_class="offscreen_cross_pid_frame_correlated",
        selection_swift_visibility_incomplete_cause="none",
        selection_visibility_fact_change_count=99,
        selection_visibility_fact_stability="changed",
        selection_visibility_raw_pid=987654,
        selection_visibility_raw_window_id=876543,
        selection_visibility_raw_frame={"x": 1, "y": 2},
        selection_visibility_raw_ax_ref="PRIVATE_AX_REF",
    )

    facts = debug._direct_selection_facts(widget)

    assert facts["selection_swift_visibility_target_process_count"] == 4
    assert facts["selection_swift_target_ax_window_count"] == 16
    assert facts["selection_swift_target_cg_offscreen_layer_zero_geometry_count"] == 16
    assert facts["selection_visibility_fact_change_count"] == 8
    assert facts["selection_swift_visibility_class"] == "offscreen_cross_pid_frame_correlated"
    assert facts["selection_swift_visibility_incomplete_cause"] == "none"
    rendered = json.dumps(facts)
    assert "PRIVATE_AX_REF" not in rendered
    assert "987654" not in rendered
    assert "876543" not in rendered

    malformed = dict(widget)
    malformed.update(
        {
            "selection_swift_visibility_probe_complete": "true",
            "selection_swift_visibility_class": "CANARY_CLASS",
            "selection_swift_visibility_incomplete_cause": "CANARY_CAUSE",
            "selection_swift_target_ax_window_count": "999",
        }
    )
    malformed_facts = debug._direct_selection_facts(malformed)
    assert "selection_swift_visibility_probe_complete" not in malformed_facts
    assert "selection_swift_visibility_class" not in malformed_facts
    assert "selection_swift_visibility_incomplete_cause" not in malformed_facts
    assert "selection_swift_target_ax_window_count" not in malformed_facts
    assert "CANARY" not in json.dumps(malformed_facts)


def test_direct_selection_visibility_stability_compares_only_closed_topology_facts():
    common = {
        "selection_swift_visibility_class": "offscreen_same_pid_frame_correlated",
        "selection_swift_visibility_probe_complete": True,
        "selection_swift_visibility_probe_truncated": False,
        "selection_swift_visibility_target_process_count": 1,
        "selection_swift_visibility_candidate_process_count": 1,
        "selection_swift_target_ax_window_count": 1,
        "selection_swift_ax_minimized_count": 0,
        "selection_swift_ax_nonminimized_count": 1,
        "selection_swift_ax_frame_valid_count": 1,
        "selection_swift_ax_display_intersection_count": 1,
        "selection_swift_ax_same_pid_cg_frame_match_count": 1,
        "selection_swift_ax_cross_pid_cg_frame_match_count": 0,
        "selection_swift_target_cg_offscreen_layer_zero_geometry_count": 1,
    }
    stable = debug._direct_visibility_fact_stability(
        {**common, "raw_pid": 123}, {**common, "raw_pid": 456}
    )
    changed = debug._direct_visibility_fact_stability(
        common,
        {
            **common,
            "selection_swift_visibility_class": "off_display_geometry",
            "selection_swift_ax_same_pid_cg_frame_match_count": 0,
            "selection_swift_target_cg_offscreen_layer_zero_geometry_count": 0,
        },
    )
    unknown = debug._direct_visibility_fact_stability({}, {})

    assert stable == {
        "selection_visibility_fact_stability": "stable",
        "selection_visibility_fact_change_count": 0,
    }
    assert changed == {
        "selection_visibility_fact_stability": "changed",
        "selection_visibility_fact_change_count": 3,
    }
    assert unknown == {
        "selection_visibility_fact_stability": "unknown",
        "selection_visibility_fact_change_count": 0,
    }


def test_selected_swift_window_ignores_degraded_secondary_permission_diagnostics():
    normalized, failure = debug._direct_permission_diagnostic_contract(
        {
            "selection_authoritative_permission_source": "swift_host",
            "selection_authoritative_permission_outcome": "permissions_ok",
            "selection_secondary_permission_outcome": "screen_capture_denied",
            "selection_permission_diagnostic_outcome": "multiple",
            "selection_quartz_screen_capture_preflight": "denied",
        },
        selected=True,
    )

    assert failure is None
    assert normalized["selection_authoritative_permission_outcome"] == "permissions_ok"
    assert normalized["selection_secondary_permission_outcome"] == "screen_capture_denied"


@pytest.mark.parametrize(
    ("outcome", "expected_code", "expected_stage"),
    [
        ("accessibility_denied", "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_DENIED", "authoritative_permission_validation"),
        ("screen_capture_denied", "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_DENIED", "authoritative_permission_validation"),
        ("system_events_denied", "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_DENIED", "authoritative_permission_validation"),
        ("unknown", "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_UNAVAILABLE", "authoritative_permission_validation"),
        ("unavailable", "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_UNAVAILABLE", "authoritative_permission_validation"),
        ("instrumentation_inconsistent", "SELECT_WINDOW_RESULT_INVALID", "authoritative_diagnostic_validation"),
        ("multiple", "SELECT_WINDOW_RESULT_INVALID", "authoritative_diagnostic_validation"),
    ],
)
def test_direct_selection_stops_before_probe_for_non_ready_authoritative_permission(
    tmp_path, monkeypatch, outcome, expected_code, expected_stage
):
    calls = []
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")
    monkeypatch.setattr(
        debug,
        "_direct_unapproved_read_widget",
        lambda _client, action, payload: calls.append((action, dict(payload)))
        or _exact_select_widget(
            selection_authoritative_permission_outcome=outcome,
            selection_secondary_permission_outcome="skipped_non_authoritative",
            selection_permission_diagnostic_outcome="multiple",
        ),
    )
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail("non-ready authoritative permission cannot reach approval"),
    )

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(io.StringIO()),
            run_nonce="authoritative-permission",
        )

    assert raised.value.error_code == expected_code
    assert raised.value.failure_stage == expected_stage
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("outcome", "expected_code", "expected_stage"),
    [
        ("screen_capture_denied", "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_DENIED", "authoritative_permission_validation"),
        ("unknown", "SELECT_WINDOW_AUTHORITATIVE_PERMISSION_UNAVAILABLE", "authoritative_permission_validation"),
        ("instrumentation_inconsistent", "SELECT_WINDOW_RESULT_INVALID", "authoritative_diagnostic_validation"),
    ],
)
def test_second_selection_observation_uses_authoritative_permission_precedence(
    tmp_path, monkeypatch, outcome, expected_code, expected_stage
):
    calls = []
    results = iter(
        (
            _eligible_selection_miss(),
            _exact_select_widget(
                selection_authoritative_permission_outcome=outcome,
                selection_secondary_permission_outcome="skipped_non_authoritative",
            ),
        )
    )
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")
    monkeypatch.setattr(debug.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        debug,
        "_direct_unapproved_read_widget",
        lambda _client, action, payload: calls.append((action, dict(payload)))
        or next(results),
    )
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail("non-ready second selection cannot reach approval"),
    )

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(io.StringIO()),
            run_nonce="second-authoritative-permission",
        )

    assert raised.value.error_code == expected_code
    assert raised.value.failure_stage == expected_stage
    assert len(calls) == 2
    assert all(action == "computer.select_window" for action, _payload in calls)


def test_direct_selection_forbidden_permission_request_stops_before_second_read(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")
    monkeypatch.setattr(
        debug,
        "_direct_unapproved_read_widget",
        lambda _client, action, payload: calls.append((action, dict(payload)))
        or _eligible_selection_miss(
            selection_permission_request_api_invoked=True,
            selection_permission_diagnostic_outcome="forbidden_action_required",
        ),
    )
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail("permission diagnostics cannot reach approval"),
    )

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(io.StringIO()),
            run_nonce="permission-request-forbidden",
        )

    assert raised.value.error_code == "SELECT_WINDOW_PERMISSION_REQUEST_FORBIDDEN"
    assert raised.value.failure_stage == "safety_policy_validation"
    assert len(calls) == 1
    assert (
        raised.value.facts["selection_authoritative_permission_outcome"]
        == "forbidden_action_required"
    )


def test_direct_selection_failure_preserves_precise_error_after_permission_change(
    tmp_path, monkeypatch
):
    calls = []
    results = iter(
        (
            _eligible_selection_miss(
                selection_swift_ax_trust="trusted",
                selection_swift_screen_capture_preflight="granted",
                selection_permission_diagnostic_outcome="permissions_ok_no_target",
                selection_authoritative_permission_source="swift_host",
                selection_authoritative_permission_outcome="permissions_ok_no_target",
                selection_secondary_permission_outcome="skipped_non_authoritative",
            ),
            _eligible_selection_miss(
                selection_swift_ax_trust="trusted",
                selection_swift_screen_capture_preflight="denied",
                selection_permission_diagnostic_outcome="screen_capture_denied",
                selection_authoritative_permission_source="swift_host",
                selection_authoritative_permission_outcome="screen_capture_denied",
                selection_secondary_permission_outcome="skipped_non_authoritative",
            ),
        )
    )
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")
    monkeypatch.setattr(debug.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        debug,
        "_direct_unapproved_read_widget",
        lambda _client, action, payload: calls.append((action, dict(payload)))
        or next(results),
    )
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail("changed permission facts cannot reach approval"),
    )

    with pytest.raises(debug.DirectSelectionContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(io.StringIO()),
            run_nonce="permission-facts-changed",
        )

    assert raised.value.error_code == "SELECT_WINDOW_TARGET_WINDOW_NOT_OBSERVED"
    assert raised.value.failure_stage == "contract_validation"
    assert len(calls) == 2
    assert all(action == "computer.select_window" for action, _payload in calls)
    assert raised.value.facts["selection_permission_fact_stability"] == "changed"
    assert raised.value.facts["selection_permission_fact_change_count"] == 1
    assert (
        raised.value.facts["selection_authoritative_permission_outcome"]
        == "instrumentation_inconsistent"
    )


@pytest.mark.parametrize(
    ("probe_widget", "expected_code"),
    [
        (
            {
                "action": "computer.probe_text_control",
                "probe_completed": False,
                "semantic_control_ready": False,
                "error_code": "TYPE_EXACT_WINDOW_NOT_FOUND",
            },
            "TYPE_EXACT_WINDOW_NOT_FOUND",
        ),
        (
            _ready_probe_widget(),
            "PROBE_FRONTMOST_SENTINEL_UNSTABLE",
        ),
    ],
)
def test_direct_probe_parses_native_result_before_context_sentinel_mismatch(
    tmp_path, monkeypatch, probe_widget, expected_code
):
    identities = iter(("codex", "codex", "codex", "terminal"))
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: next(identities))

    def unapproved(_client, action, _payload):
        if action == "computer.select_window":
            return _exact_select_widget()
        assert action == "computer.probe_text_control"
        return probe_widget

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", unapproved)
    monkeypatch.setattr(
        debug,
        "_direct_approved_widget",
        lambda *_args, **_kwargs: pytest.fail("probe failure must precede approval"),
    )

    with pytest.raises(debug.DirectProbeContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(io.StringIO()),
            run_nonce="probe-precedence",
        )

    assert raised.value.error_code == expected_code
    assert raised.value.facts["context_frontmost_check_completed"] is True
    assert raised.value.facts["context_frontmost_unchanged"] is False


def test_direct_probe_proven_native_frontmost_failure_takes_precedence(
    tmp_path, monkeypatch
):
    identities = iter(("codex", "codex", "codex", "codex"))
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: next(identities))

    def unapproved(_client, action, _payload):
        if action == "computer.select_window":
            return _exact_select_widget()
        return {
            "action": "computer.probe_text_control",
            "probe_completed": False,
            "semantic_control_ready": False,
            "error_code": "TYPE_EXACT_WINDOW_NOT_FOUND",
            "native_frontmost_check_completed": True,
            "native_target_non_frontmost_before": True,
            "native_target_non_frontmost_after": False,
            "native_frontmost_unchanged": False,
        }

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", unapproved)

    with pytest.raises(debug.DirectProbeContractError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=tmp_path / "viewer",
            direct_artifact_root=tmp_path / "artifacts",
            reporter=debug.SmokeReporter(io.StringIO()),
            run_nonce="native-frontmost-precedence",
        )

    assert raised.value.error_code == "PROBE_BACKGROUND_INVARIANT_FAILED"
    assert raised.value.facts["native_target_non_frontmost_after"] is False


def test_direct_selection_api_failure_preserves_only_fixed_code():
    error = debug._direct_selection_api_failure(
        debug.DebugApiError(
            "private title pid=123 SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND /private/path"
        )
    )

    assert debug._direct_failure_report(error) == {
        "error": "SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND",
        "error_code": "SELECT_WINDOW_USABLE_WINDOW_NOT_FOUND",
        "failure_stage": "contract_validation",
    }


def test_direct_harness_clamps_final_stale_and_quartz_aggregate_diagnostics():
    selection = debug._direct_selection_facts(
        {
            "selection_quartz_cg_all_windows_query_outcome": "success_nonempty_truncated",
            "selection_quartz_cg_all_windows_records_aggregated_count": 999,
            "selection_authoritative_permission_outcome": "permissions_ok_target_unknown",
            "quartz_records": [{"owner": "CANARY_OWNER"}],
        }
    )
    assert selection == {
        "selection_quartz_cg_all_windows_query_outcome": "success_nonempty_truncated",
        "selection_quartz_cg_all_windows_records_aggregated_count": 256,
        "selection_authoritative_permission_outcome": "permissions_ok_target_unknown",
    }

    probe = debug._direct_probe_facts(
        {
            "action": "computer.probe_text_control",
            "diagnostics": {
                "semantic_stale_parent_refresh_attempted": True,
                "semantic_stale_parent_refresh_succeeded": True,
                "semantic_stale_recovery_final_scan_complete": True,
                "semantic_stale_additional_read_budget_exhausted": False,
                "semantic_stale_parent_refresh_count": 2,
                "semantic_stale_parent_refresh_read_count": 3,
                "semantic_stale_additional_ax_read_count": 65,
                "semantic_discovery_pass_count": 4,
                "semantic_stale_recovery_restart_count": 3,
                "semantic_third_pass_stale_count": 65,
                "semantic_third_pass_unknown_branch_count": 65,
                "semantic_third_pass_nodes_visited_count": 256,
                "semantic_third_pass_final_candidate_count": 9,
                "semantic_stale_reference_refresh_class": "stale_reference_absent_nonempty",
                "semantic_stale_branch_comparison": "same_class_and_depth",
                "semantic_second_third_stale_reference_class": "same_parent_new_reference",
                "semantic_stale_recovery_outcome": "final_pass_incomplete",
                "raw_ax_path": "/private/CANARY_PATH",
            },
        }
    )
    assert probe["semantic_stale_parent_refresh_count"] == 1
    assert probe["semantic_stale_parent_refresh_read_count"] == 2
    assert probe["semantic_stale_additional_ax_read_count"] == 64
    assert probe["semantic_discovery_pass_count"] == 3
    assert probe["semantic_stale_recovery_restart_count"] == 2
    assert probe["semantic_third_pass_nodes_visited_count"] == 255
    assert probe["semantic_third_pass_final_candidate_count"] == 8
    assert probe["semantic_stale_reference_refresh_class"] == "stale_reference_absent_nonempty"
    assert probe["semantic_stale_branch_comparison"] == "same_class_and_depth"
    assert probe["semantic_second_third_stale_reference_class"] == "same_parent_new_reference"
    assert probe["semantic_stale_recovery_outcome"] == "final_pass_incomplete"
    assert "CANARY" not in json.dumps(probe)


def test_direct_type_completion_false_negative_does_not_abort_before_screenshot():
    widget = {
        "action": "computer.type",
        "is_error": True,
        "reason": "/private/path and sensitive text must not be reported",
        "error_code": "TYPE_COMPLETION_NOT_VERIFIED",
        "result": {
            "background": True,
            "foreground": False,
            "delivered": True,
            "executed": True,
            "uses_physical_input": False,
            "requires_foreground": False,
            "can_parallel_user_work": True,
            "pid": 12345,
            "window_id": "private-window-id",
            "title": "private title",
            "url": "https://private.example.test/secret",
        },
        "diagnostics": {
            "error": {"code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND"},
            "completion_verified": False,
            "input_dispatched": True,
            "failure_stage": "completion_verification",
        },
    }

    evidence = debug._direct_result_evidence("computer.type", widget)

    assert evidence["classification"] == "DELIVERY_UNVERIFIED"
    assert evidence["continue_to_screenshot"] is True
    assert evidence["error_code"] == "TYPE_COMPLETION_NOT_VERIFIED"
    assert evidence["delivery_facts"] == {
        "input_dispatched": True,
        "background": True,
        "foreground": False,
        "delivered": True,
        "executed": True,
        "uses_physical_input": False,
        "requires_foreground": False,
        "can_parallel": True,
        "approval_approved": True,
        "frontmost_non_atlas": True,
        "frontmost_unchanged": True,
    }
    assert "reason" not in evidence


def test_direct_screenshot_requires_at_least_one_supported_artifact():
    with pytest.raises(debug.DirectArtifactCopyError, match="SCREENSHOT_COPY_SOURCE_MISSING"):
        debug._direct_result_evidence(
            "computer.screenshot",
            {"action": "computer.screenshot", "executed": True},
        )


def test_direct_screenshot_rejects_stale_source_before_copy(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "stale.png"
    source.write_bytes(b"stale")
    os.utime(source, (100.0, 100.0))
    stat = source.stat()
    replay_started_at = max(
        float(stat.st_mtime), float(getattr(stat, "st_birthtime", 0.0) or 0.0)
    ) + 100.0

    with pytest.raises(debug.DirectArtifactCopyError, match="SCREENSHOT_COPY_STALE"):
        debug.copy_direct_screenshot_artifacts(
            {"screenshot_path": str(source)},
            tmp_path / "evidence",
            source_root=source_root,
            step_index=3,
            replay_started_at=replay_started_at,
        )
    assert not list((tmp_path / "evidence").glob("*.png"))


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        ("missing", "SCREENSHOT_COPY_SOURCE_MISSING"),
        ("symlink", "SCREENSHOT_COPY_SYMLINK_REJECTED"),
        ("directory", "SCREENSHOT_COPY_NOT_REGULAR"),
        ("empty", "SCREENSHOT_COPY_EMPTY"),
        ("outside", "SCREENSHOT_COPY_OUTSIDE_TRUSTED_ROOT"),
        ("type", "SCREENSHOT_COPY_TYPE_REJECTED"),
        ("large", "SCREENSHOT_COPY_TOO_LARGE"),
    ],
)
def test_direct_artifact_copy_failures_use_fixed_allowlisted_report(
    tmp_path, kind, expected_code
):
    source_root = tmp_path / "trusted" / "workspace" / "tools" / "computer"
    source_root.mkdir(parents=True)
    source = source_root / "shot.png"
    if kind == "missing":
        pass
    elif kind == "symlink":
        target = source_root / "target.png"
        target.write_bytes(b"image")
        source.symlink_to(target)
    elif kind == "directory":
        source.mkdir()
    elif kind == "empty":
        source.write_bytes(b"")
    elif kind == "outside":
        source = tmp_path / "outside.png"
        source.write_bytes(b"image")
    elif kind == "type":
        source = source_root / "shot.txt"
        source.write_bytes(b"image")
    elif kind == "large":
        with source.open("wb") as stream:
            stream.truncate(50 * 1024 * 1024 + 1)

    with pytest.raises(debug.DirectArtifactCopyError) as raised:
        debug.copy_direct_screenshot_artifacts(
            {"screenshot_path": str(source)},
            tmp_path / "evidence",
            source_root=source_root,
            step_index=1,
            replay_started_at=time.time(),
        )

    report = debug._direct_failure_report(raised.value)
    assert report["error"] == expected_code
    assert report["error_code"] == expected_code
    assert report["failure_stage"] == "artifact_copy"
    assert report["artifact_count"] == 1
    assert set(report) == {
        "error",
        "error_code",
        "failure_stage",
        "artifact_count",
        "source_regular",
        "source_nonempty",
        "source_symlink",
        "source_type_allowed",
        "source_size_allowed",
        "source_fresh",
        "trusted_root_match",
        "copy_attempted",
        "copy_succeeded",
    }
    assert all(
        isinstance(report[key], bool)
        for key in debug._DIRECT_ARTIFACT_REPORT_BOOL_FIELDS
    )
    assert str(tmp_path) not in json.dumps(report)
    assert str(raised.value) == expected_code


def test_direct_artifact_copy_io_failure_is_fixed_and_does_not_leak(tmp_path, monkeypatch):
    source_root = tmp_path / "trusted" / "workspace" / "tools" / "computer"
    source_root.mkdir(parents=True)
    source = source_root / "shot.png"
    source.write_bytes(b"image")

    def fail_copy(*_args, **_kwargs):
        raise OSError("CANARY private path and content")

    monkeypatch.setattr(debug.shutil, "copy2", fail_copy)
    with pytest.raises(debug.DirectArtifactCopyError) as raised:
        debug.copy_direct_screenshot_artifacts(
            {"screenshot_path": str(source)},
            tmp_path / "evidence",
            source_root=source_root,
            step_index=1,
            replay_started_at=time.time(),
        )

    report = debug._direct_failure_report(raised.value)
    assert report["error_code"] == "SCREENSHOT_COPY_IO_FAILED"
    assert report["copy_attempted"] is True
    assert report["copy_succeeded"] is False
    assert "CANARY" not in json.dumps(report)


def test_direct_type_unverified_requires_complete_safe_delivery_diagnostics():
    widget = {
        "action": "computer.type",
        "is_error": True,
        "error": {"code": "TYPE_COMPLETION_NOT_VERIFIED"},
        "result": {
            "background": True,
            "foreground": False,
            "delivered": True,
            "executed": True,
            "uses_physical_input": False,
            "requires_foreground": False,
        },
    }

    with pytest.raises(debug.SmokeRunnerError, match="DIAGNOSTICS_MISSING"):
        debug._direct_result_evidence("computer.type", widget)


@pytest.mark.parametrize("error_code", sorted(debug._SAFE_TYPE_PREDISPATCH_CODES))
def test_direct_type_predispatch_failure_preserves_only_fixed_native_code(error_code):
    widget = {
        "action": "computer.type",
        "is_error": True,
        "error_code": error_code,
        "input_dispatched": False,
        "completion_verified": False,
        "reason": "CANARY content title pid=123 window=456 /private/path",
        "window": {"x": 1, "y": 2, "width": 3, "height": 4},
    }

    with pytest.raises(debug.DirectTypeClassificationError) as raised:
        debug._direct_result_evidence("computer.type", widget)

    report = debug._direct_failure_report(raised.value)
    assert report == {
        "error_code": error_code,
        "classification": "PRECONDITION_FAILED",
        "input_dispatched": False,
        "completion_verified": False,
    }
    assert "CANARY" not in json.dumps(report)
    assert "123" not in json.dumps(report)
    assert str(raised.value) == error_code


def test_direct_type_older_helper_uses_allowlisted_inner_code_only_when_not_dispatched():
    widget = {
        "action": "computer.type",
        "is_error": True,
        "error_code": "TYPE_COMPLETION_NOT_VERIFIED",
        "result": {
            "input_dispatched": False,
            "completion_verified": False,
            "diagnostics": {
                "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
                "raw_error": "CANARY must not escape",
            },
        },
    }

    with pytest.raises(debug.DirectTypeClassificationError) as raised:
        debug._direct_result_evidence("computer.type", widget)

    assert debug._direct_failure_report(raised.value) == {
        "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
        "classification": "PRECONDITION_FAILED",
        "input_dispatched": False,
        "completion_verified": False,
    }


def test_direct_supervisor_predispatch_event_has_only_fixed_classification_fields():
    error = debug.DirectTypeClassificationError(
        "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    )
    output = io.StringIO()
    reporter = debug.SmokeReporter(output, secrets_to_hide=("one-shot-token-canary",))

    reporter.emit("viewer_direct_failed", ok=False, **debug._direct_failure_report(error))

    event = json.loads(output.getvalue())
    assert set(event) == {
        "event",
        "ok",
        "error_code",
        "classification",
        "input_dispatched",
        "completion_verified",
    }
    assert event["error_code"] == "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    rendered = json.dumps(event)
    for forbidden in (
        "content",
        "title",
        "element_id",
        "geometry",
        "pid",
        "window_id",
        "token",
        "/private/path",
        "raw_error",
        "traceback",
    ):
        assert forbidden not in rendered


def test_direct_type_missing_input_dispatched_is_diagnostics_missing():
    widget = {
        "action": "computer.type",
        "is_error": True,
        "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
        "completion_verified": False,
    }

    with pytest.raises(debug.SmokeRunnerError, match="DIAGNOSTICS_MISSING"):
        debug._direct_result_evidence("computer.type", widget)


def test_direct_type_arbitrary_nested_code_cannot_override_action_code():
    widget = {
        "action": "computer.type",
        "is_error": True,
        "error_code": "TYPE_COMPLETION_NOT_VERIFIED",
        "input_dispatched": False,
        "completion_verified": False,
        "telemetry": {
            "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
            "traceback": "CANARY /private/path pid=123",
        },
    }

    with pytest.raises(debug.SmokeRunnerError, match="TYPE_HARD_FAILURE") as raised:
        debug._direct_result_evidence("computer.type", widget)
    assert "CANARY" not in str(raised.value)


def test_direct_type_inner_compatibility_does_not_override_other_action_code():
    widget = {
        "action": "computer.type",
        "is_error": True,
        "error_code": "TYPE_TARGET_DRIFTED",
        "input_dispatched": False,
        "completion_verified": False,
        "result": {
            "diagnostics": {
                "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
            },
        },
    }

    with pytest.raises(debug.SmokeRunnerError, match="TYPE_HARD_FAILURE"):
        debug._direct_result_evidence("computer.type", widget)


@pytest.mark.parametrize(
    "override",
    [
        {"foreground": True},
        {"background": False},
        {"uses_physical_input": True},
        {"requires_foreground": True},
    ],
)
def test_direct_type_unverified_hard_fails_foreground_or_physical_delivery(override):
    result = {
        "input_dispatched": True,
        "background": True,
        "foreground": False,
        "delivered": True,
        "executed": True,
        "uses_physical_input": False,
        "requires_foreground": False,
        "can_parallel": True,
    }
    result.update(override)
    widget = {
        "action": "computer.type",
        "is_error": True,
        "error_code": "TYPE_COMPLETION_NOT_VERIFIED",
        "result": result,
    }

    with pytest.raises(debug.SmokeRunnerError, match="TYPE_DELIVERY_POLICY_VIOLATION"):
        debug._direct_result_evidence("computer.type", widget)


def test_direct_type_other_error_is_hard_failure_without_raw_error_leakage():
    widget = {
        "action": "computer.type",
        "is_error": True,
        "input_dispatched": False,
        "error": {
            "code": "UNEXPECTED_FAILURE",
            "message": "private body https://private.example.test /private/path pid=12345",
        },
    }

    with pytest.raises(debug.SmokeRunnerError, match="TYPE_HARD_FAILURE") as raised:
        debug._direct_result_evidence("computer.type", widget)
    rendered = str(raised.value)
    assert "private" not in rendered
    assert "12345" not in rendered


def test_direct_action_error_ignores_unrelated_nested_error_record():
    widget = {
        "action": "computer.type",
        "is_error": False,
        "executed": True,
        "delivered": True,
        "completion_verified": True,
        "background": True,
        "driver": "mac_accessibility",
        "uses_physical_input": False,
        "requires_foreground": False,
        "can_parallel_user_work": True,
        "edge_haze": {"attempted": True, "started": True},
        "telemetry": {
            "action": "computer.context",
            "is_error": True,
            "error": "CANARY unrelated nested failure",
        },
    }

    evidence = debug._direct_result_evidence("computer.type", widget)

    assert evidence["is_error"] is False
    assert evidence["executed"] is True
    assert evidence["background"] is True


def test_direct_predispatch_failure_stops_before_screenshot_or_retry(tmp_path, monkeypatch):
    viewer_root = tmp_path / "viewer"
    direct_artifact_root = tmp_path / "direct" / "workspace" / "tools" / "computer"
    direct_artifact_root.mkdir(parents=True)
    sent_actions = []
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")
    def ready_unapproved(_client, action, _payload):
        if action == "computer.probe_text_control":
            return _ready_probe_widget()
        return {
            "action": "computer.select_window",
                "selected": True,
                "selection_exact_binding_required": True,
                "selection_exact_binding_present": True,
                "selection_authoritative_permission_source": "swift_host",
                "selection_authoritative_permission_outcome": "permissions_ok",
                "selection_secondary_permission_outcome": "skipped_non_authoritative",
                "target_window": {
                "app": "ChatGPT Atlas",
                "pid": 123,
                "window_id": 456,
                "x": 0,
                "y": 0,
                "width": 1000,
                "height": 700,
            },
        }

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", ready_unapproved)

    def approved_widget(_client, _reporter, action, _payload):
        sent_actions.append(action)
        if action != "computer.type" or len(sent_actions) != 1:
            pytest.fail("predispatch failure must not screenshot or retry input")
        return (
            {
                "action": action,
                "is_error": True,
                "error_code": "TYPE_COMPLETION_NOT_VERIFIED",
                "result": {
                    "input_dispatched": False,
                    "completion_verified": False,
                    "diagnostics": {
                        "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
                    },
                },
            },
            True,
            time.time(),
        )

    monkeypatch.setattr(debug, "_direct_approved_widget", approved_widget)
    reporter = debug.SmokeReporter(io.StringIO())

    with pytest.raises(debug.DirectTypeClassificationError) as raised:
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=viewer_root,
            direct_artifact_root=direct_artifact_root,
            reporter=reporter,
            run_nonce="predispatch-gate",
        )

    assert sent_actions == ["computer.type"]
    assert debug._direct_failure_report(raised.value) == {
        "error_code": "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
        "classification": "PRECONDITION_FAILED",
        "input_dispatched": False,
        "completion_verified": False,
    }


def test_direct_legacy_key_unverified_stops_before_type(tmp_path, monkeypatch):
    viewer_root = tmp_path / "viewer"
    direct_artifact_root = tmp_path / "direct" / "workspace" / "tools" / "computer"
    direct_artifact_root.mkdir(parents=True)
    sent_actions = []
    legacy_plan = [
        {
            "label": "legacy_address_focus",
            "action": "computer.key",
            "payload": {
                "app": "ChatGPT Atlas",
                "background": True,
                "focus": False,
                "include_screenshot": False,
                "key_combo": "command+l",
            },
            "wait_after": 0.0,
        },
        {
            "label": "legacy_address_type",
            "action": "computer.type",
            "payload": {
                "app": "ChatGPT Atlas",
                "background": True,
                "focus": False,
                "include_screenshot": False,
                "text": "must-not-be-dispatched",
            },
            "wait_after": 0.0,
        },
    ]
    monkeypatch.setattr(debug, "direct_background_action_plan", lambda _nonce: legacy_plan)
    monkeypatch.setattr(debug, "frontmost_application_name", lambda: "Codex")
    monkeypatch.setattr(debug, "_direct_context_sentinel", lambda _client: "codex")
    def ready_unapproved(_client, action, _payload):
        if action == "computer.probe_text_control":
            return _ready_probe_widget()
        return {
            "action": "computer.select_window",
                "selected": True,
                "selection_exact_binding_required": True,
                "selection_exact_binding_present": True,
                "selection_authoritative_permission_source": "swift_host",
                "selection_authoritative_permission_outcome": "permissions_ok",
                "selection_secondary_permission_outcome": "skipped_non_authoritative",
                "target_window": {
                "app": "ChatGPT Atlas",
                "pid": 123,
                "window_id": 456,
                "x": 0,
                "y": 0,
                "width": 1000,
                "height": 700,
            },
        }

    monkeypatch.setattr(debug, "_direct_unapproved_read_widget", ready_unapproved)

    def approved_widget(_client, _reporter, action, _payload):
        sent_actions.append(action)
        if action != "computer.key":
            pytest.fail("type must not be dispatched after an unverified key action")
        return (
            {
                "action": action,
                "is_error": True,
                "error_code": "KEY_EFFECT_NOT_VERIFIED",
                "result": {
                    "executed": True,
                    "delivered": True,
                    "completion_verified": False,
                },
            },
            True,
            time.time(),
        )

    monkeypatch.setattr(debug, "_direct_approved_widget", approved_widget)
    reporter = debug.SmokeReporter(io.StringIO())

    with pytest.raises(debug.SmokeRunnerError, match="KEY_EFFECT_NOT_VERIFIED"):
        debug.direct_computer_use_sequence(
            object(),
            run_dir=tmp_path / "run",
            viewer_user_data_root=viewer_root,
            direct_artifact_root=direct_artifact_root,
            reporter=reporter,
            run_nonce="legacy-key-gate",
        )

    assert sent_actions == ["computer.key"]


def test_direct_supervisor_failure_reporting_never_echoes_body_url_or_path():
    error = debug.DebugApiError(
        "private body https://private.example.test /private/path pid=12345 title=secret"
    )
    rendered = debug._direct_failure_code(error)

    assert rendered == "DIRECT_API_FAILED"
    assert "private" not in rendered
    assert "12345" not in rendered


def test_viewer_direct_supervisor_starts_provider_free_owned_pair_and_cleans_up(
    tmp_path, monkeypatch
):
    class Process:
        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

        def kill(self):
            self.returncode = -9

    class LogTee:
        wry_detached_panic = False

        def join(self, _timeout=1.0):
            return None

    viewer = Process()
    defaultspack = Process()
    run_root = tmp_path / "runs"
    api_token = tmp_path / "api-token"
    browser_token = tmp_path / "browser-token"
    api_token.write_text("local-api-secret", encoding="utf-8")
    browser_token.write_text("browser-api-secret", encoding="utf-8")
    monkeypatch.setattr(debug, "RUN_ROOT", run_root)
    monkeypatch.setattr(debug, "_has_live_pty", lambda: True)
    monkeypatch.setattr(
        debug,
        "load_connection",
        lambda path, **kwargs: ({}, {"ok": False, "connection": {}, "health": {}}),
    )
    viewer_kwargs = []

    def fake_start(path, **kwargs):
        viewer_kwargs.append(kwargs)
        return viewer, LogTee()

    monkeypatch.setattr(debug, "start_viewer_dev", fake_start)
    monkeypatch.setattr(
        debug,
        "wait_for_viewer_broker",
        lambda *args, **kwargs: {"ok": True, "connection": {"port": 18770}},
    )
    monkeypatch.setattr(debug, "port_is_open", lambda _port: False)
    launch_args = []

    def fake_launch(args, include_process=False):
        launch_args.append(args)
        assert include_process is True
        return {
            "ok": True,
            "launch": {
                "token_file": str(api_token),
                "browser_approval_token_file": str(browser_token),
            },
            "_process": defaultspack,
            "_log_tee": LogTee(),
        }

    monkeypatch.setattr(debug, "launch", fake_launch)
    direct_calls = []

    def fake_direct(client, **kwargs):
        direct_calls.append((client, kwargs))
        return {
            "ok": True,
            "provider_used": False,
            "chat_used": False,
            "model_used": False,
        }

    monkeypatch.setattr(debug, "direct_computer_use_sequence", fake_direct)

    result = debug.viewer_direct_computer_use(
        type(
            "Args",
            (),
            {
                "connection": None,
                "viewer_broker_port": 18770,
                "port": None,
                "defaultspack_http_port": None,
                "kernel_port": None,
                "wait_seconds": 1.0,
                "viewer_wait_seconds": 1.0,
                "viewer_min_free_mb": 1024,
            },
        )()
    )

    assert result["ok"] is True
    assert result["provider_used"] is False
    assert result["chat_used"] is False
    assert result["model_used"] is False
    assert viewer_kwargs[0]["isolated_provider_parent_env"] is None
    assert launch_args[0].isolated_provider_parent_env is None
    assert direct_calls
    assert direct_calls[0][1]["direct_artifact_root"] == (
        launch_args[0].defaultspack_debug_state_root
        / "chat"
        / "conversations"
        / "direct-http"
        / "workspace"
        / "tools"
        / "computer"
    )
    assert viewer.terminated is True
    assert defaultspack.terminated is True
