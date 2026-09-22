"""Adversarial contract tests for the direct macOS VZ Host supervisor."""

from __future__ import annotations

import base64
import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tobkiri_protocol.canonical import canonical_digest, canonical_json

from tobkiri_host.artifact_materialization import (
    MaterializedArtifactFile,
    MaterializedPackArtifact,
    _materialization_digest,
)
from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.macos_vz_supervisor import (
    MacOSVZAgentIdentity,
    MacOSVZDomainAllocation,
    MacOSVZHelperIdentity,
    MacOSVZLaunchAssets,
    MacOSVZRuntime,
    MacOSVZSupervisorDriver,
)
from tobkiri_host.platform_backends import IsolationLaunch, IsolationLease


def _digest(value: str | bytes) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


class _Verifier:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready

    def verify(
        self, path: Path, expected: MacOSVZHelperIdentity
    ) -> tuple[bool, str | None]:
        del expected
        return (True, None) if self.ready and path.is_file() else (
            False,
            "test helper identity mismatch",
        )


class _Allocator:
    """Fake provisioner that makes actual measured dynamic assets per domain."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.private_keys: dict[str, Ed25519PrivateKey] = {}
        self.transports: dict[str, _Transport] = {}
        self.channel_keys: dict[str, bytes] = {}
        self.released: list[MacOSVZDomainAllocation] = []

    def allocate(
        self,
        *,
        domain_id: str,
        reservation_id: str,
        lease_id: str,
        artifact_digest: str,
        executable_digest: str,
        materialization_digest: str,
        channel_key: bytes,
    ) -> MacOSVZDomainAllocation:
        assert all(
            value.startswith("sha256:")
            for value in (artifact_digest, executable_digest, materialization_digest)
        )
        root = self.root / hashlib.sha256(domain_id.encode()).hexdigest()
        root.mkdir(parents=True, mode=0o700)
        files = {
            "cow": root / "boot-cow.raw",
            "efi": root / "efi-variable-store.bin",
            "agent": root / "agent-seed.iso",
            "config": root / "config-seed.iso",
        }
        for name, path in files.items():
            path.write_bytes(f"{name}:{domain_id}".encode())
            path.chmod(0o600)
        private_key = Ed25519PrivateKey.generate()
        self.private_keys[domain_id] = private_key
        self.channel_keys[domain_id] = channel_key
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        allocation = MacOSVZDomainAllocation(
            domain_id=domain_id,
            reservation_id=reservation_id,
            lease_id=lease_id,
            run_root=str(root),
            cow_disk_path=str(files["cow"]),
            cow_disk_digest=_digest(files["cow"].read_bytes()),
            efi_store_path=str(files["efi"]),
            efi_variable_store_digest=_digest(files["efi"].read_bytes()),
            agent_seed_path=str(files["agent"]),
            agent_seed_digest=_digest(files["agent"].read_bytes()),
            config_seed_path=str(files["config"]),
            config_seed_digest=_digest(files["config"].read_bytes()),
            guest_public_key=public_key,
        )
        self.transports[domain_id] = _Transport(self, allocation)
        return allocation

    def transport_for(self, allocation: MacOSVZDomainAllocation) -> "_Transport":
        return self.transports[allocation.domain_id]

    def release(self, allocation: MacOSVZDomainAllocation) -> None:
        transport = self.transports.get(allocation.domain_id)
        if transport is not None:
            transport.close()
        self.released.append(allocation)


class _Transport:
    """One helper process which HMACs outer and forwards guest signatures."""

    def __init__(self, allocator: _Allocator, allocation: MacOSVZDomainAllocation) -> None:
        self.allocator = allocator
        self.allocation = allocation
        self.requests: list[dict[str, Any]] = []
        self.binding_digests: Mapping[str, str] | None = None
        self.tamper_mac = False
        self.tamper_guest_signature = False
        self.tamper_guest_binding = False
        self.guest_rejection = False
        self.pending_bridge = False
        self.replay_host_nonce: str | None = None
        self.closed = False
        self.enrollment: tuple[str, str, str] | None = None

    def enroll_launch_secret(
        self,
        *,
        domain_id: str,
        host_nonce: str,
        launch_binding_digest: str,
        secret: bytes,
    ) -> None:
        assert domain_id == self.allocation.domain_id
        assert secret == self.allocator.channel_keys[domain_id]
        assert len(host_nonce) == 64
        self.enrollment = (domain_id, host_nonce, launch_binding_digest)

    def close(self) -> None:
        self.closed = True

    def _guest(
        self,
        request: Mapping[str, Any],
        *,
        operation: str,
        request_id: str,
        data: Mapping[str, Any] | None = None,
        error: Mapping[str, str] | None = None,
        attest: bool = False,
    ) -> Mapping[str, Any]:
        assert self.binding_digests is not None
        core: dict[str, Any] = {
            "kind": "tobkiri.packvm.guest.response.v1",
            "protocol": "io.tobkiri.macos-vz-supervisor.v1",
            "version": 1,
            "operation": operation,
            "request_id": request_id,
            "domain_id": self.allocation.domain_id,
            "binding_digests": dict(self.binding_digests),
            "guest_challenge": request["guest_challenge"],
            "success": error is None,
        }
        if error is None:
            core["data"] = dict(data or {})
        else:
            core["error"] = dict(error)
        if attest:
            core["attestation_nonce"] = request["host_nonce"]
        if self.tamper_guest_binding:
            self.tamper_guest_binding = False
            core["binding_digests"] = {"domain": _digest("wrong")}
        signature = self.allocator.private_keys[self.allocation.domain_id].sign(
            canonical_json(core)
        )
        if self.tamper_guest_signature:
            self.tamper_guest_signature = False
            signature = b"not-an-ed25519-signature"
        return {**core, "agent_signature": _b64(signature)}

    def exchange(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        request = dict(envelope)
        assert self.enrollment == (
            request["domain_id"],
            request["host_nonce"] if request["operation"] == "launch" else self.enrollment[1],
            request["launch_binding_digest"],
        )
        self.requests.append(request)
        operation = request["operation"]
        if operation == "launch":
            self.binding_digests = request["launch_binding"]["binding_digests"]
            payload = self._guest(
                request,
                operation="attest",
                request_id=f"attest-{self.allocation.domain_id}",
                data={"guest_artifact_identity": canonical_digest(self.binding_digests)},
                attest=True,
            )
        elif operation == "invoke":
            request_id = request["request"]["request_id"]
            if self.guest_rejection:
                payload = self._guest(
                    request,
                    operation="invoke",
                    request_id=request_id,
                    error={"code": "GUEST_DENIED", "message": "request rejected"},
                )
            elif self.pending_bridge:
                bridge_payload = {
                    "messages": [{"role": "user", "content": "hello"}],
                    "requirements": {"request_surface": "defaultspack.conversation"},
                }
                target = {
                    "contract_id": "tobkiri.service.ai.generate.v1",
                    "operation_id": "rumi_ai_gateway_pack.ai-gateway.generate",
                }
                bridge_request = {
                    "kind": "tobkiri.packvm.bridge.request.v1",
                    "protocol": "io.tobkiri.packvm.bridge.v1",
                    "version": 1,
                    "target": target,
                    "request": bridge_payload,
                    "request_digest": canonical_digest(bridge_payload),
                    "continuation": {
                        "kind": "tobkiri.packvm.continuation.v1",
                        "protocol": "io.tobkiri.packvm.bridge.v1",
                        "version": 1,
                        "operation_id": "complete",
                        "nonce": "b" * 48,
                        "target": target,
                        "request_digest": canonical_digest(bridge_payload),
                    },
                }
                host_bridge_request = {
                    "kind": "tobkiri.packvm.bridge.host-request.v1",
                    "protocol": "io.tobkiri.packvm.bridge.v1",
                    "version": 1,
                    "request_id": request_id,
                    "target_domain": self.allocation.domain_id,
                    "guest_artifact_identity": canonical_digest(self.binding_digests),
                    "request_digest": request["request"]["request_digest"],
                    "bridge_request_digest": canonical_digest(bridge_request),
                    "bridge_request": bridge_request,
                    "deadline_monotonic": request["request"]["deadline_monotonic"],
                }
                payload = self._guest(
                    request,
                    operation="invoke",
                    request_id=request_id,
                    data={
                        "state": "pending",
                        "host_bridge_request": host_bridge_request,
                    },
                )
            else:
                payload = self._guest(
                    request,
                    operation="invoke",
                    request_id=request_id,
                    data={"kind": "tobkiri.packvm.invoke.result.v1", "outcome": {"text": request_id}},
                )
        elif operation == "bridge_result":
            request_id = request["host_bridge_result"]["request_id"]
            payload = self._guest(
                request,
                operation="bridge_result",
                request_id=request_id,
                data={"kind": "tobkiri.packvm.invoke.result.v1", "outcome": {"text": "bridged"}},
            )
        elif operation == "cancel":
            payload = self._guest(
                request,
                operation="cancel",
                request_id=request["request_id"],
                data={"state": "cancelled", "request_id": request["request_id"], "signals": ["TERM"]},
            )
        elif operation == "terminate":
            payload = {
                "state": "terminated",
                "domain_id": self.allocation.domain_id,
                "lease_id": request["lease_id"],
                "reservation_id": request["reservation_id"],
                "cleanup": {"vm": "released", "cow_disk": "detached", "efi_store": "detached"},
            }
        else:
            raise AssertionError(operation)
        core = {
            "kind": "tobkiri.macos-vz.supervisor.response.v1",
            "protocol": "io.tobkiri.macos-vz-supervisor.v1",
            "version": 1,
            "operation": operation,
            "host_nonce": self.replay_host_nonce or request["host_nonce"],
            "domain_id": self.allocation.domain_id,
            "launch_binding_digest": request["launch_binding_digest"],
            "payload": payload,
        }
        mac = hmac.new(
            self.allocator.channel_keys[self.allocation.domain_id],
            canonical_json(core),
            hashlib.sha256,
        ).hexdigest()
        if self.tamper_mac:
            self.tamper_mac = False
            mac = "0" * 64
        return {**core, "agent_mac": mac}


def _artifact() -> MaterializedPackArtifact:
    content = b"print('guest')\n"
    artifact_file = MaterializedArtifactFile(
        path="runtime/handler.py", digest=_digest(content), executable=False, content=content
    )
    files = (artifact_file,)
    return MaterializedPackArtifact(
        pack_id="defaultspack.conversation",
        artifact_digest=_digest("artifact"),
        function_id="conversation.complete",
        implementation_digest=artifact_file.digest,
        implementation_path=artifact_file.path,
        materialization_digest=_materialization_digest(
            "defaultspack.conversation", _digest("artifact"), "conversation.complete",
            artifact_file.digest, artifact_file.path, files,
        ),
        root_device=1,
        root_inode=2,
        files=files,
    )


def _driver(
    tmp_path: Path, *, verifier: _Verifier | None = None
) -> tuple[MacOSVZSupervisorDriver, _Allocator]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    helper = tmp_path / "TobkiriVZSupervisor"
    helper.write_bytes(b"signed helper")
    base_image = tmp_path / "verified-base.raw"
    base_image.write_bytes(b"immutable verified base")
    base_image.chmod(0o400)
    allocator = _Allocator(tmp_path / "domains")
    agent_template = _digest("agent-template")
    driver = MacOSVZSupervisorDriver(
        transport_factory=allocator.transport_for,
        helper_path=helper,
        helper_identity=MacOSVZHelperIdentity(
            binary_digest=_digest(b"signed helper"), bundle_id="io.tobkiri.vz-supervisor",
            team_id="TEAMID1234", signing_identity="Developer ID Application: Tobkiri",
        ),
        launch_assets=MacOSVZLaunchAssets(
            base_image_digest=_digest(base_image.read_bytes()), base_image_path=str(base_image),
            agent_template_digest=agent_template, config_template_digest=_digest("config-template"),
            base_image_read_only=True,
        ),
        agent_identity=MacOSVZAgentIdentity(agent_digest=agent_template),
        domain_allocator=allocator,
        runtime=MacOSVZRuntime(cpu_count=1, memory_bytes=512 * 1024 * 1024),
        identity_verifier=verifier or _Verifier(),
    )
    return driver, allocator


def _launch(driver: MacOSVZSupervisorDriver, domain_id: str = "domain.provider.conversation") -> None:
    artifact = _artifact()
    attestation = driver.launch(
        IsolationLaunch(
            backend_id="tobkiri.python-pack-v4", platform="macos-arm64",
            artifact_digest=artifact.artifact_digest, executable_digest=artifact.implementation_digest,
            isolation_profile="packvm.defaultspack.conversation.v1", target_domain_id=domain_id,
            reservation_id=f"reservation-{domain_id}",
            lease=IsolationLease(f"lease-{domain_id}", f"reservation-{domain_id}", 100.0),
            artifact=artifact,
        )
    )
    assert attestation.authenticated_channel and attestation.nonce_fresh


def _request(domain_id: str, request_id: str = "request-1") -> SimpleNamespace:
    return SimpleNamespace(
        target_domain=SimpleNamespace(value=domain_id),
        context=SimpleNamespace(request_id=request_id), request_digest=_digest(request_id),
        contract_id="conversation.turn.v1", contract_version="1.0.0", operation_id="complete",
        payload={"messages": [{"role": "user", "content": "hello"}]}, deadline_monotonic=50.0,
    )


def test_direct_driver_uses_dynamic_assets_and_per_domain_helper(tmp_path: Path) -> None:
    driver, allocator = _driver(tmp_path)
    _launch(driver)
    transport = allocator.transports["domain.provider.conversation"]
    binding = transport.requests[0]["launch_binding"]
    assert binding["runtime"] == {
        "cpu_count": 1, "memory_bytes": 512 * 1024 * 1024, "guest_vsock_port": 19001,
    }
    assert binding["domain_allocation"]["agent_seed_digest"].startswith("sha256:")
    assert binding["domain_allocation"]["config_seed_digest"].startswith("sha256:")
    assert binding["lease"]["expires_monotonic_ns"] == 100 * 1_000_000_000
    assert binding["binding_digests"]["config"] == binding["launch_assets"]["config_template_digest"]
    assert "channel_key" not in transport.requests[0]
    assert driver.invoke(_request("domain.provider.conversation")).payload == {"text": "request-1"}
    driver.terminate("domain.provider.conversation")
    assert transport.closed and allocator.released == [transport.allocation]


def test_hmac_and_nested_guest_signature_tamper_fail_closed(tmp_path: Path) -> None:
    driver, allocator = _driver(tmp_path)
    # The allocation's helper is available only after launch; corrupt it on first response.
    original = allocator.transport_for
    def mac_factory(allocation: MacOSVZDomainAllocation) -> _Transport:
        transport = original(allocation)
        transport.tamper_mac = True
        return transport
    driver._transport_factory = mac_factory  # type: ignore[attr-defined]
    with pytest.raises(BackendUnavailableError, match="helper MAC"):
        _launch(driver)
    assert driver.capability()[0] is False

    signature_driver, signature_allocator = _driver(tmp_path / "signature")
    original = signature_allocator.transport_for
    def signature_factory(allocation: MacOSVZDomainAllocation) -> _Transport:
        transport = original(allocation)
        transport.tamper_guest_signature = True
        return transport
    signature_driver._transport_factory = signature_factory  # type: ignore[attr-defined]
    with pytest.raises(BackendUnavailableError, match="guest response signature"):
        _launch(signature_driver)
    assert signature_driver.capability()[0] is False


def test_guest_binding_tamper_fails_but_signed_guest_rejection_does_not_compromise(
    tmp_path: Path,
) -> None:
    driver, allocator = _driver(tmp_path)
    original = allocator.transport_for
    def tampered_factory(allocation: MacOSVZDomainAllocation) -> _Transport:
        transport = original(allocation)
        transport.tamper_guest_binding = True
        return transport
    driver._transport_factory = tampered_factory  # type: ignore[attr-defined]
    with pytest.raises(BackendUnavailableError, match="binding mismatch"):
        _launch(driver)
    assert driver.capability()[0] is False

    rejected_driver, rejected_allocator = _driver(tmp_path / "rejected")
    _launch(rejected_driver)
    rejected_allocator.transports["domain.provider.conversation"].guest_rejection = True
    with pytest.raises(BackendUnavailableError, match="guest rejected"):
        rejected_driver.invoke(_request("domain.provider.conversation"))
    assert rejected_driver.capability() == (True, None)


def test_replayed_helper_nonce_and_identity_failure_are_unavailable(tmp_path: Path) -> None:
    driver, allocator = _driver(tmp_path)
    _launch(driver)
    transport = allocator.transports["domain.provider.conversation"]
    transport.replay_host_nonce = transport.requests[0]["host_nonce"]
    with pytest.raises(BackendUnavailableError, match="binding mismatch"):
        driver.invoke(_request("domain.provider.conversation"))
    assert driver.capability()[0] is False

    unavailable, _allocator = _driver(tmp_path / "missing", verifier=_Verifier(False))
    assert unavailable.capability() == (False, "test helper identity mismatch")


def test_concurrent_domains_have_distinct_helpers_and_close_after_cleanup(tmp_path: Path) -> None:
    driver, allocator = _driver(tmp_path)
    _launch(driver, "domain.conversation")
    _launch(driver, "domain.model-catalog")
    conversation = allocator.transports["domain.conversation"]
    catalog = allocator.transports["domain.model-catalog"]
    assert conversation is not catalog
    assert driver.invoke(_request("domain.conversation", "request-a")).payload == {"text": "request-a"}
    assert driver.invoke(_request("domain.model-catalog", "request-b")).payload == {"text": "request-b"}
    driver.terminate("domain.conversation")
    assert conversation.closed and not catalog.closed
    driver.terminate("domain.model-catalog")
    assert catalog.closed


def test_signed_pending_bridge_uses_host_callback_and_resumes_once(tmp_path: Path) -> None:
    """A strict signed bridge resumes once and its continuation cannot replay."""

    driver, allocator = _driver(tmp_path)
    observed: list[tuple[object, Mapping[str, Any]]] = []

    def capability_bridge(
        outer_request: object,
        bridge_request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        observed.append((outer_request, bridge_request))
        continuation = bridge_request["continuation"]
        result = {"status": "ok", "value": {"model": "host-bound"}}
        return {
            "kind": "tobkiri.packvm.bridge.result.v1",
            "protocol": "io.tobkiri.packvm.bridge.v1",
            "version": 1,
            "operation_id": "complete",
            "nonce": continuation["nonce"],
            "target": continuation["target"],
            "request_digest": continuation["request_digest"],
            "result": result,
            "result_digest": canonical_digest(result),
        }

    driver.bind_capability_bridge(capability_bridge)
    _launch(driver)
    transport = allocator.transports["domain.provider.conversation"]
    transport.pending_bridge = True

    request = _request("domain.provider.conversation")
    assert driver.invoke(request).payload == {"text": "bridged"}
    assert len(observed) == 1
    assert observed[0][0] is request
    assert observed[0][1]["target"] == {
        "contract_id": "tobkiri.service.ai.generate.v1",
        "operation_id": "rumi_ai_gateway_pack.ai-gateway.generate",
    }

    bridge_resume = transport.requests[-1]
    assert bridge_resume["operation"] == "bridge_result"
    assert bridge_resume["host_bridge_result"]["request_id"] == "request-1"
    assert bridge_resume["host_bridge_result"]["bridge_result"]["result"] == {
        "status": "ok",
        "value": {"model": "host-bound"},
    }
    assert "channel_key" not in bridge_resume

    with pytest.raises(BackendUnavailableError, match="bridge nonce replay"):
        driver.invoke(_request("domain.provider.conversation", "request-2"))
    assert driver.capability()[0] is False


def test_runtime_bounds_are_fail_closed() -> None:
    with pytest.raises(BackendUnavailableError, match="vsock port"):
        MacOSVZRuntime(guest_vsock_port=8765)
    with pytest.raises(BackendUnavailableError, match="memory"):
        MacOSVZRuntime(memory_bytes=128 * 1024 * 1024)


@pytest.mark.parametrize(
    ("field", "message"),
    (("agent_seed_path", "agent seed"), ("config_seed_path", "config seed")),
)
def test_tampered_dynamic_seed_is_remeasured_before_helper_launch(
    tmp_path: Path, field: str, message: str
) -> None:
    driver, allocator = _driver(tmp_path)
    original_allocate = allocator.allocate

    def tampering_allocate(**kwargs: Any) -> MacOSVZDomainAllocation:
        allocation = original_allocate(**kwargs)
        Path(str(getattr(allocation, field))).write_bytes(b"tampered")
        return allocation

    allocator.allocate = tampering_allocate  # type: ignore[method-assign]
    with pytest.raises(BackendUnavailableError, match=message):
        _launch(driver)
    assert allocator.transports["domain.provider.conversation"].closed
