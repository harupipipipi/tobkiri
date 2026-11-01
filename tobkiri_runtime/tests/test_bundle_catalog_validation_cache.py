"""Repeated bundle reads retain byte, schema, and filesystem validation."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tobkiri_protocol import bundle_catalog
from tobkiri_protocol.bundle_catalog import BundleIntegrityError, BundledCatalog
from tobkiri_protocol.errors import SchemaValidationError


@pytest.fixture
def bundle(tmp_path: Path) -> Iterator[Path]:
    """Copy one real manifest into a minimal, digest-pinned bundle."""

    source = (
        Path(__file__).resolve().parents[1]
        / "ecosystem/defaultspack/v4/packs/defaultspack.pack.v4.json"
    )
    (tmp_path / "pack.json").write_bytes(source.read_bytes())
    _write_lock(tmp_path)
    bundle_catalog._validated_document.cache_clear()
    bundle_catalog._clear_large_validation_cache()
    try:
        yield tmp_path
    finally:
        bundle_catalog._validated_document.cache_clear()
        bundle_catalog._clear_large_validation_cache()


def _write_lock(root: Path, relative: str = "pack.json") -> None:
    raw = (root / relative).read_bytes()
    lock = {
        "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
        "entries": [
            {
                "path": relative,
                "kind": "pack",
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            }
        ],
    }
    (root / "bundle.lock.json").write_text(json.dumps(lock), encoding="utf-8")


def _padded(raw: bytes, size: int) -> bytes:
    assert len(raw) <= size, (len(raw), size)
    return raw + b" " * (size - len(raw))


def _replace(bundle: Path, raw: bytes) -> None:
    (bundle / "pack.json").write_bytes(raw)
    _write_lock(bundle)


def _large_bundle(bundle: Path, size: int | None = None) -> bytes:
    if size is None:
        size = bundle_catalog._MAX_CACHED_DOCUMENT_BYTES + 1024
    raw = _padded((bundle / "pack.json").read_bytes(), size)
    _replace(bundle, raw)
    return raw


def _variant(raw: bytes, index: int) -> bytes:
    document = json.loads(raw)
    document["pack"]["description"] = f"cache regression {index:04d}"
    return _padded(json.dumps(document).encode("utf-8"), len(raw))


def _record_validation(monkeypatch: pytest.MonkeyPatch) -> list[tuple[bytes, str]]:
    original = bundle_catalog.validate_document
    calls: list[tuple[bytes, str]] = []
    lock = threading.Lock()

    def record(raw: bytes, kind: str) -> dict[str, Any]:
        with lock:
            calls.append((raw, kind))
        return original(raw, kind)

    monkeypatch.setattr(bundle_catalog, "validate_document", record)
    return calls


def _assert_large_cache_bounds() -> None:
    with bundle_catalog._LARGE_CACHE_LOCK:
        assert bundle_catalog._LARGE_CACHE_BYTES == sum(
            len(raw) for raw, _ in bundle_catalog._LARGE_CACHE
        )
        assert (
            bundle_catalog._LARGE_CACHE_BYTES <= bundle_catalog._MAX_LARGE_CACHE_BYTES
        )
        assert (
            len(bundle_catalog._LARGE_CACHE) <= bundle_catalog._MAX_LARGE_CACHE_ENTRIES
        )
        assert all(
            bundle_catalog._MAX_CACHED_DOCUMENT_BYTES
            < len(raw)
            <= bundle_catalog._MAX_LARGE_DOCUMENT_BYTES
            for raw, _ in bundle_catalog._LARGE_CACHE
        )


def test_repeated_reads_validate_once_and_do_not_share_mutable_documents(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validate = bundle_catalog.validate_document
    calls: list[str] = []

    def record(raw: bytes, kind: str) -> dict[str, Any]:
        calls.append(kind)
        return validate(raw, kind)

    monkeypatch.setattr(bundle_catalog, "validate_document", record)
    first = BundledCatalog.load(bundle)
    first.packs["defaultspack"]["pack"]["id"] = "changed"
    second = BundledCatalog.load(bundle)
    assert second.packs["defaultspack"]["pack"]["id"] == "defaultspack"
    assert calls == ["pack"]


def test_warm_read_rejects_changed_bytes_even_with_same_size_and_timestamp(
    bundle: Path,
) -> None:
    BundledCatalog.load(bundle)
    path = bundle / "pack.json"
    before = path.stat()
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b"defaultspack", b"defaultspacx", 1))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(BundleIntegrityError, match="digest changed"):
        BundledCatalog.load(bundle)


def test_repinning_invalid_document_does_not_reuse_old_validation(bundle: Path) -> None:
    BundledCatalog.load(bundle)
    path = bundle / "pack.json"
    document = json.loads(path.read_bytes())
    document["pack"]["id"] = "invalid ID"
    path.write_text(json.dumps(document), encoding="utf-8")
    _write_lock(bundle)
    with pytest.raises(BundleIntegrityError, match="invalid pack document"):
        BundledCatalog.load(bundle)


@pytest.mark.parametrize("replacement", ["symlink", "missing"])
def test_warm_read_still_checks_the_artifact_path(
    bundle: Path, replacement: str
) -> None:
    BundledCatalog.load(bundle)
    path = bundle / "pack.json"
    saved = bundle / "saved.json"
    path.rename(saved)
    if replacement == "symlink":
        path.symlink_to(saved)
    with pytest.raises((BundleIntegrityError, FileNotFoundError)):
        BundledCatalog.load(bundle)


def test_large_valid_documents_are_not_retained(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _large_bundle(bundle, bundle_catalog._MAX_LARGE_DOCUMENT_BYTES + 1)
    calls = _record_validation(monkeypatch)
    BundledCatalog.load(bundle)
    BundledCatalog.load(bundle)
    assert calls == [(raw, "pack"), (raw, "pack")]
    assert bundle_catalog._validated_document.cache_info().currsize == 0
    assert not bundle_catalog._LARGE_CACHE
    assert bundle_catalog._LARGE_CACHE_BYTES == 0


def test_large_exact_byte_hit_and_semantically_identical_byte_miss(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _large_bundle(bundle)
    calls = _record_validation(monkeypatch)
    first = BundledCatalog.load(bundle)
    second = BundledCatalog.load(bundle)
    assert first.packs == second.packs
    assert calls == [(raw, "pack")]
    changed = raw[:-1] + b"\t"
    assert changed != raw and json.loads(changed) == json.loads(raw)
    _replace(bundle, changed)
    third = BundledCatalog.load(bundle)
    assert third.packs == first.packs
    assert calls == [(raw, "pack"), (changed, "pack")]
    assert set(bundle_catalog._LARGE_CACHE) == {(raw, "pack"), (changed, "pack")}
    assert bundle_catalog._validated_document.cache_info().currsize == 0
    _assert_large_cache_bounds()


def test_large_warm_read_rejects_same_size_mtime_tamper(bundle: Path) -> None:
    raw = _large_bundle(bundle)
    BundledCatalog.load(bundle)
    path = bundle / "pack.json"
    before = path.stat()
    changed = raw.replace(b"defaultspack", b"defaultspacx", 1)
    assert changed != raw and len(changed) == len(raw)
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(BundleIntegrityError, match="digest changed"):
        BundledCatalog.load(bundle)
    assert set(bundle_catalog._LARGE_CACHE) == {(raw, "pack")}


@pytest.mark.parametrize("violation", ["schema", "authority", "date"])
def test_large_repinned_invalid_document_is_rejected_each_time(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    violation: str,
) -> None:
    raw = _large_bundle(bundle)
    calls = _record_validation(monkeypatch)
    BundledCatalog.load(bundle)
    document = json.loads(raw)
    if violation == "schema":
        document["pack"]["id"] = "invalid ID"
    elif violation == "authority":
        # This schema-permissive object still requires semantic validation.
        document["requirements"]["network"]["approved"] = True
    else:
        document["migration"]["sunset_at"] = "2026-02-30"
    invalid = _padded(json.dumps(document).encode("utf-8"), len(raw))
    _replace(bundle, invalid)
    for _ in range(2):
        with pytest.raises(
            BundleIntegrityError, match="invalid pack document"
        ) as caught:
            BundledCatalog.load(bundle)
        cause = caught.value.__cause__
        assert isinstance(cause, SchemaValidationError)
        diagnostics = " ".join(cause.diagnostics)
        if violation == "authority":
            assert "forbidden authority-bearing field" in diagnostics
        elif violation == "date":
            assert "date" in diagnostics
    assert calls == [(raw, "pack"), (invalid, "pack"), (invalid, "pack")]
    assert set(bundle_catalog._LARGE_CACHE) == {(raw, "pack")}
    _assert_large_cache_bounds()


@pytest.mark.parametrize("component", ["root", "parent", "lock", "document"])
@pytest.mark.parametrize("replacement", ["symlink", "missing"])
def test_large_warm_read_still_checks_paths(
    bundle: Path,
    component: str,
    replacement: str,
) -> None:
    raw = _large_bundle(bundle)
    if component == "parent":
        (bundle / "nested").mkdir()
        (bundle / "pack.json").rename(bundle / "nested" / "pack.json")
        _write_lock(bundle, "nested/pack.json")
    BundledCatalog.load(bundle)
    target = bundle
    if component == "root":
        saved = bundle.with_name(bundle.name + "-saved")
        bundle.rename(saved)
        if replacement == "symlink":
            bundle.symlink_to(saved, target_is_directory=True)
    else:
        name = {
            "parent": "nested",
            "lock": "bundle.lock.json",
            "document": "pack.json",
        }[component]
        path = bundle / name
        saved = bundle / ("saved-" + name)
        path.rename(saved)
        if replacement == "symlink":
            path.symlink_to(saved, target_is_directory=component == "parent")
    with pytest.raises((BundleIntegrityError, FileNotFoundError)):
        BundledCatalog.load(target)
    assert set(bundle_catalog._LARGE_CACHE) == {(raw, "pack")}
    _assert_large_cache_bounds()


def test_large_lru_entry_limit_eviction(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _large_bundle(bundle)
    capacity = bundle_catalog._MAX_LARGE_CACHE_ENTRIES
    variants = [_variant(raw, index) for index in range(capacity + 1)]
    calls = _record_validation(monkeypatch)
    for value in variants[:capacity]:
        _replace(bundle, value)
        BundledCatalog.load(bundle)
        _assert_large_cache_bounds()
    assert len(bundle_catalog._LARGE_CACHE) == capacity
    assert bundle_catalog._LARGE_CACHE_BYTES < bundle_catalog._MAX_LARGE_CACHE_BYTES
    _replace(bundle, variants[0])
    BundledCatalog.load(bundle)  # Refresh oldest; entry 1 is now least recently used.
    assert len(calls) == capacity
    _replace(bundle, variants[-1])
    BundledCatalog.load(bundle)
    expected = [*variants[2:capacity], variants[0], variants[-1]]
    assert list(bundle_catalog._LARGE_CACHE) == [(value, "pack") for value in expected]
    assert calls == [(value, "pack") for value in variants]
    _replace(bundle, variants[1])
    BundledCatalog.load(bundle)  # Evicted exact bytes must validate again.
    assert calls == [(value, "pack") for value in [*variants, variants[1]]]
    assert list(bundle_catalog._LARGE_CACHE) == [
        (value, "pack") for value in [*expected[1:], variants[1]]
    ]
    _assert_large_cache_bounds()


def test_large_lru_aggregate_byte_limit_eviction(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _large_bundle(bundle, bundle_catalog._MAX_LARGE_DOCUMENT_BYTES)
    capacity = bundle_catalog._MAX_LARGE_CACHE_BYTES // len(raw)
    assert capacity < bundle_catalog._MAX_LARGE_CACHE_ENTRIES
    variants = [_variant(raw, index) for index in range(capacity + 1)]
    calls = _record_validation(monkeypatch)
    for value in variants[:capacity]:
        _replace(bundle, value)
        BundledCatalog.load(bundle)
        _assert_large_cache_bounds()
    assert bundle_catalog._LARGE_CACHE_BYTES == bundle_catalog._MAX_LARGE_CACHE_BYTES
    _replace(bundle, variants[0])
    BundledCatalog.load(bundle)
    assert len(calls) == capacity
    _replace(bundle, variants[-1])
    BundledCatalog.load(bundle)
    expected = [*variants[2:capacity], variants[0], variants[-1]]
    assert list(bundle_catalog._LARGE_CACHE) == [(value, "pack") for value in expected]
    assert bundle_catalog._LARGE_CACHE_BYTES == capacity * len(raw)
    assert calls == [(value, "pack") for value in variants]
    _replace(bundle, variants[1])
    BundledCatalog.load(bundle)
    assert calls == [(value, "pack") for value in [*variants, variants[1]]]
    assert list(bundle_catalog._LARGE_CACHE) == [
        (value, "pack") for value in [*expected[1:], variants[1]]
    ]
    _assert_large_cache_bounds()


def test_large_aggregate_limit_evicts_multiple_smaller_documents(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _large_bundle(bundle)
    variants = [_variant(raw, index) for index in range(3)]
    monkeypatch.setattr(bundle_catalog, "_MAX_LARGE_CACHE_BYTES", 3 * len(raw))
    calls = _record_validation(monkeypatch)
    for value in variants:
        _replace(bundle, value)
        BundledCatalog.load(bundle)
        _assert_large_cache_bounds()
    assert list(bundle_catalog._LARGE_CACHE) == [(value, "pack") for value in variants]
    # Two evictions leave the cache one byte over budget, requiring a third.
    larger = _padded(variants[0], 2 * len(raw) + 1)
    assert len(larger) <= bundle_catalog._MAX_LARGE_DOCUMENT_BYTES
    _replace(bundle, larger)
    BundledCatalog.load(bundle)
    BundledCatalog.load(bundle)
    assert calls == [(value, "pack") for value in [*variants, larger]]
    assert list(bundle_catalog._LARGE_CACHE) == [(larger, "pack")]
    assert bundle_catalog._LARGE_CACHE_BYTES == len(larger)
    _assert_large_cache_bounds()


def test_large_document_bigger_than_aggregate_budget_is_not_retained(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = _large_bundle(bundle)
    monkeypatch.setattr(bundle_catalog, "_MAX_LARGE_CACHE_BYTES", len(retained))
    calls = _record_validation(monkeypatch)
    BundledCatalog.load(bundle)
    raw = retained + b" "
    assert len(raw) <= bundle_catalog._MAX_LARGE_DOCUMENT_BYTES
    _replace(bundle, raw)
    BundledCatalog.load(bundle)
    BundledCatalog.load(bundle)
    assert calls == [(retained, "pack"), (raw, "pack"), (raw, "pack")]
    assert set(bundle_catalog._LARGE_CACHE) == {(retained, "pack")}
    assert bundle_catalog._LARGE_CACHE_BYTES == len(retained)
    assert bundle_catalog._validated_document.cache_info().currsize == 0
    _assert_large_cache_bounds()


@pytest.mark.parametrize("entry_budget", [0, -1])
def test_large_document_bypasses_disabled_entry_budget(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_budget: int,
) -> None:
    raw = _large_bundle(bundle)
    monkeypatch.setattr(bundle_catalog, "_MAX_LARGE_CACHE_ENTRIES", entry_budget)
    calls = _record_validation(monkeypatch)
    BundledCatalog.load(bundle)
    BundledCatalog.load(bundle)
    assert calls == [(raw, "pack"), (raw, "pack")]
    assert not bundle_catalog._LARGE_CACHE
    assert bundle_catalog._LARGE_CACHE_BYTES == 0
    assert bundle_catalog._validated_document.cache_info().currsize == 0


def test_large_transient_validation_failure_is_not_cached(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _large_bundle(bundle)
    original = bundle_catalog.validate_document
    calls: list[tuple[bytes, str]] = []

    def fail_once(value: bytes, kind: str) -> dict[str, Any]:
        calls.append((value, kind))
        if len(calls) == 1:
            raise SchemaValidationError("transient rejection")
        return original(value, kind)

    monkeypatch.setattr(bundle_catalog, "validate_document", fail_once)
    with pytest.raises(BundleIntegrityError, match="transient rejection"):
        BundledCatalog.load(bundle)
    assert not bundle_catalog._LARGE_CACHE
    assert bundle_catalog._LARGE_CACHE_BYTES == 0
    BundledCatalog.load(bundle)
    BundledCatalog.load(bundle)
    assert calls == [(raw, "pack"), (raw, "pack")]
    assert set(bundle_catalog._LARGE_CACHE) == {(raw, "pack")}
    _assert_large_cache_bounds()


def test_large_schema_kind_is_part_of_exact_byte_key(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _large_bundle(bundle)
    calls = _record_validation(monkeypatch)
    # The schema alias tests the private cache key, without changing the public
    # BundledCatalog kind allowlist.
    for kind in ("pack", "pack_manifest", "pack", "pack_manifest"):
        document = bundle_catalog._validated_large_document(raw, kind)
        assert document["pack"]["id"] == "defaultspack"
    assert calls == [(raw, "pack"), (raw, "pack_manifest")]
    for _ in range(2):
        with pytest.raises(SchemaValidationError):
            bundle_catalog._validated_large_document(raw, "shell")
    assert calls[-2:] == [(raw, "shell"), (raw, "shell")]
    assert set(bundle_catalog._LARGE_CACHE) == {(raw, "pack"), (raw, "pack_manifest")}
    assert bundle_catalog._validated_document.cache_info().currsize == 0
    _assert_large_cache_bounds()


def test_large_parallel_cold_loads_preserve_accounting_and_mutable_isolation(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _large_bundle(bundle)
    original = bundle_catalog.validate_document
    original(raw, "pack")  # Initialize immutable schema data before threads.
    workers = 4
    barrier = threading.Barrier(workers)
    count_lock = threading.Lock()
    calls: list[tuple[bytes, str]] = []

    def synchronized(value: bytes, kind: str) -> dict[str, Any]:
        with count_lock:
            calls.append((value, kind))
        barrier.wait(timeout=20)
        return original(value, kind)

    monkeypatch.setattr(bundle_catalog, "validate_document", synchronized)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(BundledCatalog.load, bundle) for _ in range(workers)]
        results = [future.result(timeout=30) for future in futures]
    assert calls == [(raw, "pack")] * workers
    assert set(bundle_catalog._LARGE_CACHE) == {(raw, "pack")}
    assert bundle_catalog._LARGE_CACHE_BYTES == len(raw)
    packs = [result.packs["defaultspack"] for result in results]
    assert len({id(pack) for pack in packs}) == workers
    assert len({id(pack["pack"]) for pack in packs}) == workers
    assert len({id(pack["artifacts"]) for pack in packs}) == workers
    packs[0]["pack"]["id"] = "mutated"
    packs[0]["artifacts"].clear()
    for pack in packs[1:]:
        assert pack["pack"]["id"] == "defaultspack" and pack["artifacts"]
    warm = BundledCatalog.load(bundle).packs["defaultspack"]
    assert warm["pack"]["id"] == "defaultspack" and warm["artifacts"]
    assert len(calls) == workers
    _assert_large_cache_bounds()


def test_large_parallel_distinct_misses_keep_bounds(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _large_bundle(bundle)
    variants = [_variant(raw, index) for index in range(4)]
    original = bundle_catalog.validate_document
    original(raw, "pack")
    barrier = threading.Barrier(len(variants))
    count_lock = threading.Lock()
    calls: list[tuple[bytes, str]] = []
    monkeypatch.setattr(bundle_catalog, "_MAX_LARGE_CACHE_ENTRIES", 2)
    monkeypatch.setattr(bundle_catalog, "_MAX_LARGE_CACHE_BYTES", 2 * len(raw))

    def synchronized(value: bytes, kind: str) -> dict[str, Any]:
        with count_lock:
            calls.append((value, kind))
            cold_miss = len(calls) <= len(variants)
        if cold_miss:
            barrier.wait(timeout=20)
        return original(value, kind)

    monkeypatch.setattr(bundle_catalog, "validate_document", synchronized)
    with ThreadPoolExecutor(max_workers=len(variants)) as pool:
        futures = [
            pool.submit(bundle_catalog._validated_large_document, value, "pack")
            for value in variants
        ]
        for future in futures:
            assert future.result(timeout=30)["pack"]["id"] == "defaultspack"
    expected_keys = {(value, "pack") for value in variants}
    retained_keys = list(bundle_catalog._LARGE_CACHE)
    assert len(calls) == len(variants) and set(calls) == expected_keys
    assert len(retained_keys) == 2 and set(retained_keys) <= expected_keys
    assert bundle_catalog._LARGE_CACHE_BYTES == 2 * len(raw)
    _assert_large_cache_bounds()
    for value, kind in retained_keys:
        bundle_catalog._validated_large_document(value, kind)
    assert len(calls) == len(variants)
    evicted = next(key for key in expected_keys if key not in retained_keys)
    bundle_catalog._validated_large_document(*evicted)
    assert calls[-1] == evicted and len(calls) == len(variants) + 1
    assert list(bundle_catalog._LARGE_CACHE) == [retained_keys[-1], evicted]
    _assert_large_cache_bounds()


@pytest.mark.parametrize("size", [64 * 1024, 64 * 1024 + 1])
def test_validation_cache_exact_tier_boundary(
    bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
) -> None:
    raw = _large_bundle(bundle, size)
    assert len(raw) == size
    calls = _record_validation(monkeypatch)
    first = BundledCatalog.load(bundle)
    second = BundledCatalog.load(bundle)
    assert first.packs == second.packs
    assert calls == [(raw, "pack")]
    if size == 64 * 1024:
        assert bundle_catalog._validated_document.cache_info().currsize == 1
        assert not bundle_catalog._LARGE_CACHE
        assert bundle_catalog._LARGE_CACHE_BYTES == 0
    else:
        assert bundle_catalog._validated_document.cache_info().currsize == 0
        assert set(bundle_catalog._LARGE_CACHE) == {(raw, "pack")}
        assert bundle_catalog._LARGE_CACHE_BYTES == size
    _assert_large_cache_bounds()


def test_clear_large_cache_preserves_mutex_and_small_tier(bundle: Path) -> None:
    BundledCatalog.load(bundle)
    _large_bundle(bundle)
    BundledCatalog.load(bundle)
    assert bundle_catalog._validated_document.cache_info().currsize == 1
    assert bundle_catalog._LARGE_CACHE
    mutex = bundle_catalog._LARGE_CACHE_LOCK
    bundle_catalog._clear_large_validation_cache()
    assert bundle_catalog._LARGE_CACHE_LOCK is mutex
    assert not bundle_catalog._LARGE_CACHE
    assert bundle_catalog._LARGE_CACHE_BYTES == 0
    assert bundle_catalog._validated_document.cache_info().currsize == 1


def _wait_child(pid: int, timeout: float) -> int | None:
    """Wait using a real deadline; return the waitpid status or None."""
    deadline = time.monotonic() + timeout
    while True:
        found, status = os.waitpid(pid, os.WNOHANG)
        if found == pid:
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(0.01, remaining))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is unavailable")
def test_large_cache_fork_replaces_inherited_lock_and_drops_pure_cache(
    bundle: Path,
) -> None:
    raw = _large_bundle(bundle)
    # Prime schemas and the large document before introducing another thread.
    BundledCatalog.load(bundle)
    assert set(bundle_catalog._LARGE_CACHE) == {(raw, "pack")}
    assert bundle_catalog._LARGE_CACHE_BYTES == len(raw)
    inherited_lock = bundle_catalog._LARGE_CACHE_LOCK
    parent_keys = tuple(bundle_catalog._LARGE_CACHE)
    parent_bytes = bundle_catalog._LARGE_CACHE_BYTES
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold_parent_lock() -> None:
        with inherited_lock:
            holder_ready.set()
            release_holder.wait()

    holder = threading.Thread(
        target=hold_parent_lock,
        name="large-cache-parent-lock-holder",
        daemon=True,
    )
    read_fd, write_fd = os.pipe()
    pid: int | None = None
    reaped = False
    status: int | None = None
    timed_out = False
    output = b""
    holder.start()
    try:
        assert holder_ready.wait(timeout=3), "parent lock holder did not start"
        # A correct after_in_child callback must not acquire the old lock. Keep
        # it held in the parent until the child exits or the real timeout fires.
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            exit_code = 1
            try:
                os.write(write_fd, b"child_started\n")
                lock_replaced = bundle_catalog._LARGE_CACHE_LOCK is not inherited_lock
                cache_cleared = (
                    not bundle_catalog._LARGE_CACHE
                    and bundle_catalog._LARGE_CACHE_BYTES == 0
                )
                # Without reset, this blocks on the vanished sibling's mutex.
                loaded = BundledCatalog.load(bundle)
                assert lock_replaced, "child retained the inherited lock object"
                assert cache_cleared, "child retained pre-fork large-cache data"
                assert loaded.packs["defaultspack"]["pack"]["id"] == "defaultspack"
                assert set(bundle_catalog._LARGE_CACHE) == {(raw, "pack")}
                assert bundle_catalog._LARGE_CACHE_BYTES == len(raw)
                os.write(write_fd, b"child_cache_reload_ok\n")
                exit_code = 0
            except BaseException as error:
                payload = json.dumps(
                    {"error": type(error).__name__, "detail": str(error)[:500]}
                ).encode("utf-8")
                try:
                    os.write(write_fd, payload + b"\n")
                except OSError:
                    pass
            finally:
                os.close(write_fd)
                os._exit(exit_code)

        os.close(write_fd)
        write_fd = -1
        status = _wait_child(pid, timeout=5)
        if status is None:
            timed_out = True
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            status = _wait_child(pid, timeout=2)
        reaped = status is not None
        if reaped:
            # Only this child inherited the writer and has now exited. Its
            # bounded status messages fit within a single pipe read.
            output = os.read(read_fd, 2048)
    finally:
        release_holder.set()
        holder.join(timeout=2)
        if pid is not None and pid > 0 and not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                status = _wait_child(pid, timeout=2)
                reaped = status is not None
            except ChildProcessError:
                reaped = True
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    assert not holder.is_alive(), "parent cache-lock holder did not stop"
    assert reaped, "fork child could not be reaped after bounded cleanup"
    assert not timed_out, (
        "fork child did not complete within 5 seconds while the other parent "
        f"thread held the inherited cache lock; status={status}, output={output!r}"
    )
    assert status is not None and os.WIFEXITED(status), (status, output)
    assert os.WEXITSTATUS(status) == 0, (status, output)
    assert b"child_cache_reload_ok" in output
    assert bundle_catalog._LARGE_CACHE_LOCK is inherited_lock
    with bundle_catalog._LARGE_CACHE_LOCK:
        assert tuple(bundle_catalog._LARGE_CACHE) == parent_keys
        assert bundle_catalog._LARGE_CACHE_BYTES == parent_bytes
    assert (
        BundledCatalog.load(bundle).packs["defaultspack"]["pack"]["id"]
        == "defaultspack"
    )
