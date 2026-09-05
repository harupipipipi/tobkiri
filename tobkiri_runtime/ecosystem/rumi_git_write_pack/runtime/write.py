"""Host-dispatched local Git mutation with exact precondition binding."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_host.ports import (
    WORKSPACE_BATCH_MAX_BYTES,
    WORKSPACE_BATCH_MAX_MUTATIONS,
    WorkspaceBatchMutation,
    WorkspaceBatchResult,
    WorkspaceMutationIdentity,
    WorkspaceMutationLeaseRequest,
    WorkspaceMutationPort,
)
from tobkiri_host.workspace_mutation import WorkspaceMutationBinding
from tobkiri_protocol.canonical import canonical_digest

AUTHORITY = "rumi.service.host.authorize.v1"
GIT_READ = "tobkiri.service.git.read.v1"
WORKSPACE = "tobkiri.resource.workspace.v1"
WORKSPACE_GET_OPERATION = "rumi_workspace_mount_pack.workspace-resource"
SERVICE_PACK_ID = "rumi_git_write_pack"
_RESTRICTED_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
    ".npmrc",
    ".pypirc",
}
_MAX_STAGE_BYTES = 64 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_SNAPSHOT_PATHS = 4_096
_MAX_SNAPSHOT_STATUS_BYTES = 8 * 1024 * 1024
_MAX_SNAPSHOT_PATH_LIST_BYTES = 8 * 1024 * 1024
_MAX_PATCH_BYTES = 8 * 1024 * 1024
CONTRACT_ID = "tobkiri.service.git.write.v1"
_V4_OPERATION_PREFIX = "rumi_git_write_pack."
COMMIT_PREPARE_OPERATION = "rumi_git_write_pack.git-commit-prepare"
COMMIT_OPERATION = "rumi_git_write_pack.git-commit"
RESTORE_PREPARE_OPERATION = "rumi_git_write_pack.git-restore-prepare"
RESTORE_OPERATION = "rumi_git_write_pack.git-restore"
APPLY_PATCH_PREPARE_OPERATION = "rumi_git_write_pack.git-apply-patch-prepare"
APPLY_PATCH_OPERATION = "rumi_git_write_pack.git-apply-patch"
COMMIT_PREPARE_FUNCTION_ID = "rumi_git_write_pack.git-commit-prepare.service"
COMMIT_FUNCTION_ID = "rumi_git_write_pack.git-commit.service"
RESTORE_PREPARE_FUNCTION_ID = "rumi_git_write_pack.git-restore-prepare.service"
RESTORE_FUNCTION_ID = "rumi_git_write_pack.git-restore.service"
APPLY_PATCH_PREPARE_FUNCTION_ID = "rumi_git_write_pack.git-apply-patch-prepare.service"
APPLY_PATCH_FUNCTION_ID = "rumi_git_write_pack.git-apply-patch.service"
_V4_OPERATIONS = frozenset(
    operation.removeprefix(_V4_OPERATION_PREFIX)
    for operation in (
        COMMIT_PREPARE_OPERATION,
        COMMIT_OPERATION,
        RESTORE_PREPARE_OPERATION,
        RESTORE_OPERATION,
        APPLY_PATCH_PREPARE_OPERATION,
        APPLY_PATCH_OPERATION,
    )
)
_V4_PREPARE_OPERATION_IDS = frozenset(
    {
        COMMIT_PREPARE_OPERATION,
        RESTORE_PREPARE_OPERATION,
        APPLY_PATCH_PREPARE_OPERATION,
    }
)
_V4_EXECUTE_OPERATION_IDS = frozenset(
    {
        COMMIT_OPERATION,
        RESTORE_OPERATION,
        APPLY_PATCH_OPERATION,
    }
)
# The V4 provider resolves the repository under the Host-owned workspace
# mount itself.  Git read remains a legacy V3 dependency only; giving a V4
# provider that unused contract would widen its captured authority without a
# corresponding dispatch edge.
_V4_DEPENDENCIES = frozenset({WORKSPACE})
_UNTRUSTED_AUTHORITY_FIELDS = frozenset(
    {
        "approved",
        "approval",
        "approval_id",
        "approval_token",
        "authority_receipt",
        "authority_token",
        "backend",
        "backend_id",
        "client_token",
        "domain",
        "domain_id",
        "grant",
        "grant_id",
        "principal",
        "principal_id",
        "provider",
        "provider_id",
        "receipt",
        "scope",
        "target",
        "target_principal",
        "token",
    }
)
_WORKSPACE_MUTATION_LOCK = threading.RLock()


class GitWriteService:
    """Apply finite local Git mutations after exact receipt redemption."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Apply stage, commit, branch, or restore without network access."""
        if name not in {"stage", "commit", "branch_create", "branch_switch", "restore"}:
            raise ValueError(f"unknown Git write operation: {name}")
        if name in {"restore", "branch_create", "branch_switch"}:
            # These operations update the caller's worktree and/or live index.
            # A Host-enforced workspace lease (or a filesystem CAS over the
            # complete preimage) is required to make that final write safe.
            # This Pack has neither contract surface, so it must fail before
            # taking a receipt or starting any Git subprocess.
            raise PermissionError(
                f"Git {name.replace('_', ' ')} is unavailable until the Host provides an "
                "exclusive workspace mutation lease"
            )
        arguments = _arguments(name, payload)
        root, repository = self._roots(payload)
        _assert_repository_oid_widths(repository, arguments)
        _assert_repository_snapshot(repository, arguments)
        self._redeem(name, payload, arguments)
        if name == "stage":
            _assert_repository_snapshot(repository, arguments)
            paths = _stage(repository, arguments)
            return {"staged": paths, "published": False}
        if name == "commit":
            _assert_commit_effect_preconditions(repository, arguments)
            return self._commit(repository, arguments)
        raise AssertionError(f"unhandled Git write operation: {name}")

    def _commit(self, repository: Path, arguments: Mapping[str, Any]) -> dict[str, Any]:
        message = str(arguments["message"])
        entries = list(arguments["expected_commit_entries"])
        _materialize_captured_entries(repository, entries)
        with tempfile.TemporaryDirectory(prefix="tobkiri-git-index-") as temp:
            index_path = Path(temp) / "index"
            environment = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
            _git(repository, ["read-tree", arguments["expected_head"]], env=environment)
            _apply_exact_entries(repository, entries, environment)
            tree = _git(repository, ["write-tree"], env=environment).strip()
            commit_hash = _git(
                repository,
                [
                    "commit-tree",
                    tree,
                    "-p",
                    arguments["expected_head"],
                    "-m",
                    message,
                ],
                env=environment,
            ).strip()
        # `update-ref` targets the receipt's explicit branch ref, never the
        # mutable symbolic name HEAD.  Its old-OID condition is the ref CAS;
        # the attached-ref check immediately before it rejects a detached or
        # switched worktree without letting that switch redirect the update.
        _assert_symbolic_head(repository, arguments["expected_head_ref"])
        _git(
            repository,
            [
                "update-ref",
                arguments["expected_head_ref"],
                commit_hash,
                arguments["expected_head"],
            ],
        )
        return {
            "commit_hash": commit_hash,
            "message": message,
            "paths": [str(entry["path"]) for entry in entries],
            "all_tracked": bool(arguments["all_tracked"]),
            "published": False,
        }

    def _roots(self, payload: Mapping[str, Any]) -> tuple[Path, Path]:
        mount = self.client.invoke(
            WORKSPACE,
            "get",
            {
                "profile_id": _profile(payload),
                "workspace_id": str(payload.get("workspace_id") or ""),
            },
        )
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        if int(mount.get("mount_revision") or 0) != int(
            payload.get("expected_mount_revision") or -1
        ):
            raise PermissionError("workspace mount revision changed")
        root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        read = self.client.invoke(
            GIT_READ,
            "root",
            {
                "profile_id": _profile(payload),
                "workspace_id": str(payload.get("workspace_id") or ""),
            },
        )
        relative = str(read.get("repository_root") or ".")
        repository = (root / relative).resolve(strict=True)
        try:
            repository.relative_to(root)
        except ValueError as exc:
            raise PermissionError("Git repository root escapes workspace") from exc
        return root, repository

    def _redeem(self, name: str, payload: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"git.{name}",
                "authority": "git.write",
                "caller_id": str(payload.get("caller_id") or ""),
                "caller_pack_id": str(payload.get("caller_pack_id") or ""),
                "caller_function_id": str(payload.get("caller_function_id") or ""),
                "profile_id": _profile(payload),
                "workspace_id": str(payload.get("workspace_id") or ""),
                "session_id": str(payload.get("session_id") or ""),
                "arguments": dict(arguments),
            },
        )
        if not result.get("authorized"):
            raise PermissionError(str(result.get("reason") or "Git write denied"))


def create_git_write_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated local Git mutation operations."""
    return GitWriteService(client).invoke


class GitWriteV4Service:
    """Prepare and execute exact local Git mutations through Host V4 dispatch."""

    def __init__(
        self,
        client: Any,
        *,
        workspace_mutation_port: WorkspaceMutationPort | None = None,
        workspace_mutation_identity: WorkspaceMutationIdentity | None = None,
    ) -> None:
        self.client = client
        self.workspace_mutation_port = workspace_mutation_port
        self.workspace_mutation_identity = workspace_mutation_identity

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch one explicit prepare or execute operation."""
        operation = name.removeprefix(_V4_OPERATION_PREFIX)
        if operation not in _V4_OPERATIONS:
            raise ValueError(f"unknown Git V4 write operation: {name}")
        if operation.endswith("-prepare"):
            return self._prepare(operation, payload)
        return self._execute(operation, payload)

    def _prepare(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        root, repository, mount_revision = _capture_roots(self.client, payload)
        plan: dict[str, Any] = {
            "plan_version": "tobkiri.git-write.plan.v4",
            "operation": operation.removesuffix("-prepare"),
            "profile_id": _profile(payload),
            "workspace_id": str(payload.get("workspace_id") or ""),
            "expected_mount_revision": mount_revision,
            "repository_root": (
                repository.relative_to(root).as_posix() if repository != root else "."
            ),
        }
        if operation == "git-commit-prepare":
            plan.update(_prepare_commit(repository, payload))
        elif operation == "git-restore-prepare":
            plan.update(_prepare_restore(repository, payload))
        elif operation == "git-apply-patch-prepare":
            plan.update(_prepare_patch(repository, payload))
        else:  # pragma: no cover - operation allowlist is exhaustive
            raise AssertionError(operation)
        return _sealed_plan(plan)

    def _execute(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan = _validated_plan(operation, payload)
        binding = {
            "profile_id": plan["profile_id"],
            "workspace_id": plan["workspace_id"],
            "expected_mount_revision": plan["expected_mount_revision"],
        }
        if payload.get("profile_id") != plan["profile_id"]:
            raise PermissionError("Git plan profile binding changed")
        if str(payload.get("workspace_id") or "") != plan["workspace_id"]:
            raise PermissionError("Git plan workspace binding changed")
        root, repository, mount_revision = _capture_roots(self.client, binding)
        if mount_revision != plan["expected_mount_revision"]:
            raise PermissionError("workspace mount revision changed after prepare")
        relative = repository.relative_to(root).as_posix() if repository != root else "."
        if relative != plan["repository_root"]:
            raise PermissionError("Git repository root changed after prepare")
        with _WORKSPACE_MUTATION_LOCK:
            if operation == "git-commit":
                return _execute_commit(repository, plan)
            if operation == "git-restore":
                return _execute_restore(
                    root,
                    repository,
                    plan,
                    self.workspace_mutation_port,
                    self.workspace_mutation_identity,
                )
            if operation == "git-apply-patch":
                patch = _patch_bytes(payload.get("patch"))
                return _execute_patch(
                    root,
                    repository,
                    plan,
                    patch,
                    self.workspace_mutation_port,
                    self.workspace_mutation_identity,
                )
        raise AssertionError(operation)


class GitWriteHostFactoryV4:
    """Capture one exact Git write operation and Function identity."""

    def __init__(self, *, function_id: str, operation_id: str) -> None:
        if operation_id not in _V4_PREPARE_OPERATION_IDS | _V4_EXECUTE_OPERATION_IDS:
            raise ValueError("Git write V4 operation is unsupported")
        self.function_id = function_id
        self.operation_id = operation_id
        self.is_prepare = operation_id in _V4_PREPARE_OPERATION_IDS

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind exactly one resolved operation principal and Host domain."""

        bindings = tuple(context.provider_bindings)
        if len(bindings) != 1 or any(
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != CONTRACT_ID
            or binding.operation.operation_id != self.operation_id
            for binding in bindings
        ):
            raise PermissionError("Git write V4 bindings are incomplete")

        binding = bindings[0]
        workspace_port = context.workspace_mutation_port
        key = (
            binding.operation.contract_id,
            binding.operation.operation_id,
            binding.principal_ref.value,
        )
        domain_id = context.domain_ids.get(key)
        if domain_id is None:
            raise PermissionError("Git write V4 domain binding is unavailable")

        contribution = HostProviderContributionV4(
            contract_id=binding.operation.contract_id,
            contract_version=binding.operation.contract_version,
            operation_id=binding.operation.operation_id,
            principal_id=binding.principal_ref.value,
            artifact_digest=binding.artifact.digest,
            implementation_digest=binding.function.implementation_digest,
            domain_id=domain_id,
            invoke=_host_git_write_invoke(
                operation_id=self.operation_id,
                is_prepare=self.is_prepare,
                principal_id=binding.principal_ref.value,
                domain_id=domain_id,
                workspace_port=workspace_port,
            ),
        )
        return CapturedHostProviderV4((contribution,), lambda: None)


HOST_PROVIDER_FACTORY = {
    COMMIT_PREPARE_FUNCTION_ID: GitWriteHostFactoryV4(
        function_id=COMMIT_PREPARE_FUNCTION_ID,
        operation_id=COMMIT_PREPARE_OPERATION,
    ),
    COMMIT_FUNCTION_ID: GitWriteHostFactoryV4(
        function_id=COMMIT_FUNCTION_ID,
        operation_id=COMMIT_OPERATION,
    ),
    RESTORE_PREPARE_FUNCTION_ID: GitWriteHostFactoryV4(
        function_id=RESTORE_PREPARE_FUNCTION_ID,
        operation_id=RESTORE_PREPARE_OPERATION,
    ),
    RESTORE_FUNCTION_ID: GitWriteHostFactoryV4(
        function_id=RESTORE_FUNCTION_ID,
        operation_id=RESTORE_OPERATION,
    ),
    APPLY_PATCH_PREPARE_FUNCTION_ID: GitWriteHostFactoryV4(
        function_id=APPLY_PATCH_PREPARE_FUNCTION_ID,
        operation_id=APPLY_PATCH_PREPARE_OPERATION,
    ),
    APPLY_PATCH_FUNCTION_ID: GitWriteHostFactoryV4(
        function_id=APPLY_PATCH_FUNCTION_ID,
        operation_id=APPLY_PATCH_OPERATION,
    ),
}


def _host_git_write_invoke(
    *,
    operation_id: str,
    is_prepare: bool,
    principal_id: str,
    domain_id: str,
    workspace_port: WorkspaceMutationPort | None,
) -> Callable[
    [str, Mapping[str, Any], HostProviderInvocationContextV4],
    Mapping[str, Any],
]:
    """Capture one exact Provider identity and its narrow workspace port."""

    def invoke(
        selected_operation_id: str,
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        envelope = invocation.envelope
        if (
            selected_operation_id != operation_id
            or envelope.target_principal.value != principal_id
            or envelope.context.target_domain_id != domain_id
        ):
            raise PermissionError("Git write invocation binding changed")
        profile_id = str(envelope.context.profile_id or "")
        if not profile_id:
            raise PermissionError("Git write invocation profile is unavailable")
        request = dict(payload)
        _reject_untrusted_authority_fields(request)
        if is_prepare:
            if "profile_id" in request:
                raise PermissionError("Git prepare profile must come from the Host context")
            # A V4 prepare is reached through an authenticated Host envelope.
            # The client can choose a workspace, but cannot select the Profile
            # whose workspace and Git read capabilities will be used.
            request["profile_id"] = profile_id
        else:
            _assert_execute_profile_binding(request, profile_id)
        client = invocation.contract_client(
            allowed_contract_ids=_V4_DEPENDENCIES,
            consumer_pack_id=SERVICE_PACK_ID,
        )
        identity = WorkspaceMutationIdentity(
            context=envelope.context,
            target_principal=envelope.target_principal,
            target_domain_id=envelope.context.target_domain_id,
            target_boot_epoch=envelope.context.target_boot_epoch,
            target_namespace=envelope.context.handle_namespace,
        )
        return GitWriteV4Service(
            client,
            workspace_mutation_port=workspace_port,
            workspace_mutation_identity=identity,
        ).invoke(selected_operation_id, request)

    return invoke


def _reject_untrusted_authority_fields(payload: Mapping[str, Any]) -> None:
    """Reject V4 client claims that could be confused with Host authority."""

    for raw_key in payload:
        if not isinstance(raw_key, str) or raw_key.casefold() in _UNTRUSTED_AUTHORITY_FIELDS:
            raise PermissionError("Git write client authority fields are forbidden")


def _assert_execute_profile_binding(
    payload: Mapping[str, Any],
    profile_id: str,
) -> None:
    """Require the coordinator's sealed Profile binding to match the envelope."""

    supplied_profile_id = payload.get("profile_id")
    if not isinstance(supplied_profile_id, str) or supplied_profile_id != profile_id:
        raise PermissionError("Git execute profile binding changed")
    raw_plan = payload.get("plan")
    if not isinstance(raw_plan, Mapping) or raw_plan.get("profile_id") != profile_id:
        raise PermissionError("Git execute profile binding changed")


def _capture_roots(
    client: Any,
    payload: Mapping[str, Any],
) -> tuple[Path, Path, int]:
    """Resolve a workspace and bind its current mount revision."""

    profile_id = _profile(payload)
    workspace_id = str(payload.get("workspace_id") or "")
    mount = client.invoke(
        WORKSPACE,
        WORKSPACE_GET_OPERATION,
        {
            # The catalog operation is intentionally stable and opaque.  The
            # service-level action belongs in the payload, as required by the
            # workspace host-provider contract.
            "operation": "get",
            "profile_id": profile_id,
            "workspace_id": workspace_id,
        },
    )
    if not isinstance(mount, Mapping):
        raise KeyError("workspace mount is unknown")
    mount_revision = int(mount.get("mount_revision") or 0)
    if mount_revision < 1:
        raise PermissionError("workspace mount revision is unavailable")
    root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
    if not root.is_dir():
        raise PermissionError("workspace root is unavailable")
    repository = Path(
        _git(root, ["rev-parse", "--show-toplevel"]).strip()
    ).resolve(strict=True)
    try:
        repository.relative_to(root)
    except ValueError as exc:
        raise PermissionError("Git repository root escapes workspace") from exc
    return root, repository, mount_revision


def _sealed_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe plan with a deterministic content digest."""

    document = json.loads(json.dumps(dict(plan)))
    document["plan_digest"] = canonical_digest(document)
    return document


def _validated_plan(operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an execute request's exact prepared plan."""

    raw = payload.get("plan")
    if not isinstance(raw, Mapping):
        raise ValueError("Git execute requires a prepared plan")
    plan = json.loads(json.dumps(dict(raw)))
    digest = str(plan.pop("plan_digest", ""))
    if not digest or digest != canonical_digest(plan):
        raise PermissionError("Git prepared plan digest is invalid")
    if plan.get("plan_version") != "tobkiri.git-write.plan.v4":
        raise PermissionError("Git prepared plan version is unsupported")
    if plan.get("operation") != operation:
        raise PermissionError("Git prepared plan operation changed")
    for field in ("profile_id", "workspace_id", "repository_root"):
        if not isinstance(plan.get(field), str):
            raise ValueError(f"Git prepared plan {field} is invalid")
    if not isinstance(plan.get("expected_mount_revision"), int):
        raise ValueError("Git prepared mount revision is invalid")
    return plan


def _prepare_commit(
    repository: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Capture an exact staged index, attached HEAD, and commit message."""

    message = str(payload.get("message") or "").strip()
    if not message or len(message) > 10_000:
        raise ValueError("Git commit message is invalid")
    _reject_nonstandard_index_layout(repository)
    head_ref = _head_ref(_git(repository, ["symbolic-ref", "-q", "HEAD"]).strip())
    head = _git(repository, ["rev-parse", "HEAD"]).strip()
    if _git(repository, ["rev-parse", head_ref]).strip() != head:
        raise PermissionError("Git attached branch changed during prepare")
    head_tree = _git(repository, ["rev-parse", "HEAD^{tree}"]).strip()
    index_tree = _git(repository, ["write-tree"]).strip()
    if index_tree == head_tree:
        raise ValueError("Git commit has no staged changes")
    index_flags_hash = _index_flags_hash(repository)
    index_path = _git_index_path(repository)
    index_identity = _index_identity(index_path)
    if index_identity is None:
        raise PermissionError("Git index is unavailable")
    return {
        "message": message,
        "expected_head": head,
        "expected_head_ref": head_ref,
        "expected_head_tree": head_tree,
        "expected_index_tree": index_tree,
        "expected_index_identity": _index_plan_identity(index_identity),
        "expected_index_flags_hash": index_flags_hash,
    }


def _execute_commit(repository: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Create a commit and CAS-update only the prepared attached ref."""

    _reject_nonstandard_index_layout(repository)
    index_path = _git_index_path(repository)
    with _exclusive_index_lock(index_path):
        _assert_commit_plan(repository, index_path, plan)
        commit_hash = _git(
            repository,
            [
                "commit-tree",
                str(plan["expected_index_tree"]),
                "-p",
                str(plan["expected_head"]),
                "-m",
                str(plan["message"]),
            ],
        ).strip()
        _assert_commit_plan(repository, index_path, plan)
        _git(
            repository,
            [
                "update-ref",
                str(plan["expected_head_ref"]),
                commit_hash,
                str(plan["expected_head"]),
            ],
        )
    return {
        "commit_hash": commit_hash,
        "message": str(plan["message"]),
        "tree": str(plan["expected_index_tree"]),
        "published": False,
    }


def _assert_commit_plan(
    repository: Path,
    index_path: Path,
    plan: Mapping[str, Any],
) -> None:
    """Revalidate every mutable input to a prepared commit."""

    if _index_flags_hash(repository) != plan.get("expected_index_flags_hash"):
        raise PermissionError("Git index flags changed after prepare")
    expected_identity = plan.get("expected_index_identity")
    actual_identity = _index_identity(index_path)
    if actual_identity is None or _index_plan_identity(actual_identity) != expected_identity:
        raise PermissionError("Git index bytes changed after prepare")
    _assert_symbolic_head(repository, str(plan["expected_head_ref"]))
    if _git(repository, ["rev-parse", "HEAD"]).strip() != plan["expected_head"]:
        raise PermissionError("Git HEAD changed after prepare")
    if _git(repository, ["rev-parse", "HEAD^{tree}"]).strip() != plan["expected_head_tree"]:
        raise PermissionError("Git HEAD tree changed after prepare")
    _git(repository, ["cat-file", "-e", f"{plan['expected_index_tree']}^{{tree}}"])


def _reject_nonstandard_index_layout(repository: Path) -> None:
    """Reject index layouts whose backing bytes are not one self-contained file."""

    shared_index = _git(repository, ["rev-parse", "--shared-index-path"]).strip()
    sparse = _git_config_bool(repository, "core.sparseCheckout")
    if shared_index:
        raise PermissionError("Git split index is unsupported for V4 commit")
    if sparse:
        raise PermissionError("Git sparse checkout is unsupported for V4 commit")


def _git_config_bool(repository: Path, name: str) -> bool:
    """Read a local Git boolean without treating an unset key as failure."""

    completed = subprocess.run(
        ["git", "-C", str(repository), *_safe_git_args(["config", "--bool", name])],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode == 1:
        return False
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    value = completed.stdout.strip().casefold()
    if value not in {"true", "false"}:
        raise PermissionError(f"Git {name} setting is invalid")
    return value == "true"


def _index_flags_hash(repository: Path) -> str:
    """Bind all stage entries and extended index flags to a prepared commit."""

    return _git_digest(
        repository,
        ["ls-files", "--stage", "--debug", "-z"],
        max_bytes=_MAX_SNAPSHOT_STATUS_BYTES,
    )


def _prepare_restore(
    repository: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Capture exact restore preimages and immutable Git blob postimages."""

    paths = _requested_paths(payload)
    source = str(payload.get("source") or "HEAD").strip()
    if source.startswith("-") or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/@{}^~:-]*",
        source,
    ):
        raise ValueError("Git restore source is invalid")
    source_tree = _git(repository, ["rev-parse", f"{source}^{{tree}}"]).strip()
    targets = _restore_targets(repository, source_tree, paths)
    snapshot = _repository_plan_snapshot(repository)
    preimages = _capture_preimages(repository, paths)
    postimages = _restore_postimages(repository, targets)
    _assert_repository_plan_snapshot(repository, snapshot)
    _assert_preimages(repository, preimages)
    _validate_publication_plan(preimages, postimages)
    return {
        "paths": paths,
        "source_tree": source_tree,
        "targets": targets,
        "preimages": preimages,
        "postimages": postimages,
        "publication_protocol": "host-batch-journal-v1",
        "external_reader_snapshot_isolation": False,
        **snapshot,
    }


def _prepare_patch(
    repository: Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Parse and dry-run a raw patch while binding all affected preimages."""

    patch = _patch_bytes(payload.get("patch"))
    paths = _patch_paths(repository, patch)
    _check_patch(repository, patch)
    snapshot = _repository_plan_snapshot(repository)
    preimages = _capture_preimages(repository, paths)
    postimages, _ = _materialize_patch(repository, patch, paths, preimages)
    _assert_repository_plan_snapshot(repository, snapshot)
    _assert_preimages(repository, preimages)
    _validate_publication_plan(preimages, postimages)
    return {
        "paths": paths,
        "stdin_sha256": hashlib.sha256(patch).hexdigest(),
        "preimages": preimages,
        "postimages": postimages,
        "publication_protocol": "host-batch-journal-v1",
        "external_reader_snapshot_isolation": False,
        **snapshot,
    }


def _execute_restore(
    root: Path,
    repository: Path,
    plan: Mapping[str, Any],
    workspace_port: WorkspaceMutationPort | None,
    identity: WorkspaceMutationIdentity | None,
) -> dict[str, Any]:
    """Reconstruct restore blobs and publish one file through descriptor CAS."""

    _assert_repository_plan_snapshot(repository, plan)
    _assert_preimages(repository, plan.get("preimages"))
    _git(repository, ["cat-file", "-e", f"{plan['source_tree']}^{{tree}}"])
    _assert_restore_targets(repository, plan.get("targets"))
    postimages = _restore_postimages(repository, plan["targets"])
    if postimages != plan.get("postimages"):
        raise PermissionError("Git restore postimages changed after prepare")
    return _publish_postimages(
        root,
        repository,
        plan,
        postimages,
        workspace_port,
        identity,
        materialized_bytes=None,
    )


def _execute_patch(
    root: Path,
    repository: Path,
    plan: Mapping[str, Any],
    patch: bytes,
    workspace_port: WorkspaceMutationPort | None,
    identity: WorkspaceMutationIdentity | None,
) -> dict[str, Any]:
    """Rebuild a patch outside the live tree and descriptor-CAS publish it."""

    if hashlib.sha256(patch).hexdigest() != plan.get("stdin_sha256"):
        raise PermissionError("Git patch bytes changed after prepare")
    if _patch_paths(repository, patch) != plan.get("paths"):
        raise PermissionError("Git patch affected paths changed after prepare")
    _check_patch(repository, patch)
    _assert_repository_plan_snapshot(repository, plan)
    _assert_preimages(repository, plan.get("preimages"))
    postimages, materialized_bytes = _materialize_patch(
        repository,
        patch,
        plan["paths"],
        plan["preimages"],
    )
    if postimages != plan.get("postimages"):
        raise PermissionError("Git patch postimages changed after prepare")
    return _publish_postimages(
        root,
        repository,
        plan,
        postimages,
        workspace_port,
        identity,
        materialized_bytes=materialized_bytes,
    )


def _requested_paths(payload: Mapping[str, Any]) -> list[str]:
    raw = payload.get("paths")
    if not isinstance(raw, list) or not raw or len(raw) > _MAX_SNAPSHOT_PATHS:
        raise ValueError("explicit Git paths are required")
    paths = [_validated_path(str(item)) for item in raw]
    if len(set(paths)) != len(paths):
        raise ValueError("Git paths must be unique")
    return paths


def _restore_targets(
    repository: Path,
    source_tree: str,
    paths: Sequence[str],
) -> list[dict[str, str]]:
    """Bind each restore path to one immutable blob or an exact deletion."""

    targets: list[dict[str, str]] = []
    for path in paths:
        output = _git_output_bounded(
            repository,
            ["ls-tree", "-z", source_tree, "--", path],
            max_bytes=_MAX_SNAPSHOT_PATH_LIST_BYTES,
        )
        if not output:
            targets.append({"path": path, "mode": "", "blob_oid": ""})
            continue
        records = [record for record in output.split(b"\0") if record]
        if len(records) != 1:
            raise PermissionError("Git restore path expands to multiple entries")
        metadata, separator, raw_path = records[0].partition(b"\t")
        fields = metadata.decode("ascii", errors="strict").split(" ")
        actual_path = raw_path.decode("utf-8", errors="strict")
        if separator != b"\t" or len(fields) != 3 or actual_path != path:
            raise PermissionError("Git restore tree entry is ambiguous")
        mode, kind, blob_oid = fields
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise PermissionError("Git restore supports explicit file paths only")
        targets.append({"path": path, "mode": mode, "blob_oid": _oid(blob_oid)})
    return targets


def _assert_restore_targets(repository: Path, raw: Any) -> None:
    """Require every restore target blob to remain locally materializable."""

    if not isinstance(raw, list):
        raise ValueError("Git prepared restore targets are invalid")
    for target in raw:
        if not isinstance(target, Mapping):
            raise ValueError("Git prepared restore target is invalid")
        path = _validated_path(str(target.get("path") or ""))
        mode = str(target.get("mode") or "")
        blob_oid = str(target.get("blob_oid") or "")
        if not mode and not blob_oid:
            continue
        if mode not in {"100644", "100755", "120000"}:
            raise ValueError(f"Git prepared restore target mode is invalid: {path}")
        _git(repository, ["cat-file", "-e", f"{_oid(blob_oid)}^{{blob}}"])


def _restore_postimages(
    repository: Path,
    targets: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Materialize immutable restore blobs as bounded regular-file postimages."""

    postimages: list[dict[str, Any]] = []
    total_bytes = 0
    for target in targets:
        path = _validated_path(str(target.get("path") or ""))
        mode = str(target.get("mode") or "")
        blob_oid = str(target.get("blob_oid") or "")
        if not mode and not blob_oid:
            postimages.append({"path": path, "kind": "absent"})
            continue
        if mode == "120000":
            raise PermissionError("Git workspace mutation port cannot publish symbolic links")
        if mode not in {"100644", "100755"}:
            raise PermissionError("Git restore target is not a regular file")
        data = _git_output_bounded(
            repository,
            ["cat-file", "blob", _oid(blob_oid)],
            max_bytes=_MAX_STAGE_BYTES,
        )
        total_bytes += len(data)
        if total_bytes > _MAX_SNAPSHOT_BYTES:
            raise ValueError("Git restore postimages exceed maximum size")
        postimages.append(_postimage(path, data, int(mode[-3:], 8)))
    return postimages


def _materialize_patch(
    repository: Path,
    patch: bytes,
    paths: Sequence[str],
    preimages: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Apply a patch only inside a private temporary tree and hash its result."""

    _validate_preimages_for_port(preimages)
    if [item.get("path") for item in preimages] != list(paths):
        raise ValueError("Git patch preimages do not match affected paths")
    with tempfile.TemporaryDirectory(prefix="tobkiri-git-patch-") as temporary:
        isolated = Path(temporary)
        for preimage in preimages:
            if preimage["kind"] == "absent":
                continue
            path = _validated_path(str(preimage["path"]))
            data, is_symlink, metadata = _capture_stage_bytes(repository, path)
            if is_symlink or hashlib.sha256(data).hexdigest() != preimage["sha256"]:
                raise PermissionError("Git patch preimage changed during materialization")
            if stat.S_IMODE(metadata.st_mode) != preimage["mode"]:
                raise PermissionError("Git patch preimage mode changed")
            destination = isolated.joinpath(*PurePosixPath(path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(int(preimage["mode"]))
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(isolated),
                *_safe_git_args(["apply", "--recount", "--whitespace=nowarn", "-"]),
            ],
            input=patch,
            capture_output=True,
            text=False,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout).decode(
                "utf-8",
                errors="replace",
            )
            raise PermissionError(message.strip() or "Git patch materialization failed")
        actual_paths = _isolated_file_paths(isolated)
        unexpected = actual_paths.difference(paths)
        if unexpected:
            raise PermissionError("Git patch materialized an unbound path")
        postimages: list[dict[str, Any]] = []
        materialized_bytes: dict[str, bytes] = {}
        total_bytes = 0
        for raw_path in paths:
            path = _validated_path(raw_path)
            candidate = isolated.joinpath(*PurePosixPath(path).parts)
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                postimages.append({"path": path, "kind": "absent"})
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise PermissionError("Git workspace mutation port cannot publish symbolic links")
            if not stat.S_ISREG(metadata.st_mode):
                raise PermissionError("Git patch postimage is not a regular file")
            data = candidate.read_bytes()
            total_bytes += len(data)
            if len(data) > _MAX_STAGE_BYTES or total_bytes > _MAX_SNAPSHOT_BYTES:
                raise ValueError("Git patch postimages exceed maximum size")
            postimages.append(_postimage(path, data, stat.S_IMODE(metadata.st_mode)))
            materialized_bytes[path] = data
        return postimages, materialized_bytes


def _isolated_file_paths(root: Path) -> set[str]:
    """List and reject non-regular outputs from one private patch tree."""

    paths: set[str] = set()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            if (current_path / name).is_symlink():
                raise PermissionError("Git patch materialized a symbolic link")
        for name in filenames:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise PermissionError("Git patch materialized an unsafe file")
            paths.add(path.relative_to(root).as_posix())
    return paths


def _postimage(path: str, data: bytes, mode: int) -> dict[str, Any]:
    """Describe one bounded regular-file postimage without embedding its bytes."""

    if mode not in {0o644, 0o755}:
        raise PermissionError("Git postimage mode is unsupported")
    return {
        "path": path,
        "kind": "file",
        "mode": mode,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _repository_plan_snapshot(repository: Path) -> dict[str, Any]:
    """Capture repository values that a worktree publication depends on."""

    head = _git(repository, ["rev-parse", "HEAD"]).strip()
    head_tree = _git(repository, ["rev-parse", "HEAD^{tree}"]).strip()
    index_tree = _git(repository, ["write-tree"]).strip()
    status_hash = _status_hash(repository)
    worktree_hash = _worktree_hash(repository)
    index_flags_hash = _index_flags_hash(repository)
    index_identity = _index_identity(_git_index_path(repository))
    if index_identity is None:
        raise PermissionError("Git index is unavailable")
    return {
        "expected_head": head,
        "expected_head_tree": head_tree,
        "expected_index_tree": index_tree,
        "expected_index_identity": _index_plan_identity(index_identity),
        "expected_index_flags_hash": index_flags_hash,
        "expected_status_hash": status_hash,
        "expected_worktree_hash": worktree_hash,
    }


def _assert_repository_plan_snapshot(
    repository: Path,
    plan: Mapping[str, Any],
) -> None:
    """Revalidate a publication plan immediately before its disabled boundary."""

    actual = _repository_plan_snapshot(repository)
    expected = {key: plan.get(key) for key in actual}
    if actual != expected:
        raise PermissionError("Git repository changed after prepare")


def _capture_preimages(
    repository: Path,
    paths: Sequence[str],
) -> list[dict[str, Any]]:
    """Capture stable path type, bytes, existence, and parent identities."""

    result: list[dict[str, Any]] = []
    for path in paths:
        normalized = _validated_path(path)
        first = _capture_preimage(repository, normalized)
        second = _capture_preimage(repository, normalized)
        if first != second:
            raise PermissionError("Git path changed while capturing its preimage")
        result.append(first)
    return result


def _capture_preimage(repository: Path, path: str) -> dict[str, Any]:
    """Capture one path without following any workspace symlink."""

    _require_safe_dirfd_support()
    parts = Path(path).parts
    root_fd = _open_nofollow(repository, os.O_RDONLY | os.O_DIRECTORY)
    current = os.dup(root_fd)
    parents: list[dict[str, Any]] = [_directory_record(".", os.fstat(root_fd))]
    parent_missing = False
    try:
        prefix: list[str] = []
        for component in parts[:-1]:
            prefix.append(component)
            relative = Path(*prefix).as_posix()
            if parent_missing:
                parents.append({"path": relative, "kind": "absent"})
                continue
            try:
                child = _open_nofollow(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY,
                    dir_fd=current,
                )
            except FileNotFoundError:
                parents.append({"path": relative, "kind": "absent"})
                parent_missing = True
                continue
            except OSError as exc:
                raise PermissionError("Git path ancestor is unsafe") from exc
            os.close(current)
            current = child
            parents.append(_directory_record(relative, os.fstat(current)))
        if parent_missing:
            return {"path": path, "kind": "absent", "parents": parents}
        try:
            os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            return {"path": path, "kind": "absent", "parents": parents}
        data, is_symlink, captured = _capture_stage_bytes(repository, path)
        return {
            "path": path,
            "kind": "symlink" if is_symlink else "file",
            "mode": stat.S_IMODE(captured.st_mode),
            "device": str(captured.st_dev),
            "inode": str(captured.st_ino),
            "size": int(captured.st_size),
            "mtime_ns": str(captured.st_mtime_ns),
            "sha256": hashlib.sha256(data).hexdigest(),
            "parents": parents,
        }
    finally:
        os.close(current)
        os.close(root_fd)


def _directory_record(path: str, metadata: os.stat_result) -> dict[str, Any]:
    """Return an exact directory descriptor identity for a preimage chain."""

    if not stat.S_ISDIR(metadata.st_mode):
        raise PermissionError("Git path ancestor is not a directory")
    return {
        "path": path,
        "kind": "directory",
        "device": str(metadata.st_dev),
        "inode": str(metadata.st_ino),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _assert_preimages(repository: Path, raw: Any) -> None:
    """Require exact preimages before any future descriptor-CAS publication."""

    if not isinstance(raw, list):
        raise ValueError("Git prepared preimages are invalid")
    paths = [str(item.get("path") or "") for item in raw if isinstance(item, Mapping)]
    if len(paths) != len(raw):
        raise ValueError("Git prepared preimages are invalid")
    if _capture_preimages(repository, paths) != raw:
        raise PermissionError("Git path preimage changed after prepare")


def _validate_preimages_for_port(
    preimages: Sequence[Mapping[str, Any]],
) -> None:
    """Reject states which the regular-file-only Host port cannot bind."""

    for preimage in preimages:
        path = _validated_path(str(preimage.get("path") or ""))
        if preimage.get("kind") not in {"file", "absent"}:
            raise PermissionError("Git workspace mutation port cannot bind symbolic links")
        parents = preimage.get("parents")
        if not isinstance(parents, list) or any(
            not isinstance(parent, Mapping) or parent.get("kind") != "directory"
            for parent in parents
        ):
            raise PermissionError(f"Git workspace mutation requires existing parents: {path}")


def _validate_publication_plan(
    preimages: Sequence[Mapping[str, Any]],
    postimages: Sequence[Mapping[str, Any]],
) -> None:
    """Validate a bounded publication plan for the Host batch transaction."""

    _validate_preimages_for_port(preimages)
    if len(preimages) != len(postimages):
        raise ValueError("Git publication preimages and postimages differ")
    for preimage, postimage in zip(preimages, postimages, strict=True):
        if preimage.get("path") != postimage.get("path"):
            raise ValueError("Git publication path binding changed")
        if postimage.get("kind") not in {"file", "absent"}:
            raise PermissionError("Git publication postimage is unsupported")
        if (
            preimage.get("kind") == "file"
            and postimage.get("kind") == "file"
            and preimage.get("mode") != postimage.get("mode")
        ):
            raise PermissionError("Git workspace mutation port cannot replace file modes")
    actions = _publication_actions(preimages, postimages)
    if len(actions) > WORKSPACE_BATCH_MAX_MUTATIONS:
        raise ValueError("Git publication exceeds Host batch mutation limit")
    total_bytes = sum(
        int(postimage.get("size") or 0)
        for _, postimage in actions
        if postimage.get("kind") == "file"
    )
    if total_bytes > WORKSPACE_BATCH_MAX_BYTES:
        raise ValueError("Git publication exceeds Host batch byte limit")


def _publication_actions(
    preimages: Sequence[Mapping[str, Any]],
    postimages: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Return only file states whose content, mode, or existence must change."""

    actions: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for preimage, postimage in zip(preimages, postimages, strict=True):
        unchanged = preimage.get("kind") == postimage.get("kind") == "absent"
        if preimage.get("kind") == postimage.get("kind") == "file":
            unchanged = all(
                preimage.get(field) == postimage.get(field) for field in ("mode", "size", "sha256")
            )
        if not unchanged:
            actions.append((preimage, postimage))
    return actions


def _publish_postimages(
    root: Path,
    repository: Path,
    plan: Mapping[str, Any],
    postimages: Sequence[Mapping[str, Any]],
    workspace_port: WorkspaceMutationPort | None,
    identity: WorkspaceMutationIdentity | None,
    *,
    materialized_bytes: Mapping[str, bytes] | None,
) -> dict[str, Any]:
    """Bind every preimage, recheck, then publish one Host batch transaction."""

    if (
        plan.get("publication_protocol") != "host-batch-journal-v1"
        or plan.get("external_reader_snapshot_isolation") is not False
    ):
        raise PermissionError("Git publication semantics binding changed")
    raw_preimages = plan.get("preimages")
    if not isinstance(raw_preimages, list):
        raise ValueError("Git publication preimages are invalid")
    preimages = [dict(item) for item in raw_preimages if isinstance(item, Mapping)]
    if len(preimages) != len(raw_preimages):
        raise ValueError("Git publication preimages are invalid")
    _validate_publication_plan(preimages, postimages)
    actions = _publication_actions(preimages, postimages)
    if not actions:
        return {
            "paths": [],
            "workspace_published": False,
            "publication_protocol": "host-batch-journal-v1",
            "external_reader_snapshot_isolation": False,
        }
    if workspace_port is None or identity is None:
        raise PermissionError("Git workspace mutation port is unavailable")
    root_metadata = root.stat()
    binding = WorkspaceMutationBinding(
        profile_id=str(plan["profile_id"]),
        workspace_id=str(plan["workspace_id"]),
        mount_revision=int(plan["expected_mount_revision"]),
        canonical_root=root,
        root_st_dev=int(root_metadata.st_dev),
        root_st_ino=int(root_metadata.st_ino),
    )
    lease = workspace_port.acquire_lease(
        WorkspaceMutationLeaseRequest(identity=identity, binding=binding)
    )
    batch_result: WorkspaceBatchResult | None = None
    close_error: Exception | None = None
    try:
        handles: dict[str, Any] = {}
        for preimage, postimage in zip(preimages, postimages, strict=True):
            relative_path = _workspace_relative_path(
                root,
                repository,
                str(preimage["path"]),
            )
            max_bytes = max(
                int(preimage.get("size") or 0),
                int(postimage.get("size") or 0),
                1,
            )
            if preimage["kind"] == "file":
                handle = workspace_port.bind_existing(
                    lease,
                    identity,
                    relative_path=relative_path,
                    ttl_seconds=30,
                    max_uses=1,
                    max_bytes=max_bytes,
                )
            else:
                handle = workspace_port.bind_absent(
                    lease,
                    identity,
                    relative_path=relative_path,
                    ttl_seconds=30,
                    max_uses=1,
                    max_bytes=max_bytes,
                )
            handles[str(preimage["path"])] = handle
        _assert_repository_plan_snapshot(repository, plan)
        _assert_preimages(repository, preimages)
        mutations: list[WorkspaceBatchMutation] = []
        for preimage, postimage in actions:
            handle = handles[str(preimage["path"])]
            if postimage["kind"] == "absent":
                mutations.append(WorkspaceBatchMutation("delete", handle))
                continue
            data = _postimage_bytes(
                repository,
                plan,
                postimage,
                materialized_bytes,
            )
            if preimage["kind"] == "file":
                mutations.append(WorkspaceBatchMutation("replace", handle, data))
            else:
                mutations.append(
                    WorkspaceBatchMutation(
                        "create",
                        handle,
                        data,
                        mode=int(postimage["mode"]),
                    )
                )
        expected_total_bytes = sum(len(mutation.data) for mutation in mutations)
        result = workspace_port.publish_batch(lease, identity, tuple(mutations))
        if (
            not isinstance(result, WorkspaceBatchResult)
            or result.status != "committed"
            or result.mutation_count != len(mutations)
            or result.total_bytes != expected_total_bytes
            or not result.transaction_id
        ):
            raise RuntimeError("WORKSPACE_BATCH_RESULT_AMBIGUOUS")
        batch_result = result
    finally:
        try:
            workspace_port.close_lease(lease, identity)
        except Exception as error:  # pragma: no cover - defensive ambiguity marker
            close_error = error
    if close_error is not None and batch_result is None:
        raise RuntimeError("WORKSPACE_LEASE_CLOSE_FAILED_NOT_COMMITTED") from close_error
    if batch_result is None:  # pragma: no cover - guarded by Host port contract
        raise RuntimeError("WORKSPACE_BATCH_RESULT_AMBIGUOUS")
    return {
        "paths": [str(preimage["path"]) for preimage, _ in actions],
        "workspace_published": True,
        "publication_protocol": "host-batch-journal-v1",
        "external_reader_snapshot_isolation": False,
        "workspace_transaction_id": batch_result.transaction_id,
        "workspace_cleanup": "lease-close-failed" if close_error else "complete",
    }


def _workspace_relative_path(root: Path, repository: Path, path: str) -> str:
    """Join an index path to its repository root without exposing an absolute path."""

    repository_relative = repository.relative_to(root)
    combined = repository_relative.joinpath(*PurePosixPath(_validated_path(path)).parts)
    return combined.as_posix()


def _postimage_bytes(
    repository: Path,
    plan: Mapping[str, Any],
    postimage: Mapping[str, Any],
    materialized_bytes: Mapping[str, bytes] | None,
) -> bytes:
    """Statelessly reconstruct the one approved regular-file postimage."""

    if plan["operation"] == "git-restore":
        targets = {str(item["path"]): item for item in plan["targets"] if isinstance(item, Mapping)}
        target = targets.get(str(postimage["path"]))
        if target is None:
            raise ValueError("Git restore postimage target is missing")
        data = _git_output_bounded(
            repository,
            ["cat-file", "blob", _oid(str(target["blob_oid"]))],
            max_bytes=_MAX_STAGE_BYTES,
        )
    elif plan["operation"] == "git-apply-patch":
        data = (materialized_bytes or {}).get(str(postimage["path"]))
        if data is None:
            raise ValueError("Git patch postimage bytes are missing")
    else:
        raise AssertionError("unsupported workspace publication")
    if _postimage(str(postimage["path"]), data, int(postimage["mode"])) != postimage:
        raise PermissionError("Git reconstructed postimage differs from prepare")
    return data


def _patch_bytes(value: Any) -> bytes:
    """Encode one bounded UTF-8 unified patch exactly as supplied."""

    if not isinstance(value, str):
        raise ValueError("Git patch must be UTF-8 text")
    try:
        patch = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("Git patch must be UTF-8 text") from exc
    if not patch or len(patch) > _MAX_PATCH_BYTES or b"\x00" in patch:
        raise ValueError("Git patch size or encoding is invalid")
    if any(
        marker in patch for marker in (b"GIT binary patch", b"Binary files ", b"Subproject commit ")
    ):
        raise PermissionError("binary and submodule patches are unsupported")
    return patch


def _patch_paths(repository: Path, patch: bytes) -> list[str]:
    """Parse a strict unified patch and return every old and new affected path."""

    text = patch.decode("utf-8", errors="strict")
    header_paths: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            fields = line.split(" ")
            if len(fields) != 4:
                raise PermissionError("quoted or whitespace Git patch paths are unsupported")
            header_paths.extend((_patch_path(fields[2], "a/"), _patch_path(fields[3], "b/")))
        elif line.startswith("rename from "):
            header_paths.append(_patch_path(line.removeprefix("rename from "), ""))
        elif line.startswith("rename to "):
            header_paths.append(_patch_path(line.removeprefix("rename to "), ""))
    previous_old: str | None = None
    for line in text.splitlines():
        if line.startswith("--- "):
            previous_old = line[4:]
        elif line.startswith("+++ ") and previous_old is not None:
            for value, prefix in ((previous_old, "a/"), (line[4:], "b/")):
                if value != "/dev/null":
                    header_paths.append(_patch_path(value, prefix))
            previous_old = None
    if previous_old is not None or not header_paths:
        raise ValueError("Git patch has incomplete unified path headers")
    discovered = list(dict.fromkeys(header_paths))
    numstat = _git_bytes(
        repository,
        ["apply", "--numstat", "-z", "--recount", "-"],
        input_bytes=patch,
    )
    for record in numstat.split(b"\0"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3:
            raise PermissionError("Git patch numstat is ambiguous")
        path = _validated_path(fields[2].decode("utf-8", errors="strict"))
        if path not in discovered:
            raise PermissionError("Git patch contains an unbound affected path")
    return discovered


def _patch_path(value: str, prefix: str) -> str:
    """Validate one strict, unquoted patch header path."""

    if not value or any(character.isspace() for character in value):
        raise PermissionError("quoted or whitespace Git patch paths are unsupported")
    if prefix and not value.startswith(prefix):
        raise PermissionError("Git patch path prefix is invalid")
    return _validated_path(value[len(prefix) :])


def _check_patch(repository: Path, patch: bytes) -> None:
    """Dry-run a patch without permitting index or worktree publication."""

    _git_bytes(
        repository,
        ["apply", "--check", "--recount", "--whitespace=nowarn", "-"],
        input_bytes=patch,
    )


def _arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    expected_head = str(payload.get("expected_head") or "").strip()
    expected_tree = str(payload.get("expected_tree") or "").strip()
    expected_index_tree = str(payload.get("expected_index_tree") or "").strip()
    expected_status_hash = str(payload.get("expected_status_hash") or "").strip()
    expected_worktree_hash = str(payload.get("expected_worktree_hash") or "").strip()
    expected_mount_revision = int(payload.get("expected_mount_revision") or -1)
    if not all(
        (
            expected_head,
            expected_tree,
            expected_index_tree,
            expected_status_hash,
            expected_worktree_hash,
        )
    ) or (expected_mount_revision < 1):
        raise ValueError(
            "expected_head, expected_tree, expected_index_tree, "
            "expected_status_hash, expected_worktree_hash, and "
            "expected_mount_revision are required"
        )
    snapshot = {
        "expected_head": _oid(expected_head),
        "expected_tree": _oid(expected_tree),
        "expected_index_tree": _oid(expected_index_tree),
        "expected_status_hash": _oid(expected_status_hash),
        "expected_worktree_hash": _oid(expected_worktree_hash),
        "expected_mount_revision": expected_mount_revision,
    }
    if name in {"branch_create", "branch_switch"}:
        branch = str(payload.get("branch") or payload.get("name") or "").strip()
        if not branch:
            raise ValueError("Git branch is required")
        expected_branch_oid = _oid_or_zero(payload.get("expected_branch_oid"))
        if name == "branch_create" and not _is_zero_oid(expected_branch_oid):
            raise ValueError("Git branch must be absent at approval time")
        if name == "branch_switch" and _is_zero_oid(expected_branch_oid):
            raise ValueError("Git branch must exist at approval time")
        return {
            "branch": branch,
            "expected_branch_oid": expected_branch_oid,
            **snapshot,
        }
    paths = payload.get("paths") or payload.get("files") or []
    if not isinstance(paths, list):
        raise ValueError("Git paths must be a list")
    normalized_paths = [_validated_path(str(item)) for item in paths]
    result: dict[str, Any] = {"paths": normalized_paths, **snapshot}
    if name == "commit":
        message = str(payload.get("message") or "").strip()
        if not message or len(message) > 10_000:
            raise ValueError("Git commit message is invalid")
        expected_head_ref = _head_ref(str(payload.get("expected_head_ref") or ""))
        entries = _commit_entries_argument(
            payload.get("expected_commit_entries"),
            normalized_paths if normalized_paths else None,
        )
        result.update(
            {
                "message": message,
                "all_tracked": bool(payload.get("all_tracked", False)),
                "expected_head_ref": expected_head_ref,
                "expected_commit_entries": entries,
            }
        )
        if result["paths"] and result["all_tracked"]:
            raise ValueError("paths and all_tracked cannot be combined")
        if not result["paths"] and not result["all_tracked"]:
            raise ValueError("commit requires explicit paths or all_tracked")
    if name == "restore":
        result["source"] = str(payload.get("source") or "")
        result["expected_restore_tree"] = _oid(str(payload.get("expected_restore_tree") or ""))
    if name in {"stage", "restore"}:
        result["expected_path_entries"] = _path_entries_argument(
            payload.get("expected_path_entries"),
            result["paths"],
        )
    if not result["paths"] and name in {"stage", "restore"}:
        raise ValueError("explicit Git paths are required")
    return result


def _paths(repository: Path, values: list[str], *, allow_missing: bool) -> list[str]:
    result = []
    for value in values:
        normalized = _validated_path(str(value))
        raw = Path(normalized)
        if raw.is_absolute() or ".." in raw.parts or ".git" in raw.parts:
            raise PermissionError("Git path escapes or targets metadata")
        if raw.name.casefold() in _RESTRICTED_NAMES or raw.suffix.casefold() in {
            ".pem",
            ".key",
            ".p12",
        }:
            raise PermissionError("Git path is credential-sensitive")
        root_fd, parent_fd, filename = _open_verified_parent(repository, normalized)
        try:
            try:
                os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not allow_missing:
                    raise FileNotFoundError("Git path is unavailable") from None
            _assert_parent_chain_stable(root_fd, parent_fd, normalized)
        finally:
            os.close(parent_fd)
            os.close(root_fd)
        result.append(raw.as_posix())
    return result


def _validated_path(value: str) -> str:
    """Validate a relative index path before it reaches index-info."""

    raw = Path(str(value))
    folded_parts = {part.casefold() for part in raw.parts}
    if raw.is_absolute() or ".." in raw.parts or ".git" in folded_parts:
        raise PermissionError("Git path escapes or targets metadata")
    normalized = raw.as_posix()
    if not normalized or any(character in normalized for character in "\x00\r\n\t"):
        raise PermissionError("Git path contains an unsafe index delimiter")
    if folded_parts.intersection(_RESTRICTED_NAMES) or Path(normalized).suffix.casefold() in {
        ".pem",
        ".key",
        ".p12",
    }:
        raise PermissionError("Git path is credential-sensitive")
    return normalized


def _head_ref(value: str) -> str:
    """Validate the symbolic branch ref captured for a commit receipt."""

    normalized = str(value or "").strip()
    if not re.fullmatch(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", normalized):
        raise ValueError("Git commit requires an attached local branch snapshot")
    if ".." in normalized or "//" in normalized or normalized.endswith((".", "/")):
        raise ValueError("Git commit branch snapshot is invalid")
    return normalized


def _path_entries_argument(value: Any, paths: list[str]) -> list[dict[str, str]]:
    """Validate the exact worktree blobs embedded in a receipt scope."""

    if not isinstance(value, list) or len(value) != len(paths):
        raise ValueError("Git path entries are required for every Git path")
    entries: list[dict[str, str]] = []
    for path, raw in zip(paths, value, strict=True):
        if not isinstance(raw, Mapping) or str(raw.get("path") or "") != path:
            raise ValueError("Git path entries do not match requested paths")
        blob_oid = str(raw.get("blob_oid") or "").strip().lower()
        mode = str(raw.get("mode") or "").strip()
        if not blob_oid and not mode:
            entries.append({"path": path, "blob_oid": "", "mode": ""})
            continue
        if not blob_oid or mode not in {"100644", "100755", "120000"}:
            raise ValueError("Git path entry is invalid")
        entries.append({"path": path, "blob_oid": _oid(blob_oid), "mode": mode})
    return entries


def _commit_entries_argument(
    value: Any,
    requested_paths: list[str] | None,
) -> list[dict[str, str]]:
    """Validate the raw blob entries captured before commit approval.

    The path, mode, and raw blob OID are part of the authority receipt scope.
    At the effect boundary the write Pack captures each approved path exactly
    once through a nofollow descriptor, verifies this OID, and writes only
    those captured bytes to the object database.
    """

    if not isinstance(value, list) or len(value) > _MAX_SNAPSHOT_PATHS:
        raise ValueError("Git commit entries are invalid")
    entries: list[dict[str, str]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("Git commit entry is invalid")
        path = _validated_path(str(raw.get("path") or ""))
        blob_oid = str(raw.get("blob_oid") or "").strip().lower()
        mode = str(raw.get("mode") or "").strip()
        if not blob_oid and not mode:
            entries.append({"path": path, "blob_oid": "", "mode": ""})
            continue
        if not blob_oid or mode not in {"100644", "100755", "120000"}:
            raise ValueError("Git commit entry is invalid")
        entries.append(
            {
                "path": path,
                "blob_oid": _oid(blob_oid),
                "mode": mode,
            }
        )
    if requested_paths is not None and [entry["path"] for entry in entries] != requested_paths:
        raise ValueError("Git commit entries do not match requested paths")
    return entries


def _stage(repository: Path, arguments: Mapping[str, Any]) -> list[str]:
    """Stage receipt-bound blobs without rereading mutable path content."""

    paths = _paths(repository, arguments["paths"], allow_missing=True)
    expected = list(arguments["expected_path_entries"])
    _assert_worktree_entries(repository, paths, expected)

    # Capture every approved path through a stable nofollow descriptor before
    # writing anything to the object database.  A race can therefore only
    # cause rejection; it cannot leave raced bytes as an unreachable object.
    materialized: list[tuple[dict[str, str], bytes]] = []
    for entry in expected:
        if not entry["blob_oid"]:
            materialized.append((entry, b""))
            continue
        data, _, _ = _capture_stage_bytes(repository, entry["path"])
        blob_oid = _hash_captured_bytes(repository, data, write=False)
        if blob_oid != entry["blob_oid"]:
            raise PermissionError("Git worktree path changed during staging")
        materialized.append((entry, data))
    approved_entries: list[dict[str, str]] = []
    for entry, data in materialized:
        if entry["blob_oid"]:
            written_oid = _hash_captured_bytes(repository, data, write=True)
            if written_oid != entry["blob_oid"]:
                raise PermissionError("Git stage bytes do not match the receipt")
        approved_entries.append(entry)
    _publish_exact_index(
        repository,
        arguments["expected_index_tree"],
        approved_entries,
    )
    return paths


def _assert_worktree_entries(
    repository: Path,
    paths: list[str],
    expected: list[Mapping[str, str]],
) -> None:
    actual = _worktree_entries(repository, paths)
    if actual != [dict(entry) for entry in expected]:
        raise PermissionError("Git worktree paths changed after preflight")


def _worktree_entries(
    repository: Path,
    paths: list[str],
    *,
    object_format: str | None = None,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    selected_format = object_format or _object_hash_format(repository)
    for path in paths:
        try:
            data, is_symlink, metadata = _capture_stage_bytes(repository, path)
        except FileNotFoundError:
            entries.append({"path": path, "blob_oid": "", "mode": ""})
            continue
        mode = "120000" if is_symlink else ("100755" if metadata.st_mode & 0o111 else "100644")
        blob_oid = _raw_blob_oid(data, object_format=selected_format)
        entries.append({"path": path, "blob_oid": blob_oid, "mode": mode})
    return entries


def _capture_stage_bytes(
    repository: Path,
    path: str,
) -> tuple[bytes, bool, os.stat_result]:
    """Read one final component through verified nofollow directory FDs."""

    root_fd, parent_fd, filename = _open_verified_parent(repository, path)
    try:
        before = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            data = os.readlink(filename, dir_fd=parent_fd).encode(
                "utf-8",
                errors="surrogateescape",
            )
            after = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            if _file_identity(before) != _file_identity(after):
                raise PermissionError("Git symlink changed during staging")
            if len(data) > _MAX_STAGE_BYTES:
                raise ValueError("Git stage input exceeds maximum size")
            _assert_parent_chain_stable(root_fd, parent_fd, path)
            return data, True, before
        descriptor = _open_nofollow(filename, os.O_RDONLY, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise PermissionError("Git path is not a regular file")
            if opened.st_size > _MAX_STAGE_BYTES:
                raise ValueError("Git stage input exceeds maximum size")
            chunks: list[bytes] = []
            remaining = _MAX_STAGE_BYTES
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise ValueError("Git stage input exceeds maximum size")
            closed = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(closed):
                raise PermissionError("Git path changed during staging")
            _assert_parent_chain_stable(root_fd, parent_fd, path)
            return b"".join(chunks), False, opened
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
        os.close(root_fd)


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return fields that must remain stable while a stage input is captured."""

    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _open_verified_parent(repository: Path, path: str) -> tuple[int, int, str]:
    """Open every parent from the repository dirfd without following links."""

    _require_safe_dirfd_support()
    parts = Path(path).parts
    if not parts or parts[-1] in {"", "."}:
        raise PermissionError("Git path is not a final file component")
    root_fd = _open_nofollow(repository, os.O_RDONLY | os.O_DIRECTORY)
    current = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            try:
                child = _open_nofollow(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY,
                    dir_fd=current,
                )
            except OSError as exc:
                raise PermissionError("Git path ancestor is unavailable or unsafe") from exc
            os.close(current)
            current = child
        return root_fd, current, parts[-1]
    except BaseException:
        os.close(current)
        os.close(root_fd)
        raise


def _require_safe_dirfd_support() -> None:
    """Fail closed unless POSIX dirfd and nofollow primitives are present."""

    required = (os.open, os.stat, os.readlink)
    if any(function not in os.supports_dir_fd for function in required):
        raise PermissionError("Git staging requires POSIX dirfd support")
    if os.stat not in os.supports_follow_symlinks:
        raise PermissionError("Git staging requires nofollow stat support")
    if getattr(os, "O_DIRECTORY", None) is None or getattr(os, "O_NOFOLLOW", None) is None:
        raise PermissionError("Git staging requires nofollow directory support")


def _open_nofollow(
    path: str | Path,
    flags: int,
    *,
    dir_fd: int | None = None,
) -> int:
    """Open one component only when symlink traversal is rejected."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise PermissionError("Git staging requires nofollow descriptor support")
    return os.open(path, flags | nofollow, dir_fd=dir_fd)


def _assert_parent_chain_stable(root_fd: int, parent_fd: int, path: str) -> None:
    """Reject a rename or replacement of a parent after capture began.

    The original repository descriptor anchors the workspace boundary.  This
    rewalk never resolves an ambient path, so a parent-symlink replacement is
    detected before raw bytes become a Git object.
    """

    expected = _directory_identity(os.fstat(parent_fd))
    current = os.dup(root_fd)
    try:
        for component in Path(path).parts[:-1]:
            try:
                child = _open_nofollow(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY,
                    dir_fd=current,
                )
            except OSError as exc:
                raise PermissionError("Git path ancestor changed during staging") from exc
            os.close(current)
            current = child
        if _directory_identity(os.fstat(current)) != expected:
            raise PermissionError("Git path ancestor changed during staging")
    finally:
        os.close(current)


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    """Return the immutable identity fields used for directory revalidation."""

    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        stat.S_IFMT(metadata.st_mode),
    )


def _hash_captured_bytes(
    repository: Path,
    data: bytes,
    *,
    write: bool,
) -> str:
    """Hash raw captured bytes without invoking repository clean filters."""

    args = ["hash-object"]
    if write:
        args.append("-w")
    args.extend(["--stdin", "--no-filters"])
    return _git_bytes(repository, args, input_bytes=data).decode("ascii").strip()


def _object_hash_format(repository: Path) -> str:
    """Read the Git object format once for in-process raw blob hashing."""

    value = _git(repository, ["rev-parse", "--show-object-format"]).strip()
    if value not in {"sha1", "sha256"}:
        raise PermissionError("Git object format is unsupported")
    return value


def _object_oid_width(object_format: str) -> int:
    """Return the object-ID width supported by one Git object format."""

    if object_format == "sha1":
        return 40
    if object_format == "sha256":
        return 64
    raise PermissionError("Git object format is unsupported")


def _zero_oid(repository: Path) -> str:
    """Return Git's format-correct absent-object sentinel for this repository."""

    return "0" * _object_oid_width(_object_hash_format(repository))


def _raw_blob_oid(data: bytes, *, object_format: str) -> str:
    """Compute a raw Git blob OID without repository clean filters."""

    digest = hashlib.new(object_format)
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def _materialize_captured_entries(
    repository: Path,
    entries: Sequence[Mapping[str, str]],
) -> None:
    """Capture then publish only receipt-bound raw blobs for one commit."""

    materialized: list[tuple[Mapping[str, str], bytes]] = []
    total_bytes = 0
    for entry in entries:
        if not entry["blob_oid"]:
            if _final_metadata(repository, entry["path"]) is not None:
                raise PermissionError("Git commit path changed after preflight")
            materialized.append((entry, b""))
            continue
        try:
            data, is_symlink, metadata = _capture_stage_bytes(
                repository,
                entry["path"],
            )
        except FileNotFoundError:
            raise PermissionError("Git commit path changed after preflight")
        mode = "120000" if is_symlink else ("100755" if metadata.st_mode & 0o111 else "100644")
        total_bytes += len(data)
        if total_bytes > _MAX_SNAPSHOT_BYTES:
            raise ValueError("Git commit snapshot exceeds maximum size")
        captured_oid = _hash_captured_bytes(repository, data, write=False)
        if captured_oid != entry["blob_oid"] or mode != entry["mode"]:
            raise PermissionError("Git commit path changed after preflight")
        materialized.append((entry, data))
    for entry, data in materialized:
        if not entry["blob_oid"]:
            continue
        written_oid = _hash_captured_bytes(repository, data, write=True)
        if written_oid != entry["blob_oid"]:
            raise PermissionError("Git commit snapshot bytes do not match its blob")


def _final_metadata(repository: Path, path: str) -> os.stat_result | None:
    """Read final-component metadata only through its verified parent dirfd."""

    root_fd, parent_fd, filename = _open_verified_parent(repository, path)
    try:
        try:
            metadata = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            metadata = None
        _assert_parent_chain_stable(root_fd, parent_fd, path)
        return metadata
    finally:
        os.close(parent_fd)
        os.close(root_fd)


def _apply_exact_entries(
    repository: Path,
    entries: Sequence[Mapping[str, str]],
    environment: Mapping[str, str],
) -> None:
    """Apply receipt entries to an isolated index using safe index-info rows."""

    if not entries:
        return
    _git(
        repository,
        ["update-index", "--index-info"],
        env=environment,
        input_text=_index_info_lines(repository, entries),
    )


def _publish_exact_index(
    repository: Path,
    expected_index_tree: str,
    entries: Sequence[Mapping[str, str]],
) -> None:
    """CAS-publish one complete approved index while holding Git's index lock.

    A sequence of live ``git update-index`` calls can merge an unselected
    concurrent mutation after the final snapshot check. Build the complete
    target index from the immutable approved tree, then replace the live index
    only while the standard Git ``index.lock`` excludes every Git index writer.
    """

    index_path = _git_index_path(repository)
    if _git(repository, ["write-tree"]).strip() != expected_index_tree:
        raise PermissionError("Git index changed after preflight")
    captured_index = _index_identity(index_path)
    with _exclusive_index_lock(index_path):
        if _index_identity(index_path) != captured_index:
            raise PermissionError("Git index changed after preflight")
        with tempfile.TemporaryDirectory(
            prefix="tobkiri-git-index-",
            dir=index_path.parent,
        ) as temporary:
            planned_index = Path(temporary) / "index"
            environment = {**os.environ, "GIT_INDEX_FILE": str(planned_index)}
            _git(
                repository,
                ["read-tree", expected_index_tree],
                env=environment,
            )
            _git(
                repository,
                ["update-index", "--index-info"],
                env=environment,
                input_text=_index_info_lines(repository, entries),
            )
            _git(repository, ["write-tree"], env=environment)
            _fsync_file(planned_index)
            os.replace(planned_index, index_path)
            _fsync_directory(index_path.parent)


def _index_info_lines(
    repository: Path,
    entries: Sequence[Mapping[str, str]],
) -> str:
    """Build delimiter-safe index-info rows with format-correct deletions."""

    zero_oid = _zero_oid(repository)
    lines = []
    for entry in entries:
        if entry["blob_oid"]:
            lines.append(f"{entry['mode']} {entry['blob_oid']}\t{entry['path']}\n")
        else:
            lines.append(f"0 {zero_oid}\t{entry['path']}\n")
    return "".join(lines)


def _git_index_path(repository: Path) -> Path:
    """Resolve the real Git index path without trusting a caller path."""

    value = _git(repository, ["rev-parse", "--git-path", "index"]).strip()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repository / candidate
    return candidate.resolve(strict=False)


def _index_identity(index_path: Path) -> tuple[int, int, int, str] | None:
    """Return a byte and inode identity without asking Git to take index.lock."""

    try:
        descriptor = _open_nofollow(index_path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("Git index is not a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            digest.hexdigest(),
        )
    finally:
        os.close(descriptor)


def _index_plan_identity(identity: tuple[int, int, int, str]) -> list[str]:
    """Bind exact index bytes while tolerating Git's identical atomic rewrite."""

    return [str(identity[2]), identity[3]]


@contextmanager
def _exclusive_index_lock(index_path: Path):
    """Own Git's standard index lock for the full compare-and-publish window."""

    lock_path = index_path.with_name(index_path.name + ".lock")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise PermissionError("Git index is busy; retry after concurrent mutation") from exc
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assert_symbolic_head(repository: Path, expected_head_ref: str) -> None:
    """Require the exact attached branch that was approved for this commit."""

    actual = _git(repository, ["symbolic-ref", "-q", "HEAD"]).strip()
    if actual != expected_head_ref:
        raise PermissionError("Git symbolic HEAD changed after preflight")


def _assert_commit_effect_preconditions(
    repository: Path,
    arguments: Mapping[str, Any],
) -> None:
    """Check ref identity after redemption without reopening mutable paths."""

    _assert_symbolic_head(repository, arguments["expected_head_ref"])
    head = _git(repository, ["rev-parse", "HEAD"]).strip()
    if head != arguments["expected_head"]:
        raise PermissionError("Git HEAD changed after preflight")


def _worktree_hash(repository: Path) -> str:
    """Hash raw candidate bytes without asking Git to interpret worktree data."""

    digest = hashlib.sha256()
    object_format = _object_hash_format(repository)
    paths = _workspace_candidate_paths(repository)
    for entry in _worktree_entries(
        repository,
        paths,
        object_format=object_format,
    ):
        _update_entry_digest(
            digest,
            entry["path"],
            entry["mode"],
            entry["blob_oid"],
        )
    return digest.hexdigest()


def _status_hash(repository: Path) -> str:
    """Bind safe index metadata without invoking `git status` or `git diff`."""

    return _git_digest(
        repository,
        ["ls-files", "--stage", "-z"],
        max_bytes=_MAX_SNAPSHOT_STATUS_BYTES,
    )


def _workspace_candidate_paths(repository: Path) -> list[str]:
    """List paths whose raw worktree values affect the approval snapshot."""

    output = _git_output_bounded(
        repository,
        [
            "ls-files",
            "--modified",
            "--deleted",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        max_bytes=_MAX_SNAPSHOT_PATH_LIST_BYTES,
    )
    paths = sorted(
        _validated_snapshot_path(item)
        for item in output.decode("utf-8", errors="surrogateescape").split("\0")
        if item
    )
    if len(paths) > _MAX_SNAPSHOT_PATHS:
        raise ValueError("Git snapshot has too many worktree changes")
    return paths


def _validated_snapshot_path(value: str) -> str:
    """Validate a discovered path without applying write-time secret policy."""

    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
        raise PermissionError("Git path escapes repository")
    normalized = path.as_posix()
    if not normalized or any(character in normalized for character in "\x00\r\n\t"):
        raise PermissionError("Git path contains an unsafe index delimiter")
    return normalized


def _update_entry_digest(
    digest: Any,
    path: str,
    mode: str,
    blob_oid: str,
) -> None:
    """Frame each snapshot entry so adjacent field values cannot collide."""

    for value in (
        path.encode("utf-8", errors="surrogateescape"),
        mode.encode("ascii"),
        blob_oid.encode("ascii"),
    ):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)


def _oid(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", normalized):
        raise ValueError("Git snapshot digest is invalid")
    return normalized


def _oid_or_zero(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if _is_zero_oid(normalized):
        return normalized
    return _oid(normalized)


def _is_zero_oid(value: str) -> bool:
    """Recognize only supported all-zero Git object-ID widths."""

    return len(value) in {40, 64} and value == "0" * len(value)


def _assert_repository_oid_widths(
    repository: Path,
    arguments: Mapping[str, Any],
) -> None:
    """Bind receipt object IDs to the repository's actual hash format."""

    object_width = _object_oid_width(_object_hash_format(repository))
    for field in (
        "expected_head",
        "expected_tree",
        "expected_index_tree",
        "expected_restore_tree",
        "expected_branch_oid",
    ):
        value = arguments.get(field)
        if value and len(str(value)) != object_width:
            raise PermissionError(f"Git {field} does not match the repository object format")
    for entries_field in ("expected_path_entries", "expected_commit_entries"):
        for entry in arguments.get(entries_field, []):
            blob_oid = str(entry.get("blob_oid") or "")
            if blob_oid and len(blob_oid) != object_width:
                raise PermissionError(
                    "Git receipt blob does not match the repository object format"
                )
    for field in ("expected_status_hash", "expected_worktree_hash"):
        if len(str(arguments[field])) != 64:
            raise PermissionError(f"Git {field} is not a SHA-256 digest")


def _assert_repository_snapshot(
    repository: Path,
    arguments: Mapping[str, Any],
) -> None:
    head = _git(repository, ["rev-parse", "HEAD"]).strip()
    tree = _git(repository, ["rev-parse", "HEAD^{tree}"]).strip()
    status_hash = _status_hash(repository)
    index_tree = _git(repository, ["write-tree"]).strip()
    worktree_hash = _worktree_hash(repository)
    if (
        head != arguments["expected_head"]
        or tree != arguments["expected_tree"]
        or index_tree != arguments["expected_index_tree"]
        or status_hash != arguments["expected_status_hash"]
        or worktree_hash != arguments["expected_worktree_hash"]
    ):
        raise PermissionError("Git repository snapshot changed")


def _git(
    repository: Path,
    args: list[str],
    *,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *_safe_git_args(args)],
        input=input_text,
        stdin=subprocess.DEVNULL if input_text is None else None,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=dict(env) if env is not None else None,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip() or "Git write failed")
    return completed.stdout


def _git_bytes(
    repository: Path,
    args: list[str],
    *,
    input_bytes: bytes,
    env: Mapping[str, str] | None = None,
) -> bytes:
    """Run Git with immutable binary stdin, preserving exact staged bytes."""

    completed = subprocess.run(
        ["git", "-C", str(repository), *_safe_git_args(args)],
        input=input_bytes,
        stdin=None,
        capture_output=True,
        text=False,
        timeout=60,
        check=False,
        env=dict(env) if env is not None else None,
    )
    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")
        raise RuntimeError(output.strip() or "Git write failed")
    return completed.stdout


def _git_digest(repository: Path, args: list[str], *, max_bytes: int) -> str:
    """Hash complete bounded Git output instead of silently truncating it."""

    digest = hashlib.sha256()
    _git_stream(repository, args, max_bytes=max_bytes, consume=digest.update)
    return digest.hexdigest()


def _git_output_bounded(
    repository: Path,
    args: list[str],
    *,
    max_bytes: int,
) -> bytes:
    """Return complete bounded machine output; reject oversized snapshots."""

    chunks: list[bytes] = []
    _git_stream(repository, args, max_bytes=max_bytes, consume=chunks.append)
    return b"".join(chunks)


def _git_stream(
    repository: Path,
    args: list[str],
    *,
    max_bytes: int,
    consume: Callable[[bytes], None],
) -> None:
    """Stream Git stdout under a hard cap while draining stderr safely."""

    process = subprocess.Popen(
        ["git", "-C", str(repository), *_safe_git_args(args)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    total = 0
    diagnostics = bytearray()
    deadline = time.monotonic() + 60
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise RuntimeError("Git snapshot timed out")
            for key, _ in selector.select(remaining):
                data = os.read(key.fd, 64 * 1024)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    total += len(data)
                    if total > max_bytes:
                        process.kill()
                        process.communicate()
                        raise ValueError("Git snapshot output exceeds maximum size")
                    consume(data)
                elif len(diagnostics) < 256_000:
                    diagnostics.extend(data[: 256_000 - len(diagnostics)])
        if process.wait(timeout=1) != 0:
            message = diagnostics.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or "Git write failed")
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.communicate()


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _safe_git_args(args: list[str]) -> list[str]:
    """Disable repository-configured process hooks for local Git operations."""

    return [
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        *args,
    ]
