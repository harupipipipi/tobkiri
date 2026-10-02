"""Actual guest-envelope/signature conformance, with a fake VM process only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ecosystem.defaultspack.backend.sandbox.isolation.resources import packvm_guest_runner as guest
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.qemu_request_ledger import QemuRequestLedger
from tobkiri_host.qemu_supervisor_transport import PROTOCOL, QemuSupervisorTransport
from tobkiri_protocol.canonical import canonical_digest


class GuestProcess:
    """Run protocol validation/signing only; never execute Pack code on Host."""

    def __init__(self, key):
        self.key = key
        self.running = False
        self.requests = []
        self.ledger = guest._PendingBridgeLedger()
        self.tamper = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def alive(self):
        return self.running

    def wait_for_serial(self, timeout):
        assert timeout > 0

    def wait_for_guest_ready(self, timeout):
        assert timeout > 0

    def exchange(self, request, timeout):
        assert timeout > 0
        self.requests.append(request)
        config = guest._VsockAgentConfig(
            request["domain_id"], request["binding_digests"], Path("/unused-test-key")
        )
        if request["operation"] == "invoke":
            # Validate the real envelope, but explicitly do not run its Pack.
            checked = guest._validate_agent_envelope(request, config, self.ledger)
            expected = {
                "operation",
                "request_id",
                "target_domain",
                "artifact_digest",
                "materialization_digest",
                "guest_artifact_identity",
                "contract_id",
                "contract_version",
                "operation_id",
                "payload",
                "request_digest",
                "deadline_monotonic",
                "cancel_token",
                "budget_seconds",
            }
            assert set(checked["payload"]) == expected
            reply = guest._agent_success(checked, {"state": "complete", "test_only": True})
        else:
            reply = guest._dispatch_agent_request(request, config, self.ledger)
        signed = guest._sign_agent_response(reply, self.key)
        if self.tamper:
            signed["domain_id"] = "wrong-domain"
        return signed


def _make_transport():
    key = Ed25519PrivateKey.generate()
    allocation = SimpleNamespace(
        domain_id="domain.test",
        lease_id="lease.test",
        reservation_id="reservation.test",
        guest_public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        to_dict=lambda: {"test-allocation": "yes"},
    )
    process = GuestProcess(key)
    assets = SimpleNamespace(verify=lambda: None)
    transport = QemuSupervisorTransport(
        process=process, allocation=allocation, assets=assets, channel_key=b"k" * 32
    )
    binding = {
        "domain_id": allocation.domain_id,
        "platform": "linux-amd64",
        "domain_allocation": allocation.to_dict(),
        "binding_digests": {
            name: "sha256:" + "a" * 64
            for name in (
                "domain",
                "lease",
                "reservation",
                "image",
                "agent",
                "config",
                "disk",
                "efi_variable_store",
                "guest_public_key",
                "artifact",
                "executable",
                "materialization",
            )
        },
        "artifact": {
            "artifact_digest": "sha256:" + "a" * 64,
            "materialization_digest": "sha256:" + "b" * 64,
        },
    }
    digest = canonical_digest(binding)
    transport.enroll_launch_secret(
        domain_id=allocation.domain_id,
        host_nonce="1" * 64,
        launch_binding_digest=digest,
        secret=b"k" * 32,
    )
    counter = iter(range(1, 100))

    def envelope(operation, **fields):
        nonce = next(counter)
        return {
            "kind": "tobkiri.macos-vz.supervisor.request.v1",
            "protocol": PROTOCOL,
            "version": 1,
            "operation": operation,
            "host_nonce": "1" * 64 if nonce == 1 else f"{nonce:064x}",
            "domain_id": allocation.domain_id,
            "launch_binding_digest": digest,
            **fields,
        }

    transport.exchange(envelope("launch", launch_binding=binding, guest_challenge="e" * 64))
    return transport, process, envelope


@pytest.fixture
def transport():
    return _make_transport()


def test_real_guest_attest_accepts_correct_request_protocol_and_signature(transport):
    supervisor, process, _envelope = transport
    assert process.requests[0]["protocol"] == guest.PROTOCOL
    assert supervisor.alive()


@pytest.mark.parametrize("monotonic_now", [None, 1000.123456789, 1e12 + 0.125])
def test_real_guest_invoke_envelope_handles_no_host_deadline(transport, monkeypatch, monotonic_now):
    supervisor, process, envelope = transport
    if monotonic_now is not None:
        monkeypatch.setattr("tobkiri_host.qemu_supervisor_transport.time.monotonic", lambda: monotonic_now)
        monkeypatch.setattr(supervisor._requests, "_clock", lambda: monotonic_now)
    reply = supervisor.exchange(
        envelope(
            "invoke",
            guest_challenge="d" * 64,
            request={
                "request_id": "request-1",
                "request_digest": "sha256:" + "f" * 64,
                "contract_id": "test.contract.v1",
                "contract_version": "1.0.0",
                "operation_id": "test",
                "payload": {},
                "deadline_monotonic": None,
            },
        )
    )
    assert reply["payload"]["data"]["test_only"] is True
    assert 0 < float(process.requests[-1]["payload"]["budget_seconds"]) <= 65


def test_modified_signed_guest_response_is_rejected(transport):
    supervisor, process, envelope = transport
    process.tamper = True
    with pytest.raises(BackendUnavailableError, match="binding"):
        supervisor.exchange(
            envelope(
                "cancel",
                guest_challenge="c" * 64,
                request_id="request-1",
                request_digest="sha256:" + "f" * 64,
            )
        )


def test_terminate_requires_owned_lease_and_actual_process_stop(transport):
    supervisor, process, envelope = transport
    with pytest.raises(BackendUnavailableError, match="identity"):
        supervisor.exchange(
            envelope("terminate", lease_id="another", reservation_id="reservation.test")
        )
    reply = supervisor.exchange(
        envelope("terminate", lease_id="lease.test", reservation_id="reservation.test")
    )
    assert not process.alive()
    assert reply["payload"]["cleanup"]["vm"] == "released"


def test_cancel_before_begin_and_late_completion_stay_fenced():
    ledger = QemuRequestLedger(clock=lambda: 10)
    ledger.cancel("first", "digest")
    with pytest.raises(BackendUnavailableError):
        ledger.begin("first", "digest", 30, maximum_bridges=4)
    ticket = ledger.begin("second", "digest", 30, maximum_bridges=4)
    ledger.cancel("second", "digest")
    with pytest.raises(BackendUnavailableError, match="cancelled"):
        ledger.settle(ticket, pending=True)


def test_bridge_exchange_cannot_overlap_and_hops_are_finite():
    ledger = QemuRequestLedger(clock=lambda: 10)
    ticket = ledger.begin("req", "digest", 30, maximum_bridges=1)
    with pytest.raises(BackendUnavailableError):
        ledger.resume("req")
    ledger.settle(ticket, pending=True)
    resumed = ledger.resume("req")
    with pytest.raises(BackendUnavailableError):
        ledger.resume("req")
    with pytest.raises(BackendUnavailableError, match="hop"):
        ledger.settle(resumed, pending=True)


def test_cancel_wrong_request_digest_cannot_retire_operation():
    ledger = QemuRequestLedger(clock=lambda: 10)
    ticket = ledger.begin("req", "digest", 30, maximum_bridges=1)
    with pytest.raises(BackendUnavailableError, match="digest"):
        ledger.cancel("req", "other")
    ledger.settle(ticket, pending=False)


def test_cancel_saturation_refuses_unrecorded_racing_calls():
    ledger = QemuRequestLedger(clock=lambda: 10)
    for index in range(130):
        ledger.cancel(f"req-{index}", "digest")
    with pytest.raises(BackendUnavailableError):
        ledger.begin("req-129", "digest", 30, maximum_bridges=1)


def test_linux_driver_attests_real_guest_signatures_over_fake_vm(tmp_path, monkeypatch):
    from tests.test_macos_vz_supervisor import _artifact, _driver
    from tobkiri_host import linux_qemu_supervisor as linux
    from tobkiri_host.macos_vz_supervisor import MacOSVZHelperIdentity
    from tobkiri_host.platform_backends import IsolationLaunch, IsolationLease, LinuxQemuBackend

    mac_driver, allocator = _driver(tmp_path)
    files = {
        "qemu": SimpleNamespace(
            path=mac_driver._helper_path, digest=mac_driver._helper_identity.expected_code_digest
        ),
        "firmware_code": SimpleNamespace(digest="sha256:" + "c" * 64),
    }
    assets = SimpleNamespace(files=files, manifest_digest="sha256:" + "f" * 64, verify=lambda: None)
    monkeypatch.setattr(linux, "kvm_capability", lambda: (True, None))

    def make_transport(allocation):
        return linux.LinuxQemuSupervisorTransport(
            process=GuestProcess(allocator.private_keys[allocation.domain_id]),
            allocation=allocation,
            assets=assets,
            channel_key=allocator.channel_keys[allocation.domain_id],
        )

    driver = linux.LinuxQemuSupervisorDriver(
        assets=assets,
        transport_factory=make_transport,
        helper_path=mac_driver._helper_path,
        helper_identity=MacOSVZHelperIdentity(
            files["qemu"].digest, "io.tobkiri.packvm.qemu", "", ""
        ),
        launch_assets=mac_driver._launch_assets,
        agent_identity=mac_driver._agent_identity,
        domain_allocator=allocator,
    )
    backend = LinuxQemuBackend(driver)
    assert backend.status.platform == "linux-amd64"
    artifact = _artifact()
    launch = IsolationLaunch(
        "tobkiri.python-pack-v4",
        "linux-amd64",
        artifact.artifact_digest,
        artifact.implementation_digest,
        "packvm.default.v1",
        "domain.linux",
        "reservation.linux",
        IsolationLease("lease.linux", "reservation.linux", 100.0),
        artifact,
    )
    proof = driver.launch(launch)
    assert proof.authenticated_channel and proof.nonce_fresh
    assert proof.platform == "linux-amd64"
    driver.terminate("domain.linux")
    assert len(allocator.released) == 1


def test_launch_uses_one_budget_for_start_ready_and_attestation(monkeypatch):
    now = [1000.0]
    budgets = []
    original_start = GuestProcess.start
    original_exchange = GuestProcess.exchange

    def start(self):
        original_start(self)
        now[0] += 10

    def ready(self, timeout):
        budgets.append(("ready", timeout))
        now[0] += 20

    def exchange(self, request, timeout):
        budgets.append(("attest", timeout))
        return original_exchange(self, request, timeout)

    monkeypatch.setattr("tobkiri_host.qemu_supervisor_transport.time.monotonic", lambda: now[0])
    monkeypatch.setattr(GuestProcess, "start", start)
    monkeypatch.setattr(GuestProcess, "wait_for_guest_ready", ready)
    monkeypatch.setattr(GuestProcess, "exchange", exchange)
    supervisor, process, _ = _make_transport()
    assert supervisor.alive() and process.requests[0]["operation"] == "attest"
    assert budgets == [("ready", 170.0), ("attest", 150.0)]


def test_diagnostic_failure_cannot_mask_startup_error_or_skip_cleanup(monkeypatch):
    stopped = []
    original = RuntimeError("startup failed")

    def ready(self, timeout):
        raise original

    def capture(self, error):
        assert error is original
        raise OSError("diagnostic failed")

    def stop(self):
        self.running = False
        stopped.append(self)

    monkeypatch.setattr(GuestProcess, "wait_for_guest_ready", ready)
    monkeypatch.setattr(GuestProcess, "_capture_failure_diagnostics", capture, raising=False)
    monkeypatch.setattr(GuestProcess, "stop", stop)
    with pytest.raises(RuntimeError, match="startup failed") as result:
        _make_transport()
    assert result.value is original
    assert len(stopped) == 1 and not stopped[0].running
