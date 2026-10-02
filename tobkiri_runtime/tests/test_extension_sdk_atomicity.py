"""Real-store rollback, concurrency and read-only legacy SDK migration coverage."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from core_runtime.authority.v4 import (
    AuditUnavailable,
    AuthorityStore,
    AuthorityStoreError,
)
from tests.test_platform_host_extension_wake_v4 import (
    Authority,
    _successor_extension_registration,
    extension_registration,
)
from tobkiri_host.errors import AuthorizationError
from tobkiri_host.extension_sdk import HostExtensionSDK


def sdk(authority):
    return HostExtensionSDK(authority, sqlite3.connect(":memory:"), clock=lambda: 7.0)


@pytest.mark.parametrize("point", ["record", "ledger", "audit"])
def test_registration_failure_rolls_back_and_restart_retry(
    tmp_path, monkeypatch, point
):
    authority = Authority(tmp_path)
    store = authority.store
    original = store._insert_record
    count = 0

    def fail_record(connection, record):
        nonlocal count
        original(connection, record)
        count += 1
        if count == 2:
            raise RuntimeError("record fault")

    def fail(*args, **kwargs):
        raise RuntimeError("injected fault")

    target = {
        "record": "_insert_record",
        "ledger": "_insert_extension_state",
        "audit": "_extension_audit",
    }[point]
    with monkeypatch.context() as patch:
        patch.setattr(store, target, fail_record if point == "record" else fail)
        with pytest.raises(RuntimeError):
            sdk(authority).register(extension_registration())
    assert store.records == []
    assert store.host_extension_registration("registration.files.v1") is None
    assert store.audit_events() == []
    store.close()
    restarted = Authority(tmp_path)
    assert sdk(restarted).register(extension_registration())
    assert len(restarted.store.records) == 3


def test_authoritative_audit_failure_leaves_no_registration(tmp_path):
    authority = Authority(tmp_path)

    def fail():
        raise RuntimeError("audit unavailable")

    authority.store._audit_fault = fail
    with pytest.raises(AuditUnavailable):
        sdk(authority).register(extension_registration())
    assert authority.store.records == []
    assert authority.store.host_extension_registration("registration.files.v1") is None


def test_independent_sdk_duplicate_claim_is_atomic(tmp_path):
    first, second = Authority(tmp_path), Authority(tmp_path)
    barrier = Barrier(2)

    def register(authority):
        instance = sdk(authority)
        barrier.wait()
        try:
            instance.register(extension_registration())
            return "registered"
        except AuthorizationError:
            return "duplicate"

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(register, [first, second]))
    assert sorted(results) == ["duplicate", "registered"]
    assert len(first.store.records) == 3
    assert len(sdk(first).audit_events("registration.files.v1")) == 1


def test_old_custom_store_rejected_before_mutation():
    class OldStore:
        security_epoch = 1

        def put_records_atomically(self, records):
            raise AssertionError("must not mutate old store")

    with pytest.raises(AuthorizationError, match="atomic Host registration"):
        sdk(SimpleNamespace(store=OldStore()))


def legacy_source(authority, request=None):
    request = request or extension_registration()
    trust, domains, providers = sdk(authority)._compile(request)
    authority.store.put_records_atomically((trust, *domains, *providers))
    source = sqlite3.connect(":memory:")
    source.executescript("""
        CREATE TABLE host_extension_registration_state (
            registration_id TEXT PRIMARY KEY, trust_id TEXT, artifact_digest TEXT,
            provider_record_ids TEXT, active INTEGER);
        CREATE TABLE host_extension_registration_audit (
            sequence INTEGER PRIMARY KEY, registration_id TEXT, event_type TEXT,
            artifact_digest TEXT, provider_record_ids TEXT, security_epoch INTEGER,
            event_time REAL);
    """)
    ids = json.dumps([provider.record_id for provider in providers])
    source.execute(
        "INSERT INTO host_extension_registration_state VALUES (?,?,?,?,1)",
        (request.registration_id, request.trust_id, request.artifact.digest, ids),
    )
    source.execute(
        "INSERT INTO host_extension_registration_audit VALUES (17,?,?,?,?,1,3.25)",
        (request.registration_id, "registered", request.artifact.digest, ids),
    )
    source.commit()
    return source


def test_legacy_readonly_import_preserves_history_and_newer_revocation(tmp_path):
    authority = Authority(tmp_path)
    source = legacy_source(authority)
    source.execute("PRAGMA query_only=ON")
    instance = HostExtensionSDK(authority, source)
    event = instance.audit_events("registration.files.v1")[0]
    assert event["sequence"] == 17 and event["event_time"] == 3.25
    instance.revoke("registration.files.v1", reason="operator")
    before = authority.store.audit_events()
    HostExtensionSDK(authority, source)
    assert authority.store.audit_events() == before
    assert not authority.store.host_extension_registration("registration.files.v1")[
        "active"
    ]
    assert source.execute(
        "SELECT active FROM host_extension_registration_state"
    ).fetchone() == (1,)


def test_legacy_import_never_resurrects_revoked_authority(tmp_path):
    authority = Authority(tmp_path)
    source = legacy_source(authority)
    authority.revoke(
        target_kind="provider_authority",
        target_id="provider-authority.registration.files.v1.0",
        reason="revoked",
    )
    HostExtensionSDK(authority, source)
    assert not authority.store.host_extension_registration("registration.files.v1")[
        "active"
    ]


def test_legacy_mismatch_is_rejected_without_partial_import(tmp_path):
    authority = Authority(tmp_path)
    source = legacy_source(authority)
    source.execute("UPDATE host_extension_registration_state SET trust_id='unknown'")
    with pytest.raises(AuthorityStoreError, match="does not match"):
        HostExtensionSDK(authority, source)
    assert authority.store.host_extension_registration("registration.files.v1") is None


def test_failed_successor_keeps_old_revocation(tmp_path, monkeypatch):
    authority = Authority(tmp_path)
    instance = sdk(authority)
    instance.register(extension_registration())

    def fail(*args):
        raise RuntimeError("successor ledger fault")

    monkeypatch.setattr(authority.store, "_insert_extension_state", fail)
    with pytest.raises(RuntimeError):
        instance.update(
            "registration.files.v1",
            _successor_extension_registration(),
            reason="update",
        )
    assert not authority.store.host_extension_registration("registration.files.v1")[
        "active"
    ]
    assert authority.store.host_extension_registration("registration.files.v2") is None
    assert len(authority.store.records) == 3
    assert (
        instance.audit_events("registration.files.v2")[-1]["event_type"]
        == "update_failed_closed"
    )


@pytest.mark.parametrize("version", ["2", "3"])
def test_historical_schema_upgrades_to_registration_ledger(tmp_path, version):
    store = AuthorityStore(tmp_path / "authority.sqlite")
    store.close()
    with sqlite3.connect(tmp_path / "authority.sqlite") as connection:
        connection.execute("DROP TABLE host_extension_registrations")
        connection.execute(
            "UPDATE authority_meta SET value=? WHERE key='schema_version'", (version,)
        )
    upgraded = AuthorityStore(tmp_path / "authority.sqlite")
    with sqlite3.connect(upgraded.path) as connection:
        assert connection.execute(
            "SELECT value FROM authority_meta WHERE key='schema_version'"
        ).fetchone() == ("4",)


def test_current_schema_missing_ledger_fails_closed(tmp_path):
    store = AuthorityStore(tmp_path / "authority.sqlite")
    store.close()
    with sqlite3.connect(tmp_path / "authority.sqlite") as connection:
        connection.execute("DROP TABLE host_extension_registrations")
    with pytest.raises(AuthorityStoreError, match="registration schema"):
        AuthorityStore(tmp_path / "authority.sqlite")


@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
def test_process_exit_preserves_atomic_registration(tmp_path, phase):
    """OS process exit cannot expose only a subset of registration state."""
    import os
    import subprocess
    import sys

    program = """
import os
from pathlib import Path
from tests.test_extension_sdk_atomicity import sdk
from tests.test_platform_host_extension_wake_v4 import Authority, extension_registration
root = Path(os.environ["SDK_CRASH_ROOT"])
authority = Authority(root)
if os.environ["SDK_CRASH_PHASE"] == "before_commit":
    original = authority.store._insert_extension_state
    def crash(connection, state):
        original(connection, state)
        os._exit(23)
    authority.store._insert_extension_state = crash
sdk(authority).register(extension_registration())
os._exit(23)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", program],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "SDK_CRASH_ROOT": str(tmp_path), "SDK_CRASH_PHASE": phase},
        check=False,
    )
    assert result.returncode == 23
    authority = Authority(tmp_path)
    state = authority.store.host_extension_registration("registration.files.v1")
    if phase == "before_commit":
        assert state is None and authority.store.records == []
        assert sdk(authority).register(extension_registration())
    else:
        assert state["active"] and len(authority.store.records) == 3
        assert len(sdk(authority).audit_events("registration.files.v1")) == 1
        with pytest.raises(AuthorizationError, match="already exists"):
            sdk(authority).register(extension_registration())


def test_real_kernel_revoke_keeps_domain_termination(tmp_path):
    from tests.test_authority_v4_lifecycle import _Harness

    terminated = []
    harness = _Harness(tmp_path, terminated=terminated)
    instance = sdk(harness.kernel)
    request = extension_registration()
    instance.register(request)
    instance.revoke(request.registration_id, reason="operator")
    assert request.providers[0].execution_domain.domain_id in terminated


def test_real_kernel_revoke_audit_failure_emergency_stops(tmp_path):
    from tests.test_authority_v4_lifecycle import _Harness

    terminated = []
    harness = _Harness(tmp_path, terminated=terminated)
    instance = sdk(harness.kernel)
    request = extension_registration()
    instance.register(request)

    def fail():
        raise RuntimeError("audit unavailable")

    harness.store._audit_fault = fail
    with pytest.raises(AuditUnavailable):
        instance.revoke(request.registration_id, reason="operator")
    assert harness.kernel._emergency_stop
    assert request.providers[0].execution_domain.domain_id in terminated
    assert harness.store.host_extension_registration(request.registration_id)["active"]
    harness.store._audit_fault = None
    instance.revoke(request.registration_id, reason="retry")
    assert not harness.store.host_extension_registration(request.registration_id)[
        "active"
    ]


def test_registration_epoch_is_checked_inside_transaction(tmp_path, monkeypatch):
    authority = Authority(tmp_path)
    instance = sdk(authority)
    commit = authority.store.commit_host_extension_registration

    def advance_then_commit(records, state, event):
        authority.store.advance_security_epoch("race")
        return commit(records, state, event)

    monkeypatch.setattr(
        authority.store, "commit_host_extension_registration", advance_then_commit
    )
    with pytest.raises(AuthorizationError, match="stale epoch"):
        instance.register(extension_registration())
    assert authority.store.records == []
    assert authority.store.host_extension_registration("registration.files.v1") is None


def test_legacy_audit_failure_rolls_back_migration_and_can_retry(tmp_path):
    authority = Authority(tmp_path)
    source = legacy_source(authority)
    before = authority.store.audit_events()

    def fail():
        raise RuntimeError("audit unavailable")

    authority.store._audit_fault = fail
    with pytest.raises(AuditUnavailable):
        HostExtensionSDK(authority, source)
    assert authority.store.host_extension_registration("registration.files.v1") is None
    assert authority.store.audit_events() == before
    authority.store._audit_fault = None
    assert HostExtensionSDK(authority, source).audit_events("registration.files.v1")


@pytest.mark.parametrize("interrupted_after", ["first_provider", "all_revocations"])
def test_migrated_interrupted_revoke_finishes_all_kernel_cleanup(
    tmp_path, interrupted_after
):
    from dataclasses import replace

    from tests.test_authority_v4_lifecycle import _Harness

    terminated = []
    harness = _Harness(tmp_path, terminated=terminated)
    original = extension_registration()
    first = original.providers[0]
    function = replace(
        original.artifact.functions[0], function_id="extension.files.second"
    )
    second = replace(
        first,
        provider_id="extension.files.second",
        function_id=function.function_id,
        execution_domain=replace(
            first.execution_domain,
            domain_id="domain.extension.files.second",
            principals=(
                replace(
                    first.execution_domain.principals[0],
                    function_id=function.function_id,
                ),
            ),
        ),
    )
    request = replace(
        original,
        artifact=replace(
            original.artifact, functions=(*original.artifact.functions, function)
        ),
        providers=(first, second),
    )
    source = legacy_source(harness.kernel, request)
    ids = tuple(
        f"provider-authority.{request.registration_id}.{index}" for index in range(2)
    )
    # Simulate process exit after durable revocation, before Kernel domain-stop.
    harness.store.revoke(
        target_kind="provider_authority", target_id=ids[0], reason="interrupted"
    )
    if interrupted_after == "all_revocations":
        harness.store.revoke(
            target_kind="provider_authority", target_id=ids[1], reason="interrupted"
        )
        harness.store.revoke(
            target_kind="host_extension",
            target_id=request.trust_id,
            reason="interrupted",
        )
    instance = HostExtensionSDK(harness.kernel, source)
    state = harness.store.host_extension_registration(request.registration_id)
    assert not state["active"] and state["revocation_pending"]
    instance.revoke(request.registration_id, reason="complete interrupted revoke")
    assert {
        provider.execution_domain.domain_id for provider in request.providers
    }.issubset(terminated)
    state = harness.store.host_extension_registration(request.registration_id)
    assert not state["active"] and not state.get("revocation_pending", False)
    assert set(ids).issubset(
        harness.store.revoked_ids_for_kind("provider_authority", ids)
    )
    assert request.trust_id in harness.store.revoked_ids_for_kind(
        "host_extension", (request.trust_id,)
    )
    with pytest.raises(AuthorizationError, match="not active"):
        instance.revoke(request.registration_id, reason="already completed")
