"""Focused PackVM guest boundary and authenticated cancellation regressions."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

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


def test_guest_child_policy_denies_all_available_process_and_socket_syscalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-root Pack child gets a fail-closed libseccomp process boundary."""

    class Callable:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            assert callable(self.callback)
            return self.callback(*args)

    rules: list[int] = []
    released: list[object] = []
    context = object()
    syscall_numbers = {
        b"clone": 220,
        b"execve": 221,
        b"socket": 198,
        b"socketpair": 199,
        b"clone3": 435,
        b"execveat": 281,
        b"fork": -1,
        b"vfork": -1,
    }
    seccomp = SimpleNamespace(
        seccomp_init=Callable(lambda _default: context),
        seccomp_rule_add=Callable(
            lambda _context, _action, syscall, _arguments: rules.append(syscall) or 0
        ),
        seccomp_syscall_resolve_name=Callable(lambda name: syscall_numbers[name]),
        seccomp_load=Callable(lambda _context: 0),
        seccomp_release=Callable(lambda value: released.append(value)),
    )
    monkeypatch.setattr(packvm_guest_runner.sys, "platform", "linux")
    monkeypatch.setattr(packvm_guest_runner.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(packvm_guest_runner.ctypes, "CDLL", lambda *_args, **_kwargs: seccomp)

    packvm_guest_runner._install_child_process_seccomp_filter()

    assert set(rules) == {198, 199, 220, 221, 281, 435}
    assert released == [context]


@pytest.mark.parametrize(
    "missing",
    (b"clone", b"clone3", b"execve", b"execveat", b"socket", b"socketpair"),
)
def test_guest_child_policy_rejects_missing_required_syscall(
    missing: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guest missing a mandatory process/socket rule fails before Pack import."""

    class Callable:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            assert callable(self.callback)
            return self.callback(*args)

    released: list[object] = []
    context = object()
    syscall_numbers = {
        b"clone": 220,
        b"execve": 221,
        b"socket": 198,
        b"socketpair": 199,
        b"clone3": 435,
        b"execveat": 281,
        b"fork": -1,
        b"vfork": -1,
    }
    syscall_numbers[missing] = -1
    seccomp = SimpleNamespace(
        seccomp_init=Callable(lambda _default: context),
        seccomp_rule_add=Callable(lambda *_args: 0),
        seccomp_syscall_resolve_name=Callable(lambda name: syscall_numbers[name]),
        seccomp_load=Callable(lambda _context: 0),
        seccomp_release=Callable(lambda value: released.append(value)),
    )
    monkeypatch.setattr(packvm_guest_runner.sys, "platform", "linux")
    monkeypatch.setattr(packvm_guest_runner.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(packvm_guest_runner.ctypes, "CDLL", lambda *_args, **_kwargs: seccomp)

    with pytest.raises(ValueError, match="policy is incomplete"):
        packvm_guest_runner._install_child_process_seccomp_filter()

    assert released == [context]


def test_guest_child_policy_rejects_missing_legacy_aliases_off_arm64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only arm64 may omit fork/vfork, which do not exist in its syscall ABI."""

    class Callable:
        def __init__(self, callback: object) -> None:
            self.callback = callback

        def __call__(self, *args: object) -> object:
            assert callable(self.callback)
            return self.callback(*args)

    released: list[object] = []
    context = object()
    syscall_numbers = {
        b"clone": 56,
        b"clone3": 435,
        b"execve": 59,
        b"execveat": 322,
        b"socket": 41,
        b"socketpair": 53,
        b"fork": -1,
        b"vfork": -1,
    }
    seccomp = SimpleNamespace(
        seccomp_init=Callable(lambda _default: context),
        seccomp_rule_add=Callable(lambda *_args: 0),
        seccomp_syscall_resolve_name=Callable(lambda name: syscall_numbers[name]),
        seccomp_load=Callable(lambda _context: 0),
        seccomp_release=Callable(lambda value: released.append(value)),
    )
    monkeypatch.setattr(packvm_guest_runner.sys, "platform", "linux")
    monkeypatch.setattr(packvm_guest_runner.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(packvm_guest_runner.ctypes, "CDLL", lambda *_args, **_kwargs: seccomp)

    with pytest.raises(ValueError, match="incomplete: fork, vfork"):
        packvm_guest_runner._install_child_process_seccomp_filter()

    assert released == [context]

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


@pytest.mark.parametrize("failure", [OSError("vsock bind failed"), ValueError("bad key")])
def test_vsock_service_startup_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """A listener startup failure remains visible to systemd's restart policy."""

    stderr = io.StringIO()

    def fail_startup() -> int:
        raise failure

    monkeypatch.setattr(packvm_guest_runner, "_serve_vsock_agent", fail_startup)
    monkeypatch.setattr(
        packvm_guest_runner.sys,
        "argv",
        ["packvm_guest_runner.py", "--serve-vsock"],
    )
    monkeypatch.setattr(packvm_guest_runner.sys, "stderr", stderr)

    assert packvm_guest_runner.main() == 1
    assert "PackVM vsock agent startup failed" in stderr.getvalue()


def test_vsock_service_returns_agent_status_without_json_protocol_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The long-lived service retains its direct listener return status."""

    stdout = io.StringIO()
    monkeypatch.setattr(packvm_guest_runner, "_serve_vsock_agent", lambda: 0)
    monkeypatch.setattr(
        packvm_guest_runner.sys,
        "argv",
        ["packvm_guest_runner.py", "--serve-vsock"],
    )
    monkeypatch.setattr(packvm_guest_runner.sys, "stdout", stdout)

    assert packvm_guest_runner.main() == 0
    assert stdout.getvalue() == ""


def test_vsock_service_bounds_active_workers_without_retaining_completed_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The long-lived listener admits only a fixed number of live workers."""

    class Connection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Listener:
        def __init__(self, connections: list[Connection]) -> None:
            self.connections = connections
            self.accepted = 0
            self.lock = threading.Lock()

        def accept(self) -> tuple[Connection, object]:
            with self.lock:
                connection = self.connections[self.accepted]
                self.accepted += 1
                return connection, object()

    connections = [Connection(), Connection(), Connection()]
    listener = Listener(connections)
    release = threading.Event()
    two_active = threading.Event()
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()

    def handle(*_args: object) -> None:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 2:
                two_active.set()
        assert release.wait(timeout=2)
        with state_lock:
            active -= 1

    monkeypatch.setattr(packvm_guest_runner, "_serve_agent_connection", handle)
    failures: list[BaseException] = []

    def serve() -> None:
        try:
            packvm_guest_runner._serve_authenticated_guest_agent(
                listener,  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                max_requests=3,
                max_active_requests=2,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    server = threading.Thread(target=serve)
    server.start()
    assert two_active.wait(timeout=2)
    with listener.lock:
        assert listener.accepted == 2
    release.set()
    server.join(timeout=2)

    assert not server.is_alive()
    assert failures == []
    assert maximum_active == 2
    assert listener.accepted == 3
    assert all(connection.closed for connection in connections)


def test_vsock_console_milestones_are_fixed_and_nonsecret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guest readiness diagnostics write only a fixed line to the VZ console."""

    written: list[bytes] = []
    closed: list[int] = []
    monkeypatch.setattr(packvm_guest_runner.os, "open", lambda *_args: 41)
    monkeypatch.setattr(
        packvm_guest_runner.os,
        "write",
        lambda descriptor, payload: written.append(payload),
    )
    monkeypatch.setattr(
        packvm_guest_runner.os,
        "close",
        lambda descriptor: closed.append(descriptor),
    )

    packvm_guest_runner._emit_vsock_console_phase("vsock-listening")
    packvm_guest_runner._emit_vsock_console_phase("vsock-request-read")
    packvm_guest_runner._emit_vsock_console_phase("vsock-envelope-validated")
    packvm_guest_runner._emit_vsock_console_phase("vsock-signing-complete")
    packvm_guest_runner._emit_vsock_console_phase("vsock-response-sent")
    packvm_guest_runner._emit_vsock_console_phase("agent-ed25519.pem")

    assert written == [
        b"TOBKIRI_AGENT:vsock-listening\n",
        b"TOBKIRI_AGENT:vsock-request-read\n",
        b"TOBKIRI_AGENT:vsock-envelope-validated\n",
        b"TOBKIRI_AGENT:vsock-signing-complete\n",
        b"TOBKIRI_AGENT:vsock-response-sent\n",
    ]
    assert closed == [41, 41, 41, 41, 41]


def test_child_abi_request_limit_stays_within_the_sandbox_memory_limit() -> None:
    """Artifact seed admission is separate from the bounded child ABI frame."""

    class Child:
        returncode = 0

        def communicate(self, *_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
            pytest.fail("oversized child input must be rejected before spawn I/O")

    with pytest.raises(ValueError, match="payload exceeds size limit"):
        packvm_guest_runner._communicate_staged_implementation(
            Child(),  # type: ignore[arg-type]
            {
                "contract_id": "example.contract.v1",
                "operation_id": "inspect",
                "payload": {"content": "x" * packvm_guest_runner.MAX_CHILD_REQUEST_BYTES},
            },
        )

    assert packvm_guest_runner.MAX_CHILD_REQUEST_BYTES < packvm_guest_runner.MAX_REQUEST_BYTES


def test_child_entrypoint_rejects_oversized_abi_input_before_json_decode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The child never allocates the 700 MiB artifact-seed request allowance."""

    stderr = io.StringIO()
    monkeypatch.setattr(packvm_guest_runner.os, "geteuid", lambda: 1)
    monkeypatch.setattr(packvm_guest_runner.sys, "platform", "darwin")
    monkeypatch.setattr(
        packvm_guest_runner.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(b"x" * (packvm_guest_runner.MAX_CHILD_REQUEST_BYTES + 1))
        ),
    )
    monkeypatch.setattr(packvm_guest_runner.sys, "stderr", stderr)

    assert packvm_guest_runner._execute_staged_module(tmp_path / "not-read.py") == 1
    assert "ValueError: PackVM child request exceeds size limit" in stderr.getvalue()


def _agent_config_for_outcome_test() -> packvm_guest_runner._VsockAgentConfig:
    """Build a non-seeded launch binding for agent result-shape tests."""

    return packvm_guest_runner._VsockAgentConfig(
        domain_id="packvm:outcome-test",
        binding_digests={"artifact": _digest("artifact")},
        private_key_path=Path("/run/tobkiri-packvm/agent-ed25519.pem"),
    )


def _runner_completion(
    config: packvm_guest_runner._VsockAgentConfig,
    payload: dict[str, object],
) -> dict[str, object]:
    """Build the exact internal completion that may cross the agent boundary."""

    return {
        "ok": True,
        "protocol": packvm_guest_runner.PROTOCOL,
        "guest_artifact_identity": packvm_guest_runner._bridge_canonical_digest(
            config.binding_digests
        ),
        "payload": payload,
    }


@pytest.mark.parametrize(
    "tamper",
    (
        {"ok": False},
        {"protocol": "other"},
        {"guest_artifact_identity": _digest("other")},
        {"payload": []},
        {"unexpected": True},
    ),
)
def test_agent_invocation_outcome_requires_an_exact_launch_bound_wrapper(
    tamper: dict[str, object],
) -> None:
    """Only the Pack ABI result—not the guest-internal wrapper—reaches Host."""

    config = _agent_config_for_outcome_test()
    outcome = {"kind": "tobkiri.packvm.invoke.result.v1", "outcome": {"ok": True}}
    completion = _runner_completion(config, outcome)

    assert packvm_guest_runner._agent_invoke_outcome(completion, config) == outcome

    completion.update(tamper)
    with pytest.raises(ValueError, match="invocation completion is invalid"):
        packvm_guest_runner._agent_invoke_outcome(completion, config)


def test_agent_direct_invoke_unwraps_the_runner_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct invoke signs the validated Pack ABI payload at data root."""

    config = _agent_config_for_outcome_test()
    outcome = {"kind": "tobkiri.packvm.invoke.result.v1", "outcome": {"ok": True}}
    base = {
        "operation": "invoke",
        "request_id": "request-direct",
        "domain_id": config.domain_id,
        "binding_digests": config.binding_digests,
        "guest_challenge": "a" * 64,
        "payload": {
            "operation": "invoke",
            "request_id": "request-direct",
            "target_domain": config.domain_id,
        },
    }
    monkeypatch.setattr(packvm_guest_runner, "_validate_agent_envelope", lambda *_args: base)
    monkeypatch.setattr(
        packvm_guest_runner,
        "_invoke",
        lambda _request: _runner_completion(config, outcome),
    )

    response = packvm_guest_runner._dispatch_agent_request({}, config, object())

    assert response["data"] == outcome


def test_agent_bridge_result_unwraps_the_runner_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed bridge uses the same Host-visible Pack ABI result shape."""

    config = _agent_config_for_outcome_test()
    outcome = {"kind": "tobkiri.packvm.invoke.result.v1", "outcome": {"ok": True}}
    base = {
        "operation": "bridge_result",
        "request_id": "request-bridge",
        "domain_id": config.domain_id,
        "binding_digests": config.binding_digests,
        "guest_challenge": "b" * 64,
        "host_bridge_result": {},
    }
    pending = packvm_guest_runner._PendingBridge(
        request={"request_digest": _digest("request")},
        guest_artifact_identity=_digest("guest"),
        bridge_request={"continuation": {}},
        expires_at=1.0,
    )

    class Ledger:
        def consume(self, **_kwargs: object) -> packvm_guest_runner._PendingBridge:
            return pending

    monkeypatch.setattr(packvm_guest_runner, "_validate_agent_envelope", lambda *_args: base)
    monkeypatch.setattr(packvm_guest_runner, "_validate_host_bridge_result", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        packvm_guest_runner,
        "_resume_bridge_invocation",
        lambda *_args: _runner_completion(config, outcome),
    )

    response = packvm_guest_runner._dispatch_agent_request({}, config, Ledger())

    assert response["data"] == outcome


def test_openssl_signer_rejects_an_invalid_signature_without_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed OpenSSL command remains a generic guest signer rejection."""

    writes: list[bytes] = []
    seeks: list[tuple[int, int, int]] = []
    closed: list[int] = []
    command: dict[str, object] = {}
    monkeypatch.setattr(
        packvm_guest_runner,
        "_assert_root_only_regular_file",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        packvm_guest_runner.os,
        "memfd_create",
        lambda *_args: 41,
        raising=False,
    )
    monkeypatch.setattr(packvm_guest_runner.os, "MFD_CLOEXEC", 1, raising=False)
    monkeypatch.setattr(
        packvm_guest_runner.os,
        "write",
        lambda _descriptor, payload: writes.append(bytes(payload)) or len(payload),
    )
    monkeypatch.setattr(
        packvm_guest_runner.os,
        "lseek",
        lambda *args: seeks.append(args) or 0,
    )
    monkeypatch.setattr(
        packvm_guest_runner.os,
        "close",
        lambda descriptor: closed.append(descriptor),
    )

    def reject(*args: object, **kwargs: object) -> SimpleNamespace:
        command["args"] = args
        command["kwargs"] = kwargs
        return SimpleNamespace(returncode=1, stdout=b"")

    monkeypatch.setattr(
        packvm_guest_runner.subprocess,
        "run",
        reject,
    )

    signer = packvm_guest_runner._OpenSSLAgentSigner(tmp_path / "agent.pem")
    with pytest.raises(ValueError, match="signature is invalid"):
        signer.sign(b"canonical payload")

    assert writes == [b"canonical payload"]
    assert seeks == [(41, 0, packvm_guest_runner.os.SEEK_SET)]
    assert closed == [41]
    argv = command["args"][0]
    assert isinstance(argv, tuple)
    assert argv[argv.index("-in") + 1] == "/proc/self/fd/41"
    assert command["kwargs"]["pass_fds"] == (41,)
    assert "input" not in command["kwargs"]


def test_openssl_signer_uses_memfd_for_a_real_ed25519_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A Linux OpenSSL build signs the memfd payload without a disk input file."""

    if not hasattr(packvm_guest_runner.os, "memfd_create"):
        pytest.skip("memfd signing is Linux-only")
    version = subprocess.run(
        ("/usr/bin/openssl", "version"),
        capture_output=True,
        check=False,
        text=True,
    )
    if version.returncode != 0 or "LibreSSL" in version.stdout:
        pytest.skip("host OpenSSL does not support the Linux Ed25519 test path")

    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "agent-ed25519.pem"
    key_path.write_bytes(
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    monkeypatch.setattr(
        packvm_guest_runner,
        "_assert_root_only_regular_file",
        lambda *_args: None,
    )

    payload = b"canonical guest attestation"
    signature = packvm_guest_runner._OpenSSLAgentSigner(key_path).sign(payload)

    assert len(signature) == 64
    key.public_key().verify(signature, payload)


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (100.0, "100"),
        (100, "100"),
        ("100", "100"),
        ("1.0000000000000001e-05", "1.0000000000000001e-05"),
        ("1e+20", "1e+20"),
    ),
)
def test_guest_deadline_accepts_numeric_and_canonical_host_forms(
    value: object,
    expected: str,
) -> None:
    """One-shot numbers and exact Host ``.17g`` strings share one validator."""

    assert packvm_guest_runner._normalise_bridge_deadline(value) == expected


@pytest.mark.parametrize(
    "value",
    (
        False,
        0,
        -1,
        float("nan"),
        float("inf"),
        "",
        "0",
        "-1",
        "nan",
        "inf",
        "1E+20",
        "1e+020",
        "1" * (packvm_guest_runner.MAX_DEADLINE_TEXT_BYTES + 1),
        10**1000,
    ),
)
def test_guest_deadline_rejects_noncanonical_or_nonfinite_values(value: object) -> None:
    """Guest deadline admission rejects invalid values before execution starts."""

    with pytest.raises(ValueError, match="deadline is invalid"):
        packvm_guest_runner._normalise_bridge_deadline(value)


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
