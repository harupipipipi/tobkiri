"""Historical SQLite schema-v2 migration and fail-closed recovery tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

import pytest

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityKernel,
    AuthorityScope,
    AuthorityStore,
    AuthorityStoreError,
    AuditUnavailable,
    LeaseState,
    authority_digest,
)

from tests.test_authority_v4_lifecycle import _Harness, _Resolver, _digest


_V1_LEASE_SCHEMA = """
CREATE TABLE invocation_leases (
    lease_id TEXT PRIMARY KEY,
    lease_digest TEXT NOT NULL,
    encrypted_payload BLOB NOT NULL,
    caller_principal_id TEXT NOT NULL,
    target_principal_id TEXT NOT NULL,
    caller_artifact_digest TEXT NOT NULL,
    target_artifact_digest TEXT NOT NULL,
    caller_publisher_lineage TEXT NOT NULL,
    target_publisher_lineage TEXT NOT NULL,
    host_extension_id TEXT NOT NULL,
    caller_domain_id TEXT NOT NULL,
    target_domain_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    activation_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    provider_authority_id TEXT NOT NULL,
    audit_reservation_id TEXT NOT NULL,
    security_epoch INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    state TEXT NOT NULL,
    outcome_digest TEXT
) STRICT
"""

_V1_COLUMNS = (
    "lease_id",
    "lease_digest",
    "encrypted_payload",
    "caller_principal_id",
    "target_principal_id",
    "caller_artifact_digest",
    "target_artifact_digest",
    "caller_publisher_lineage",
    "target_publisher_lineage",
    "host_extension_id",
    "caller_domain_id",
    "target_domain_id",
    "profile_id",
    "activation_id",
    "grant_id",
    "provider_authority_id",
    "audit_reservation_id",
    "security_epoch",
    "expires_at",
    "state",
    "outcome_digest",
)


@dataclass(frozen=True)
class HistoricalAuthorityFixture:
    """A real encrypted v1 database plus live pre-migration lease tokens."""

    path: Path
    key_path: Path
    harness: _Harness
    issued_token: str
    dispatched_token: str
    issued_lease_id: str
    dispatched_lease_id: str
    audit_before: tuple[dict[str, object], ...]
    authority_digests_before: tuple[tuple[str, str, str], ...]


@pytest.fixture
def historical_authority_v1(tmp_path: Path) -> HistoricalAuthorityFixture:
    """Build a realistic encrypted schema-v1 database from actual records."""

    harness = _Harness(tmp_path)
    dispatched = harness.kernel.authorize(
        harness.context(request_id="request-dispatched"),
        harness.scope,
    )
    harness.kernel.dispatch(
        dispatched.lease_token,
        target_domain_id=harness.target_domain.domain_id,
        target_boot_epoch=harness.target_domain.boot_epoch,
        request_digest=_digest("5"),
    )
    issued = harness.kernel.authorize(
        harness.context(request_id="request-issued"),
        harness.scope,
    )
    audit_before = tuple(harness.store.audit_events())
    with sqlite3.connect(harness.store.path) as connection:
        authority_digests_before = tuple(
            connection.execute(
                "SELECT record_type, record_id, record_digest"
                " FROM authority_records ORDER BY record_type, record_id"
            ).fetchall()
        )
    _downgrade_to_v1(harness.store)
    return HistoricalAuthorityFixture(
        path=harness.store.path,
        key_path=harness.store.key_path,
        harness=harness,
        issued_token=issued.lease_token,
        dispatched_token=dispatched.lease_token,
        issued_lease_id=issued.lease_id,
        dispatched_lease_id=dispatched.lease_id,
        audit_before=audit_before,
        authority_digests_before=authority_digests_before,
    )


def _downgrade_to_v1(store: AuthorityStore) -> None:
    """Rewrite only the lease table/payload into the committed historical shape."""

    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT lease_id, encrypted_payload FROM invocation_leases"
        ).fetchall()
        for row in rows:
            payload = store._decrypt(row["encrypted_payload"])
            payload.pop("request_id")
            payload.pop("activation_digest")
            payload.pop("plan_digest")
            connection.execute(
                "UPDATE invocation_leases SET lease_digest=?, encrypted_payload=? WHERE lease_id=?",
                (
                    authority_digest(payload),
                    store._encrypt(payload),
                    row["lease_id"],
                ),
            )
        connection.execute("DROP INDEX leases_request")
        connection.execute("ALTER TABLE invocation_leases RENAME TO leases_v2")
        connection.execute(_V1_LEASE_SCHEMA)
        columns = ", ".join(_V1_COLUMNS)
        connection.execute(
            f"INSERT INTO invocation_leases ({columns}) SELECT {columns} FROM leases_v2"
        )
        connection.execute("DROP TABLE leases_v2")
        connection.execute("UPDATE authority_meta SET value='1' WHERE key='schema_version'")


def _schema_version(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT value FROM authority_meta WHERE key='schema_version'"
        ).fetchone()
    assert row is not None
    return str(row[0])


def _lease_columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(invocation_leases)").fetchall()
        }


def test_historical_v1_migrates_without_silent_authority_expansion(
    historical_authority_v1: HistoricalAuthorityFixture,
) -> None:
    fixture = historical_authority_v1
    migrated = AuthorityStore(
        fixture.path,
        key_path=fixture.key_path,
        clock=fixture.harness.clock,
    )

    assert _schema_version(fixture.path) == "3"
    assert "request_id" in _lease_columns(fixture.path)
    assert migrated.security_epoch == 1
    assert migrated.grant_usage(fixture.harness.grant.grant_id) == (0, 1)
    issued = migrated.get_lease(fixture.issued_lease_id)
    dispatched = migrated.get_lease(fixture.dispatched_lease_id)
    assert issued is not None and issued[1] is LeaseState.REVOKED
    assert dispatched is not None and dispatched[1] is LeaseState.AMBIGUOUS
    assert issued[0].request_id == f"legacy-{fixture.issued_lease_id}"
    assert dispatched[0].request_id == f"legacy-{fixture.dispatched_lease_id}"
    with sqlite3.connect(fixture.path) as connection:
        states = dict(
            connection.execute("SELECT lease_id, state FROM invocation_leases").fetchall()
        )
        authority_digests_after = tuple(
            connection.execute(
                "SELECT record_type, record_id, record_digest"
                " FROM authority_records ORDER BY record_type, record_id"
            ).fetchall()
        )
    assert states == {
        fixture.issued_lease_id: LeaseState.REVOKED.value,
        fixture.dispatched_lease_id: LeaseState.AMBIGUOUS.value,
    }
    assert authority_digests_after == fixture.authority_digests_before

    events = migrated.audit_events()
    before_digests = [str(item["event_digest"]) for item in fixture.audit_before]
    assert [item["event_digest"] for item in events[: len(before_digests)]] == before_digests
    assert {item["event_state"] for item in events[-2:]} == {
        LeaseState.REVOKED.value,
        LeaseState.AMBIGUOUS.value,
    }

    kernel = AuthorityKernel(migrated, _Resolver(fixture.harness.scope))
    omitted_quota = fixture.harness.scope.to_dict()
    omitted_quota["quotas"] = {}
    with pytest.raises(AuthorityDenied):
        kernel.authorize(
            fixture.harness.context(request_id="request-unbounded-after-migration"),
            AuthorityScope.from_dict(omitted_quota),
        )
    for token in (fixture.issued_token, fixture.dispatched_token):
        with pytest.raises(AuthorityDenied):
            kernel.dispatch(
                token,
                target_domain_id=fixture.harness.target_domain.domain_id,
                target_boot_epoch=fixture.harness.target_domain.boot_epoch,
                request_digest=_digest("5"),
            )


def test_migrated_database_restart_recovery_preserves_chain(
    historical_authority_v1: HistoricalAuthorityFixture,
) -> None:
    fixture = historical_authority_v1
    migrated = AuthorityStore(
        fixture.path,
        key_path=fixture.key_path,
        clock=fixture.harness.clock,
    )
    kernel = AuthorityKernel(
        migrated,
        _Resolver(fixture.harness.scope),
        clock=fixture.harness.clock,
    )
    result = kernel.authorize(
        fixture.harness.context(
            request_id="request-after-migration",
            request_digest=_digest("after-migration"),
        ),
        fixture.harness.scope,
    )
    kernel.dispatch(
        result.lease_token,
        target_domain_id=fixture.harness.target_domain.domain_id,
        target_boot_epoch=fixture.harness.target_domain.boot_epoch,
        request_digest=_digest("after-migration"),
    )
    chain_before_restart = migrated.audit_events()

    restarted_store = AuthorityStore(
        fixture.path,
        key_path=fixture.key_path,
        clock=fixture.harness.clock,
    )
    restarted_kernel = AuthorityKernel(
        restarted_store,
        _Resolver(fixture.harness.scope),
        clock=fixture.harness.clock,
    )
    assert restarted_kernel.recover() == [result.lease_id]
    chain_after_restart = restarted_store.audit_events()
    assert [item["event_digest"] for item in chain_after_restart[:-1]] == [
        item["event_digest"] for item in chain_before_restart
    ]
    assert chain_after_restart[-1]["event_state"] == LeaseState.AMBIGUOUS.value
    assert restarted_kernel.recover() == []


def test_corrupted_historical_audit_fails_before_schema_write(
    historical_authority_v1: HistoricalAuthorityFixture,
) -> None:
    fixture = historical_authority_v1
    with sqlite3.connect(fixture.path) as connection:
        connection.execute(
            "UPDATE authority_audit SET event_digest=? WHERE sequence=1",
            (_digest("tampered"),),
        )

    with pytest.raises(AuthorityStoreError, match="audit chain"):
        AuthorityStore(fixture.path, key_path=fixture.key_path)

    assert _schema_version(fixture.path) == "1"
    assert "request_id" not in _lease_columns(fixture.path)


def test_corrupted_historical_lease_fails_and_rolls_back_migration(
    historical_authority_v1: HistoricalAuthorityFixture,
) -> None:
    fixture = historical_authority_v1
    with sqlite3.connect(fixture.path) as connection:
        connection.execute(
            "UPDATE invocation_leases SET encrypted_payload=? WHERE lease_id=?",
            (b"not-authenticated-ciphertext", fixture.issued_lease_id),
        )

    with pytest.raises(AuthorityStoreError, match="authentication"):
        AuthorityStore(
            fixture.path,
            key_path=fixture.key_path,
            clock=fixture.harness.clock,
        )

    assert _schema_version(fixture.path) == "1"
    assert "request_id" not in _lease_columns(fixture.path)


def test_historical_lease_digest_mismatch_fails_before_normalization(
    historical_authority_v1: HistoricalAuthorityFixture,
) -> None:
    fixture = historical_authority_v1
    with sqlite3.connect(fixture.path) as connection:
        connection.execute(
            "UPDATE invocation_leases SET lease_digest=? WHERE lease_id=?",
            (_digest("tampered-lease-digest"), fixture.issued_lease_id),
        )

    with pytest.raises(AuthorityStoreError, match="digest is inconsistent"):
        AuthorityStore(
            fixture.path,
            key_path=fixture.key_path,
            clock=fixture.harness.clock,
        )

    assert _schema_version(fixture.path) == "1"
    assert "request_id" not in _lease_columns(fixture.path)


def test_migration_audit_failure_rolls_back_schema_and_authority_state(
    historical_authority_v1: HistoricalAuthorityFixture,
) -> None:
    fixture = historical_authority_v1

    def fail_audit() -> None:
        raise OSError("audit volume unavailable")

    with pytest.raises(AuditUnavailable):
        AuthorityStore(
            fixture.path,
            key_path=fixture.key_path,
            clock=fixture.harness.clock,
            audit_fault=fail_audit,
        )

    assert _schema_version(fixture.path) == "1"
    assert "request_id" not in _lease_columns(fixture.path)
    with sqlite3.connect(fixture.path) as connection:
        states = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT lease_id, state FROM invocation_leases"
            ).fetchall()
        }
    assert states == {
        fixture.issued_lease_id: LeaseState.ISSUED.value,
        fixture.dispatched_lease_id: LeaseState.DISPATCHED.value,
    }

    recovered = AuthorityStore(
        fixture.path,
        key_path=fixture.key_path,
        clock=fixture.harness.clock,
    )
    assert recovered.audit_events()[-1]["event_state"] in {
        LeaseState.REVOKED.value,
        LeaseState.AMBIGUOUS.value,
    }


def test_partial_v1_schema_fails_closed_without_normalization(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    with sqlite3.connect(harness.store.path) as connection:
        connection.execute("UPDATE authority_meta SET value='1' WHERE key='schema_version'")

    with pytest.raises(AuthorityStoreError, match="partial or inconsistent"):
        AuthorityStore(harness.store.path, key_path=harness.store.key_path)

    assert _schema_version(harness.store.path) == "1"
    assert "request_id" in _lease_columns(harness.store.path)


def test_missing_historical_table_is_not_silently_recreated(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    with sqlite3.connect(harness.store.path) as connection:
        connection.execute("DROP TABLE authority_audit")

    with pytest.raises(AuthorityStoreError, match="table set"):
        AuthorityStore(harness.store.path, key_path=harness.store.key_path)

    with sqlite3.connect(harness.store.path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "authority_audit" not in tables


def test_unknown_schema_version_fails_closed_without_downgrade(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    with sqlite3.connect(harness.store.path) as connection:
        connection.execute("UPDATE authority_meta SET value='99' WHERE key='schema_version'")

    with pytest.raises(AuthorityStoreError, match="unsupported"):
        AuthorityStore(harness.store.path, key_path=harness.store.key_path)

    assert _schema_version(harness.store.path) == "99"
