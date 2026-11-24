"""Adversarial regressions for the PackVM-owned resumable image cache."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import threading
import time
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.defaultspack.backend.sandbox.isolation import packvm_image_cache as image_cache
from ecosystem.defaultspack.backend.sandbox.isolation.lima_runtime import (
    PACKVM_LIMA_DERIVED_PATH_SUFFIXES,
    PACKVM_LIMA_UNIX_PATH_LIMIT_BYTES,
    _packvm_runtime_path_diagnostic,
)
from ecosystem.defaultspack.backend.sandbox.isolation.packvm_image_cache import (
    PackVMImageAuthority,
    PackVMImageCache,
    PackVMImageCancelled,
    PackVMImageError,
    PackVMImageProgress,
)


class _Response:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        url: str = "https://images.example.test/base.img",
        headers: dict[str, str] | None = None,
        fail_after_reads: int | None = None,
        on_read: Any | None = None,
    ) -> None:
        self.status = status
        self._url = url
        self._body = io.BytesIO(body)
        self._reads = 0
        self._fail_after_reads = fail_after_reads
        self._on_read = on_read
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._on_read is not None:
            self._on_read(self._reads)
        if self._fail_after_reads is not None and self._reads > self._fail_after_reads:
            raise socket.timeout()
        return self._body.read(size)

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


class _Opener:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request: Any, *, timeout: float) -> _Response:
        assert 0 < timeout <= 300
        self.requests.append(request)
        return self.responses.pop(0)


def _authority(content: bytes, **changes: object) -> PackVMImageAuthority:
    values: dict[str, object] = {
        "source_url": "https://images.example.test/base.img",
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "platform": "macos",
        "architecture": "arm64",
        "plan_digest": "sha256:" + "1" * 64,
        "session_digest": "sha256:" + "2" * 64,
        "operation_id": "00000000-0000-0000-0000-000000000001",
    }
    values.update(changes)
    return PackVMImageAuthority(**values)  # type: ignore[arg-type]


def _headers(content: bytes, **changes: str) -> dict[str, str]:
    values = {
        "Content-Length": str(len(content)),
        "Content-Encoding": "identity",
        "ETag": '"v1"',
        "Last-Modified": "Tue, 11 Aug 2026 00:00:00 GMT",
    }
    values.update(changes)
    return values


def _cache(tmp_path: Path, opener: _Opener) -> PackVMImageCache:
    return PackVMImageCache(
        tmp_path / "owned-cache",
        opener=opener,
        disk_usage=lambda _path: SimpleNamespace(free=4 * 1024**3),
    )


def test_prefetch_atomically_publishes_exact_digest_and_binding(tmp_path: Path) -> None:
    content = b"signed image bytes"
    opener = _Opener(_Response(content, headers=_headers(content)))
    cache = _cache(tmp_path, opener)

    verified = cache.prefetch(_authority(content))

    assert verified.path.name == "image.img"
    assert verified.path.read_bytes() == content
    assert verified.digest == _authority(content).digest
    assert not (verified.path.parent / "partial.img").exists()
    metadata = json.loads((verified.path.parent / "published.json").read_text())
    assert metadata["acquired_plan_digest"] == _authority(content).plan_digest
    assert metadata["acquired_session_digest"] == _authority(content).session_digest


def test_concurrent_generations_share_one_atomic_capacity_reservation(
    tmp_path: Path,
) -> None:
    first = b"first-generation"
    second = b"second-generation"
    first_authority = _authority(first)
    second_authority = _authority(
        second, source_url="https://images.example.test/second.img"
    )
    root = tmp_path / "owned-cache"
    responses = {
        first_authority.source_url: first,
        second_authority.source_url: second,
    }
    active = 0
    maximum_active = 0
    activity_lock = threading.Lock()

    def opener(request: Any, *, timeout: float) -> _Response:
        nonlocal active, maximum_active
        with activity_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        content = responses[str(request.full_url)]

        def reading(_count: int) -> None:
            nonlocal active
            time.sleep(0.02)
            if _count == 2:
                with activity_lock:
                    active -= 1

        return _Response(
            content,
            url=str(request.full_url),
            headers=_headers(content),
            on_read=reading,
        )

    def disk_usage(_path: Path) -> Any:
        allocated = sum(
            path.stat().st_size
            for path in root.rglob("*.img")
            if path.is_file()
        ) if root.exists() else 0
        return SimpleNamespace(
            free=image_cache.PACKVM_IMAGE_DISK_RESERVE_BYTES
            + max(len(first), len(second))
            - allocated
        )

    cache = PackVMImageCache(root, opener=opener, disk_usage=disk_usage)
    cache._ensure_root()
    results: list[object] = []

    def acquire(authority: PackVMImageAuthority) -> None:
        try:
            results.append(cache.prefetch(authority))
        except Exception as exc:  # noqa: BLE001 - assert exact typed result below
            results.append(exc)

    workers = [
        threading.Thread(target=acquire, args=(first_authority,)),
        threading.Thread(target=acquire, args=(second_authority,)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)

    assert all(not worker.is_alive() for worker in workers)
    assert maximum_active == 1
    assert sum(isinstance(result, PackVMImageError) for result in results) == 1
    failure = next(result for result in results if isinstance(result, PackVMImageError))
    assert failure.code == "packvm_image_disk_insufficient"


def test_crash_after_image_rename_is_reclaimed_by_cleanup_and_gc(
    tmp_path: Path,
) -> None:
    content = b"renamed-before-metadata"
    authority = _authority(content)
    cache = _cache(tmp_path, _Opener())
    entry = cache.image_path(authority).parent
    entry.mkdir(parents=True, mode=0o700)
    image = entry / "image.img"
    image.write_bytes(content)
    old = time.time() - 7200
    os.utime(image, (old, old))

    assert cache.cleanup(authority) is True
    assert not image.exists()

    image.write_bytes(content)
    os.utime(image, (old, old))
    current_content = b"current-generation"
    current_authority = _authority(
        current_content, source_url="https://images.example.test/current.img"
    )
    current = PackVMImageCache(
        tmp_path / "owned-cache",
        opener=_Opener(
            _Response(
                current_content,
                url=current_authority.source_url,
                headers=_headers(current_content),
            )
        ),
        disk_usage=lambda _path: SimpleNamespace(free=4 * 1024**3),
    ).prefetch(current_authority)
    assert cache.garbage_collect(
        current_authority,
        maximum_generations=1,
        quota_bytes=len(current_content),
        uncheckpointed_minimum_age_seconds=3600,
    ) == 1
    assert current.path.exists()
    assert not image.exists()


def test_verified_cache_hit_emits_terminal_progress_before_consumer_mutation(
    tmp_path: Path,
) -> None:
    content = b"signed image bytes"
    cache = _cache(tmp_path, _Opener(_Response(content, headers=_headers(content))))
    authority = _authority(content)
    cache.prefetch(authority)
    updates: list[image_cache.PackVMImageProgress] = []

    cache.prefetch(authority, progress=updates.append)

    assert [update.stage for update in updates] == ["verified"]
    assert updates[0].downloaded_bytes == len(content)
    assert updates[0].total_bytes == len(content)


def test_cache_hit_hashing_observes_cancellation_before_verified_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 1)
    content = b"signed image bytes"
    cache = _cache(tmp_path, _Opener(_Response(content, headers=_headers(content))))
    authority = _authority(content)
    cache.prefetch(authority)
    checks = 0
    updates: list[image_cache.PackVMImageProgress] = []

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks > 2

    with pytest.raises(PackVMImageCancelled):
        cache.prefetch(authority, progress=updates.append, cancelled=cancelled)
    assert updates == []


def test_cache_hit_final_read_revalidates_entry_path_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"signed image bytes"
    cache = _cache(tmp_path, _Opener(_Response(content, headers=_headers(content))))
    authority = _authority(content)
    published = cache.prefetch(authority)
    entry = published.path.parent
    displaced = tmp_path / "displaced-entry"
    original_read = os.read
    swapped = False

    def replacing_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        data = original_read(descriptor, size)
        if (
            not swapped
            and data
            and os.fstat(descriptor).st_ino == published.inode
            and os.lseek(descriptor, 0, os.SEEK_CUR) == len(content)
        ):
            swapped = True
            entry.rename(displaced)
            entry.mkdir(mode=0o700)
            (entry / "image.img").write_bytes(b"replacement image")
        return data

    monkeypatch.setattr(image_cache.os, "read", replacing_read)
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(authority)
    assert captured.value.code == "packvm_image_parent_swap"


def test_cache_hit_final_read_observes_cancel_before_verified_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"signed image bytes"
    cache = _cache(tmp_path, _Opener(_Response(content, headers=_headers(content))))
    authority = _authority(content)
    published = cache.prefetch(authority)
    original_read = os.read
    cancel_requested = False
    updates: list[image_cache.PackVMImageProgress] = []

    def final_read(descriptor: int, size: int) -> bytes:
        nonlocal cancel_requested
        data = original_read(descriptor, size)
        if (
            data
            and os.fstat(descriptor).st_ino == published.inode
            and os.lseek(descriptor, 0, os.SEEK_CUR) == len(content)
        ):
            cancel_requested = True
        return data

    monkeypatch.setattr(image_cache.os, "read", final_read)
    with pytest.raises(PackVMImageCancelled):
        cache.prefetch(
            authority,
            progress=updates.append,
            cancelled=lambda: cancel_requested,
        )
    assert updates == []


def test_provisioning_handoff_pins_cache_hit_without_rehashing_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"signed image bytes"
    cache = _cache(tmp_path, _Opener(_Response(content, headers=_headers(content))))
    authority = _authority(content)
    published = cache.prefetch(authority)

    def redundant_hash(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("provisioning cache hit redundantly hashed the source")

    monkeypatch.setattr(image_cache, "_descriptor_digest", redundant_hash)
    assert cache.status(authority) == ("verified_source", None)
    with cache.provisioning_image(authority) as pinned:
        assert pinned.verified == published
        assert os.fstat(pinned.descriptor).st_ino == published.inode

    published.path.chmod(0o600)
    published.path.write_bytes(b"tampered image byt")
    published.path.chmod(0o400)
    status, reason = cache.status(authority)
    assert status == "unsafe"
    assert "identity changed" in str(reason)


def test_timeout_checkpoint_resumes_only_with_exact_range_and_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    content = b"0123456789abcdef"
    first = _Response(
        content,
        headers=_headers(content),
        fail_after_reads=2,
    )
    remaining = content[8:]
    second = _Response(
        remaining,
        status=206,
        headers=_headers(
            remaining,
            **{"Content-Range": f"bytes 8-15/{len(content)}"},
        ),
    )
    opener = _Opener(first, second)
    cache = _cache(tmp_path, opener)
    authority = _authority(content)

    with pytest.raises(PackVMImageError, match="inactive"):
        cache.prefetch(authority)
    verified = cache.prefetch(authority)

    assert verified.path.read_bytes() == content
    assert opener.requests[1].get_header("Range") == "bytes=8-"
    assert opener.requests[1].get_header("If-range") == '"v1"'


def test_truncated_before_periodic_checkpoint_retries_successfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    content = b"0123456789abcdef"
    opener = _Opener(
        _Response(content[:4], headers=_headers(content)),
        _Response(
            content[4:],
            status=206,
            headers=_headers(content[4:], **{"Content-Range": "bytes 4-15/16"}),
        ),
    )
    cache = _cache(tmp_path, opener)
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(content))
    assert captured.value.code == "packvm_image_truncated"
    assert cache.prefetch(_authority(content)).path.read_bytes() == content


def test_validatorless_truncation_restarts_from_zero_without_range(
    tmp_path: Path,
) -> None:
    content = b"validatorless-content"
    headers = {
        "Content-Length": str(len(content)),
        "Content-Encoding": "identity",
    }
    opener = _Opener(
        _Response(content[:5], headers=headers),
        _Response(content, headers=headers),
    )
    cache = _cache(tmp_path, opener)

    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(content))
    assert captured.value.code == "packvm_image_truncated"
    assert cache.prefetch(_authority(content)).path.read_bytes() == content
    assert len(opener.requests) == 2
    assert opener.requests[1].get_header("Range") is None


def test_digest_mismatch_uncheckpointed_partial_is_reclaimed_for_retry(
    tmp_path: Path,
) -> None:
    expected = b"expected-content"
    wrong = b"different-bytes!"
    assert len(wrong) == len(expected)
    opener = _Opener(
        _Response(wrong, headers=_headers(wrong)),
        _Response(expected, headers=_headers(expected, ETag='"v2"')),
    )
    cache = _cache(tmp_path, opener)
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(expected))
    assert captured.value.code == "packvm_image_digest_mismatch"
    assert cache.prefetch(_authority(expected)).path.read_bytes() == expected


def test_cancel_checkpoints_and_later_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    content = b"abcdefghijklmnop"
    opener = _Opener(
        _Response(content, headers=_headers(content)),
        _Response(
            content[4:],
            status=206,
            headers=_headers(
                content[4:], **{"Content-Range": "bytes 4-15/16"}
            ),
        ),
    )
    cache = _cache(tmp_path, opener)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    with pytest.raises(PackVMImageCancelled):
        cache.prefetch(_authority(content), cancelled=cancelled)
    assert cache.prefetch(_authority(content)).path.read_bytes() == content


def test_cancel_after_final_chunk_publishes_cache_but_never_emits_verified_barrier(
    tmp_path: Path,
) -> None:
    content = b"final-chunk"
    cache = _cache(tmp_path, _Opener(_Response(content, headers=_headers(content))))
    calls = 0
    updates: list[image_cache.PackVMImageProgress] = []

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(PackVMImageCancelled):
        cache.prefetch(
            _authority(content), progress=updates.append, cancelled=cancelled
        )
    assert updates == []
    assert cache.verified(_authority(content)).path.read_bytes() == content


@pytest.mark.parametrize(
    ("response_headers", "code"),
    [
        ({}, "content_range_invalid"),
        ({"Content-Range": "bytes 7-15/16"}, "content_range_mismatch"),
        ({"Content-Range": "bytes 8-15/16", "ETag": '"v2"'}, "validator_changed"),
    ],
)
def test_resume_rejects_invalid_range_or_changed_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_headers: dict[str, str],
    code: str,
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    content = b"0123456789abcdef"
    first = _Response(content, headers=_headers(content), fail_after_reads=2)
    resumed_headers = _headers(content[8:], **response_headers)
    opener = _Opener(first, _Response(content[8:], status=206, headers=resumed_headers))
    cache = _cache(tmp_path, opener)
    with pytest.raises(PackVMImageError):
        cache.prefetch(_authority(content))
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(content))
    assert code in captured.value.code


def test_valid_200_fallback_safely_restarts_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    content = b"0123456789abcdef"
    opener = _Opener(
        _Response(content, headers=_headers(content), fail_after_reads=2),
        _Response(content, status=200, headers=_headers(content, ETag='"v2"')),
    )
    cache = _cache(tmp_path, opener)
    with pytest.raises(PackVMImageError):
        cache.prefetch(_authority(content))
    assert cache.prefetch(_authority(content)).path.read_bytes() == content


def test_slow_trickle_is_stopped_by_overall_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 1)
    clock = [0.0]

    def advance(_reads: int) -> None:
        clock[0] += 2.0

    content = b"slow"
    cache = PackVMImageCache(
        tmp_path / "owned-cache",
        opener=_Opener(_Response(content, headers=_headers(content), on_read=advance)),
        disk_usage=lambda _path: SimpleNamespace(free=4 * 1024**3),
        monotonic=lambda: clock[0],
        overall_timeout_seconds=3.0,
    )
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(content))
    assert captured.value.code == "packvm_image_overall_timeout"


def test_slow_trickle_is_stopped_by_inactivity_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 1)
    clock = [0.0]

    def advance(_reads: int) -> None:
        clock[0] += 2.0

    content = b"slow"
    cache = PackVMImageCache(
        tmp_path / "owned-cache",
        opener=_Opener(_Response(content, headers=_headers(content), on_read=advance)),
        disk_usage=lambda _path: SimpleNamespace(free=4 * 1024**3),
        monotonic=lambda: clock[0],
        inactivity_timeout_seconds=1.0,
        overall_timeout_seconds=20.0,
    )
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(content))
    assert captured.value.code == "packvm_image_inactivity_timeout"


def test_slow_trickle_observes_cancel_after_each_receive_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 1)
    clock = [0.0]

    def advance(_reads: int) -> None:
        clock[0] += 1.0

    content = b"slow"
    cache = PackVMImageCache(
        tmp_path / "owned-cache",
        opener=_Opener(_Response(content, headers=_headers(content), on_read=advance)),
        disk_usage=lambda _path: SimpleNamespace(free=4 * 1024**3),
        monotonic=lambda: clock[0],
        inactivity_timeout_seconds=10.0,
        overall_timeout_seconds=20.0,
    )
    with pytest.raises(PackVMImageCancelled):
        cache.prefetch(_authority(content), cancelled=lambda: clock[0] >= 1.0)
    assert cache.remaining_bytes(_authority(content)) == len(content) - 1


def test_resume_rejects_changed_final_redirect_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    content = b"0123456789abcdef"
    opener = _Opener(
        _Response(content, headers=_headers(content), fail_after_reads=2),
        _Response(
            content[8:],
            status=206,
            url="https://cdn.example.test/replaced.img",
            headers=_headers(content[8:], **{"Content-Range": "bytes 8-15/16"}),
        ),
    )
    cache = _cache(tmp_path, opener)
    with pytest.raises(PackVMImageError):
        cache.prefetch(_authority(content))
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(content))
    assert captured.value.code == "packvm_image_redirect_rejected"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://images.example.test/base.img", "redirect_rejected"),
        ("file:///tmp/base.img", "redirect_rejected"),
    ],
)
def test_final_url_downgrade_is_rejected(tmp_path: Path, url: str, code: str) -> None:
    content = b"image"
    cache = _cache(tmp_path, _Opener(_Response(content, url=url, headers=_headers(content))))
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(content))
    assert code in captured.value.code


@pytest.mark.parametrize(
    "target",
    [
        "https://127.0.0.1/internal",
        "https://169.254.169.254/latest/meta-data",
        "https://cross-origin.example.test/base.img",
    ],
)
def test_redirect_handler_never_authorizes_a_second_origin(target: str) -> None:
    handler = image_cache._StrictRedirectHandler()
    request = image_cache.urllib.request.Request(
        "https://images.example.test/base.img"
    )
    with pytest.raises(PackVMImageError) as captured:
        handler.redirect_request(request, None, 302, "redirect", {}, target)
    assert captured.value.code == "packvm_image_redirect_rejected"


@pytest.mark.parametrize("body", [b"short", b"too-long-content"])
def test_wrong_length_or_digest_never_publishes(tmp_path: Path, body: bytes) -> None:
    expected = b"expected-content"
    headers = _headers(body)
    cache = _cache(tmp_path, _Opener(_Response(body, headers=headers)))
    with pytest.raises(PackVMImageError):
        cache.prefetch(_authority(expected))
    assert not cache.image_path(_authority(expected)).exists()


def test_partial_tamper_and_hardlink_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    content = b"0123456789abcdef"
    opener = _Opener(_Response(content, headers=_headers(content), fail_after_reads=2))
    cache = _cache(tmp_path, opener)
    with pytest.raises(PackVMImageError):
        cache.prefetch(_authority(content))
    partial = cache.image_path(_authority(content)).parent / "partial.img"
    partial.write_bytes(b"tampered")
    with pytest.raises(PackVMImageError, match="changed"):
        cache.prefetch(_authority(content))
    os.link(partial, partial.parent / "alias")
    with pytest.raises(PackVMImageError, match="unsafe"):
        cache.prefetch(_authority(content))


def test_symlinked_entry_and_disk_guard_fail_before_network(tmp_path: Path) -> None:
    content = b"image"
    opener = _Opener(_Response(content, headers=_headers(content)))
    cache = _cache(tmp_path, opener)
    cache.root.mkdir(mode=0o700)
    target = tmp_path / "foreign"
    target.mkdir()
    cache.image_path(_authority(content)).parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(PackVMImageError, match="unsafe"):
        cache.prefetch(_authority(content))
    assert not opener.requests

    guarded = PackVMImageCache(
        tmp_path / "guarded",
        opener=opener,
        disk_usage=lambda _path: SimpleNamespace(free=1),
    )
    with pytest.raises(PackVMImageError, match="insufficient"):
        guarded.prefetch(_authority(content))
    assert not opener.requests


def test_parent_swap_mid_download_never_writes_through_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    content = b"0123456789abcdef"
    cache_root = tmp_path / "owned-cache"
    displaced = tmp_path / "displaced-cache"
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap(reads: int) -> None:
        if reads == 1:
            cache_root.rename(displaced)
            cache_root.symlink_to(outside, target_is_directory=True)

    cache = PackVMImageCache(
        cache_root,
        opener=_Opener(_Response(content, headers=_headers(content), on_read=swap)),
        disk_usage=lambda _path: SimpleNamespace(free=4 * 1024**3),
    )
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(content))
    assert captured.value.code == "packvm_image_parent_swap"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("during_download", [False, True])
def test_cache_ancestor_swap_never_publishes_outside_owned_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    during_download: bool,
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    content = b"0123456789abcdef"
    ancestor = tmp_path / "owned-ancestor"
    ancestor.mkdir()
    root = ancestor / "cache"
    displaced = tmp_path / "displaced-ancestor"
    outside = tmp_path / "outside-ancestor"
    outside.mkdir()
    swapped = False

    def swap(_reads: int = 0) -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        ancestor.rename(displaced)
        ancestor.symlink_to(outside, target_is_directory=True)

    response = _Response(
        content,
        headers=_headers(content),
        on_read=swap if during_download else None,
    )
    cache = PackVMImageCache(
        root,
        opener=_Opener(response),
        disk_usage=lambda _path: SimpleNamespace(free=4 * 1024**3),
    )
    if not during_download:
        swap()
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(content))
    assert captured.value.code == "packvm_image_parent_swap"
    assert list(outside.iterdir()) == []


def test_first_use_missing_ancestor_symlink_never_creates_outside_cache(
    tmp_path: Path,
) -> None:
    owned = tmp_path / "owned"
    outside = tmp_path / "outside-first-use"
    owned.mkdir()
    outside.mkdir()
    missing = owned / "inserted"
    cache = PackVMImageCache(
        missing / "cache",
        opener=_Opener(_Response(b"image", headers=_headers(b"image"))),
        disk_usage=lambda _path: SimpleNamespace(free=4 * 1024**3),
    )
    missing.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(b"image"))
    assert captured.value.code == "packvm_image_parent_swap"
    assert list(outside.iterdir()) == []


def test_first_use_created_chain_cannot_be_replaced_before_trust_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"image"
    ancestor = tmp_path / "missing-ancestor"
    root = ancestor / "cache"
    displaced = tmp_path / "displaced-created-ancestor"
    outside = tmp_path / "outside-created-ancestor"
    outside.mkdir()
    cache = PackVMImageCache(
        root,
        opener=_Opener(_Response(content, headers=_headers(content))),
        disk_usage=lambda _path: SimpleNamespace(free=4 * 1024**3),
    )
    original = cache._create_root_chain_descriptor_relative

    def create_then_replace():
        descriptors, identities = original()
        ancestor.rename(displaced)
        ancestor.symlink_to(outside, target_is_directory=True)
        return descriptors, identities

    monkeypatch.setattr(cache, "_create_root_chain_descriptor_relative", create_then_replace)
    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(_authority(content))
    assert captured.value.code == "packvm_image_parent_swap"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("cache_hit", [False, True])
def test_verified_progress_rejects_terminal_image_replacement(
    tmp_path: Path, cache_hit: bool
) -> None:
    content = b"verified-image"
    cache = _cache(
        tmp_path,
        _Opener(_Response(content, headers=_headers(content))),
    )
    authority = _authority(content)
    if cache_hit:
        cache.prefetch(authority)

    def replace(_progress: PackVMImageProgress) -> None:
        image = cache.image_path(authority)
        displaced = image.with_name("verified-before-callback.img")
        image.rename(displaced)
        image.write_bytes(b"untrusted replacement")
        image.chmod(0o400)

    with pytest.raises(PackVMImageError) as captured:
        cache.prefetch(authority, progress=replace)
    assert captured.value.code == "packvm_image_path_swap"


def test_concurrent_writer_is_rejected_without_waiting(tmp_path: Path) -> None:
    fcntl = pytest.importorskip("fcntl")
    content = b"image"
    opener = _Opener(_Response(content, headers=_headers(content)))
    cache = _cache(tmp_path, opener)
    entry = cache.image_path(_authority(content)).parent
    entry.mkdir(parents=True, mode=0o700)
    lock = cache.root / f"entry-{entry.name}.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PackVMImageError) as captured:
            cache.prefetch(_authority(content))
        assert captured.value.code == "packvm_image_concurrent_writer"
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert not opener.requests


def test_windows_lock_backend_is_lazy_and_never_imports_fcntl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    imported: list[str] = []

    class _Backend:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(_descriptor: int, _mode: int, _size: int) -> None:
            return None

    def load(name: str) -> _Backend:
        imported.append(name)
        assert name == "msvcrt"
        return _Backend()

    monkeypatch.setattr(image_cache.os, "name", "nt")
    monkeypatch.setattr(image_cache.importlib, "import_module", load)
    try:
        image_cache._try_portable_lock(descriptor)
        image_cache._release_portable_lock(descriptor)
    finally:
        os.close(descriptor)
    assert imported == ["msvcrt", "msvcrt"]


def test_cleanup_requires_exact_authenticated_authority(tmp_path: Path) -> None:
    content = b"image"
    cache = _cache(tmp_path, _Opener(_Response(content, headers=_headers(content))))
    authority = _authority(content)
    verified = cache.prefetch(authority)
    wrong = _authority(content, architecture="amd64")
    with pytest.raises(FileNotFoundError):
        cache.cleanup(wrong)
    assert cache.cleanup(authority) is True
    assert not verified.path.exists()


def test_content_addressed_reuse_records_acquisition_but_allows_new_operation(
    tmp_path: Path,
) -> None:
    content = b"content-addressed-image"
    cache = _cache(tmp_path, _Opener(_Response(content, headers=_headers(content))))
    acquired = _authority(content)
    reused = _authority(
        content,
        plan_digest="sha256:" + "a" * 64,
        session_digest="sha256:" + "b" * 64,
        operation_id="00000000-0000-0000-0000-000000000099",
    )
    verified = cache.prefetch(acquired)
    assert cache.verified(reused).path == verified.path
    metadata = json.loads((verified.path.parent / "published.json").read_text())
    assert metadata["acquired_operation_id"] == acquired.operation_id
    assert cache.cleanup(reused) is True


def test_resume_disk_guard_counts_only_authenticated_remaining_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    content = b"0123456789abcdef"
    root = tmp_path / "owned-cache"
    first = PackVMImageCache(
        root,
        opener=_Opener(_Response(content, headers=_headers(content), fail_after_reads=2)),
        disk_usage=lambda _path: SimpleNamespace(free=4 * 1024**3),
    )
    with pytest.raises(PackVMImageError):
        first.prefetch(_authority(content))
    remaining = 8
    free = image_cache.PACKVM_IMAGE_DISK_RESERVE_BYTES + remaining
    resumed = PackVMImageCache(
        root,
        opener=_Opener(_Response(
            content[8:], status=206,
            headers=_headers(content[8:], **{"Content-Range": "bytes 8-15/16"}),
        )),
        disk_usage=lambda _path: SimpleNamespace(free=free),
    )
    assert resumed.remaining_bytes(_authority(content)) == remaining
    assert resumed.prefetch(_authority(content)).path.read_bytes() == content


def test_gc_removes_only_authenticated_old_generations(tmp_path: Path) -> None:
    contents = (b"generation-one", b"generation-two", b"generation-three")
    authorities = [
        _authority(item, source_url=f"https://images.example.test/{index}.img")
        for index, item in enumerate(contents)
    ]
    opener = _Opener(
        *(
            _Response(item, url=authority.source_url, headers=_headers(item))
            for item, authority in zip(contents, authorities, strict=True)
        )
    )
    cache = _cache(tmp_path, opener)
    paths = [cache.prefetch(authority).path for authority in authorities]
    removed = cache.garbage_collect(
        authorities[-1], maximum_generations=2,
        quota_bytes=len(contents[-1]) + len(contents[-2]),
    )
    assert removed == 1
    assert not paths[0].exists()
    assert not paths[0].parent.exists()
    assert paths[1].exists()
    assert paths[2].exists()
    assert cache.garbage_collect(
        authorities[-1], maximum_generations=2,
        quota_bytes=len(contents[-1]) + len(contents[-2]),
    ) == 0


def test_gc_preserves_locked_entry_then_removes_directory_but_not_root_lock(
    tmp_path: Path,
) -> None:
    fcntl = pytest.importorskip("fcntl")
    stale_content = b"stale-generation"
    current_content = b"current-generation"
    stale = _authority(
        stale_content, source_url="https://images.example.test/stale-lock.img"
    )
    current = _authority(
        current_content, source_url="https://images.example.test/current-lock.img"
    )
    cache = _cache(
        tmp_path,
        _Opener(
            _Response(stale_content, url=stale.source_url, headers=_headers(stale_content)),
            _Response(
                current_content,
                url=current.source_url,
                headers=_headers(current_content),
            ),
        ),
    )
    stale_entry = cache.prefetch(stale).path.parent
    cache.prefetch(current)
    root_lock = cache.root / f"entry-{stale_entry.name}.lock"
    root_lock_identity = root_lock.stat().st_dev, root_lock.stat().st_ino
    lock_descriptor = os.open(root_lock, os.O_RDWR)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cache.garbage_collect(
            current,
            maximum_generations=1,
            quota_bytes=len(current_content),
        ) == 0
        assert stale_entry.exists()
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)

    assert cache.garbage_collect(
        current,
        maximum_generations=1,
        quota_bytes=len(current_content),
    ) == 1
    assert not stale_entry.exists()
    assert root_lock.exists()
    assert (root_lock.stat().st_dev, root_lock.stat().st_ino) == root_lock_identity
    assert cache.garbage_collect(
        current,
        maximum_generations=1,
        quota_bytes=len(current_content),
    ) == 0
    assert (root_lock.stat().st_dev, root_lock.stat().st_ino) == root_lock_identity


def test_provisioning_pin_allows_stale_gc_and_preserves_current(tmp_path: Path) -> None:
    stale_content = b"stale-generation"
    current_content = b"current-generation"
    stale = _authority(
        stale_content, source_url="https://images.example.test/stale-nested.img"
    )
    current = _authority(
        current_content, source_url="https://images.example.test/current-nested.img"
    )
    cache = _cache(
        tmp_path,
        _Opener(
            _Response(stale_content, url=stale.source_url, headers=_headers(stale_content)),
            _Response(
                current_content,
                url=current.source_url,
                headers=_headers(current_content),
            ),
        ),
    )
    stale_entry = cache.prefetch(stale).path.parent
    current_entry = cache.prefetch(current).path.parent

    with cache.provisioning_image(current) as pinned:
        removed: list[int] = []
        worker = threading.Thread(target=lambda: removed.append(cache.garbage_collect(
            current,
            maximum_generations=1,
            quota_bytes=len(current_content),
        )))
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert removed == [1]
        assert os.pread(pinned.descriptor, len(current_content), 0) == current_content
        assert not stale_entry.exists()
        assert current_entry.exists()

    assert cache.verified(current).digest == current.digest


def test_gc_reclaims_authenticated_stale_partial_but_preserves_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_cache, "PACKVM_IMAGE_CHUNK_BYTES", 4)
    partial_content = b"0123456789abcdef"
    current_content = b"current-generation"
    partial_authority = _authority(
        partial_content, source_url="https://images.example.test/partial.img"
    )
    current_authority = _authority(
        current_content, source_url="https://images.example.test/current.img"
    )
    opener = _Opener(
        _Response(
            partial_content,
            url=partial_authority.source_url,
            headers=_headers(partial_content),
            fail_after_reads=2,
        ),
        _Response(
            current_content,
            url=current_authority.source_url,
            headers=_headers(current_content),
        ),
    )
    cache = _cache(tmp_path, opener)
    with pytest.raises(PackVMImageError):
        cache.prefetch(partial_authority)
    current = cache.prefetch(current_authority)

    assert cache.garbage_collect(
        current_authority,
        maximum_generations=1,
        quota_bytes=len(current_content),
    ) == 1
    assert current.path.exists()
    partial_entry = cache.image_path(partial_authority).parent
    assert not (partial_entry / "partial.img").exists()
    assert not (partial_entry / "partial.json").exists()


@pytest.mark.parametrize("kind", ["stale", "young", "hardlink"])
def test_gc_accounts_uncheckpointed_crash_partial_without_crossing_safety_bounds(
    tmp_path: Path,
    kind: str,
) -> None:
    current_content = b"current-generation"
    orphan_content = b"crash-before-checkpoint"
    current_authority = _authority(current_content)
    orphan_authority = _authority(
        orphan_content, source_url="https://images.example.test/orphan.img"
    )
    cache = _cache(
        tmp_path,
        _Opener(_Response(current_content, headers=_headers(current_content))),
    )
    current = cache.prefetch(current_authority)
    orphan_entry = cache.image_path(orphan_authority).parent
    orphan_entry.mkdir(mode=0o700)
    partial = orphan_entry / "partial.img"
    partial.write_bytes(orphan_content)
    if kind == "stale":
        old = time.time() - 7200
        os.utime(partial, (old, old))
    elif kind == "hardlink":
        os.link(partial, orphan_entry / "alias")

    removed = cache.garbage_collect(
        current_authority,
        maximum_generations=1,
        quota_bytes=len(current_content),
        uncheckpointed_minimum_age_seconds=3600,
    )

    assert current.path.exists()
    if kind == "stale":
        assert removed == 1
        assert not partial.exists()
    else:
        assert removed == 0
        assert partial.exists()


def test_gc_treats_concurrently_disappeared_uncheckpointed_partial_as_reclaimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_content = b"current-generation"
    orphan_content = b"crash-before-checkpoint"
    current_authority = _authority(current_content)
    orphan_authority = _authority(
        orphan_content, source_url="https://images.example.test/orphan-race.img"
    )
    cache = _cache(
        tmp_path,
        _Opener(_Response(current_content, headers=_headers(current_content))),
    )
    current = cache.prefetch(current_authority)
    orphan_entry = cache.image_path(orphan_authority).parent
    orphan_entry.mkdir(mode=0o700)
    partial = orphan_entry / "partial.img"
    partial.write_bytes(orphan_content)
    old = time.time() - 7200
    os.utime(partial, (old, old))
    original_metadata = cache._owned_entry_metadata
    observations = 0

    def reclaim_before_delete(entry, pinned):
        nonlocal observations
        metadata = original_metadata(entry, pinned)
        if entry == orphan_entry:
            observations += 1
            if observations == 2:
                os.unlink("partial.img", dir_fd=pinned.entry_descriptor)
        return metadata

    monkeypatch.setattr(cache, "_owned_entry_metadata", reclaim_before_delete)
    removed = cache.garbage_collect(
        current_authority,
        maximum_generations=1,
        quota_bytes=len(current_content),
        uncheckpointed_minimum_age_seconds=3600,
    )

    assert removed == 0
    assert current.path.exists()
    assert not partial.exists()


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_runtime_path_preflight_uses_encoded_byte_boundary(delta: int) -> None:
    longest = max(PACKVM_LIMA_DERIVED_PATH_SUFFIXES, key=lambda value: len(os.fsencode(value)))
    suffix_bytes = len(os.fsencode("/" + longest))
    target = PACKVM_LIMA_UNIX_PATH_LIMIT_BYTES + delta - suffix_bytes
    root = Path("/" + "x" * (target - 1))
    reason = _packvm_runtime_path_diagnostic(root)
    assert (reason is None) is (delta < 0)


def test_runtime_path_preflight_counts_unicode_bytes() -> None:
    root = Path("/" + "界" * 30)
    assert len(str(root)) < PACKVM_LIMA_UNIX_PATH_LIMIT_BYTES
    assert _packvm_runtime_path_diagnostic(root) is not None
