"""Exercise the original Host's synchronous macOS Registry connection."""

from __future__ import annotations

import json
import math
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
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core_runtime import native_provider_prepare_ipc as ipc
from core_runtime.host_provider_backend_v4 import HostPublicationUnconfirmed
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


@pytest.mark.parametrize(("split", "checks"), [(3, 1), (_HEADER.size + 3, 2)])
def test_receive_retains_partial_frame_when_lease_check_fails(
    split: int, checks: int,
) -> None:
    body = b"exact bytes"
    frame = _HEADER.pack(ipc._PLAN, len(body)) + body
    connection, launcher = socket.socketpair()
    with connection, launcher:
        exchange = ipc._Exchange(
            connection,
            len(body),
            permits_publication=False,
            conversation_mutation=False,
            requires_body_ready=False,
        )
        launcher.sendall(frame[:split])

        def assert_lease() -> float:
            nonlocal checks
            if not checks:
                raise PermissionError("fixture lease revoked")
            checks -= 1
            return 1.0

        with pytest.raises(PermissionError, match="fixture lease revoked"):
            exchange.receive(assert_lease)

        launcher.sendall(frame[split:])
        _send(launcher, ipc._CHECK)
        assert exchange.receive(lambda: 1.0) == (ipc._PLAN, body)
        assert exchange.receive(lambda: 1.0) == (ipc._CHECK, b"")


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


_TURN_REFERENCE = {
    "conversation_id": "conversation-1",
    "conversation_revision": 5,
    "user_message_id": "message:" + "c" * 64,
    "assistant_message_id": "message:" + "d" * 64,
    "outcome_digest": "sha256:" + "e" * 64,
}
_TURN_DIGEST = "sha256:" + "b" * 64
_SOURCE_DIGEST = "sha256:" + "c" * 64
_STARTED_TURN = {
    "id": "turn-1", "profile_id": "defaults", "request_id": "saved-turn." + "d" * 64,
    "conversation_id": "conversation-1", "conversation_revision": 3,
    "status": "queued", "revision": 1, "input_digest": _TURN_DIGEST,
    "guidance": [], "events": [{"name": "turn.queued", "wide": 1 << 128}],
}
_SETTLED_TURN = {
    "id": "turn-1", "profile_id": "defaults", "request_id": "saved-turn.fixture",
    "conversation_id": "conversation-1", "conversation_revision": 3,
    "status": "completed", "revision": 8, "input_digest": _TURN_DIGEST,
    "result_reference": _TURN_REFERENCE, "created_at": 100, "updated_at": 200,
    "guidance": [], "events": [{"name": "turn.completed", "wide": 1 << 128}],
}


def _turn_start_request(
    borrow: _Borrow,
    marker: Callable[[], None],
    action: ipc.SavedTurnStartAction = "claim",
) -> ipc.NativeSavedTurnStartRequest:
    return ipc.NativeSavedTurnStartRequest(
        context=borrow.context, runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic, assert_current=borrow.assert_current,
        action=action, turn_id="turn-1", conversation_id="conversation-1",
        conversation_revision=3, input_digest=_TURN_DIGEST, source_digest=_SOURCE_DIGEST,
        mark_publication_possible=marker,
    )


@contextmanager
def _served_turn_start(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeSavedTurnStartRequest], Mapping[str, Any]]]:
    with _served(monkeypatch, serve):
        monkeypatch.setenv(ipc._SAVED_TURN_START_ENV, "1")
        writer = ipc.capture_launcher_saved_turn_start_writer()
        assert writer is not None
        yield writer


def test_turn_start_capture_requires_managed_endpoint_and_exact_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ipc._SAVED_TURN_START_ENV, raising=False)
    monkeypatch.delenv(ipc._SOCKET_ENV, raising=False)
    monkeypatch.delenv(ipc._OWNER_ENV, raising=False)
    assert ipc.capture_launcher_saved_turn_start_writer() is None
    monkeypatch.setenv(ipc._SAVED_TURN_START_ENV, "1")
    assert ipc.capture_launcher_saved_turn_start_writer() is None
    monkeypatch.setenv(ipc._SOCKET_ENV, "/missing")
    with pytest.raises(ipc._ProtocolError, match="launch binding is invalid"):
        ipc.capture_launcher_saved_turn_start_writer()
    with _listener() as (_, address):
        monkeypatch.setenv(ipc._SOCKET_ENV, address)
        monkeypatch.setenv(ipc._OWNER_ENV, _owner())
        monkeypatch.setattr(ipc.os, "getppid", os.getpid)
        assert ipc.capture_launcher_saved_turn_start_writer() is not None
        for invalid_value in ("0", "true", " 1"):
            monkeypatch.setenv(ipc._SAVED_TURN_START_ENV, invalid_value)
            assert ipc.capture_launcher_saved_turn_start_writer() is None


@pytest.mark.parametrize("action", ["begin", "claim"])
@pytest.mark.parametrize("published", [False, True])
def test_turn_start_sends_exact_binding_and_preserves_raw_owner_row(
    monkeypatch: pytest.MonkeyPatch, action: ipc.SavedTurnStartAction, published: bool,
) -> None:
    borrow = _Borrow()
    marks: list[bool] = []
    guarded_after_marker = threading.Event()

    def guard() -> None:
        borrow.assert_current()
        if marks:
            guarded_after_marker.set()

    request = replace(
        _turn_start_request(borrow, lambda: marks.append(True), action),
        assert_current=guard,
    )
    status = "queued" if action == "begin" and published else "running"
    row = dict(_STARTED_TURN, status=status, details="会話")
    encoded = json.dumps(row, ensure_ascii=False).encode()
    row["details"] += "x" * (1024 * 1024 - len(encoded))
    encoded = json.dumps(row, ensure_ascii=False).encode()
    assert len(encoded) == 1024 * 1024
    claimed = action == "claim" and published
    response = b'{"claimed":' + json.dumps(claimed).encode() + b',"turn":' + encoded + b'}'

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
            "runtime_instance": borrow.runtime_instance,
            "deadline_ns": numerator * 1_000_000_000 // denominator,
            "turn_start": {
                "action": action, "turn_id": "turn-1", "conversation_id": "conversation-1",
                "conversation_revision": 3, "input_digest": _TURN_DIGEST,
                "source_digest": _SOURCE_DIGEST,
            },
        }
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        assert not marks
        if published:
            _send(peer, 11)
            assert _receive(peer) == (12, b"")
            assert marks == [True] and guarded_after_marker.is_set()
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, 7, response)
        assert peer.recv(1) == b""

    with _served_turn_start(monkeypatch, serve) as writer:
        monkeypatch.delenv(ipc._SAVED_TURN_START_ENV)
        assert writer(request) == {"claimed": claimed, "turn": row}
    assert marks == ([True] if published else [])


@pytest.mark.parametrize("change", [
    {"action": "reconcile"}, {"turn_id": "é"}, {"turn_id": "a" * 257},
    {"conversation_id": "a/b"}, {"conversation_revision": 0},
    {"conversation_revision": True}, {"conversation_revision": 1 << 53},
    {"input_digest": "sha256:" + "A" * 64}, {"source_digest": "absent"},
])
def test_turn_start_rejects_out_of_contract_fields_before_start(
    change: dict[str, Any],
) -> None:
    request = replace(_turn_start_request(_Borrow(), _unexpected_marker), **change)
    with pytest.raises(ipc._ProtocolError, match="saved Turn start inputs are invalid"):
        ipc._start_payload(request, 1)


@pytest.mark.parametrize("live, code, exception", [
    (False, b"legacy-read-required", ipc._ProtocolError),
    (True, b"legacy-read-required", ipc.NativeProviderRegistryRequiresLegacyRead),
    (True, b"conflict", ipc.NativeSavedTurnStartFailure),
    (True, b"invalid", ipc.NativeSavedTurnStartFailure),
    (True, b"not-found", ipc.NativeSavedTurnStartFailure),
    (True, b"unavailable", ipc.NativeSavedTurnStartFailure),
])
def test_turn_start_distinguishes_fallback_from_typed_failure(
    monkeypatch: pytest.MonkeyPatch, live: bool, code: bytes,
    exception: type[RuntimeError],
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if live:
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, 8, code)
        assert peer.recv(1) == b""

    with _served_turn_start(monkeypatch, serve) as writer, pytest.raises(exception) as exc:
        writer(_turn_start_request(_Borrow(), _unexpected_marker))
    if isinstance(exc.value, ipc.NativeSavedTurnStartFailure):
        assert exc.value.code == code.decode("ascii")


@pytest.mark.parametrize("action, permitted, claimed, exception", [
    ("claim", False, True, ipc._ProtocolError),
    ("begin", True, True, HostPublicationUnconfirmed),
    ("claim", True, False, HostPublicationUnconfirmed),
])
def test_turn_start_rejects_claimed_results_that_mismatch_the_action_or_permit(
    monkeypatch: pytest.MonkeyPatch, action: ipc.SavedTurnStartAction, permitted: bool,
    claimed: bool, exception: type[RuntimeError],
) -> None:
    marks: list[bool] = []
    row = dict(_STARTED_TURN, status="running" if action == "claim" else "queued")
    payload = json.dumps({"claimed": claimed, "turn": row}).encode()

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        if permitted:
            _send(peer, 11)
            assert _receive(peer) == (12, b"")
        _send(peer, 7, payload)
        assert _receive(peer) == (9, b"")
        _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with _served_turn_start(monkeypatch, serve) as writer, pytest.raises(exception):
        writer(_turn_start_request(_Borrow(), lambda: marks.append(True), action))
    assert marks == ([True] if permitted else [])


@pytest.mark.parametrize("code", [b"legacy-read-required", b"unavailable", None])
def test_turn_start_never_retries_after_permission_when_the_reply_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, code: bytes | None,
) -> None:
    marked = threading.Event()

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 11)
        assert _receive(peer) == (12, b"")
        if code is not None:
            _send(peer, 8, code)
            assert peer.recv(1) == b""

    with (
        _served_turn_start(monkeypatch, serve) as writer,
        pytest.raises(HostPublicationUnconfirmed),
    ):
        writer(_turn_start_request(_Borrow(), marked.set))
    assert marked.is_set()


def test_turn_start_rejects_an_over_limit_reply_before_reading_its_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        peer.sendall(_HEADER.pack(7, 1024 * 1024 + 129))
        assert _receive(peer) == (9, b"")

    with (
        _served_turn_start(monkeypatch, serve) as writer,
        pytest.raises(ipc._ProtocolError, match="cleanup was unconfirmed"),
    ):
        writer(_turn_start_request(_Borrow(), _unexpected_marker))


@pytest.mark.parametrize("change", [
    {"id": "other"}, {"profile_id": "other"}, {"conversation_id": "other"},
    {"conversation_revision": 3.0}, {"input_digest": _SOURCE_DIGEST},
])
def test_turn_start_checks_owner_row_binding_without_normalizing(
    change: dict[str, Any],
) -> None:
    request = _turn_start_request(_Borrow(), _unexpected_marker, "begin")
    row = dict(_STARTED_TURN, **change)
    payload = json.dumps({"claimed": False, "turn": row}).encode()
    with pytest.raises(ipc._ProtocolError, match="owner result is invalid"):
        ipc._decode_turn_start(payload, request, publication_possible=False)


def _settlement_request(
    borrow: _Borrow, marker: Callable[[], None],
) -> ipc.NativeSavedTurnSettlementRequest:
    return ipc.NativeSavedTurnSettlementRequest(
        context=borrow.context, runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic, assert_current=borrow.assert_current,
        caller="saved-turn", turn_id="turn-1", input_digest=_TURN_DIGEST,
        expected_revision=None, result_reference=_TURN_REFERENCE,
        mark_publication_possible=marker,
    )


@contextmanager
def _served_turn_settlement(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeSavedTurnSettlementRequest], Mapping[str, Any]]]:
    with _served(monkeypatch, serve):
        writer = ipc.capture_launcher_saved_turn_settlement_writer()
        assert writer is not None
        yield writer


@pytest.mark.parametrize("replay", [False, True])
def test_turn_settlement_sends_only_the_bound_reference_and_preserves_the_owner_row(
    monkeypatch: pytest.MonkeyPatch, replay: bool,
) -> None:
    borrow = _Borrow()
    marked = threading.Event()
    checked = threading.Event()

    def guard() -> None:
        borrow.assert_current()
        if marked.is_set():
            checked.set()

    request = replace(_settlement_request(borrow, marked.set), assert_current=guard)
    if replay:
        request = replace(request, caller="saved-reconcile", expected_revision=7)
    owner_row = dict(_SETTLED_TURN, details="")
    row_bytes = json.dumps(owner_row).encode()
    owner_row["details"] = "x" * (1024 * 1024 - len(row_bytes))
    row_bytes = json.dumps(owner_row).encode()
    assert len(row_bytes) == 1024 * 1024

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
            "turn_settlement": {
                "caller": request.caller, "turn_id": "turn-1",
                "input_digest": _TURN_DIGEST, "expected_revision": request.expected_revision,
                "result_reference": json.dumps(_TURN_REFERENCE, separators=(",", ":")),
            },
        }
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        assert not marked.is_set()
        if not replay:
            _send(peer, 11)
            assert _receive(peer) == (12, b"")
            assert marked.is_set() and checked.is_set()
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, 7, row_bytes)
        assert peer.recv(1) == b""

    with _served_turn_settlement(monkeypatch, serve) as writer:
        assert writer(request) == owner_row
    assert marked.is_set() is (not replay)


@pytest.mark.parametrize("change", [
    {"caller": "shell"}, {"caller": "saved-reconcile"},
    {"turn_id": "a" * 257}, {"turn_id": "été"}, {"turn_id": "a/b"},
    {"input_digest": "sha256:" + "A" * 64},
    {"expected_revision": True}, {"expected_revision": 0},
    {"expected_revision": 1 << 64},
    {"result_reference": {"non-finite": math.inf}},
    {"result_reference": {"payload": "é" * 1024}},
])
def test_turn_settlement_rejects_invalid_inputs_before_start(change: dict[str, Any]) -> None:
    request = replace(_settlement_request(_Borrow(), _unexpected_marker), **change)
    with pytest.raises(ipc._ProtocolError):
        ipc._start_payload(request, 1)


@pytest.mark.parametrize("live, tag, body, error_type", [
    (False, 10, b"", ipc.NativeProviderPrepareUnavailable),
    (False, 8, b"legacy-read-required", ipc._ProtocolError),
    (True, 8, b"legacy-read-required", ipc.NativeProviderRegistryRequiresLegacyRead),
    (True, 8, b"conflict", ipc.NativeSavedTurnSettlementFailure),
    (True, 8, b"unavailable", ipc.NativeSavedTurnSettlementFailure),
])
def test_turn_settlement_distinguishes_only_explicit_fallback_from_failure(
    monkeypatch: pytest.MonkeyPatch, live: bool, tag: int, body: bytes,
    error_type: type[RuntimeError],
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if live:
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, tag, body)
        assert peer.recv(1) == b""

    with _served_turn_settlement(monkeypatch, serve) as writer, pytest.raises(error_type) as exc:
        writer(_settlement_request(_Borrow(), _unexpected_marker))
    if isinstance(exc.value, ipc.NativeSavedTurnSettlementFailure):
        assert exc.value.code == body.decode()


@pytest.mark.parametrize("terminal", [
    None, (8, b"legacy-read-required"), (8, b"conflict"), (11, b""),
    (7, json.dumps(dict(_SETTLED_TURN, id="another-turn")).encode()),
    (7, json.dumps({"turn": _SETTLED_TURN}).encode()),
])
def test_turn_settlement_never_retries_after_possible_publication(
    monkeypatch: pytest.MonkeyPatch, terminal: tuple[int, bytes] | None,
) -> None:
    marks = []

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 11)
        assert _receive(peer) == (12, b"")
        if terminal is not None:
            _send(peer, *terminal)
            if terminal[0] != 8:
                assert _receive(peer) == (9, b"")
                if terminal[0] == 11:
                    return  # Closing without acknowledgement leaves cleanup unconfirmed.
                _send(peer, 8, b"cancelled")
            assert peer.recv(1) == b""

    with (
        _served_turn_settlement(monkeypatch, serve) as writer,
        pytest.raises(ipc.NativeSavedTurnSettlementPublicationUnconfirmed),
    ):
        writer(_settlement_request(_Borrow(), lambda: marks.append(True)))
    assert marks == [True]


_CONFIRMATION_TERMINAL = {
    key: _SETTLED_TURN[key]
    for key in (
        "id", "profile_id", "conversation_id", "status", "revision",
        "conversation_revision", "input_digest", "result_reference",
    )
} | {"request_id": "saved-turn." + "a" * 64}
_CONFIRMATION_RESULT = {
    "confirmed": True,
    "conversation_id": "conversation-1",
    "conversation_revision": 8,
    "store_revision": 12,
}


def _confirmation_request(
    borrow: _Borrow,
    marker: Callable[[], None],
    caller: ipc.SavedReceiptCaller = "saved-turn",
) -> ipc.NativeConversationConfirmationRequest:
    return ipc.NativeConversationConfirmationRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        caller=caller,
        terminal=_CONFIRMATION_TERMINAL,
        mark_publication_possible=marker,
    )


@contextmanager
def _served_confirmation(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeConversationConfirmationRequest], Mapping[str, Any]]]:
    with _served(monkeypatch, serve):
        writer = ipc.capture_launcher_conversation_confirmation_writer()
        assert writer is not None
        yield writer


@pytest.mark.parametrize("caller", ["saved-turn", "saved-reconcile"])
@pytest.mark.parametrize("confirmed, publish", [(True, True), (True, False), (False, False)])
def test_conversation_confirmation_binds_terminal_and_permits_only_publication(
    monkeypatch: pytest.MonkeyPatch,
    caller: ipc.SavedReceiptCaller,
    confirmed: bool,
    publish: bool,
) -> None:
    borrow = _Borrow()
    marked = threading.Event()
    checked = threading.Event()
    expected = dict(_CONFIRMATION_RESULT, confirmed=confirmed)

    def guard() -> None:
        borrow.assert_current()
        if marked.is_set():
            checked.set()

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
            "conversation_confirmation": {
                "caller": caller,
                "terminal": _CONFIRMATION_TERMINAL,
            },
        }
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        assert not marked.is_set()
        if publish:
            _send(peer, 11)
            assert _receive(peer) == (12, b"")
            assert marked.is_set() and checked.is_set()
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, 7, json.dumps(expected).encode().ljust(16 * 1024, b" "))
        assert peer.recv(1) == b""

    request = replace(_confirmation_request(borrow, marked.set, caller), assert_current=guard)
    with _served_confirmation(monkeypatch, serve) as writer:
        monkeypatch.delenv(ipc._OWNER_ENV)
        assert writer(request) == expected
    assert marked.is_set() is publish


@pytest.mark.parametrize("terminal_change", [
    {"events": []}, {"id": "a/b"}, {"profile_id": "wrong-profile"},
    {"conversation_id": "x" * 257}, {"request_id": "saved-turn.foo"},
    {"status": "running"}, {"revision": True}, {"revision": 2},
    {"conversation_revision": 1 << 64}, {"input_digest": "sha256:" + "A" * 64},
    {"result_reference": {}}, {"result_reference": dict(_TURN_REFERENCE, payload="text")},
    {"result_reference": dict(_TURN_REFERENCE, outcome_digest=math.inf)},
    {"result_reference": dict(_TURN_REFERENCE, conversation_revision=4)},
])
def test_conversation_confirmation_rejects_invalid_terminal_before_start(
    terminal_change: dict[str, Any],
) -> None:
    request = replace(
        _confirmation_request(_Borrow(), _unexpected_marker),
        terminal=_CONFIRMATION_TERMINAL | terminal_change,
    )
    with pytest.raises(ipc._ProtocolError, match="inputs are invalid"):
        ipc._start_payload(request, 1)


def test_conversation_confirmation_restricts_caller_and_entire_start_frame() -> None:
    borrow = _Borrow()
    request = _confirmation_request(borrow, _unexpected_marker)
    with pytest.raises(ipc._ProtocolError, match="inputs are invalid"):
        ipc._start_payload(replace(request, caller="saved-orchestration"), 1)
    context = SimpleNamespace(**vars(borrow.context) | {"activation_id": "雪" * 8192})
    with pytest.raises(ipc._ProtocolError, match="request exceeds its bound"):
        ipc._start_payload(replace(request, context=context), 1)


@pytest.mark.parametrize("live, tag, body, error_type", [
    (False, 10, b"", ipc.NativeProviderPrepareUnavailable),
    (False, 8, b"requires-legacy-read", ipc._ProtocolError),
    (True, 8, b"requires-legacy-read", ipc.NativeProviderRegistryRequiresLegacyRead),
    (True, 8, b"legacy-read-required", ipc._ProtocolError),
    (False, 8, b"unavailable", ipc.NativeConversationConfirmationFailure),
    (True, 8, b"denied", ipc.NativeConversationConfirmationFailure),
    (True, 8, b"publication-unconfirmed", ipc._ProtocolError),
])
def test_conversation_confirmation_classifies_prepermit_failures(
    monkeypatch: pytest.MonkeyPatch, live: bool, tag: int, body: bytes,
    error_type: type[RuntimeError],
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if live:
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, tag, body)
        if body == b"legacy-read-required":
            assert _receive(peer) == (9, b"")
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with (
        _served_confirmation(monkeypatch, serve) as writer,
        pytest.raises(error_type) as failure,
    ):
        writer(_confirmation_request(_Borrow(), _unexpected_marker))
    if isinstance(failure.value, ipc.NativeConversationConfirmationFailure):
        assert failure.value.code == body.decode("ascii")


@pytest.mark.parametrize("terminal", [
    None, (8, b"publication-unconfirmed"), (8, b"requires-legacy-read"),
    (11, b""), (7, json.dumps(dict(_CONFIRMATION_RESULT, confirmed=False)).encode()),
])
def test_conversation_confirmation_requires_proof_after_permit(
    monkeypatch: pytest.MonkeyPatch, terminal: tuple[int, bytes] | None,
) -> None:
    marks: list[bool] = []

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 11)
        assert _receive(peer) == (12, b"")
        if terminal is not None:
            _send(peer, *terminal)
            if terminal[0] != 8:
                assert _receive(peer) == (9, b"")
                _send(peer, 8, b"cancelled")
            assert peer.recv(1) == b""

    with (
        _served_confirmation(monkeypatch, serve) as writer,
        pytest.raises(ipc.NativeConversationConfirmationPublicationUnconfirmed),
    ):
        writer(_confirmation_request(_Borrow(), lambda: marks.append(True)))
    assert marks == [True]


def test_conversation_confirmation_rechecks_guard_after_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _Borrow()

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 11)
        assert _receive(peer) == (9, b"")
        _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with (
        _served_confirmation(monkeypatch, serve) as writer,
        pytest.raises(ipc.NativeConversationConfirmationPublicationUnconfirmed),
    ):
        writer(_confirmation_request(borrow, borrow.revoked.set))


@pytest.mark.parametrize("change", [
    {"extra": False}, {"confirmed": 1}, {"conversation_id": "another"},
    {"conversation_revision": 0}, {"conversation_revision": 8.0},
    {"store_revision": -1}, {"store_revision": True}, {"store_revision": 1 << 64},
])
def test_conversation_confirmation_rejects_invalid_owner_result(
    change: dict[str, Any],
) -> None:
    request = _confirmation_request(_Borrow(), _unexpected_marker)
    result = json.dumps(_CONFIRMATION_RESULT | change).encode()
    with pytest.raises(ipc._ProtocolError, match="owner result is invalid"):
        ipc._decode_conversation_confirmation(result, request, publication_possible=False)


_APPEND_MESSAGE = {
    "id": "message:" + "c" * 64,
    "role": "assistant",
    "content": "応答",
    "parent_id": "message:" + "d" * 64,
}
_APPEND_REQUEST = {
    "conversation_id": "fixture-conversation",
    "expected_conversation_revision": 4,
    "message": _APPEND_MESSAGE,
    "saved_input": {"request": {
        "turn_id": "fixture-turn",
        "conversation_id": "fixture-conversation",
        "conversation_revision": 3,
        "content": "入力",
    }},
}
_APPEND_RESULT = {
    "action": "message_appended",
    "message": _APPEND_MESSAGE | {"sequence_number": 3},
    "conversation_revision": 5,
    "store_revision": 25,
}


def _append_request(
    borrow: _Borrow, marker: Callable[[], None],
) -> ipc.NativeConversationAppendRequest:
    return ipc.NativeConversationAppendRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        request=_APPEND_REQUEST,
        mark_publication_possible=marker,
    )


@contextmanager
def _served_append(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeConversationAppendRequest], Mapping[str, Any]]]:
    monkeypatch.setenv(ipc._APPEND_ENV, "1")
    with _served(monkeypatch, serve):
        writer = ipc.capture_launcher_conversation_append_writer()
        assert writer is not None
        yield writer


@pytest.mark.parametrize("start_hint", [None, "", "0", "true"])
def test_conversation_append_requires_exact_version_hint(
    monkeypatch: pytest.MonkeyPatch, start_hint: str | None,
) -> None:
    if start_hint is None:
        monkeypatch.delenv(ipc._APPEND_ENV, raising=False)
    else:
        monkeypatch.setenv(ipc._APPEND_ENV, start_hint)
    monkeypatch.setenv(ipc._SOCKET_ENV, "invalid-endpoint")
    assert ipc.capture_launcher_conversation_append_writer() is None


def test_conversation_append_binds_full_start_and_permits_one_mib_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _Borrow()
    marked = threading.Event()
    checked = threading.Event()
    one_mib = 1024 * 1024

    def guard() -> None:
        borrow.assert_current()
        if marked.is_set():
            checked.set()

    numerator, denominator = borrow.deadline_monotonic.as_integer_ratio()
    deadline_ns = numerator * 1_000_000_000 // denominator
    base = replace(_append_request(borrow, marked.set), assert_current=guard)
    fill = one_mib - len(ipc._start_payload(base, deadline_ns))
    content = _APPEND_MESSAGE["content"] + "雪" * (fill // 3) + "a" * (fill % 3)
    message = _APPEND_MESSAGE | {"content": content}
    request = replace(base, request=_APPEND_REQUEST | {"message": message})
    result = _APPEND_RESULT | {
        "message": _APPEND_RESULT["message"] | {"content": content},
    }
    owner_result = json.dumps(result, ensure_ascii=False).encode().ljust(one_mib, b" ")
    assert len(owner_result) == one_mib

    def serve(peer: socket.socket) -> None:
        tag, payload = _receive(peer)
        assert tag == 13
        assert len(payload) == one_mib
        assert json.loads(payload) == {
            "identity": {
                key: value for key, value in vars(borrow.context).items()
                if key != "security_epoch"
            },
            "security_epoch": 3,
            "runtime_instance": borrow.runtime_instance,
            "deadline_ns": deadline_ns,
            "conversation_append": {"request": request.request},
        }
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        assert not marked.is_set()
        _send(peer, 11)
        assert _receive(peer) == (12, b"")
        assert marked.is_set() and checked.is_set()
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, owner_result)
        assert peer.recv(1) == b""

    with _served_append(monkeypatch, serve) as writer:
        monkeypatch.delenv(ipc._OWNER_ENV)
        monkeypatch.delenv(ipc._APPEND_ENV)
        assert writer(request) == result


def test_conversation_append_oversize_falls_back_before_sending_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _Borrow()
    context = SimpleNamespace(**(vars(borrow.context) | {"activation_id": "雪" * 4096}))
    base = replace(_append_request(borrow, _unexpected_marker), context=context)
    numerator, denominator = borrow.deadline_monotonic.as_integer_ratio()
    deadline_ns = numerator * 1_000_000_000 // denominator
    fill = 1024 * 1024 - len(ipc._start_payload(base, deadline_ns)) + 1
    message = _APPEND_MESSAGE | {"content": _APPEND_MESSAGE["content"] + "x" * fill}
    request = replace(base, request=_APPEND_REQUEST | {"message": message})

    def serve(peer: socket.socket) -> None:
        assert peer.recv(1) == b""

    with (
        _served_append(monkeypatch, serve) as writer,
        pytest.raises(ipc.NativeProviderRegistryRequiresLegacyRead, match="request bound"),
    ):
        writer(request)


@pytest.mark.parametrize("change", [
    {"operation": "append_saved"},
    {"conversation_id": "invalid/id"},
    {"expected_conversation_revision": True},
    {"message": {"id": "other"}},
    {"saved_input": None},
    {"saved_input": {"request": {"conversation_id": "different", "turn_id": "turn"}}},
])
def test_conversation_append_rejects_invalid_inputs_before_start(
    change: dict[str, Any],
) -> None:
    request = replace(
        _append_request(_Borrow(), _unexpected_marker),
        request=_APPEND_REQUEST | change,
    )
    with pytest.raises(ipc._ProtocolError, match="inputs are invalid"):
        ipc._start_payload(request, 1)


@pytest.mark.parametrize("live, tag, body, error_type", [
    (False, 10, b"", ipc.NativeProviderPrepareUnavailable),
    (False, 8, b"requires-legacy-read", ipc._ProtocolError),
    (False, 8, b"unavailable", ipc.NativeConversationAppendFailure),
    (True, 8, b"requires-legacy-read", ipc.NativeProviderRegistryRequiresLegacyRead),
    (True, 8, b"legacy-read-required", ipc._ProtocolError),
    (True, 8, b"conflict", ipc.NativeConversationAppendFailure),
    (True, 8, b"invalid", ipc.NativeConversationAppendFailure),
    (True, 8, b"not-found", ipc.NativeConversationAppendFailure),
    (True, 8, b"publication-unconfirmed", ipc._ProtocolError),
    (True, 7, json.dumps(_APPEND_RESULT).encode(), ipc._ProtocolError),
])
def test_conversation_append_classifies_failures_before_permit(
    monkeypatch: pytest.MonkeyPatch,
    live: bool,
    tag: int,
    body: bytes,
    error_type: type[RuntimeError],
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 13
        if live:
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, tag, body)
        if tag == 7 or body == b"legacy-read-required":
            assert _receive(peer) == (9, b"")
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with (
        _served_append(monkeypatch, serve) as writer,
        pytest.raises(error_type) as failure,
    ):
        writer(_append_request(_Borrow(), _unexpected_marker))
    if isinstance(failure.value, ipc.NativeConversationAppendFailure):
        assert failure.value.code == body.decode("ascii")


@pytest.mark.parametrize("terminal", [
    None,
    (8, b"requires-legacy-read"),
    (8, b"publication-unconfirmed"),
    (8, b"conflict"),
    (11, b""),
    (7, json.dumps(_APPEND_RESULT | {"conversation_revision": 4}).encode()),
])
def test_conversation_append_is_unconfirmed_after_permit_without_bound_result(
    monkeypatch: pytest.MonkeyPatch, terminal: tuple[int, bytes] | None,
) -> None:
    marks: list[bool] = []

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 13
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 11)
        assert _receive(peer) == (12, b"")
        if terminal is not None:
            _send(peer, *terminal)
            if terminal[0] != 8:
                assert _receive(peer) == (9, b"")
                _send(peer, 8, b"cancelled")
            assert peer.recv(1) == b""

    with (
        _served_append(monkeypatch, serve) as writer,
        pytest.raises(ipc.NativeConversationAppendPublicationUnconfirmed),
    ):
        writer(_append_request(_Borrow(), lambda: marks.append(True)))
    assert marks == [True]


def test_conversation_append_checks_guard_after_marker_before_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _Borrow()

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 13
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 11)
        assert _receive(peer) == (9, b"")
        _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with (
        _served_append(monkeypatch, serve) as writer,
        pytest.raises(ipc.NativeConversationAppendPublicationUnconfirmed),
    ):
        writer(_append_request(borrow, borrow.revoked.set))


def test_conversation_append_above_one_mib_result_is_unconfirmed_at_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marked = threading.Event()

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 13
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 11)
        assert _receive(peer) == (12, b"")
        peer.sendall(_HEADER.pack(7, 1024 * 1024 + 1))
        assert _receive(peer) == (9, b"")
        assert peer.recv(1) == b""

    with (
        _served_append(monkeypatch, serve) as writer,
        pytest.raises(ipc.NativeConversationAppendPublicationUnconfirmed),
    ):
        writer(_append_request(_Borrow(), marked.set))
    assert marked.is_set()


@pytest.mark.parametrize("change", [
    {"extra": None},
    {"action": "saved"},
    {"message": _APPEND_MESSAGE | {"id": "message:" + "e" * 64}},
    {"message": _APPEND_MESSAGE | {"role": "user"}},
    {"message": _APPEND_MESSAGE | {"content": "different"}},
    {"conversation_revision": 5.0},
    {"conversation_revision": 6},
    {"store_revision": True},
    {"store_revision": 0},
    {"store_revision": 1 << 64},
])
def test_conversation_append_rejects_unbound_owner_result(change: dict[str, Any]) -> None:
    payload = json.dumps(_APPEND_RESULT | change).encode()
    request = _append_request(_Borrow(), _unexpected_marker)
    with pytest.raises(ipc._ProtocolError, match="owner result is invalid"):
        ipc._decode_conversation_append(payload, request)


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


@pytest.mark.parametrize(
    "operation", [
        "provider", "model-resolve", "saved-receipt", "turn-read", "turn-settlement",
        "conversation-confirmation",
    ],
)
def test_real_child_rejects_socket_owned_by_a_different_same_uid_process(
    operation: str,
) -> None:
    runtime = Path(__file__).resolve().parents[1]
    child_code = """
import sys
import time
from types import SimpleNamespace
from core_runtime import native_provider_prepare_ipc
borrow = SimpleNamespace(
    deadline_monotonic=time.monotonic() + 4, assert_current=lambda: None,
)
if sys.argv[1] == "model-resolve":
    invoke = native_provider_prepare_ipc.capture_launcher_model_resolve_reader()
    borrow = native_provider_prepare_ipc.NativeModelResolveRequest(
        None, "a" * 32, borrow.deadline_monotonic, borrow.assert_current,
        "generate", "fixture",
    )
elif sys.argv[1] == "saved-receipt":
    invoke = native_provider_prepare_ipc.capture_launcher_saved_receipt_reader()
    borrow = native_provider_prepare_ipc.NativeSavedReceiptRequest(
        None, "a" * 32, borrow.deadline_monotonic, borrow.assert_current,
        "saved-turn", "fixture",
    )
elif sys.argv[1] == "turn-read":
    invoke = native_provider_prepare_ipc.capture_launcher_saved_turn_reader()
    borrow = native_provider_prepare_ipc.NativeSavedTurnReadRequest(
        None, "a" * 32, borrow.deadline_monotonic, borrow.assert_current,
        "saved-reconcile", "fixture",
    )
elif sys.argv[1] == "turn-settlement":
    invoke = native_provider_prepare_ipc.capture_launcher_saved_turn_settlement_writer()
    borrow = native_provider_prepare_ipc.NativeSavedTurnSettlementRequest(
        None, "a" * 32, borrow.deadline_monotonic, borrow.assert_current,
        "saved-turn", "fixture", "sha256:" + "b" * 64, None, {}, lambda: None,
    )
elif sys.argv[1] == "conversation-confirmation":
    invoke = native_provider_prepare_ipc.capture_launcher_conversation_confirmation_writer()
    borrow = native_provider_prepare_ipc.NativeConversationConfirmationRequest(
        context=None, runtime_instance="a" * 32,
        deadline_monotonic=borrow.deadline_monotonic, assert_current=borrow.assert_current,
        caller="saved-turn", terminal={}, mark_publication_possible=lambda: None,
    )
else:
    planner = native_provider_prepare_ipc.capture_launcher_provider_prepare_planner()
    invoke = planner.plan
try:
    invoke(borrow)
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
        environment = os.environ | {
            ipc._SOCKET_ENV: address, ipc._OWNER_ENV: _owner(),
            ipc._SAVED_TURN_READ_ENV: "1",
        }
        with subprocess.Popen(
            [sys.executable, "-B", "-c", server_code, address],
            stdout=subprocess.PIPE,
        ) as server:
            try:
                assert server.stdout is not None
                assert server.stdout.readline() == b"ready\n"
                subprocess.run(
                    [sys.executable, "-B", "-c", child_code, operation],
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


def _saved_receipt_request(
    borrow: _Borrow,
    caller: ipc.SavedReceiptCaller = "saved-turn",
    turn_id: str = "turn-1",
) -> ipc.NativeSavedReceiptRequest:
    return ipc.NativeSavedReceiptRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        caller=caller,
        turn_id=turn_id,
    )


@contextmanager
def _served_saved_receipt(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeSavedReceiptRequest], Mapping[str, Any]]]:
    with _served(monkeypatch, serve):
        reader = ipc.capture_launcher_saved_receipt_reader()
        assert reader is not None
        yield reader


@pytest.mark.parametrize(
    ("caller", "turn_id", "receipt"),
    [
        ("saved-turn", "", None),
        ("saved-reconcile", " ../もの \n ", {}),
        (
            "saved-turn", "\0" * ipc.SAVED_RECEIPT_MAX_ID_CHARS,
            {"extra": ["会話", {"zero": -0.0, "wide": 1 << 145}]},
        ),
    ],
)
def test_saved_receipt_preserves_raw_turn_id_and_original_owner_values(
    monkeypatch: pytest.MonkeyPatch,
    caller: ipc.SavedReceiptCaller,
    turn_id: str,
    receipt: dict[str, Any] | None,
) -> None:
    borrow = _Borrow()
    request = _saved_receipt_request(borrow, caller, turn_id)
    expected = {"receipt": receipt}
    response = json.dumps(expected, ensure_ascii=False).encode("utf-8")

    def serve(peer: socket.socket) -> None:
        tag, payload = _receive(peer)
        numerator, denominator = borrow.deadline_monotonic.as_integer_ratio()
        assert tag == 1
        assert len(payload) <= 16 * 1024
        assert json.loads(payload) == {
            "identity": {
                key: value for key, value in vars(borrow.context).items()
                if key != "security_epoch"
            },
            "security_epoch": 3,
            "runtime_instance": borrow.runtime_instance,
            "deadline_ns": numerator * 1_000_000_000 // denominator,
            "conversation_receipt": {"caller": caller, "turn_id": turn_id},
        }
        for _ in range(2):
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, 7, response.ljust(1024 * 1024, b" "))
        assert peer.recv(1) == b""

    with _served_saved_receipt(monkeypatch, serve) as reader:
        monkeypatch.setenv(ipc._SOCKET_ENV, "/invalidated-after-capture")
        monkeypatch.delenv(ipc._OWNER_ENV)
        actual = reader(request)
        assert actual == expected
        assert json.dumps(actual, ensure_ascii=False).encode("utf-8") == response


@pytest.mark.parametrize(
    ("caller", "turn_id"),
    [
        ("shell", "turn-1"),
        ("saved-orchestration", "turn-1"),
        ("saved-turn", "a" * (ipc.SAVED_RECEIPT_MAX_ID_CHARS + 1)),
        ("saved-reconcile", 42),
    ],
)
def test_saved_receipt_rejects_open_callers_and_unbounded_inputs(
    monkeypatch: pytest.MonkeyPatch, caller: ipc.SavedReceiptCaller, turn_id: str,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert peer.recv(1) == b""

    with (
        _served_saved_receipt(monkeypatch, serve) as reader,
        pytest.raises(ipc._ProtocolError, match="Saved Receipt lookup inputs are invalid"),
    ):
        reader(_saved_receipt_request(_Borrow(), caller, turn_id))


def test_saved_receipt_reader_requires_the_correct_request_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _listener() as (_, address):
        monkeypatch.setenv(ipc._SOCKET_ENV, address)
        monkeypatch.setenv(ipc._OWNER_ENV, _owner())
        reader = ipc.capture_launcher_saved_receipt_reader()
        planner = ipc.capture_launcher_provider_prepare_planner()
        assert reader is not None
        assert planner is not None
        with pytest.raises(ipc._ProtocolError, match="request role is invalid"):
            reader(_conversation_get_request(_Borrow()))
        with pytest.raises(ipc._ProtocolError, match="request role is invalid"):
            planner.plan(_saved_receipt_request(_Borrow()))


@pytest.mark.parametrize(
    ("live", "tag", "payload", "expected"),
    [
        (False, 7, b'{"receipt":null}', ipc._ProtocolError),
        (False, 10, b"", ipc.NativeProviderPrepareUnavailable),
        (False, 8, b"legacy-read-required", ipc._ProtocolError),
        (False, 8, b"not-found", ipc._ProtocolError),
        (True, 8, b"legacy-read-required", ipc.NativeProviderRegistryRequiresLegacyRead),
        (True, 8, b"not-found", ipc.NativeSavedReceiptFailure),
        (True, 8, b"invalid", ipc.NativeSavedReceiptFailure),
        (True, 8, b"denied", ipc.NativeSavedReceiptFailure),
        (True, 8, b"internal", ipc._ProtocolError),
        (True, 10, b"", ipc._ProtocolError),
    ],
)
def test_saved_receipt_distinguishes_compatibility_and_owner_failure(
    monkeypatch: pytest.MonkeyPatch, live: bool, tag: int, payload: bytes,
    expected: type[RuntimeError],
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if live:
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, tag, payload)
        if (tag == 7 and not live) or (tag == 10 and live):
            assert _receive(peer) == (9, b"")
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with (
        _served_saved_receipt(monkeypatch, serve) as reader,
        pytest.raises(expected) as failure,
    ):
        reader(_saved_receipt_request(_Borrow()))
    if isinstance(failure.value, ipc.NativeSavedReceiptFailure):
        assert failure.value.code == payload.decode("ascii")


@pytest.mark.parametrize(
    ("tag", "size", "payload"),
    [
        (7, 1024 * 1024 + 1, b""),
        (7, None, b'{"receipt":{"bad_utf8":"\xff"}}'),
        (7, None, b'{"receipt":{"non_finite":1e999}}'),
        (7, None, b'{"receipt":{"duplicate":0,"duplicate":1}}'),
        (7, None, b'{"receipt":null,"extra":true}'),
        (7, None, b'{"receipt":[]}'),
        (7, None, b'{"receipt":false}'),
        (7, None, b'{"owner_result":"{}"}'),
        (11, 0, b""),
    ],
)
def test_saved_receipt_rejects_malformed_results_and_publication(
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
        _served_saved_receipt(monkeypatch, serve) as reader,
        pytest.raises(ipc._ProtocolError, match=message),
    ):
        reader(_saved_receipt_request(_Borrow()))


def _saved_turn_read_request(
    borrow: _Borrow,
    caller: ipc.SavedReceiptCaller = "saved-turn",
    turn_id: str = "turn-1",
) -> ipc.NativeSavedTurnReadRequest:
    return ipc.NativeSavedTurnReadRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        caller=caller,
        turn_id=turn_id,
    )


@contextmanager
def _served_saved_turn_read(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeSavedTurnReadRequest], dict[str, Any] | None]]:
    monkeypatch.setenv(ipc._SAVED_TURN_READ_ENV, "1")
    with _served(monkeypatch, serve):
        reader = ipc.capture_launcher_saved_turn_reader()
        assert reader is not None
        yield reader


def test_saved_turn_read_capture_requires_managed_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ipc._SOCKET_ENV, raising=False)
    monkeypatch.delenv(ipc._OWNER_ENV, raising=False)
    monkeypatch.setenv(ipc._SAVED_TURN_READ_ENV, "1")
    assert ipc.capture_launcher_saved_turn_reader() is None
    monkeypatch.setenv(ipc._SOCKET_ENV, "/fixture.sock")
    monkeypatch.setenv(ipc._OWNER_ENV, _owner())
    monkeypatch.setenv(ipc._SAVED_TURN_READ_ENV, "true")
    assert ipc.capture_launcher_saved_turn_reader() is None
    monkeypatch.delenv(ipc._SAVED_TURN_READ_ENV)
    assert ipc.capture_launcher_saved_turn_reader() is None
    monkeypatch.setenv(ipc._SAVED_TURN_READ_ENV, "1")
    reader = ipc.capture_launcher_saved_turn_reader()
    assert reader is not None
    with pytest.raises(ipc._ProtocolError, match="request role is invalid"):
        reader(_saved_receipt_request(_Borrow()))


@pytest.mark.parametrize(
    ("caller", "turn_id", "present"),
    [
        ("saved-turn", "absent", False),
        ("saved-reconcile", "もの\x1c/🧪", True),
        ("saved-turn", "🧪" * ipc.SAVED_TURN_READ_MAX_ID_CHARS, True),
    ],
)
def test_saved_turn_read_preserves_owner_values_through_the_bounded_wire(
    monkeypatch: pytest.MonkeyPatch,
    caller: ipc.SavedReceiptCaller,
    turn_id: str,
    present: bool,
) -> None:
    borrow = _Borrow()
    expected = {
        "id": turn_id,
        "profile_id": borrow.context.profile_id,
        "extra": ["会話", {"zero": -0.0, "wide": 1 << 145}],
    } if present else None
    response = json.dumps(expected, ensure_ascii=False).encode("utf-8")

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
            "turn_read": {"caller": caller, "turn_id": turn_id},
        }
        for _ in range(2):
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, 7, response.ljust(1024 * 1024, b" "))
        assert peer.recv(1) == b""

    with _served_saved_turn_read(monkeypatch, serve) as reader:
        monkeypatch.delenv(ipc._SAVED_TURN_READ_ENV)
        monkeypatch.setenv(ipc._SOCKET_ENV, "/invalidated-after-capture")
        monkeypatch.delenv(ipc._OWNER_ENV)
        actual = reader(_saved_turn_read_request(borrow, caller, turn_id))
        assert actual == expected
        assert json.dumps(actual, ensure_ascii=False).encode("utf-8") == response


@pytest.mark.parametrize(
    ("caller", "turn_id"),
    [
        ("shell", "turn-1"),
        ("saved-turn", ""),
        ("saved-turn", "turn-1\x1c"),
        ("saved-reconcile", "\u3000turn-1"),
        ("saved-turn", "🧪" * (ipc.SAVED_TURN_READ_MAX_ID_CHARS + 1)),
        ("saved-reconcile", "\ud800"),
        ("saved-reconcile", 42),
    ],
)
def test_saved_turn_read_rejects_open_callers_and_invalid_identifiers(
    caller: ipc.SavedReceiptCaller, turn_id: str,
) -> None:
    with pytest.raises(ipc._ProtocolError, match="saved Turn read inputs are invalid"):
        ipc._start_payload(_saved_turn_read_request(_Borrow(), caller, turn_id), 1)


@pytest.mark.parametrize(
    ("live", "tag", "payload", "expected"),
    [
        (False, 7, b"null", ipc._ProtocolError),
        (False, 10, b"", ipc.NativeProviderPrepareUnavailable),
        (False, 8, b"legacy-read-required", ipc._ProtocolError),
        (False, 8, b"invalid", ipc._ProtocolError),
        (True, 8, b"legacy-read-required", ipc.NativeProviderRegistryRequiresLegacyRead),
        (True, 8, b"not-found", ipc.NativeSavedTurnReadFailure),
        (True, 8, b"invalid", ipc.NativeSavedTurnReadFailure),
        (True, 8, b"denied", ipc.NativeSavedTurnReadFailure),
        (True, 8, b"limit-exceeded", ipc.NativeSavedTurnReadFailure),
        (True, 8, b"internal", ipc.NativeSavedTurnReadFailure),
        (True, 10, b"", ipc._ProtocolError),
    ],
)
def test_saved_turn_read_distinguishes_proven_fallback_from_failures(
    monkeypatch: pytest.MonkeyPatch, live: bool, tag: int, payload: bytes,
    expected: type[RuntimeError],
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if live:
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, tag, payload)
        if (tag == 7 and not live) or (tag == 10 and live):
            assert _receive(peer) == (9, b"")
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with (
        _served_saved_turn_read(monkeypatch, serve) as reader,
        pytest.raises(expected) as failure,
    ):
        reader(_saved_turn_read_request(_Borrow()))
    if isinstance(failure.value, ipc.NativeSavedTurnReadFailure):
        assert failure.value.code == payload.decode("ascii")


@pytest.mark.parametrize(
    ("tag", "size", "payload"),
    [
        (7, 1024 * 1024 + 1, b""),
        (7, None, b'[]'),
        (7, None, b'{"turn":null}'),
        (7, None, b'{"id":"other","profile_id":"defaults"}'),
        (7, None, b'{"id":"turn-1","profile_id":"other"}'),
        (7, None, b'{"id":"turn-1","profile_id":"defaults","extra":1e999}'),
        (7, None, b'{"id":"turn-1","id":"turn-1","profile_id":"defaults"}'),
        (11, 0, b""),
    ],
)
def test_saved_turn_read_rejects_malformed_results_and_publication(
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
        _served_saved_turn_read(monkeypatch, serve) as reader,
        pytest.raises(ipc._ProtocolError, match=message),
    ):
        reader(_saved_turn_read_request(_Borrow()))


def _saved_orchestration_request(
    borrow: _Borrow, step_input: Mapping[str, Any],
) -> ipc.NativeSavedOrchestrationRequest:
    return ipc.NativeSavedOrchestrationRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        input=step_input,
    )


@contextmanager
def _served_saved_orchestration(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeSavedOrchestrationRequest], Mapping[str, Any]]]:
    monkeypatch.setenv(ipc._SAVED_ORCHESTRATION_ENV, "1")
    with _served(monkeypatch, serve):
        step = ipc.capture_launcher_saved_orchestration_step()
        assert step is not None
        yield step


def test_saved_orchestration_capture_requires_managed_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ipc._SOCKET_ENV, raising=False)
    monkeypatch.delenv(ipc._OWNER_ENV, raising=False)
    monkeypatch.setenv(ipc._SAVED_ORCHESTRATION_ENV, "1")
    assert ipc.capture_launcher_saved_orchestration_step() is None
    monkeypatch.setenv(ipc._SOCKET_ENV, "/fixture.sock")
    monkeypatch.setenv(ipc._OWNER_ENV, _owner())
    monkeypatch.setenv(ipc._SAVED_ORCHESTRATION_ENV, "true")
    assert ipc.capture_launcher_saved_orchestration_step() is None
    monkeypatch.delenv(ipc._SAVED_ORCHESTRATION_ENV)
    assert ipc.capture_launcher_saved_orchestration_step() is None
    monkeypatch.setenv(ipc._SAVED_ORCHESTRATION_ENV, "1")
    assert ipc.capture_launcher_saved_orchestration_step() is not None


@pytest.mark.parametrize("at_limit", [False, True])
def test_saved_orchestration_sends_canonical_bytes_only_after_readiness(
    monkeypatch: pytest.MonkeyPatch, at_limit: bool,
) -> None:
    borrow = _Borrow()
    limit = 4 * 1024 * 1024
    if at_limit:
        fill = "x" * (limit - len(b'{"request":{"content":""}}'))
        step_input = {"request": {"content": fill}}
        expected_input = b'{"request":{"content":"' + fill.encode() + b'"}}'
        assert len(expected_input) == limit
        result = {"output": "y" * (limit - len(b'{"output":""}'))}
        response = b'{"output":"' + result["output"].encode() + b'"}'
        assert len(response) == limit
    else:
        step_input = {"state": {"z": "最後", "a": True}, "outcome": {"first": "\n"}}
        expected_input = (
            '{"outcome":{"first":"\\n"},"state":{"a":true,"z":"最後"}}'.encode()
        )
        result = {"output": {"done": True}}
        response = b'{"output":{"done":true}}'

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
            "saved_orchestration": {"input_bytes": len(expected_input)},
        }
        peer.settimeout(0.02)
        with pytest.raises(TimeoutError):
            peer.recv(1)
        peer.settimeout(5)
        _send(peer, 14)
        assert _receive(peer) == (15, expected_input)
        for _ in range(2):
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, 7, response)
        assert peer.recv(1) == b""

    with _served_saved_orchestration(monkeypatch, serve) as step:
        monkeypatch.delenv(ipc._SAVED_ORCHESTRATION_ENV)
        monkeypatch.delenv(ipc._SOCKET_ENV)
        monkeypatch.delenv(ipc._OWNER_ENV)
        assert step(_saved_orchestration_request(borrow, step_input)) == result


@pytest.mark.parametrize(
    "step_input", [{"request": {}, "state": {}}, {"request": {"value": 0.5}}],
)
def test_saved_orchestration_rejects_invalid_input_before_start(
    monkeypatch: pytest.MonkeyPatch, step_input: Mapping[str, Any],
) -> None:
    _reject_saved_orchestration_input(monkeypatch, step_input)


def test_saved_orchestration_rejects_input_over_four_mib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = 4 * 1024 * 1024
    fill = "x" * (limit - len(b'{"request":{"content":""}}') + 1)
    _reject_saved_orchestration_input(monkeypatch, {"request": {"content": fill}})


def _reject_saved_orchestration_input(
    monkeypatch: pytest.MonkeyPatch, step_input: Mapping[str, Any],
) -> None:
    def serve(peer: socket.socket) -> None:
        assert peer.recv(1) == b""

    with (
        _served_saved_orchestration(monkeypatch, serve) as step,
        pytest.raises(ipc._ProtocolError, match="Saved Orchestration input is invalid"),
    ):
        step(_saved_orchestration_request(_Borrow(), step_input))


@pytest.mark.parametrize(
    ("body_sent", "tag", "payload", "expected"),
    [
        (False, 10, b"", ipc.NativeProviderPrepareUnavailable),
        (False, 8, b"denied", ipc.NativeSavedOrchestrationFailure),
        (True, 8, b"limit-exceeded", ipc.NativeSavedOrchestrationFailure),
    ],
)
def test_saved_orchestration_distinguishes_typed_failure_from_inapplicability(
    monkeypatch: pytest.MonkeyPatch, body_sent: bool, tag: int, payload: bytes,
    expected: type[RuntimeError],
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if body_sent:
            _send(peer, 14)
            assert _receive(peer) == (15, b'{"request":{}}')
        _send(peer, tag, payload)
        assert peer.recv(1) == b""

    with (
        _served_saved_orchestration(monkeypatch, serve) as step,
        pytest.raises(expected) as failure,
    ):
        step(_saved_orchestration_request(_Borrow(), {"request": {}}))
    if isinstance(failure.value, ipc.NativeSavedOrchestrationFailure):
        assert failure.value.code == payload.decode("ascii")


def test_saved_orchestration_inapplicable_rechecks_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _Borrow()
    receive = ipc._Exchange.receive

    def receive_and_revoke(
        exchange: ipc._Exchange, wait_seconds: Callable[[], float],
    ) -> tuple[int, bytes]:
        frame = receive(exchange, wait_seconds)
        if frame == (10, b""):
            borrow.revoked.set()
        return frame

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 10)
        assert _receive(peer) == (9, b"")
        _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    monkeypatch.setattr(ipc._Exchange, "receive", receive_and_revoke)
    with (
        _served_saved_orchestration(monkeypatch, serve) as step,
        pytest.raises(PermissionError, match="fixture lease revoked"),
    ):
        step(_saved_orchestration_request(borrow, {"request": {}}))


@pytest.mark.parametrize(
    ("phase", "tag", "payload", "invalid_header"),
    [
        ("start", 2, b"", False),
        ("start", 7, b"{}", False),
        ("start", 14, b"not empty", True),
        ("body", 7, b"{}", False),
        ("body", 10, b"", False),
        ("body", 14, b"", False),
        ("live", 11, b"", True),
        ("live", 8, b"legacy-read-required", True),
        ("live", 7, b"[]", False),
        ("live", 7, b'{"field":null,"field":true}', False),
    ],
)
def test_saved_orchestration_rejects_unexpected_frames(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    tag: int,
    payload: bytes,
    invalid_header: bool,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if phase != "start":
            _send(peer, 14)
            assert _receive(peer)[0] == 15
        if phase == "live":
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, tag, payload)
        assert _receive(peer) == (9, b"")
        if not invalid_header:
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with (
        _served_saved_orchestration(monkeypatch, serve) as step,
        pytest.raises(ipc._ProtocolError),
    ):
        step(_saved_orchestration_request(_Borrow(), {"request": {}}))


def test_saved_orchestration_rejects_plan_header_over_four_mib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 14)
        assert _receive(peer)[0] == 15
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        peer.sendall(_HEADER.pack(7, 4 * 1024 * 1024 + 1))
        assert _receive(peer) == (9, b"")
        assert peer.recv(1) == b""

    with (
        _served_saved_orchestration(monkeypatch, serve) as step,
        pytest.raises(ipc._ProtocolError),
    ):
        step(_saved_orchestration_request(_Borrow(), {"request": {}}))


@pytest.mark.parametrize("body_sent", [False, True])
def test_saved_orchestration_cancels_after_guard_revocation(
    monkeypatch: pytest.MonkeyPatch, body_sent: bool,
) -> None:
    borrow = _Borrow()

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if body_sent:
            _send(peer, 14)
            assert _receive(peer)[0] == 15
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        borrow.revoked.set()
        _send(peer, 2 if body_sent else 14)
        assert _receive(peer) == (9, b"")
        _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with (
        _served_saved_orchestration(monkeypatch, serve) as step,
        pytest.raises(PermissionError, match="fixture lease revoked"),
    ):
        step(_saved_orchestration_request(borrow, {"request": {}}))


@contextmanager
def _served_saved_local_chat(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeSavedLocalChatRequest], bytes]]:
    monkeypatch.setenv(ipc._SAVED_LOCAL_CHAT_ENV, "1")
    with _served(monkeypatch, serve):
        execute = ipc.capture_launcher_saved_local_chat_executor()
        assert execute is not None
        yield execute


def _saved_local_chat_request(
    borrow: _Borrow, *, role: ipc.SavedLocalChatRole = "generate",
    body: bytes = b'{"model":"fixture"}',
) -> ipc.NativeSavedLocalChatRequest:
    return ipc.NativeSavedLocalChatRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        role=role,
        connection_id="fixture-local",
        record_revision=7,
        endpoint="http://127.0.0.1:11434/v1",
        input=body,
        assert_dispatch=lambda: None,
        mark_dispatch_possible=lambda: None,
    )


def _accept_saved_local_chat_body(peer: socket.socket, expected: bytes) -> None:
    _send(peer, 14)
    assert _receive(peer) == (16, expected)


def _check_saved_local_chat_live(peer: socket.socket) -> None:
    _send(peer, 2)
    assert _receive(peer) == (3, b"")


def _confirm_saved_local_chat_cancellation(peer: socket.socket) -> None:
    assert _receive(peer) == (9, b"")
    _send(peer, 8, b"cancelled")
    assert peer.recv(1) == b""


def test_saved_local_chat_capture_requires_managed_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ipc._SOCKET_ENV, raising=False)
    monkeypatch.delenv(ipc._OWNER_ENV, raising=False)
    monkeypatch.setenv(ipc._SAVED_LOCAL_CHAT_ENV, "1")
    assert ipc.capture_launcher_saved_local_chat_executor() is None
    monkeypatch.setenv(ipc._SOCKET_ENV, "/fixture.sock")
    monkeypatch.setenv(ipc._OWNER_ENV, _owner())
    monkeypatch.setenv(ipc._SAVED_LOCAL_CHAT_ENV, "true")
    assert ipc.capture_launcher_saved_local_chat_executor() is None
    monkeypatch.delenv(ipc._SAVED_LOCAL_CHAT_ENV)
    assert ipc.capture_launcher_saved_local_chat_executor() is None
    monkeypatch.setenv(ipc._SAVED_LOCAL_CHAT_ENV, "1")
    assert ipc.capture_launcher_saved_local_chat_executor() is not None


@pytest.mark.parametrize("role", ["generate", "stream"])
def test_saved_local_chat_passes_exact_bytes_and_permits_dispatch_once(
    monkeypatch: pytest.MonkeyPatch, role: ipc.SavedLocalChatRole,
) -> None:
    borrow = _Borrow()
    original = ('{ "z": "ローカル", "stream": false, "a": "value" }\n').encode()
    body = (
        original + b" " * (1024 * 1024 - len(original))
        if role == "generate"
        else original
    )
    reply = (b'\xff' + b"x" * (4 * 1024 * 1024 - 1)) if role == "generate" else b""
    guard_calls: list[str] = []

    def assert_dispatch() -> None:
        borrow.assert_current()
        assert not guard_calls
        guard_calls.append("authority")

    def mark_dispatch() -> None:
        borrow.assert_current()
        assert guard_calls == ["authority"]
        guard_calls.append("marker")

    request = replace(
        _saved_local_chat_request(borrow, role=role, body=body),
        assert_dispatch=assert_dispatch,
        mark_dispatch_possible=mark_dispatch,
    )

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
            "saved_local_chat": {
                "role": role,
                "connection_id": "fixture-local",
                "record_revision": 7,
                "endpoint": "http://127.0.0.1:11434/v1",
                "input_bytes": len(body),
            },
        }
        peer.settimeout(0.02)
        with pytest.raises(TimeoutError):
            peer.recv(1)
        peer.settimeout(5)
        _accept_saved_local_chat_body(peer, body)
        _check_saved_local_chat_live(peer)
        _send(peer, 17)
        assert _receive(peer) == (18, b"")
        assert guard_calls == ["authority", "marker"]
        _check_saved_local_chat_live(peer)
        _send(peer, 7, reply)
        assert peer.recv(1) == b""

    with _served_saved_local_chat(monkeypatch, serve) as execute:
        monkeypatch.delenv(ipc._SAVED_LOCAL_CHAT_ENV)
        monkeypatch.delenv(ipc._SOCKET_ENV)
        monkeypatch.delenv(ipc._OWNER_ENV)
        assert execute(request) == reply


@pytest.mark.parametrize(
    ("body", "code"), [(b"", "invalid"), (b"x" * (1024 * 1024 + 1), "limit-exceeded")],
)
def test_saved_local_chat_rejects_out_of_bounds_body_before_connection(
    monkeypatch: pytest.MonkeyPatch, body: bytes, code: str,
) -> None:
    class UnexpectedSocket:
        def __init__(self, *args: Any) -> None:
            pytest.fail("invalid input reached the Launcher socket")

    monkeypatch.setenv(ipc._SAVED_LOCAL_CHAT_ENV, "1")
    monkeypatch.setenv(ipc._SOCKET_ENV, "/fixture.sock")
    monkeypatch.setenv(ipc._OWNER_ENV, _owner())
    monkeypatch.setattr(ipc.os, "getppid", os.getpid)
    monkeypatch.setattr(ipc.socket, "socket", UnexpectedSocket)
    execute = ipc.capture_launcher_saved_local_chat_executor()
    assert execute is not None
    with pytest.raises(ipc.NativeSavedLocalChatFailure) as failure:
        execute(_saved_local_chat_request(_Borrow(), body=body))
    assert failure.value.code == code


@pytest.mark.parametrize(
    ("phase", "tag", "payload", "exception_type"),
    [
        ("start", 10, b"", ipc.NativeProviderPrepareUnavailable),
        ("start", 8, b"unavailable", ipc.NativeSavedLocalChatFailure),
        ("body", 8, b"invalid", ipc.NativeSavedLocalChatFailure),
        ("permit", 8, b"limit-exceeded", ipc.NativeSavedLocalChatFailure),
        ("permit", 8, b"cancelled", ipc.NativeSavedLocalChatFailure),
        ("body", 10, b"", ipc._ProtocolError),
        ("body", 17, b"", ipc._ProtocolError),
        ("live", 7, b"premature", ipc._ProtocolError),
        ("permit", 17, b"", ipc._ProtocolError),
    ],
)
def test_saved_local_chat_enforces_terminal_and_dispatch_order(
    monkeypatch: pytest.MonkeyPatch, phase: str, tag: int, payload: bytes,
    exception_type: type[RuntimeError],
) -> None:
    request = _saved_local_chat_request(_Borrow())

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if phase != "start":
            _accept_saved_local_chat_body(peer, request.input)
        if phase in ("live", "permit"):
            _check_saved_local_chat_live(peer)
        if phase == "permit":
            _send(peer, 17)
            assert _receive(peer) == (18, b"")
        _send(peer, tag, payload)
        if exception_type is ipc._ProtocolError:
            _confirm_saved_local_chat_cancellation(peer)
        else:
            assert peer.recv(1) == b""

    with (
        _served_saved_local_chat(monkeypatch, serve) as execute,
        pytest.raises(exception_type) as failure,
    ):
        execute(request)
    if isinstance(failure.value, ipc.NativeSavedLocalChatFailure):
        assert failure.value.code == payload.decode("ascii")


@pytest.mark.parametrize("revoke_after", ["authority", "marker", "permit"])
def test_saved_local_chat_waits_for_cancellation_after_authority_revocation(
    monkeypatch: pytest.MonkeyPatch, revoke_after: str,
) -> None:
    borrow = _Borrow()
    marked = False

    def assert_dispatch() -> None:
        if revoke_after == "authority":
            borrow.revoked.set()

    def mark_dispatch() -> None:
        nonlocal marked
        marked = True
        if revoke_after == "marker":
            borrow.revoked.set()

    request = replace(
        _saved_local_chat_request(borrow),
        assert_dispatch=assert_dispatch,
        mark_dispatch_possible=mark_dispatch,
    )

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _accept_saved_local_chat_body(peer, request.input)
        _check_saved_local_chat_live(peer)
        _send(peer, 17)
        if revoke_after == "permit":
            assert _receive(peer) == (18, b"")
            borrow.revoked.set()
        _confirm_saved_local_chat_cancellation(peer)

    with (
        _served_saved_local_chat(monkeypatch, serve) as execute,
        pytest.raises(PermissionError, match="fixture lease revoked"),
    ):
        execute(request)
    assert marked == (revoke_after != "authority")


def test_saved_local_chat_rejects_fallback_requested_by_authority_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def assert_dispatch() -> None:
        raise ipc.NativeProviderPrepareUnavailable("unavailable dependency")

    request = replace(
        _saved_local_chat_request(_Borrow()), assert_dispatch=assert_dispatch,
    )

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _accept_saved_local_chat_body(peer, request.input)
        _check_saved_local_chat_live(peer)
        _send(peer, 17)
        _confirm_saved_local_chat_cancellation(peer)

    with (
        _served_saved_local_chat(monkeypatch, serve) as execute,
        pytest.raises(ipc.NativeSavedLocalChatFailure) as failure,
    ):
        execute(request)
    assert failure.value.code == "unavailable"


@pytest.mark.parametrize("oversized_result", [False, True])
def test_saved_local_chat_reports_unconfirmed_dispatch_without_cleanup_proof(
    monkeypatch: pytest.MonkeyPatch, oversized_result: bool,
) -> None:
    request = _saved_local_chat_request(_Borrow())

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _accept_saved_local_chat_body(peer, request.input)
        _check_saved_local_chat_live(peer)
        _send(peer, 17)
        assert _receive(peer) == (18, b"")
        if oversized_result:
            peer.sendall(_HEADER.pack(7, 4 * 1024 * 1024 + 1))
            assert _receive(peer) == (9, b"")
            assert peer.recv(1) == b""

    with (
        _served_saved_local_chat(monkeypatch, serve) as execute,
        pytest.raises(ipc.NativeSavedLocalChatDispatchUnconfirmed),
    ):
        execute(request)


@pytest.mark.parametrize("terminal_tag", [7, 8])
def test_saved_local_chat_drains_to_terminal_after_original_deadline(
    monkeypatch: pytest.MonkeyPatch, terminal_tag: int,
) -> None:
    request = _saved_local_chat_request(_Borrow(lifetime=1.0))

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _accept_saved_local_chat_body(peer, request.input)
        _check_saved_local_chat_live(peer)
        _send(peer, 17)
        assert _receive(peer) == (18, b"")
        assert _receive(peer) == (9, b"")
        payload = b"late successful response" if terminal_tag == 7 else b"cancelled"
        _send(peer, terminal_tag, payload)
        assert peer.recv(1) == b""

    with (
        _served_saved_local_chat(monkeypatch, serve) as execute,
        pytest.raises(TimeoutError, match="deadline elapsed"),
    ):
        execute(request)


def _model_resolve_request(
    borrow: _Borrow,
    role: ipc.ModelResolveRole = "generate",
    identifier: str = "fixture/gpt_1-x.y:z",
) -> ipc.NativeModelResolveRequest:
    return ipc.NativeModelResolveRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        role=role,
        identifier=identifier,
    )


@contextmanager
def _served_model_resolve(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
) -> Iterator[Callable[[ipc.NativeModelResolveRequest], Mapping[str, Any]]]:
    with _served(monkeypatch, serve):
        reader = ipc.capture_launcher_model_resolve_reader()
        assert reader is not None
        yield reader


@pytest.mark.parametrize(
    "role", ["quote-generate", "quote-stream", "generate", "stream"],
)
def test_model_resolve_preserves_original_owner_values_and_order(
    monkeypatch: pytest.MonkeyPatch, role: ipc.ModelResolveRole,
) -> None:
    borrow = _Borrow()
    request = _model_resolve_request(borrow, role)
    expected = {
        "requested_id": request.identifier,
        "resolved_profile_id": "saved",
        "aliased": True,
        "profile": {
            "credential_handle": "credential:synthetic",
            "parameters": {"temperature": -0.0, "top_p": 2e-7, "seed": (1 << 145)},
            "requirements": {"preferred_provider_instance_id": "local"},
            "metadata": {"policy": {"z": 1, "a": "日本語"}},
        },
        "store_revision": False if role == "quote-stream" else (1 << 100),
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
            "model_resolve": {"role": role, "identifier": request.identifier},
        }
        for _ in range(2):
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, 7, response)
        assert peer.recv(1) == b""

    with _served_model_resolve(monkeypatch, serve) as reader:
        monkeypatch.setenv(ipc._SOCKET_ENV, "/invalidated-after-capture")
        monkeypatch.delenv(ipc._OWNER_ENV)
        actual = reader(request)
        assert dict(actual) == expected
        assert json.dumps(actual, ensure_ascii=False).encode() == response.rstrip()
        assert math.copysign(1.0, actual["profile"]["parameters"]["temperature"]) == -1


@pytest.mark.parametrize(
    ("role", "identifier"),
    [
        ("shell", "fixture"),
        ("generate", ""),
        ("generate", "a" * 257),
        ("generate", "äbc"),
        ("generate", "-leading"),
        ("generate", " spaced"),
        ("generate", "a\n"),
        ("generate", "embedded space"),
        ("generate", 42),
    ],
)
def test_model_resolve_refuses_open_roles_and_noncanonical_identifiers(
    monkeypatch: pytest.MonkeyPatch, role: ipc.ModelResolveRole, identifier: str,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert peer.recv(1) == b""

    with (
        _served_model_resolve(monkeypatch, serve) as reader,
        pytest.raises(ipc._ProtocolError, match="Model Resolve inputs are invalid"),
    ):
        reader(_model_resolve_request(_Borrow(), role, identifier))


def test_model_resolve_reader_requires_the_correct_request_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _listener() as (_, address):
        monkeypatch.setenv(ipc._SOCKET_ENV, address)
        monkeypatch.setenv(ipc._OWNER_ENV, _owner())
        reader = ipc.capture_launcher_model_resolve_reader()
        planner = ipc.capture_launcher_provider_prepare_planner()
        assert reader is not None
        assert planner is not None
        with pytest.raises(ipc._ProtocolError, match="request role is invalid"):
            reader(_conversation_get_request(_Borrow()))
        with pytest.raises(ipc._ProtocolError, match="request role is invalid"):
            planner.plan(_model_resolve_request(_Borrow()))


@pytest.mark.parametrize(
    ("live", "tag", "payload", "expected"),
    [
        (False, 10, b"", ipc.NativeProviderPrepareUnavailable),
        (False, 8, b"legacy-read-required", ipc._ProtocolError),
        (False, 8, b"not-found", ipc._ProtocolError),
        (
            True, 8, b"legacy-read-required",
            ipc.NativeProviderRegistryRequiresLegacyRead,
        ),
        (True, 8, b"not-found", ipc.NativeModelResolveFailure),
        (True, 8, b"invalid", ipc.NativeModelResolveFailure),
        (True, 8, b"denied", ipc.NativeModelResolveFailure),
        (True, 8, b"internal", ipc._ProtocolError),
        (True, 10, b"", ipc._ProtocolError),
    ],
)
def test_model_resolve_distinguishes_authenticated_fallback_and_owner_failure(
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
        _served_model_resolve(monkeypatch, serve) as reader,
        pytest.raises(expected) as failure,
    ):
        reader(_model_resolve_request(_Borrow(), identifier="a" * 256))
    if isinstance(failure.value, ipc.NativeModelResolveFailure):
        assert failure.value.code == payload.decode("ascii")


@pytest.mark.parametrize(
    ("tag", "size", "payload"),
    [
        (7, 1024 * 1024 + 1, b""),
        (7, None, b'{"model":"\xff"}'),
        (7, None, b'{"model":1e999}'),
        (7, None, b'{"model":0,"model":1}'),
        (7, None, b'{"owner_result":"{}"}'),
        (11, 0, b""),
    ],
)
def test_model_resolve_rejects_malformed_results_and_publication(
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
        _served_model_resolve(monkeypatch, serve) as reader,
        pytest.raises(ipc._ProtocolError, match=message),
    ):
        reader(_model_resolve_request(_Borrow()))


@pytest.mark.parametrize("revoked", [False, True])
@pytest.mark.parametrize("operation", ["model-resolve", "saved-receipt", "turn-read"])
def test_owner_read_requires_a_live_guard_during_result_delivery(
    monkeypatch: pytest.MonkeyPatch, revoked: bool, operation: str,
) -> None:
    borrow = _Borrow(lifetime=3 if revoked else 0.5)

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        failure = b"legacy-read-required"
        peer.sendall(_HEADER.pack(8, len(failure)) + failure[:1])
        if revoked:
            borrow.revoked.set()
        assert _receive(peer) == (9, b"")
        peer.sendall(failure[1:])
        assert peer.recv(1) == b""

    expected = PermissionError if revoked else TimeoutError
    with _served(monkeypatch, serve), pytest.raises(expected):
        if operation == "turn-read":
            monkeypatch.setenv(ipc._SAVED_TURN_READ_ENV, "1")
            turn_reader = ipc.capture_launcher_saved_turn_reader()
            assert turn_reader is not None
            turn_reader(_saved_turn_read_request(borrow))
        elif operation == "saved-receipt":
            reader = ipc.capture_launcher_saved_receipt_reader()
            assert reader is not None
            reader(_saved_receipt_request(borrow))
        else:
            model_reader = ipc.capture_launcher_model_resolve_reader()
            assert model_reader is not None
            model_reader(_model_resolve_request(borrow))
