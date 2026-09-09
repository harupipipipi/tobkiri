"""Real stdio resource ownership; Broker admission is tested separately."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from core_runtime.mcp.connection_owner import (
    CALL,
    CONNECT,
    CONTRACT_ID,
    DISCONNECT,
    CapturedMcpConnectionOwner,
)
from core_runtime.mcp.transport import McpConnections
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.models import OpaqueAuthorityRef


class _Invocation:
    """Supply Host-private context without pretending to issue approval."""

    def __init__(self, operation: str, payload: dict[str, Any]) -> None:
        self.presentation_owner_principal_id = "origin"
        self.presentation_owner_session_id = "origin-session"
        self.envelope = RequestEnvelope(
            context=SimpleNamespace(
                profile_id="defaults", activation_id="active", plan_digest="plan", security_epoch=1
            ),
            target_principal=OpaqueAuthorityRef("gateway"),
            target_domain=OpaqueAuthorityRef("domain"),
            contract_id=CONTRACT_ID,
            contract_version="1.0.0",
            operation_id=operation,
            payload=payload,
            request_digest="broker-digest",
            deadline_monotonic=time.monotonic() + 5,
            lease=None,
            idempotency_key=None,
        )
        self.stale = False
        self.client_requests: list[dict[str, Any]] = []

    def assert_current(self) -> None:
        if self.stale:
            raise PermissionError("stale capture")

    def contract_client(self, **kwargs: Any) -> None:
        self.client_requests.append(kwargs)


@pytest.fixture
def owner(tmp_path: Path):
    value = CapturedMcpConnectionOwner(
        profile_id="defaults",
        activation_id="active",
        plan_digest="plan",
        security_epoch=1,
        principal_id="gateway",
        consumer_pack_id="mcp-owner",
        workspace_root=tmp_path,
    )
    yield value
    value.close()


@pytest.fixture
def connection_request(tmp_path: Path) -> dict[str, Any]:
    server = tmp_path / "server.py"
    server.write_text("""import json, os, sys, time
from pathlib import Path
for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    if method == "initialize":
        result = {"capabilities": {"tools": {}}}
    elif method == "tools/list":
        result = {"tools": [{"name": "ping"}, {"name": "blocked"}, {"name": "wait"}]}
    elif method == "tools/call":
        with Path("calls.jsonl").open("a") as output:
            output.write(json.dumps(message["params"]) + "\\n")
        if message["params"]["name"] == "wait":
            time.sleep(20)
        result = {"content": [{"type": "text", "text": json.dumps({
            "literal": os.environ.get("LITERAL"),
            "ambient": os.environ.get("MCP_TEST_AMBIENT"),
            "argv": sys.argv[1:],
            "arguments": message["params"]["arguments"],
        })}]}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
""")
    return {
        "server_id": "fixture",
        "allowed_tools": ["ping", "wait"],
        "config": {
            "transport": "stdio",
            "command": [sys.executable, str(server), ""],
            "env": {"LITERAL": "${MCP_TEST_AMBIENT}"},
            "cwd": ".",
        },
    }


def _call(connection_id: str, **updates: Any) -> _Invocation:
    return _Invocation(
        CALL, {"connection_id": connection_id, "tool": "ping", "arguments": {}, **updates}
    )


def test_owned_real_connection_binds_session_tools_and_exact_environment(
    owner,
    connection_request,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("MCP_TEST_AMBIENT", "must-not-inherit")
    invocation = _Invocation(CONNECT, connection_request)
    connected = owner.invoke(invocation)
    connection_id = connected["connection_id"]
    assert connected["tools"] == ["ping", "wait"]
    assert invocation.client_requests == [
        {
            "allowed_contract_ids": frozenset(),
            "consumer_pack_id": "mcp-owner",
            "include_credentials": False,
        }
    ]
    result = owner.invoke(_call(connection_id, arguments={"value": 42}))
    assert json.loads(result["result"]) == {
        "literal": "${MCP_TEST_AMBIENT}",
        "ambient": None,
        "arguments": {"value": 42},
        "argv": [""],
    }
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 1
    process = owner._connections._servers[connection_id]._transport._proc
    owner.invoke(_Invocation(DISCONNECT, {"connection_id": connection_id}))
    assert process.poll() is not None
    with pytest.raises(PermissionError):
        owner.invoke(_call(connection_id))


@pytest.mark.parametrize(
    "change",
    [
        "session",
        "principal",
        "profile",
        "activation",
        "plan",
        "epoch",
        "target",
        "contract",
        "version",
        "tool",
        "cancel",
        "expired",
        "stale",
        "approved",
    ],
)
def test_wrong_scope_never_reaches_connected_child(owner, connection_request, tmp_path, change):
    connection_id = owner.invoke(_Invocation(CONNECT, connection_request))["connection_id"]
    invocation = _call(connection_id)
    if change in {"session", "principal"}:
        field = (
            "presentation_owner_session_id"
            if change == "session"
            else "presentation_owner_principal_id"
        )
        setattr(invocation, field, "other")
    elif change in {"profile", "activation", "plan", "epoch"}:
        field = {
            "profile": "profile_id",
            "activation": "activation_id",
            "plan": "plan_digest",
            "epoch": "security_epoch",
        }[change]
        setattr(invocation.envelope.context, field, 2 if change == "epoch" else "other")
    elif change == "target":
        invocation.envelope = replace(
            invocation.envelope, target_principal=OpaqueAuthorityRef("other")
        )
    elif change in {"contract", "version"}:
        invocation.envelope = replace(
            invocation.envelope,
            **{"contract_id" if change == "contract" else "contract_version": "other"},
        )
    elif change == "tool":
        invocation.envelope.payload["tool"] = "blocked"
    elif change == "cancel":
        invocation.envelope.cancellation_requested.set()
    elif change == "expired":
        invocation.envelope = replace(invocation.envelope, deadline_monotonic=0)
    elif change == "stale":
        invocation.stale = True
    else:
        invocation.envelope.payload["approved"] = True
    with pytest.raises((PermissionError, ValueError, InterruptedError, TimeoutError)):
        owner.invoke(invocation)
    assert not (tmp_path / "calls.jsonl").exists()


def test_cancelled_in_flight_request_is_not_replayed(owner, connection_request, tmp_path):
    connection_id = owner.invoke(_Invocation(CONNECT, connection_request))["connection_id"]
    invocation = _call(connection_id, tool="wait")
    failures = []

    def run() -> None:
        try:
            owner.invoke(invocation)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        until = time.monotonic() + 3
        while not (tmp_path / "calls.jsonl").exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert (tmp_path / "calls.jsonl").exists()
        invocation.envelope.cancellation_requested.set()
        thread.join(1)
        assert not thread.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], InterruptedError)
        assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 1
    finally:
        owner.close()
        thread.join(3)


def test_unknown_tool_during_registration_reaps_child(owner, connection_request):
    connection_request["allowed_tools"] = ["not-discovered"]
    with pytest.raises(RuntimeError, match="connection failed"):
        owner.invoke(_Invocation(CONNECT, connection_request))
    assert owner._records == {}
    assert owner._connections.list_servers() == []


def test_close_retains_failed_cleanup_for_retry(owner, connection_request, monkeypatch):
    connection_id = owner.invoke(_Invocation(CONNECT, connection_request))["connection_id"]
    original = owner._connections.close
    monkeypatch.setattr(
        owner._connections,
        "close",
        lambda: (_ for _ in ()).throw(RuntimeError("fixture cleanup failure")),
    )
    with pytest.raises(RuntimeError):
        owner.close()
    assert connection_id in owner._records
    with pytest.raises(PermissionError, match="closed"):
        owner.invoke(_call(connection_id))
    monkeypatch.setattr(owner._connections, "close", original)
    owner.close()
    assert owner._records == {}


def test_stale_registration_does_not_create_a_transport(owner, connection_request, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("transport must not start")

    monkeypatch.setattr(McpConnections, "connect", forbidden)
    invocation = _Invocation(CONNECT, connection_request)
    invocation.stale = True
    with pytest.raises(PermissionError, match="stale"):
        owner.invoke(invocation)
    assert owner._records == {}
