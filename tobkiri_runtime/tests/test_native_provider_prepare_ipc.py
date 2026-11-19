"""Exercise the original Host's synchronous macOS Registry connection."""

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
from collections.abc import Callable, Iterator, Mapping
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


@contextmanager
def _served_model_settings(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeModelSettingsRequest], Mapping[str, Any]]]:
    with _served(monkeypatch, serve):
        reader = ipc.capture_launcher_model_settings_reader()
        assert reader is not None
        yield reader


def _model_request(borrow: _Borrow) -> ipc.NativeModelSettingsRequest:
    return ipc.NativeModelSettingsRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
    )


@contextmanager
def _served_model_save(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeModelSaveRequest], Mapping[str, Any]]]:
    with _served(monkeypatch, serve):
        writer = ipc.model_save_from_environment()
        assert writer is not None
        yield writer


def _model_save_request(
    borrow: _Borrow, marker: Callable[[], None],
) -> ipc.NativeModelSaveRequest:
    return ipc.NativeModelSaveRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        request={
            "model_profile_id": "fixture-profile",
            "model_id": "fixture-model",
            "provider_instance_id": "fixture-provider",
            "display_name": "ローカル",
            "expected_revision": 12,
            "provider_registry_revision": 4,
            "max_output_tokens": 16,
        },
        provider={
            "revision": 4,
            "providers": [{"provider_instance_id": "fixture-provider", "enabled": True}],
        },
        mark_publication_possible=marker,
    )


_SAVE_RESULT = {
    "action": "saved",
    "store_revision": 13,
    "profile": {
        "model_profile_id": "fixture-profile",
        "model_id": "fixture-model",
        "display_name": "ローカル",
        "enabled": True,
        "parameters": {"temperature": 0.5, "max_tokens": 16},
        "metadata": {"wide": 1 << 128, "provider_connection_id": "fixture-provider"},
    },
}
_SAVE_RESULT_BYTES = json.dumps(_SAVE_RESULT, ensure_ascii=False).encode("utf-8")


def test_model_save_marker_and_live_guard_run_before_exactly_one_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SaveBorrow(_Borrow):
        def __init__(self) -> None:
            super().__init__()
            self.marked = False
            self.checked_after_marker = threading.Event()

        def mark_possible(self) -> None:
            assert threading.get_ident() == self.thread_id
            assert not self.marked
            self.marked = True

        def assert_current(self) -> None:
            super().assert_current()
            if self.marked:
                self.checked_after_marker.set()

    borrow = SaveBorrow()
    request = _model_save_request(borrow, borrow.mark_possible)

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
            "model_save": {"request": request.request, "provider": request.provider},
        }
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        assert not borrow.marked
        _send(peer, 11)
        assert _receive(peer) == (12, b"")
        assert borrow.checked_after_marker.is_set()
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, _SAVE_RESULT_BYTES)
        assert peer.recv(1) == b""

    with _served_model_save(monkeypatch, serve) as writer:
        monkeypatch.delenv(ipc._SOCKET_ENV)
        monkeypatch.delenv(ipc._OWNER_ENV)
        assert writer(request) == _SAVE_RESULT


def test_model_save_noop_returns_owner_result_without_publication_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, _SAVE_RESULT_BYTES)
        assert peer.recv(1) == b""

    with _served_model_save(monkeypatch, serve) as writer:
        assert writer(_model_save_request(_Borrow(), _unexpected_marker)) == _SAVE_RESULT


def _unexpected_marker() -> None:
    pytest.fail("this exchange cannot grant publication permission")


@pytest.mark.parametrize("live", [False, True])
def test_model_save_compatibility_fallback_requires_live_admission(
    monkeypatch: pytest.MonkeyPatch, live: bool,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if live:
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, 8, b"legacy-read-required")
        assert peer.recv(1) == b""

    expected = (
        ipc.NativeProviderRegistryRequiresLegacyRead if live else ipc._ProtocolError
    )
    with _served_model_save(monkeypatch, serve) as writer, pytest.raises(expected):
        writer(_model_save_request(_Borrow(), _unexpected_marker))


@pytest.mark.parametrize(
    "tag, payload, expected",
    [
        (10, b"", ipc.NativeProviderPrepareUnavailable),
        (8, b"conflict", ipc.NativeModelSaveFailure),
    ],
)
def test_model_save_maps_pre_permit_failures(
    monkeypatch: pytest.MonkeyPatch, tag: int, payload: bytes,
    expected: type[RuntimeError],
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, tag, payload)
        assert peer.recv(1) == b""

    with (
        _served_model_save(monkeypatch, serve) as writer,
        pytest.raises(expected) as failure,
    ):
        writer(_model_save_request(_Borrow(), _unexpected_marker))
    if isinstance(failure.value, ipc.NativeModelSaveFailure):
        assert failure.value.code == "conflict"


@pytest.mark.parametrize("tag, payload", [(11, b""), (12, b""), (7, _SAVE_RESULT_BYTES)])
def test_model_save_rejects_out_of_order_frames_before_live(
    monkeypatch: pytest.MonkeyPatch, tag: int, payload: bytes,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, tag, payload)
        assert _receive(peer) == (9, b"")
        if tag != 12:
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with _served_model_save(monkeypatch, serve) as writer:
        with pytest.raises(RuntimeError) as failure:
            writer(_model_save_request(_Borrow(), _unexpected_marker))
        assert not isinstance(failure.value, ipc.NativeModelSavePublicationUnconfirmed)


@pytest.mark.parametrize("list_role", [False, True])
def test_legacy_registry_roles_do_not_accept_publication_checks(
    monkeypatch: pytest.MonkeyPatch, list_role: bool,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 11)
        assert _receive(peer) == (9, b"")
        assert peer.recv(1) == b""

    with (
        _served(monkeypatch, serve) as planner,
        pytest.raises(RuntimeError, match="cleanup was unconfirmed"),
    ):
        if list_role:
            reader = ipc.capture_launcher_model_settings_reader()
            assert reader is not None
            reader(_model_request(_Borrow()))
        else:
            planner.plan(_Borrow())


@pytest.mark.parametrize("after_marker", [False, True])
def test_model_save_checks_revocation_before_and_after_marker(
    monkeypatch: pytest.MonkeyPatch, after_marker: bool,
) -> None:
    borrow = _Borrow()

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        if not after_marker:
            borrow.revoked.set()
        _send(peer, 11)
        assert _receive(peer) == (9, b"")
        _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    marker = borrow.revoked.set if after_marker else _unexpected_marker
    expected = (
        ipc.NativeModelSavePublicationUnconfirmed if after_marker else PermissionError
    )
    with _served_model_save(monkeypatch, serve) as writer, pytest.raises(expected):
        writer(_model_save_request(borrow, marker))


@pytest.mark.parametrize("terminal", [
    None,
    (11, b""),
    (8, b"legacy-read-required"),
    (8, b"conflict"),
    (10, b""),
    (7, b"{}"),
    (7, _SAVE_RESULT_BYTES.replace(b"0.5", b"1e999")),
    (7, _SAVE_RESULT_BYTES.replace(b'"wide":', b'"wide":1,"wide":')),
])
def test_model_save_only_confirms_a_valid_terminal_result_after_permit(
    monkeypatch: pytest.MonkeyPatch, terminal: tuple[int, bytes] | None,
) -> None:
    marks = []

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 11)
        assert _receive(peer) == (12, b"")
        if terminal is None:
            return
        _send(peer, *terminal)
        if terminal[0] != 8:
            assert _receive(peer) == (9, b"")
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with (
        _served_model_save(monkeypatch, serve) as writer,
        pytest.raises(ipc.NativeModelSavePublicationUnconfirmed),
    ):
        writer(_model_save_request(_Borrow(), lambda: marks.append(True)))
    assert marks == [True]


@pytest.mark.parametrize("revoke", [False, True])
def test_model_save_loses_guard_during_result_delivery(
    monkeypatch: pytest.MonkeyPatch, revoke: bool,
) -> None:
    borrow = _Borrow(lifetime=3 if revoke else 0.5)

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 11)
        assert _receive(peer) == (12, b"")
        peer.sendall(_HEADER.pack(7, len(_SAVE_RESULT_BYTES)))
        if revoke:
            borrow.revoked.set()
        assert _receive(peer) == (9, b"")
        peer.sendall(_SAVE_RESULT_BYTES)
        assert peer.recv(1) == b""

    with (
        _served_model_save(monkeypatch, serve) as writer,
        pytest.raises(ipc.NativeModelSavePublicationUnconfirmed),
    ):
        writer(_model_save_request(borrow, lambda: None))


def test_model_settings_reader_sends_only_its_role_and_accepts_one_mib_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _Borrow()
    request = _model_request(borrow)
    result = {"profiles": [], "count": 0, "registry_revision": 12}
    response = json.dumps(result).encode("utf-8").ljust(1024 * 1024, b" ")

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
            "model_list": {},
        }
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, response)

    with _served_model_settings(monkeypatch, serve) as reader:
        monkeypatch.setenv(ipc._SOCKET_ENV, "/invalidated-after-capture")
        monkeypatch.delenv(ipc._OWNER_ENV)
        assert reader(request) == result


def test_captured_registry_callables_reject_requests_for_the_other_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _listener() as (_, address):
        monkeypatch.setenv(ipc._SOCKET_ENV, address)
        monkeypatch.setenv(ipc._OWNER_ENV, _owner())
        planner = ipc.capture_launcher_provider_prepare_planner()
        reader = ipc.capture_launcher_model_settings_reader()
        writer = ipc.model_save_from_environment()
        assert planner is not None
        assert reader is not None
        assert writer is not None
        save_request = _model_save_request(_Borrow(), _unexpected_marker)
        with pytest.raises(RuntimeError, match="request role is invalid"):
            planner.plan(_model_request(_Borrow()))
        with pytest.raises(RuntimeError, match="request role is invalid"):
            planner.plan(save_request)
        with pytest.raises(RuntimeError, match="request role is invalid"):
            reader(_Borrow())
        with pytest.raises(RuntimeError, match="request role is invalid"):
            reader(save_request)
        with pytest.raises(RuntimeError, match="request role is invalid"):
            writer(_model_request(_Borrow()))


@pytest.mark.parametrize("save", [False, True])
def test_model_settings_start_still_uses_the_16_kib_bound(
    monkeypatch: pytest.MonkeyPatch, save: bool,
) -> None:
    borrow = _Borrow()
    identity = vars(borrow.context) | {"profile_id": "p" * (16 * 1024)}
    borrow.context = SimpleNamespace(**identity)

    def serve(peer: socket.socket) -> None:
        assert peer.recv(1) == b""

    with (
        _served(monkeypatch, serve),
        pytest.raises(RuntimeError, match="request exceeds its bound"),
    ):
        if save:
            writer = ipc.model_save_from_environment()
            assert writer is not None
            writer(_model_save_request(borrow, _unexpected_marker))
        else:
            reader = ipc.capture_launcher_model_settings_reader()
            assert reader is not None
            reader(_model_request(borrow))


def test_model_settings_revocation_waits_for_cleanup_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _Borrow()

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        pending_check = _HEADER.pack(2, 0)
        peer.sendall(pending_check[:2])
        borrow.revoked.set()
        assert _receive(peer) == (9, b"")
        peer.sendall(pending_check[2:])
        _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with _served_model_settings(monkeypatch, serve) as reader:
        with pytest.raises(PermissionError, match="fixture lease revoked"):
            reader(_model_request(borrow))


def test_model_settings_result_above_one_mib_cancels_at_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        peer.sendall(_HEADER.pack(7, 1024 * 1024 + 1))
        assert _receive(peer) == (9, b"")
        assert peer.recv(1) == b""

    with _served_model_settings(monkeypatch, serve) as reader:
        with pytest.raises(RuntimeError, match="cleanup was unconfirmed"):
            reader(_model_request(_Borrow()))


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


def _conversation_get_request(
    borrow: _Borrow, caller: ipc.ConversationGetCaller = "shell",
) -> ipc.NativeConversationGetRequest:
    return ipc.NativeConversationGetRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        caller=caller,
        conversation_id="conversation-1",
    )


@contextmanager
def _served_conversation_get(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeConversationGetRequest], Mapping[str, Any]]]:
    with _served(monkeypatch, serve):
        reader = ipc.capture_launcher_conversation_get_reader()
        assert reader is not None
        yield reader


@pytest.mark.parametrize("caller", ["shell", "saved-turn", "saved-orchestration"])
def test_conversation_get_uses_closed_role_and_opaque_bounded_owner_json(
    monkeypatch: pytest.MonkeyPatch, caller: ipc.ConversationGetCaller,
) -> None:
    borrow = _Borrow()
    request = _conversation_get_request(borrow, caller)
    expected = {
        "conversation": {
            "id": request.conversation_id,
            "metadata": {"probability": 0.25, "huge_id": 1 << 140},
            "messages": [{"content": "会話", "metadata": {"linked": ["parent"]}}],
        },
    }
    response = json.dumps(expected, ensure_ascii=False).encode("utf-8")
    response = response.ljust(1024 * 1024, b" ")

    def serve(peer: socket.socket) -> None:
        tag, payload = _receive(peer)
        numerator, denominator = borrow.deadline_monotonic.as_integer_ratio()
        assert tag == 1
        assert json.loads(payload) == {
            "identity": {
                key: value for key, value in vars(borrow.context).items()
                if key != "security_epoch"
            },
            "security_epoch": 3,
            "runtime_instance": borrow.runtime_instance,
            "deadline_ns": numerator * 1_000_000_000 // denominator,
            "conversation_get": {
                "caller": caller,
                "conversation_id": request.conversation_id,
            },
        }
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, response)
        assert peer.recv(1) == b""

    with _served_conversation_get(monkeypatch, serve) as reader:
        assert reader(request) == expected


@pytest.mark.parametrize(
    ("live", "tag", "payload", "expected"),
    [
        (False, 10, b"", ipc.NativeProviderPrepareUnavailable),
        (False, 8, b"legacy-read-required", ipc._ProtocolError),
        (
            True, 8, b"legacy-read-required",
            ipc.NativeProviderRegistryRequiresLegacyRead,
        ),
        (True, 8, b"not-found", ipc.NativeConversationGetFailure),
        (True, 8, b"invalid", ipc.NativeConversationGetFailure),
        (True, 8, b"denied", ipc.NativeConversationGetFailure),
        (True, 8, b"internal", ipc._ProtocolError),
        (True, 10, b"", ipc._ProtocolError),
    ],
)
def test_conversation_get_distinguishes_authenticated_fallback_and_owner_failure(
    monkeypatch: pytest.MonkeyPatch, live: bool, tag: int, payload: bytes,
    expected: type[RuntimeError],
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if live:
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, tag, payload)
        if live and tag == 10:
            assert _receive(peer) == (9, b"")
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with (
        _served_conversation_get(monkeypatch, serve) as reader,
        pytest.raises(expected) as failure,
    ):
        reader(_conversation_get_request(_Borrow()))
    if isinstance(failure.value, ipc.NativeConversationGetFailure):
        assert failure.value.code == payload.decode("ascii")


@pytest.mark.parametrize(
    ("tag", "size", "payload"),
    [
        (7, 1024 * 1024 + 1, b""),
        (7, None, b'{"conversation":{"value":1e999}}'),
        (7, None, b'{"conversation":{"value":0,"value":1}}'),
        (7, None, b'{"conversation":[],"extra":true}'),
        (11, 0, b""),
    ],
)
def test_conversation_get_rejects_malformed_results_and_publication(
    monkeypatch: pytest.MonkeyPatch, tag: int, size: int | None, payload: bytes,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        length = len(payload) if size is None else size
        peer.sendall(_HEADER.pack(tag, length) + payload)
        assert _receive(peer) == (9, b"")
        if size is None:
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    message = "owner result is invalid" if size is None else "cleanup was unconfirmed"
    with (
        _served_conversation_get(monkeypatch, serve) as reader,
        pytest.raises(ipc._ProtocolError, match=message),
    ):
        reader(_conversation_get_request(_Borrow()))
