"""QEMU serial framing and unchanged guest authentication, without a VM claim."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import threading
from types import SimpleNamespace
from typing import BinaryIO

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat,
)
import pytest

from ecosystem.defaultspack.backend.sandbox.isolation.resources import (
    packvm_guest_runner as runner,
)


class _TestSigner:
    """Explicit test signer; never substitutes for the production Ed25519 path."""

    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def sign(self, payload: bytes) -> bytes:
        self.payloads.append(payload)
        return hashlib.sha512(payload).digest()


def _config() -> runner._VsockAgentConfig:
    fields = (
        "domain", "lease", "reservation", "image", "agent", "config", "disk",
        "guest_public_key", "efi_variable_store", "artifact", "executable",
        "materialization", "kernel", "initrd",
    )
    return runner._VsockAgentConfig(
        domain_id="packvm:linux-test",
        binding_digests={
            field: "sha256:" + hashlib.sha256(field.encode()).hexdigest()
            for field in fields
        },
        private_key_path=runner.PACKVM_GUEST_AGENT_KEY,
    )


def _request(operation: str = "attest", challenge: str = "a" * 64) -> dict:
    config = _config()
    value = {
        "protocol": runner.PROTOCOL,
        "operation": operation,
        "request_id": "request-1",
        "domain_id": config.domain_id,
        "binding_digests": config.binding_digests,
        "guest_challenge": challenge,
    }
    if operation == "attest":
        value.update(
            request_id=f"attest-{config.domain_id}", attestation_nonce="b" * 64,
        )
    if operation == "invoke":
        value["payload"] = {
            "operation": "invoke", "request_id": value["request_id"],
            "target_domain": config.domain_id, "budget_seconds": "5",
        }
    return value


def _encoded(request: dict) -> bytes:
    return runner._bridge_canonical_json(request) + b"\n"


@dataclass
class _Harness:
    client: socket.socket
    reader: BinaryIO
    errors: list[BaseException]

    def response(self) -> dict:
        encoded = self.reader.readline(runner.MAX_AGENT_RESPONSE_BYTES + 2)
        assert encoded.endswith(b"\n")
        response = json.loads(encoded)
        assert encoded == _encoded(response)
        assert response["agent_signature"]
        return response


@contextmanager
def _agent(
    signer: runner._AgentSigner | None = None,
    *,
    max_requests: int | None = None,
    max_active_requests: int = runner.MAX_ACTIVE_AGENT_REQUESTS,
) -> Iterator[_Harness]:
    client, guest = socket.socketpair()
    client.settimeout(3)
    reader = client.makefile("rb")
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            runner._serve_authenticated_serial_agent(
                guest.fileno(), _config(), signer or _TestSigner(),
                max_requests=max_requests, max_active_requests=max_active_requests,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            guest.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield _Harness(client, reader, errors)
    finally:
        reader.close()
        client.close()
        thread.join(timeout=3)
        assert not thread.is_alive(), "serial service did not retire its stream"


def test_serial_fragmented_and_coalesced_frames_share_exact_signed_protocol() -> None:
    signer = _TestSigner()
    first = _encoded(_request(challenge="1" * 64))
    second = _encoded(_request(challenge="2" * 64))
    with _agent(signer, max_requests=2) as agent:
        agent.client.sendall(first[:31])
        agent.client.sendall(first[31:] + second)
        responses = [agent.response(), agent.response()]
    assert not agent.errors
    assert {item["guest_challenge"] for item in responses} == {"1" * 64, "2" * 64}
    for response in responses:
        signature = base64.b64decode(response.pop("agent_signature"))
        encoded = runner._bridge_canonical_json(response)
        assert signature == hashlib.sha512(encoded).digest()
        assert encoded in signer.payloads
        assert response["success"] is True
        assert response["protocol"] == runner.PACKVM_GUEST_AGENT_RESPONSE_PROTOCOL
        assert response["data"] == {
            "guest_artifact_identity": runner._bridge_canonical_digest(
                _config().binding_digests,
            ),
        }


def test_serial_response_uses_actual_openssl_ed25519_signer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "memfd_create") or not Path("/usr/bin/openssl").is_file():
        pytest.skip("Linux OpenSSL and memfd are required for the signer test")
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "test-agent.pem"
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    path.chmod(0o600)
    checked: list[Path] = []
    # The test process is unprivileged. Only the uid check is adapted here;
    # actual memfd, OpenSSL subprocess, canonical bytes, and signature are used.
    def test_owned_key(key_path: Path, _label: str) -> None:
        assert key_path == path
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        checked.append(key_path)

    monkeypatch.setattr(runner, "_assert_root_only_regular_file", test_owned_key)
    with _agent(runner._OpenSSLAgentSigner(path), max_requests=1) as agent:
        agent.client.sendall(_encoded(_request()))
        response = agent.response()
    assert not agent.errors
    signature = base64.b64decode(response.pop("agent_signature"), validate=True)
    key.public_key().verify(signature, runner._bridge_canonical_json(response))
    assert checked == [path]


@pytest.mark.parametrize(
    "encoded",
    [
        b"\n", b"{broken}\n", b"[]\n", b'{"x": 1}\n',
        b'{"x":1,"x":2}\n', b'{"x":NaN}\n', b'\xff\n',
        b'{"x":"\\ud800"}\n', b"[" * 2000 + b"]" * 2000 + b"\n",
    ],
    ids=[
        "empty", "invalid-json", "array", "whitespace", "duplicate-key",
        "nan", "invalid-utf8", "surrogate", "deeply-nested",
    ],
)
def test_serial_malformed_frames_retire_without_signing(encoded: bytes) -> None:
    signer = _TestSigner()
    with _agent(signer) as agent:
        agent.client.sendall(encoded)
        assert agent.reader.read() == b""
    assert len(agent.errors) == 1
    assert isinstance(agent.errors[0], ValueError)
    assert not signer.payloads


def test_serial_oversized_frame_is_bounded_before_json_or_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "MAX_AGENT_REQUEST_BYTES", 128)
    signer = _TestSigner()
    with _agent(signer) as agent:
        agent.client.sendall(b"x" * 128)
        assert agent.reader.read() == b""
    assert isinstance(agent.errors[0], ValueError)
    assert "size limit" in str(agent.errors[0])
    assert not signer.payloads


@pytest.mark.parametrize("suffix", [b"", b'{"incomplete":'])
def test_serial_idle_or_truncated_eof_retires_service(suffix: bytes) -> None:
    with _agent() as agent:
        if suffix:
            agent.client.sendall(suffix)
        agent.client.shutdown(socket.SHUT_WR)
        assert agent.reader.read() == b""
    assert len(agent.errors) == 1
    assert isinstance(agent.errors[0], ConnectionError)


def test_serial_partial_frame_has_finite_read_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "AGENT_IO_TIMEOUT_SECONDS", 0.02)
    with _agent() as agent:
        agent.client.sendall(b"{")
        assert agent.reader.read() == b""
    assert isinstance(agent.errors[0], TimeoutError)


def test_serial_replayed_challenge_is_signed_failure() -> None:
    with _agent(max_requests=2) as agent:
        agent.client.sendall(_encoded(_request()))
        assert agent.response()["success"] is True
        agent.client.sendall(_encoded(_request()))
        assert agent.response()["success"] is False
    assert not agent.errors


def test_serial_cancel_has_reserved_capacity_and_root_request_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()
    record = {
        "request_id": "request-1", "target_domain": _config().domain_id,
        "guest_artifact_identity": "sha256:" + "1" * 64,
        "cancel_token": "2" * 64, "process_group": 1234,
    }
    ownership_reads: list[Path] = []

    def invoke(_payload: dict, **_kwargs: object) -> dict:
        started.set()
        try:
            assert cancelled.wait(2), "cancellation was blocked behind invocation"
            return {
                "ok": True, "protocol": runner.PROTOCOL,
                "guest_artifact_identity": runner._bridge_canonical_digest(
                    _config().binding_digests,
                ),
                "payload": {"cancel_observed": True},
            }
        finally:
            finished.set()

    def read_record(path: Path) -> dict:
        assert path == runner._request_path("request-1")
        ownership_reads.append(path)
        return dict(record)

    def terminate(process_group: int) -> list[str]:
        assert process_group == record["process_group"]
        cancelled.set()
        return ["TERM"]

    # Explicit process/root-record adapters; real envelope, ledger, ownership
    # field/token checks and response signing stay in the serial service.
    monkeypatch.setattr(runner, "_invoke", invoke)
    monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner, "_read_request", read_record)
    monkeypatch.setattr(runner, "_terminate_process_group", terminate)
    try:
        with _agent(max_requests=3, max_active_requests=2) as agent:
            agent.client.sendall(_encoded(_request("invoke", "1" * 64)))
            assert started.wait(2)
            agent.client.sendall(_encoded(_request("invoke", "2" * 64)))
            busy = agent.response()
            assert busy["success"] is False
            assert busy["guest_challenge"] == "2" * 64
            agent.client.sendall(_encoded(_request("cancel", "3" * 64)))
            responses = [agent.response(), agent.response()]
    finally:
        cancelled.set()
    assert finished.wait(2)
    assert not agent.errors
    assert len(ownership_reads) == 2
    by_challenge = {response["guest_challenge"]: response for response in responses}
    assert by_challenge["1" * 64]["success"] is True
    assert by_challenge["3" * 64]["data"]["signals"] == ["TERM"]
    assert {response["request_id"] for response in responses} == {"request-1"}


def test_serial_cross_domain_cancel_never_touches_ownership_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(_path: Path) -> dict:
        pytest.fail("invalid domain must not access an ownership record")

    monkeypatch.setattr(runner, "_read_request", forbidden)
    request = _request("cancel")
    request["domain_id"] = "packvm:other-domain"
    with _agent(max_requests=1) as agent:
        agent.client.sendall(_encoded(request))
        assert agent.response()["success"] is False
    assert not agent.errors


def test_serial_signer_failure_retires_stream() -> None:
    class BrokenSigner:
        def sign(self, _payload: bytes) -> bytes:
            raise ValueError("test signer unavailable")

    with _agent(BrokenSigner(), max_requests=1) as agent:
        agent.client.sendall(_encoded(_request()))
        assert agent.reader.read() == b""
    assert len(agent.errors) == 1
    assert isinstance(agent.errors[0], ValueError)


def test_serial_response_limit_retires_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "MAX_AGENT_RESPONSE_BYTES", 64)
    with _agent(max_requests=1) as agent:
        agent.client.sendall(_encoded(_request()))
        assert agent.reader.read() == b""
    assert len(agent.errors) == 1
    assert isinstance(agent.errors[0], ValueError)


def test_serial_stream_descriptor_is_never_inheritable() -> None:
    client, guest = socket.socketpair()
    try:
        os.set_inheritable(guest.fileno(), True)
        runner._SerialAgentStream(guest.fileno(), threading.Event())
        assert not os.get_inheritable(guest.fileno())
        assert not os.get_blocking(guest.fileno())
    finally:
        client.close()
        guest.close()


def test_serial_child_spawn_closes_even_an_inheritable_transport_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, guest = socket.socketpair()
    try:
        runner._SerialAgentStream(guest.fileno(), threading.Event())
        # Defense in depth: even accidental later inheritance must not expose
        # the host channel to a Pack child. Only the sandbox executable is a
        # test adapter here; the actual Popen close_fds boundary is exercised.
        os.set_inheritable(guest.fileno(), True)
        code = (
            "import os,sys\n"
            f"try: os.fstat({guest.fileno()})\n"
            "except OSError: print('closed')\n"
            "else: sys.exit('transport leaked')\n"
        )
        monkeypatch.setattr(
            runner, "_sandbox_argv", lambda *_args: (sys.executable, "-c", code),
        )
        process = runner._spawn_staged_implementation(tmp_path, tmp_path / "pack.py")
        output, error = process.communicate(timeout=3)
        assert process.returncode == 0, error
        assert output == b"closed\n"
    finally:
        client.close()
        guest.close()


def test_serial_response_short_writes_preserve_whole_concurrent_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, guest = socket.socketpair()
    client.settimeout(3)
    actual_write = os.write
    monkeypatch.setattr(
        runner.os, "write", lambda descriptor, data: actual_write(descriptor, data[:7]),
    )
    stream = runner._SerialAgentStream(guest.fileno(), threading.Event())
    errors: list[BaseException] = []

    def write(data: bytes) -> None:
        try:
            stream.write_response(data)
        except BaseException as exc:
            errors.append(exc)

    workers = [
        threading.Thread(target=write, args=(data,))
        for data in (b"x" * 1000, b"y" * 1000)
    ]
    try:
        for worker in workers:
            worker.start()
        with client.makefile("rb") as reader:
            assert {reader.readline(), reader.readline()} == {
                b"x" * 1000 + b"\n", b"y" * 1000 + b"\n",
            }
        for worker in workers:
            worker.join(timeout=3)
            assert not worker.is_alive()
        assert not errors
    finally:
        client.close()
        guest.close()


def test_serial_response_backpressure_has_finite_write_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "AGENT_IO_TIMEOUT_SECONDS", 0.02)
    client, guest = socket.socketpair()
    try:
        guest.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        stream = runner._SerialAgentStream(guest.fileno(), threading.Event())
        with pytest.raises(TimeoutError, match="timed out"):
            stream.write_response(b"x" * (1024 * 1024))
    finally:
        client.close()
        guest.close()


def test_serial_response_to_closed_peer_fails() -> None:
    client, guest = socket.socketpair()
    try:
        stream = runner._SerialAgentStream(guest.fileno(), threading.Event())
        client.close()
        with pytest.raises(OSError):
            stream.write_response(b"{}")
    finally:
        client.close()
        guest.close()


def test_serial_production_entry_requires_root_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.os, "geteuid", lambda: 1000)
    with pytest.raises(ValueError, match="root-owned"):
        runner._serve_virtio_serial_agent()


@pytest.mark.parametrize("mode,uid", [
    (stat.S_IFREG | 0o600, 0),
    (stat.S_IFCHR | 0o666, 0),
    (stat.S_IFCHR | 0o600, 1000),
])
def test_serial_production_rejects_unsafe_device_and_closes_fd(
    monkeypatch: pytest.MonkeyPatch, mode: int, uid: int,
) -> None:
    monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner, "_load_vsock_agent_config", lambda _path: _config())
    monkeypatch.setattr(runner, "_assert_root_only_regular_file", lambda *_args: None)
    descriptor = os.open("/dev/null", os.O_RDWR)

    def open_fixed_device(path: Path, flags: int) -> int:
        assert path == Path("/dev/virtio-ports/io.tobkiri.packvm.agent")
        assert flags == os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK
        return descriptor

    monkeypatch.setattr(runner.os, "open", open_fixed_device)
    monkeypatch.setattr(
        runner.os, "fstat", lambda _fd: SimpleNamespace(st_mode=mode, st_uid=uid),
    )
    with pytest.raises(ValueError, match="device is unsafe"):
        runner._serve_virtio_serial_agent()
    with pytest.raises(OSError):
        os.get_inheritable(descriptor)


def test_serial_production_checks_key_then_serves_fixed_root_owned_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner, "_load_vsock_agent_config", lambda _path: config)
    checked: list[Path] = []
    monkeypatch.setattr(
        runner, "_assert_root_only_regular_file",
        lambda path, _label: checked.append(path),
    )
    descriptor = os.open("/dev/null", os.O_RDWR)

    def open_device(path: Path, flags: int) -> int:
        assert checked == [config.private_key_path]
        assert path == runner.PACKVM_GUEST_AGENT_SERIAL_DEVICE
        assert flags == os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK
        return descriptor

    def serve(fd: int, loaded: runner._VsockAgentConfig, signer: object) -> int:
        assert fd == descriptor and loaded is config
        assert isinstance(signer, runner._OpenSSLAgentSigner)
        assert signer._key_path == config.private_key_path
        assert not os.get_inheritable(fd)
        return 0

    monkeypatch.setattr(runner.os, "open", open_device)
    monkeypatch.setattr(
        runner.os, "fstat",
        lambda _fd: SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_uid=0),
    )
    monkeypatch.setattr(runner, "_serve_authenticated_serial_agent", serve)
    assert runner._serve_virtio_serial_agent() == 0
    with pytest.raises(OSError):
        os.get_inheritable(descriptor)


def test_serial_main_failure_has_nonzero_status_without_raw_diagnostics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def unavailable() -> int:
        raise OSError("test-private-key-path-do-not-log")

    monkeypatch.setattr(runner.sys, "argv", ["runner", "--serve-virtio-serial"])
    monkeypatch.setattr(runner, "_serve_virtio_serial_agent", unavailable)
    assert runner.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "PackVM virtio-serial agent stopped.\n"
