"""Fresh credential guards retain every fence without per-target connections."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from core_runtime.authority.v4_models import LeaseState
from core_runtime.credential_transport import HostBoundCredentialTransport
from tests.test_authority_v4_lifecycle import _Harness, _digest
from tests.test_credential_broker_pack import _dispatched_envelope


@pytest.fixture
def guard(
    tmp_path: Path,
) -> Iterator[tuple[_Harness, HostBoundCredentialTransport]]:
    authority, envelope = _dispatched_envelope(tmp_path)
    owner = HostBoundCredentialTransport.from_authorized_envelope(
        envelope,
        provider_principal=authority.target,
        store=SimpleNamespace(),
        authority_store=authority.store,
        credential_handle="opaque:offline-fixture",
        credential_key_version="fixture-v1",
        provider_instance_id="provider.fixture",
        credential_scope="generate",
        credential_purpose="provider.invoke",
        endpoint_origin="https://fixture.invalid",
        current_security_epoch=lambda: authority.store.security_epoch,
        consumer_pack_id="provider-adapter-pack",
    )
    try:
        yield authority, owner
    finally:
        authority.store.close()


def test_production_guard_uses_two_fresh_connections_and_keeps_callbacks(
    guard: tuple[_Harness, HostBoundCredentialTransport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, owner = guard
    connections, parents, epochs = [], [], []
    connect = authority.store._connect

    def counted_connect() -> Any:
        connections.append(True)
        return connect()

    def epoch() -> int:
        epochs.append(True)
        return authority.store.security_epoch

    def unexpected_legacy(*args: Any) -> Any:
        pytest.fail("production guard used the legacy per-target inspection path")

    owner._parent_guard = lambda: parents.append(True)
    owner._current_security_epoch = epoch
    monkeypatch.setattr(authority.store, "_connect", counted_connect)
    monkeypatch.setattr(authority.store, "inspect_lease_token", unexpected_legacy)
    monkeypatch.setattr(authority.store, "is_revoked", unexpected_legacy)
    for _ in range(3):
        assert owner._authority_still_active()
    assert len(connections) == 6
    assert len(parents) == len(epochs) == 3


@pytest.mark.parametrize("target", range(9))
def test_warm_guard_observes_each_revocation_without_lease_state_changes(
    guard: tuple[_Harness, HostBoundCredentialTransport],
    target: int,
) -> None:
    authority, owner = guard
    durable, state = authority.store.inspect_lease_token(owner._invocation_token)
    assert state is LeaseState.DISPATCHED and owner._authority_still_active()
    targets = (
        ("function_principal", durable.caller.principal_id),
        ("function_principal", durable.target.principal_id),
        ("execution_domain", durable.caller_domain_id),
        ("execution_domain", durable.target_domain_id),
        ("profile", durable.profile_id),
        ("activation", durable.activation_id),
        ("grant", durable.grant_id),
        ("provider_authority", durable.provider_authority_id),
        ("global", "fixture-global"),
    )
    kind, identity = targets[target]
    with sqlite3.connect(authority.store.path) as connection:
        connection.execute(
            "INSERT INTO revocations VALUES (?, ?, ?, ?, ?, ?)",
            ("fixture-revocation", kind, identity, 1, _digest("reason"), 1000),
        )
    # Keep the lease DISPATCHED so the revocation query itself must reject it.
    assert authority.store.inspect_lease_token(owner._invocation_token)[1] is state
    assert not owner._authority_still_active()


@pytest.mark.parametrize(
    "field",
    [
        "caller_principal_id",
        "provider_principal_id",
        "provider_function_id",
        "operation_id",
        "profile_id",
        "activation_id",
        "target_domain_id",
        "target_boot_epoch",
        "request_id",
        "request_digest",
    ],
)
def test_fused_guard_retains_all_binding_identity_comparisons(
    guard: tuple[_Harness, HostBoundCredentialTransport],
    field: str,
) -> None:
    _, owner = guard
    assert owner._authority_still_active()
    owner._binding = replace(
        owner._binding,
        **{field: 2 if field == "target_boot_epoch" else "mismatch"},
    )
    assert not owner._authority_still_active()


@pytest.mark.parametrize("ending", ["parent", "callback_epoch", "cancel", "deadline"])
def test_fused_guard_keeps_parent_callback_cancellation_and_deadline(
    guard: tuple[_Harness, HostBoundCredentialTransport],
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    authority, owner = guard

    def unexpected_inspect(*args: Any) -> Any:
        pytest.fail("a prior fence should reject before inspecting the lease")

    def rejected_parent() -> None:
        raise PermissionError("offline parent was revoked")

    monkeypatch.setattr(
        authority.store,
        "inspect_dispatch_authority",
        unexpected_inspect,
    )
    if ending == "parent":
        owner._parent_guard = rejected_parent
    elif ending == "callback_epoch":
        owner._current_security_epoch = lambda: 2
    elif ending == "cancel":
        owner._envelope.cancellation_requested.set()
    else:
        owner._monotonic_clock = lambda: owner._envelope.deadline_monotonic
    assert not owner._authority_still_active()


def test_fused_database_epoch_is_checked_even_if_callback_is_stale(
    guard: tuple[_Harness, HostBoundCredentialTransport],
) -> None:
    authority, owner = guard
    owner._current_security_epoch = lambda: 1
    assert owner._authority_still_active()
    with sqlite3.connect(authority.store.path) as connection:
        connection.execute(
            "UPDATE authority_meta SET value='2' WHERE key='security_epoch'"
        )
    assert not owner._authority_still_active()


@pytest.mark.parametrize("change", ["state", "ciphertext", "digest", "token"])
def test_fused_guard_reauthenticates_each_current_lease(
    guard: tuple[_Harness, HostBoundCredentialTransport],
    change: str,
) -> None:
    authority, owner = guard
    assert owner._authority_still_active()
    if change == "token":
        owner._invocation_token = "invalid-fixture-token"
    else:
        field, value = {
            "state": ("state", LeaseState.FAILED.value),
            "ciphertext": ("encrypted_payload", b"unauthenticated-fixture"),
            "digest": ("lease_digest", _digest("tampered")),
        }[change]
        with sqlite3.connect(authority.store.path) as connection:
            connection.execute(f"UPDATE invocation_leases SET {field}=?", (value,))
    assert not owner._authority_still_active()


def test_fused_inspection_failure_never_falls_back(
    guard: tuple[_Harness, HostBoundCredentialTransport],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, owner = guard
    legacy = []

    def unavailable(token: str) -> Any:
        raise RuntimeError("offline fresh inspection unavailable")

    monkeypatch.setattr(authority.store, "inspect_dispatch_authority", unavailable)
    monkeypatch.setattr(
        authority.store,
        "inspect_lease_token",
        lambda token: legacy.append(token),
    )
    assert not owner._authority_still_active()
    assert not legacy


def test_legacy_authority_keeps_fresh_per_target_checks(
    guard: tuple[_Harness, HostBoundCredentialTransport],
) -> None:
    authority, owner = guard
    targets = []
    revoked = [False]

    def is_revoked(kind: str, identity: str) -> bool:
        targets.append((kind, identity))
        return revoked[0]

    owner._authority_store = SimpleNamespace(
        inspect_lease_token=authority.store.inspect_lease_token,
        is_revoked=is_revoked,
    )
    assert owner._authority_still_active()
    assert len(targets) == 8
    revoked[0] = True
    assert not owner._authority_still_active()
