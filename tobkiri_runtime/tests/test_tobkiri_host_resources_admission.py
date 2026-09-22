"""Adversarial ResourceHandle and admission accounting tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path

import pytest

from tobkiri_host.admission import (
    AdmissionEstimate,
    FairAdmissionQueue,
    QueueScope,
    ResourceAmount,
    ResourceLedger,
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
