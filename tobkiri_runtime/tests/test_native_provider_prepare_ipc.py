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


@pytest.mark.parametrize("revoke_after_reply", [False, True])
def test_application_presentation_exchanges_only_the_admitted_body_and_live_reply(
    monkeypatch: pytest.MonkeyPatch, revoke_after_reply: bool,
) -> None:
    borrow = _Borrow()
    request = ipc.NativeApplicationPresentationRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        role="settings",
        input={
            "profile_id": "defaults", "kind": "ui",
            "model_options": [{"value": "sample", "label": "日本語"}],
        },
    )
    body = (
        '{"kind":"ui","model_options":[{"label":"日本語","value":"sample"}],'
        '"profile_id":"defaults"}'
    ).encode("utf-8")
    expected = {"sections": [], "definitions": {}}

    def serve(peer: socket.socket) -> None:
        tag, payload = _receive(peer)
        assert tag == ipc._START
        start = json.loads(payload)
        assert set(start) == {
            "identity", "security_epoch", "runtime_instance", "deadline_ns",
            "application_presentation",
        }
        assert start["application_presentation"] == {
            "role": "settings", "input_bytes": len(body),
        }
        assert start["runtime_instance"] == request.runtime_instance
        assert start["deadline_ns"] <= int(request.deadline_monotonic * 10**9)
        _send(peer, ipc._BODY_READY)
        assert _receive(peer) == (ipc._ORCHESTRATION_INPUT, body)
        _send(peer, ipc._CHECK)
        assert _receive(peer) == (ipc._LIVE, b"")
        _send(peer, ipc._PLAN, json.dumps(expected).encode())

    if revoke_after_reply:
        decode = ipc._decode_plan

        def decode_and_revoke(payload: bytes, limit: int) -> dict[str, Any]:
            result = decode(payload, limit)
            borrow.revoked.set()
            return result

        monkeypatch.setattr(ipc, "_decode_plan", decode_and_revoke)
    manifest = "sha256:" + "a" * 64
    monkeypatch.setenv(ipc._APPLICATION_PRESENTATION_ENV, manifest)
    with _served(monkeypatch, serve):
        captured = ipc.capture_launcher_application_presentation()
        assert captured is not None
        captured_manifest, invoke = captured
        assert captured_manifest == manifest
        if revoke_after_reply:
            with pytest.raises(PermissionError, match="fixture lease revoked"):
                invoke(request)
        else:
            assert invoke(request) == expected


def test_application_presentation_requires_an_attested_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ipc._APPLICATION_PRESENTATION_ENV, raising=False)
    assert ipc.capture_launcher_application_presentation() is None
    monkeypatch.setenv(ipc._APPLICATION_PRESENTATION_ENV, "1")
    assert ipc.capture_launcher_application_presentation() is None


@pytest.mark.parametrize(
    ("phase", "reply", "failure"),
    [
        ("prepare", b'{"messages":[{"content":"hello","role":"user"}]}', None),
        ("finish", b'{"content":[],"tool_calls":[]}', None),
        ("finish", b"invalid", ipc.NativeConversationCompleteFailure),
        ("finish", b'{"tool_calls":[],"content":[]}', ipc._ProtocolError),
    ],
)
def test_conversation_complete_uses_funded_canonical_phase_ipc(
    monkeypatch: pytest.MonkeyPatch,
    phase: ipc.ConversationCompletePhase,
    reply: bytes,
    failure: type[Exception] | None,
) -> None:
    """Keep the authenticated phase, body size and liveness on the wire."""
    borrow = _Borrow()
    if phase == "prepare":
        data = {"messages": [{"role": "user", "content": "日本語"}]}
    else:
        data = {"status": "ok", "value": {"output": "日本語"}}
    body = ipc.canonical_json(data)
    request = ipc.NativeConversationCompleteRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        phase=phase,
        input=data,
    )

    def serve(peer: socket.socket) -> None:
        tag, payload = _receive(peer)
        assert tag == ipc._START
        start = json.loads(payload)
        assert set(start) == {
            "identity", "security_epoch", "runtime_instance", "deadline_ns",
            "conversation_complete",
        }
        assert start["conversation_complete"] == {
            "phase": phase, "input_bytes": len(body),
        }
        assert start["runtime_instance"] == request.runtime_instance
        assert start["deadline_ns"] <= int(request.deadline_monotonic * 10**9)
        _send(peer, ipc._BODY_READY)
        assert _receive(peer) == (ipc._ORCHESTRATION_INPUT, body)
        _send(peer, ipc._CHECK)
        assert _receive(peer) == (ipc._LIVE, b"")
        tag = ipc._FAILURE if failure is ipc.NativeConversationCompleteFailure else ipc._PLAN
        _send(peer, tag, reply)

    manifest = "sha256:" + "b" * 64
    monkeypatch.setenv(ipc._CONVERSATION_COMPLETE_ENV, manifest)
    with _served(monkeypatch, serve):
        captured = ipc.capture_launcher_conversation_complete()
        assert captured is not None
        captured_manifest, invoke = captured
        assert captured_manifest == manifest
        if failure is None:
            assert invoke(request) == json.loads(reply)
        else:
            with pytest.raises(failure):
                invoke(request)


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


def test_side_chat_turn_start_uses_separate_admission_and_exact_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ipc._SAVED_TURN_START_ENV, "1")
    monkeypatch.delenv(ipc._SIDE_CHAT_START_ENV, raising=False)
    monkeypatch.setattr(ipc, "_capture_launcher", lambda: object())
    assert ipc.capture_launcher_saved_turn_start_writer() is not None
    assert ipc.capture_launcher_side_chat_turn_start_writer() is None
    monkeypatch.delenv(ipc._SAVED_TURN_START_ENV)
    monkeypatch.setenv(ipc._SIDE_CHAT_START_ENV, "1")
    assert ipc.capture_launcher_saved_turn_start_writer() is None
    assert ipc.capture_launcher_side_chat_turn_start_writer() is not None
    ordinary = _turn_start_request(_Borrow(), lambda: None)
    linked = replace(ordinary, caller="side-chat")
    assert "caller" not in ipc._turn_start_payload(ordinary)
    assert ipc._turn_start_payload(linked) == {
        **ipc._turn_start_payload(ordinary), "caller": "side-chat",
    }
    with pytest.raises(ipc._ProtocolError):
        ipc._turn_start_payload(replace(ordinary, caller="forged"))  # type: ignore[arg-type]


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
@pytest.mark.parametrize("side_chat", [False, True])
def test_turn_settlement_sends_only_the_bound_reference_and_preserves_the_owner_row(
    monkeypatch: pytest.MonkeyPatch, replay: bool, side_chat: bool,
) -> None:
    borrow = _Borrow()
    marked = threading.Event()
    checked = threading.Event()

    def guard() -> None:
        borrow.assert_current()
        if marked.is_set():
            checked.set()

    request = replace(_settlement_request(borrow, marked.set), assert_current=guard)
    if side_chat:
        request = replace(
            request,
            caller="side-chat-reconcile" if replay else "side-chat-turn",
            expected_revision=7 if replay else None,
        )
    elif replay:
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
    {"caller": "side-chat-reconcile"}, {"caller": "side-chat"},
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
    caller: ipc.SavedTurnOrigin = "saved-turn",
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


@pytest.mark.parametrize(
    "caller", ["saved-turn", "saved-reconcile", "side-chat-turn", "side-chat-reconcile"],
)
@pytest.mark.parametrize("confirmed, publish", [(True, True), (True, False), (False, False)])
def test_conversation_confirmation_binds_terminal_and_permits_only_publication(
    monkeypatch: pytest.MonkeyPatch,
    caller: ipc.SavedTurnOrigin,
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


def test_side_chat_append_uses_separate_admission_and_exact_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ipc._APPEND_ENV, "1")
    monkeypatch.setattr(ipc, "_capture_launcher", lambda: object())
    assert ipc.capture_launcher_conversation_append_writer() is not None
    assert ipc.capture_launcher_side_chat_conversation_append_writer() is None
    monkeypatch.delenv(ipc._APPEND_ENV)
    monkeypatch.setenv(ipc._SIDE_CHAT_APPEND_ENV, "1")
    assert ipc.capture_launcher_conversation_append_writer() is None
    assert ipc.capture_launcher_side_chat_conversation_append_writer() is not None
    ordinary = _append_request(_Borrow(), lambda: None)
    linked = replace(ordinary, caller="side-chat")
    assert ipc._conversation_append_payload(ordinary) == {"request": _APPEND_REQUEST}
    assert ipc._conversation_append_payload(linked) == {
        "caller": "side-chat",
        "request": _APPEND_REQUEST,
    }
    with pytest.raises(ipc._ProtocolError):
        ipc._conversation_append_payload(replace(ordinary, caller="forged"))  # type: ignore[arg-type]


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


@pytest.mark.parametrize(
    "role", (*ipc._SAVED_LOCAL_CHAT_ROLES, *ipc.GATEWAY_PROVIDER_SNAPSHOT_ROLES.values()),
)
def test_provider_snapshot_receives_the_original_owner_reply(
    monkeypatch: pytest.MonkeyPatch, role: ipc.ProviderSnapshotRole,
) -> None:
    borrow = _Borrow()
    request = ipc.NativeProviderSnapshotRequest(
        borrow.context, borrow.runtime_instance, borrow.deadline_monotonic,
        borrow.assert_current, role,
    )
    expected = {
        "version": "rumi.provider-registry.store.v1",
        "profile_id": borrow.context.profile_id,
        "revision": 1 << 140,
        "providers": [{"metadata": {"weight": 0.25, "note": "接続" * 6000}}],
    }

    def serve(peer: socket.socket) -> None:
        tag, frame = _receive(peer)
        assert tag == 1
        assert json.loads(frame)["provider_snapshot"] == {"role": role}
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, json.dumps(expected, ensure_ascii=False).encode())
        assert peer.recv(1) == b""

    monkeypatch.delenv(ipc._PROVIDER_SNAPSHOT_ENV, raising=False)
    with _served(monkeypatch, serve):
        assert ipc.capture_launcher_provider_snapshot_reader() is None
        monkeypatch.setenv(ipc._PROVIDER_SNAPSHOT_ENV, "1")
        reader = ipc.capture_launcher_provider_snapshot_reader()
        assert reader is not None
        assert not reader.supports_gateway
        if role in ipc.GATEWAY_PROVIDER_SNAPSHOT_ROLES.values():
            with pytest.raises(ipc.NativeProviderPrepareUnavailable):
                reader(request)
            monkeypatch.setenv(ipc._PROVIDER_SNAPSHOT_ENV, "2")
            reader = ipc.capture_launcher_provider_snapshot_reader()
            assert reader is not None and reader.supports_gateway
        assert reader(request) == expected


@pytest.mark.parametrize("role", ipc._WORKFLOW_PROVIDER_SNAPSHOT_ROLES)
@pytest.mark.parametrize("entry", ipc.WORKFLOW_PROVIDER_SNAPSHOT_ENTRIES)
def test_workflow_provider_snapshot_carries_the_exact_entry(
    monkeypatch: pytest.MonkeyPatch,
    role: ipc.WorkflowProviderSnapshotRole,
    entry: ipc.WorkflowSnapshotEntry,
) -> None:
    borrow = _Borrow()
    request = ipc.NativeProviderSnapshotRequest(
        borrow.context, borrow.runtime_instance, borrow.deadline_monotonic,
        borrow.assert_current, role, workflow_entry=entry,
    )
    result = {
        "version": "rumi.provider-registry.store.v1",
        "profile_id": borrow.context.profile_id,
        "revision": 1,
        "providers": [{"display_name": "ローカル", "metadata": {"cost": 0.125}}],
    }

    def serve(peer: socket.socket) -> None:
        tag, frame = _receive(peer)
        assert tag == 1
        assert json.loads(frame)["provider_snapshot"] == {"role": role, "entry": entry}
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, json.dumps(result).encode())
        assert peer.recv(1) == b""

    with _served(monkeypatch, serve):
        monkeypatch.setenv(ipc._PROVIDER_SNAPSHOT_ENV, "4")
        reader = ipc.capture_launcher_provider_snapshot_reader()
        assert reader is not None and not reader.supports_workflow
        with pytest.raises(ipc.NativeProviderPrepareUnavailable):
            reader(request)
        monkeypatch.setenv(ipc._PROVIDER_SNAPSHOT_ENV, "5")
        reader = ipc.capture_launcher_provider_snapshot_reader()
        assert reader is not None and reader.supports_workflow and reader.supports_query
        assert not reader.supports_workflow_adapter
        if role in ipc._WORKFLOW_ADAPTER_PROVIDER_SNAPSHOT_ROLES:
            with pytest.raises(ipc.NativeProviderPrepareUnavailable):
                reader(request)
            monkeypatch.setenv(ipc._PROVIDER_SNAPSHOT_ENV, "6")
            reader = ipc.capture_launcher_provider_snapshot_reader()
            assert reader is not None and reader.supports_workflow_adapter
            assert reader.supports_workflow and reader.supports_query
        assert reader(request) == result


@pytest.mark.parametrize("role", ipc._MODEL_ACCESS_PROVIDER_SNAPSHOT_ROLES)
def test_model_access_provider_snapshot_is_a_v7_raw_borrow(
    monkeypatch: pytest.MonkeyPatch, role: ipc.ModelAccessProviderSnapshotRole,
) -> None:
    borrow = _Borrow()
    request = ipc.NativeProviderSnapshotRequest(
        borrow.context, borrow.runtime_instance, borrow.deadline_monotonic,
        borrow.assert_current, role,
    )
    result = {
        "version": "rumi.provider-registry.store.v1",
        "profile_id": borrow.context.profile_id,
        "revision": 1,
        "providers": [{"metadata": {"cost": 0.125}}],
    }

    def serve(peer: socket.socket) -> None:
        tag, frame = _receive(peer)
        assert tag == 1
        assert json.loads(frame)["provider_snapshot"] == {"role": role}
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, json.dumps(result).encode())
        assert peer.recv(1) == b""

    with _served(monkeypatch, serve):
        for version in ("4", "6"):
            monkeypatch.setenv(ipc._PROVIDER_SNAPSHOT_ENV, version)
            reader = ipc.capture_launcher_provider_snapshot_reader()
            assert reader is not None and not reader.supports_model_access
            with pytest.raises(ipc.NativeProviderPrepareUnavailable):
                reader(request)
        monkeypatch.setenv(ipc._PROVIDER_SNAPSHOT_ENV, "7")
        reader = ipc.capture_launcher_provider_snapshot_reader()
        assert reader is not None and reader.supports_model_access
        assert reader.supports_workflow_adapter and reader.supports_query
        assert reader(request) == result


@pytest.mark.parametrize(("role", "entry"), [
    ("workflow-text", None), ("workflow-messages", "run.get"), ("generate", "run.advance"),
    ("workflow-text-adapter", None), ("workflow-messages-adapter", "run.resume"),
    ("model-access-catalog", "run.advance"),
])
def test_workflow_provider_snapshot_rejects_mismatched_entry(role, entry) -> None:
    borrow = _Borrow()
    request = ipc.NativeProviderSnapshotRequest(
        borrow.context, borrow.runtime_instance, borrow.deadline_monotonic,
        borrow.assert_current, role, workflow_entry=entry,
    )
    with pytest.raises(ipc._ProtocolError, match="snapshot role"):
        ipc._start_payload(request, 1)


@pytest.mark.parametrize("role", ipc._PROJECTED_PROVIDER_SNAPSHOT_ROLES)
@pytest.mark.parametrize("unexpected_field", [None, "credential_handle", "endpoint"])
def test_provider_snapshot_settings_role_only_accepts_the_public_view(
    monkeypatch: pytest.MonkeyPatch,
    role: ipc.ProjectedProviderSnapshotRole,
    unexpected_field: str | None,
) -> None:
    borrow = _Borrow()
    request = ipc.NativeProviderSnapshotRequest(
        borrow.context, borrow.runtime_instance, borrow.deadline_monotonic,
        borrow.assert_current, role,
    )
    row = {
        "provider_instance_id": "provider/one", "display_name": "ローカル",
        "enabled": True, "credential_status": "not_required",
        "health_status": "verified", "reachability": "available",
        "observed_at": 108, "adapter_id": "local-openai-compatible",
    }
    if unexpected_field is not None:
        row[unexpected_field] = "unexpected value"
    result = {"revision": 2, "providers": [row]}

    def serve(peer: socket.socket) -> None:
        tag, frame = _receive(peer)
        assert tag == 1
        assert json.loads(frame)["provider_snapshot"] == {"role": role}
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, json.dumps(result, ensure_ascii=False).encode())
        if unexpected_field is not None:
            assert _receive(peer) == (9, b"")
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    with _served(monkeypatch, serve):
        query_role = role in ipc._QUERY_PROVIDER_SNAPSHOT_ROLES
        for old_version in (("1", "2", "3") if query_role else ("1", "2")):
            monkeypatch.setenv(ipc._PROVIDER_SNAPSHOT_ENV, old_version)
            reader = ipc.capture_launcher_provider_snapshot_reader()
            assert reader is not None
            with pytest.raises(ipc.NativeProviderPrepareUnavailable):
                reader(request)
        monkeypatch.setenv(ipc._PROVIDER_SNAPSHOT_ENV, "4" if query_role else "3")
        reader = ipc.capture_launcher_provider_snapshot_reader()
        assert reader is not None and reader.supports_gateway and reader.supports_projected
        assert reader.supports_query is query_role
        if unexpected_field is not None:
            with pytest.raises(ipc._ProtocolError):
                reader(request)
        else:
            assert reader(request) == result


@pytest.mark.parametrize(
    ("live", "tag", "response", "error"),
    [
        (False, 10, b"", ipc.NativeProviderPrepareUnavailable),
        (False, 8, b"legacy-read-required", ipc._ProtocolError),
        (True, 8, b"legacy-read-required", ipc.NativeProviderRegistryRequiresLegacyRead),
        (True, 8, b"denied", ipc._ProtocolError),
        (True, 7, b'{"version":"rumi.provider-registry.store.v1","profile_id":"other",'
         b'"revision":0,"providers":[]}', ipc._ProtocolError),
    ],
)
def test_provider_snapshot_only_falls_back_on_authenticated_compatibility(
    monkeypatch: pytest.MonkeyPatch, live: bool, tag: int, response: bytes,
    error: type[RuntimeError],
) -> None:
    borrow = _Borrow()
    request = ipc.NativeProviderSnapshotRequest(
        borrow.context, borrow.runtime_instance, borrow.deadline_monotonic,
        borrow.assert_current, "side-chat-stream",
    )

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if live:
            _send(peer, 2)
            assert _receive(peer) == (3, b"")
        _send(peer, tag, response)
        if tag == 7:
            assert _receive(peer) == (9, b"")
            _send(peer, 8, b"cancelled")
        assert peer.recv(1) == b""

    monkeypatch.setenv(ipc._PROVIDER_SNAPSHOT_ENV, "1")
    with _served(monkeypatch, serve):
        reader = ipc.capture_launcher_provider_snapshot_reader()
        assert reader is not None
        with pytest.raises(error) as failure:
            reader(request)
        if tag == 7:
            assert str(failure.value) == "native Provider snapshot result is invalid"


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
    ("version", "mode", "identifier"),
    [
        ("1", "retain", "child"), ("1", "reuse", "parent"),
        ("2", "retain-for-recheck", "child"), ("2", "reuse", "parent"),
        ("2", "recheck-child", "child"), ("2", "recheck-parent", "parent"),
    ],
)
def test_linked_conversation_get_sends_only_the_selected_id_and_nonce(
    monkeypatch: pytest.MonkeyPatch, version: str, mode: str, identifier: str,
) -> None:
    nonce = "ab" * 16
    request = replace(
        _conversation_get_request(_Borrow(), "saved-orchestration"),
        conversation_id=identifier,
        linked_parent=ipc.NativeConversationLinkedParent(mode, nonce),
    )
    expected = {"conversation": {"id": identifier, "messages": []}}

    def serve(peer: socket.socket) -> None:
        tag, payload = _receive(peer)
        assert tag == 1
        assert json.loads(payload)["conversation_get"] == {
            "caller": "saved-orchestration",
            "conversation_id": identifier,
            "linked_parent": {"mode": mode, "nonce": nonce},
        }
        _send(peer, 2)
        assert _receive(peer) == (3, b"")
        _send(peer, 7, json.dumps(expected).encode())
        assert peer.recv(1) == b""

    monkeypatch.setenv(ipc._LINKED_CONVERSATION_READ_ENV, version)
    with _served_conversation_get(monkeypatch, serve) as reader:
        # Capture the opt-in once with the managed Launcher endpoint.
        monkeypatch.delenv(ipc._LINKED_CONVERSATION_READ_ENV)
        assert reader(request) == expected


@pytest.mark.parametrize(
    ("caller", "identifier", "mode", "nonce"),
    [
        ("shell", "child", "retain", "a" * 32),
        ("saved-turn", "parent", "reuse", "a" * 32),
        ("saved-orchestration", "nested/path", "retain", "a" * 32),
        ("saved-orchestration", "parent", "reuse", "A" * 32),
        ("saved-orchestration", "child", "retain", "a" * 31),
        ("saved-orchestration", "child", "unknown", "a" * 32),
    ],
)
def test_linked_conversation_get_rejects_unclosed_start(
    caller: str, identifier: str, mode: str, nonce: str,
) -> None:
    request = replace(
        _conversation_get_request(_Borrow(), caller),
        conversation_id=identifier,
        linked_parent=ipc.NativeConversationLinkedParent(mode, nonce),
    )
    with pytest.raises(ipc._ProtocolError, match="linked Conversation GET inputs"):
        ipc._start_payload(request, 1)


def test_linked_conversation_get_requires_managed_launch_capture_and_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = ipc._Launcher(b"/fixture.sock", 2, "darwin:2:1:000000")
    monkeypatch.setattr(ipc, "_capture_launcher", lambda: launcher)
    monkeypatch.delenv(ipc._LINKED_CONVERSATION_READ_ENV, raising=False)
    ordinary = ipc.capture_launcher_conversation_get_reader()
    assert ordinary is not None and ordinary.supports_linked_parent is False
    monkeypatch.setenv(ipc._LINKED_CONVERSATION_READ_ENV, "1")
    linked = ipc.capture_launcher_conversation_get_reader()
    assert linked is not None and linked.supports_linked_parent is True
    assert linked.supports_linked_recheck is False
    request = replace(
        _conversation_get_request(_Borrow(), "saved-orchestration"),
        linked_parent=ipc.NativeConversationLinkedParent("retain", "a" * 32),
    )
    with pytest.raises(ipc._ProtocolError, match="linked Conversation GET is unavailable"):
        ordinary(request)
    recheck = replace(
        request, linked_parent=ipc.NativeConversationLinkedParent("recheck-child", "a" * 32),
    )
    with pytest.raises(ipc._ProtocolError, match="linked Conversation recheck is unavailable"):
        linked(recheck)
    monkeypatch.setattr(ipc, "_capture_launcher", lambda: None)
    assert ipc.capture_launcher_conversation_get_reader() is None


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
    caller: ipc.SavedTurnOrigin = "saved-turn",
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
        ("side-chat-turn", "child-turn", {"conversation_id": "child"}),
        ("side-chat-reconcile", "child-turn", {"conversation_id": "child"}),
        (
            "saved-turn", "\0" * ipc.SAVED_RECEIPT_MAX_ID_CHARS,
            {"extra": ["会話", {"zero": -0.0, "wide": 1 << 145}]},
        ),
    ],
)
def test_saved_receipt_preserves_raw_turn_id_and_original_owner_values(
    monkeypatch: pytest.MonkeyPatch,
    caller: ipc.SavedTurnOrigin,
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
        ("side-chat", "turn-1"),
        ("side-chat-reconcile", "a" * (ipc.SAVED_RECEIPT_MAX_ID_CHARS + 1)),
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
    caller: ipc.SavedTurnOrigin = "saved-turn",
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
        ("side-chat-turn", "side-turn", True),
        ("side-chat-reconcile", "side-reconcile", True),
    ],
)
def test_saved_turn_read_preserves_owner_values_through_the_bounded_wire(
    monkeypatch: pytest.MonkeyPatch,
    caller: ipc.SavedTurnOrigin,
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


@contextmanager
def _served_ai_routing(
    monkeypatch: pytest.MonkeyPatch, serve: Callable[[socket.socket], None],
    *, stream_result: bool = False,
) -> Iterator[Callable[[ipc.NativeAiRoutingRequest], Mapping[str, Any]]]:
    if stream_result:
        monkeypatch.setenv(ipc._AI_STREAM_RESULT_ENV, "1")
        capture = ipc.capture_launcher_ai_stream_result_normalizer
    else:
        monkeypatch.setenv(ipc._AI_ROUTING_ENV, "1")
        capture = ipc.capture_launcher_ai_routing_evaluator
    with _served(monkeypatch, serve):
        evaluate = capture()
        assert evaluate is not None
        yield evaluate


def _ai_routing_request(
    borrow: _Borrow, role: ipc.ModelResolveRole = "generate",
    payload: Mapping[str, Any] | None = None,
) -> ipc.NativeAiRoutingRequest:
    return ipc.NativeAiRoutingRequest(
        context=borrow.context,
        runtime_instance=borrow.runtime_instance,
        deadline_monotonic=borrow.deadline_monotonic,
        assert_current=borrow.assert_current,
        role=role,
        input=payload if payload is not None else {"decision_time": 100.25},
    )


def _ai_stream_request(borrow: _Borrow) -> ipc.NativeAiRoutingRequest:
    return replace(_ai_routing_request(borrow, "stream", {
        "request_id": " stream.fixture ",
        "provider_attempt": 2,
        "value": {"events": [{"type": "finish", "usage": {"cost": 6.5}}]},
    }), stream_result=True)


_STREAM_RESULT = {"events": [{
    "request_id": "stream.fixture", "sequence": 0, "type": "finish",
    "delta": None, "tool_intent": None, "usage": {"cost": 6.5},
    "finish_reason": None, "error_code": None, "provider_attempt": 2,
}]}


def _ai_gateway_result_fixture(
    role: ipc.ModelResolveRole,
) -> tuple[ipc.NativeAiRoutingRequest, dict[str, Any]]:
    candidate = {
        "model_id": "模型", "provider_instance_id": "provider.fixture",
        "catalog_provider_instance_id": "catalog.fixture", "catalog_revision": "r1",
    }
    value = {"output": ["例", 2**70, 0.125, -0.0], "finish_reason": "stop"}
    payload = {
        "role": role, "value": value, "request_id": "request.fixture",
        "candidate": candidate,
    }
    result = {
        "status": "ok", "request_id": payload["request_id"],
        "model_id": candidate["model_id"], "provider_instance_id": "provider.fixture",
        **value, "tool_intents": [],
    }
    if role == "stream":
        additions = {
            "tool_intents": [],
            "events": [{"type": "finish", "finish_reason": "stop"}],
            "attempts": [{"attempt": 1, "error_code": "unavailable"}],
        }
        payload.update(additions)
        result.update(additions, delivery_mode="buffered")
    else:
        result.update(
            candidate, pricing_revision="r1", usage={},
            usage_provenance="provider_reported",
        )
    return replace(
        _ai_routing_request(_Borrow(), role, payload), gateway_result=True,
    ), result


@pytest.mark.parametrize("role", ["generate", "stream"])
def test_ai_gateway_result_uses_its_own_route_and_preserves_lossless_json(
    monkeypatch: pytest.MonkeyPatch, role: ipc.ModelResolveRole,
) -> None:
    borrow, result = _ai_gateway_result_fixture(role)
    expected_input = json.dumps(
        dict(borrow.input), ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")

    def serve(peer: socket.socket) -> None:
        tag, body = _receive(peer)
        assert tag == ipc._START
        assert json.loads(body)["ai_route"] == {
            "role": role, "input_bytes": len(expected_input), "gateway_result": True,
        }
        _send(peer, ipc._BODY_READY)
        assert _receive(peer) == (ipc._ORCHESTRATION_INPUT, expected_input)
        _send(peer, ipc._CHECK)
        assert _receive(peer) == (ipc._LIVE, b"")
        _send(peer, ipc._PLAN, json.dumps(result, ensure_ascii=False).encode("utf-8"))
        assert peer.recv(1) == b""

    monkeypatch.setenv(ipc._AI_ROUTING_ENV, "1")
    monkeypatch.delenv(ipc._AI_GATEWAY_RESULT_ENV, raising=False)
    with _served(monkeypatch, serve):
        assert ipc.capture_launcher_ai_gateway_result_projector() is None
        route = ipc.capture_launcher_ai_routing_evaluator()
        assert route is not None
        with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
            route(borrow)
        monkeypatch.setenv(ipc._AI_GATEWAY_RESULT_ENV, "1")
        project = ipc.capture_launcher_ai_gateway_result_projector()
        assert project is not None
        for invalid in (
            replace(borrow, role="quote-generate"),
            replace(borrow, stream_result=True),
            replace(borrow, usage_cost=True),
            replace(borrow, tool_intents=True),
            replace(borrow, pipeline="prepare"),
            replace(borrow, input={**borrow.input, "role": "quote-stream"}),
            replace(borrow, input={**borrow.input, "execute": True}),
        ):
            with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
                project(invalid)
        for value in ({1: "json key"}, ("tuple",), "x" * ipc._AI_ROUTING_LIMIT):
            with pytest.raises(ipc.NativeProviderRegistryRequiresLegacyRead):
                project(replace(borrow, input={**borrow.input, "value": value}))
        projected = project(borrow)
        assert projected == result
        assert projected["output"][-1].hex() == "-0x0.0p+0"


@pytest.mark.parametrize("terminal", [
    "inapplicable", "legacy-read-required", "denied", "unavailable", "malformed",
])
def test_ai_gateway_result_preserves_the_terminal_frame(
    monkeypatch: pytest.MonkeyPatch, terminal: str,
) -> None:
    borrow, result = _ai_gateway_result_fixture("generate")

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == ipc._START
        if terminal == "inapplicable":
            _send(peer, ipc._INAPPLICABLE)
        else:
            _send(peer, ipc._BODY_READY)
            assert _receive(peer)[0] == ipc._ORCHESTRATION_INPUT
            _send(peer, ipc._CHECK)
            assert _receive(peer) == (ipc._LIVE, b"")
            if terminal == "malformed":
                malformed = json.dumps({**result, "executes": True}).encode()
                _send(peer, ipc._PLAN, malformed)
            else:
                _send(peer, ipc._FAILURE, terminal.encode())
        assert peer.recv(1) == b""

    failures = {
        "inapplicable": ipc.NativeProviderPrepareUnavailable,
        "legacy-read-required": ipc.NativeProviderRegistryRequiresLegacyRead,
        "malformed": ipc._ProtocolError,
    }
    monkeypatch.setenv(ipc._AI_GATEWAY_RESULT_ENV, "1")
    with _served(monkeypatch, serve):
        project = ipc.capture_launcher_ai_gateway_result_projector()
        assert project is not None
        with pytest.raises(failures.get(terminal, ipc.NativeAiRoutingFailure)) as error:
            project(borrow)
        if isinstance(error.value, ipc.NativeAiRoutingFailure):
            assert error.value.code == terminal


def _ai_gateway_policy_fixture(
    role: ipc.ModelResolveRole, phase: str,
) -> tuple[ipc.NativeAiRoutingRequest, dict[str, Any]]:
    if phase == "model-profile":
        parameters = {"ordered": ["例", 2**100, -0.0, 1.25, True, None]}
        payload = {
            "phase": phase, "identifier": "alias", "resolved_profile_id": "saved",
            "model_id": "模型", "profile_requirements": {"minimum_context": 256},
            "request_requirements": {}, "profile_parameters": parameters,
            "request_parameters": {},
        }
        result = {
            "model_profile_id": "saved",
            "requirements": {"minimum_context": 256, "preferred_model_id": "模型"},
            "parameters": parameters,
        }
    else:
        payload = {"phase": phase, "requirements": {"maximum_cost": -0.0}}
        result = {
            "modalities": ["image", "text"], "capabilities": [], "tool_calling": True,
            "thinking": False, "minimum_context": 2**80, "request_surface": "chat",
            "data_residency": None, "maximum_cost": -0.0, "preferred_model_id": "模型",
            "preferred_provider_id": None, "preferred_provider_instance_id": None,
            "health_max_age": 60.0,
        }
    return replace(
        _ai_routing_request(_Borrow(), role, payload), gateway_policy=True,
    ), result


@pytest.mark.parametrize(
    "role", ["generate", "stream", "quote-generate", "quote-stream"],
)
@pytest.mark.parametrize("phase", ["model-profile", "route-requirement"])
def test_ai_gateway_policy_preserves_phase_role_and_lossless_json(
    monkeypatch: pytest.MonkeyPatch, role: ipc.ModelResolveRole, phase: str,
) -> None:
    borrow, result = _ai_gateway_policy_fixture(role, phase)

    def encode(value: Mapping[str, Any]) -> bytes:
        return json.dumps(
            dict(value), ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")

    expected_input = encode(borrow.input)

    def serve(peer: socket.socket) -> None:
        tag, body = _receive(peer)
        assert tag == ipc._START
        assert json.loads(body)["ai_route"] == {
            "role": role, "input_bytes": len(expected_input), "gateway_policy": True,
        }
        _send(peer, ipc._BODY_READY)
        assert _receive(peer) == (ipc._ORCHESTRATION_INPUT, expected_input)
        _send(peer, ipc._CHECK)
        assert _receive(peer) == (ipc._LIVE, b"")
        _send(peer, ipc._PLAN, encode(result))
        assert peer.recv(1) == b""

    monkeypatch.setenv(ipc._AI_ROUTING_ENV, "1")
    monkeypatch.setenv(ipc._AI_GATEWAY_RESULT_ENV, "1")
    monkeypatch.delenv(ipc._AI_GATEWAY_POLICY_ENV, raising=False)
    with _served(monkeypatch, serve):
        assert ipc.capture_launcher_ai_gateway_policy_projector() is None
        for capture in (
            ipc.capture_launcher_ai_routing_evaluator,
            ipc.capture_launcher_ai_gateway_result_projector,
        ):
            unrelated_probe = capture()
            assert unrelated_probe is not None
            with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
                unrelated_probe(borrow)
        monkeypatch.setenv(ipc._AI_GATEWAY_POLICY_ENV, "1")
        project = ipc.capture_launcher_ai_gateway_policy_projector()
        assert project is not None
        for invalid in (
            replace(borrow, role="route-quote"),
            replace(borrow, stream_result=True),
            replace(borrow, usage_cost=True),
            replace(borrow, tool_intents=True),
            replace(borrow, pipeline="prepare"),
            replace(borrow, gateway_result=True),
            replace(borrow, input={**borrow.input, "role": role}),
            replace(borrow, input={**borrow.input, "phase": "custom"}),
        ):
            with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
                project(invalid)
        field = "profile_parameters" if phase == "model-profile" else "requirements"
        for unsupported in (
            {1: "json key"}, ("tuple",), float("nan"), "\ud800",
            "x" * ipc._AI_ROUTING_LIMIT,
        ):
            with pytest.raises(ipc.NativeProviderRegistryRequiresLegacyRead):
                project(replace(borrow, input={**borrow.input, field: unsupported}))
        projected = project(borrow)
        assert encode(projected) == encode(result)
        number = (
            projected["parameters"]["ordered"][2]
            if phase == "model-profile"
            else projected["maximum_cost"]
        )
        assert number.hex() == "-0x0.0p+0"


@pytest.mark.parametrize(("terminal", "failure_type"), [
    ("inapplicable", ipc.NativeProviderPrepareUnavailable),
    ("legacy-read-required", ipc.NativeProviderRegistryRequiresLegacyRead),
    ("invalid", ipc.NativeAiRoutingFailure),
    ("denied", ipc.NativeAiRoutingFailure),
    ("unavailable", ipc.NativeAiRoutingFailure),
    ("malformed", ipc._ProtocolError),
])
def test_ai_gateway_policy_preserves_terminal_or_declines(
    monkeypatch: pytest.MonkeyPatch, terminal: str, failure_type: type[Exception],
) -> None:
    borrow, result = _ai_gateway_policy_fixture("quote-stream", "route-requirement")

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == ipc._START
        if terminal == "inapplicable":
            _send(peer, ipc._INAPPLICABLE)
        else:
            _send(peer, ipc._BODY_READY)
            assert _receive(peer)[0] == ipc._ORCHESTRATION_INPUT
            _send(peer, ipc._CHECK)
            assert _receive(peer) == (ipc._LIVE, b"")
            if terminal == "malformed":
                _send(peer, ipc._PLAN, json.dumps({**result, "thinking": 1}).encode())
            else:
                _send(peer, ipc._FAILURE, terminal.encode())
        assert peer.recv(1) == b""

    monkeypatch.setenv(ipc._AI_GATEWAY_POLICY_ENV, "1")
    with _served(monkeypatch, serve):
        project = ipc.capture_launcher_ai_gateway_policy_projector()
        assert project is not None
        with pytest.raises(failure_type) as error:
            project(borrow)
        if isinstance(error.value, ipc.NativeAiRoutingFailure):
            assert error.value.code == terminal


def test_ai_stream_result_uses_its_own_opt_in_and_preserves_signed_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrow = _ai_stream_request(_Borrow())
    expected_input = json.dumps(dict(borrow.input), separators=(",", ":")).encode()

    def serve(peer: socket.socket) -> None:
        tag, body = _receive(peer)
        assert tag == ipc._START
        start = json.loads(body)
        assert start.pop("ai_route") == {
            "role": "stream", "input_bytes": len(expected_input), "stream_result": True,
        }
        assert set(start) == {
            "identity", "security_epoch", "runtime_instance", "deadline_ns",
        }
        _send(peer, ipc._BODY_READY)
        assert _receive(peer) == (ipc._ORCHESTRATION_INPUT, expected_input)
        _send(peer, ipc._CHECK)
        assert _receive(peer) == (ipc._LIVE, b"")
        _send(peer, ipc._PLAN, json.dumps(_STREAM_RESULT).encode())
        assert peer.recv(1) == b""

    monkeypatch.delenv(ipc._AI_ROUTING_ENV, raising=False)
    with _served_ai_routing(monkeypatch, serve, stream_result=True) as normalize:
        assert ipc.capture_launcher_ai_routing_evaluator() is None
        monkeypatch.delenv(ipc._AI_STREAM_RESULT_ENV)
        monkeypatch.setenv(ipc._AI_ROUTING_ENV, "1")
        assert ipc.capture_launcher_ai_stream_result_normalizer() is None
        evaluate = ipc.capture_launcher_ai_routing_evaluator()
        assert evaluate is not None
        with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
            evaluate(borrow)
        with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
            normalize(replace(borrow, role="generate"))
        assert normalize(borrow) == _STREAM_RESULT


@pytest.mark.parametrize("role", ["quote-generate", "quote-stream", "generate", "stream"])
def test_ai_routing_contract_preserves_signed_input_and_finite_result(
    monkeypatch: pytest.MonkeyPatch, role: ipc.ModelResolveRole,
) -> None:
    borrow = _Borrow()
    payload = {"models": [{"model_id": "模型", "input_cost": 0.25}], "decision_time": 6.5}
    expected_input = (
        '{"models":[{"model_id":"模型","input_cost":0.25}],"decision_time":6.5}'.encode()
    )
    candidate = {"model_id": "模型", "input_cost": 0.25}
    expected = {
        "selected": candidate, "candidates": [candidate], "excluded": [],
        "deterministic": True,
    }

    def serve(peer: socket.socket) -> None:
        tag, body = _receive(peer)
        assert tag == ipc._START
        start = json.loads(body)
        assert start.pop("ai_route") == {"role": role, "input_bytes": len(expected_input)}
        assert set(start) == {
            "identity", "security_epoch", "runtime_instance", "deadline_ns",
        }
        assert start["runtime_instance"] == borrow.runtime_instance
        peer.settimeout(0.02)
        with pytest.raises(TimeoutError):
            peer.recv(1)
        peer.settimeout(5)
        _send(peer, ipc._BODY_READY)
        assert _receive(peer) == (ipc._ORCHESTRATION_INPUT, expected_input)
        for _ in range(2):
            _send(peer, ipc._CHECK)
            assert _receive(peer) == (ipc._LIVE, b"")
        _send(peer, ipc._PLAN, json.dumps(expected).encode())
        assert peer.recv(1) == b""

    with _served_ai_routing(monkeypatch, serve) as evaluate:
        monkeypatch.delenv(ipc._AI_ROUTING_ENV)
        assert ipc.capture_launcher_ai_routing_evaluator() is None
        assert evaluate(_ai_routing_request(borrow, role, payload)) == expected


@pytest.mark.parametrize("role", ["generate", "stream"])
def test_ai_tool_intents_preserves_the_signed_payload_and_inert_result(
    monkeypatch: pytest.MonkeyPatch, role: ipc.ModelResolveRole,
) -> None:
    from ecosystem.rumi_ai_tool_bridge_pack.runtime.bridge import (
        create_tool_intent_operation,
    )

    signed_input = {
        "request_id": "\u3000request.fixture\t",
        "intents": [
            {
                "id": "提供者の識別子",
                "function": {
                    "name": " \ttool.fixture:render ",
                    "arguments": '{"label":"例","integer":1208925819614629174706176}',
                },
                "approved": True,
                "authority_granted": True,
                "executes": True,
            },
            {"name": "fixture.other", "arguments": {"approved": True}},
        ],
    }
    request = replace(_ai_routing_request(_Borrow(), role, signed_input), tool_intents=True)
    expected_input = json.dumps(signed_input, ensure_ascii=False, separators=(",", ":"))
    expected_input = expected_input.encode()
    expected = create_tool_intent_operation(None)("normalize", signed_input)

    def serve(peer: socket.socket) -> None:
        tag, body = _receive(peer)
        assert tag == ipc._START
        start = json.loads(body)
        assert start.pop("ai_route") == {
            "role": role, "input_bytes": len(expected_input), "tool_intents": True,
        }
        assert set(start) == {
            "identity", "security_epoch", "runtime_instance", "deadline_ns",
        }
        _send(peer, ipc._BODY_READY)
        assert _receive(peer) == (ipc._ORCHESTRATION_INPUT, expected_input)
        _send(peer, ipc._CHECK)
        assert _receive(peer) == (ipc._LIVE, b"")
        _send(peer, ipc._PLAN, json.dumps(expected).encode())
        assert peer.recv(1) == b""

    for env in (
        ipc._AI_ROUTING_ENV, ipc._AI_STREAM_RESULT_ENV,
        ipc._AI_USAGE_COST_ENV, ipc._AI_TOOL_INTENTS_ENV,
    ):
        monkeypatch.delenv(env, raising=False)
    with _served(monkeypatch, serve):
        assert ipc.capture_launcher_ai_tool_intents_normalizer() is None
        monkeypatch.setenv(ipc._AI_TOOL_INTENTS_ENV, "1")
        normalize = ipc.capture_launcher_ai_tool_intents_normalizer()
        assert normalize is not None
        assert ipc.capture_launcher_ai_routing_evaluator() is None
        assert ipc.capture_launcher_ai_stream_result_normalizer() is None
        assert ipc.capture_launcher_ai_usage_cost_calculator() is None
        for invalid in (
            replace(request, stream_result=True),
            replace(request, usage_cost=True),
            replace(request, role="quote-stream"),
            replace(request, tool_intents=False),
            replace(request, input={"intents": []}),
        ):
            with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
                normalize(invalid)
        result = normalize(request)
    assert result == expected
    for intent in result["intents"]:
        assert intent["authority_granted"] is False
        assert intent["approved"] is False
        assert intent["approval_status"] == "unrequested"
        assert intent["executes"] is False


@pytest.mark.parametrize(
    "replace_fields",
    [
        {"authority_granted": True},
        {"approved": 0},
        {"approved": True},
        {"approval_status": "approved"},
        {"executes": True},
        {"request_id": "another.request"},
        {"intent_id": 1},
        {"operation": []},
        {"arguments": []},
        {"unexpected": False},
    ],
)
def test_ai_tool_intents_result_rejects_authority_and_invalid_fields(
    replace_fields: Mapping[str, Any],
) -> None:
    signed_input = {"request_id": "request", "intents": [{"name": "fixture"}]}
    request = replace(
        _ai_routing_request(_Borrow(), "generate", signed_input), tool_intents=True,
    )
    inert = {
        "intent_id": "request:tool:0", "request_id": "request", "operation": "fixture",
        "arguments": {}, "authority_granted": False, "approved": False,
        "approval_status": "unrequested", "executes": False,
    }
    result = {"intents": [{**inert, **replace_fields}]}
    with pytest.raises(ipc._ProtocolError, match="tool intent normalization"):
        ipc._decode_ai_tool_intents(json.dumps(result).encode(), request)


@pytest.mark.parametrize("stream_result", [False, True], ids=["routing", "normalizer"])
@pytest.mark.parametrize(
    ("phase", "tag", "payload", "expected"),
    [
        ("start", 10, b"", ipc.NativeProviderPrepareUnavailable),
        ("start", 8, b"legacy-read-required", ipc._ProtocolError),
        ("body", 8, b"legacy-read-required", ipc._ProtocolError),
        ("start", 8, b"denied", ipc.NativeAiRoutingFailure),
        ("start", 8, b"unavailable", ipc.NativeAiRoutingFailure),
        ("live", 8, b"legacy-read-required", ipc.NativeProviderRegistryRequiresLegacyRead),
        ("live", 8, b"invalid", ipc.NativeAiRoutingFailure),
        ("live", 8, b"denied", ipc.NativeAiRoutingFailure),
        ("live", 8, b"unavailable", ipc.NativeAiRoutingFailure),
        ("live", 8, b"limit-exceeded", ipc.NativeAiRoutingFailure),
        ("live", 7, b"{}", ipc._ProtocolError),
        ("live", 7, b'{"selected":null,"selected":false}', ipc._ProtocolError),
    ],
)
def test_ai_routing_accepts_only_preflight_inapplicability_and_live_decline(
    monkeypatch: pytest.MonkeyPatch, phase: str, tag: int, payload: bytes,
    expected: type[RuntimeError], stream_result: bool,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == ipc._START
        if phase != "start":
            _send(peer, ipc._BODY_READY)
            assert _receive(peer)[0] == ipc._ORCHESTRATION_INPUT
        if phase == "live":
            _send(peer, ipc._CHECK)
            assert _receive(peer) == (ipc._LIVE, b"")
        _send(peer, tag, payload)
        assert peer.recv(1) == b""

    with (
        _served_ai_routing(monkeypatch, serve, stream_result=stream_result) as evaluate,
        pytest.raises(expected) as failure,
    ):
        borrow = _Borrow()
        evaluate(_ai_stream_request(borrow) if stream_result else _ai_routing_request(borrow))
    if isinstance(failure.value, ipc.NativeAiRoutingFailure):
        assert failure.value.code == payload.decode()


@pytest.mark.parametrize("stream_result", [False, True], ids=["routing", "normalizer"])
def test_ai_routing_declines_unrepresentable_input_before_connection(
    monkeypatch: pytest.MonkeyPatch, stream_result: bool,
) -> None:
    if stream_result:
        monkeypatch.setenv(ipc._AI_STREAM_RESULT_ENV, "1")
        capture = ipc.capture_launcher_ai_stream_result_normalizer
    else:
        monkeypatch.setenv(ipc._AI_ROUTING_ENV, "1")
        capture = ipc.capture_launcher_ai_routing_evaluator
    monkeypatch.setenv(ipc._SOCKET_ENV, "/fixture.sock")
    monkeypatch.setenv(ipc._OWNER_ENV, _owner())
    monkeypatch.setattr(ipc.os, "getppid", os.getpid)
    evaluate = capture()
    assert evaluate is not None
    fixture = _Borrow()
    borrow = _ai_stream_request(fixture) if stream_result else _ai_routing_request(fixture)
    with pytest.raises(ipc.NativeProviderRegistryRequiresLegacyRead):
        evaluate(replace(borrow, input={**borrow.input, "large": "x" * (1024 * 1024)}))
    with pytest.raises(ipc.NativeProviderRegistryRequiresLegacyRead):
        evaluate(replace(borrow, input={**borrow.input, "float": math.inf}))


@pytest.mark.parametrize("stream_result", [False, True], ids=["routing", "normalizer"])
def test_ai_routing_rejects_reply_after_live_revocation(
    monkeypatch: pytest.MonkeyPatch, stream_result: bool,
) -> None:
    borrow = _Borrow()
    receive = ipc._Exchange.receive

    def receive_and_revoke(
        exchange: ipc._Exchange, wait_seconds: Callable[[], float],
    ) -> tuple[int, bytes]:
        frame = receive(exchange, wait_seconds)
        if frame[0] == ipc._PLAN:
            borrow.revoked.set()
        return frame

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == ipc._START
        _send(peer, ipc._BODY_READY)
        assert _receive(peer)[0] == ipc._ORCHESTRATION_INPUT
        _send(peer, ipc._CHECK)
        assert _receive(peer) == (ipc._LIVE, b"")
        result = b'{"selected":null,"candidates":[],"excluded":[],"deterministic":true}'
        _send(peer, ipc._PLAN, json.dumps(_STREAM_RESULT).encode() if stream_result else result)
        assert peer.recv(1) == b""

    monkeypatch.setattr(ipc._Exchange, "receive", receive_and_revoke)
    with (
        _served_ai_routing(monkeypatch, serve, stream_result=stream_result) as evaluate,
        pytest.raises(PermissionError, match="fixture lease revoked"),
    ):
        evaluate(_ai_stream_request(borrow) if stream_result else _ai_routing_request(borrow))


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


@pytest.mark.parametrize("tag", [ipc._BODY_READY, ipc._CHECK])
def test_saved_orchestration_guards_reply_before_its_first_byte(
    monkeypatch: pytest.MonkeyPatch, tag: int,
) -> None:
    borrow = _Borrow()
    receive = ipc._Exchange.receive

    def receive_and_revoke(
        exchange: ipc._Exchange, wait_seconds: Callable[[], float],
    ) -> tuple[int, bytes]:
        frame = receive(exchange, wait_seconds)
        if frame == (tag, b""):
            borrow.revoked.set()
        return frame

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == ipc._START
        if tag == ipc._CHECK:
            _send(peer, ipc._BODY_READY)
            assert _receive(peer)[0] == ipc._ORCHESTRATION_INPUT
        _send(peer, tag)
        assert _receive(peer) == (ipc._CANCEL, b"")
        _send(peer, ipc._FAILURE, b"cancelled")
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
    monkeypatch: pytest.MonkeyPatch,
    serve: Callable[[socket.socket], None],
    *,
    version: str = "1",
) -> Iterator[Callable[[ipc.NativeSavedLocalChatRequest], bytes | dict[str, Any]]]:
    monkeypatch.setenv(ipc._SAVED_LOCAL_CHAT_ENV, version)
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
    for version in ("1", "2", "3", "4", "5"):
        monkeypatch.setenv(ipc._SAVED_LOCAL_CHAT_ENV, version)
        execute = ipc.capture_launcher_saved_local_chat_executor()
        assert execute is not None
        assert execute.supports_request_compilation is (version in ("3", "4", "5"))
        assert execute.supports_policy_compilation is (version in ("4", "5"))
        assert execute.supports_side_chat is (version == "5")
        if not execute.supports_side_chat:
            with pytest.raises(ipc.NativeProviderPrepareUnavailable):
                execute(_saved_local_chat_request(_Borrow(), role="side-chat-generate"))


@pytest.mark.parametrize("version", ["1", "2", "3", "4"])
@pytest.mark.parametrize("role", ["generate", "stream"])
def test_saved_local_chat_passes_exact_bytes_and_permits_dispatch_once(
    monkeypatch: pytest.MonkeyPatch, role: ipc.SavedLocalChatRole, version: str,
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
        expected = {
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
        if version in ("2", "3", "4"):
            expected["saved_local_chat"]["project_response"] = True
        assert payload == json.dumps(
            expected, ensure_ascii=False, separators=(",", ":"),
        ).encode()
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

    with _served_saved_local_chat(monkeypatch, serve, version=version) as execute:
        monkeypatch.delenv(ipc._SAVED_LOCAL_CHAT_ENV)
        monkeypatch.delenv(ipc._SOCKET_ENV)
        monkeypatch.delenv(ipc._OWNER_ENV)
        assert execute(request) == reply


@pytest.mark.parametrize("version", ["3", "4"])
@pytest.mark.parametrize(
    ("phase", "compile_request", "exception"),
    [
        ("start", True, ipc.NativeSavedLocalChatFailure),
        ("body", True, ipc.NativeProviderPrepareUnavailable),
        ("live", True, ipc.NativeProviderPrepareUnavailable),
        ("permit", True, ipc.NativeSavedLocalChatFailure),
        ("live", False, ipc._ProtocolError),
    ],
)
def test_saved_local_chat_allows_legacy_request_only_before_dispatch_offer(
    monkeypatch: pytest.MonkeyPatch, version: str, phase: str, compile_request: bool,
    exception: type[RuntimeError],
) -> None:
    body = '{"messages":[{"role":"user","content":"ローカル"}],"model_id":"m"}'.encode()
    dispatches: list[bool] = []
    request = replace(
        _saved_local_chat_request(_Borrow(), body=body),
        compile_request=compile_request,
        compile_policy=version == "4" and compile_request,
        mark_dispatch_possible=lambda: dispatches.append(True),
    )

    def serve(peer: socket.socket) -> None:
        tag, payload = _receive(peer)
        assert tag == 1
        expected = {
            "role": "generate",
            "connection_id": "fixture-local",
            "record_revision": 7,
            "endpoint": "http://127.0.0.1:11434/v1",
            "input_bytes": len(body),
            "project_response": True,
        }
        if compile_request:
            expected["compile_request"] = True
        if request.compile_policy:
            expected["compile_policy"] = True
        assert json.loads(payload)["saved_local_chat"] == expected
        if phase != "start":
            _accept_saved_local_chat_body(peer, body)
        if phase in ("live", "permit"):
            _check_saved_local_chat_live(peer)
        if phase == "permit":
            _send(peer, 17)
            assert _receive(peer) == (18, b"")
        _send(peer, 8, b"legacy-request")
        if exception is ipc._ProtocolError:
            _confirm_saved_local_chat_cancellation(peer)
        else:
            assert peer.recv(1) == b""

    with (
        _served_saved_local_chat(monkeypatch, serve, version=version) as execute,
        pytest.raises(exception) as failure,
    ):
        execute(request)
    if isinstance(failure.value, ipc.NativeSavedLocalChatFailure):
        assert failure.value.code == "legacy-request"
    assert bool(dispatches) is (phase == "permit")


@pytest.mark.parametrize(
    ("compile_policy", "phase", "code", "exception"),
    [
        (True, "source", b"policy-model-denied", ipc.NativeSavedLocalChatFailure),
        (True, "source", b"policy-model-invalid", ipc.NativeSavedLocalChatFailure),
        (True, "source", b"policy-parameters-invalid", ipc.NativeSavedLocalChatFailure),
        (False, "source", b"policy-model-invalid", ipc._ProtocolError),
        (True, "start", b"policy-model-denied", ipc._ProtocolError),
        (True, "permit", b"policy-model-denied", ipc._ProtocolError),
    ],
)
def test_saved_local_chat_accepts_policy_failures_only_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    compile_policy: bool,
    phase: str,
    code: bytes,
    exception: type[RuntimeError],
) -> None:
    dispatches: list[bool] = []
    request = replace(
        _saved_local_chat_request(_Borrow(), body=b'{"model_id":"fixture"}'),
        compile_request=True,
        compile_policy=compile_policy,
        mark_dispatch_possible=lambda: dispatches.append(True),
    )

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        if phase != "start":
            _accept_saved_local_chat_body(peer, request.input)
        if phase == "permit":
            _check_saved_local_chat_live(peer)
            _send(peer, 17)
            assert _receive(peer) == (18, b"")
        _send(peer, 8, code)
        if not compile_policy:
            assert _receive(peer) == (9, b"")
            assert peer.recv(1) == b""
        elif exception is ipc._ProtocolError:
            _confirm_saved_local_chat_cancellation(peer)
        else:
            assert peer.recv(1) == b""

    with (
        _served_saved_local_chat(monkeypatch, serve, version="4") as execute,
        pytest.raises(exception) as failure,
    ):
        execute(request)
    if isinstance(failure.value, ipc.NativeSavedLocalChatFailure):
        assert failure.value.code == code.decode("ascii")
    assert bool(dispatches) is (phase == "permit")


@pytest.mark.parametrize(("version", "compile_request"), [("3", True), ("4", False)])
def test_saved_local_chat_rejects_policy_without_negotiated_source(
    monkeypatch: pytest.MonkeyPatch, version: str, compile_request: bool,
) -> None:
    request = replace(
        _saved_local_chat_request(_Borrow()),
        compile_request=compile_request,
        compile_policy=True,
    )
    monkeypatch.setenv(ipc._SAVED_LOCAL_CHAT_ENV, version)
    monkeypatch.setenv(ipc._SOCKET_ENV, "/fixture.sock")
    monkeypatch.setenv(ipc._OWNER_ENV, _owner())
    monkeypatch.setattr(ipc.os, "getppid", os.getpid)
    execute = ipc.capture_launcher_saved_local_chat_executor()
    assert execute is not None
    with pytest.raises(ipc.NativeSavedLocalChatFailure) as failure:
        execute(request)
    assert failure.value.code == "invalid"


@pytest.mark.parametrize(
    "role", ["generate", "stream", "side-chat-generate", "side-chat-stream"],
)
def test_saved_local_chat_decodes_projected_terminal_after_permit(
    monkeypatch: pytest.MonkeyPatch, role: ipc.SavedLocalChatRole,
) -> None:
    request = _saved_local_chat_request(_Borrow(), role=role)
    expected: dict[str, Any] = {
        "output": False,
        "tool_intents": [{"id": "call_1"}, 0],
        "usage": {"fixture": {"large": -int("7" * 4300)}},
        "finish_reason": 0,
    }
    if role in ("stream", "side-chat-stream"):
        expected["events"] = [
            {"type": "tool_intent_delta", "tool_intent": {"id": "call_1"}},
            {"type": "usage", "usage": expected["usage"]},
            {"type": "finish", "finish_reason": 0},
        ]

    def serve(peer: socket.socket) -> None:
        tag, start = _receive(peer)
        assert tag == 1
        assert json.loads(start)["saved_local_chat"]["role"] == role
        _accept_saved_local_chat_body(peer, request.input)
        _check_saved_local_chat_live(peer)
        _send(peer, 17)
        assert _receive(peer) == (18, b"")
        _send(peer, 19, json.dumps(expected, separators=(",", ":")).encode())
        assert peer.recv(1) == b""

    version = "5" if role.startswith("side-chat-") else "2"
    with _served_saved_local_chat(monkeypatch, serve, version=version) as execute:
        result = execute(request)
    assert isinstance(result, dict)
    assert result == expected
    assert type(result["output"]) is bool
    assert type(result["finish_reason"]) is int


@pytest.mark.parametrize(
    ("version", "role", "response", "exception_type"),
    [
        (
            "1", "generate",
            b'{"output":"","usage":{},"tool_intents":[],"finish_reason":null}',
            ipc.NativeSavedLocalChatDispatchUnconfirmed,
        ),
        ("2", "generate", b"not JSON", ipc._ProtocolError),
        (
            "2", "generate",
            b'{"output":"","usage":{},"tool_intents":[],"finish_reason":null,"events":[]}',
            ipc._ProtocolError,
        ),
        (
            "2", "stream",
            b'{"output":"","usage":{},"tool_intents":[],"finish_reason":null}',
            ipc._ProtocolError,
        ),
        (
            "2", "generate",
            b'{"output":"","usage":{},"tool_intents":{},"finish_reason":null}',
            ipc._ProtocolError,
        ),
    ],
)
def test_saved_local_chat_rejects_invalid_or_unrequested_projection(
    monkeypatch: pytest.MonkeyPatch, version: str, role: ipc.SavedLocalChatRole,
    response: bytes, exception_type: type[RuntimeError],
) -> None:
    request = _saved_local_chat_request(_Borrow(), role=role)

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _accept_saved_local_chat_body(peer, request.input)
        _check_saved_local_chat_live(peer)
        _send(peer, 17)
        assert _receive(peer) == (18, b"")
        _send(peer, 19, response)
        if version == "1":
            assert _receive(peer) == (9, b"")
        else:
            assert peer.recv(1) == b""

    with (
        _served_saved_local_chat(monkeypatch, serve, version=version) as execute,
        pytest.raises(exception_type),
    ):
        execute(request)


@pytest.mark.parametrize(
    ("version", "compile_request", "body", "code"),
    [
        ("1", False, b"", "invalid"),
        ("1", False, b"x" * (1024 * 1024 + 1), "limit-exceeded"),
        ("2", True, b'{"model_id":"m"}', "invalid"),
        ("3", True, b"", "invalid"),
        ("3", True, b"x" * (1024 * 1024 + 1), "limit-exceeded"),
    ],
)
def test_saved_local_chat_rejects_out_of_bounds_body_before_connection(
    monkeypatch: pytest.MonkeyPatch, version: str, compile_request: bool,
    body: bytes, code: str,
) -> None:
    class UnexpectedSocket:
        def __init__(self, *args: Any) -> None:
            pytest.fail("invalid input reached the Launcher socket")

    monkeypatch.setenv(ipc._SAVED_LOCAL_CHAT_ENV, version)
    monkeypatch.setenv(ipc._SOCKET_ENV, "/fixture.sock")
    monkeypatch.setenv(ipc._OWNER_ENV, _owner())
    monkeypatch.setattr(ipc.os, "getppid", os.getpid)
    monkeypatch.setattr(ipc.socket, "socket", UnexpectedSocket)
    execute = ipc.capture_launcher_saved_local_chat_executor()
    assert execute is not None
    with pytest.raises(ipc.NativeSavedLocalChatFailure) as failure:
        execute(replace(
            _saved_local_chat_request(_Borrow(), body=body),
            compile_request=compile_request,
        ))
    assert failure.value.code == code


@pytest.mark.parametrize(
    ("phase", "tag", "payload", "exception_type"),
    [
        ("start", 10, b"", ipc.NativeProviderPrepareUnavailable),
        ("start", 8, b"unavailable", ipc.NativeSavedLocalChatFailure),
        ("body", 8, b"invalid", ipc.NativeSavedLocalChatFailure),
        ("permit", 8, b"limit-exceeded", ipc.NativeSavedLocalChatFailure),
        ("permit", 8, b"invalid-response", ipc.NativeSavedLocalChatFailure),
        ("permit", 8, b"cancelled", ipc.NativeSavedLocalChatFailure),
        ("body", 10, b"", ipc._ProtocolError),
        ("body", 17, b"", ipc._ProtocolError),
        ("live", 7, b"premature", ipc._ProtocolError),
        ("live", 19, b"premature", ipc._ProtocolError),
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
        _served_saved_local_chat(
            monkeypatch, serve, version="2" if tag == 19 else "1",
        ) as execute,
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


@pytest.mark.parametrize("oversized_tag", [None, 7, 19])
def test_saved_local_chat_reports_unconfirmed_dispatch_without_cleanup_proof(
    monkeypatch: pytest.MonkeyPatch, oversized_tag: int | None,
) -> None:
    request = _saved_local_chat_request(_Borrow())

    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == 1
        _accept_saved_local_chat_body(peer, request.input)
        _check_saved_local_chat_live(peer)
        _send(peer, 17)
        assert _receive(peer) == (18, b"")
        if oversized_tag is not None:
            peer.sendall(_HEADER.pack(oversized_tag, 4 * 1024 * 1024 + 1))
            assert _receive(peer) == (9, b"")
            assert peer.recv(1) == b""

    with (
        _served_saved_local_chat(
            monkeypatch, serve, version="2" if oversized_tag == 19 else "1",
        ) as execute,
        pytest.raises(ipc.NativeSavedLocalChatDispatchUnconfirmed),
    ):
        execute(request)


@pytest.mark.parametrize("terminal_tag", [7, 8, 19])
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
        payload = b"cancelled" if terminal_tag == 8 else b"late successful response"
        _send(peer, terminal_tag, payload)
        assert peer.recv(1) == b""

    with (
        _served_saved_local_chat(
            monkeypatch, serve, version="2" if terminal_tag == 19 else "1",
        ) as execute,
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


@pytest.mark.parametrize("role", ["generate", "stream"])
def test_ai_usage_cost_preserves_the_signed_payload_and_result(
    monkeypatch: pytest.MonkeyPatch, role: ipc.ModelResolveRole,
) -> None:
    signed_input = {
        "usage": {"prompt_tokens": "3.5", "completion_tokens": 2},
        "pricing": {"input": 0.1, "output": 0.2, "currency": "USD"},
        "usage_provenance": "provider_reported",
        "pricing_revision": {"revision": 2**80, "unit": "per-token"},
    }
    request = replace(_ai_routing_request(_Borrow(), role, signed_input), usage_cost=True)
    expected_input = json.dumps(signed_input, separators=(",", ":")).encode()
    expected = {
        "input_tokens": 3.5,
        "output_tokens": 2.0,
        "total_tokens": 5.5,
        "cost": 0.7500000000000001,
        "currency": "USD",
        "known": True,
        "usage_provenance": "provider_reported",
        "pricing_revision": signed_input["pricing_revision"],
    }

    def serve(peer: socket.socket) -> None:
        tag, body = _receive(peer)
        assert tag == ipc._START
        start = json.loads(body)
        assert start.pop("ai_route") == {
            "role": role, "input_bytes": len(expected_input), "usage_cost": True,
        }
        assert set(start) == {
            "identity", "security_epoch", "runtime_instance", "deadline_ns",
        }
        _send(peer, ipc._BODY_READY)
        assert _receive(peer) == (ipc._ORCHESTRATION_INPUT, expected_input)
        _send(peer, ipc._CHECK)
        assert _receive(peer) == (ipc._LIVE, b"")
        _send(peer, ipc._PLAN, json.dumps(expected).encode())
        assert peer.recv(1) == b""

    monkeypatch.delenv(ipc._AI_ROUTING_ENV, raising=False)
    monkeypatch.delenv(ipc._AI_STREAM_RESULT_ENV, raising=False)
    monkeypatch.delenv(ipc._AI_USAGE_COST_ENV, raising=False)
    with _served(monkeypatch, serve):
        assert ipc.capture_launcher_ai_usage_cost_calculator() is None
        monkeypatch.setenv(ipc._AI_USAGE_COST_ENV, "1")
        calculate = ipc.capture_launcher_ai_usage_cost_calculator()
        assert calculate is not None
        assert ipc.capture_launcher_ai_routing_evaluator() is None
        assert ipc.capture_launcher_ai_stream_result_normalizer() is None
        for invalid in (
            replace(request, stream_result=True),
            replace(request, role="quote-generate"),
            replace(request, usage_cost=False),
            replace(request, input={"usage": {}}),
        ):
            with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
                calculate(invalid)
        assert calculate(request) == expected


@pytest.mark.parametrize("kind", ["usage-cost", "tool-intents", "pipeline"])
@pytest.mark.parametrize(
    ("phase", "tag", "payload", "expected"),
    [
        ("start", ipc._INAPPLICABLE, b"", ipc.NativeProviderPrepareUnavailable),
        ("body", ipc._INAPPLICABLE, b"", ipc._ProtocolError),
        ("start", ipc._FAILURE, b"legacy-read-required", ipc._ProtocolError),
        ("body", ipc._FAILURE, b"legacy-read-required", ipc._ProtocolError),
        ("live", ipc._FAILURE, b"legacy-read-required",
         ipc.NativeProviderRegistryRequiresLegacyRead),
        ("live", ipc._FAILURE, b"denied", ipc.NativeAiRoutingFailure),
        ("live", ipc._FAILURE, b"unavailable", ipc.NativeAiRoutingFailure),
        ("live", ipc._PLAN, b"{}", ipc._ProtocolError),
    ],
)
def test_ai_gateway_child_limits_protocol_fallback_to_preflight_and_live_decline(
    monkeypatch: pytest.MonkeyPatch, phase: str, tag: int, payload: bytes,
    expected: type[RuntimeError], kind: str,
) -> None:
    def serve(peer: socket.socket) -> None:
        assert _receive(peer)[0] == ipc._START
        if phase != "start":
            _send(peer, ipc._BODY_READY)
            assert _receive(peer)[0] == ipc._ORCHESTRATION_INPUT
        if phase == "live":
            _send(peer, ipc._CHECK)
            assert _receive(peer) == (ipc._LIVE, b"")
        _send(peer, tag, payload)
        if phase == "body" and tag == ipc._INAPPLICABLE:
            assert _receive(peer) == (ipc._CANCEL, b"")
            _send(peer, ipc._FAILURE, b"unavailable")
        assert peer.recv(1) == b""

    if kind == "usage-cost":
        env = ipc._AI_USAGE_COST_ENV
        capture = ipc.capture_launcher_ai_usage_cost_calculator
        signed_input = dict.fromkeys(ipc.AI_USAGE_COST_INPUT_FIELDS)
        variant = {"usage_cost": True}
    elif kind == "tool-intents":
        env = ipc._AI_TOOL_INTENTS_ENV
        capture = ipc.capture_launcher_ai_tool_intents_normalizer
        signed_input = {"request_id": "request", "intents": []}
        variant = {"tool_intents": True}
    else:
        env = ipc._AI_PIPELINE_ENV
        capture = ipc.capture_launcher_ai_pipeline_evaluator
        signed_input = {"request_id": "request", "decision_time": 10}
        variant = {"pipeline": "prepare"}
    monkeypatch.setenv(env, "1")
    with _served(monkeypatch, serve), pytest.raises(expected) as failure:
        evaluate = capture()
        assert evaluate is not None
        evaluate(replace(
            _ai_routing_request(_Borrow(), "generate", signed_input), **variant,
        ))
    if isinstance(failure.value, ipc.NativeAiRoutingFailure):
        assert failure.value.code == payload.decode()


@pytest.mark.parametrize(
    "replace_fields",
    [
        {"input_tokens": True},
        {"total_tokens": None},
        {"output_tokens": -1},
        {"cost": 0},
        {"currency": "USD"},
        {"known": 0},
        {"known": True},
        {"usage_provenance": None},
        {"pricing_revision": {"rate": float("inf")}},
        {"unexpected_rate": 10},
    ],
)
def test_ai_usage_cost_result_rejects_invalid_types_and_nonfinite_revisions(
    replace_fields: Mapping[str, Any],
) -> None:
    unknown = {
        "input_tokens": 3.5,
        "output_tokens": 0.0,
        "total_tokens": 3.5,
        "cost": None,
        "currency": None,
        "known": False,
        "usage_provenance": "provider_reported",
        "pricing_revision": ["fixture", {"catalog_revision": None}],
    }
    assert ipc._decode_ai_usage_cost(json.dumps(unknown).encode()) == unknown
    with pytest.raises(ipc._ProtocolError, match="AI Usage Cost result is invalid"):
        ipc._decode_ai_usage_cost(json.dumps({**unknown, **replace_fields}).encode())


@pytest.mark.parametrize(
    ("role", "step"),
    [
        ("generate", "prepare"), ("stream", "prepare"), ("quote-generate", "prepare"),
        ("quote-stream", "prepare"), ("generate", "failover"),
    ],
)
def test_ai_pipeline_borrows_only_its_signed_gateway_input(
    monkeypatch: pytest.MonkeyPatch, role: ipc.ModelResolveRole, step: ipc.AiPipelineStep,
) -> None:
    from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
        create_failover_operation,
        create_prepare_operation,
    )

    if step == "prepare":
        signed_input = {
            "request_id": " request-例 ", "decision_time": 17.5, "messages": [],
            "model_reference": {"profile_id": "daily"}, "resolve_only": role.startswith("quote-"),
            "delivery_mode": "incremental" if role == "quote-stream" else "buffered",
            "credential_handle": "opaque:fixture",
        }
        expected = create_prepare_operation(None)(step, signed_input)
    else:
        signed_input = {
            "allow_failover": True, "idempotency_key": "bound", "tools": [],
            "error_code": "quota", "attempt": 1, "candidate_count": 2,
            "deadline": 20.0, "decision_time": 17.5,
        }
        expected = create_failover_operation(None)("decide", signed_input)
        assert expected["allowed"] is True
    body_input = json.dumps(signed_input, ensure_ascii=False, separators=(",", ":")).encode()
    request = replace(_ai_routing_request(_Borrow(), role, signed_input), pipeline=step)

    def serve(peer: socket.socket) -> None:
        tag, body = _receive(peer)
        assert tag == ipc._START
        assert json.loads(body)["ai_route"] == {
            "role": role, "input_bytes": len(body_input), "pipeline": step,
        }
        _send(peer, ipc._BODY_READY)
        assert _receive(peer) == (ipc._ORCHESTRATION_INPUT, body_input)
        _send(peer, ipc._CHECK)
        assert _receive(peer) == (ipc._LIVE, b"")
        _send(peer, ipc._PLAN, json.dumps(expected).encode())
        assert peer.recv(1) == b""

    monkeypatch.delenv(ipc._AI_PIPELINE_ENV, raising=False)
    with _served(monkeypatch, serve):
        assert ipc.capture_launcher_ai_pipeline_evaluator() is None
        monkeypatch.setenv(ipc._AI_PIPELINE_ENV, "1")
        monkeypatch.setenv(ipc._AI_ROUTING_ENV, "1")
        evaluate = ipc.capture_launcher_ai_pipeline_evaluator()
        route = ipc.capture_launcher_ai_routing_evaluator()
        assert evaluate is not None and route is not None
        with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
            route(request)
        for invalid in (
            replace(request, pipeline=None), replace(request, pipeline="unknown"),
            replace(request, stream_result=True), replace(request, usage_cost=True),
            replace(request, tool_intents=True), replace(request, input={}),
        ):
            with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
                evaluate(invalid)
        with pytest.raises(ipc.NativeProviderRegistryRequiresLegacyRead):
            evaluate(replace(request, input={
                **signed_input, "decision_time": float("inf"),
            }))
        with pytest.raises(ipc.NativeProviderRegistryRequiresLegacyRead):
            evaluate(replace(request, input={
                **signed_input, "decision_time": "x" * (1024 * 1024),
            }))
        for unsupported in ([{"nested": {1: "value"}}], ("tuple",)):
            with pytest.raises(ipc.NativeProviderRegistryRequiresLegacyRead):
                evaluate(replace(request, input={
                    **signed_input, "tools": unsupported,
                }))
        if step == "prepare":
            with pytest.raises(ipc.NativeProviderRegistryRequiresLegacyRead):
                evaluate(replace(request, input={
                    **signed_input, "credential_handle": "raw-fixture-value",
                }))
        else:
            for invalid_role in ("stream", "quote-generate", "quote-stream"):
                with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
                    evaluate(replace(request, role=invalid_role))
            with pytest.raises(ipc.NativeAiRoutingFailure, match="invalid"):
                evaluate(replace(request, input={**signed_input, "execute": True}))
        assert evaluate(request) == expected


@pytest.mark.parametrize("step", ["prepare", "failover"])
def test_ai_pipeline_result_rejects_extra_fields_and_inconsistent_values(
    step: ipc.AiPipelineStep,
) -> None:
    from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
        create_failover_operation,
        create_prepare_operation,
    )

    if step == "prepare":
        inputs = {"request_id": "request", "decision_time": 10, "credential_handle": "opaque:a"}
        request = replace(_ai_routing_request(_Borrow(), "generate", inputs), pipeline=step)
        expected = create_prepare_operation(None)(step, inputs)

        def decode(body: bytes) -> dict[str, Any]:
            return ipc._decode_ai_pipeline_prepare(body, request)

        changes = [
            {"request_id": "different"}, {"deadline": 10}, {"deadline": True},
            {"credential_handle": "opaque:other"}, {"credential_handle": None},
            {"requirements": []}, {"allow_failover": "true"}, {"allow_failover": True},
        ]
    else:
        expected = create_failover_operation(None)("decide", {})
        decode = ipc._decode_ai_pipeline_failover
        changes = [
            {"allowed": True}, {"allowed": 0}, {"reason": "idempotency_bound"},
            {"checks": {**expected["checks"], "tool_free": 1}},
            {"checks": {**expected["checks"], "execute": False}},
        ]
    assert decode(json.dumps(expected).encode()) == expected
    for change in [*changes, {"authority_granted": True}]:
        with pytest.raises(ipc._ProtocolError, match="result is invalid"):
            decode(json.dumps({**expected, **change}).encode())
