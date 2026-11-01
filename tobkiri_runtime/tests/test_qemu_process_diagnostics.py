"""Bounded private evidence must not change public errors or cleanup facts."""

import os

from tobkiri_host.qemu_process_diagnostics import QemuProcessDiagnostics


def test_stderr_tail_is_bounded_and_keeps_latest_bytes() -> None:
    evidence = QemuProcessDiagnostics()
    reader, writer = os.pipe()
    stream = os.fdopen(reader, "rb", buffering=0)
    os.set_blocking(reader, False)
    try:
        for byte in range(32):
            os.write(writer, bytes([byte]) * 1024)
            evidence.drain(stream)
        first, tail = evidence.snapshot()
        assert first is None
        assert tail == b"".join(bytes([i]) * 1024 for i in range(16, 32))
        assert len(tail) == 16 * 1024
        # An empty but still-open pipe does not block the owner.
        evidence.drain(stream)
        assert evidence.snapshot()[1] == tail
        os.close(writer)
        writer = -1
        evidence.drain(stream)
        assert evidence.snapshot()[1] == tail
    finally:
        stream.close()
        if writer >= 0:
            os.close(writer)


def test_first_failure_survives_cleanup_without_exposing_tail() -> None:
    evidence = QemuProcessDiagnostics()
    first = RuntimeError("stream ended")
    evidence.capture(first, phase="serial", pid=42, returncode=None)
    cleanup = RuntimeError("stopped")
    evidence.capture(cleanup, phase="launch", pid=42, returncode=-15)
    assert evidence.snapshot()[0] == ("serial", 42, None, "RuntimeError")
    assert first._qemu_process_diagnostics is evidence
    assert cleanup._qemu_process_diagnostics is evidence
    assert str(first) == "stream ended"
    assert repr(evidence) == "<private QEMU process diagnostics>"


def test_evidence_remains_reachable_through_original_failure_chain() -> None:
    evidence = QemuProcessDiagnostics()
    original = RuntimeError("stream ended")
    evidence.capture(original, phase="serial", pid=43, returncode=1)
    try:
        raise RuntimeError("channel retired") from original
    except RuntimeError as wrapped:
        assert wrapped.__cause__._qemu_process_diagnostics.snapshot()[0] == (
            "serial", 43, 1, "RuntimeError",
        )
        assert "43" not in str(wrapped)
