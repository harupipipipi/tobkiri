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


def _saved_host_result(pending: dict, outcome: dict) -> dict:
    wrapper = dict(pending["host_bridge_request"])
    wrapper["kind"] = "tobkiri.packvm.bridge.host-result.v2"
    wrapper.pop("deadline_monotonic")
    frame = wrapper.pop("bridge_request")
    wrapper["bridge_result"] = {
        "kind": "tobkiri.packvm.continuation.result.v2", "version": 2,
        "request_digest": _digest(frame), "outcome": outcome,
    }
    return wrapper


def test_saved_agent_four_signed_exchanges_use_real_owner_and_one_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Socket/signing/ledger/owner are real; sandbox and AI are explicit adapters."""
    from ecosystem.defaultspack.runtime import saved_conversation as saved
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
    from tobkiri_host.saved_guest_dispatch import TARGETS

    assert TARGETS == saved.TARGETS
    config = _config()
    ledger = runner._PendingBridgeLedger()
    signer = _FakeSigner()
    owner = ConversationStore("defaults", user_data_root=tmp_path)
    owner.create({"id": "conversation", "model_reference": "model"}, expected_revision=0)
    request = _invoke_payload("saved", config)
    request.update(contract_id="conversation.saved-turn.v1", operation_id="saved_complete",
                   payload={"request": {"turn_id": "turn", "conversation_id": "conversation",
                                        "conversation_revision": 1, "content": "Hello"}})
    steps = []

    def execute(captured: dict, arguments: dict, deadline: float, **kwargs: Any) -> dict:
        kwargs["execution_guard"]()
        steps.append((arguments, deadline))
        value = saved.tobkiri_packvm_invoke("saved_complete", arguments)
        if value.get("kind") != "tobkiri.packvm.continuation.intent.v2":
            value = runner._host_invoke_result(value)
        return {"ok": True, "protocol": runner.PROTOCOL,
                "guest_artifact_identity": _guest_artifact_identity(config), "payload": value}

    def initial(captured: dict, *, guest_deadline: float, **kwargs: Any) -> dict:
        return execute(captured, captured["payload"], guest_deadline, **kwargs)

    monkeypatch.setattr(runner, "_invoke", initial)
    monkeypatch.setattr(runner, "_execute_invocation_step", execute)
    response = _roundtrip(_envelope(config, "invoke", "saved", "0" * 64, payload=request),
                          config, ledger, signer)
    for hop in range(4):
        assert response["success"] is True
        frame = response["data"]["host_bridge_request"]["bridge_request"]
        assert frame["hop"] == hop
        assert tuple(frame["target"].values()) == TARGETS[hop]
        payload = frame["payload"]
        if hop == 0:
            value = {"conversation": owner.get("conversation")}
        elif hop == 2:
            value = {"status": "ok", "output": "Hi"}
        else:
            value = owner.append_message("conversation", payload["message"],
                                         expected_conversation_revision=payload["expected_conversation_revision"])
        result = _saved_host_result(response["data"], {"status": "ok", "value": value})
        response = _roundtrip(_envelope(config, "bridge_result", "saved", str(hop + 1) * 64,
                                        host_bridge_result=result), config, ledger, signer)
    assert response["success"] is True
    assert response["data"]["kind"] == runner.PACKVM_INVOKE_RESULT_KIND
    assert len(steps) == 5
    assert len({deadline for _, deadline in steps}) == 1
    assert all(set(arguments) == {"state", "outcome"} for arguments, _ in steps[1:])
    assert len(signer.payloads) == 5
    replay = _roundtrip(_envelope(config, "bridge_result", "saved", "f" * 64,
                                  host_bridge_result=result), config, ledger, signer)
    assert replay["success"] is False
    assert len(steps) == 5


@pytest.mark.parametrize("payload", [
    {"state": {}, "outcome": {}},
    {"request": {"turn_id": "turn", "conversation_id": "conversation",
                 "conversation_revision": 1, "content": "hello", "target": "other"}},
])
def test_saved_initial_invoke_rejects_resume_or_routing_before_artifact_access(
    monkeypatch: pytest.MonkeyPatch, payload: dict,
) -> None:
    def forbidden(*args: object) -> None:
        pytest.fail("invalid initial input must not inspect or execute an artifact")

    monkeypatch.setattr(runner, "_verify_invocation_artifact", forbidden)
    request = {
        "operation": "invoke", "request_id": "request", "target_domain": "domain",
        "artifact_digest": "sha256:" + "a" * 64,
        "materialization_digest": "sha256:" + "a" * 64,
        "guest_artifact_identity": "sha256:" + "a" * 64,
        "contract_id": "conversation.saved-turn.v1", "contract_version": "1.0.0",
        "operation_id": "saved_complete", "payload": payload,
        "request_digest": "sha256:" + "b" * 64,
        "deadline_monotonic": "60", "cancel_token": "c" * 64,
    }
    with pytest.raises(ValueError, match="fields are invalid"):
        runner._invoke(request)


def test_cancellation_capacity_never_evicts_a_live_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overflow stays bounded without allowing an old or new cancel to revive."""
    now = [100.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runner, "MAX_PENDING_BRIDGES", 2)
    ledger = runner._PendingBridgeLedger()
    for request_id in ("first", "second"):
        ledger.cancel(domain_id="domain", request_id=request_id)
    now[0] = 110.0
    ledger.cancel(domain_id="domain", request_id="overflow")
    assert len(ledger._cancelled) == 2
    assert ("domain", "first") in ledger._cancelled
    for request_id in ("first", "second", "overflow", "new"):
        with pytest.raises(ValueError, match="saturated"):
            ledger.add(domain_id="domain", request={"request_id": request_id}, guest_artifact_identity="artifact", bridge_request=_bridge_request())
    # The first two tombstones expire earlier than the overflow cancellation.
    now[0] = 100.0 + runner.PENDING_BRIDGE_TTL_SECONDS
    with pytest.raises(ValueError, match="saturated"):
        ledger.add(domain_id="domain", request={"request_id": "overflow"}, guest_artifact_identity="artifact", bridge_request=_bridge_request())
    now[0] = 110.0 + runner.PENDING_BRIDGE_TTL_SECONDS
    ledger.add(domain_id="domain", request={"request_id": "fresh"}, guest_artifact_identity="artifact", bridge_request=_bridge_request())
    assert ledger.consume(domain_id="domain", request_id="fresh").request["request_id"] == "fresh"


def test_overflow_cancellation_removes_pending_and_extends_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling pending work still works while the tombstone ledger is full."""
    now = [100.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runner, "MAX_PENDING_BRIDGES", 1)
    ledger = runner._PendingBridgeLedger()
    ledger.add(domain_id="domain", request={"request_id": "active"}, guest_artifact_identity="artifact", bridge_request=_bridge_request())
    ledger.cancel(domain_id="domain", request_id="first")
    assert ledger.cancel(domain_id="domain", request_id="active") is True
    with pytest.raises(ValueError, match="unavailable"):
        ledger.consume(domain_id="domain", request_id="active")
    now[0] = 120.0
    ledger.cancel(domain_id="domain", request_id="later")
    now[0] = 100.0 + runner.PENDING_BRIDGE_TTL_SECONDS
    with pytest.raises(ValueError, match="saturated"):
        ledger.add(domain_id="domain", request={"request_id": "later"}, guest_artifact_identity="artifact", bridge_request=_bridge_request())
    assert len(ledger._cancelled) <= 1


def test_real_conversation_model_survives_guest_and_host_validation() -> None:
    """Use actual producer bytes through both independent boundary validators."""
    from ecosystem.defaultspack.runtime import conversation
    from tobkiri_host.macos_vz_supervisor import _validate_bridge_request

    produced = conversation.tobkiri_packvm_invoke("complete", {
        "messages": [{"role": "user", "content": "hello"}],
        "model": "local/selected", "profile_id": "not-forwarded",
    })
    checked = runner._validate_bridge_request(produced)
    assert checked == produced
    assert checked["request"]["model_reference"] == "local/selected"
    assert _validate_bridge_request(checked) == produced["continuation"]


@pytest.mark.parametrize("model", [None, True, 1, "", " x", "x\n", "x\x00y", "x" * 257])
def test_guest_boundary_rejects_malformed_model_reference(model: object) -> None:
    """Even a correctly digested frame cannot smuggle malformed references."""
    produced = _bridge_request()
    produced["request"]["model_reference"] = model
    produced["request_digest"] = _digest(produced["request"])
    produced["continuation"]["request_digest"] = produced["request_digest"]
    with pytest.raises(ValueError, match="model reference"):
        runner._validate_bridge_request(produced)


def test_guest_boundary_rejects_model_tampering_and_authority_fields() -> None:
    """Model binding is digest-covered and does not broaden payload authority."""
    produced = _bridge_request()
    produced["request"]["model_reference"] = "local/changed"
    with pytest.raises(ValueError, match="digest"):
        runner._validate_bridge_request(produced)
    produced = _bridge_request()
    produced["request"]["profile_id"] = "other"
    with pytest.raises(ValueError):
        runner._validate_bridge_request(produced)


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
        "contract_id": "conversation.turn.v1",
        "contract_version": "1.0.0",
        "operation_id": "complete",
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
        lambda _payload, **_kwargs: {
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
        *, guest_deadline: float,
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


@pytest.mark.parametrize("finish_late", [False, True])
def test_guest_dispatch_retains_initial_local_deadline_across_host_wait(
    monkeypatch: pytest.MonkeyPatch, finish_late: bool,
) -> None:
    config = _config()
    ledger = runner._PendingBridgeLedger()
    bridge = _bridge_request()
    now = [1000.0]
    deadlines = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])

    def initial(payload: dict, *, guest_deadline: float) -> dict:
        deadlines.append(guest_deadline)
        now[0] = 1020.0
        return {"ok": True, "protocol": runner.PROTOCOL,
                "guest_artifact_identity": _guest_artifact_identity(config), "payload": bridge}

    def resume(*args: object, guest_deadline: float) -> dict:
        deadlines.append(guest_deadline)
        now[0] = 1061.0 if finish_late else 1055.0
        return {"ok": True, "protocol": runner.PROTOCOL,
                "guest_artifact_identity": _guest_artifact_identity(config), "payload": {"output": "done"}}

    monkeypatch.setattr(runner, "_invoke", initial)
    monkeypatch.setattr(runner, "_resume_bridge_invocation", resume)
    response = runner._dispatch_agent_request(
        _envelope(config, "invoke", "local-budget", "a" * 64,
                  payload=_invoke_payload("local-budget")), config, ledger,
    )
    # Host 123.5 and guest 1000.0 are intentionally unrelated clock origins.
    assert response["data"]["host_bridge_request"]["deadline_monotonic"] == "123.5"
    assert ledger._pending[(config.domain_id, "local-budget")].expires_at == 1060.0
    now[0] = 1050.0
    request = _envelope(config, "bridge_result", "local-budget", "b" * 64,
                        host_bridge_result=_host_result(config, "local-budget", bridge))
    if finish_late:
        with pytest.raises(TimeoutError, match="deadline expired"):
            runner._dispatch_agent_request(request, config, ledger)
    else:
        assert runner._dispatch_agent_request(request, config, ledger)["data"] == {"output": "done"}
    assert deadlines == [1060.0, 1060.0]
    with pytest.raises(ValueError, match="unavailable"):
        ledger.consume(domain_id=config.domain_id, request_id="local-budget")


def test_expired_initial_child_cannot_register_a_fresh_pending_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    config = _config()
    ledger = runner._PendingBridgeLedger()
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])

    def late(*args: object, **kwargs: object) -> dict:
        now[0] = 1060.0
        return {"payload": _bridge_request()}

    monkeypatch.setattr(runner, "_invoke", late)
    with pytest.raises(TimeoutError, match="deadline expired"):
        runner._dispatch_agent_request(
            _envelope(config, "invoke", "expired", "a" * 64,
                      payload=_invoke_payload("expired")), config, ledger,
        )
    assert ledger._pending == {}


def test_original_pending_deadline_expires_without_renewal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    ledger = runner._PendingBridgeLedger()
    ledger.add(domain_id="domain", request={"request_id": "request"},
               guest_artifact_identity="artifact", bridge_request=_bridge_request(),
               guest_deadline=1010.0)
    now[0] = 1010.0
    with pytest.raises(ValueError, match="unavailable"):
        ledger.consume(domain_id="domain", request_id="request")


def test_expired_resume_cannot_verify_artifact_or_spawn_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.time, "monotonic", lambda: 1000.0)

    def forbidden(*args: object) -> None:
        pytest.fail("expired resume entered artifact execution")

    monkeypatch.setattr(runner, "_verify_invocation_artifact", forbidden)
    with pytest.raises(TimeoutError, match="deadline expired"):
        runner._resume_bridge_invocation({}, {}, {}, guest_deadline=1000.0)


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
        lambda _payload, **_kwargs: {
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
