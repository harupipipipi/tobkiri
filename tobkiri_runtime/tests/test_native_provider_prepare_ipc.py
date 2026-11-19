"""Exercise the original Host's synchronous macOS Provider connection."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core_runtime import native_provider_prepare_ipc as ipc
from core_runtime.process_identity import process_start_identity
from core_runtime.provider_prepare_native_v4 import NativeProviderPreparePlanner


pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin peer identity")
_HEADER = struct.Struct("!BI")


class _Borrow:
    context = SimpleNamespace(
        profile_id="defaults",
        profile_revision="sha256:" + "a" * 64,
        activation_id="activation",
        plan_digest="sha256:" + "b" * 64,
        security_epoch=3,
    )
    request = {
        "connection_name": "fixture",
        "endpoint": "http://127.0.0.1:11434/v1/",
        "catalog_provider_id": "fixture-id",
        "display_name": "ローカル",
        "capabilities": ("ai.stream", "ai.generate"),
        "key_value": "discarded-by-the-wire-shape",
    }
    runtime_instance = "a" * 32
    frozen_plan: dict[str, Any] | None = None

    def __init__(self, lifetime: float = 3.0) -> None:
        self.deadline_monotonic = time.monotonic() + lifetime
        self.thread_id = threading.get_ident()
        self.revoked = threading.Event()

    def assert_current(self) -> None:
        assert threading.get_ident() == self.thread_id
        if self.revoked.is_set():
            raise PermissionError("fixture lease revoked")


def _read_exact(peer: socket.socket, size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        part = peer.recv(size - len(value))
        assert part, "peer closed mid-frame"
        value.extend(part)
    return bytes(value)


def _receive(peer: socket.socket) -> tuple[int, bytes]:
    tag, size = _HEADER.unpack(_read_exact(peer, _HEADER.size))
    return tag, _read_exact(peer, size)


def _send(peer: socket.socket, tag: int, payload: bytes = b"") -> None:
    peer.sendall(_HEADER.pack(tag, len(payload)) + payload)


def _owner() -> str:
    evidence = process_start_identity(os.getpid())
    assert evidence.state == "live"
    return evidence.identity.removeprefix("darwin:")


@contextmanager
def _listener() -> Iterator[tuple[socket.socket, str]]:
    with tempfile.TemporaryDirectory(dir="/tmp") as root:
        address = str(Path(root) / "provider.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.settimeout(5)
            listener.bind(address)
            listener.listen(1)
            yield listener, address


@contextmanager
def _served(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[NativeProviderPreparePlanner]:
    with _listener() as (listener, address):
        monkeypatch.setenv(ipc._SOCKET_ENV, address)
        monkeypatch.setenv(ipc._OWNER_ENV, _owner())
        monkeypatch.setattr(ipc.os, "getppid", os.getpid)
        planner = ipc.capture_launcher_provider_prepare_planner()
        assert planner is not None

        def accept() -> None:
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(5)
                serve(peer)

        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(accept)
            yield planner
            result.result(timeout=5)


@pytest.mark.parametrize("apply", [False, True])
def test_registry_limit_allows_fallback_after_a_live_checkpoint(
    monkeypatch: pytest.MonkeyPatch, apply: bool,
) -> None:
    borrow = _Borrow()
    if apply:
        borrow.frozen_plan = {"expected_revision": 42}

    def serve(peer: socket.socket) -> None:
        tag, data = _receive(peer)
        assert tag == 1
        frame = json.loads(data)
        assert frame.get("apply") == borrow.frozen_plan
        assert ("apply" in frame) is apply
        assert "plan" not in frame
        assert "key_value" not in frame["request"]
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 8, b"legacy-read-required")
        assert peer.recv(1) == b""

    with _served(monkeypatch, serve) as planner:
        with pytest.raises(ipc.NativeProviderRegistryRequiresLegacyRead):
            planner.plan(borrow)


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("tag, size", [(4, 0), (5, 2), (6, 0), (7, 16 * 1024 + 1)])
def test_obsolete_document_tags_and_oversized_results_cancel_the_exchange(
    monkeypatch: pytest.MonkeyPatch, apply: bool, tag: int, size: int,
) -> None:
    borrow = _Borrow()
    if apply:
        borrow.frozen_plan = {"expected_revision": 42}

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        # The Host must reject the header without waiting for the claimed body.
        peer.sendall(_HEADER.pack(tag, size))
        assert _receive(peer) == (9, b"")
        assert peer.recv(1) == b""

    with _served(monkeypatch, serve) as planner:
        with pytest.raises(RuntimeError, match="cleanup was unconfirmed"):
            planner.plan(borrow)


def test_captured_launcher_borrow_preserves_raw_request_and_checks_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _Borrow()
    plan = {"expected_revision": 42, "endpoint": "http://127.0.0.1:11434/v1"}

    def serve(peer: socket.socket) -> None:
        tag, payload = _receive(peer)
        assert tag == 1
        numerator, denominator = borrow.deadline_monotonic.as_integer_ratio()
        assert json.loads(payload) == {
            "identity": {
                key: value for key, value in vars(borrow.context).items()
                if key != "security_epoch"
            },
            "security_epoch": 3,
            "runtime_instance": "a" * 32,
            "deadline_ns": numerator * 1_000_000_000 // denominator,
            "request": {
                "connection-name": "fixture",
                "endpoint": "http://127.0.0.1:11434/v1/",
                "catalog-provider-id": "fixture-id",
                "display-name": "ローカル",
                "capabilities": ["ai.stream", "ai.generate"],
            },
        }
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, json.dumps(plan).encode())

    with _served(monkeypatch, serve) as planner:
        monkeypatch.setenv(ipc._SOCKET_ENV, "/invalidated-after-capture")
        monkeypatch.delenv(ipc._OWNER_ENV)
        assert planner.plan(borrow) == plan


def test_result_before_a_live_checkpoint_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 7, b"{}")
        assert _receive(peer) == (9, b"")
        _send(peer, 8, b"cancelled")

    with _served(monkeypatch, serve) as planner:
        with pytest.raises(RuntimeError, match="frame is out of order"):
            planner.plan(_Borrow())


def test_size_fallback_requires_a_live_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 8, b"legacy-read-required")

    with _served(monkeypatch, serve) as planner:
        with pytest.raises(RuntimeError, match="failed") as error:
            planner.plan(_Borrow())
        assert not isinstance(error.value, ipc.NativeProviderRegistryRequiresLegacyRead)


@pytest.mark.parametrize("work_started", [False, True])
def test_inapplicable_can_decline_only_before_a_live_checkpoint(
    monkeypatch: pytest.MonkeyPatch, work_started: bool,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if work_started:
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, 10)
        if work_started:
            assert _receive(peer) == (9, b"")
            _send(peer, 8, b"cancelled")

    with _served(monkeypatch, serve) as planner:
        if work_started:
            with pytest.raises(RuntimeError, match="frame is out of order") as error:
                planner.plan(_Borrow())
            assert not isinstance(error.value, ipc.NativeProviderPrepareUnavailable)
        else:
            with pytest.raises(ipc.NativeProviderPrepareUnavailable):
                planner.plan(_Borrow())


def test_revocation_cancels_once_across_a_fragmented_live_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _Borrow()

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        pending_check = _HEADER.pack(2, 0)
        peer.sendall(pending_check[:2])
        borrow.revoked.set()
        assert _receive(peer) == (9, b"")
        peer.sendall(pending_check[2:])
        _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with _served(monkeypatch, serve) as planner:
        with pytest.raises(PermissionError, match="fixture lease revoked"):
            planner.plan(borrow)


@pytest.mark.parametrize("terminal", [None, (7, b"{}"), (8, b"unexpected")])
def test_deadline_discards_late_plan_and_requires_cleanup_proof(
    monkeypatch: pytest.MonkeyPatch, terminal: tuple[int, bytes] | None,
) -> None:
    borrow = _Borrow(lifetime=0.25)

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        assert _receive(peer) == (9, b"")
        if terminal is not None:
            _send(peer, *terminal)

    with _served(monkeypatch, serve) as planner:
        with pytest.raises(RuntimeError, match="cleanup was unconfirmed"):
            planner.plan(borrow)


def test_deadline_keeps_original_failure_after_cancel_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _Borrow(lifetime=0.25)

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        assert _receive(peer) == (9, b"")
        _send(peer, 8, b"cancelled")

    with _served(monkeypatch, serve) as planner:
        with pytest.raises(TimeoutError, match="deadline elapsed"):
            planner.plan(borrow)


def test_real_child_rejects_socket_owned_by_a_different_same_uid_process() -> None:
    runtime = Path(__file__).resolve().parents[1]
    child_code = """
import time
from types import SimpleNamespace
from core_runtime import native_provider_prepare_ipc
planner = native_provider_prepare_ipc.capture_launcher_provider_prepare_planner()
borrow = SimpleNamespace(
    deadline_monotonic=time.monotonic() + 4, assert_current=lambda: None,
)
try:
    planner.plan(borrow)
except PermissionError as error:
    if str(error) != 'native Provider preparation peer could not be verified':
        raise
else:
    raise AssertionError('substituted socket owner was authenticated')
"""
    server_code = """
import socket
import sys
with socket.socket(socket.AF_UNIX) as listener:
    listener.settimeout(5)
    listener.bind(sys.argv[1])
    listener.listen(1)
    print('ready', flush=True)
    peer, _ = listener.accept()
    with peer:
        peer.settimeout(5)
        assert peer.recv(1) == b'', 'untrusted server received data'
"""
    with tempfile.TemporaryDirectory(dir="/tmp") as root:
        address = str(Path(root) / "substitute.sock")
        environment = os.environ | {ipc._SOCKET_ENV: address, ipc._OWNER_ENV: _owner()}
        with subprocess.Popen(
            [sys.executable, "-B", "-c", server_code, address],
            stdout=subprocess.PIPE,
        ) as server:
            try:
                assert server.stdout is not None
                assert server.stdout.readline() == b"ready\n"
                subprocess.run(
                    [sys.executable, "-B", "-c", child_code],
                    env=environment, cwd=runtime, timeout=6, check=True,
                )
                assert server.wait(timeout=6) == 0
            finally:
                if server.poll() is None:
                    server.kill()
