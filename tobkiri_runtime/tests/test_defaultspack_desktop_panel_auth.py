"""Real-process regressions for Desktop Panel auth initialization."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

_CHILD = r"""
import json
import sys

from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import get_panel_auth_manager
from defaultspack import desktop_app

manager = desktop_app._require_host_panel_auth_manager()
server = PackAPIServer(port=0, panel_auth_manager=manager)
server.start()
print(json.dumps({
    "port": server.port,
    "manager_id": id(manager),
    "singleton": manager is get_panel_auth_manager(),
    "server_manager": server._panel_auth_manager is manager,
}), flush=True)
if sys.stdin.readline().strip() != "restart":
    raise RuntimeError("restart command is required")
server.stop()
restarted_manager = desktop_app._require_host_panel_auth_manager()
server = PackAPIServer(port=0, panel_auth_manager=restarted_manager)
server.start()
print(json.dumps({
    "port": server.port,
    "manager_id": id(restarted_manager),
    "singleton": restarted_manager is get_panel_auth_manager(),
    "server_manager": server._panel_auth_manager is restarted_manager,
}), flush=True)
if sys.stdin.readline().strip() != "stop":
    raise RuntimeError("stop command is required")
server.stop()
"""


def _write_host_contract(root: Path, secret: str) -> Path:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    path = root / "host_contract.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "tobkiri.host-contract.v1",
                "profile_id": "defaults",
                "values": {"panel_bootstrap_secret": secret},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _request(
    port: int,
    path: str,
    *,
    body: object,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object], list[tuple[str, str]]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    encoded = json.dumps(body).encode("utf-8")
    request_headers = {"Content-Type": "application/json", **dict(headers or {})}
    connection.request("POST", path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    response_headers = response.getheaders()
    connection.close()
    return response.status, payload, response_headers


def _read_child_state(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise AssertionError(f"Panel auth child exited before readiness: {stderr}")
    return json.loads(line)


def test_desktop_panel_auth_uses_exact_host_contract_across_restart(
    tmp_path: Path,
) -> None:
    secret = "host-owned-panel-bootstrap-secret"
    user_data = tmp_path / "user-data"
    contract_path = _write_host_contract(user_data, secret)
    log_dir = tmp_path / "logs"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                (str(ROOT), str(DEFAULTSPACK_ROOT), env.get("PYTHONPATH", ""))
            ),
            "RUMI_LOG_DIR": str(log_dir),
            "RUMI_USER_DATA": str(user_data),
            "TOBKIRI_HOST_CONTRACT_PATH": str(contract_path),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    observed_payloads: list[dict[str, object]] = []
    try:
        first = _read_child_state(process)
        first_port = int(first["port"])
        assert first["singleton"] is True
        assert first["server_manager"] is True

        for headers, body in (
            ({}, {}),
            ({"X-Rumi-Desktop-Bootstrap": "wrong"}, {}),
            ({}, {"bootstrap_secret": secret}),
        ):
            status, payload, _ = _request(
                first_port,
                "/api/panel/auth/bootstrap",
                body=body,
                headers=headers,
            )
            observed_payloads.append(payload)
            assert status == 401

        status, bootstrap, _ = _request(
            first_port,
            "/api/panel/auth/bootstrap",
            body={},
            headers={"X-Rumi-Desktop-Bootstrap": secret},
        )
        observed_payloads.append(bootstrap)
        assert status == 200
        code = str(bootstrap["data"]["code"])

        assert process.stdin is not None
        process.stdin.write("restart\n")
        process.stdin.flush()
        restarted = _read_child_state(process)
        restarted_port = int(restarted["port"])
        assert restarted["manager_id"] == first["manager_id"]
        assert restarted["singleton"] is True
        assert restarted["server_manager"] is True

        origin = f"http://127.0.0.1:{restarted_port}"
        status, exchange, response_headers = _request(
            restarted_port,
            "/api/panel/auth/exchange",
            body={"code": code},
            headers={"Origin": origin},
        )
        observed_payloads.append(exchange)
        assert status == 200
        cookie = next(
            value
            for key, value in response_headers
            if key.lower() == "set-cookie"
        )
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert exchange["data"]["csrf_token"]

        replay_status, replay, _ = _request(
            restarted_port,
            "/api/panel/auth/exchange",
            body={"code": code},
            headers={"Origin": origin},
        )
        observed_payloads.append(replay)
        assert replay_status == 401

        process.stdin.write("stop\n")
        process.stdin.flush()
        remaining_stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        public_evidence = json.dumps(
            {
                "states": (first, restarted),
                "payloads": observed_payloads,
                "stdout": remaining_stdout,
                "stderr": stderr,
            },
            sort_keys=True,
        )
        assert secret not in public_evidence
        for path in user_data.rglob("*"):
            if path.is_file() and path != contract_path:
                assert secret.encode("utf-8") not in path.read_bytes()
        if log_dir.exists():
            assert all(secret.encode("utf-8") not in path.read_bytes() for path in log_dir.rglob("*") if path.is_file())
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_desktop_panel_auth_fails_closed_without_host_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from defaultspack import desktop_app

    user_data = tmp_path / "standalone-user-data"
    user_data.mkdir(mode=0o700)
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setenv(
        "TOBKIRI_HOST_CONTRACT_PATH",
        str(user_data / "host_contract.json"),
    )
    monkeypatch.setattr(
        desktop_app,
        "_restore_active_profile_contracts",
        lambda: (None, ()),
    )
    monkeypatch.setattr(desktop_app, "_write_launch_event", lambda *_args, **_kwargs: None)

    with patch("core_runtime.pack_api_server.PackAPIServer") as server_type:
        with pytest.raises(
            RuntimeError,
            match="Launcher-owned panel bootstrap secret is required",
        ):
            desktop_app.main([])
    server_type.assert_not_called()
