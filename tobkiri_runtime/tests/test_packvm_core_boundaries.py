"""Focused PackVM guest boundary and authenticated cancellation regressions."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from ecosystem.defaultspack.backend.sandbox.isolation.resources import (
    packvm_guest_runner,
)
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.platform_backends import ManagedLimaPackVMDriver, PlatformAttestation


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def test_guest_sandbox_is_nonprivileged_and_default_deny(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "artifacts" / "pack-a"
    implementation = target / "runtime" / "handler.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(
        packvm_guest_runner.shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )

    argv = packvm_guest_runner._sandbox_argv(target, implementation)

    assert "--unshare-user" in argv
    assert "--unshare-pid" in argv
    assert "--unshare-net" in argv
    assert argv[argv.index("--uid") + 1] == "65534"
    assert argv[argv.index("--gid") + 1] == "65534"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert ("--ro-bind", str(target), "/pack") == tuple(
        argv[argv.index(str(target)) - 1 : argv.index(str(target)) + 2]
    )
    assert str(packvm_guest_runner.ARTIFACT_ROOT) not in argv
    assert "/var/lib/tobkiri-packvm" not in argv
    assert "--tmpfs" in argv
    assert "/tmp" in argv


def test_private_pack_entrypoint_refuses_root_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    implementation = tmp_path / "handler.py"
    implementation.write_text(
        "def tobkiri_packvm_invoke(operation_id, payload): return payload\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)
    assert packvm_guest_runner._execute_staged_module(implementation) == 1


def test_guest_cancel_requires_exact_owned_identity_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "operation": "cancel",
        "request_id": "request-1",
        "target_domain": "packvm:domain-1",
        "guest_artifact_identity": _digest("guest"),
        "cancel_token": "a" * 64,
    }
    record = {
        **request,
        "cancel_token": "b" * 64,
        "process_group": 1234,
    }
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(packvm_guest_runner, "_read_request", lambda _path: record)
    with pytest.raises(ValueError, match="authentication failed"):
        packvm_guest_runner._cancel(request)

    record["cancel_token"] = request["cancel_token"]
    record["request_id"] = "request-other"
    with pytest.raises(ValueError, match="request_id mismatch"):
        packvm_guest_runner._cancel(request)

    record["request_id"] = request["request_id"]
    monkeypatch.setattr(
        packvm_guest_runner,
        "_terminate_process_group",
        lambda process_group: ["TERM"] if process_group == 1234 else [],
    )
    assert packvm_guest_runner._cancel(request) == {
        "ok": True,
        "protocol": packvm_guest_runner.PROTOCOL,
        "operation": "cancel",
        "request_id": "request-1",
        "target_domain": "packvm:domain-1",
        "state": "cancelled",
        "signals": ["TERM"],
    }


def test_existing_challenge_and_authenticated_cancel_share_guest_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge = "c" * 64
    challenge_request = {
        "operation": "invoke",
        "contract_id": "io.tobkiri.packvm.attestation.v1",
        "operation_id": "challenge",
        "payload": {"challenge": challenge},
    }

    def run_main(request: dict[str, object]) -> dict[str, object]:
        stdout = io.StringIO()
        monkeypatch.setattr(
            packvm_guest_runner.sys,
            "stdin",
            SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())),
        )
        monkeypatch.setattr(packvm_guest_runner.sys, "stdout", stdout)
        monkeypatch.setattr(packvm_guest_runner.sys, "argv", ["packvm_guest_runner.py"])
        assert packvm_guest_runner.main() == 0
        response = json.loads(stdout.getvalue())
        assert isinstance(response, dict)
        return response

    assert run_main(challenge_request) == {
        "ok": True,
        "protocol": packvm_guest_runner.PROTOCOL,
        "payload": {"challenge_digest": _digest(challenge)},
    }

    cancel_request = {
        "operation": "cancel",
        "request_id": "request-1",
        "target_domain": "packvm:domain-1",
        "guest_artifact_identity": _digest("guest"),
        "cancel_token": "a" * 64,
    }
    cancel_response = {
        "ok": True,
        "protocol": packvm_guest_runner.PROTOCOL,
        "operation": "cancel",
        "request_id": "request-1",
        "target_domain": "packvm:domain-1",
        "state": "cancelled",
        "signals": ["TERM"],
    }
    monkeypatch.setattr(
        packvm_guest_runner,
        "_cancel",
        lambda request: cancel_response if request == cancel_request else {},
    )
    assert run_main(cancel_request) == cancel_response


class _BlockingProvisioner:
    def __init__(self, cancel_mode: str = "ok") -> None:
        self.cancel_mode = cancel_mode
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_request: dict[str, object] | None = None

    def doctor(self) -> SimpleNamespace:
        return SimpleNamespace(
            ready=True,
            reason=None,
            platform="macos-arm64",
            attestation_digest=_digest("backend"),
        )

    def invoke_guest(self, request: dict[str, object]) -> dict[str, object]:
        if request["operation"] == "invoke":
            self.started.set()
            self.release.wait(timeout=2)
            return {
                "ok": True,
                "protocol": packvm_guest_runner.PROTOCOL,
                "payload": {"done": True},
            }
        self.cancel_request = request
        if self.cancel_mode == "transport":
            raise OSError("transport unavailable")
        response = {
            "ok": True,
            "protocol": packvm_guest_runner.PROTOCOL,
            "operation": "cancel",
            "request_id": request["request_id"],
            "target_domain": request["target_domain"],
            "state": "cancelled",
            "signals": ["TERM", "KILL"],
        }
        if self.cancel_mode == "mismatch":
            response["request_id"] = "attacker-request"
        return response


def _driver_with_domain(provisioner: _BlockingProvisioner) -> ManagedLimaPackVMDriver:
    driver = ManagedLimaPackVMDriver(provisioner)
    domain = "packvm:domain-1"
    driver._domains[domain] = PlatformAttestation(
        domain_id=domain,
        backend_id=driver.backend_id,
        backend_digest=driver.backend_digest,
        platform=driver.platform,
        executable_digest=_digest("executable"),
        artifact_digest=_digest("artifact"),
        materialization_digest=_digest("materialization"),
        guest_artifact_identity=_digest("guest"),
        isolation_profile="packvm.default.v1",
        attestation_digest=_digest("attestation"),
        attestation_nonce="lima-nonce-1",
        lease_id="lease-1",
        reservation_id="reservation-1",
        authenticated_channel=True,
        nonce_fresh=True,
    )
    return driver


@pytest.mark.parametrize("mode", ("ok", "mismatch", "transport"))
def test_driver_confirms_exact_cancel_ack(mode: str) -> None:
    provisioner = _BlockingProvisioner(mode)
    driver = _driver_with_domain(provisioner)
    request = SimpleNamespace(
        target_domain=SimpleNamespace(value="packvm:domain-1"),
        context=SimpleNamespace(request_id="request-1"),
        contract_id="sample.v1",
        contract_version="1.0.0",
        operation_id="run",
        payload={},
        request_digest=_digest("request"),
        deadline_monotonic=10.0,
    )
    thread = threading.Thread(target=driver.invoke, args=(request,))
    thread.start()
    assert provisioner.started.wait(timeout=1)
    try:
        if mode == "ok":
            driver.cancel("request-1")
            assert provisioner.cancel_request is not None
            assert provisioner.cancel_request["cancel_token"]
        else:
            with pytest.raises(BackendUnavailableError, match="ACK mismatch|transport failed"):
                driver.cancel("request-1")
    finally:
        provisioner.release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
