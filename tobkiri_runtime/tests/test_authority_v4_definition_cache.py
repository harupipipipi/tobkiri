"""Definition reuse must preserve fresh authority and storage checks."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, Iterator

import pytest

from core_runtime.authority._definition_cache import _DefinitionCache
from core_runtime.authority.v4_models import (
    AuthorityDenied,
    DomainState,
    GrantRecord,
    ProviderAuthorityRecord,
)
from core_runtime.authority.v4_store import AuthorityStore, AuthorityStoreError
from tests.test_authority_v4_lifecycle import _Harness, _digest, _scope


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[_Harness]:
    value = _Harness(tmp_path)
    try:
        yield value
    finally:
        value.store.close()


def _listing(store: AuthorityStore, kind: str) -> list[Any]:
    return store.list_grants() if kind == "grant" else store.list_provider_authorities()


def _definition(harness: _Harness, kind: str) -> GrantRecord | ProviderAuthorityRecord:
    return harness.grant if kind == "grant" else harness.provider


def _changed_id(
    definition: GrantRecord | ProviderAuthorityRecord,
    identity: str,
) -> GrantRecord | ProviderAuthorityRecord:
    return (
        replace(definition, grant_id=identity)
        if isinstance(definition, GrantRecord)
        else replace(definition, record_id=identity)
    )


@pytest.mark.parametrize("kind", ["grant", "provider_authority"])
def test_reuse_reads_current_rows_and_authenticates_only_changed_definitions(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    store = harness.store
    decrypts = 0
    original = store._decrypt_payload

    def counted(ciphertext: bytes, fernet: Any) -> dict[str, Any]:
        nonlocal decrypts
        decrypts += 1
        return original(ciphertext, fernet)

    monkeypatch.setattr(store, "_decrypt_payload", counted)
    first = _listing(store, kind)
    second = _listing(store, kind)
    assert decrypts == 1
    assert first is not second and first[0] is second[0]
    first.clear()
    assert len(_listing(store, kind)) == 1

    # A separately opened writer models authority updates from another owner.
    with AuthorityStore(store.path) as writer:
        added = _changed_id(_definition(harness, kind), "added-definition")
        writer.put_record(added)
    assert len(_listing(store, kind)) == 2
    assert decrypts == 2

    with AuthorityStore(store.path) as writer:
        updated = replace(added, expires_at=2000.0)
        writer.put_record(updated, replace=True)
    assert updated in _listing(store, kind)
    assert decrypts == 3

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DELETE FROM authority_records WHERE record_type=? AND record_id=?",
            (kind, "added-definition"),
        )
    assert len(_listing(store, kind)) == 1
    assert decrypts == 3


@pytest.mark.parametrize("kind", ["grant", "provider_authority"])
@pytest.mark.parametrize("tamper", ["ciphertext", "digest"])
def test_warm_definition_cache_rejects_row_tampering(
    harness: _Harness,
    kind: str,
    tamper: str,
) -> None:
    _listing(harness.store, kind)
    column, value = (
        ("encrypted_payload", b"not-authenticated")
        if tamper == "ciphertext"
        else ("record_digest", _digest("tampered"))
    )
    with sqlite3.connect(harness.store.path) as connection:
        connection.execute(
            f"UPDATE authority_records SET {column}=? WHERE record_type=?",
            (value, kind),
        )
    with pytest.raises(AuthorityStoreError, match="authentication|digest mismatch"):
        _listing(harness.store, kind)


@pytest.mark.parametrize("kind", ["grant", "provider_authority"])
def test_same_plaintext_with_new_ciphertext_is_reauthenticated(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    original = _listing(harness.store, kind)[0]
    harness.store.put_record(_definition(harness, kind), replace=True)
    calls = 0
    decrypt = harness.store._decrypt_payload

    def counted(ciphertext: bytes, fernet: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return decrypt(ciphertext, fernet)

    monkeypatch.setattr(harness.store, "_decrypt_payload", counted)
    current = _listing(harness.store, kind)[0]
    assert current == original and current is not original
    assert calls == 1


@pytest.mark.parametrize("kind", ["grant", "provider_authority"])
def test_nested_mutable_legacy_fields_remain_uncached(
    harness: _Harness,
    kind: str,
) -> None:
    definition = _definition(harness, kind)
    mutable = (
        replace(definition, revoked=[])
        if isinstance(definition, GrantRecord)
        else replace(definition, host_broker_binding=[])
    )
    harness.store.put_record(mutable, replace=True)
    first = _listing(harness.store, kind)[0]
    field = "revoked" if kind == "grant" else "host_broker_binding"
    getattr(first, field).append("mutated by reader")
    second = _listing(harness.store, kind)[0]
    assert first is not second and getattr(second, field) == []
    assert not harness.store._definition_cache._entries


def test_reused_definitions_are_deeply_immutable(harness: _Harness) -> None:
    grant = harness.store.list_grants()[0]
    provider = harness.store.list_provider_authorities()[0]
    for record in (grant, provider):
        with pytest.raises(FrozenInstanceError):
            record.revoked = True
        with pytest.raises(TypeError):
            record.scope.dimensions["path"] = ("/attacker",)
        with pytest.raises(TypeError):
            record.scope.quotas["max_bytes"] = 999999
    with pytest.raises(FrozenInstanceError):
        grant.caller.function_id = "attacker"
    with pytest.raises(FrozenInstanceError):
        provider.provider.operation_id = "attacker"


@pytest.mark.parametrize("entrypoint", ["check_static_path", "authorize"])
@pytest.mark.parametrize(
    "change",
    ["grant_revoke", "provider_revoke", "approval", "time", "usage", "domain"],
)
def test_warm_definitions_do_not_cache_authorization(
    harness: _Harness,
    entrypoint: str,
    change: str,
) -> None:
    if change == "time":
        harness.store.put_record(replace(harness.grant, expires_at=1001), replace=True)
    if change == "usage":
        harness.store.put_record(replace(harness.grant, max_uses=1), replace=True)
    harness.kernel.check_static_path(harness.context(), harness.scope)
    cached_grant = harness.store.list_grants()[0]
    cached_provider = harness.store.list_provider_authorities()[0]
    if change in {"grant_revoke", "provider_revoke"}:
        harness.store.revoke(
            target_kind="grant" if change == "grant_revoke" else "provider_authority",
            target_id=(
                harness.grant.grant_id
                if change == "grant_revoke"
                else harness.provider.record_id
            ),
            reason="fresh cache regression",
        )
    elif change == "approval":
        harness.store.put_record(
            replace(harness.approval, decision="denied"),
            replace=True,
        )
    elif change == "time":
        harness.clock.value = 1002
    elif change == "usage":
        harness.kernel.authorize(harness.context(request_id="reserved"), harness.scope)
    else:
        harness.store.transition_domain(
            harness.target_domain.domain_id,
            expected_boot_epoch=1,
            expected_state=DomainState.ACTIVE,
            new_state=DomainState.FENCED,
        )
    assert harness.store.list_grants()[0] is cached_grant
    assert harness.store.list_provider_authorities()[0] is cached_provider
    with pytest.raises(AuthorityDenied):
        getattr(harness.kernel, entrypoint)(harness.context(), harness.scope)


@pytest.mark.parametrize("kind", ["grant", "provider_authority"])
def test_new_ambiguous_definition_is_visible_after_cache_warms(
    harness: _Harness,
    kind: str,
) -> None:
    harness.kernel.check_static_path(harness.context(), harness.scope)
    added = _changed_id(_definition(harness, kind), "ambiguous-definition")
    if isinstance(added, ProviderAuthorityRecord):
        added = replace(added, scope=_scope(paths=("/safe", "/other")))
    harness.store.put_record(added)
    with pytest.raises(AuthorityDenied, match="missing or ambiguous"):
        harness.kernel.check_static_path(harness.context(), harness.scope)


def test_warm_hits_keep_periodic_pinned_key_checks(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(64):
        harness.store.put_record(replace(harness.grant, grant_id=f"grant-{index + 2}"))
    harness.store.list_grants()
    checks = 0
    original = harness.store._assert_crypto_material

    def counted() -> None:
        nonlocal checks
        checks += 1
        original()

    def unexpected_decrypt(*args: Any) -> Any:
        pytest.fail("unchanged warm row was decrypted again")

    monkeypatch.setattr(harness.store, "_assert_crypto_material", counted)
    monkeypatch.setattr(harness.store, "_decrypt_payload", unexpected_decrypt)
    assert len(harness.store.list_grants()) == 65
    assert checks == 4


@pytest.mark.skipif(os.name == "nt", reason="POSIX key replacement race fixture")
def test_warm_hit_rejects_key_replacement_during_scan(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.store.list_grants()
    cache = harness.store._definition_cache
    original = cache.get

    def swap(key: Any) -> Any:
        result = original(key)
        replacement = harness.store.key_path.with_name("replacement.key")
        replacement.write_bytes(harness.store.key_path.read_bytes())
        os.replace(replacement, harness.store.key_path)
        return result

    monkeypatch.setattr(cache, "get", swap)
    with pytest.raises(AuthorityStoreError, match="key path is unsafe"):
        harness.store.list_grants()


def test_close_clears_cache_and_rejects_warm_listing(harness: _Harness) -> None:
    harness.store.list_grants()
    assert harness.store._definition_cache._entries
    harness.store.close()
    assert not harness.store._definition_cache._entries
    assert harness.store._definition_cache._key_bytes == 0
    with pytest.raises(AuthorityStoreError, match="closed"):
        harness.store.list_grants()


def _fork_listing(store: AuthorityStore, sender: Any) -> None:
    try:
        store.list_grants()
    except AuthorityStoreError as error:
        sender.send(str(error))
    else:
        sender.send("unexpected cache access")
    finally:
        sender.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_warm_cache_is_fenced_after_fork(harness: _Harness) -> None:
    harness.store.list_grants()
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_fork_listing, args=(harness.store, sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(10), "forked store did not fail closed promptly"
        assert "after fork" in receiver.recv()
        process.join(10)
        assert process.exitcode == 0
    finally:
        receiver.close()
        process.join(10)


@pytest.mark.parametrize("component", range(4))
def test_definition_key_requires_type_id_digest_and_exact_ciphertext(
    harness: _Harness,
    component: int,
) -> None:
    cache = _DefinitionCache()
    key = ("grant", "id", "digest", b"ciphertext")
    cache.put(key, harness.grant)
    changed = list(key)
    changed[component] = b"different" if component == 3 else "different"
    assert cache.get(tuple(changed)) is None
    assert cache.get(key) is harness.grant


@pytest.mark.parametrize("limit", ["entries", "bytes"])
def test_definition_cache_bounds_lru_and_oversized_skip(
    harness: _Harness,
    limit: str,
) -> None:
    cache = _DefinitionCache(
        max_entries=2 if limit == "entries" else 99,
        max_key_bytes=100 if limit == "entries" else 8,
    )
    keys = [("g", str(index), "d", b"x") for index in range(3)]
    cache.put(keys[0], harness.grant)
    cache.put(keys[1], harness.grant)
    assert cache.get(keys[0]) is harness.grant
    cache.put(keys[2], harness.grant)
    assert cache.get(keys[1]) is None
    assert cache.get(keys[0]) is harness.grant
    assert cache.get(keys[2]) is harness.grant
    cache.put(("g", "oversized", "d", b"x" * 101), harness.grant)
    assert len(cache._entries) == 2
    cache.put(keys[2], harness.grant)
    assert cache._key_bytes == 8
    cache.clear()
    assert not cache._entries and cache._key_bytes == 0
