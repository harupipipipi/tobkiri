"""Regression proofs for bounded control locking and ceremony retention."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import time
from typing import Mapping
from urllib.parse import quote
import uuid

import pytest

from core_runtime.control_reconciliation_v4 import (
    ControlReconciliationCapacityError,
    ControlReconciliationStore,
    ControlReconciliationUnavailableError,
)
from core_runtime.frontend_contract_routes import (
    FrontendContractBinding,
    FrontendContractTarget,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import PanelAuthManager
from tobkiri_protocol.canonical import canonical_digest


def _hold_posix_lock(path_value: str, ready: object, release: object) -> None:
    import fcntl

    descriptor = os.open(path_value, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()  # type: ignore[attr-defined]
        release.wait(10.0)  # type: ignore[attr-defined]
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _begin(store: ControlReconciliationStore, index: int) -> str:
    request_id = f"00000000-0000-4000-8000-{index:012d}"
    store.begin_operation(
        request_id=request_id,
        session_id="session-a",
        operation_id="profile.change.activate",
        contract_id="tobkiri.host.control-presentation.v4",
        request_digest=canonical_digest({"request": index}),
        session_expires_at=10_000.0,
    )
    return request_id


class _Dispatch:
    profile_id = "defaults"
    plan_digest = "sha256:" + "a" * 64

    def __init__(self) -> None:
        self.calls = 0

    def assert_current(self) -> None:
        pass

    def assert_operation_ready(self, contract_id: str, operation_id: str) -> None:
        pass

    def provider_metadata(self, contract_id: str) -> tuple[Mapping[str, object], ...]:
        return (
            {
                "provider_id": "test.provider",
                "operation_id": "test.write",
                "profile_id": self.profile_id,
                "plan_digest": self.plan_digest,
            },
        )

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, object],
        *,
        version_range: str | None = None,
    ) -> Mapping[str, object]:
        self.calls += 1
        return {"state": "succeeded", "value": payload["value"]}


def _binding() -> FrontendContractBinding:
    return FrontendContractBinding(
        method="POST",
        path="/api/test/write",
        presentation="identity",
        targets=(
            FrontendContractTarget(
                contribution_id="test.write",
                contract_id="test.contract.v1",
                operation_id="test.write",
                provider_id="test.provider",
                function_id="test.provider",
                allowed_payload_keys=frozenset({"value"}),
            ),
        ),
    )


def _request(
    server: PackAPIServer,
    method: str,
    path: str,
    *,
    body: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object], list[tuple[str, str]]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = dict(headers or {})
    if encoded is not None:
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    response_headers = response.getheaders()
    connection.close()
    return response.status, payload, response_headers


def _authenticate(server: PackAPIServer) -> tuple[str, str]:
    origin = f"http://127.0.0.1:{server.port}"
    status, bootstrap, _ = _request(
        server,
        "POST",
        "/api/panel/auth/bootstrap",
        body={},
        headers={"X-Rumi-Desktop-Bootstrap": "lock-test"},
    )
    assert status == 200
    status, exchange, response_headers = _request(
        server,
        "POST",
        "/api/panel/auth/exchange",
        body={"code": bootstrap["data"]["code"]},  # type: ignore[index]
        headers={"Origin": origin},
    )
    assert status == 200
    cookie = next(value for key, value in response_headers if key.lower() == "set-cookie").split(
        ";", 1
    )[0]
    return cookie, str(exchange["data"]["csrf_token"])  # type: ignore[index]


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX flock")
def test_posix_lock_deadline_bounds_32_waiters_and_recovers_cleanly(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control" / "reconciliation.sqlite3"
    owner = ControlReconciliationStore(path)
    owner.prepare_for_operation()
    owner.close()
    lock_path = Path(f"{path}.lock")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_posix_lock,
        args=(str(lock_path), ready, release),
    )
    process.start()
    assert ready.wait(10.0)

    started = time.monotonic()

    def contend(index: int) -> str:
        store = ControlReconciliationStore(
            path,
            instance_id=f"waiter-{index}",
            open_retry_seconds=0.1,
        )
        try:
            store.prepare_for_operation()
        except ControlReconciliationUnavailableError as error:
            return str(error)
        finally:
            store.close()
        return "unexpected-success"

    with ThreadPoolExecutor(max_workers=32) as executor:
        outcomes = list(executor.map(contend, range(32)))
    elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert all("deadline" in outcome for outcome in outcomes)

    release.set()
    process.join(10.0)
    assert process.exitcode == 0
    restarted = ControlReconciliationStore(
        path,
        heartbeat_interval_seconds=0.05,
    )
    request_id = _begin(restarted, 1)
    heartbeat = restarted._heartbeat_thread  # noqa: SLF001
    restarted.finish_operation(
        request_id,
        session_id="session-a",
        state="succeeded",
        result={"state": "succeeded"},
    )
    restarted.close()
    assert heartbeat is not None and not heartbeat.is_alive()
    restarted.prepare_for_operation()
    restarted.close()


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX flock")
def test_32_pack_api_requests_return_503_and_server_restarts_after_lock_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    auth = PanelAuthManager(bootstrap_secret="lock-test")
    dispatch = _Dispatch()
    server = PackAPIServer(
        port=0,
        panel_auth_manager=auth,
        dispatch_session=dispatch,
        contract_bindings=(_binding(),),
    )
    server._operation_journal._open_retry_seconds = 0.1  # noqa: SLF001
    server._operation_journal.prepare_for_operation()
    server.start()
    cookie, csrf = _authenticate(server)
    lock_path = Path(f"{server._operation_journal.path}.lock")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_posix_lock,
        args=(str(lock_path), ready, release),
    )
    process.start()
    assert ready.wait(10.0)
    route = "/api/contracts/defaultspack/" + quote("POST /api/test/write", safe="")

    def post(_index: int) -> int:
        origin = f"http://127.0.0.1:{server.port}"
        status, payload, _ = _request(
            server,
            "POST",
            route,
            body={"value": "blocked"},
            headers={
                "Cookie": cookie,
                "Origin": origin,
                "X-Rumi-CSRF": csrf,
                "X-Tobkiri-Request-ID": str(uuid.uuid4()),
            },
        )
        assert payload["data"]["code"] == "operation_reconciliation_unavailable"  # type: ignore[index]
        return status

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=32) as executor:
        statuses = list(executor.map(post, range(32)))
    assert time.monotonic() - started < 3.0
    assert statuses == [503] * 32
    assert dispatch.calls == 0

    server.stop()
    server.start()
    blocked_status = post(100)
    assert blocked_status == 503
    release.set()
    process.join(10.0)
    assert process.exitcode == 0

    origin = f"http://127.0.0.1:{server.port}"
    status, payload, _ = _request(
        server,
        "POST",
        route,
        body={"value": "after-release"},
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert status == 200
    assert payload["data"] == {"state": "succeeded", "value": "after-release"}
    heartbeat = server._operation_journal._heartbeat_thread  # noqa: SLF001
    server.stop()
    assert heartbeat is None or not heartbeat.is_alive()


def _seed_ceremonies(
    path: Path,
    *,
    expired_count: int,
    payload_bytes: int,
) -> None:
    states = ("reviewed", "approval_prepared", "approved", "activated")
    rows: list[tuple[object, ...]] = []
    for index in range(expired_count):
        rows.append(
            (
                f"expired-{index}",
                f"digest-expired-{index}",
                "session",
                "resolved",
                "revision",
                "plan",
                "definition",
                "catalog",
                "bundle",
                "authority",
                1,
                "x" * payload_bytes,
                1.0,
                1.0,
            )
        )
    for index, state in enumerate(states):
        rows.append(
            (
                f"expired-{state}",
                f"digest-expired-{state}",
                "session",
                state,
                "revision",
                "plan",
                "definition",
                "catalog",
                "bundle",
                "authority",
                1,
                "retained",
                1.0,
                1.0 + index,
            )
        )
    for index, state in enumerate(("resolved", *states)):
        rows.append(
            (
                f"active-{state}",
                f"digest-active-{state}",
                "session",
                state,
                "revision",
                "plan",
                "definition",
                "catalog",
                "bundle",
                "authority",
                1,
                "active",
                2_000.0,
                2.0 + index,
            )
        )
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT INTO profile_ceremonies(
                candidate_id, candidate_digest, session_digest, state,
                expected_profile_revision, expected_plan_digest,
                profile_definition_digest, profile_catalog_digest,
                bundle_lock_digest, authority_snapshot_digest,
                security_epoch, review_json, expires_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def test_one_mib_ceremony_compaction_is_bounded_and_operations_keep_reserve(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control" / "reconciliation.sqlite3"
    store = ControlReconciliationStore(
        path,
        clock=lambda: 1_000.0,
        max_database_bytes=1024 * 1024,
        max_ceremony_records=256,
        max_ceremony_bytes=640 * 1024,
        operation_database_reserve_bytes=256 * 1024,
        ceremony_retention_seconds=10.0,
        compaction_batch_size=11,
        session_renewal_debounce_seconds=0.0,
    )
    store.prepare_for_operation()
    store.close()
    _seed_ceremonies(path, expired_count=120, payload_bytes=2_048)

    first_request = _begin(store, 1)
    with sqlite3.connect(path) as connection:
        expired_after_one_batch = int(
            connection.execute(
                "SELECT COUNT(*) FROM profile_ceremonies WHERE state='resolved'"
            ).fetchone()[0]
        )
    assert expired_after_one_batch == 110

    for index in range(20):
        store.renew_session("session-a", expires_at=10_000.0 + index)
    with sqlite3.connect(path) as connection:
        retained = {
            str(row[0])
            for row in connection.execute("SELECT state FROM profile_ceremonies ORDER BY state")
        }
        compacted = int(
            connection.execute("SELECT compacted_ceremonies FROM control_journal_audit").fetchone()[
                0
            ]
        )
    assert retained == {"resolved", "reviewed", "approval_prepared", "approved", "activated"}
    assert compacted == 124

    expected = store.finish_operation(
        first_request,
        session_id="session-a",
        state="succeeded",
        result={"state": "succeeded", "value": "durable"},
    )
    store.close()
    restarted = ControlReconciliationStore(
        path,
        clock=lambda: 1_000.0,
        max_database_bytes=1024 * 1024,
        max_ceremony_bytes=640 * 1024,
        operation_database_reserve_bytes=256 * 1024,
    )
    assert restarted.operation_status(first_request, session_id="session-a") == expected
    snapshot = restarted.journal_snapshot()
    assert snapshot["operation_database_reserve_bytes"] == 256 * 1024
    assert snapshot["ceremony_records"] == 5
    restarted.close()


def test_ceremony_byte_admission_fails_without_consuming_operation_reserve(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control" / "reconciliation.sqlite3"
    store = ControlReconciliationStore(
        path,
        max_database_bytes=1024 * 1024,
        max_ceremony_bytes=4 * 1024,
        operation_database_reserve_bytes=256 * 1024,
    )
    review = {
        "predecessor": {"profile_revision": "revision", "plan_digest": "plan"},
        "catalog_binding": {
            "profile_definition_digest": "definition",
            "profile_catalog_digest": "catalog",
            "bundle_lock_digest": "bundle",
        },
        "profile": {"profile_authority_snapshot_digest": "authority"},
        "resolved_plan": {"security_epoch": 1},
        "padding": "x" * 8_192,
    }
    with pytest.raises(ControlReconciliationCapacityError, match="ceremony byte"):
        store.save_candidate(
            candidate_id="too-large",
            candidate_digest=canonical_digest(review),
            session_id="session-a",
            review=review,
            expires_at=1.0,
        )

    request_id = _begin(store, 2)
    result = store.finish_operation(
        request_id,
        session_id="session-a",
        state="succeeded",
        result={"state": "succeeded"},
    )
    assert result["state"] == "succeeded"
    assert store.journal_snapshot()["ceremony_records"] == 0
    store.close()


def test_migration_lock_repeats_ten_concurrent_initializations(tmp_path: Path) -> None:
    for iteration in range(10):
        path = tmp_path / f"iteration-{iteration}" / "reconciliation.sqlite3"
        path.parent.mkdir(parents=True)
        legacy = sqlite3.connect(path)
        try:
            legacy.executescript(
                """
                CREATE TABLE control_journal_audit (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    compacted_operations INTEGER NOT NULL,
                    compacted_succeeded INTEGER NOT NULL,
                    compacted_failed INTEGER NOT NULL,
                    compacted_indeterminate INTEGER NOT NULL,
                    compacted_recovery_audits INTEGER NOT NULL,
                    first_compacted_at REAL,
                    last_compacted_at REAL
                );
                INSERT INTO control_journal_audit VALUES (1, 0, 0, 0, 0, 0, NULL, NULL);
                """
            )
        finally:
            legacy.close()
        stores = [
            ControlReconciliationStore(path, instance_id=f"{iteration}-{index}")
            for index in range(10)
        ]
        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(lambda store: store.prepare_for_operation(), stores))
        for store in stores:
            store.close()
        connection = sqlite3.connect(path)
        try:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(control_journal_audit)")
            }
            assert "compacted_ceremonies" in columns
        finally:
            connection.close()
        assert not Path(f"{path}-wal").exists()
        assert not Path(f"{path}-shm").exists()
