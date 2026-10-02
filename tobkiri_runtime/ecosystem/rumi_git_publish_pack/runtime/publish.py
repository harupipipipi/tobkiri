"""Host-brokered Git publication to an exact prepared remote state."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from core_runtime.executable_trust import (
    ExecutableTrustError,
    capture_trusted_executable,
    trusted_executable_path,
)
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_protocol.canonical import canonical_digest

AUTHORITY = "rumi.service.host.authorize.v1"
GIT_READ = "rumi.service.git.read.v1"
LEGACY_WORKSPACE = "rumi.resource.workspace.v1"
WORKSPACE = "tobkiri.resource.workspace.v1"
SERVICE_PACK_ID = "rumi_git_publish_pack"
FUNCTION_ID = "rumi_git_publish_pack.git-publish.service"
PREPARE_FUNCTION_ID = "rumi_git_publish_pack.git-push-prepare.service"
CONTRACT_ID = "tobkiri.service.git.publish.v1"
PREPARE_OPERATION = "rumi_git_publish_pack.git-push-prepare"
PUSH_OPERATION = "rumi_git_publish_pack.git-push"
WORKSPACE_GET_OPERATION = "rumi_workspace_mount_pack.workspace-resource"
GIT_CREDENTIAL_SCOPE = "git.publish"
GIT_CREDENTIAL_PROVIDER_INSTANCE_ID = "git-publish.service"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CLIENT_AUTHORITY_FIELDS = frozenset(
    {
        "approved",
        "approval",
        "approval_token",
        "authority_receipt",
        "authority_token",
        "credential",
        "credential_handle",
        "credential_secret",
        "password",
        "receipt",
        "secret",
        "token",
    }
)
_DANGEROUS_LOCAL_CONFIG = re.compile(
    r"^(?:"
    r"core\.(?:askpass|fsmonitor|hookspath|sshcommand)|"
    r"credential(?:\..+)?\..+|"
    r"http(?:\..+)?\.(?:cookiefile|extraheader|proxy|sslcert|sslkey)|"
    r"include\.path|includeif\..+\.path|"
    r"protocol\..+|push\..+|ssh\.variant|"
    r"url\..+\.(?:insteadof|pushinsteadof)|"
    r"remote\..+\.(?:proxy|proxycommand|pushoption|receivepack|uploadpack|vcs)"
    r")$",
    re.IGNORECASE,
)


class GitPushProviderV4:
    """Prepare and execute one exact Git push through a canonical CAS plan.

    The Git Publish Pack remains present in the default Profile, but the
    machine-local Git executable is an optional provider dependency.  A
    missing or untrusted executable leaves only this provider unavailable;
    it must never abort Profile capture for unrelated local-first features.
    """

    def __init__(self, capture: HostProviderCaptureContextV4) -> None:
        self._profile_id = capture.profile_id
        self._host_plan_digest = capture.plan_digest
        self._security_epoch = capture.security_epoch
        self._state_root = capture.state_root.resolve(strict=True)
        self._state_root_identity = _directory_identity(self._state_root)
        try:
            self._toolchain: dict[str, Any] | None = _git_toolchain_identity()
        except (ExecutableTrustError, OSError, RuntimeError, ValueError):
            self._toolchain = None

    def close(self) -> None:
        """Close the stateless provider; Broker grants own one-shot replay state."""

    def invoke(
        self,
        operation_id: str,
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        """Dispatch only the two explicit V4 publication operations."""

        _reject_client_authority(payload)
        context = invocation.envelope.context
        if (
            context.profile_id != self._profile_id
            or context.plan_digest != self._host_plan_digest
            or context.security_epoch != self._security_epoch
        ):
            raise PermissionError("Git publication Host binding changed")
        toolchain = self._require_toolchain()
        client = invocation.contract_client(
            allowed_contract_ids=frozenset({WORKSPACE}),
            consumer_pack_id=SERVICE_PACK_ID,
        )
        try:
            if operation_id == PREPARE_OPERATION:
                return self._prepare(payload, context, client, toolchain=toolchain)
            if operation_id == PUSH_OPERATION:
                return self._push(payload, context, client, toolchain=toolchain)
        except ExecutableTrustError:
            raise PermissionError("GIT_EXECUTABLE_UNAVAILABLE") from None
        raise ValueError("Git publication operation is unavailable")

    def _prepare(
        self,
        payload: Mapping[str, Any],
        context: Any,
        client: Any,
        *,
        toolchain: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        workspace_id = _required_text(payload.get("workspace_id"), "workspace_id")
        remote = _remote_name(payload.get("remote") or "origin")
        branch = _branch_name(payload.get("branch"))
        if payload.get("set_upstream"):
            raise PermissionError("Git push cannot mutate local upstream configuration")
        allow_non_fast_forward = _strict_bool(
            payload.get("force_with_lease", False),
            "force_with_lease",
        )
        root, repository, mount_revision = _v4_repository(
            client,
            profile_id=context.profile_id,
            workspace_id=workspace_id,
        )
        push_url = _push_url(repository, remote)
        credential_identity, _selection_receipt = _select_git_https_credential(
            client,
            workspace_id=workspace_id,
            push_url=push_url,
        )
        public_plan = _build_plan(
            root=root,
            repository=repository,
            mount_revision=mount_revision,
            workspace_id=workspace_id,
            remote=remote,
            branch=branch,
            allow_non_fast_forward=allow_non_fast_forward,
            context=context,
            state_root_identity=self._state_root_identity,
            toolchain=toolchain,
            push_url=push_url,
            credential_identity=credential_identity,
        )
        plan_digest = canonical_digest(public_plan)
        return {"plan": dict(public_plan), "plan_digest": plan_digest}

    def _push(
        self,
        payload: Mapping[str, Any],
        context: Any,
        client: Any,
        *,
        toolchain: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        plan_digest = _required_text(payload.get("plan_digest"), "plan_digest")
        supplied = payload.get("plan")
        if not isinstance(supplied, Mapping):
            raise ValueError("Git push plan is required")
        plan = dict(supplied)
        if canonical_digest(plan) != plan_digest:
            raise PermissionError("Git push plan digest is invalid")
        workspace_id = _required_text(plan.get("workspace_id"), "workspace_id")
        remote = _remote_name(plan.get("remote_name"))
        branch = _branch_name(plan.get("branch"))
        lease = plan.get("force_with_lease")
        allow_non_fast_forward = (
            lease.get("allow_non_fast_forward") if isinstance(lease, Mapping) else None
        )
        allow_non_fast_forward = _strict_bool(
            allow_non_fast_forward,
            "force_with_lease.allow_non_fast_forward",
        )
        _credential_handle_from_plan(plan)
        root, repository, mount_revision = _v4_repository(
            client,
            profile_id=context.profile_id,
            workspace_id=workspace_id,
        )
        if int(plan.get("mount_revision") or 0) != mount_revision:
            raise PermissionError(
                "workspace mount revision changed after Git push prepare"
            )
        push_url = _push_url(repository, remote)
        credential_identity, selection_receipt = _select_git_https_credential(
            client,
            workspace_id=workspace_id,
            push_url=push_url,
        )
        rebuilt = _build_plan(
            root=root,
            repository=repository,
            mount_revision=mount_revision,
            workspace_id=workspace_id,
            remote=remote,
            branch=branch,
            allow_non_fast_forward=allow_non_fast_forward,
            context=context,
            state_root_identity=self._state_root_identity,
            toolchain=toolchain,
            push_url=push_url,
            credential_identity=credential_identity,
        )
        if rebuilt != plan or canonical_digest(rebuilt) != plan_digest:
            raise PermissionError(
                "Git push compare-and-swap state changed after prepare"
            )
        output = _execute_force_with_lease(
            root=root,
            repository=repository,
            mount_revision=mount_revision,
            plan=plan,
            state_root=self._state_root,
            state_root_identity=self._state_root_identity,
            toolchain=toolchain,
            client=client,
            selection_receipt=selection_receipt,
        )
        return {
            "plan_digest": plan_digest,
            "workspace_id": plan["workspace_id"],
            "repository_root": plan["repository_root"],
            "remote": plan["remote_name"],
            "remote_host": plan["remote_host"],
            "branch": plan["branch"],
            "source_oid": plan["source_oid"],
            "expected_remote_oid": plan["remote_oid"],
            "published": True,
            "output": output,
        }

    def _assert_host_execution_identity(self) -> None:
        """Reject a changed Host state after a Git toolchain was captured."""

        if self._toolchain is None:
            raise PermissionError("GIT_EXECUTABLE_UNAVAILABLE")
        try:
            current_toolchain = _git_toolchain_identity()
        except (ExecutableTrustError, OSError, RuntimeError, ValueError):
            raise PermissionError("GIT_EXECUTABLE_UNAVAILABLE") from None
        if (
            _directory_identity(self._state_root) != self._state_root_identity
            or current_toolchain != self._toolchain
        ):
            raise PermissionError("Git Host execution identity changed")

    def _require_toolchain(self) -> dict[str, Any]:
        """Return the captured Git toolchain or one operation-level denial."""

        if self._toolchain is None:
            raise PermissionError("GIT_EXECUTABLE_UNAVAILABLE")
        self._assert_host_execution_identity()
        return dict(self._toolchain)


class GitPublishHostFactoryV4:
    """Capture one exact Git publication operation and Function identity."""

    def __init__(self, *, function_id: str, operation_id: str) -> None:
        self.function_id = function_id
        self.operation_id = operation_id

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Bind one operation to its verified Function principal and domain."""

        bindings = tuple(context.provider_bindings)
        if len(bindings) != 1 or any(
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != CONTRACT_ID
            or binding.operation.operation_id != self.operation_id
            for binding in bindings
        ):
            raise PermissionError("Git publication V4 bindings are incomplete")
        service = GitPushProviderV4(context)
        contributions: list[HostProviderContributionV4] = []
        for binding in bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                raise PermissionError("Git publication domain binding is unavailable")
            contributions.append(
                HostProviderContributionV4(
                    contract_id=binding.operation.contract_id,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=service.invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), service.close)


HOST_PROVIDER_FACTORY = {
    PREPARE_FUNCTION_ID: GitPublishHostFactoryV4(
        function_id=PREPARE_FUNCTION_ID,
        operation_id=PREPARE_OPERATION,
    ),
    FUNCTION_ID: GitPublishHostFactoryV4(
        function_id=FUNCTION_ID,
        operation_id=PUSH_OPERATION,
    ),
}


class GitPublishService:
    """Legacy V3 receipt adapter; never used by the V4 Host Provider path."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Push or dry-run an exact remote/branch pair."""
        if name not in {"push", "dry_run"}:
            raise ValueError(f"unknown Git publish operation: {name}")
        arguments = _arguments(payload, dry_run=name == "dry_run")
        root, repository = self._roots(payload)
        _assert_repository_oid_widths(repository, arguments)
        _assert_local_source(
            repository,
            arguments["branch"],
            arguments["expected_source_oid"],
        )
        remote_url = _git(
            repository, ["remote", "get-url", "--push", arguments["remote"]]
        ).strip()
        if remote_url != arguments["expected_remote_url"]:
            raise PermissionError("Git remote URL changed or was not preflighted")
        remote_host = _remote_host(arguments["expected_remote_url"])
        remote_url_hash = hashlib.sha256(
            arguments["expected_remote_url"].encode("utf-8")
        ).hexdigest()
        if arguments["expected_remote_url_hash"] != remote_url_hash:
            raise PermissionError("Git remote URL changed or was not preflighted")
        self._redeem(name, payload, arguments)
        _assert_local_source(
            repository,
            arguments["branch"],
            arguments["expected_source_oid"],
        )
        current_url = _git(
            repository, ["remote", "get-url", "--push", arguments["remote"]]
        ).strip()
        if current_url != arguments["expected_remote_url"]:
            raise PermissionError("Git remote URL changed after authorization")
        _assert_non_force_fast_forward(repository, arguments)
        args = ["push"]
        if arguments["dry_run"]:
            args.append("--dry-run")
        # A lease is required for both normal and force flows.  The normal
        # path separately proves its update is fast-forward before using the
        # lease, so this CAS option cannot turn an unapproved non-FF update
        # into a force push.
        args.append(
            "--force-with-lease="
            f"refs/heads/{arguments['branch']}:"
            f"{arguments['expected_remote_oid']}"
        )
        if arguments["set_upstream"]:
            args.append("--set-upstream")
        # Never push a mutable local branch name.  The source side is the
        # object ID sealed into the authority receipt; changing the branch
        # after approval cannot change the bytes that reach the remote.
        exact_refspec = (
            f"{arguments['expected_source_oid']}:refs/heads/{arguments['branch']}"
        )
        # Use the captured push URL itself.  Passing the remote name would
        # re-read .git/config inside `git push`, allowing a retarget after the
        # last hash check to choose a different network destination.
        args.extend(["--", arguments["expected_remote_url"], exact_refspec])
        output = _git(repository, args, timeout=180)
        return {
            "workspace_id": str(payload.get("workspace_id") or ""),
            "repository_root": (
                repository.relative_to(root).as_posix() if repository != root else "."
            ),
            "remote": arguments["remote"],
            "remote_host": remote_host,
            "remote_url": arguments["expected_remote_url"],
            "branch": arguments["branch"],
            "source_oid": arguments["expected_source_oid"],
            "expected_remote_oid": arguments["expected_remote_oid"],
            "force_with_lease": arguments["force_with_lease"],
            "dry_run": arguments["dry_run"],
            "published": not arguments["dry_run"],
            "output": output,
            "authority_receipt_redeemed": True,
        }

    def _roots(self, payload: Mapping[str, Any]) -> tuple[Path, Path]:
        common = {
            "profile_id": _profile(payload),
            "workspace_id": str(payload.get("workspace_id") or ""),
        }
        mount = self.client.invoke(LEGACY_WORKSPACE, "get", common)
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        if int(mount.get("mount_revision") or 0) != int(
            payload.get("expected_mount_revision") or -1
        ):
            raise PermissionError("workspace mount revision changed")
        root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        read = self.client.invoke(GIT_READ, "root", common)
        repository = (root / str(read.get("repository_root") or ".")).resolve(
            strict=True
        )
        try:
            repository.relative_to(root)
        except ValueError as exc:
            raise PermissionError("Git repository root escapes workspace") from exc
        return root, repository

    def _redeem(
        self, name: str, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"git.publish.{name}",
                "authority": "git.publish",
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
            raise PermissionError(str(result.get("reason") or "Git publication denied"))


def create_git_publish_operation(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated Git publication operations."""
    return GitPublishService(client).invoke


def _reject_client_authority(payload: Mapping[str, Any]) -> None:
    claims = _CLIENT_AUTHORITY_FIELDS.intersection(payload)
    if claims:
        raise PermissionError("client-supplied Git authority claims are denied")


def _required_text(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 512 or "\x00" in normalized:
        raise ValueError(f"Git {field} is invalid")
    return normalized


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Git {field} must be boolean")
    return value


def _credential_handle_from_plan(plan: Mapping[str, Any]) -> str | None:
    """Validate the immutable credential binding included in a push plan."""

    binding = plan.get("credential_transport")
    if not isinstance(binding, Mapping):
        raise PermissionError("Git credential transport binding is unavailable")
    mode = str(binding.get("mode") or "")
    identity = binding.get("credential_identity")
    if mode == "anonymous-https-only" and identity is None:
        if dict(binding) != {
            "mode": "anonymous-https-only",
            "credential_identity": None,
        }:
            raise PermissionError(
                "Git credential transport binding changed after prepare"
            )
        return None
    if mode != "host-bound-https" or not isinstance(identity, Mapping):
        raise PermissionError("Git credential transport binding changed after prepare")
    public_identity = dict(identity)
    binding_digest = str(public_identity.pop("binding_digest", ""))
    resource_binding = public_identity.get("resource_binding")
    handle = str(public_identity.get("handle") or "")
    expected_resource_binding = {
        "endpoint_origin": _https_origin(str(plan.get("push_url") or "")),
        "workspace_id": str(plan.get("workspace_id") or ""),
    }
    if (
        set(public_identity)
        != {
            "consumer_pack_id",
            "handle",
            "key_version",
            "profile_id",
            "provider_instance_id",
            "purpose",
            "resource_binding",
            "scope",
        }
        or not handle.startswith(("credential:", "opaque:"))
        or not str(public_identity.get("key_version") or "")
        or public_identity.get("consumer_pack_id") != SERVICE_PACK_ID
        or public_identity.get("provider_instance_id")
        != GIT_CREDENTIAL_PROVIDER_INSTANCE_ID
        or public_identity.get("scope") != GIT_CREDENTIAL_SCOPE
        or public_identity.get("purpose") != "provider.invoke"
        or not isinstance(resource_binding, Mapping)
        or dict(resource_binding) != expected_resource_binding
        or public_identity.get("profile_id")
        != dict(plan.get("authority_binding") or {}).get("profile_id")
        or binding_digest != canonical_digest(public_identity)
        or dict(binding)
        != {
            "mode": "host-bound-https",
            "credential_identity": dict(identity),
        }
    ):
        raise PermissionError("Git credential transport binding changed after prepare")
    return handle


def _https_origin(remote_url: str) -> str | None:
    """Return the canonical TLS origin for a validated Git remote URL."""

    parsed = urlparse(remote_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError as exc:
        raise PermissionError("Git remote port is invalid") from exc
    host = parsed.hostname.lower()
    rendered_host = f"[{host}]" if ":" in host else host
    port_text = "" if port in {None, 443} else f":{port}"
    return f"https://{rendered_host}{port_text}"


def _select_git_https_credential(
    client: Any,
    *,
    workspace_id: str,
    push_url: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Select an exact Host-owned credential identity for one remote."""

    endpoint_origin = _https_origin(push_url)
    if endpoint_origin is None:
        return None, None
    selected = client.select_git_https_credential(
        workspace_id=workspace_id,
        endpoint_origin=endpoint_origin,
        provider_instance_id=GIT_CREDENTIAL_PROVIDER_INSTANCE_ID,
        credential_scope=GIT_CREDENTIAL_SCOPE,
    )
    if selected is None:
        return None, None
    if not isinstance(selected, Mapping):
        raise PermissionError("Git credential selection is invalid")
    identity = dict(selected)
    selection_receipt = str(identity.pop("selection_receipt", ""))
    resource_binding = identity.get("resource_binding")
    expected_binding = {
        "endpoint_origin": endpoint_origin,
        "workspace_id": workspace_id,
    }
    public_identity = dict(identity)
    binding_digest = str(public_identity.pop("binding_digest", ""))
    if (
        set(public_identity)
        != {
            "consumer_pack_id",
            "handle",
            "key_version",
            "profile_id",
            "provider_instance_id",
            "purpose",
            "resource_binding",
            "scope",
        }
        or not str(public_identity.get("handle") or "").startswith(
            ("credential:", "opaque:")
        )
        or not str(public_identity.get("key_version") or "")
        or public_identity.get("consumer_pack_id") != SERVICE_PACK_ID
        or public_identity.get("provider_instance_id")
        != GIT_CREDENTIAL_PROVIDER_INSTANCE_ID
        or not str(public_identity.get("profile_id") or "")
        or public_identity.get("scope") != GIT_CREDENTIAL_SCOPE
        or public_identity.get("purpose") != "provider.invoke"
        or not isinstance(resource_binding, Mapping)
        or dict(resource_binding) != expected_binding
        or binding_digest != canonical_digest(public_identity)
        or not selection_receipt.startswith("credential-selection:")
    ):
        raise PermissionError("Git credential selection is invalid")
    return identity, selection_receipt


def _push_url(repository: Path, remote: str) -> str:
    """Read one actual push URL through the hardened captured Git toolchain."""

    return _git(
        repository,
        ["remote", "get-url", "--push", remote],
        hardened=True,
    ).strip()


def _remote_name(value: Any) -> str:
    normalized = str(value or "").strip()
    if not _REMOTE.fullmatch(normalized):
        raise ValueError("Git remote name is invalid")
    return normalized


def _branch_name(value: Any) -> str:
    normalized = str(value or "").strip()
    if (
        not _NAME.fullmatch(normalized)
        or normalized.startswith(("-", "/", "."))
        or normalized.endswith(("/", "."))
        or ".." in normalized
        or "//" in normalized
        or "@{" in normalized
        or "\\" in normalized
    ):
        raise ValueError("Git branch is invalid")
    return normalized


def _v4_repository(
    client: Any,
    *,
    profile_id: str,
    workspace_id: str,
) -> tuple[Path, Path, int]:
    mount = client.invoke(
        WORKSPACE,
        WORKSPACE_GET_OPERATION,
        {
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
        _git(
            root,
            ["rev-parse", "--show-toplevel"],
            hardened=True,
        ).strip()
    ).resolve(strict=True)
    try:
        repository.relative_to(root)
    except ValueError as exc:
        raise PermissionError("Git repository root escapes workspace") from exc
    return root, repository, mount_revision


def _repository_identity(root: Path, repository: Path) -> str:
    root_stat = root.stat()
    repository_stat = repository.stat()
    common_value = _git(
        repository,
        ["rev-parse", "--git-common-dir"],
        hardened=True,
    ).strip()
    common = Path(common_value)
    if not common.is_absolute():
        common = repository / common
    common = common.resolve(strict=True)
    common_stat = common.stat()
    return canonical_digest(
        {
            "root": str(root),
            "root_device": str(root_stat.st_dev),
            "root_inode": str(root_stat.st_ino),
            "repository": str(repository),
            "repository_device": str(repository_stat.st_dev),
            "repository_inode": str(repository_stat.st_ino),
            "git_common_dir": str(common),
            "git_common_device": str(common_stat.st_dev),
            "git_common_inode": str(common_stat.st_ino),
        }
    )


def _git_object_directory(repository: Path) -> Path:
    value = _git(
        repository,
        ["rev-parse", "--git-path", "objects"],
        hardened=True,
    ).strip()
    path = Path(value)
    if not path.is_absolute():
        path = repository / path
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise PermissionError("Git object directory is unavailable")
    return path


def _authority_binding(context: Any) -> dict[str, Any]:
    binding = {
        "profile_id": str(context.profile_id),
        "activation_id": str(context.activation_id),
        "activation_digest": str(context.activation_digest),
        "host_plan_digest": str(context.plan_digest),
        "security_epoch": int(context.security_epoch),
        "caller_principal_id": str(context.caller_principal.value),
        "caller_session_id": str(context.caller_session_id),
        "caller_domain_id": str(context.caller_domain_id),
    }
    if any(value in {"", 0} for value in binding.values()):
        raise PermissionError("Git Host authority binding is incomplete")
    return binding


def _build_plan(
    *,
    root: Path,
    repository: Path,
    mount_revision: int,
    workspace_id: str,
    remote: str,
    branch: str,
    allow_non_fast_forward: bool,
    context: Any,
    state_root_identity: str,
    toolchain: Mapping[str, Any],
    push_url: str,
    credential_identity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    _assert_safe_local_git_config(repository)
    source_oid = _git(
        repository,
        ["rev-parse", "--verify", f"refs/heads/{branch}"],
        hardened=True,
    ).strip()
    remote_host = _remote_host(push_url)
    remote_transport = "https" if push_url.startswith("https://") else "ssh"
    object_width = _object_oid_width(repository)
    source_oid = _oid_for_width(source_oid, object_width)
    remote_oid = _tracking_oid_or_zero(repository, remote, branch, object_width)
    _assert_prepared_fast_forward_policy(
        repository,
        remote_oid=remote_oid,
        source_oid=source_oid,
        allow_non_fast_forward=allow_non_fast_forward,
    )
    object_directory = _git_object_directory(repository)
    destination_ref = f"refs/heads/{branch}"
    return {
        "schema": "tobkiri.git-push-plan.v1",
        "authority_binding": _authority_binding(context),
        "host_state_root_identity": state_root_identity,
        "host_toolchain": dict(toolchain),
        "workspace_id": workspace_id,
        "mount_revision": mount_revision,
        "repository_root": (
            repository.relative_to(root).as_posix() if repository != root else "."
        ),
        "repository_identity": _repository_identity(root, repository),
        "object_directory_identity": _directory_identity(object_directory),
        "object_format": "sha256" if object_width == 64 else "sha1",
        "remote_name": remote,
        "remote_host": remote_host,
        "remote_transport": remote_transport,
        "push_url": push_url,
        "push_url_digest": hashlib.sha256(push_url.encode("utf-8")).hexdigest(),
        "branch": branch,
        "source_ref": f"refs/heads/{branch}",
        "source_oid": source_oid,
        "remote_oid": remote_oid,
        "destination_ref": destination_ref,
        "refspec": f"{source_oid}:{destination_ref}",
        "force_with_lease": {
            "mode": "exact-remote-oid",
            "allow_non_fast_forward": allow_non_fast_forward,
            "argument": f"--force-with-lease={destination_ref}:{remote_oid}",
        },
        "credential_transport": {
            "mode": (
                "host-bound-https"
                if credential_identity is not None
                else "anonymous-https-only"
            ),
            "credential_identity": (
                dict(credential_identity)
                if credential_identity is not None
                else None
            ),
        },
    }


def _oid_for_width(value: Any, width: int, *, allow_zero: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != width or not re.fullmatch(r"[0-9a-f]+", normalized):
        raise PermissionError("Git object ID does not match repository object format")
    if not allow_zero and normalized == "0" * width:
        raise PermissionError("Git source object ID cannot be zero")
    return normalized


def _tracking_oid_or_zero(
    repository: Path,
    remote: str,
    branch: str,
    object_width: int,
) -> str:
    completed = _run_git(
        repository,
        ["rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"],
        timeout=30,
        hardened=True,
    )
    if completed.returncode == 0:
        return _oid_for_width(completed.stdout.strip(), object_width, allow_zero=True)
    if completed.returncode == 1:
        return "0" * object_width
    output = (completed.stdout + completed.stderr).strip()
    raise RuntimeError(output or "Git remote tracking ref lookup failed")


def _assert_safe_local_git_config(repository: Path) -> None:
    completed = _run_git(
        repository,
        ["config", "--local", "--name-only", "--get-regexp", ".*"],
        timeout=30,
        hardened=True,
    )
    if completed.returncode not in {0, 1}:
        output = (completed.stdout + completed.stderr).strip()
        raise RuntimeError(output or "Git local configuration inspection failed")
    dangerous = [
        line.strip()
        for line in completed.stdout.splitlines()
        if _DANGEROUS_LOCAL_CONFIG.fullmatch(line.strip())
    ]
    if dangerous:
        raise PermissionError(
            "Git local configuration contains denied execution controls"
        )


def _revalidate_plan(
    plan: Mapping[str, Any],
    *,
    root: Path,
    repository: Path,
    mount_revision: int,
    state_root: Path,
    state_root_identity: str,
    toolchain: Mapping[str, Any],
) -> None:
    if int(plan["mount_revision"]) != mount_revision:
        raise PermissionError("workspace mount revision changed after Git push prepare")
    relative = repository.relative_to(root).as_posix() if repository != root else "."
    if relative != plan["repository_root"]:
        raise PermissionError("Git repository root changed after prepare")
    if _repository_identity(root, repository) != plan["repository_identity"]:
        raise PermissionError("Git repository identity changed after prepare")
    if (
        _directory_identity(_git_object_directory(repository))
        != plan["object_directory_identity"]
        or _directory_identity(state_root) != state_root_identity
        or plan["host_state_root_identity"] != state_root_identity
        or _git_toolchain_identity() != dict(toolchain)
        or plan["host_toolchain"] != dict(toolchain)
    ):
        raise PermissionError("Git Host execution identity changed after prepare")
    _assert_safe_local_git_config(repository)
    branch = _branch_name(plan["branch"])
    remote = _remote_name(plan["remote_name"])
    destination_ref = f"refs/heads/{branch}"
    source_oid = _git(
        repository,
        ["rev-parse", "--verify", f"refs/heads/{branch}"],
        hardened=True,
    ).strip()
    object_width = _object_oid_width(repository)
    source_oid = _oid_for_width(source_oid, object_width)
    remote_oid = _tracking_oid_or_zero(repository, remote, branch, object_width)
    push_url = _git(
        repository,
        ["remote", "get-url", "--push", remote],
        hardened=True,
    ).strip()
    remote_host = _remote_host(push_url)
    expected = {
        "source_ref": f"refs/heads/{branch}",
        "source_oid": source_oid,
        "remote_oid": remote_oid,
        "destination_ref": destination_ref,
        "refspec": f"{source_oid}:{destination_ref}",
        "push_url": push_url,
        "push_url_digest": hashlib.sha256(push_url.encode("utf-8")).hexdigest(),
        "remote_host": remote_host,
    }
    if any(plan[key] != value for key, value in expected.items()):
        raise PermissionError("Git push compare-and-swap state changed after prepare")
    lease = plan.get("force_with_lease")
    allow_non_fast_forward = (
        lease.get("allow_non_fast_forward") if isinstance(lease, Mapping) else None
    )
    if (
        not isinstance(lease, Mapping)
        or not isinstance(allow_non_fast_forward, bool)
        or dict(lease)
        != {
            "mode": "exact-remote-oid",
            "allow_non_fast_forward": allow_non_fast_forward,
            "argument": f"--force-with-lease={destination_ref}:{remote_oid}",
        }
    ):
        raise PermissionError("Git force-with-lease policy changed after prepare")
    _assert_prepared_fast_forward_policy(
        repository,
        remote_oid=remote_oid,
        source_oid=source_oid,
        allow_non_fast_forward=allow_non_fast_forward,
    )


def _assert_prepared_fast_forward_policy(
    repository: Path,
    *,
    remote_oid: str,
    source_oid: str,
    allow_non_fast_forward: bool,
) -> None:
    if allow_non_fast_forward or _is_zero_oid(remote_oid):
        return
    completed = _run_git(
        repository,
        ["merge-base", "--is-ancestor", remote_oid, source_oid],
        timeout=30,
        hardened=True,
    )
    if completed.returncode != 0:
        raise PermissionError("Git push is not a prepared fast-forward update")


def _execute_force_with_lease(
    *,
    root: Path,
    repository: Path,
    mount_revision: int,
    plan: Mapping[str, Any],
    state_root: Path,
    state_root_identity: str,
    toolchain: Mapping[str, Any],
    client: Any,
    selection_receipt: str | None,
) -> str:
    """Run the sole V4 mutation command after Broker-authorized plan replay."""

    if plan["remote_transport"] != "https":
        raise PermissionError("HOST_CREDENTIAL_PORT_UNAVAILABLE")
    credential_handle = _credential_handle_from_plan(plan)
    lease = plan["force_with_lease"]
    object_directory = _git_object_directory(repository)
    with tempfile.TemporaryDirectory(
        prefix="tobkiri-git-publish-",
        dir=state_root,
    ) as temporary:
        temporary_root = Path(temporary)
        template = temporary_root / "empty-template"
        bare = temporary_root / "transport.git"
        template.mkdir(mode=0o700)
        _git(
            temporary_root,
            [
                "init",
                "--bare",
                f"--object-format={plan['object_format']}",
                f"--template={template}",
                str(bare),
            ],
            hardened=True,
        )
        alternates = bare / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(f"{object_directory}\n", encoding="utf-8")
        _git(
            bare,
            ["cat-file", "-e", f"{plan['source_oid']}^{{commit}}"],
            hardened=True,
        )
        _revalidate_plan(
            plan,
            root=root,
            repository=repository,
            mount_revision=mount_revision,
            state_root=state_root,
            state_root_identity=state_root_identity,
            toolchain=toolchain,
        )
        if credential_handle is None:
            try:
                _git(
                    bare,
                    [
                        "push",
                        str(lease["argument"]),
                        "--",
                        str(plan["push_url"]),
                        str(plan["refspec"]),
                    ],
                    timeout=180,
                    hardened=True,
                )
            except RuntimeError as exc:
                message = str(exc).lower()
                if any(
                    marker in message
                    for marker in (
                        "authentication failed",
                        "could not read username",
                        "http 401",
                        "http 403",
                    )
                ):
                    raise PermissionError("HOST_CREDENTIAL_PORT_UNAVAILABLE") from None
                raise RuntimeError("Git publication failed") from None
        else:
            if not selection_receipt:
                raise PermissionError("HOST_CREDENTIAL_TRANSPORT_FAILED")
            try:
                client.push_git_https_with_credential(
                    git_executable=_git_executable(),
                    git_executable_identity=dict(toolchain["git"]),
                    bare_repository=str(bare),
                    remote_url=str(plan["push_url"]),
                    refspec=str(plan["refspec"]),
                    force_with_lease=str(lease["argument"]),
                    credential_handle=credential_handle,
                    provider_instance_id=GIT_CREDENTIAL_PROVIDER_INSTANCE_ID,
                    credential_scope=GIT_CREDENTIAL_SCOPE,
                    workspace_id=str(plan["workspace_id"]),
                    selection_receipt=selection_receipt,
                )
            except Exception:
                raise PermissionError("HOST_CREDENTIAL_TRANSPORT_FAILED") from None
    return "Git publication completed"


def _arguments(payload: Mapping[str, Any], *, dry_run: bool) -> dict[str, Any]:
    remote = str(payload.get("remote") or "origin").strip()
    branch = str(payload.get("branch") or "").strip()
    if not _REMOTE.fullmatch(remote):
        raise ValueError("Git remote name is invalid")
    if not _NAME.fullmatch(branch) or branch.startswith(("-", "/")) or ".." in branch:
        raise ValueError("Git branch is invalid")
    expected_source_oid = _oid(
        payload.get("expected_source_oid") or payload.get("expected_head")
    )
    expected_remote_oid = _oid(
        payload.get("expected_remote_oid"),
        allow_zero=True,
    )
    expected_mount_revision = int(payload.get("expected_mount_revision") or -1)
    if expected_mount_revision < 1:
        raise ValueError("expected_mount_revision is required")
    expected_remote_url = str(payload.get("expected_remote_url") or "").strip()
    _remote_host(expected_remote_url)
    expected_remote_url_hash = str(
        payload.get("expected_remote_url_hash") or ""
    ).strip()
    if (
        hashlib.sha256(expected_remote_url.encode("utf-8")).hexdigest()
        != expected_remote_url_hash
    ):
        raise ValueError("Git remote URL snapshot is invalid")
    return {
        "remote": remote,
        "branch": branch,
        "force_with_lease": bool(payload.get("force_with_lease", False)),
        "set_upstream": bool(payload.get("set_upstream", False)),
        "dry_run": dry_run,
        "expected_remote_url": expected_remote_url,
        "expected_remote_url_hash": expected_remote_url_hash,
        "expected_source_oid": expected_source_oid,
        "expected_remote_oid": expected_remote_oid,
        "expected_mount_revision": expected_mount_revision,
    }


def _oid(value: Any, *, allow_zero: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if allow_zero and _is_zero_oid(normalized):
        return normalized
    if not re.fullmatch(r"[0-9a-f]{40,64}", normalized):
        raise ValueError("Git object ID is invalid")
    return normalized


def _is_zero_oid(value: str) -> bool:
    """Recognize only supported all-zero Git object-ID widths."""

    return len(value) in {40, 64} and value == "0" * len(value)


def _object_oid_width(repository: Path) -> int:
    """Return the object-ID width selected by this repository."""

    object_format = _git(
        repository,
        ["rev-parse", "--show-object-format"],
        hardened=True,
    ).strip()
    if object_format == "sha1":
        return 40
    if object_format == "sha256":
        return 64
    raise PermissionError("Git object format is unsupported")


def _assert_repository_oid_widths(
    repository: Path,
    arguments: Mapping[str, Any],
) -> None:
    """Reject receipt OIDs whose width differs from the Git object format."""

    object_width = _object_oid_width(repository)
    for field in ("expected_source_oid", "expected_remote_oid"):
        if len(str(arguments[field])) != object_width:
            raise PermissionError(
                f"Git {field} does not match the repository object format"
            )


def _assert_local_source(
    repository: Path,
    branch: str,
    expected_source_oid: str,
) -> None:
    """Reject a changed source branch before its immutable OID is published."""

    current = _git(
        repository, ["rev-parse", "--verify", f"refs/heads/{branch}"]
    ).strip()
    if current != expected_source_oid:
        raise PermissionError("Git local source ref changed after preflight")


def _assert_non_force_fast_forward(
    repository: Path,
    arguments: Mapping[str, Any],
) -> None:
    """Require a normal leased push to remain a genuine fast-forward."""

    if arguments["force_with_lease"]:
        return
    expected_remote_oid = arguments["expected_remote_oid"]
    if _is_zero_oid(expected_remote_oid):
        return
    completed = _run_git(
        repository,
        [
            "merge-base",
            "--is-ancestor",
            expected_remote_oid,
            arguments["expected_source_oid"],
        ],
        timeout=30,
        hardened=True,
    )
    if completed.returncode != 0:
        raise PermissionError(
            "Git normal publication is not a fast-forward from the approved remote"
        )


def _remote_host(remote_url: str) -> str:
    value = str(remote_url or "").strip()
    if (
        not value
        or len(value) > 4_096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PermissionError("Git remote URL is invalid")
    if value.startswith(("file:", "/", "./", "../", "ext::")):
        raise PermissionError("local and external-helper Git remotes are denied")
    if "://" in value:
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"https", "ssh"}
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
        ):
            raise PermissionError("Git remote transport is denied")
        if parsed.password or (parsed.scheme == "https" and parsed.username):
            raise PermissionError("credential-bearing Git remote URLs are denied")
        try:
            port = parsed.port
        except ValueError as exc:
            raise PermissionError("Git remote port is invalid") from exc
        if port is not None and not 1 <= port <= 65_535:
            raise PermissionError("Git remote port is invalid")
        return _validated_network_host(parsed.hostname)
    scp_like = re.fullmatch(
        r"(?P<user>[A-Za-z0-9._-]{1,64})@"
        r"(?P<host>[A-Za-z0-9][A-Za-z0-9.-]{0,252}):"
        r"(?P<path>[^\s:][^\s]{0,4095})",
        value,
    )
    if scp_like is not None:
        return _validated_network_host(scp_like.group("host"))
    raise PermissionError("Git remote URL is not an approved network form")


def _validated_network_host(value: str) -> str:
    host = value.rstrip(".").lower()
    if not host or len(host) > 253 or host.startswith("-"):
        raise PermissionError("Git remote host is invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in labels
        ):
            raise PermissionError("Git remote host is invalid")
    return host


def _directory_identity(path: Path) -> str:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not resolved.is_dir():
        raise PermissionError("Git directory identity is unavailable")
    return canonical_digest(
        {
            "path": str(resolved),
            "device": str(info.st_dev),
            "inode": str(info.st_ino),
            "mode": str(info.st_mode),
        }
    )


def _executable_identity(path: Path) -> dict[str, Any]:
    """Capture POSIX or Windows executable trust evidence for one binary."""

    try:
        _resolved, identity = capture_trusted_executable(path)
    except PermissionError as exc:
        raise ExecutableTrustError("Git executable identity is unavailable") from exc
    return identity


def _trusted_executable(name: str, fixed_paths: tuple[Path, ...]) -> Path | None:
    candidates = [
        *fixed_paths,
        Path(found) if (found := shutil.which(name, path=os.defpath)) else None,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            try:
                return trusted_executable_path(candidate)
            except ExecutableTrustError as exc:
                raise ExecutableTrustError(
                    "Git executable is writable by an untrusted principal"
                ) from exc
    return None


def _ssh_executable() -> Path | None:
    return _trusted_executable("ssh", (Path("/usr/bin/ssh"),))


def _askpass_executable() -> Path | None:
    return _trusted_executable("false", (Path("/usr/bin/false"), Path("/bin/false")))


def _git_toolchain_identity() -> dict[str, Any]:
    ssh = _ssh_executable()
    askpass = _askpass_executable()
    return {
        "git": _executable_identity(Path(_git_executable())),
        "ssh": _executable_identity(ssh) if ssh is not None else None,
        "askpass": _executable_identity(askpass) if askpass is not None else None,
    }


def _hardened_git_environment() -> dict[str, str]:
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    for name in ("SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    ssh = _ssh_executable()
    if ssh is not None:
        environment["GIT_SSH_COMMAND"] = " ".join(
            [
                shlex.quote(str(ssh)),
                "-F",
                shlex.quote(os.devnull),
                "-oBatchMode=yes",
                "-oClearAllForwardings=yes",
                "-oIdentitiesOnly=yes",
                "-oIdentityAgent=none",
                "-oPermitLocalCommand=no",
            ]
        )
    askpass = _askpass_executable()
    if askpass is not None:
        environment["GIT_ASKPASS"] = str(askpass)
        environment["SSH_ASKPASS"] = str(askpass)
    return environment


def _git_executable() -> str:
    fixed = [Path("/usr/bin/git")]
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        fixed.extend(
            (
                Path(program_files) / "Git" / "bin" / "git.exe",
                Path(program_files) / "Git" / "cmd" / "git.exe",
            )
        )
    candidates = [
        *fixed,
        Path(found) if (found := shutil.which("git", path=os.defpath)) else None,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            try:
                return str(trusted_executable_path(candidate))
            except ExecutableTrustError as exc:
                raise ExecutableTrustError(
                    "Git executable is writable by an untrusted principal"
                ) from exc
    raise RuntimeError("trusted Git executable is unavailable")


def _run_git(
    repository: Path,
    args: list[str],
    *,
    timeout: int,
    hardened: bool,
) -> subprocess.CompletedProcess[str]:
    prefix = [_git_executable(), "-C", str(repository)]
    environment = None
    if hardened:
        prefix.extend(
            [
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "credential.helper=",
                "-c",
                "http.extraHeader=",
                "-c",
                "http.followRedirects=false",
                "-c",
                "push.followTags=false",
                "-c",
                "push.gpgSign=false",
                "-c",
                "push.recurseSubmodules=no",
                "-c",
                "push.useForceIfIncludes=false",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "protocol.ssh.allow=always",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                "protocol.file.allow=never",
            ]
        )
        environment = _hardened_git_environment()
    return subprocess.run(
        [*prefix, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=environment,
    )


def _git(
    repository: Path,
    args: list[str],
    *,
    timeout: int = 30,
    hardened: bool = False,
) -> str:
    completed = _run_git(
        repository,
        args,
        timeout=timeout,
        hardened=hardened,
    )
    output = (completed.stdout + completed.stderr)[:256_000]
    if completed.returncode != 0:
        raise RuntimeError(output.strip() or "Git publication failed")
    return output


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")
