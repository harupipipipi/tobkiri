"""Focused Host workspace lease and descriptor-CAS tests."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ecosystem.rumi_workspace_mount_pack.runtime.mounts import (
    HOST_PROVIDER_FACTORY,
    WorkspaceMountStore,
    WorkspaceResourceHostFactoryV4,
    capture_selected_workspace_binding,
)
from tobkiri_host.errors import ResourceHandleError
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import (
    WorkspaceBatchMutation,
    WorkspaceMutationIdentity,
    WorkspaceMutationLeaseRequest,
    WorkspaceMutationPort,
)
from tobkiri_host.resources import ResourceHandleTable
from tobkiri_host.workspace_mutation import (
    HostWorkspaceMutationPort,
    WorkspaceMutationBinding,
    WorkspaceMutationCoordinator,
    WorkspaceMutationError,
)


TARGET = OpaqueAuthorityRef("authority:target")


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _context(**changes: object) -> RequestContext:
    value = RequestContext(
        request_id="request-1",
        trace_id="trace-1",
        caller_principal=OpaqueAuthorityRef("authority:caller"),
        profile_id="profile-1",
        activation_id="activation-1",
        activation_digest=_digest("activation"),
        plan_digest=_digest("plan"),
        security_epoch=7,
        caller_session_id="session-1",
        caller_domain_id="caller-domain",
        caller_boot_epoch=2,
        target_domain_id="target-domain",
        target_boot_epoch=3,
        target_backend_digest=_digest("backend"),
        profile_authority_digest=_digest("authority"),
        fencing_token=1,
        handle_namespace="target-handles",
    )
    return replace(value, **changes)


def _binding(root: Path, *, revision: int = 4) -> WorkspaceMutationBinding:
    value = root.stat()
    return WorkspaceMutationBinding(
        profile_id="profile-1",
        workspace_id="workspace-1",
        mount_revision=revision,
        canonical_root=root,
        root_st_dev=value.st_dev,
        root_st_ino=value.st_ino,
    )


def _identity(*, context: RequestContext | None = None) -> WorkspaceMutationIdentity:
    return WorkspaceMutationIdentity(
        context=context or _context(),
        target_principal=TARGET,
        target_domain_id="target-domain",
        target_boot_epoch=3,
        target_namespace="target-handles",
    )


def test_workspace_mutation_identity_requires_host_namespace() -> None:
    with pytest.raises(ValueError, match="target namespace mismatch"):
        WorkspaceMutationIdentity(
            context=_context(handle_namespace="other-handles"),
            target_principal=TARGET,
            target_domain_id="target-domain",
            target_boot_epoch=3,
            target_namespace="target-handles",
        )


def _acquire(
    coordinator: WorkspaceMutationCoordinator,
    binding: WorkspaceMutationBinding,
    *,
    context: RequestContext | None = None,
):
    return coordinator.acquire(
        binding=binding,
        context=context or _context(),
        target=TARGET,
        target_domain_id="target-domain",
        target_boot_epoch=3,
        target_namespace="target-handles",
    )


def _bind_replace(table: ResourceHandleTable, root: Path):
    return table.bind_file(
        root=root,
        relative_path="nested/document.txt",
        operations=frozenset({"write"}),
        owner=OpaqueAuthorityRef("authority:caller"),
        target=TARGET,
        context=_context(),
        target_domain_id="target-domain",
        target_boot_epoch=3,
        target_namespace="target-handles",
        ttl_seconds=30,
        max_uses=1,
        max_bytes=100,
        atomic_replace=True,
    )


def _bind_absent(table: ResourceHandleTable, root: Path):
    return table.bind_absent_file(
        root=root,
        relative_path="nested/created.txt",
        owner=OpaqueAuthorityRef("authority:caller"),
        target=TARGET,
        context=_context(),
        target_domain_id="target-domain",
        target_boot_epoch=3,
        target_namespace="target-handles",
        ttl_seconds=30,
        max_uses=1,
        max_bytes=100,
    )


def _replace(
    table: ResourceHandleTable,
    handle: object,
    lease: object,
    data: bytes,
) -> int:
    return table.compare_and_replace_file(
        handle,  # type: ignore[arg-type]
        data,
        lease=lease,  # type: ignore[arg-type]
        context=_context(),
        target=TARGET,
        domain_id="target-domain",
        boot_epoch=3,
        namespace="target-handles",
    )


def test_lease_is_request_bound_and_namespace_close_releases_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    binding = _binding(root)
    coordinator = WorkspaceMutationCoordinator(
        tmp_path / "host-locks",
        lock_timeout_seconds=0.05,
    )
    lease = _acquire(coordinator, binding)
    with pytest.raises(WorkspaceMutationError, match="binding mismatch"):
        lease.assert_bound(
            context=_context(security_epoch=8),
            target=TARGET,
            target_domain_id="target-domain",
            target_boot_epoch=3,
            target_namespace="target-handles",
        )
    with pytest.raises(WorkspaceMutationError, match="deadline"):
        _acquire(coordinator, binding)
    coordinator.close_namespace("target-handles")
    with _acquire(coordinator, binding) as replacement:
        replacement.revalidate_root()


@pytest.mark.skipif(os.name == "nt", reason="the probe uses POSIX flock")
def test_lease_holds_kernel_lock_against_another_process(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    lease = _acquire(coordinator, _binding(root))
    lock_path = next((tmp_path / "host-locks").glob("*.lock"))
    probe = (
        "import fcntl, os, sys; "
        "fd = os.open(sys.argv[1], os.O_RDWR); "
        "\ntry: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"
        "\nexcept BlockingIOError: sys.exit(7)"
    )
    blocked = subprocess.run(
        [sys.executable, "-c", probe, str(lock_path)],
        check=False,
    )
    assert blocked.returncode == 7
    lease.close()
    released = subprocess.run(
        [sys.executable, "-c", probe, str(lock_path)],
        check=False,
    )
    assert released.returncode == 0


def test_lease_rejects_mount_root_path_swap(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    binding = _binding(root)
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    lease = _acquire(coordinator, binding)
    moved = tmp_path / "moved"
    root.rename(moved)
    root.mkdir()
    with pytest.raises(WorkspaceMutationError, match="path identity changed"):
        lease.revalidate_root()
    lease.close()


def test_mount_binding_uses_exact_mount_revision_and_root_inode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    data_root = tmp_path / "user-data"
    store = WorkspaceMountStore("profile-1", user_data_root=data_root)
    mounted = store.mount("workspace-1", str(root), expected_revision=0)
    store.select("workspace-1", expected_revision=int(mounted["revision"]))
    binding = capture_selected_workspace_binding(
        "profile-1",
        user_data_root=data_root,
    )
    value = root.stat()
    assert binding["mount_revision"] == 1
    assert binding["root_st_dev"] == value.st_dev
    assert binding["root_st_ino"] == value.st_ino


def test_workspace_resource_factory_hook_resolves_exact_requested_mount(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    data_root = tmp_path / "user-data"
    store = WorkspaceMountStore("profile-1", user_data_root=data_root)
    mounted = store.mount("workspace-1", str(first), expected_revision=0)
    mounted = store.mount(
        "workspace-2",
        str(second),
        expected_revision=int(mounted["revision"]),
    )
    store.select("workspace-1", expected_revision=int(mounted["revision"]))
    context = cast(
        Any,
        SimpleNamespace(
            profile_id="profile-1",
            user_data_root=data_root,
            state_root=tmp_path / "state",
        ),
    )
    resolver = WorkspaceResourceHostFactoryV4().capture_workspace_binding_resolver(context)
    binding = resolver("profile-1", "workspace-2")
    assert binding.workspace_id == "workspace-2"
    assert binding.canonical_root == second
    with pytest.raises(PermissionError, match="profile changed"):
        resolver("profile-2", "workspace-2")
    assert set(HOST_PROVIDER_FACTORY) == {
        "rumi_workspace_mount_pack.workspace-mount.resource",
        "rumi_workspace_mount_pack.workspace-mount.manage",
    }


def test_resource_binding_rejects_intermediate_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "document.txt").write_text("outside", encoding="utf-8")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "nested").symlink_to(outside, target_is_directory=True)
    table = ResourceHandleTable()
    with pytest.raises(ResourceHandleError, match="safely bound"):
        _bind_replace(table, root)


def test_resource_binding_rejects_git_control_path(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text("safe", encoding="utf-8")
    table = ResourceHandleTable()
    with pytest.raises(ResourceHandleError, match="dedicated provider"):
        table.bind_file(
            root=root,
            relative_path=".git/config",
            operations=frozenset({"read"}),
            owner=OpaqueAuthorityRef("authority:caller"),
            target=TARGET,
            context=_context(),
            target_domain_id="target-domain",
            target_boot_epoch=3,
            target_namespace="target-handles",
            ttl_seconds=30,
            max_uses=1,
            max_bytes=100,
        )


def test_cas_rejects_parent_path_identity_swap(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "document.txt").write_bytes(b"approved")
    table = ResourceHandleTable()
    handle = _bind_replace(table, root)
    nested.rename(root / "original-nested")
    nested.mkdir()
    (nested / "document.txt").write_bytes(b"attacker")
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    with _acquire(coordinator, _binding(root)) as lease:
        with pytest.raises(ResourceHandleError, match="parent identity changed"):
            _replace(table, handle, lease, b"replacement")
    assert (nested / "document.txt").read_bytes() == b"attacker"
    table.close()


def test_compare_and_replace_preserves_mode_and_exact_preimage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    path = nested / "document.txt"
    path.write_bytes(b"approved")
    path.chmod(0o640)
    table = ResourceHandleTable()
    handle = _bind_replace(table, root)
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    with _acquire(coordinator, _binding(root)) as lease:
        assert _replace(table, handle, lease, b"replacement") == len(b"replacement")
    assert path.read_bytes() == b"replacement"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    table.close()


def test_compare_and_create_and_delete_require_exact_bound_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    table = ResourceHandleTable()
    absent = _bind_absent(table, root)
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    binding = _binding(root)
    with _acquire(coordinator, binding) as lease:
        assert table.compare_and_create_file(
            absent,
            b"created",
            lease=lease,
            context=_context(),
            target=TARGET,
            domain_id="target-domain",
            boot_epoch=3,
            namespace="target-handles",
            mode=0o640,
        ) == len(b"created")
    created = nested / "created.txt"
    assert created.read_bytes() == b"created"
    assert stat.S_IMODE(created.stat().st_mode) == 0o640
    existing = table.bind_file(
        root=root,
        relative_path="nested/created.txt",
        operations=frozenset({"write"}),
        owner=OpaqueAuthorityRef("authority:caller"),
        target=TARGET,
        context=_context(),
        target_domain_id="target-domain",
        target_boot_epoch=3,
        target_namespace="target-handles",
        ttl_seconds=30,
        max_uses=1,
        max_bytes=100,
        atomic_replace=True,
    )
    with _acquire(coordinator, binding) as lease:
        table.compare_and_delete_file(
            existing,
            lease=lease,
            context=_context(),
            target=TARGET,
            domain_id="target-domain",
            boot_epoch=3,
            namespace="target-handles",
        )
    assert not created.exists()
    table.close()


def test_compare_and_create_rejects_destination_appearing_after_bind(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    table = ResourceHandleTable()
    absent = _bind_absent(table, root)
    destination = nested / "created.txt"
    destination.write_bytes(b"attacker")
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    with _acquire(coordinator, _binding(root)) as lease:
        with pytest.raises(ResourceHandleError, match="no longer absent"):
            table.compare_and_create_file(
                absent,
                b"created",
                lease=lease,
                context=_context(),
                target=TARGET,
                domain_id="target-domain",
                boot_epoch=3,
                namespace="target-handles",
            )
    assert destination.read_bytes() == b"attacker"
    table.close()


def test_two_host_cas_operations_from_same_preimage_have_one_winner(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    path = nested / "document.txt"
    path.write_bytes(b"approved")
    table = ResourceHandleTable()
    first = _bind_replace(table, root)
    second = _bind_replace(table, root)
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    binding = _binding(root)
    with _acquire(coordinator, binding) as lease:
        _replace(table, first, lease, b"first")
    with _acquire(coordinator, binding) as lease:
        with pytest.raises(ResourceHandleError, match="identity changed"):
            _replace(table, second, lease, b"second")
    assert path.read_bytes() == b"first"
    table.close()


def test_cas_rejects_content_change_even_when_size_and_mtime_match(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    path = nested / "document.txt"
    path.write_bytes(b"approved")
    original = path.stat()
    table = ResourceHandleTable()
    handle = _bind_replace(table, root)
    path.write_bytes(b"attacker")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    with _acquire(coordinator, _binding(root)) as lease:
        with pytest.raises(ResourceHandleError, match="content changed"):
            _replace(table, handle, lease, b"replacement")
    assert path.read_bytes() == b"attacker"
    table.close()


def test_host_port_exposes_only_opaque_lease_and_handles(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    nested = root / "nested"
    nested.mkdir(parents=True)
    path = nested / "document.txt"
    path.write_bytes(b"approved")
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    binding = _binding(root)
    port: WorkspaceMutationPort = HostWorkspaceMutationPort(
        coordinator,
        binding_resolver=lambda _profile_id, _workspace_id: binding,
    )
    identity = _identity()
    lease = port.acquire_lease(WorkspaceMutationLeaseRequest(identity=identity, binding=binding))
    assert not hasattr(lease, "root_fd")
    handle = port.bind_existing(
        lease,
        identity,
        relative_path="nested/document.txt",
        ttl_seconds=30,
        max_uses=1,
        max_bytes=100,
    )
    assert not hasattr(handle, "fd")
    assert port.replace_file(lease, identity, handle, b"replacement") == 11
    port.close_lease(lease, identity)
    assert path.read_bytes() == b"replacement"
    port.close()
    with pytest.raises(WorkspaceMutationError, match="port is closed"):
        port.acquire_lease(WorkspaceMutationLeaseRequest(identity=identity, binding=binding))


def test_host_port_rejects_provider_forged_workspace_binding(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    authoritative = _binding(root)
    outside_stat = outside.stat()
    forged = WorkspaceMutationBinding(
        profile_id="profile-1",
        workspace_id=authoritative.workspace_id,
        mount_revision=authoritative.mount_revision,
        canonical_root=outside,
        root_st_dev=outside_stat.st_dev,
        root_st_ino=outside_stat.st_ino,
    )
    port = HostWorkspaceMutationPort(
        WorkspaceMutationCoordinator(tmp_path / "host-locks"),
        binding_resolver=lambda _profile_id, _workspace_id: authoritative,
    )
    with pytest.raises(WorkspaceMutationError, match="not authoritative"):
        port.acquire_lease(WorkspaceMutationLeaseRequest(identity=_identity(), binding=forged))
    port.close()


def test_host_port_rejects_identity_and_cross_lease_handle_mismatch(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    first_root.mkdir()
    first_path = first_root / "document.txt"
    first_path.write_bytes(b"first")
    second_root = tmp_path / "second"
    second_root.mkdir()
    second_path = second_root / "document.txt"
    second_path.write_bytes(b"second")
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    identity = _identity()
    first_binding = _binding(first_root)
    second_binding = WorkspaceMutationBinding(
        profile_id="profile-1",
        workspace_id="workspace-2",
        mount_revision=1,
        canonical_root=second_root,
        root_st_dev=second_root.stat().st_dev,
        root_st_ino=second_root.stat().st_ino,
    )
    bindings = {
        first_binding.workspace_id: first_binding,
        second_binding.workspace_id: second_binding,
    }
    port = HostWorkspaceMutationPort(
        coordinator,
        binding_resolver=lambda _profile_id, workspace_id: bindings[workspace_id],
    )
    first_lease = port.acquire_lease(
        WorkspaceMutationLeaseRequest(identity=identity, binding=first_binding)
    )
    second_lease = port.acquire_lease(
        WorkspaceMutationLeaseRequest(identity=identity, binding=second_binding)
    )
    first_handle = port.bind_existing(
        first_lease,
        identity,
        relative_path="document.txt",
        ttl_seconds=30,
        max_uses=1,
        max_bytes=100,
    )
    with pytest.raises(WorkspaceMutationError, match="another lease"):
        port.replace_file(second_lease, identity, first_handle, b"attacker")
    mismatched = _identity(context=_context(activation_id="activation-2"))
    with pytest.raises(WorkspaceMutationError, match="identity mismatch"):
        port.replace_file(first_lease, mismatched, first_handle, b"attacker")
    assert first_path.read_bytes() == b"first"
    assert second_path.read_bytes() == b"second"
    port.close()


def test_host_port_namespace_close_releases_leases_and_handles(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "document.txt"
    path.write_bytes(b"approved")
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    binding = _binding(root)
    port = HostWorkspaceMutationPort(
        coordinator,
        binding_resolver=lambda _profile_id, _workspace_id: binding,
    )
    identity = _identity()
    request = WorkspaceMutationLeaseRequest(identity=identity, binding=binding)
    lease = port.acquire_lease(request)
    handle = port.bind_existing(
        lease,
        identity,
        relative_path="document.txt",
        ttl_seconds=30,
        max_uses=1,
        max_bytes=100,
    )
    port.close_namespace("target-handles")
    with pytest.raises(WorkspaceMutationError, match="unknown"):
        port.replace_file(lease, identity, handle, b"replacement")
    replacement_lease = port.acquire_lease(request)
    port.close_lease(replacement_lease, identity)
    port.close()


def _batch_port(
    tmp_path: Path,
    root: Path,
    *,
    fault=None,
):
    binding = _binding(root)
    coordinator = WorkspaceMutationCoordinator(tmp_path / "host-locks")
    port = HostWorkspaceMutationPort(
        coordinator,
        binding_resolver=lambda _profile_id, _workspace_id: binding,
        batch_fault_injector=fault,
    )
    identity = _identity()
    lease = port.acquire_lease(WorkspaceMutationLeaseRequest(identity=identity, binding=binding))
    return port, identity, lease, binding


def _bind_batch_handles(port, lease, identity):
    replace_handle = port.bind_existing(
        lease,
        identity,
        relative_path="replace.txt",
        ttl_seconds=30,
        max_uses=1,
        max_bytes=20 * 1024 * 1024,
    )
    create_handle = port.bind_absent(
        lease,
        identity,
        relative_path="create.txt",
        ttl_seconds=30,
        max_uses=1,
        max_bytes=20 * 1024 * 1024,
    )
    delete_handle = port.bind_existing(
        lease,
        identity,
        relative_path="delete.txt",
        ttl_seconds=30,
        max_uses=1,
        max_bytes=20 * 1024 * 1024,
    )
    return replace_handle, create_handle, delete_handle


def _batch_mutations(handles):
    replace_handle, create_handle, delete_handle = handles
    return (
        WorkspaceBatchMutation("replace", replace_handle, b"replacement"),
        WorkspaceBatchMutation("create", create_handle, b"created", 0o640),
        WorkspaceBatchMutation("delete", delete_handle),
    )


def _batch_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "replace.txt").write_bytes(b"before-replace")
    (root / "delete.txt").write_bytes(b"before-delete")
    return root


def test_host_port_publishes_replace_create_delete_as_one_batch(
    tmp_path: Path,
) -> None:
    root = _batch_root(tmp_path)
    port, identity, lease, _binding_value = _batch_port(tmp_path, root)
    handles = _bind_batch_handles(port, lease, identity)
    result = port.publish_batch(lease, identity, _batch_mutations(handles))
    assert result.status == "committed"
    assert result.mutation_count == 3
    assert (root / "replace.txt").read_bytes() == b"replacement"
    assert (root / "create.txt").read_bytes() == b"created"
    assert stat.S_IMODE((root / "create.txt").stat().st_mode) == 0o640
    assert not (root / "delete.txt").exists()
    port.close()


@pytest.mark.parametrize(
    ("phase", "index"),
    [
        ("after_journal", 0),
        ("after_stage", 1),
        ("after_stage", 2),
        ("after_stage", 3),
        ("after_prepare", 0),
        ("before_publish", 0),
        ("after_publish", 1),
        ("after_publish", 2),
        ("after_publish", 3),
    ],
)
def test_batch_faults_roll_back_all_paths(
    tmp_path: Path,
    phase: str,
    index: int,
) -> None:
    root = _batch_root(tmp_path)

    def fault(actual_phase: str, actual_index: int) -> None:
        if (actual_phase, actual_index) == (phase, index):
            raise RuntimeError("injected batch fault")

    port, identity, lease, _binding_value = _batch_port(
        tmp_path,
        root,
        fault=fault,
    )
    handles = _bind_batch_handles(port, lease, identity)
    with pytest.raises(ResourceHandleError, match="batch publication failed"):
        port.publish_batch(lease, identity, _batch_mutations(handles))
    assert (root / "replace.txt").read_bytes() == b"before-replace"
    assert not (root / "create.txt").exists()
    assert (root / "delete.txt").read_bytes() == b"before-delete"
    assert not list(root.glob(".tobkiri-batch-*"))
    port.close()


def test_batch_crash_is_recovered_before_next_lease(tmp_path: Path) -> None:
    class Crash(BaseException):
        pass

    root = _batch_root(tmp_path)

    def crash(phase: str, index: int) -> None:
        if (phase, index) == ("after_publish", 1):
            raise Crash()

    port, identity, lease, binding = _batch_port(tmp_path, root, fault=crash)
    handles = _bind_batch_handles(port, lease, identity)
    with pytest.raises(Crash):
        port.publish_batch(lease, identity, _batch_mutations(handles))
    port.close()
    restarted = HostWorkspaceMutationPort(
        WorkspaceMutationCoordinator(tmp_path / "host-locks"),
        binding_resolver=lambda _profile_id, _workspace_id: binding,
    )
    restarted_lease = restarted.acquire_lease(
        WorkspaceMutationLeaseRequest(identity=identity, binding=binding)
    )
    assert (root / "replace.txt").read_bytes() == b"before-replace"
    assert not (root / "create.txt").exists()
    assert (root / "delete.txt").read_bytes() == b"before-delete"
    restarted.close_lease(restarted_lease, identity)
    restarted.close()


def test_batch_publish_intent_consumes_handles_before_any_retry(
    tmp_path: Path,
) -> None:
    root = _batch_root(tmp_path)
    injected = False

    def fault(phase: str, index: int) -> None:
        nonlocal injected
        if not injected and (phase, index) == ("before_publish", 0):
            injected = True
            raise RuntimeError("injected publish-intent fault")

    port, identity, lease, _binding_value = _batch_port(
        tmp_path,
        root,
        fault=fault,
    )
    handles = _bind_batch_handles(port, lease, identity)
    mutations = _batch_mutations(handles)
    with pytest.raises(ResourceHandleError, match="batch publication failed"):
        port.publish_batch(lease, identity, mutations)
    with pytest.raises(ResourceHandleError, match="quota exceeded"):
        port.publish_batch(lease, identity, mutations)
    assert (root / "replace.txt").read_bytes() == b"before-replace"
    assert not (root / "create.txt").exists()
    assert (root / "delete.txt").read_bytes() == b"before-delete"
    port.close()


def test_batch_crash_after_commit_recovers_committed_outcome(tmp_path: Path) -> None:
    class Crash(BaseException):
        pass

    root = _batch_root(tmp_path)

    def crash(phase: str, index: int) -> None:
        if (phase, index) == ("after_commit", 3):
            raise Crash()

    port, identity, lease, binding = _batch_port(tmp_path, root, fault=crash)
    handles = _bind_batch_handles(port, lease, identity)
    with pytest.raises(Crash):
        port.publish_batch(lease, identity, _batch_mutations(handles))
    port.close()
    restarted = HostWorkspaceMutationPort(
        WorkspaceMutationCoordinator(tmp_path / "host-locks"),
        binding_resolver=lambda _profile_id, _workspace_id: binding,
    )
    restarted_lease = restarted.acquire_lease(
        WorkspaceMutationLeaseRequest(identity=identity, binding=binding)
    )
    assert (root / "replace.txt").read_bytes() == b"replacement"
    assert (root / "create.txt").read_bytes() == b"created"
    assert not (root / "delete.txt").exists()
    restarted.close_lease(restarted_lease, identity)
    restarted.close()


def test_batch_cleanup_failure_returns_durable_committed_outcome(
    tmp_path: Path,
) -> None:
    root = _batch_root(tmp_path)

    def fault(phase: str, index: int) -> None:
        if (phase, index) == ("after_commit", 3):
            raise RuntimeError("injected cleanup fault")

    port, identity, lease, _binding_value = _batch_port(
        tmp_path,
        root,
        fault=fault,
    )
    handles = _bind_batch_handles(port, lease, identity)
    result = port.publish_batch(lease, identity, _batch_mutations(handles))
    assert result.status == "committed"
    assert (root / "replace.txt").read_bytes() == b"replacement"
    assert (root / "create.txt").read_bytes() == b"created"
    assert not (root / "delete.txt").exists()
    port.close()


def test_batch_stale_preimage_and_limits_fail_before_target_write(
    tmp_path: Path,
) -> None:
    root = _batch_root(tmp_path)
    port, identity, lease, _binding_value = _batch_port(tmp_path, root)
    handles = _bind_batch_handles(port, lease, identity)
    (root / "replace.txt").write_bytes(b"external-change")
    with pytest.raises(ResourceHandleError, match="generation changed"):
        port.publish_batch(lease, identity, _batch_mutations(handles))
    assert not (root / "create.txt").exists()
    assert (root / "delete.txt").read_bytes() == b"before-delete"
    with pytest.raises(WorkspaceMutationError, match="count limit"):
        port.publish_batch(
            lease,
            identity,
            tuple(WorkspaceBatchMutation("delete", handles[2]) for _ in range(65)),
        )
    with pytest.raises(ResourceHandleError, match="byte limit"):
        port.publish_batch(
            lease,
            identity,
            (
                WorkspaceBatchMutation(
                    "replace",
                    handles[0],
                    b"x" * (16 * 1024 * 1024 + 1),
                ),
            ),
        )
    port.close()


def test_batch_rejects_duplicate_and_cross_lease_handles(tmp_path: Path) -> None:
    root = _batch_root(tmp_path)
    port, identity, lease, _binding_value = _batch_port(tmp_path, root)
    handles = _bind_batch_handles(port, lease, identity)
    duplicate = (
        WorkspaceBatchMutation("replace", handles[0], b"one"),
        WorkspaceBatchMutation("replace", handles[0], b"two"),
    )
    with pytest.raises(WorkspaceMutationError, match="duplicated"):
        port.publish_batch(lease, identity, duplicate)
    fake = type(handles[0])("not-owned-by-lease")
    with pytest.raises(WorkspaceMutationError, match="another lease"):
        port.publish_batch(
            lease,
            identity,
            (WorkspaceBatchMutation("delete", fake),),
        )
    port.close()
