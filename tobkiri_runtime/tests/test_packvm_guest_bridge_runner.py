"""Focused root-agent tests for the isolated PackVM bridge continuation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import threading
from typing import Any

import pytest

from ecosystem.defaultspack.backend.sandbox.isolation.resources import (
    packvm_guest_runner as runner,
)


class _FakeSigner:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def sign(self, payload: bytes) -> bytes:
        self.payloads.append(payload)
        return hashlib.sha512(payload).digest()


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        runner._bridge_canonical_json(value)
    ).hexdigest()


def _config() -> runner._VsockAgentConfig:
    bindings = {
        key: "sha256:" + character * 64
        for key, character in zip(
            (
                "domain",
                "lease",
                "reservation",
                "image",
                "agent",
                "config",
                "disk",
                "guest_public_key",
                "efi_variable_store",
                "artifact",
                "executable",
                "materialization",
            ),
            "123456789abc",
            strict=True,
        )
    }
    return runner._VsockAgentConfig(
        domain_id="packvm:domain-1",
        binding_digests=bindings,
        private_key_path=runner.PACKVM_GUEST_AGENT_KEY,
    )


def _guest_artifact_identity(config: runner._VsockAgentConfig) -> str:
    """Return the launch-bound identity accepted by the guest ABI."""

    return _digest(config.binding_digests)


def _bridge_request() -> dict[str, object]:
    request = {
        "messages": [{"role": "user", "content": "hello"}],
        "requirements": {"request_surface": "defaultspack.conversation"},
    }
    request_digest = _digest(request)
    target = dict(runner.PACKVM_BRIDGE_TARGET)
    continuation = {
        "kind": runner.PACKVM_CONTINUATION_KIND,
        "protocol": runner.PACKVM_BRIDGE_PROTOCOL,
        "version": runner.PACKVM_BRIDGE_VERSION,
        "operation_id": "complete",
        "nonce": "a" * 48,
        "target": target,
        "request_digest": request_digest,
    }
    return {
        "kind": runner.PACKVM_BRIDGE_REQUEST_KIND,
        "protocol": runner.PACKVM_BRIDGE_PROTOCOL,
        "version": runner.PACKVM_BRIDGE_VERSION,
        "target": target,
        "request": request,
        "request_digest": request_digest,
        "continuation": continuation,
    }


def _bridge_result(bridge_request: dict[str, object]) -> dict[str, object]:
    continuation = bridge_request["continuation"]
    assert isinstance(continuation, dict)
    outcome = {"status": "ok", "value": {"output": "safe Host result"}}
    return {
        "kind": runner.PACKVM_BRIDGE_RESULT_KIND,
        "protocol": runner.PACKVM_BRIDGE_PROTOCOL,
        "version": runner.PACKVM_BRIDGE_VERSION,
        "operation_id": "complete",
        "nonce": continuation["nonce"],
        "target": continuation["target"],
        "request_digest": continuation["request_digest"],
        "result": outcome,
        "result_digest": _digest(outcome),
    }


def _invoke_payload(
    request_id: str,
    config: runner._VsockAgentConfig | None = None,
) -> dict[str, object]:
    config = config or _config()
    return {
        "operation": "invoke",
        "request_id": request_id,
        "target_domain": "packvm:domain-1",
        "artifact_digest": "sha256:" + "1" * 64,
        "materialization_digest": "sha256:" + "2" * 64,
        "guest_artifact_identity": _guest_artifact_identity(config),
        "contract_id": "tobkiri.service.conversation.turn.v1",
        "contract_version": "1.0.0",
        "operation_id": "defaultspack.conversation.complete",
        "payload": {"messages": [{"role": "user", "content": "hello"}]},
        "request_digest": "sha256:" + "4" * 64,
        "deadline_monotonic": 123.5,
        "cancel_token": "5" * 64,
    }


def _envelope(
    config: runner._VsockAgentConfig,
    operation: str,
    request_id: str,
    challenge: str,
    **extra: object,
) -> dict[str, object]:
    return {
        "protocol": runner.PROTOCOL,
        "operation": operation,
        "request_id": request_id,
        "domain_id": config.domain_id,
        "binding_digests": config.binding_digests,
        "guest_challenge": challenge,
        **extra,
    }


def _host_result(
    config: runner._VsockAgentConfig,
    request_id: str,
    bridge_request: dict[str, object],
) -> dict[str, object]:
    bridge_result = _bridge_result(bridge_request)
    return {
        "kind": runner.PACKVM_BRIDGE_HOST_RESULT_KIND,
        "protocol": runner.PACKVM_BRIDGE_PROTOCOL,
        "version": runner.PACKVM_BRIDGE_VERSION,
        "request_id": request_id,
        "target_domain": config.domain_id,
        "guest_artifact_identity": _guest_artifact_identity(config),
        "request_digest": "sha256:" + "4" * 64,
        "bridge_request_digest": _digest(bridge_request),
        "continuation_nonce": "a" * 48,
        "bridge_result": bridge_result,
        "bridge_result_digest": _digest(bridge_result),
    }


def _roundtrip(
    request: dict[str, object],
    config: runner._VsockAgentConfig,
    ledger: runner._PendingBridgeLedger,
    signer: _FakeSigner,
) -> dict[str, Any]:
    client, agent = socket.socketpair()
    error: list[BaseException] = []

    def serve() -> None:
        try:
            runner._serve_agent_connection(agent, config, signer, ledger)
        except BaseException as exc:  # pragma: no cover - aid assertions
            error.append(exc)
        finally:
            agent.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        client.sendall(runner._bridge_canonical_json(request) + b"\n")
        chunks: list[bytes] = []
        while chunk := client.recv(4096):
            chunks.append(chunk)
    finally:
        client.close()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not error
    encoded = b"".join(chunks)
    assert encoded.endswith(b"\n")
    response = json.loads(encoded[:-1])
    assert isinstance(response, dict)
    assert response["agent_signature"]
    return response


def test_guest_agent_persists_only_a_verified_pending_bridge_then_resumes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Untrusted Pack output becomes a one-shot Host bridge, never an FD call."""

    config = _config()
    ledger = runner._PendingBridgeLedger()
    signer = _FakeSigner()
    bridge_request = _bridge_request()
    invoked = _invoke_payload("request-1")
    monkeypatch.setattr(
        runner,
        "_invoke",
        lambda _payload: {
            "ok": True,
            "protocol": runner.PROTOCOL,
            "guest_artifact_identity": _guest_artifact_identity(config),
            "payload": bridge_request,
        },
    )
    resumed: list[dict[str, object]] = []

    def resume(
        payload: dict[str, object],
        stored_bridge: dict[str, object],
        result: dict[str, object],
    ) -> dict[str, object]:
        resumed.extend((payload, stored_bridge, result))
        return {
            "ok": True,
            "protocol": runner.PROTOCOL,
            "guest_artifact_identity": _guest_artifact_identity(config),
            "payload": {"content": [{"type": "text", "text": "done"}]},
        }

    monkeypatch.setattr(runner, "_resume_bridge_invocation", resume)
    initial = _roundtrip(
        _envelope(
            config,
            "invoke",
            "request-1",
            "a" * 64,
            payload=invoked,
        ),
        config,
        ledger,
        signer,
    )
    assert initial["success"] is True
    pending = initial["data"]
    assert pending["state"] == "pending"
    host_request = pending["host_bridge_request"]
    assert host_request["kind"] == "tobkiri.packvm.bridge.host-request.v1"
    assert host_request["target_domain"] == config.domain_id
    assert host_request["guest_artifact_identity"] == _guest_artifact_identity(config)
    assert host_request["bridge_request"] == bridge_request
    assert host_request["deadline_monotonic"] == "123.5"

    bridge_result = _bridge_result(bridge_request)
    host_result = _host_result(config, "request-1", bridge_request)
    final = _roundtrip(
        _envelope(
            config,
            "bridge_result",
            "request-1",
            "b" * 64,
            host_bridge_result=host_result,
        ),
        config,
        ledger,
        signer,
    )
    assert final["success"] is True, json.dumps(final, indent=2, sort_keys=True)
    assert final["data"]["content"][0]["text"] == "done"
    assert resumed[0] == invoked
    assert resumed[1] == bridge_request
    assert resumed[2] == bridge_result

    replay = _roundtrip(
        _envelope(
            config,
            "bridge_result",
            "request-1",
            "c" * 64,
            host_bridge_result=host_result,
        ),
        config,
        ledger,
        signer,
    )
    assert replay["success"] is False
    assert resumed[2] == bridge_result


@pytest.mark.parametrize(
    "field",
    ("request_id", "target_domain", "guest_artifact_identity", "continuation_nonce"),
)
def test_guest_agent_rejects_swapped_or_replayed_host_result(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """Every resumed result is tied to one domain, request, and continuation."""

    config = _config()
    ledger = runner._PendingBridgeLedger()
    signer = _FakeSigner()
    bridge_request = _bridge_request()
    monkeypatch.setattr(
        runner,
        "_invoke",
        lambda _payload: {
            "ok": True,
            "protocol": runner.PROTOCOL,
            "guest_artifact_identity": _guest_artifact_identity(config),
            "payload": bridge_request,
        },
    )
    _roundtrip(
        _envelope(
            config,
            "invoke",
            "request-2",
            "d" * 64,
            payload=_invoke_payload("request-2"),
        ),
        config,
        ledger,
        signer,
    )
    host_result = _host_result(config, "request-2", bridge_request)
    host_result[field] = "swapped"
    rejected = _roundtrip(
        _envelope(
            config,
            "bridge_result",
            "request-2",
            "e" * 64,
            host_bridge_result=host_result,
        ),
        config,
        ledger,
        signer,
    )
    assert rejected["success"] is False


def test_guest_agent_attests_launch_bindings_with_a_root_only_signature() -> None:
    """The helper gets a challenge-bound signed identity before it invokes Pack."""

    config = _config()
    signer = _FakeSigner()
    response = _roundtrip(
        _envelope(
            config,
            "attest",
            f"attest-{config.domain_id}",
            "f" * 64,
            attestation_nonce="1" * 64,
        ),
        config,
        runner._PendingBridgeLedger(),
        signer,
    )
    assert response["success"] is True
    assert response["attestation_nonce"] == "1" * 64
    assert response["data"] == {
        "guest_artifact_identity": _digest(config.binding_digests)
    }
    unsigned = dict(response)
    unsigned.pop("agent_signature")
    assert signer.payloads == [runner._bridge_canonical_json(unsigned)]


def test_cancel_fences_a_bridge_that_finishes_racing_with_its_cancel() -> None:
    """A cancellation cannot lose the race to initial-child bridge persistence."""

    ledger = runner._PendingBridgeLedger()
    bridge_request = _bridge_request()
    ledger.cancel(domain_id="packvm:domain-1", request_id="request-race")
    with pytest.raises(ValueError, match="was cancelled"):
        ledger.add(
            domain_id="packvm:domain-1",
            request={"request_id": "request-race"},
            guest_artifact_identity="sha256:" + "3" * 64,
            bridge_request=bridge_request,
        )


def test_guest_agent_rejects_noncanonical_input_and_never_signs_it() -> None:
    """Ambiguous JSON and raw errors cannot cross the helper trust boundary."""

    signer = _FakeSigner()
    client, agent = socket.socketpair()
    try:
        client.sendall(b'{"operation": "invoke"}\n')
        with pytest.raises(ValueError, match="invalid"):
            runner._read_agent_request(agent)
    finally:
        client.close()
        agent.close()
    assert not signer.payloads


def test_vsock_config_binds_artifact_executable_and_materialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The guest rejects launch metadata that omits Host-fixed artifact state."""

    path = tmp_path / "agent-config.json"
    bindings = _config().binding_digests
    path.write_text(
        json.dumps(
            {
                "version": runner.PACKVM_GUEST_AGENT_VERSION,
                "domain_id": "packvm:domain-1",
                "binding_digests": bindings,
                "private_key_path": "/run/tobkiri-packvm/agent-ed25519.pem",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_assert_root_only_regular_file", lambda *_args: None)

    config = runner._load_vsock_agent_config(path)

    assert config.binding_digests == bindings
    for key in ("artifact", "executable", "materialization"):
        missing = dict(bindings)
        missing.pop(key)
        path.write_text(
            json.dumps(
                {
                    "version": runner.PACKVM_GUEST_AGENT_VERSION,
                    "domain_id": "packvm:domain-1",
                    "binding_digests": missing,
                    "private_key_path": "/run/tobkiri-packvm/agent-ed25519.pem",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="bindings are invalid"):
            runner._load_vsock_agent_config(path)
