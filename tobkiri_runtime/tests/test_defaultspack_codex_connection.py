from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
SAFE_APP_SERVER_ARGS = [
    "-c",
    'approval_policy="untrusted"',
    "-c",
    'sandbox_mode="read-only"',
]

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _fresh_token() -> str:
    return f"codex-{secrets.token_urlsafe(24)}"


def _save_app_server_secret(pack_root: Path, value: str) -> dict[str, object]:
    """Persist app-server auth as its own opaque connection credential."""
    from domain.connections.store import import_connection_bundle

    return import_connection_bundle(
        {
            "schema": "rumi.connection.credential_bundle.v1",
            "provider_id": "codex",
            "connection_id": "default",
            "material_type": "app_server_secret",
            "credentials": {"ws_token": value},
        },
        pack_root=pack_root,
    )


def test_codex_token_status_and_route_responses_redact_raw_token():
    from blocks.connections import codex as codex_block
    from domain.codex.connection_store import codex_connection_status, save_codex_access_token

    with tempfile.TemporaryDirectory() as tmpdir:
        pack_root = Path(tmpdir)
        secrets_dir = pack_root / "user_data" / "secrets"
        token = _fresh_token()
        env = {
            "RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir),
            "RUMI_CODEX_ACCESS_TOKEN": "",
            "CODEX_ACCESS_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            saved = save_codex_access_token(token, pack_root=pack_root)
            status = codex_connection_status(pack_root=pack_root)
            routed = codex_block.run(
                {"_method": "POST", "action": "save_token", "access_token": token},
                {},
            )

            assert saved["success"] is True
            assert status["configured"] is True
            assert status["provider_kind"] == "codex"
            assert status["auth_type"] == "codex"
            assert status["platform_api_key_required"] is False
            assert [method["id"] for method in status["auth_methods"]] == [
                "chatgpt_account",
                "codex_access_token",
                "app_server_secret",
            ]
            assert status["active_auth_methods"] == ["codex_access_token"]
            assert routed["status"] == "ok"
            assert token not in _text(saved)
            assert token not in _text(status)
            assert token not in _text(routed)
            for path in secrets_dir.rglob("*"):
                if path.is_file():
                    assert token not in path.read_text(encoding="utf-8", errors="ignore")


def test_codex_connection_status_uses_secret_existence_without_decrypting():
    from domain.codex import connection_store

    with tempfile.TemporaryDirectory() as tmpdir:
        pack_root = Path(tmpdir)
        secrets_dir = pack_root / "user_data" / "secrets"
        token = _fresh_token()
        env = {
            "RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir),
            "RUMI_CODEX_ACCESS_TOKEN": "",
            "CODEX_ACCESS_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            saved = connection_store.save_codex_access_token(token, pack_root=pack_root)
            with patch.object(connection_store, "_read_secret_value", side_effect=AssertionError("status must not decrypt token")):
                status = connection_store.codex_connection_status(pack_root=pack_root)

    assert saved["success"] is True
    assert status["configured"] is True
    assert status["connected"] is True
    assert status["token_source"] == "secret_store"


def test_codex_app_server_remote_endpoint_requires_app_server_auth_not_codex_token():
    from domain.codex.app_server import codex_app_server_status, save_codex_app_server_config
    from domain.codex.connection_store import save_codex_access_token

    with tempfile.TemporaryDirectory() as tmpdir:
        pack_root = Path(tmpdir)
        secrets_dir = pack_root / "user_data" / "secrets"
        token = _fresh_token()
        app_secret = _fresh_token()
        env = {
            "RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir),
            "RUMI_CODEX_ACCESS_TOKEN": "",
            "CODEX_ACCESS_TOKEN": "",
            "RUMI_CODEX_APP_SERVER_WS_TOKEN": "",
            "RUMI_CODEX_APP_SERVER_SHARED_SECRET": "",
        }
        with patch.dict(os.environ, env, clear=False):
            saved_without_app_auth = save_codex_app_server_config(
                {
                    "enabled": True,
                    "base_url": "https://codex-app.example.test",
                    "tool_source_enabled": True,
                    "automation_endpoint_enabled": True,
                },
                pack_root=pack_root,
            )
            save_codex_access_token(token, pack_root=pack_root)
            status_with_only_codex_token = codex_app_server_status(pack_root=pack_root)
        with patch.dict(os.environ, env, clear=False):
            imported = _save_app_server_secret(pack_root, app_secret)
            saved_with_app_auth = save_codex_app_server_config(
                {
                    "enabled": True,
                    "transport": "websocket_remote",
                    "base_url": "https://codex-app.example.test",
                    "websocket_url": "wss://codex-app.example.test/ws",
                    "tool_source_enabled": True,
                    "automation_endpoint_enabled": True,
                },
                pack_root=pack_root,
            )
            status = codex_app_server_status(pack_root=pack_root)

    assert saved_without_app_auth["success"] is True
    assert saved_without_app_auth["app_server"]["connection_status"] == "blocked_auth_required"
    assert status_with_only_codex_token["connection_status"] == "blocked_auth_required"
    assert status_with_only_codex_token["auth_configured"] is False
    assert imported["success"] is True
    assert saved_with_app_auth["success"] is True
    assert status["auth_required"] is True
    assert status["auth_configured"] is True
    assert status["auth_source"] == "secret_store"
    assert status["auth_kind"] == "ws_token"
    assert token not in _text(saved_with_app_auth)
    assert token not in _text(status)
    assert app_secret not in _text(saved_with_app_auth)
    assert app_secret not in _text(status)


def test_codex_app_server_rejects_transport_url_mismatch_even_with_app_server_auth():
    from domain.codex.app_server import (
        build_codex_app_server_command,
        codex_app_server_probe,
        codex_app_server_status,
        save_codex_app_server_config,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        pack_root = Path(tmpdir)
        secrets_dir = pack_root / "user_data" / "secrets"
        app_secret = _fresh_token()
        env = {
            "RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir),
            "RUMI_CODEX_ACCESS_TOKEN": "",
            "CODEX_ACCESS_TOKEN": "",
            "RUMI_CODEX_APP_SERVER_WS_TOKEN": app_secret,
            "RUMI_CODEX_APP_SERVER_SHARED_SECRET": "",
        }
        config = {
            "enabled": True,
            "transport": "websocket_loopback",
            "base_url": "https://codex-app.example.test",
            "websocket_url": "wss://codex-app.example.test/ws",
            "tool_source_enabled": True,
            "automation_endpoint_enabled": True,
        }
        with patch.dict(os.environ, env, clear=False):
            imported = _save_app_server_secret(pack_root, app_secret)
            saved = save_codex_app_server_config(config, pack_root=pack_root)
            status = codex_app_server_status(pack_root=pack_root)
            probe = codex_app_server_probe(pack_root=pack_root)
            remote_saved = save_codex_app_server_config(
                {
                    "enabled": True,
                    "transport": "websocket_remote",
                    "base_url": "http://127.0.0.1:7331",
                    "websocket_url": "ws://127.0.0.1:7331/ws",
                    "tool_source_enabled": True,
                },
                pack_root=pack_root,
            )
            remote_status = codex_app_server_status(pack_root=pack_root)

    assert build_codex_app_server_command(config) == []
    assert imported["success"] is True
    assert saved["success"] is True
    assert saved["app_server"]["connection_status"] == "transport_url_mismatch"
    assert status["configured"] is False
    assert status["connection_status"] == "transport_url_mismatch"
    assert status["status_label"] == "Transport mismatch"
    assert status["transport_url_mismatch"] is True
    assert "loopback" in status["blocked_reason"]
    assert status["auth_required"] is True
    assert status["auth_configured"] is True
    assert status["command"] == []
    assert status["tool_source"]["status"] == "transport_url_mismatch"
    assert status["automation_endpoint"]["status"] == "transport_url_mismatch"
    assert probe["probe"]["status"] == "transport_url_mismatch"
    assert app_secret not in _text(saved)
    assert app_secret not in _text(status)
    assert app_secret not in _text(probe)
    assert remote_saved["app_server"]["connection_status"] == "transport_url_mismatch"
    assert remote_status["configured"] is False
    assert remote_status["connection_status"] == "transport_url_mismatch"
    assert "non-loopback" in remote_status["blocked_reason"]


def test_codex_app_server_rejects_query_secret_urls_without_echoing_secret():
    from domain.codex.app_server import (
        build_codex_app_server_command,
        codex_app_server_probe,
        codex_app_server_status,
        save_codex_app_server_config,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        pack_root = Path(tmpdir)
        secrets_dir = pack_root / "user_data" / "secrets"
        raw_secret = _fresh_token()
        env = {
            "RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir),
            "RUMI_CODEX_APP_SERVER_WS_TOKEN": _fresh_token(),
            "RUMI_CODEX_APP_SERVER_SHARED_SECRET": "",
        }
        config = {
            "enabled": True,
            "transport": "websocket_loopback",
            "base_url": f"http://127.0.0.1:7331?access_token={raw_secret}",
            "websocket_url": f"ws://127.0.0.1:7331/ws?token={raw_secret}",
            "tool_source_enabled": True,
            "automation_endpoint_enabled": True,
        }
        with patch.dict(os.environ, env, clear=False):
            saved = save_codex_app_server_config(config, pack_root=pack_root)
            status = codex_app_server_status(pack_root=pack_root)
            probe = codex_app_server_probe(pack_root=pack_root)

    assert build_codex_app_server_command(config) == []
    assert saved["success"] is True
    assert saved["app_server"]["connection_status"] == "url_secret_rejected"
    assert status["configured"] is False
    assert status["connection_status"] == "url_secret_rejected"
    assert status["status_label"] == "URL secret rejected"
    assert status["url_secret_rejected"] is True
    assert status["base_url"] == ""
    assert status["websocket_url"] == ""
    assert status["command"] == []
    assert status["tool_source"]["status"] == "url_secret_rejected"
    assert status["automation_endpoint"]["status"] == "url_secret_rejected"
    assert probe["probe"]["status"] == "url_secret_rejected"
    assert raw_secret not in _text(saved)
    assert raw_secret not in _text(status)
    assert raw_secret not in _text(probe)


def test_codex_app_server_probe_never_uses_codex_access_token_for_app_server_auth():
    from domain.codex.app_server import codex_app_server_probe, save_codex_app_server_config
    from domain.codex.connection_store import save_codex_access_token

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    with tempfile.TemporaryDirectory() as tmpdir:
        pack_root = Path(tmpdir)
        secrets_dir = pack_root / "user_data" / "secrets"
        codex_token = _fresh_token()
        app_secret = _fresh_token()
        captured_headers: dict[str, str] = {}
        env = {
            "RUMI_DEFAULTSPACK_SECRETS_DIR": str(secrets_dir),
            "RUMI_CODEX_ACCESS_TOKEN": "",
            "CODEX_ACCESS_TOKEN": "",
            "RUMI_CODEX_APP_SERVER_WS_TOKEN": "",
            "RUMI_CODEX_APP_SERVER_SHARED_SECRET": "",
        }

        def fake_urlopen(request, timeout):
            del timeout
            captured_headers.update(dict(request.header_items()))
            assert request.full_url == "https://codex-app.example.test/readyz"
            return FakeResponse()

        with patch.dict(os.environ, env, clear=False):
            save_codex_access_token(codex_token, pack_root=pack_root)
            save_codex_app_server_config(
                {
                    "enabled": True,
                    "transport": "websocket_remote",
                    "base_url": "https://codex-app.example.test",
                    "websocket_url": "wss://codex-app.example.test/ws",
                },
                pack_root=pack_root,
            )
            blocked = codex_app_server_probe(pack_root=pack_root)

        with patch.dict(os.environ, env, clear=False):
            imported = _save_app_server_secret(pack_root, app_secret)
            with patch("urllib.request.urlopen", fake_urlopen):
                probed = codex_app_server_probe(pack_root=pack_root)

    assert blocked["probe"]["status"] == "blocked_auth_required"
    assert imported["success"] is True
    assert probed["probe"]["status"] == "ok"
    assert captured_headers["Authorization"] == f"Bearer {app_secret}"
    assert codex_token not in _text(captured_headers)
    assert app_secret not in _text(probed)


def test_codex_app_server_transport_command_uses_file_paths_not_raw_tokens(tmp_path):
    from domain.codex.app_server import build_codex_app_server_command, codex_app_server_status, save_codex_app_server_config

    app_secret = _fresh_token()
    token_file = tmp_path / "codex-app-server.token"
    token_file.write_text(app_secret, encoding="utf-8")

    assert build_codex_app_server_command({}) == []

    command = build_codex_app_server_command(
        {
            "enabled": True,
            "transport": "unix",
            "unix_socket_path": "/tmp/rumi-codex.sock",
            "ws_token_file": str(token_file),
            "ws_token": app_secret,
        }
    )

    assert command == [
        "codex",
        "app-server",
        *SAFE_APP_SERVER_ARGS,
        "--listen",
        "unix:///tmp/rumi-codex.sock",
        "--ws-auth",
        "capability-token",
        "--ws-token-file",
        str(token_file),
    ]
    assert app_secret not in _text(command)

    with tempfile.TemporaryDirectory() as tmpdir:
        pack_root = Path(tmpdir)
        saved = save_codex_app_server_config(
            {
                "enabled": True,
                "transport": "stdio",
                "tool_source_enabled": True,
            },
            pack_root=pack_root,
        )
        status = codex_app_server_status(pack_root=pack_root)

    assert saved["success"] is True
    assert status["transport"] == "stdio"
    assert status["connection_status"] == "configured"
    assert status["command"] == ["codex", "app-server", *SAFE_APP_SERVER_ARGS, "--listen", "stdio://"]


def test_codex_app_server_stdio_smoke_runs_thread_turn_and_streams_events(monkeypatch):
    from core_runtime.host_contract import bind_host_contract
    from domain.codex import app_server

    token = _fresh_token()
    created: dict[str, object] = {}

    class FakeStdout:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def push(self, payload: dict[str, object]) -> None:
            self.lines.append(json.dumps(payload) + "\n")

        def readline(self) -> str:
            return self.lines.pop(0) if self.lines else ""

    class FakeStdin:
        def __init__(self, stdout: FakeStdout) -> None:
            self.stdout = stdout
            self.messages: list[dict[str, object]] = []

        def write(self, text: str) -> int:
            payload = json.loads(text)
            self.messages.append(payload)
            if payload.get("method") == "initialize":
                self.stdout.push({"id": 0, "result": {"platformFamily": "macos"}})
            if payload.get("method") == "thread/start":
                self.stdout.push({"id": 1, "result": {"thread": {"id": "thr_smoke"}}})
            if payload.get("method") == "turn/start":
                self.stdout.push({"id": 2, "result": {"turn": {"id": "turn_smoke"}}})
                self.stdout.push(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thr_smoke",
                            "turnId": "turn_smoke",
                            "itemId": "item_1",
                            "delta": f"rumi-codex-smoke-ok {token}",
                        },
                    }
                )
                self.stdout.push(
                    {
                        "method": "turn/completed",
                        "params": {"threadId": "thr_smoke", "turn": {"id": "turn_smoke"}},
                    }
                )
            return len(text)

        def flush(self) -> None:
            return None

    class FakeProcess:
        def __init__(self, command: list[str], **_kwargs: object) -> None:
            created["command"] = command
            self.stdout = FakeStdout()
            self.stdin = FakeStdin(self.stdout)
            self.stderr = None
            created["process"] = self

        def terminate(self) -> None:
            created["terminated"] = True

        def wait(self, timeout: float | None = None) -> int:
            created["wait_timeout"] = timeout
            return 0

        def kill(self) -> None:
            created["killed"] = True

    monkeypatch.setattr(app_server.subprocess, "Popen", FakeProcess)
    with bind_host_contract(
        {
            "profile_id": "default",
            "values": {"RUMI_CODEX_ACCESS_TOKEN": token},
        }
    ):
        result = app_server.codex_app_server_stdio_smoke(
            prompt="Hello. Return exactly: rumi-codex-smoke-ok",
            cwd=str(ROOT),
            model="gpt-5.4",
            timeout=2,
        )

    process = created["process"]
    assert result["success"] is True
    assert result["command"] == ["codex", "app-server", *SAFE_APP_SERVER_ARGS, "--listen", "stdio://"]
    assert result["thread_id"] == "thr_smoke"
    assert result["turn_id"] == "turn_smoke"
    assert "rumi-codex-smoke-ok" in result["final_output"]
    assert token not in _text(result)
    assert result["sent_methods"] == ["initialize", "initialized", "thread/start", "turn/start"]
    sent_messages = process.stdin.messages
    assert sent_messages[0]["params"]["clientInfo"]["name"] == "rumi_defaultspack"
    assert sent_messages[2]["params"] == {"model": "gpt-5.4", "cwd": str(ROOT)}
    assert sent_messages[3]["params"]["threadId"] == "thr_smoke"
    assert sent_messages[3]["params"]["input"] == [
        {"type": "text", "text": "Hello. Return exactly: rumi-codex-smoke-ok"}
    ]
    assert created["terminated"] is True


def test_codex_app_server_stdio_smoke_accepts_final_delta_idle(monkeypatch):
    from domain.codex import app_server

    class FakeStdout:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def push(self, payload: dict[str, object]) -> None:
            self.lines.append(json.dumps(payload) + "\n")

        def readline(self) -> str:
            return self.lines.pop(0) if self.lines else ""

    class FakeStdin:
        def __init__(self, stdout: FakeStdout) -> None:
            self.stdout = stdout

        def write(self, text: str) -> int:
            payload = json.loads(text)
            if payload.get("method") == "thread/start":
                self.stdout.push({"id": 1, "result": {"thread": {"id": "thr_smoke"}}})
            if payload.get("method") == "turn/start":
                self.stdout.push({"id": 2, "result": {"turn": {"id": "turn_smoke"}}})
                self.stdout.push(
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": "thr_smoke",
                            "turnId": "turn_smoke",
                            "itemId": "item_1",
                            "delta": "rumi-codex-smoke-ok",
                        },
                    }
                )
            return len(text)

        def flush(self) -> None:
            return None

    class FakeProcess:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.stdout = FakeStdout()
            self.stdin = FakeStdin(self.stdout)
            self.stderr = None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr(app_server.subprocess, "Popen", FakeProcess)
    result = app_server.codex_app_server_stdio_smoke(prompt="hello", timeout=2)

    assert result["success"] is True
    assert result["thread_id"] == "thr_smoke"
    assert result["turn_id"] == "turn_smoke"
    assert result["final_output"] == "rumi-codex-smoke-ok"
    assert result["error"] == ""


def test_codex_app_server_stdio_smoke_surfaces_approval_requests(monkeypatch):
    from domain.codex import app_server

    class FakeStdout:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def push(self, payload: dict[str, object]) -> None:
            self.lines.append(json.dumps(payload) + "\n")

        def readline(self) -> str:
            return self.lines.pop(0) if self.lines else ""

    class FakeStdin:
        def __init__(self, stdout: FakeStdout) -> None:
            self.stdout = stdout

        def write(self, text: str) -> int:
            payload = json.loads(text)
            if payload.get("method") == "thread/start":
                self.stdout.push({"id": 1, "result": {"thread": {"id": "thr_smoke"}}})
            if payload.get("method") == "turn/start":
                self.stdout.push({"id": 2, "result": {"turn": {"id": "turn_smoke"}}})
                self.stdout.push(
                    {
                        "method": "file-change/requestApproval",
                        "params": {"threadId": "thr_smoke", "turnId": "turn_smoke"},
                    }
                )
            return len(text)

        def flush(self) -> None:
            return None

    class FakeProcess:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.stdout = FakeStdout()
            self.stdin = FakeStdin(self.stdout)
            self.stderr = None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr(app_server.subprocess, "Popen", FakeProcess)

    result = app_server.codex_app_server_stdio_smoke(prompt="write a file", timeout=0.2)

    assert result["success"] is False
    assert result["approval_required"] is True
    assert result["approval_requests"][0]["method"] == "file-change/requestApproval"
    assert result["error"] == "turn_not_completed"


def test_codex_app_server_probe_reads_and_caches_chatgpt_account(monkeypatch):
    from domain.codex import app_server

    created: dict[str, object] = {}

    class FakeStdout:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def push(self, payload: dict[str, object]) -> None:
            self.lines.append(json.dumps(payload) + "\n")

        def readline(self) -> str:
            return self.lines.pop(0) if self.lines else ""

    class FakeStdin:
        def __init__(self, stdout: FakeStdout) -> None:
            self.stdout = stdout
            self.messages: list[dict[str, object]] = []

        def write(self, text: str) -> int:
            payload = json.loads(text)
            self.messages.append(payload)
            if payload.get("method") == "initialize":
                self.stdout.push({"id": 0, "result": {"platformFamily": "macos"}})
            if payload.get("method") == "account/read":
                self.stdout.push(
                    {
                        "id": 1,
                        "result": {
                            "account": {
                                "type": "chatgpt",
                                "email": "rumi-user@example.test",
                                "planType": "prolite",
                            },
                            "requiresOpenaiAuth": True,
                        },
                    }
                )
            return len(text)

        def flush(self) -> None:
            return None

    class FakeProcess:
        def __init__(self, command: list[str], **_kwargs: object) -> None:
            created["command"] = command
            self.stdout = FakeStdout()
            self.stdin = FakeStdin(self.stdout)
            self.stderr = None
            created["process"] = self

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr(app_server.subprocess, "Popen", FakeProcess)

    with tempfile.TemporaryDirectory() as tmpdir:
        pack_root = Path(tmpdir)
        result = app_server.codex_app_server_probe(pack_root=pack_root, timeout=0.2)
        status = app_server.codex_app_server_status(pack_root=pack_root)

    process = created["process"]
    assert result["success"] is True
    assert result["probe"]["status"] == "ok"
    assert result["account"] == {
        "provider_id": "codex",
        "provider_kind": "codex",
        "type": "chatgpt",
        "auth_method": "chatgpt_account",
        "auth_method_label": "ChatGPT account",
        "account_label": "rumi-user@example.test",
        "email": "rumi-user@example.test",
        "plan_type": "prolite",
        "requires_openai_auth": True,
    }
    assert status["account"] == result["account"]
    assert status["provider_kind"] == "codex"
    assert status["auth_type"] == "codex"
    assert [method["id"] for method in status["auth_methods"]] == ["chatgpt_account", "app_server_secret"]
    assert created["command"] == ["codex", "app-server", *SAFE_APP_SERVER_ARGS, "--listen", "stdio://"]
    assert result["sent_methods"] == ["initialize", "initialized", "account/read"]
    sent_messages = process.stdin.messages
    assert sent_messages[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert sent_messages[2]["params"] == {"refreshToken": False}


def test_frontend_registry_drops_client_supplied_codex_secret_payloads():
    from domain.frontend.registry import FrontendRegistry

    with tempfile.TemporaryDirectory() as tmpdir:
        pack_root = Path(tmpdir)
        token = _fresh_token()
        env = {
            "RUMI_DEFAULTSPACK_SECRETS_DIR": str(pack_root / "user_data" / "secrets"),
            "RUMI_CODEX_ACCESS_TOKEN": "",
            "CODEX_ACCESS_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            values = FrontendRegistry(pack_root=pack_root).update_settings(
                {
                    "accounts_connections": {
                        "providers": {
                            "codex": {
                                "access_token": token,
                                "token": token,
                                "configured": True,
                            },
                        },
                    },
                    "tools_mcp": {
                        "codex_app_server": {
                            "token": token,
                            "websocket_url": "wss://codex-app.example.test/ws",
                        },
                    },
                }
            )

    assert values["accounts_connections"]["providers"]["codex"]["configured"] is False
    assert values["tools_mcp"]["codex_app_server"]["connection_status"] == "not_configured"
    assert token not in _text(values)
