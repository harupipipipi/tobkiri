"""Platform-specific process birth identity tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import cast

import pytest

import core_runtime.process_identity as process_identity
from core_runtime.authority.v4_store import AuthorityStore, AuthorityStoreError
from core_runtime.process_identity import ProcessIdentityEvidence


class _FakeDarwinProcessAPI:
    def __init__(self, start_time: object = (1_700_000_000, 123_456)) -> None:
        self.start_time = start_time
        self.error: BaseException | None = None
        self.queried: list[int] = []

    def process_start_time(self, process_id: int) -> tuple[int, int] | None:
        self.queried.append(process_id)
        if self.error is not None:
            raise self.error
        return cast("tuple[int, int] | None", self.start_time)


def _select_darwin(
    monkeypatch: pytest.MonkeyPatch,
    api: _FakeDarwinProcessAPI | None,
) -> None:
    monkeypatch.setattr(process_identity, "_is_windows", lambda: False)
    monkeypatch.setattr(process_identity, "_is_darwin", lambda: True)
    monkeypatch.setattr(process_identity, "_load_darwin_process_api", lambda: api)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires Darwin libproc")
def test_native_darwin_current_pid_has_kernel_birth_identity() -> None:
    evidence = process_identity.process_start_identity(os.getpid())

    assert evidence.state == "live"
    prefix, process_id, seconds, microseconds = evidence.identity.split(":")
    assert prefix == "darwin"
    assert int(process_id) == os.getpid()
    assert int(seconds) > 0
    assert 0 <= int(microseconds) < 1_000_000


def test_darwin_same_second_pid_reuse_has_distinct_kernel_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeDarwinProcessAPI((1_700_000_000, 100))
    _select_darwin(monkeypatch, api)
    monkeypatch.setattr(
        process_identity.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Darwin identity launched ps"),
    )
    monkeypatch.setattr(
        process_identity.os,
        "kill",
        lambda *_args, **_kwargs: pytest.fail("Darwin identity used a POSIX probe"),
    )

    first = process_identity.process_start_identity(4242)
    api.start_time = (1_700_000_000, 900_000)
    reused = process_identity.process_start_identity(4242)

    assert first == ProcessIdentityEvidence(
        "live", "darwin:4242:1700000000:000100"
    )
    assert reused == ProcessIdentityEvidence(
        "live", "darwin:4242:1700000000:900000"
    )
    assert first.identity != reused.identity


def test_darwin_same_second_pid_reuse_invalidates_process_owned_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeDarwinProcessAPI((1_700_000_000, 100))
    _select_darwin(monkeypatch, api)
    store = AuthorityStore(
        tmp_path / "authority.sqlite3",
        process_start_reader=process_identity.process_start_identity,
    )
    assert store.security_epoch == 1

    api.start_time = (1_700_000_000, 101)
    with pytest.raises(AuthorityStoreError, match="identity"):
        _ = store.security_epoch

    api.start_time = (1_700_000_000, 100)
    store.close()


def test_darwin_missing_pid_is_explicitly_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakeDarwinProcessAPI()
    api.error = ProcessLookupError("gone")
    _select_darwin(monkeypatch, api)

    assert process_identity.process_start_identity(4242) == ProcessIdentityEvidence("dead")


@pytest.mark.parametrize(
    "start_time",
    [None, (), (1,), (1, 2, 3), (0, 1), (1, -1), (1, 1_000_000), ("1", 2)],
)
def test_darwin_malformed_process_info_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    start_time: object,
) -> None:
    _select_darwin(monkeypatch, _FakeDarwinProcessAPI(start_time))

    assert process_identity.process_start_identity(4242) == ProcessIdentityEvidence(
        "unknown"
    )


@pytest.mark.parametrize("failure", ["unavailable", "denied", "api-error"])
def test_darwin_unavailable_process_api_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    api = _FakeDarwinProcessAPI()
    if failure == "denied":
        api.error = PermissionError("denied")
    elif failure == "api-error":
        api.error = OSError("libproc failed")
    _select_darwin(monkeypatch, None if failure == "unavailable" else api)

    assert process_identity.process_start_identity(4242) == ProcessIdentityEvidence(
        "unknown"
    )


def test_linux_proc_identity_path_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields = ["S", *(str(index) for index in range(1, 19)), "8675309"]
    monkeypatch.setattr(process_identity, "_is_windows", lambda: False)
    monkeypatch.setattr(process_identity, "_is_darwin", lambda: False)
    monkeypatch.setattr(process_identity.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(process_identity.Path, "exists", lambda _path: True)
    monkeypatch.setattr(
        process_identity.Path,
        "read_text",
        lambda _path, encoding: f"4242 (worker name) {' '.join(fields)}",
    )
    monkeypatch.setattr(
        process_identity.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("Linux identity launched ps"),
    )

    assert process_identity.process_start_identity(4242) == ProcessIdentityEvidence(
        "live", "linux:8675309"
    )


def test_non_darwin_posix_fallback_uses_argument_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, "Mon Aug 10 12:34:56 2026\n", "")

    monkeypatch.setattr(process_identity, "_is_windows", lambda: False)
    monkeypatch.setattr(process_identity, "_is_darwin", lambda: False)
    monkeypatch.setattr(process_identity.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(process_identity.Path, "exists", lambda _path: False)
    monkeypatch.setattr(process_identity.subprocess, "run", run)

    evidence = process_identity.process_start_identity(os.getpid())

    assert evidence.state == "live"
    assert calls == [
        (
            ["ps", "-o", "lstart=", "-p", str(os.getpid())],
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 1.0,
            },
        )
    ]
