"""Record-local reuse preserves validation, ownership, limits and fork safety."""

from __future__ import annotations

import copy
import json
import os
import select
import signal
import threading
from collections import Counter, UserDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from ecosystem.defaultspack.domain.runtime_v4 import _record_validation_cache as module
from tests.test_defaultspack_runtime_v4 import _resolve
from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.errors import SchemaValidationError
from tobkiri_protocol.validation import validate_document


class _Validator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def __call__(
        self, document: Any, kind: str, *, reject_authority_fields: bool
    ) -> dict[str, Any]:
        assert reject_authority_fields is True
        self.calls.append((kind, canonical_json(document)))
        return copy.deepcopy(document)


@pytest.fixture(scope="module")
def records() -> dict[str, dict[str, Any]]:
    resolved = _resolve()
    return {
        "profile": dict(resolved.profile),
        "profile_lock": dict(resolved.lock),
        "resolved_plan": dict(resolved.plan),
    }


@pytest.mark.parametrize("kind", ["profile", "profile_lock", "resolved_plan"])
def test_real_record_miss_and_hit_preserve_fresh_nested_copies(records, kind):
    cache = module._RecordValidationCache()
    original = copy.deepcopy(records[kind])
    calls = []

    def validator(document, schema_name, **kwargs):
        calls.append(schema_name)
        return validate_document(document, schema_name, **kwargs)

    first = cache.validate(original, kind, validator=validator)
    second = cache.validate(original, kind, validator=validator)
    assert calls == [kind]
    assert first == second == original
    assert first is not second and first is not original and second is not original
    nested_key = next(key for key, value in original.items() if isinstance(value, list))
    first[nested_key].append({"changed": True})
    second[nested_key].clear()
    assert original == records[kind]
    assert cache.validate(original, kind, validator=validator) == records[kind]
    assert calls == [kind]
    assert len(cache._entries) == 1
    assert cache._retained_bytes == len(canonical_json(original))
    assert all(value is None for value in cache._entries.values())


def test_exact_canonical_bytes_kind_version_and_policy_are_keyed(monkeypatch):
    cache = module._RecordValidationCache()
    validator = _Validator()
    document = {"profile_api_version": "io.tobkiri.profile.v5", "items": [1]}
    cache.validate(document, "profile", validator=validator)
    cache.validate(document, "profile", validator=validator)
    cache.validate(document, "profile_lock", validator=validator)
    document["profile_api_version"] = "io.tobkiri.profile.v4"
    cache.validate(document, "profile", validator=validator)
    document["items"].append(2)
    cache.validate(document, "profile", validator=validator)
    monkeypatch.setattr(module, "_VALIDATION_POLICY", ("revised-policy", True))
    cache.validate(document, "profile", validator=validator)
    assert len(validator.calls) == 5
    assert len(cache._entries) == 5


def test_validator_identity_does_not_use_callable_equality():
    class EqualValidator(_Validator):
        __hash__ = None

        def __eq__(self, other):
            return True

    cache = module._RecordValidationCache()
    first, second = EqualValidator(), EqualValidator()
    for validator in (first, second, first, second):
        cache.validate({"items": []}, "profile", validator=validator)
    assert len(first.calls) == len(second.calls) == 1
    assert len(cache._entries) == 2


def test_canonical_equivalence_keeps_fresh_input_order_and_ownership():
    cache = module._RecordValidationCache()
    validator = _Validator()
    first = {"a": [{"value": 1}], "b": 2}
    second = {"b": 2, "a": [{"value": 1}]}
    cache.validate(first, "profile", validator=validator)
    result = cache.validate(second, "profile", validator=validator)
    assert len(validator.calls) == 1
    assert list(result) == ["b", "a"]
    second["a"][0]["value"] = 2
    result["a"][0]["value"] = 3
    assert first["a"][0]["value"] == 1
    assert cache.validate(first, "profile", validator=validator) == first


@pytest.mark.parametrize("kind", ["profile", "profile_lock", "resolved_plan"])
def test_warm_invalid_record_is_revalidated_and_never_retained(records, kind):
    cache = module._RecordValidationCache()
    document = copy.deepcopy(records[kind])
    cache.validate(document, kind, validator=validate_document)
    document["profile_id"] = "invalid ID"
    with pytest.raises(SchemaValidationError) as expected:
        validate_document(document, kind)
    for _ in range(2):
        with pytest.raises(SchemaValidationError) as actual:
            cache.validate(document, kind, validator=validate_document)
        assert str(actual.value) == str(expected.value)
        assert actual.value.diagnostics == expected.value.diagnostics
    assert len(cache._entries) == 1


@pytest.mark.parametrize("document", [{"bad": 1.5}, {"bad": object()}, {1: "bad"}, ["not-object"]])
def test_unsupported_values_keep_original_exception_and_diagnostics(document):
    cache = module._RecordValidationCache()
    with pytest.raises(SchemaValidationError) as expected:
        validate_document(document, "profile")
    with pytest.raises(SchemaValidationError) as actual:
        cache.validate(document, "profile", validator=validate_document)
    assert str(actual.value) == str(expected.value)
    assert actual.value.diagnostics == expected.value.diagnostics
    assert not cache._entries


@pytest.mark.parametrize("form", ["text", "bytes", "mapping", "dict-subclass"])
def test_non_owned_dict_inputs_use_original_validation(records, form):
    class ForeignDict(dict):
        pass

    document = records["profile"]
    value = {
        "text": canonical_json(document).decode(),
        "bytes": canonical_json(document),
        "mapping": UserDict(document),
        "dict-subclass": ForeignDict(document),
    }[form]
    calls = []

    def validator(document, kind, **kwargs):
        calls.append(kind)
        return validate_document(document, kind, **kwargs)

    cache = module._RecordValidationCache()
    for _ in range(2):
        assert cache.validate(value, "profile", validator=validator) == document
    assert calls == ["profile", "profile"] and not cache._entries


def test_other_record_kinds_never_use_the_cache():
    cache = module._RecordValidationCache()
    validator = _Validator()
    for _ in range(2):
        cache.validate({"state": "active"}, "activation", validator=validator)
    assert len(validator.calls) == 2 and not cache._entries


@pytest.mark.parametrize("limit_delta, expected_entries", [(-1, 0), (0, 1), (1, 1)])
def test_exact_record_size_boundary(limit_delta, expected_entries):
    document = {"value": "bounded"}
    size = len(canonical_json(document))
    cache = module._RecordValidationCache(max_record_bytes=size + limit_delta)
    validator = _Validator()
    for _ in range(2):
        assert cache.validate(document, "profile", validator=validator) == document
    assert len(cache._entries) == expected_entries
    assert cache._retained_bytes == size * expected_entries
    assert len(validator.calls) == (1 if expected_entries else 2)


@pytest.mark.parametrize("budget_delta, expected_entries", [(-1, 0), (0, 1), (1, 1)])
def test_exact_aggregate_budget_boundary(budget_delta, expected_entries):
    document = {"value": "bounded"}
    size = len(canonical_json(document))
    cache = module._RecordValidationCache(max_retained_bytes=size + budget_delta)
    validator = _Validator()
    for _ in range(2):
        cache.validate(document, "profile", validator=validator)
    assert len(cache._entries) == expected_entries
    assert cache._retained_bytes == size * expected_entries


def test_entry_eviction_is_exact_lru_and_hits_refresh_recency():
    cache = module._RecordValidationCache(max_entries=2)
    validator = _Validator()
    for value in (1, 2, 1, 3, 1, 2):
        cache.validate({"value": value}, "profile", validator=validator)
    assert [json.loads(raw)["value"] for _, raw in validator.calls] == [1, 2, 3, 2]
    assert [json.loads(key[-1])["value"] for key in cache._entries] == [1, 2]
    assert cache._retained_bytes == sum(len(key[-1]) for key in cache._entries)


def test_aggregate_eviction_removes_multiple_entries_and_counts_key_bytes():
    cache = module._RecordValidationCache(max_retained_bytes=50)
    validator = _Validator()
    for value in ("1", "2", "3", "x" * 25):
        cache.validate({"v": value}, "profile", validator=validator)
    assert [json.loads(key[-1])["v"] for key in cache._entries] == ["3", "x" * 25]
    assert cache._retained_bytes == 42


def test_disabled_cache_retains_nothing():
    cache = module._RecordValidationCache(max_entries=0)
    validator = _Validator()
    for _ in range(2):
        cache.validate({}, "profile", validator=validator)
    assert len(validator.calls) == 2
    assert not cache._entries and cache._retained_bytes == 0


def test_concurrent_misses_validate_outside_lock_and_count_once():
    cache = module._RecordValidationCache()
    barrier = threading.Barrier(4)
    calls = Counter()
    guard = threading.Lock()
    document = {"nested": {"items": [1]}}

    def validator(value, kind, *, reject_authority_fields):
        assert reject_authority_fields is True
        with guard:
            calls[kind] += 1
        barrier.wait(timeout=5)
        return copy.deepcopy(value)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(cache.validate, document, "profile", validator=validator) for _ in range(4)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert calls == {"profile": 4}
    assert len(cache._entries) == 1
    assert cache._retained_bytes == len(canonical_json(document))
    results[0]["nested"]["items"].append(2)
    assert all(result == document for result in results[1:])
    assert cache.validate(document, "profile", validator=validator) == document
    assert calls == {"profile": 4}


def test_validation_errors_are_not_cached_and_next_success_can_warm():
    cache = module._RecordValidationCache()
    calls = []

    def validator(value, kind, *, reject_authority_fields):
        assert reject_authority_fields is True
        calls.append(kind)
        if len(calls) <= 2:
            raise SchemaValidationError("expected failure", diagnostics=("detail",))
        return copy.deepcopy(value)

    for _ in range(2):
        with pytest.raises(SchemaValidationError, match="expected failure"):
            cache.validate({}, "profile", validator=validator)
        assert not cache._entries and cache._retained_bytes == 0
    assert cache.validate({}, "profile", validator=validator) == {}
    assert cache.validate({}, "profile", validator=validator) == {}
    assert len(calls) == 3


def test_concurrent_error_does_not_poison_or_share_another_success():
    cache = module._RecordValidationCache()
    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    calls = 0

    def validator(value, kind, *, reject_authority_fields):
        nonlocal calls
        assert reject_authority_fields is True
        with counter_lock:
            calls += 1
            ordinal = calls
        barrier.wait(timeout=5)
        if ordinal == 1:
            raise SchemaValidationError("this validation failed")
        return copy.deepcopy(value)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(cache.validate, {}, "profile", validator=validator) for _ in range(2)
        ]
        errors = 0
        for future in futures:
            try:
                assert future.result(timeout=10) == {}
            except SchemaValidationError:
                errors += 1
    assert errors == 1 and calls == 2
    assert len(cache._entries) == 1 and cache._retained_bytes == 2
    assert cache.validate({}, "profile", validator=validator) == {}
    assert calls == 2


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_registered_child_reset_does_not_acquire_inherited_mutex(monkeypatch):
    cache = module._RecordValidationCache()
    monkeypatch.setattr(module, "_RECORD_VALIDATION_CACHE", cache)
    validator = _Validator()
    cache.validate({"before": True}, "profile", validator=validator)
    retained = cache._retained_bytes
    acquired, release = threading.Event(), threading.Event()

    def hold_lock():
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
                cache.validate({"after": True}, "profile", validator=validator)
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
        assert cache._retained_bytes == retained
        assert len(cache._entries) == 1
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
