"""Adversarial tests for the one-shot PackVM loopback image handoff."""

from __future__ import annotations

import hashlib
import http.client
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from ecosystem.defaultspack.backend.sandbox.isolation.packvm_image_handoff import (
    PackVMImageHandoffError,
    PackVMLoopbackImageHandoff,
)


def _descriptor(tmp_path: Path, content: bytes) -> int:
    path = tmp_path / "staged.img"
    path.write_bytes(content)
    descriptor = os.open(path, os.O_RDONLY)
    path.unlink()
    return descriptor


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def test_one_shot_serves_exact_unlinked_bytes_once(tmp_path: Path) -> None:
    content = b"verified image bytes"
    descriptor = _descriptor(tmp_path, content)
    try:
        with PackVMLoopbackImageHandoff(
            descriptor, size_bytes=len(content), digest=_digest(content)
        ) as handoff:
            assert urllib.request.urlopen(handoff.url, timeout=2).read() == content
            with pytest.raises(urllib.error.HTTPError) as repeated:
                urllib.request.urlopen(handoff.url, timeout=2)
            assert repeated.value.code == 410
            handoff.require_consumed()
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("attack", ["token", "range", "head"])
def test_invalid_request_does_not_consume_capability(tmp_path: Path, attack: str) -> None:
    content = b"verified image bytes"
    descriptor = _descriptor(tmp_path, content)
    try:
        with PackVMLoopbackImageHandoff(
            descriptor, size_bytes=len(content), digest=_digest(content)
        ) as handoff:
            if attack == "token":
                changed = "0" if handoff.url[-1] != "0" else "1"
                request = urllib.request.Request(handoff.url[:-1] + changed)
            elif attack == "range":
                request = urllib.request.Request(handoff.url, headers={"Range": "bytes=0-3"})
            else:
                request = urllib.request.Request(handoff.url, method="HEAD")
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(request, timeout=2)
            assert urllib.request.urlopen(handoff.url, timeout=2).read() == content
            handoff.require_consumed()
    finally:
        os.close(descriptor)


def test_cancelled_or_digest_changed_stream_is_never_authoritative(
    tmp_path: Path,
) -> None:
    content = b"verified image bytes"
    for cancelled, digest in ((lambda: True, _digest(content)), (None, "sha256:" + "0" * 64)):
        descriptor = _descriptor(tmp_path, content)
        try:
            with PackVMLoopbackImageHandoff(
                descriptor,
                size_bytes=len(content),
                digest=digest,
                cancelled=cancelled,
            ) as handoff:
                try:
                    urllib.request.urlopen(handoff.url, timeout=2).read()
                except (ConnectionError, OSError, http.client.IncompleteRead):
                    pass
                with pytest.raises(PackVMImageHandoffError):
                    handoff.require_consumed()
        finally:
            os.close(descriptor)


def test_stalled_and_junk_connections_cannot_deny_legitimate_get(
    tmp_path: Path,
) -> None:
    content = b"verified image bytes"
    descriptor = _descriptor(tmp_path, content)
    junk: list[socket.socket] = []
    try:
        started = time.monotonic()
        with PackVMLoopbackImageHandoff(
            descriptor,
            size_bytes=len(content),
            digest=_digest(content),
            overall_timeout_seconds=3,
        ) as handoff:
            parsed = urllib.parse.urlsplit(handoff.url)
            assert parsed.port is not None
            for index in range(96):
                client = socket.create_connection(("127.0.0.1", parsed.port), timeout=1)
                if index % 2:
                    client.sendall(b"GET /partial")
                junk.append(client)
            assert urllib.request.urlopen(handoff.url, timeout=2).read() == content
            handoff.require_consumed()
        assert time.monotonic() - started < 2.5
    finally:
        for client in junk:
            client.close()
        os.close(descriptor)


def test_continuous_junk_accepts_cannot_starve_legitimate_get(tmp_path: Path) -> None:
    content = b"verified image bytes"
    descriptor = _descriptor(tmp_path, content)
    stop = threading.Event()
    workers: list[threading.Thread] = []

    def flood(port: int) -> None:
        while not stop.is_set():
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=0.1)
                client.sendall(b"GET /junk HTTP/1.1\r\nHost: junk\r\n\r\n")
                client.close()
            except OSError:
                pass

    try:
        with PackVMLoopbackImageHandoff(
            descriptor,
            size_bytes=len(content),
            digest=_digest(content),
            overall_timeout_seconds=4,
        ) as handoff:
            parsed = urllib.parse.urlsplit(handoff.url)
            assert parsed.port is not None
            workers = [threading.Thread(target=flood, args=(parsed.port,)) for _ in range(4)]
            for worker in workers:
                worker.start()
            time.sleep(0.05)
            assert urllib.request.urlopen(handoff.url, timeout=3).read() == content
            handoff.require_consumed()
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=1)
        os.close(descriptor)


def test_late_final_send_never_marks_handoff_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation import packvm_image_handoff

    content = b"verified image bytes"
    descriptor = _descriptor(tmp_path, content)
    clock = [0.0]

    class LateFinalSocket:
        sends = 0

        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, _payload: bytes) -> None:
            self.sends += 1
            if self.sends == 2:
                clock[0] = 2.0

    monkeypatch.setattr(packvm_image_handoff.time, "monotonic", lambda: clock[0])
    handoff = PackVMLoopbackImageHandoff(
        descriptor, size_bytes=len(content), digest=_digest(content)
    )
    try:
        handoff._deadline = 1.0
        handoff._stream(LateFinalSocket())  # type: ignore[arg-type]
        with pytest.raises(PackVMImageHandoffError):
            handoff.require_consumed()
        assert handoff._consumed is False
    finally:
        os.close(handoff._descriptor)
        os.close(descriptor)


def test_cancel_and_exit_are_bounded_with_silent_connection(tmp_path: Path) -> None:
    content = b"verified image bytes"
    descriptor = _descriptor(tmp_path, content)
    cancelled = threading.Event()
    client: socket.socket | None = None
    try:
        started = time.monotonic()
        with PackVMLoopbackImageHandoff(
            descriptor,
            size_bytes=len(content),
            digest=_digest(content),
            cancelled=cancelled.is_set,
            overall_timeout_seconds=30,
        ) as handoff:
            parsed = urllib.parse.urlsplit(handoff.url)
            assert parsed.port is not None
            client = socket.create_connection(("127.0.0.1", parsed.port), timeout=1)
            cancelled.set()
            with pytest.raises(PackVMImageHandoffError):
                handoff.require_consumed()
        assert time.monotonic() - started < 2
    finally:
        if client is not None:
            client.close()
        os.close(descriptor)


def test_thread_start_failure_closes_listener_and_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"verified image bytes"
    descriptor = _descriptor(tmp_path, content)
    before = len(os.listdir("/dev/fd")) if Path("/dev/fd").exists() else None

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    try:
        with pytest.raises(RuntimeError, match="thread start failed"):
            with PackVMLoopbackImageHandoff(
                descriptor, size_bytes=len(content), digest=_digest(content)
            ):
                pass
        if before is not None:
            assert len(os.listdir("/dev/fd")) == before
    finally:
        os.close(descriptor)


def test_thread_partial_start_failure_is_joined_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"verified image bytes"
    descriptor = _descriptor(tmp_path, content)
    original_start = threading.Thread.start
    before = len(os.listdir("/dev/fd")) if Path("/dev/fd").exists() else None

    def start_then_fail(thread: threading.Thread) -> None:
        original_start(thread)
        raise RuntimeError("partial thread start failed")

    monkeypatch.setattr(threading.Thread, "start", start_then_fail)
    try:
        with pytest.raises(RuntimeError, match="partial thread start failed"):
            with PackVMLoopbackImageHandoff(
                descriptor, size_bytes=len(content), digest=_digest(content)
            ):
                pass
        assert not any(
            thread.name == "packvm-image-handoff" and thread.is_alive()
            for thread in threading.enumerate()
        )
        if before is not None:
            assert len(os.listdir("/dev/fd")) == before
    finally:
        os.close(descriptor)
