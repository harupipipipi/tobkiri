"""Adversarial ResourceHandle and admission accounting tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core_runtime.process_identity import ProcessIdentityEvidence
from tobkiri_host.admission import (
    AdmissionError,
    AdmissionEstimate,
    DurableResourceLedger,
    FairAdmissionQueue,
    QueueScope,
    ResourceAmount,
    ResourceLedger,
    ResourceReservation,
    dead_owner_reservation_ids,
    process_is_alive,
)
from tobkiri_host.errors import (
    QueueFullError,
    ResourceExhaustedError,
    ResourceHandleError,
)
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.resources import ResourceHandleTable


def digest(character: str) -> str:
    return f"sha256:{hashlib.sha256(character.encode()).hexdigest()}"


def context(**changes: object) -> RequestContext:
    base = RequestContext(
        request_id="request-1",
        trace_id="trace-1",
        caller_principal=OpaqueAuthorityRef("authority:caller"),
        profile_id="profile-1",
        activation_id="activation-1",
        activation_digest=digest("a"),
        plan_digest=digest("p"),
        security_epoch=7,
        caller_session_id="caller-session",
        caller_domain_id="domain-caller",
        caller_boot_epoch=2,
        target_domain_id="domain-target",
        target_boot_epoch=3,
        target_backend_digest=digest("backend"),
        profile_authority_digest=digest("profile-authority"),
        fencing_token=1,
        handle_namespace="caller-handles",
    )
    return replace(base, **changes)


def bind_read(table: ResourceHandleTable, root: Path):
    return table.bind_file(
        root=root,
        relative_path="document.txt",
        operations=frozenset({"read"}),
        owner=OpaqueAuthorityRef("authority:caller"),
        target=OpaqueAuthorityRef("authority:target"),
        context=context(),
        target_domain_id="domain-target",
        target_boot_epoch=3,
        target_namespace="target-handles",
        ttl_seconds=30,
        max_uses=2,
        max_bytes=300,
    )


def read(table: ResourceHandleTable, handle, **changes: object) -> bytes:
    arguments = {
        "context": context(),
        "target": OpaqueAuthorityRef("authority:target"),
        "domain_id": "domain-target",
        "boot_epoch": 3,
        "namespace": "target-handles",
        "max_bytes": 100,
    }
    arguments.update(changes)
    return table.read(handle, **arguments)


def test_handle_reads_bound_descriptor_and_enforces_use_count(tmp_path: Path) -> None:
    path = tmp_path / "document.txt"
    path.write_text("safe", encoding="utf-8")
    table = ResourceHandleTable()
    handle = bind_read(table, tmp_path)
    assert read(table, handle) == b"safe"
    assert read(table, handle) == b"safe"
    with pytest.raises(ResourceHandleError, match="use count"):
        read(table, handle)
    table.close()


def test_handle_rejects_path_traversal_and_symlink(tmp_path: Path) -> None:
    (tmp_path / "document.txt").write_text("safe", encoding="utf-8")
    table = ResourceHandleTable()
    with pytest.raises(ResourceHandleError, match="relative"):
        table.bind_file(
            root=tmp_path,
            relative_path="../document.txt",
            operations=frozenset({"read"}),
            owner=OpaqueAuthorityRef("authority:caller"),
            target=OpaqueAuthorityRef("authority:target"),
            context=context(),
            target_domain_id="domain-target",
            target_boot_epoch=3,
            target_namespace="target-handles",
            ttl_seconds=30,
            max_uses=1,
            max_bytes=10,
        )
    (tmp_path / "link.txt").symlink_to(tmp_path / "document.txt")
    with pytest.raises(ResourceHandleError, match="safely bound"):
        table.bind_file(
            root=tmp_path,
            relative_path="link.txt",
            operations=frozenset({"read"}),
            owner=OpaqueAuthorityRef("authority:caller"),
            target=OpaqueAuthorityRef("authority:target"),
            context=context(),
            target_domain_id="domain-target",
            target_boot_epoch=3,
            target_namespace="target-handles",
            ttl_seconds=30,
            max_uses=1,
            max_bytes=10,
        )


def test_handle_revokes_on_toctou_path_identity_swap(tmp_path: Path) -> None:
    path = tmp_path / "document.txt"
    path.write_text("approved", encoding="utf-8")
    table = ResourceHandleTable()
    handle = bind_read(table, tmp_path)
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("attacker", encoding="utf-8")
    os.replace(replacement, path)
    with pytest.raises(ResourceHandleError, match="identity changed"):
        read(table, handle)
    with pytest.raises(ResourceHandleError, match="unknown or revoked"):
        read(table, handle)


def test_handle_revokes_on_generation_or_epoch_change(tmp_path: Path) -> None:
    path = tmp_path / "document.txt"
    path.write_text("approved", encoding="utf-8")
    table = ResourceHandleTable()
    handle = bind_read(table, tmp_path)
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ResourceHandleError, match="generation changed"):
        read(table, handle)

    handle = bind_read(table, tmp_path)
    with pytest.raises(ResourceHandleError, match="binding mismatch"):
        read(table, handle, context=context(security_epoch=8))


def test_write_handle_requires_consistency_precondition(tmp_path: Path) -> None:
    path = tmp_path / "document.txt"
    path.write_text("old", encoding="utf-8")
    table = ResourceHandleTable()
    with pytest.raises(ResourceHandleError, match="version precondition"):
        table.bind_file(
            root=tmp_path,
            relative_path="document.txt",
            operations=frozenset({"write"}),
            owner=OpaqueAuthorityRef("authority:caller"),
            target=OpaqueAuthorityRef("authority:target"),
            context=context(),
            target_domain_id="domain-target",
            target_boot_epoch=3,
            target_namespace="target-handles",
            ttl_seconds=30,
            max_uses=1,
            max_bytes=10,
        )


def ledger(memory: int = 1000) -> ResourceLedger:
    return ResourceLedger(
        runtime_limit=ResourceAmount(memory, 1000, 10, 10),
        host_free_guard=ResourceAmount(100, 0, 0, 0),
        profile_limits={"p1": ResourceAmount(800, 1000, 10, 10)},
    )


def scope(binding: str, caller: str = "c1") -> QueueScope:
    return QueueScope("p1", caller, "pack1", binding)


def test_admission_charge_uses_maximum_and_concurrency() -> None:
    estimate = AdmissionEstimate(
        measured_p95_bytes=300,
        declared_minimum_bytes=100,
        runtime_floor_bytes=200,
        profile_reservation_bytes=250,
        backend_overhead_bytes=150,
        concurrency=2,
    )
    assert estimate.charge().memory_bytes == 600


@pytest.mark.parametrize("field", [
    "measured_p95_bytes", "declared_minimum_bytes", "runtime_floor_bytes",
    "profile_reservation_bytes", "backend_overhead_bytes", "disk_bytes",
    "declared_upper_bound_bytes", "concurrency",
])
@pytest.mark.parametrize("value", [-1, True, 1.5, float("nan"), float("inf")])
def test_admission_rejects_each_invalid_input_before_maximum(
    field: str, value: object
) -> None:
    estimate = AdmissionEstimate(100, 100, 100, 100, 100)
    with pytest.raises(AdmissionError):
        replace(estimate, **{field: value}).charge()


@pytest.mark.parametrize("field", [
    "memory_bytes", "disk_bytes", "process_slots", "start_slots",
])
@pytest.mark.parametrize("value", [-1, True, 1.5, float("nan"), float("inf")])
def test_resource_amount_rejects_invalid_axes(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(ResourceAmount(100), **{field: value})


def test_admission_charge_honors_declared_upper_bound() -> None:
    estimate = AdmissionEstimate(
        measured_p95_bytes=300,
        declared_minimum_bytes=100,
        runtime_floor_bytes=200,
        profile_reservation_bytes=250,
        backend_overhead_bytes=150,
        declared_upper_bound_bytes=900,
    )
    assert estimate.charge().memory_bytes == 900
    with pytest.raises(AdmissionError, match="cannot be negative"):
        AdmissionEstimate(
            measured_p95_bytes=1,
            declared_minimum_bytes=1,
            runtime_floor_bytes=1,
            profile_reservation_bytes=1,
            backend_overhead_bytes=1,
            declared_upper_bound_bytes=-1,
        ).charge()


def test_durable_ledger_survives_restart_and_fences_successor(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "admission" / "reservations.json"
    limits = {
        "runtime_limit": ResourceAmount(1000, 100, 4, 4),
        "host_free_guard": ResourceAmount(100, 0, 0, 0),
        "profile_limits": {"p1": ResourceAmount(800, 100, 4, 4)},
    }
    identity = {
        "profile_id": "p1",
        "profile_revision": digest("revision-a"),
        "activation_id": "activation-a",
        "plan_digest": digest("plan-a"),
    }
    first = DurableResourceLedger(state_path=state_path, identity=identity, **limits)
    reservation = first.reserve("p1", ResourceAmount(300, 2, 1, 1))
    assert state_path.is_file()

    restarted = DurableResourceLedger(
        state_path=state_path,
        identity=identity,
        **limits,
    )
    assert restarted.runtime_used == reservation.amount
    restarted.release(reservation.reservation_id)
    assert restarted.runtime_used == ResourceAmount(0, 0, 0, 0)

    successor = DurableResourceLedger(
        state_path=state_path,
        identity={**identity, "activation_id": "activation-b"},
        **limits,
    )
    assert successor.runtime_used == ResourceAmount(0, 0, 0, 0)


def test_durable_ledger_rejects_corrupt_state(tmp_path: Path) -> None:
    state_path = tmp_path / "reservations.json"
    state_path.write_text("not-json", encoding="utf-8")
    with pytest.raises(AdmissionError, match="ledger is invalid"):
        DurableResourceLedger(
            runtime_limit=ResourceAmount(1000, 100, 4, 4),
            host_free_guard=ResourceAmount(100, 0, 0, 0),
            profile_limits={"p1": ResourceAmount(800, 100, 4, 4)},
            state_path=state_path,
            identity={"profile_id": "p1", "activation_id": "a"},
        )


def test_durable_reservation_requires_release_after_deadline_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither wall time nor an activation change proves a worker has exited."""
    options = {
        "runtime_limit": ResourceAmount(100, 0, 1, 1),
        "host_free_guard": ResourceAmount(0, 0, 0, 0),
        "profile_limits": {"p1": ResourceAmount(100, 0, 1, 1)},
        "state_path": tmp_path / "reservations.json",
        "lease_ttl_seconds": 1,
    }
    identity = {"profile_id": "p1", "activation_id": "a"}
    monkeypatch.setattr("tobkiri_host.admission.time.time", lambda: 1000)
    first = DurableResourceLedger(identity=identity, **options)
    reservation = first.reserve("p1", ResourceAmount(100))
    original = options["state_path"].read_bytes()

    monkeypatch.setattr("tobkiri_host.admission.time.time", lambda: 2000)
    with pytest.raises(ResourceExhaustedError):
        first.reserve("p1", ResourceAmount(100))
    reopened = DurableResourceLedger(identity=identity, **options)
    assert reopened.runtime_used == reservation.amount
    with pytest.raises(ResourceExhaustedError):
        reopened.reserve("p1", ResourceAmount(100))
    with pytest.raises(AdmissionError, match="confirmed supervisor release"):
        DurableResourceLedger(
            identity={**identity, "activation_id": "b"}, **options,
        )
    assert options["state_path"].read_bytes() == original

    # This is the existing trusted supervisor release, not a timed lease reap.
    reopened.release(reservation.reservation_id)
    successor = DurableResourceLedger(
        identity={**identity, "activation_id": "b"}, **options,
    )
    assert successor.reserve("p1", ResourceAmount(100)).amount == reservation.amount


def test_durable_ledger_accepts_only_confirmed_supervisor_release(
    tmp_path: Path,
) -> None:
    """A successor persists an empty ledger only after exact child cleanup."""

    options = {
        "runtime_limit": ResourceAmount(100, 0, 2, 2),
        "host_free_guard": ResourceAmount(0, 0, 0, 0),
        "profile_limits": {"p1": ResourceAmount(100, 0, 2, 2)},
        "state_path": tmp_path / "reservations.json",
    }
    identity = {"profile_id": "p1", "activation_id": "a"}
    first = DurableResourceLedger(identity=identity, **options)
    reservation = first.reserve("p1", ResourceAmount(10))
    calls: list[object] = []

    successor = DurableResourceLedger(
        identity={**identity, "activation_id": "b"},
        confirmed_supervisor_release=lambda saved, rows: (
            calls.append((saved, rows)) or (reservation.reservation_id,)
        ),
        **options,
    )

    assert calls == [(identity, (reservation,))]
    assert successor.runtime_used == ResourceAmount(0, 0, 0, 0)
    saved = json.loads(options["state_path"].read_text(encoding="utf-8"))
    assert saved["identity"]["activation_id"] == "b"
    assert saved["reservations"] == []


def test_durable_ledger_retains_each_unproven_predecessor_reservation(
    tmp_path: Path,
) -> None:
    """One exact child receipt never releases another ledger row."""

    state_path = tmp_path / "reservations.json"
    options = {
        "runtime_limit": ResourceAmount(100, 0, 2, 2),
        "host_free_guard": ResourceAmount(0, 0, 0, 0),
        "profile_limits": {"p1": ResourceAmount(100, 0, 2, 2)},
        "state_path": state_path,
    }
    identity = {"profile_id": "p1", "activation_id": "a"}
    first = DurableResourceLedger(identity=identity, **options)
    proven = first.reserve("p1", ResourceAmount(10))
    retained = first.reserve("p1", ResourceAmount(20))

    with pytest.raises(AdmissionError, match="confirmed supervisor release"):
        DurableResourceLedger(
            identity={**identity, "activation_id": "b"},
            confirmed_supervisor_release=lambda _saved, _rows: (
                proven.reservation_id,
            ),
            **options,
        )

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["identity"] == identity
    assert [item["reservation_id"] for item in saved["reservations"]] == [
        retained.reservation_id
    ]

    # A second interruption resumes from the one remaining row. The already
    # released reservation cannot be recovered or charged twice.
    reopened = DurableResourceLedger(identity=identity, **options)
    assert reopened.runtime_used == retained.amount
    calls: list[tuple[str, ...]] = []
    successor = DurableResourceLedger(
        identity={**identity, "activation_id": "b"},
        confirmed_supervisor_release=lambda _saved, rows: (
            calls.append(tuple(item.reservation_id for item in rows))
            or (retained.reservation_id,)
        ),
        **options,
    )
    assert calls == [(retained.reservation_id,)]
    assert successor.runtime_used == ResourceAmount(0, 0, 0, 0)


@pytest.mark.parametrize(
    "released",
    ["reservation", ("unknown",), ("duplicate", "duplicate")],
)
def test_durable_ledger_rejects_invalid_release_identity_without_mutation(
    tmp_path: Path, released: object,
) -> None:
    """Malformed, unknown, or duplicate recovery results cannot alter a ledger."""

    state_path = tmp_path / "reservations.json"
    options = {
        "runtime_limit": ResourceAmount(100, 0, 1, 1),
        "host_free_guard": ResourceAmount(0, 0, 0, 0),
        "profile_limits": {"p1": ResourceAmount(100, 0, 1, 1)},
        "state_path": state_path,
    }
    identity = {"profile_id": "p1", "activation_id": "a"}
    first = DurableResourceLedger(identity=identity, **options)
    reservation = first.reserve("p1", ResourceAmount(10))
    if released == ("duplicate", "duplicate"):
        released = (reservation.reservation_id, reservation.reservation_id)
    before = state_path.read_bytes()

    with pytest.raises(AdmissionError, match="durable admission ledger is invalid"):
        DurableResourceLedger(
            identity={**identity, "activation_id": "b"},
            confirmed_supervisor_release=lambda _saved, _rows: released,  # type: ignore[arg-type,return-value]
            **options,
        )

    assert state_path.read_bytes() == before


@pytest.mark.parametrize("ttl", [True, None, "1", 0, -1, float("nan"), float("inf")])
def test_durable_ledger_rejects_invalid_ttl_before_creating_state(
    tmp_path: Path, ttl: object,
) -> None:
    """Invalid journal metadata must not create a silently droppable charge."""
    path = tmp_path / "reservations.json"
    with pytest.raises(ValueError, match="TTL"):
        DurableResourceLedger(
            runtime_limit=ResourceAmount(100),
            host_free_guard=ResourceAmount(0, 0, 0, 0),
            profile_limits={"p1": ResourceAmount(100)},
            state_path=path, identity={"profile_id": "p1"},
            lease_ttl_seconds=ttl,
        )
    assert not path.exists()


@pytest.mark.parametrize("field,value", [
    ("reservation_id", ""), ("profile_id", ""),
    ("expires_at", None), ("expires_at", True), ("expires_at", float("nan")),
])
def test_durable_ledger_never_skips_a_malformed_reservation(
    tmp_path: Path, field: str, value: object,
) -> None:
    """Corrupt metadata cannot erase an otherwise outstanding resource charge."""
    path = tmp_path / "reservations.json"
    options = {
        "runtime_limit": ResourceAmount(100),
        "host_free_guard": ResourceAmount(0, 0, 0, 0),
        "profile_limits": {"p1": ResourceAmount(100)},
        "state_path": path, "identity": {"profile_id": "p1"},
    }
    ledger = DurableResourceLedger(**options)
    ledger.reserve("p1", ResourceAmount(100))
    saved = json.loads(path.read_text())
    saved["reservations"][0][field] = value
    path.write_text(json.dumps(saved))
    with pytest.raises(AdmissionError, match="ledger is invalid"):
        DurableResourceLedger(**options)


def test_ledger_rejects_before_crossing_host_guard() -> None:
    resource_ledger = ledger()
    resource_ledger.reserve("p1", ResourceAmount(700, 0, 1, 1))
    with pytest.raises(ResourceExhaustedError, match="Host free-resource guard"):
        resource_ledger.reserve("p1", ResourceAmount(250, 0, 1, 1))


def test_queue_enforces_binding_quota_and_releases_on_cancel() -> None:
    resource_ledger = ledger()
    queue = FairAdmissionQueue(resource_ledger, binding_limit=1)
    item = queue.enqueue(scope("b1"), ResourceAmount(100), wait_timeout_seconds=10)
    with pytest.raises(QueueFullError, match="binding"):
        queue.enqueue(scope("b1"), ResourceAmount(100), wait_timeout_seconds=10)
    queue.cancel(item.item_id)
    assert resource_ledger.runtime_used.memory_bytes == 0


def test_queue_round_robins_across_bindings() -> None:
    resource_ledger = ledger()
    queue = FairAdmissionQueue(resource_ledger)
    first = queue.enqueue(scope("b1"), ResourceAmount(100), wait_timeout_seconds=10)
    second = queue.enqueue(scope("b1"), ResourceAmount(100), wait_timeout_seconds=10)
    third = queue.enqueue(scope("b2", "c2"), ResourceAmount(100), wait_timeout_seconds=10)
    assert queue.pop() == first
    assert queue.pop() == third
    assert queue.pop() == second
    for item in (first, second, third):
        queue.complete(item)


def _dead_pid() -> int:
    """Return a pid that is provably no longer a live process."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _ledger_row(
    reservation_id: str,
    *,
    owner_pid: int,
    detached: bool,
    owner_identity: str | None = None,
    memory_bytes: int = 100,
) -> dict[str, object]:
    row: dict[str, object] = {
        "reservation_id": reservation_id,
        "profile_id": "p1",
        "amount": {
            "memory_bytes": memory_bytes,
            "disk_bytes": 0,
            "process_slots": 1,
            "start_slots": 1,
        },
        "expires_at": 1.0,
        "owner_pid": owner_pid,
        "detached": detached,
    }
    if owner_identity is not None:
        row["owner_identity"] = owner_identity
    return row


def _write_ledger(
    state_path: Path,
    identity: dict[str, str],
    rows: list[dict[str, object]],
) -> None:
    state_path.write_text(
        json.dumps(
            {
                "schema": "io.tobkiri.admission-ledger.v1",
                "identity": identity,
                "reservations": rows,
            }
        ),
        encoding="utf-8",
    )


def _durable_options(state_path: Path) -> dict[str, object]:
    return {
        "runtime_limit": ResourceAmount(1000, 100, 8, 8),
        "host_free_guard": ResourceAmount(100, 0, 0, 0),
        "profile_limits": {"p1": ResourceAmount(800, 100, 8, 8)},
        "state_path": state_path,
    }


def test_process_is_alive_only_accepts_live_positive_pids() -> None:
    assert process_is_alive(os.getpid()) is True
    assert process_is_alive(_dead_pid()) is False
    for invalid in (0, -1, True, "1", None, 1.5):
        assert process_is_alive(invalid) is False


def test_dead_owner_reservation_ids_release_only_proven_rows() -> None:
    dead = _dead_pid()
    rows = (
        ResourceReservation(
            "dead", "p1", ResourceAmount(1), owner_pid=dead,
        ),
        ResourceReservation(
            "live", "p1", ResourceAmount(1), owner_pid=os.getpid(),
        ),
        ResourceReservation("unrecorded", "p1", ResourceAmount(1)),
        ResourceReservation(
            "detached", "p1", ResourceAmount(1),
            owner_pid=dead, detached=True,
        ),
    )
    assert dead_owner_reservation_ids(rows) == ("dead",)


def test_dead_owner_reservation_ids_release_reused_pid_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live PID with a mismatched start identity is a dead reused owner."""
    monkeypatch.setattr(
        "tobkiri_host.admission.process_start_identity",
        lambda pid: ProcessIdentityEvidence("live", f"darwin:{pid}:9:000001"),
    )
    rows = (
        ResourceReservation(
            "reused",
            "p1",
            ResourceAmount(1),
            owner_pid=os.getpid(),
            owner_identity="darwin:1:1:000000",
        ),
    )
    assert dead_owner_reservation_ids(rows) == ("reused",)


def test_dead_owner_reservation_ids_retain_matching_identity_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live PID with a matching start identity is still the owner."""
    identity = f"darwin:{os.getpid()}:9:000001"
    monkeypatch.setattr(
        "tobkiri_host.admission.process_start_identity",
        lambda pid: ProcessIdentityEvidence("live", identity),
    )
    rows = (
        ResourceReservation(
            "owned",
            "p1",
            ResourceAmount(1),
            owner_pid=os.getpid(),
            owner_identity=identity,
        ),
    )
    assert dead_owner_reservation_ids(rows) == ()


def test_dead_owner_reservation_ids_retain_unknown_identity_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indeterminate identity evidence never releases a journaled charge."""
    monkeypatch.setattr(
        "tobkiri_host.admission.process_start_identity",
        lambda pid: ProcessIdentityEvidence("unknown"),
    )
    rows = (
        ResourceReservation(
            "owned",
            "p1",
            ResourceAmount(1),
            owner_pid=os.getpid(),
            owner_identity=f"darwin:{os.getpid()}:9:000001",
        ),
    )
    assert dead_owner_reservation_ids(rows) == ()


def test_dead_owner_reservation_ids_release_dead_identity_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded identity whose PID is gone is a dead owner."""
    monkeypatch.setattr(
        "tobkiri_host.admission.process_start_identity",
        lambda pid: ProcessIdentityEvidence("dead"),
    )
    rows = (
        ResourceReservation(
            "gone",
            "p1",
            ResourceAmount(1),
            owner_pid=_dead_pid(),
            owner_identity="darwin:1:1:000000",
        ),
    )
    assert dead_owner_reservation_ids(rows) == ("gone",)


def test_dead_owner_reservation_ids_legacy_rows_use_existence_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows without a recorded identity keep the raw PID liveness fallback."""
    monkeypatch.setattr(
        "tobkiri_host.admission.process_start_identity",
        lambda pid: pytest.fail("identity probe on a legacy row"),
    )
    monkeypatch.setattr(
        "tobkiri_host.admission.process_is_alive",
        lambda pid: pid != 4242,
    )
    rows = (
        ResourceReservation(
            "legacy-dead", "p1", ResourceAmount(1), owner_pid=4242
        ),
        ResourceReservation(
            "legacy-live", "p1", ResourceAmount(1), owner_pid=7
        ),
    )
    assert dead_owner_reservation_ids(rows) == ("legacy-dead",)


def test_durable_ledger_persists_owner_pid_and_detached_flag(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "reservations.json"
    identity = {"profile_id": "p1", "activation_id": "a"}
    ledger = DurableResourceLedger(identity=identity, **_durable_options(state_path))
    detached = ledger.reserve("p1", ResourceAmount(10), detached=True)
    plain = ledger.reserve("p1", ResourceAmount(20))

    rows = {
        item["reservation_id"]: item
        for item in json.loads(state_path.read_text(encoding="utf-8"))[
            "reservations"
        ]
    }
    assert rows[detached.reservation_id]["owner_pid"] == os.getpid()
    assert rows[detached.reservation_id]["detached"] is True
    assert rows[plain.reservation_id]["owner_pid"] == os.getpid()
    assert rows[plain.reservation_id]["detached"] is False


def test_durable_ledger_persists_owner_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New reservations journal the owner's kernel process-start token."""
    state_path = tmp_path / "reservations.json"
    monkeypatch.setattr(
        "tobkiri_host.admission.process_start_identity",
        lambda pid: ProcessIdentityEvidence("live", f"darwin:{pid}:9:000001"),
    )
    ledger = DurableResourceLedger(
        identity={"profile_id": "p1", "activation_id": "a"},
        **_durable_options(state_path),
    )
    reservation = ledger.reserve("p1", ResourceAmount(10))
    expected = f"darwin:{os.getpid()}:9:000001"
    assert reservation.owner_identity == expected
    rows = json.loads(state_path.read_text(encoding="utf-8"))["reservations"]
    assert rows[0]["owner_identity"] == expected


def test_durable_ledger_releases_dead_owner_rows_on_identity_change(
    tmp_path: Path,
) -> None:
    """A journaled owner that is provably dead releases its own charge."""
    state_path = tmp_path / "reservations.json"
    _write_ledger(
        state_path,
        {"profile_id": "p1", "activation_id": "a"},
        [_ledger_row("stale", owner_pid=_dead_pid(), detached=False)],
    )
    ledger = DurableResourceLedger(
        identity={"profile_id": "p1", "activation_id": "b"},
        confirmed_supervisor_release=(
            lambda _saved, rows: dead_owner_reservation_ids(rows)
        ),
        **_durable_options(state_path),
    )
    assert ledger.runtime_used == ResourceAmount(0, 0, 0, 0)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["identity"]["activation_id"] == "b"
    assert saved["reservations"] == []


def test_durable_ledger_retains_detached_row_with_dead_owner(
    tmp_path: Path,
) -> None:
    """A dead owner never releases a charge that may back a live domain."""
    state_path = tmp_path / "reservations.json"
    _write_ledger(
        state_path,
        {"profile_id": "p1", "activation_id": "a"},
        [_ledger_row("pack", owner_pid=_dead_pid(), detached=True)],
    )
    with pytest.raises(AdmissionError, match="confirmed supervisor release"):
        DurableResourceLedger(
            identity={"profile_id": "p1", "activation_id": "b"},
            confirmed_supervisor_release=(
                lambda _saved, rows: dead_owner_reservation_ids(rows)
            ),
            **_durable_options(state_path),
        )


def test_durable_ledger_retains_row_whose_recorded_owner_is_alive(
    tmp_path: Path,
) -> None:
    """A live recorded owner keeps its reservations across identity change."""
    state_path = tmp_path / "reservations.json"
    _write_ledger(
        state_path,
        {"profile_id": "p1", "activation_id": "a"},
        [_ledger_row("owned", owner_pid=os.getpid(), detached=False)],
    )
    with pytest.raises(AdmissionError, match="confirmed supervisor release"):
        DurableResourceLedger(
            identity={"profile_id": "p1", "activation_id": "b"},
            confirmed_supervisor_release=(
                lambda _saved, rows: dead_owner_reservation_ids(rows)
            ),
            **_durable_options(state_path),
        )


def test_durable_ledger_retains_row_without_recorded_owner(
    tmp_path: Path,
) -> None:
    """Rows written before owner journaling remain unproven and fail closed."""
    state_path = tmp_path / "reservations.json"
    row = _ledger_row("legacy", owner_pid=_dead_pid(), detached=False)
    del row["owner_pid"]
    del row["detached"]
    _write_ledger(state_path, {"profile_id": "p1", "activation_id": "a"}, [row])
    with pytest.raises(AdmissionError, match="confirmed supervisor release"):
        DurableResourceLedger(
            identity={"profile_id": "p1", "activation_id": "b"},
            confirmed_supervisor_release=(
                lambda _saved, rows: dead_owner_reservation_ids(rows)
            ),
            **_durable_options(state_path),
        )


def test_durable_ledger_releases_reused_pid_owner_on_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live PID bound to a different start token is a dead reused owner."""
    state_path = tmp_path / "reservations.json"
    _write_ledger(
        state_path,
        {"profile_id": "p1", "activation_id": "a"},
        [
            _ledger_row(
                "reused",
                owner_pid=os.getpid(),
                detached=False,
                owner_identity="darwin:1:1:000000",
            )
        ],
    )
    monkeypatch.setattr(
        "tobkiri_host.admission.process_start_identity",
        lambda pid: ProcessIdentityEvidence("live", f"darwin:{pid}:9:000001"),
    )
    ledger = DurableResourceLedger(
        identity={"profile_id": "p1", "activation_id": "b"},
        confirmed_supervisor_release=(
            lambda _saved, rows: dead_owner_reservation_ids(rows)
        ),
        **_durable_options(state_path),
    )
    assert ledger.runtime_used == ResourceAmount(0, 0, 0, 0)


def test_durable_ledger_retains_row_with_matching_owner_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live owner with a matching start identity keeps its charge."""
    state_path = tmp_path / "reservations.json"
    identity_token = f"darwin:{os.getpid()}:9:000001"
    _write_ledger(
        state_path,
        {"profile_id": "p1", "activation_id": "a"},
        [
            _ledger_row(
                "owned",
                owner_pid=os.getpid(),
                detached=False,
                owner_identity=identity_token,
            )
        ],
    )
    monkeypatch.setattr(
        "tobkiri_host.admission.process_start_identity",
        lambda pid: ProcessIdentityEvidence("live", identity_token),
    )
    with pytest.raises(AdmissionError, match="confirmed supervisor release"):
        DurableResourceLedger(
            identity={"profile_id": "p1", "activation_id": "b"},
            confirmed_supervisor_release=(
                lambda _saved, rows: dead_owner_reservation_ids(rows)
            ),
            **_durable_options(state_path),
        )


def test_durable_ledger_retains_row_when_owner_identity_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indeterminate identity evidence never releases shared capacity."""
    state_path = tmp_path / "reservations.json"
    _write_ledger(
        state_path,
        {"profile_id": "p1", "activation_id": "a"},
        [
            _ledger_row(
                "owned",
                owner_pid=os.getpid(),
                detached=False,
                owner_identity="darwin:1:1:000000",
            )
        ],
    )
    monkeypatch.setattr(
        "tobkiri_host.admission.process_start_identity",
        lambda pid: ProcessIdentityEvidence("unknown"),
    )
    with pytest.raises(AdmissionError, match="confirmed supervisor release"):
        DurableResourceLedger(
            identity={"profile_id": "p1", "activation_id": "b"},
            confirmed_supervisor_release=(
                lambda _saved, rows: dead_owner_reservation_ids(rows)
            ),
            **_durable_options(state_path),
        )


def test_durable_ledger_sweeps_reused_pid_owner_on_same_identity_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restarting one activation reclaims a reused-PID owner row."""
    state_path = tmp_path / "reservations.json"
    identity = {"profile_id": "p1", "activation_id": "a"}
    _write_ledger(
        state_path,
        identity,
        [
            _ledger_row(
                "reused",
                owner_pid=os.getpid(),
                detached=False,
                owner_identity="darwin:1:1:000000",
            ),
            _ledger_row(
                "pack",
                owner_pid=os.getpid(),
                detached=True,
                owner_identity="darwin:1:1:000000",
            ),
        ],
    )
    monkeypatch.setattr(
        "tobkiri_host.admission.process_start_identity",
        lambda pid: ProcessIdentityEvidence("live", f"darwin:{pid}:9:000001"),
    )
    ledger = DurableResourceLedger(
        identity=identity, **_durable_options(state_path)
    )
    assert ledger.runtime_used == ResourceAmount(100, 0, 1, 1)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert [item["reservation_id"] for item in saved["reservations"]] == [
        "pack"
    ]


def test_durable_ledger_sweeps_dead_owner_rows_on_same_identity_restart(
    tmp_path: Path,
) -> None:
    """Restarting one activation reclaims only provably dead non-detached rows."""
    state_path = tmp_path / "reservations.json"
    identity = {"profile_id": "p1", "activation_id": "a"}
    dead = _dead_pid()
    _write_ledger(
        state_path,
        identity,
        [
            _ledger_row("stale", owner_pid=dead, detached=False),
            _ledger_row("pack", owner_pid=dead, detached=True),
            _ledger_row("owned", owner_pid=os.getpid(), detached=False),
        ],
    )
    ledger = DurableResourceLedger(identity=identity, **_durable_options(state_path))
    assert ledger.runtime_used == ResourceAmount(200, 0, 2, 2)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert [item["reservation_id"] for item in saved["reservations"]] == [
        "owned",
        "pack",
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_pid", -1),
        ("owner_pid", "7"),
        ("owner_pid", True),
        ("owner_pid", 1.5),
        ("owner_identity", 7),
        ("owner_identity", True),
        ("owner_identity", 1.5),
        ("detached", "yes"),
        ("detached", 1),
    ],
)
def test_durable_ledger_rejects_invalid_owner_metadata(
    tmp_path: Path, field: str, value: object,
) -> None:
    state_path = tmp_path / "reservations.json"
    row = _ledger_row("row", owner_pid=_dead_pid(), detached=False)
    row[field] = value
    _write_ledger(state_path, {"profile_id": "p1", "activation_id": "a"}, [row])
    with pytest.raises(AdmissionError, match="durable admission ledger is invalid"):
        DurableResourceLedger(
            identity={"profile_id": "p1", "activation_id": "a"},
            **_durable_options(state_path),
        )
