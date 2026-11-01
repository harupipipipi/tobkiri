"""Exact-byte Host metadata reuse preserves validation and ownership."""

from __future__ import annotations

import gc
import os
from pathlib import Path
import select
import signal
import threading
from collections import Counter, UserDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any
import weakref

import pytest

from tobkiri_host import _metadata_validation_cache as module
from tobkiri_protocol.canonical import strict_loads
from tobkiri_protocol.errors import CanonicalizationError, SchemaValidationError
from tobkiri_protocol.validation import validate_document


_FIXTURE = Path(__file__).parent / "fixtures" / "conformance_minimal_echo_pack"
_METADATA = {
    "pack": "pack.v4.json",
    "pack_contract_catalog": "contracts.v4.json",
    "pack_artifact_index": "artifact-index.v4.json",
    "executable_catalog": "executables.v4.json",
}


class _Validator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def __call__(
        self, raw: bytes, kind: str, *, reject_authority_fields: bool
    ) -> dict[str, Any]:
        assert reject_authority_fields is True
        self.calls.append((kind, raw))
        return strict_loads(raw)


@pytest.mark.parametrize("kind", _METADATA)
def test_real_metadata_returns_fresh_nested_objects_and_retains_only_markers(
    kind: str,
) -> None:
    cache = module._MetadataValidationCache()
    raw = (_FIXTURE / _METADATA[kind]).read_bytes()
    original = strict_loads(raw)
    calls = []

    def validator(value: bytes, schema: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((schema, value))
        return validate_document(value, schema, **kwargs)

    first = cache.validate(raw, kind, validator=validator)
    second = cache.validate(raw, kind, validator=validator)
    assert first == second == original
    assert first is not second
    nested_key = next(
        key for key, value in original.items() if isinstance(value, (dict, list))
    )
    assert first[nested_key] is not second[nested_key]
    first[nested_key].clear()
    second[nested_key].clear()
    assert cache.validate(raw, kind, validator=validator) == original
    assert calls == [(kind, raw)]
    assert len(cache._entries) == 1
    assert cache._retained_bytes == len(raw)
    assert all(value is None for value in cache._entries.values())


def test_exact_bytes_kind_and_fixed_policy_are_part_of_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = module._MetadataValidationCache()
    validator = _Validator()
    raw = b'{"items":[1],"version":"v1"}'
    cache.validate(raw, "pack", validator=validator)
    cache.validate(raw, "pack", validator=validator)
    # Equal parsed objects are distinct inputs, including whitespace and order.
    for variant in (
        raw + b" ",
        b'{"version":"v1","items":[1]}',
        b'{"items":[1],"version":"v2"}',
        b'{"items":[2],"version":"v1"}',
    ):
        cache.validate(variant, "pack", validator=validator)
    for kind in _METADATA:
        cache.validate(raw, kind, validator=validator)
    monkeypatch.setattr(module, "_VALIDATION_POLICY", ("revised-policy", True))
    cache.validate(raw, "pack", validator=validator)
    assert len(validator.calls) == len(cache._entries) == 9


def test_validator_identity_ignores_equality_and_hash_collisions() -> None:
    class EqualValidator(_Validator):
        def __hash__(self) -> int:
            return 0

        def __eq__(self, other: object) -> bool:
            return True

    cache = module._MetadataValidationCache()
    first, second = EqualValidator(), EqualValidator()
    for validator in (first, second, first, second):
        cache.validate(b"{}", "pack", validator=validator)
    assert len(first.calls) == len(second.calls) == 1
    assert len(cache._entries) == 2


def test_validator_identity_accepts_unhashable_callables() -> None:
    class UnhashableValidator(_Validator):
        __hash__ = None

        def __eq__(self, other: object) -> bool:
            return True

    cache = module._MetadataValidationCache()
    first, second = UnhashableValidator(), UnhashableValidator()
    for validator in (first, second, first, second):
        cache.validate(b"{}", "pack", validator=validator)
    assert len(first.calls) == len(second.calls) == 1


def test_validator_reference_is_strong_until_entry_eviction() -> None:
    cache = module._MetadataValidationCache(max_entries=1)
    validator = _Validator()
    reference = weakref.ref(validator)
    cache.validate(b"{}", "pack", validator=validator)
    del validator
    gc.collect()
    assert reference() is not None
    cache.validate(b"{}", "pack", validator=_Validator())
    gc.collect()
    assert reference() is None


@pytest.mark.parametrize(
    "transformation",
    ["add", "bool-to-int", "object-order", "mapping", "list-to-tuple"],
)
def test_transforming_validator_results_are_returned_but_never_retained(
    transformation: str,
) -> None:
    cache = module._MetadataValidationCache()
    raw = b'{"nested":{"flag":true},"items":[1]}'
    calls = []

    def validator(value: bytes, kind: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"reject_authority_fields": True}
        calls.append(kind)
        result = strict_loads(value)
        if transformation == "add":
            result["new"] = True
        elif transformation == "bool-to-int":
            result["nested"]["flag"] = 1
        elif transformation == "object-order":
            result = dict(reversed(list(result.items())))
        elif transformation == "mapping":
            result["nested"] = UserDict(result["nested"])
        else:
            result["items"] = tuple(result["items"])
        return result

    first = cache.validate(raw, "pack", validator=validator)
    second = cache.validate(raw, "pack", validator=validator)
    assert first == second and first is not second
    assert calls == ["pack", "pack"]
    assert not cache._entries and cache._retained_bytes == 0


@pytest.mark.parametrize("raw", [b"not-json", b"[]"])
def test_non_object_or_unparseable_validator_successes_are_not_retained(
    raw: bytes,
) -> None:
    cache = module._MetadataValidationCache()
    calls = []

    def validator(value: bytes, kind: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"reject_authority_fields": True}
        calls.append(kind)
        return {"synthetic": True}

    for _ in range(2):
        assert cache.validate(raw, "pack", validator=validator) == {"synthetic": True}
    assert calls == ["pack", "pack"]
    assert not cache._entries and cache._retained_bytes == 0


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'"\xff"',
        b'{"duplicate":1,"duplicate":2}',
        b'{"number":1.5}',
        b'{"number":NaN}',
        b'{"number":9007199254740992}',
        b'{"nested":' + b"[" * 70 + b"0" + b"]" * 70 + b"}",
        b"[]",
        b"{}",
    ],
)
def test_invalid_bytes_keep_validator_diagnostics_and_are_never_retained(
    raw: bytes,
) -> None:
    cache = module._MetadataValidationCache()
    with pytest.raises(SchemaValidationError) as expected:
        validate_document(raw, "pack")
    for _ in range(2):
        with pytest.raises(SchemaValidationError) as actual:
            cache.validate(raw, "pack", validator=validate_document)
        assert str(actual.value) == str(expected.value)
        assert actual.value.diagnostics == expected.value.diagnostics
        assert not cache._entries and cache._retained_bytes == 0


@pytest.mark.parametrize("kind", _METADATA)
def test_warm_success_does_not_retain_authority_or_schema_failures(kind: str) -> None:
    cache = module._MetadataValidationCache()
    raw = (_FIXTURE / _METADATA[kind]).read_bytes()
    cache.validate(raw, kind, validator=validate_document)
    # A new authority field must still fail after the original bytes warmed.
    invalid = raw.rstrip()[:-1] + b',"approved":true}'
    with pytest.raises(SchemaValidationError) as expected:
        validate_document(invalid, kind)
    for _ in range(2):
        with pytest.raises(SchemaValidationError) as actual:
            cache.validate(invalid, kind, validator=validate_document)
        assert str(actual.value) == str(expected.value)
        assert actual.value.diagnostics == expected.value.diagnostics
    assert len(cache._entries) == 1 and cache._retained_bytes == len(raw)


@pytest.mark.parametrize("form", ["text", "dict", "mapping", "bytes-subclass"])
def test_other_input_forms_always_take_the_original_validator_path(form: str) -> None:
    class ForeignBytes(bytes):
        pass

    raw = (_FIXTURE / _METADATA["pack"]).read_bytes()
    document = strict_loads(raw)
    value = {
        "text": raw.decode(),
        "dict": document,
        "mapping": UserDict(document),
        "bytes-subclass": ForeignBytes(raw),
    }[form]
    calls = []

    def validator(value: Any, kind: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"reject_authority_fields": True}
        calls.append(kind)
        return validate_document(value, kind, **kwargs)

    cache = module._MetadataValidationCache()
    for _ in range(2):
        assert cache.validate(value, "pack", validator=validator) == document
    assert calls == ["pack", "pack"]
    assert not cache._entries and cache._retained_bytes == 0


@pytest.mark.parametrize("kind", ["activation", "profile", "unknown"])
def test_other_document_kinds_are_never_cached(kind: str) -> None:
    cache = module._MetadataValidationCache()
    validator = _Validator()
    for _ in range(2):
        cache.validate(b"{}", kind, validator=validator)
    assert len(validator.calls) == 2
    assert not cache._entries and cache._retained_bytes == 0


@pytest.mark.parametrize("parse_failure", ["exception", "non-object"])
def test_hit_parse_fallback_preserves_ordinary_validator_result(
    monkeypatch: pytest.MonkeyPatch, parse_failure: str
) -> None:
    cache = module._MetadataValidationCache()
    raw = (_FIXTURE / _METADATA["pack"]).read_bytes()
    calls = []

    def validator(value: bytes, kind: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(kind)
        return validate_document(value, kind, **kwargs)

    expected = cache.validate(raw, "pack", validator=validator)

    def changed_parser(value: bytes) -> Any:
        if parse_failure == "exception":
            raise CanonicalizationError("parser rejection")
        return []

    monkeypatch.setattr(module, "strict_loads", changed_parser)
    assert cache.validate(raw, "pack", validator=validator) == expected
    assert calls == ["pack", "pack"]


@pytest.mark.parametrize("limit_delta, retained", [(-1, False), (0, True), (1, True)])
@pytest.mark.parametrize("limit", ["max_document_bytes", "max_retained_bytes"])
def test_exact_document_and_aggregate_size_boundaries(
    limit_delta: int, retained: bool, limit: str
) -> None:
    raw = b'{"value":"bounded"}'
    cache = module._MetadataValidationCache(**{limit: len(raw) + limit_delta})
    validator = _Validator()
    for _ in range(2):
        assert cache.validate(raw, "pack", validator=validator) == strict_loads(raw)
    assert len(cache._entries) == int(retained)
    assert cache._retained_bytes == len(raw) * int(retained)
    assert len(validator.calls) == (1 if retained else 2)


@pytest.mark.parametrize("limit", [0, -1])
def test_disabled_entry_budget_retains_nothing(limit: int) -> None:
    cache = module._MetadataValidationCache(max_entries=limit)
    validator = _Validator()
    for _ in range(2):
        cache.validate(b"{}", "pack", validator=validator)
    assert len(validator.calls) == 2
    assert not cache._entries and cache._retained_bytes == 0


def test_oversized_inputs_do_not_evict_existing_successes() -> None:
    cache = module._MetadataValidationCache(max_document_bytes=4, max_entries=1)
    validator = _Validator()
    for raw in (b"{}", b'{"large":true}', b'{"large":true}', b"{}"):
        cache.validate(raw, "pack", validator=validator)
    assert len(validator.calls) == 3
    assert [key[-1] for key in cache._entries] == [b"{}"]
    assert cache._retained_bytes == 2


def test_entry_eviction_is_lru_and_hits_refresh_recency() -> None:
    cache = module._MetadataValidationCache(max_entries=2)
    validator = _Validator()
    for value in (1, 2, 1, 3, 1, 2):
        cache.validate(f'{{"value":{value}}}'.encode(), "pack", validator=validator)
    assert [strict_loads(raw)["value"] for _, raw in validator.calls] == [1, 2, 3, 2]
    assert [strict_loads(key[-1])["value"] for key in cache._entries] == [1, 2]
    assert cache._retained_bytes == sum(len(key[-1]) for key in cache._entries)


def test_byte_eviction_can_remove_multiple_entries_and_counts_raw_keys() -> None:
    cache = module._MetadataValidationCache(max_retained_bytes=50)
    validator = _Validator()
    for value in ("1", "2", "3", "x" * 25):
        cache.validate(f'{{"v":"{value}"}}'.encode(), "pack", validator=validator)
    assert [strict_loads(key[-1])["v"] for key in cache._entries] == ["3", "x" * 25]
    assert cache._retained_bytes == 42


def test_failures_are_not_retained_and_later_success_can_warm() -> None:
    cache = module._MetadataValidationCache()
    calls = []

    def validator(raw: bytes, kind: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"reject_authority_fields": True}
        calls.append(kind)
        if len(calls) <= 2:
            raise SchemaValidationError("expected failure", diagnostics=("detail",))
        return strict_loads(raw)

    for _ in range(2):
        with pytest.raises(SchemaValidationError, match="expected failure") as error:
            cache.validate(b"{}", "pack", validator=validator)
        assert error.value.diagnostics == ("detail",)
        assert not cache._entries and cache._retained_bytes == 0
    assert cache.validate(b"{}", "pack", validator=validator) == {}
    assert cache.validate(b"{}", "pack", validator=validator) == {}
    assert len(calls) == 3


def test_concurrent_misses_validate_outside_mutex_and_count_once() -> None:
    cache = module._MetadataValidationCache()
    barrier = threading.Barrier(4)
    calls = Counter()
    guard = threading.Lock()
    raw = b'{"nested":{"items":[1]}}'

    def validator(value: bytes, kind: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"reject_authority_fields": True}
        with guard:
            calls[kind] += 1
        barrier.wait(timeout=5)
        return strict_loads(value)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(cache.validate, raw, "pack", validator=validator)
            for _ in range(4)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert calls == {"pack": 4}
    assert len(cache._entries) == 1 and cache._retained_bytes == len(raw)
    results[0]["nested"]["items"].append(2)
    assert all(result == strict_loads(raw) for result in results[1:])
    assert cache.validate(raw, "pack", validator=validator) == strict_loads(raw)
    assert calls == {"pack": 4}


def test_concurrent_error_does_not_share_or_poison_another_success() -> None:
    cache = module._MetadataValidationCache()
    barrier = threading.Barrier(2)
    guard = threading.Lock()
    calls = 0

    def validator(raw: bytes, kind: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        assert kwargs == {"reject_authority_fields": True}
        with guard:
            calls += 1
            ordinal = calls
        barrier.wait(timeout=5)
        if ordinal == 1:
            raise SchemaValidationError("this validation failed")
        return strict_loads(raw)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(cache.validate, b"{}", "pack", validator=validator)
            for _ in range(2)
        ]
        errors = 0
        for future in futures:
            try:
                assert future.result(timeout=10) == {}
            except SchemaValidationError:
                errors += 1
    assert errors == 1 and calls == 2
    assert len(cache._entries) == 1 and cache._retained_bytes == 2
    assert cache.validate(b"{}", "pack", validator=validator) == {}
    assert calls == 2


def test_concurrent_distinct_misses_preserve_entry_and_byte_bounds() -> None:
    cache = module._MetadataValidationCache(max_entries=3, max_retained_bytes=30)

    def validate_one(value: int) -> None:
        raw = f'{{"v":{value}}}'.encode()
        assert cache.validate(raw, "pack", validator=strict_validator) == {"v": value}

    def strict_validator(raw: bytes, kind: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"reject_authority_fields": True}
        return strict_loads(raw)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(validate_one, range(80)))
    assert len(cache._entries) <= 3
    assert cache._retained_bytes <= 30
    assert cache._retained_bytes == sum(len(key[-1]) for key in cache._entries)
    assert all(value is None for value in cache._entries.values())


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_registered_fork_reset_discards_cache_and_inherited_mutex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = module._MetadataValidationCache()
    monkeypatch.setattr(module, "_METADATA_VALIDATION_CACHE", cache)
    validator = _Validator()
    cache.validate(b'{"before":true}', "pack", validator=validator)
    retained = cache._retained_bytes
    acquired, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with cache._lock:
            acquired.set()
            release.wait(timeout=15)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(timeout=5)
    read_fd, write_fd = os.pipe()
    child = None
    reaped = False
    try:
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                assert not cache._entries and cache._retained_bytes == 0
                cache.validate(b'{"after":true}', "pack", validator=validator)
                assert len(cache._entries) == 1
                os.write(write_fd, b"reset-and-validated")
                os._exit(0)
            except BaseException:
                os._exit(1)
        os.close(write_fd)
        write_fd = -1
        ready, _, _ = select.select([read_fd], [], [], 8)
        assert ready, "child blocked on inherited cache mutex"
        assert os.read(read_fd, 64) == b"reset-and-validated"
        waited, status = os.waitpid(child, 0)
        reaped = True
        assert waited == child and os.waitstatus_to_exitcode(status) == 0
        assert cache._retained_bytes == retained and len(cache._entries) == 1
    finally:
        release.set()
        holder.join(timeout=5)
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if child and not reaped:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
    assert not holder.is_alive()
