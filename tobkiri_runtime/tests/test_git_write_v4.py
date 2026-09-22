from __future__ import annotations

from dataclasses import replace
import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from ecosystem.rumi_git_write_pack.runtime.write import (
    APPLY_PATCH_FUNCTION_ID,
    APPLY_PATCH_OPERATION,
    APPLY_PATCH_PREPARE_FUNCTION_ID,
    APPLY_PATCH_PREPARE_OPERATION,
    COMMIT_FUNCTION_ID,
    COMMIT_OPERATION,
    COMMIT_PREPARE_FUNCTION_ID,
    COMMIT_PREPARE_OPERATION,
    CONTRACT_ID,
    GIT_READ,
    HOST_PROVIDER_FACTORY,
    RESTORE_FUNCTION_ID,
    RESTORE_OPERATION,
    RESTORE_PREPARE_FUNCTION_ID,
    RESTORE_PREPARE_OPERATION,
    WORKSPACE,
    WORKSPACE_GET_OPERATION,
    GitWriteV4Service,
)
from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import WorkspaceMutationIdentity
from tobkiri_host.workspace_mutation import (
    HostWorkspaceMutationPort,
    WorkspaceMutationBinding,
    WorkspaceMutationCoordinator,
    WorkspaceMutationError,
)


TARGET = OpaqueAuthorityRef("authority:git-write")


class _GitClient:
    def __init__(self, root: Path, *, profile_id: str = "profile") -> None:
        self.root = root
        self.profile_id = profile_id
        self.calls: list[tuple[str, str]] = []
        self.payloads: list[dict[str, Any]] = []

    def invoke(
        self,
        contract_id: str,
        operation: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls.append((contract_id, operation))
        self.payloads.append(dict(payload))
        assert payload["profile_id"] == self.profile_id
        assert payload["workspace_id"] == "workspace"
        if (contract_id, operation) == (WORKSPACE, WORKSPACE_GET_OPERATION):
            assert payload["operation"] == "get"
            return {"root_path": str(self.root), "mount_revision": 7}
        raise AssertionError(f"unexpected dependency: {contract_id}.{operation}")


def _git(repository: Path, *args: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Tobkiri Test")
    _git(repository, "config", "user.email", "test@tobkiri.invalid")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-qm", "initial")
    return repository


def _request(**values: Any) -> dict[str, Any]:
    return {"profile_id": "profile", "workspace_id": "workspace", **values}


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _identity(**context_changes: object) -> WorkspaceMutationIdentity:
    context = RequestContext(
        request_id="git-write-request",
        trace_id="git-write-trace",
        caller_principal=OpaqueAuthorityRef("authority:caller"),
        profile_id="profile",
        activation_id="activation",
        activation_digest=_digest("activation"),
        plan_digest=_digest("plan"),
        security_epoch=3,
        caller_session_id="session",
        caller_domain_id="caller-domain",
        caller_boot_epoch=1,
        target_domain_id="git-write-domain",
        target_boot_epoch=2,
        target_backend_digest=_digest("backend"),
        profile_authority_digest=_digest("authority"),
        fencing_token=1,
        handle_namespace="git-write-handles",
    )
    context = replace(context, **context_changes)
    return WorkspaceMutationIdentity(
        context=context,
        target_principal=TARGET,
        target_domain_id=context.target_domain_id,
        target_boot_epoch=context.target_boot_epoch,
        target_namespace=context.handle_namespace,
    )


def _mutation_service(
    repository: Path,
    state_root: Path,
    *,
    identity: WorkspaceMutationIdentity | None = None,
) -> tuple[GitWriteV4Service, HostWorkspaceMutationPort]:
    metadata = repository.stat()
    binding = WorkspaceMutationBinding(
        profile_id="profile",
        workspace_id="workspace",
        mount_revision=7,
        canonical_root=repository,
        root_st_dev=metadata.st_dev,
        root_st_ino=metadata.st_ino,
    )
    port = HostWorkspaceMutationPort(
        WorkspaceMutationCoordinator(state_root),
        binding_resolver=lambda _profile, _workspace: binding,
    )
    service = GitWriteV4Service(
        _GitClient(repository),
        workspace_mutation_port=port,
        workspace_mutation_identity=identity or _identity(),
    )
    return service, port


def test_v4_commit_uses_exact_staged_index_without_authority_receipt(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = repository / "tracked.txt"
    path.write_text("staged\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "update-index", "--assume-unchanged", "tracked.txt")
    index = Path(_git(repository, "rev-parse", "--git-path", "index").strip())
    if not index.is_absolute():
        index = repository / index
    service = GitWriteV4Service(_GitClient(repository))

    plan = service.invoke(
        "git-commit-prepare",
        _request(message="prepared commit"),
    )
    index_bytes = index.read_bytes()
    path.write_text("unstaged after prepare\n", encoding="utf-8")
    result = service.invoke("git-commit", _request(plan=plan))

    assert result["commit_hash"] == _git(repository, "rev-parse", "HEAD").strip()
    assert _git(repository, "show", "HEAD:tracked.txt") == "staged\n"
    assert index.read_bytes() == index_bytes
    assert all(
        contract_id != "rumi.service.host.authorize.v1" for contract_id, _ in service.client.calls
    )


def test_v4_commit_rejects_index_change_after_prepare(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    path = repository / "tracked.txt"
    path.write_text("first\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    service = GitWriteV4Service(_GitClient(repository))
    plan = service.invoke("git-commit-prepare", _request(message="first"))
    original_head = _git(repository, "rev-parse", "HEAD").strip()
    path.write_text("second\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")

    with pytest.raises(PermissionError, match="index (bytes|flags) changed"):
        service.invoke("git-commit", _request(plan=plan))

    assert _git(repository, "rev-parse", "HEAD").strip() == original_head


def test_v4_execute_rejects_forged_prepared_plan(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    path = repository / "tracked.txt"
    path.write_text("staged\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    service = GitWriteV4Service(_GitClient(repository))
    plan = service.invoke("git-commit-prepare", _request(message="approved"))
    forged = {**plan, "message": "forged"}

    with pytest.raises(PermissionError, match="plan digest"):
        service.invoke("git-commit", _request(plan=forged))

    assert _git(repository, "log", "-1", "--format=%s").strip() == "initial"


@pytest.mark.parametrize(
    "configuration",
    [
        ("config", "core.sparseCheckout", "true"),
        ("update-index", "--split-index"),
    ],
)
def test_v4_commit_rejects_non_self_contained_index(
    tmp_path: Path,
    configuration: tuple[str, ...],
) -> None:
    repository = _repository(tmp_path)
    (repository / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, *configuration)

    with pytest.raises(PermissionError, match="sparse|split"):
        GitWriteV4Service(_GitClient(repository)).invoke(
            "git-commit-prepare",
            _request(message="unsupported"),
        )


def test_restore_execute_publishes_through_workspace_descriptor_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    unchanged = repository / "unchanged.txt"
    unchanged.write_text("unchanged\n", encoding="utf-8")
    _git(repository, "add", "unchanged.txt")
    _git(repository, "commit", "-qm", "add unchanged target")
    path = repository / "tracked.txt"
    path.write_text("dirty\n", encoding="utf-8")
    service, port = _mutation_service(repository, tmp_path / "state")
    bound_paths: list[str] = []
    published_batch_sizes: list[int] = []
    bind_existing = port.bind_existing
    publish_batch = port.publish_batch

    def record_bind(*args: Any, **kwargs: Any) -> Any:
        bound_paths.append(str(kwargs["relative_path"]))
        return bind_existing(*args, **kwargs)

    def record_publish(*args: Any, **kwargs: Any) -> Any:
        mutations = args[2] if len(args) > 2 else kwargs["mutations"]
        published_batch_sizes.append(len(mutations))
        return publish_batch(*args, **kwargs)

    monkeypatch.setattr(port, "bind_existing", record_bind)
    monkeypatch.setattr(port, "publish_batch", record_publish)
    plan = service.invoke(
        "git-restore-prepare",
        _request(paths=["tracked.txt", "unchanged.txt"], source="HEAD"),
    )
    assert plan["targets"][0]["path"] == "tracked.txt"
    assert plan["targets"][0]["blob_oid"]

    result = service.invoke("git-restore", _request(plan=plan))
    assert result["workspace_published"] is True
    assert result["publication_protocol"] == "host-batch-journal-v1"
    assert result["external_reader_snapshot_isolation"] is False
    assert path.read_text(encoding="utf-8") == "base\n"
    assert bound_paths == ["tracked.txt", "unchanged.txt"]
    assert published_batch_sizes == [1]
    port.close()


def test_restore_execute_rejects_stale_preimage_without_publication(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = repository / "tracked.txt"
    path.write_text("dirty\n", encoding="utf-8")
    service, port = _mutation_service(repository, tmp_path / "state")
    plan = service.invoke(
        "git-restore-prepare",
        _request(paths=["tracked.txt"], source="HEAD"),
    )

    path.write_text("concurrent\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="changed after prepare"):
        service.invoke("git-restore", _request(plan=plan))
    assert path.read_text(encoding="utf-8") == "concurrent\n"
    port.close()


def test_restore_execute_rejects_cross_identity_lease(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    path = repository / "tracked.txt"
    path.write_text("dirty\n", encoding="utf-8")
    service, port = _mutation_service(
        repository,
        tmp_path / "state",
        identity=_identity(profile_id="another-profile"),
    )
    plan = service.invoke(
        "git-restore-prepare",
        _request(paths=["tracked.txt"], source="HEAD"),
    )

    with pytest.raises(WorkspaceMutationError, match="profile binding mismatch"):
        service.invoke("git-restore", _request(plan=plan))
    assert path.read_text(encoding="utf-8") == "dirty\n"
    port.close()


def test_restore_prepare_rejects_symlink_preimage_and_postimage(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = repository / "tracked.txt"
    path.unlink()
    path.symlink_to("target.txt")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-qm", "symlink")

    with pytest.raises(PermissionError, match="symbolic links"):
        GitWriteV4Service(_GitClient(repository)).invoke(
            "git-restore-prepare",
            _request(paths=["tracked.txt"], source="HEAD"),
        )


def test_patch_execute_materializes_off_tree_and_publishes_through_cas(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = repository / "tracked.txt"
    path.write_text("patched\n", encoding="utf-8")
    patch = _git(repository, "diff", "--", "tracked.txt")
    path.write_text("base\n", encoding="utf-8")
    service, port = _mutation_service(repository, tmp_path / "state")

    plan = service.invoke("git-apply-patch-prepare", _request(patch=patch))
    assert plan["paths"] == ["tracked.txt"]
    result = service.invoke("git-apply-patch", _request(plan=plan, patch=patch))
    assert result["workspace_published"] is True
    assert path.read_text(encoding="utf-8") == "patched\n"

    with pytest.raises(PermissionError, match="patch bytes changed"):
        service.invoke("git-apply-patch", _request(plan=plan, patch=patch + "\n"))
    port.close()


def test_patch_prepare_binds_create_delete_and_rename_preimages(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    tracked = repository / "tracked.txt"
    renamed = repository / "renamed.txt"
    tracked.rename(renamed)
    _git(repository, "add", "-A")
    patch = _git(repository, "diff", "--cached", "--binary")
    _git(repository, "reset", "--hard", "-q", "HEAD")

    service, port = _mutation_service(repository, tmp_path / "state")
    rename_plan = service.invoke(
        "git-apply-patch-prepare",
        _request(patch=patch),
    )
    assert rename_plan["paths"] == ["tracked.txt", "renamed.txt"]
    rename_result = service.invoke(
        "git-apply-patch",
        _request(plan=rename_plan, patch=patch),
    )
    assert rename_result["paths"] == ["tracked.txt", "renamed.txt"]
    assert rename_result["publication_protocol"] == "host-batch-journal-v1"
    assert rename_result["external_reader_snapshot_isolation"] is False
    assert renamed.read_text(encoding="utf-8") == "base\n"
    assert not tracked.exists()
    port.close()

    _git(repository, "reset", "--hard", "-q", "HEAD")

    tracked.unlink()
    delete_patch = _git(repository, "diff", "--", "tracked.txt")
    tracked.write_text("base\n", encoding="utf-8")
    delete_plan = GitWriteV4Service(_GitClient(repository)).invoke(
        "git-apply-patch-prepare",
        _request(patch=delete_patch),
    )
    assert delete_plan["paths"] == ["tracked.txt"]
    assert delete_plan["preimages"][0]["kind"] == "file"

    created = repository / "created.txt"
    created.write_text("created\n", encoding="utf-8")
    _git(repository, "add", "created.txt")
    create_patch = _git(repository, "diff", "--cached", "--binary")
    _git(repository, "reset", "-q", "HEAD", "--", "created.txt")
    created.unlink()
    create_plan = GitWriteV4Service(_GitClient(repository)).invoke(
        "git-apply-patch-prepare",
        _request(patch=create_patch),
    )
    assert create_plan["paths"] == ["created.txt"]
    assert create_plan["preimages"][0]["kind"] == "absent"


def test_restore_reports_committed_when_lease_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    path = repository / "tracked.txt"
    path.write_text("dirty\n", encoding="utf-8")
    service, port = _mutation_service(repository, tmp_path / "state")
    plan = service.invoke(
        "git-restore-prepare",
        _request(paths=["tracked.txt"], source="HEAD"),
    )

    def fail_close(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("injected cleanup failure")

    monkeypatch.setattr(port, "close_lease", fail_close)
    result = service.invoke("git-restore", _request(plan=plan))

    assert path.read_text(encoding="utf-8") == "base\n"
    assert result["workspace_published"] is True
    assert result["workspace_cleanup"] == "lease-close-failed"
    assert result["workspace_transaction_id"]
    port.close()


def test_restore_rejects_malformed_committed_batch_result_as_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    path = repository / "tracked.txt"
    path.write_text("dirty\n", encoding="utf-8")
    service, port = _mutation_service(repository, tmp_path / "state")
    plan = service.invoke(
        "git-restore-prepare",
        _request(paths=["tracked.txt"], source="HEAD"),
    )
    publish_batch = port.publish_batch

    def malformed_result(*args: Any, **kwargs: Any) -> None:
        publish_batch(*args, **kwargs)
        return None

    monkeypatch.setattr(port, "publish_batch", malformed_result)
    with pytest.raises(RuntimeError, match="WORKSPACE_BATCH_RESULT_AMBIGUOUS"):
        service.invoke("git-restore", _request(plan=plan))

    assert path.read_text(encoding="utf-8") == "base\n"
    port.close()


def test_restore_prepare_rejects_more_than_host_batch_action_limit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    paths = []
    for index in range(65):
        path = repository / f"file-{index:02d}.txt"
        path.write_text("base\n", encoding="utf-8")
        paths.append(path.name)
    _git(repository, "add", "--", *paths)
    _git(repository, "commit", "-qm", "add batch files")
    for name in paths:
        (repository / name).write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Host batch mutation limit"):
        GitWriteV4Service(_GitClient(repository)).invoke(
            "git-restore-prepare",
            _request(paths=paths, source="HEAD"),
        )


def test_restore_prepare_rejects_more_than_host_batch_byte_limit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = repository / "large.bin"
    path.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
    _git(repository, "add", "large.bin")
    _git(repository, "commit", "-qm", "add large blob")
    path.write_bytes(b"dirty")

    with pytest.raises(ValueError, match="Host batch byte limit"):
        GitWriteV4Service(_GitClient(repository)).invoke(
            "git-restore-prepare",
            _request(paths=["large.bin"], source="HEAD"),
        )


def test_patch_create_and_delete_publish_with_exact_absent_and_file_cas(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    created = repository / "created.txt"
    created.write_text("created\n", encoding="utf-8")
    _git(repository, "add", "created.txt")
    create_patch = _git(repository, "diff", "--cached", "--binary")
    _git(repository, "reset", "-q", "HEAD", "--", "created.txt")
    created.unlink()
    service, port = _mutation_service(repository, tmp_path / "state")

    create_plan = service.invoke(
        "git-apply-patch-prepare",
        _request(patch=create_patch),
    )
    service.invoke(
        "git-apply-patch",
        _request(plan=create_plan, patch=create_patch),
    )
    assert created.read_text(encoding="utf-8") == "created\n"

    _git(repository, "add", "created.txt")
    _git(repository, "commit", "-qm", "created")
    created.unlink()
    delete_patch = _git(repository, "diff", "--", "created.txt")
    created.write_text("created\n", encoding="utf-8")
    delete_plan = service.invoke(
        "git-apply-patch-prepare",
        _request(patch=delete_patch),
    )
    service.invoke(
        "git-apply-patch",
        _request(plan=delete_plan, patch=delete_patch),
    )
    assert not created.exists()
    port.close()


@pytest.mark.parametrize(
    "path",
    ["../escape", ".git/config", ".GIT/config", ".env", "config/credentials/token"],
)
def test_patch_prepare_rejects_unsafe_affected_paths(
    tmp_path: Path,
    path: str,
) -> None:
    repository = _repository(tmp_path)
    patch = f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+secret\n"

    with pytest.raises(PermissionError):
        GitWriteV4Service(_GitClient(repository)).invoke(
            "git-apply-patch-prepare",
            _request(patch=patch),
        )


def _provider_binding(
    *,
    function_id: str,
    operation_id: str,
    index: int,
) -> Any:
    return SimpleNamespace(
        operation=SimpleNamespace(
            contract_id=CONTRACT_ID,
            contract_version="1.0.0",
            operation_id=operation_id,
        ),
        principal_ref=OpaqueAuthorityRef(f"authority:git-write-{index}"),
        artifact=SimpleNamespace(digest=_digest(f"artifact-{index}")),
        function=SimpleNamespace(
            function_id=function_id,
            implementation_digest=_digest(f"implementation-{index}"),
        ),
    )


def _capture_context(
    *,
    tmp_path: Path,
    bindings: tuple[Any, ...],
    domains: Mapping[tuple[str, str, str], str],
) -> HostProviderCaptureContextV4:
    return HostProviderCaptureContextV4(
        profile_id="profile",
        plan_digest=_digest("git-write-host-provider-plan"),
        security_epoch=2,
        activation={"activation_id": "activation.git-write-host-provider"},
        state_root=tmp_path,
        provider_bindings=bindings,
        catalog_bindings=(),
        domain_ids=domains,
    )


class _CapturedInvocation:
    """Minimal authenticated V4 invocation used at the Host Provider edge."""

    def __init__(
        self,
        *,
        client: _GitClient,
        principal: OpaqueAuthorityRef,
        domain_id: str,
        profile_id: str,
    ) -> None:
        context = _identity(
            profile_id=profile_id,
            target_domain_id=domain_id,
        ).context
        self.envelope = SimpleNamespace(
            target_principal=principal,
            context=context,
        )
        self._client = client
        self.contract_client_calls: list[tuple[frozenset[str], str]] = []

    def contract_client(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
    ) -> _GitClient:
        self.contract_client_calls.append((allowed_contract_ids, consumer_pack_id))
        return self._client


def _captured_contribution(
    *,
    tmp_path: Path,
    function_id: str,
    operation_id: str,
) -> tuple[Any, Any, str]:
    """Capture one factory contribution with a unique authenticated domain."""

    binding = _provider_binding(
        function_id=function_id,
        operation_id=operation_id,
        index=1,
    )
    domain_id = "git-write-host-provider-domain"
    captured = HOST_PROVIDER_FACTORY[function_id].capture(
        _capture_context(
            tmp_path=tmp_path,
            bindings=(binding,),
            domains={
                (
                    binding.operation.contract_id,
                    binding.operation.operation_id,
                    binding.principal_ref.value,
                ): domain_id,
            },
        )
    )
    return captured, binding, domain_id


def test_v4_prepare_injects_nondefault_profile_from_host_context(
    tmp_path: Path,
) -> None:
    """A prepare cannot select a Profile outside its authenticated envelope."""

    repository = _repository(tmp_path)
    (repository / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    captured, binding, domain_id = _captured_contribution(
        tmp_path=tmp_path,
        function_id=COMMIT_PREPARE_FUNCTION_ID,
        operation_id=COMMIT_PREPARE_OPERATION,
    )
    invocation = _CapturedInvocation(
        client=_GitClient(repository, profile_id="non-default-profile"),
        principal=binding.principal_ref,
        domain_id=domain_id,
        profile_id="non-default-profile",
    )
    try:
        plan = captured.contributions[0].invoke(
            COMMIT_PREPARE_OPERATION,
            {"message": "non-default profile", "workspace_id": "workspace"},
            invocation,
        )
    finally:
        captured.close()

    assert plan["profile_id"] == "non-default-profile"
    assert invocation.contract_client_calls == [
        (frozenset({WORKSPACE}), "rumi_git_write_pack")
    ]


@pytest.mark.parametrize(
    ("function_id", "operation_id"),
    (
        (COMMIT_PREPARE_FUNCTION_ID, COMMIT_PREPARE_OPERATION),
        (RESTORE_PREPARE_FUNCTION_ID, RESTORE_PREPARE_OPERATION),
        (APPLY_PATCH_PREPARE_FUNCTION_ID, APPLY_PATCH_PREPARE_OPERATION),
    ),
)
def test_v4_prepare_rejects_client_profile_smuggling(
    tmp_path: Path,
    function_id: str,
    operation_id: str,
) -> None:
    """Prepare rejects a client Profile instead of silently overwriting it."""

    repository = _repository(tmp_path)
    captured, binding, domain_id = _captured_contribution(
        tmp_path=tmp_path,
        function_id=function_id,
        operation_id=operation_id,
    )
    client = _GitClient(repository)
    invocation = _CapturedInvocation(
        client=client,
        principal=binding.principal_ref,
        domain_id=domain_id,
        profile_id="profile",
    )
    try:
        with pytest.raises(PermissionError, match="prepare profile"):
            captured.contributions[0].invoke(
                operation_id,
                {
                    "message": "smuggled profile",
                    "profile_id": "another-profile",
                    "workspace_id": "workspace",
                },
                invocation,
            )
    finally:
        captured.close()

    assert not invocation.contract_client_calls
    assert not client.calls


@pytest.mark.parametrize("field", ("approved", "token", "authority_token"))
def test_v4_prepare_rejects_client_authority_claims(
    tmp_path: Path,
    field: str,
) -> None:
    """V4 client approval and token claims never reach Git preparation."""

    repository = _repository(tmp_path)
    captured, binding, domain_id = _captured_contribution(
        tmp_path=tmp_path,
        function_id=COMMIT_PREPARE_FUNCTION_ID,
        operation_id=COMMIT_PREPARE_OPERATION,
    )
    client = _GitClient(repository)
    invocation = _CapturedInvocation(
        client=client,
        principal=binding.principal_ref,
        domain_id=domain_id,
        profile_id="profile",
    )
    try:
        with pytest.raises(PermissionError, match="authority fields"):
            captured.contributions[0].invoke(
                COMMIT_PREPARE_OPERATION,
                {"message": "untrusted authority", "workspace_id": "workspace", field: True},
                invocation,
            )
    finally:
        captured.close()

    assert not invocation.contract_client_calls
    assert not client.calls


def test_v4_execute_rejects_sealed_profile_different_from_host_context(
    tmp_path: Path,
) -> None:
    """A coordinator execute cannot redeem a plan under another Profile."""

    repository = _repository(tmp_path)
    (repository / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    plan = GitWriteV4Service(_GitClient(repository)).invoke(
        "git-commit-prepare",
        _request(message="profile-bound plan"),
    )
    captured, binding, domain_id = _captured_contribution(
        tmp_path=tmp_path,
        function_id=COMMIT_FUNCTION_ID,
        operation_id=COMMIT_OPERATION,
    )
    client = _GitClient(repository, profile_id="other-profile")
    invocation = _CapturedInvocation(
        client=client,
        principal=binding.principal_ref,
        domain_id=domain_id,
        profile_id="other-profile",
    )
    try:
        with pytest.raises(PermissionError, match="execute profile binding"):
            captured.contributions[0].invoke(
                COMMIT_OPERATION,
                {
                    "plan": plan,
                    "profile_id": "profile",
                    "workspace_id": "workspace",
                },
                invocation,
            )
    finally:
        captured.close()

    assert not invocation.contract_client_calls
    assert not client.calls


def test_v4_factories_each_capture_one_exact_operation_principal(
    tmp_path: Path,
) -> None:
    """Every Git write operation has one unambiguous Function principal."""

    identities = (
        (COMMIT_PREPARE_FUNCTION_ID, COMMIT_PREPARE_OPERATION),
        (COMMIT_FUNCTION_ID, COMMIT_OPERATION),
        (RESTORE_PREPARE_FUNCTION_ID, RESTORE_PREPARE_OPERATION),
        (RESTORE_FUNCTION_ID, RESTORE_OPERATION),
        (APPLY_PATCH_PREPARE_FUNCTION_ID, APPLY_PATCH_PREPARE_OPERATION),
        (APPLY_PATCH_FUNCTION_ID, APPLY_PATCH_OPERATION),
    )
    bindings = tuple(
        _provider_binding(
            function_id=function_id,
            operation_id=operation_id,
            index=index,
        )
        for index, (function_id, operation_id) in enumerate(identities, start=1)
    )
    domains = {
        (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        ): f"git-write-domain-{index}"
        for index, binding in enumerate(bindings, start=1)
    }

    assert set(HOST_PROVIDER_FACTORY) == {function_id for function_id, _ in identities}
    contributions = []
    for binding, (function_id, operation_id) in zip(
        bindings,
        identities,
        strict=True,
    ):
        captured = HOST_PROVIDER_FACTORY[function_id].capture(
            _capture_context(
                tmp_path=tmp_path,
                bindings=(binding,),
                domains=domains,
            )
        )
        assert len(captured.contributions) == 1
        contribution = captured.contributions[0]
        assert contribution.operation_id == operation_id
        assert contribution.principal_id == binding.principal_ref.value
        contributions.append(contribution)
        captured.close()

    assert len({item.principal_id for item in contributions}) == len(identities)
    assert {item.operation_id for item in contributions} == {
        operation_id for _, operation_id in identities
    }


def test_v4_factories_reject_mixed_or_extra_operation_bindings(tmp_path: Path) -> None:
    """A Function cannot be captured for another or multiple operations."""

    commit_prepare = _provider_binding(
        function_id=COMMIT_PREPARE_FUNCTION_ID,
        operation_id=COMMIT_PREPARE_OPERATION,
        index=1,
    )
    commit = _provider_binding(
        function_id=COMMIT_FUNCTION_ID,
        operation_id=COMMIT_OPERATION,
        index=2,
    )
    domains = {
        (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        ): f"git-write-domain-{index}"
        for index, binding in enumerate((commit_prepare, commit), start=1)
    }
    factory = HOST_PROVIDER_FACTORY[COMMIT_PREPARE_FUNCTION_ID]

    with pytest.raises(PermissionError, match="bindings are incomplete"):
        factory.capture(
            _capture_context(
                tmp_path=tmp_path,
                bindings=(commit_prepare, commit),
                domains=domains,
            )
        )
    with pytest.raises(PermissionError, match="bindings are incomplete"):
        factory.capture(
            _capture_context(
                tmp_path=tmp_path,
                bindings=(commit,),
                domains=domains,
            )
        )


def test_v4_factory_rejects_another_operation_at_dispatch(tmp_path: Path) -> None:
    """Captured principals cannot be reused for a sibling Git write operation."""

    binding = _provider_binding(
        function_id=COMMIT_PREPARE_FUNCTION_ID,
        operation_id=COMMIT_PREPARE_OPERATION,
        index=1,
    )
    domain_id = "git-write-domain-1"
    captured = HOST_PROVIDER_FACTORY[COMMIT_PREPARE_FUNCTION_ID].capture(
        _capture_context(
            tmp_path=tmp_path,
            bindings=(binding,),
            domains={
                (
                    binding.operation.contract_id,
                    binding.operation.operation_id,
                    binding.principal_ref.value,
                ): domain_id,
            },
        )
    )
    invocation = SimpleNamespace(
        envelope=SimpleNamespace(
            target_principal=binding.principal_ref,
            context=SimpleNamespace(target_domain_id=domain_id),
        )
    )

    with pytest.raises(PermissionError, match="invocation binding changed"):
        captured.contributions[0].invoke(COMMIT_OPERATION, {}, invocation)
    captured.close()


def test_v4_factory_capture_uses_canonical_contracts() -> None:
    assert CONTRACT_ID == "tobkiri.service.git.write.v1"
    assert WORKSPACE == "tobkiri.resource.workspace.v1"
    assert GIT_READ == "tobkiri.service.git.read.v1"
