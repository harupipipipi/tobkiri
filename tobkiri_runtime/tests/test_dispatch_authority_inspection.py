"""Fresh durable authority inspections retain execution and persistence fences."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import time

import pytest

from core_runtime.authority.v4_models import AuthorityDenied, LeaseState
from core_runtime.authority.v4_store import AuthorityStoreError
from core_runtime.invocation_scope_v4 import assert_dispatched_invocation
from tests.test_credential_broker_pack import _dispatched_envelope
from tobkiri_host.models import OpaqueAuthorityRef


@pytest.fixture
def dispatched(tmp_path: Path):
    authority, envelope = _dispatched_envelope(tmp_path)
    try:
        yield authority, envelope
    finally:
        authority.store.close()


def _targets(lease):
    return (
        ("function_principal", lease.caller.principal_id),
        ("function_principal", lease.target.principal_id),
        ("execution_domain", lease.caller_domain_id),
        ("execution_domain", lease.target_domain_id),
        ("profile", lease.profile_id),
        ("activation", lease.activation_id),
        ("grant", lease.grant_id),
        ("provider_authority", lease.provider_authority_id),
    )


def test_each_guard_opens_one_fresh_connection_for_all_durable_checks(dispatched, monkeypatch):
    authority, envelope = dispatched
    store = authority.store
    lease = store.inspect_lease_token(envelope.lease.token.decode())[0]
    original_connection, original_revoked = store._connection, store._is_revoked
    opened, checked = [], []

    @contextmanager
    def connection():
        with original_connection() as current:
            opened.append(current)
            yield current

    def revoked(current, kind, identity):
        checked.append((current, kind, identity))
        return original_revoked(current, kind, identity)

    monkeypatch.setattr(store, "_connection", connection)
    monkeypatch.setattr(store, "_is_revoked", revoked)
    assert_dispatched_invocation(envelope, store)
    assert_dispatched_invocation(envelope, store)
    assert len(opened) == 2 and opened[0] is not opened[1]
    for index, current in enumerate(opened):
        assert [entry[1:] for entry in checked[index * 8 : (index + 1) * 8]] == list(
            _targets(lease)
        )
        assert all(entry[0] is current for entry in checked[index * 8 : (index + 1) * 8])


@pytest.mark.parametrize(
    "place,field,value",
    [
        ("context", "caller_principal", OpaqueAuthorityRef("foreign")),
        ("envelope", "target_principal", OpaqueAuthorityRef("foreign")),
        ("envelope", "operation_id", "foreign"),
        ("context", "profile_id", "foreign"),
        ("context", "activation_id", "foreign"),
        ("context", "activation_digest", "foreign"),
        ("context", "plan_digest", "foreign"),
        ("context", "profile_authority_digest", "foreign"),
        ("context", "fencing_token", 99),
        ("context", "caller_domain_id", "foreign"),
        ("context", "caller_boot_epoch", 99),
        ("context", "security_epoch", 99),
        ("context", "target_domain_id", "foreign"),
        ("context", "target_boot_epoch", 99),
        ("context", "request_id", "foreign"),
        ("envelope", "request_digest", "foreign"),
    ],
)
def test_all_existing_envelope_identity_comparisons_still_deny(dispatched, place, field, value):
    authority, envelope = dispatched
    changed = (
        replace(envelope, **{field: value})
        if place == "envelope"
        else replace(envelope, context=replace(envelope.context, **{field: value}))
    )
    with pytest.raises(PermissionError):
        assert_dispatched_invocation(changed, authority.store)


@pytest.mark.parametrize("index", range(9))
def test_fresh_second_guard_observes_each_exact_revocation_and_global(dispatched, index):
    authority, envelope = dispatched
    store = authority.store
    token = envelope.lease.token.decode()
    lease, _, _, revoked = store.inspect_dispatch_authority(token)
    assert revoked is False
    assert_dispatched_invocation(envelope, store)
    kind, identity = _targets(lease)[index] if index < 8 else ("global", "all")
    store.revoke(target_kind=kind, target_id=identity, reason="fixture revocation")
    assert store.inspect_dispatch_authority(token)[3] is True
    with pytest.raises(PermissionError):
        assert_dispatched_invocation(envelope, store)


def test_unrelated_revocation_does_not_deny_authenticated_lease(dispatched):
    authority, envelope = dispatched
    authority.store.revoke(target_kind="function_principal", target_id="other", reason="other")
    assert_dispatched_invocation(envelope, authority.store)


def test_fresh_second_guard_observes_epoch_advance(dispatched):
    authority, envelope = dispatched
    assert_dispatched_invocation(envelope, authority.store)
    authority.store.advance_security_epoch("fixture epoch")
    assert authority.store.inspect_dispatch_authority(envelope.lease.token.decode())[2] == 2
    with pytest.raises(PermissionError):
        assert_dispatched_invocation(envelope, authority.store)


@pytest.mark.parametrize("during", ["cancel", "deadline"])
def test_cancel_or_deadline_crossing_during_inspection_denies(dispatched, monkeypatch, during):
    authority, envelope = dispatched
    if during == "deadline":
        envelope = replace(envelope, deadline_monotonic=time.monotonic() + 10)
    original = authority.store.inspect_dispatch_authority

    def inspect(token):
        result = original(token)
        if during == "cancel":
            envelope.cancellation_requested.set()
        else:
            monkeypatch.setattr(
                "core_runtime.invocation_scope_v4.time.monotonic",
                lambda: envelope.deadline_monotonic + 1,
            )
        return result

    monkeypatch.setattr(authority.store, "inspect_dispatch_authority", inspect)
    with pytest.raises(PermissionError):
        assert_dispatched_invocation(envelope, authority.store)


@pytest.mark.parametrize(
    "tamper", ["malformed", "mac", "unknown", "token_digest", "stored_digest", "ciphertext"]
)
def test_token_and_durable_authentication_fail_closed(dispatched, tamper):
    authority, envelope = dispatched
    store = authority.store
    token = envelope.lease.token.decode()
    lease = store.inspect_lease_token(token)[0]
    if tamper == "malformed":
        token = "malformed"
    elif tamper == "mac":
        token = ("A" if token[0] != "A" else "B") + token[1:]
    elif tamper == "unknown":
        token = store._encode_lease_token(replace(lease, lease_id="unknown"))
    elif tamper == "token_digest":
        token = store._encode_lease_token(replace(lease, request_id="different"))
    else:
        field = "lease_digest" if tamper == "stored_digest" else "encrypted_payload"
        with store._lock, store._connection() as current:
            current.execute(
                f"UPDATE invocation_leases SET {field}=? WHERE lease_id=?",
                ("tampered", lease.lease_id),
            )
    with pytest.raises((AuthorityDenied, AuthorityStoreError)):
        store.inspect_dispatch_authority(token)


def test_wall_clock_expiry_semantics_remain_identical_to_existing_inspection(dispatched):
    authority, envelope = dispatched
    token = envelope.lease.token.decode()
    authority.clock.value += 100000
    expected = authority.store.inspect_lease_token(token)
    actual = authority.store.inspect_dispatch_authority(token)
    assert actual[:2] == expected
    assert actual[1] is LeaseState.DISPATCHED


@pytest.mark.parametrize("target", ["path", "key_path", "_guard_path"])
def test_guard_api_rejects_replaced_persistence_identity(dispatched, target):
    authority, envelope = dispatched
    store = authority.store
    path = getattr(store, target)
    moved = path.with_name(path.name + ".original")
    path.rename(moved)
    path.write_bytes(moved.read_bytes())
    path.chmod(0o600)
    try:
        with pytest.raises(AuthorityStoreError):
            store.inspect_dispatch_authority(envelope.lease.token.decode())
    finally:
        path.unlink()
        moved.rename(path)


def test_guard_api_rejects_closed_process_owned_store(dispatched):
    authority, envelope = dispatched
    authority.store.close()
    with pytest.raises(AuthorityStoreError):
        authority.store.inspect_dispatch_authority(envelope.lease.token.decode())


def test_finished_durable_lease_still_denies_execution(dispatched):
    from tobkiri_protocol.canonical import canonical_digest

    authority, envelope = dispatched
    store = authority.store
    lease = store.inspect_lease_token(envelope.lease.token.decode())[0]
    store.finish_lease(
        lease.lease_id,
        state=LeaseState.COMMITTED,
        outcome_digest=canonical_digest({"fixture": "done"}),
    )
    assert (
        store.inspect_dispatch_authority(envelope.lease.token.decode())[1] is LeaseState.COMMITTED
    )
    with pytest.raises(PermissionError):
        assert_dispatched_invocation(envelope, store)


@pytest.mark.parametrize("epoch", [None, "0"])
def test_missing_or_nonpositive_epoch_fails_closed(dispatched, epoch):
    authority, envelope = dispatched
    with authority.store._lock, authority.store._connection() as current:
        if epoch is None:
            current.execute("DELETE FROM authority_meta WHERE key='security_epoch'")
        else:
            current.execute(
                "UPDATE authority_meta SET value=? WHERE key='security_epoch'", (epoch,)
            )
    with pytest.raises(AuthorityStoreError, match="security epoch"):
        authority.store.inspect_dispatch_authority(envelope.lease.token.decode())


def test_guard_api_rejects_broad_database_permissions(dispatched):
    authority, envelope = dispatched
    path = authority.store.path
    old_mode = path.stat().st_mode & 0o777
    path.chmod(0o666)
    try:
        with pytest.raises(AuthorityStoreError):
            authority.store.inspect_dispatch_authority(envelope.lease.token.decode())
    finally:
        path.chmod(old_mode)


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_guard_api_rejects_unsafe_sidecar_permissions(dispatched, suffix):
    authority, envelope = dispatched
    path = authority.store.path.with_name(authority.store.path.name + suffix)
    assert not path.exists()
    path.write_bytes(b"unsafe fixture")
    path.chmod(0o666)
    try:
        with pytest.raises(AuthorityStoreError):
            authority.store.inspect_dispatch_authority(envelope.lease.token.decode())
    finally:
        path.unlink()


def test_guard_api_rejects_inherited_process_fence(dispatched, monkeypatch):
    authority, envelope = dispatched
    with monkeypatch.context() as changed:
        changed.setattr(authority.store, "_fork_fenced", True)
        with pytest.raises(AuthorityStoreError):
            authority.store.inspect_dispatch_authority(envelope.lease.token.decode())


def test_guard_api_preserves_bounded_lifecycle_lock_contention(dispatched, monkeypatch):
    import os

    fcntl = pytest.importorskip("fcntl")
    authority, envelope = dispatched
    monkeypatch.setattr(authority.store, "_guard_acquire_timeout_seconds", 0.1)
    descriptor = os.open(authority.store._guard_path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(AuthorityStoreError, match="lifecycle lock deadline"):
            authority.store.inspect_dispatch_authority(envelope.lease.token.decode())
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@pytest.mark.parametrize(
    "kind,attribute",
    [
        ("pack_artifact", "target"),
        ("publisher", "target_publisher_lineage"),
        ("host_extension", "host_extension_id"),
    ],
)
def test_additional_revocations_still_fence_through_durable_lease_state(
    dispatched, kind, attribute
):
    authority, envelope = dispatched
    store = authority.store
    lease = store.inspect_lease_token(envelope.lease.token.decode())[0]
    value = (
        lease.target.parent_artifact_digest
        if kind == "pack_artifact"
        else getattr(lease, attribute)
    )
    store.revoke(target_kind=kind, target_id=value, reason="fixture durable fence")
    assert store.inspect_dispatch_authority(envelope.lease.token.decode())[1] is LeaseState.REVOKED
    with pytest.raises(PermissionError):
        assert_dispatched_invocation(envelope, store)


def test_crypto_key_replacement_during_read_fails_before_return(dispatched, monkeypatch):
    authority, envelope = dispatched
    store = authority.store
    original = store._is_revoked
    path = store.key_path
    moved = path.with_name(path.name + ".original")
    replaced = False

    def revoked(current, kind, identity):
        nonlocal replaced
        if not replaced:
            path.rename(moved)
            path.write_bytes(moved.read_bytes())
            path.chmod(0o600)
            replaced = True
        return original(current, kind, identity)

    monkeypatch.setattr(store, "_is_revoked", revoked)
    try:
        with pytest.raises(AuthorityStoreError):
            store.inspect_dispatch_authority(envelope.lease.token.decode())
    finally:
        if replaced:
            path.unlink()
            moved.rename(path)
