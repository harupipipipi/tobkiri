"""Brokered canonical shell execution with receipt-gated legacy compatibility."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import stat
import sys
import time
from hmac import compare_digest
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from core_runtime.bounded_process_runner import (
    HostBoundedProcessRunner,
    ProcessExecutionPolicy,
)
from core_runtime.host_context import HostWorkspaceScope, require_host_workspace_scope
from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderInvocationContextV4,
)
from tobkiri_protocol.canonical import canonical_digest

AUTHORITY = "rumi.service.host.authorize.v1"
POLICY = "rumi.service.shell.inspect.v1"
WORKSPACE = "rumi.resource.workspace.v1"
SERVICE_PACK_ID = "rumi_shell_execute_pack"
_MAX_OUTPUT_BYTES = 128 * 1024
_MAX_TIMEOUT = 900
_ENV_ALLOWLIST = frozenset({"LANG", "LC_ALL", "TEMP", "TMP", "TMPDIR"})
_SECRET_ENV_WORDS = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "cookie",
)
_RAW_CREDENTIAL_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "approval_token",
        "authority_token",
        "credential",
        "credential_handle",
        "password",
        "secret",
        "token",
    }
)
_V4_PREPARE_FUNCTION_ID = "rumi_shell_execute_pack.shell-prepare.service"
_V4_EXECUTE_FUNCTION_ID = "rumi_shell_execute_pack.shell-execute.service"
_V4_CONTRACT_ID = "tobkiri.service.shell.execute.v1"
_V4_PREPARE_OPERATION = "rumi_shell_execute_pack.shell-prepare"
_V4_EXECUTE_OPERATION = "rumi_shell_execute_pack.shell-execute"
_V4_POLICY_CONTRACT = "tobkiri.service.shell.inspect.v1"
_V4_WORKSPACE_CONTRACT = "tobkiri.resource.workspace.v1"
_V4_POLICY_OPERATION = "rumi_shell_policy_pack.shell-inspect"
_V4_WORKSPACE_OPERATION = "rumi_workspace_mount_pack.workspace-resource"
_V4_DEPENDENCIES = frozenset({_V4_POLICY_CONTRACT, _V4_WORKSPACE_CONTRACT})
_V4_UNTRUSTED_AUTHORITY_FIELDS = frozenset(
    {
        "approved",
        "approval",
        "authority_receipt",
        "authority_token",
        "approval_token",
        "token",
    }
)
_V4_PLAN_VERSION = "tobkiri.shell-execute.plan.v4"
# Keep this finite set aligned with Command Protocol v1's terminal class. V4
# additionally requires a bare name resolved only through a Host-controlled path.
_V4_COMMAND_ALLOWLIST = frozenset(
    {
        "cargo",
        "git",
        "just",
        "mypy",
        "node",
        "npm",
        "npx",
        "python",
        "python3",
        "pytest",
        "ruff",
        "rustc",
        "swift",
        "true",
        "uv",
        "xcodebuild",
    }
)


class ShellExecuteService:
    """Execute one bounded command after exact Host authority redemption."""

    def __init__(
        self,
        client: Any,
        *,
        host_context: object | None = None,
    ) -> None:
        self.client = client
        self.host_context = (
            host_context if host_context is not None else getattr(client, "host_context", client)
        )
        self.runner = HostBoundedProcessRunner()

    def invoke(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one command; Host identity and approval are not payload data."""
        if name != "execute":
            raise ValueError(f"unknown shell execute operation: {name}")
        host_scope = self._host_scope()
        arguments = _arguments(payload)
        policy = self.client.invoke(POLICY, "classify", arguments)
        if not isinstance(policy, Mapping):
            raise PermissionError("shell policy response is invalid")
        if policy.get("shell_syntax") and not arguments["shell"]:
            raise PermissionError("shell syntax requires explicit shell mode")
        root = self._workspace(host_scope)
        cwd = _cwd(root, arguments["cwd"])
        argv = _execution_argv(arguments, cwd, root)
        environment = _host_environment(arguments["env"])
        process_policy = _process_policy(argv, cwd, environment)
        self._redeem(payload, arguments)
        started = time.monotonic()
        result = self.runner.run_local(
            argv=argv,
            cwd=cwd,
            stdin=None,
            timeout_seconds=arguments["timeout"],
            environment=environment,
            policy=process_policy,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        relative_cwd = cwd.relative_to(root).as_posix() if cwd != root else "."
        return {
            "command": arguments["command"],
            "workspace_id": host_scope.workspace_id,
            "cwd": relative_cwd,
            "classification": policy.get("classification"),
            "risk_reasons": list(policy.get("risk_reasons") or []),
            "approval_required": bool(policy.get("approval_required")),
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "timed_out": result.timed_out,
            "duration_ms": duration_ms,
            "executed": result.transport_error is None,
            "transport_error": result.transport_error,
            "authority_receipt_redeemed": True,
            "attestation": _attestation(result.attestation),
        }

    def _host_scope(self) -> HostWorkspaceScope:
        """Revalidate the Host envelope at every effect boundary."""

        return require_host_workspace_scope(self.host_context)

    def _workspace(self, host_scope: HostWorkspaceScope) -> Path:
        workspace_id = host_scope.workspace_id
        if not workspace_id:
            raise PermissionError("Host workspace binding is unavailable")
        mount = self.client.invoke(
            WORKSPACE,
            "get",
            {"workspace_id": workspace_id},
        )
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        root = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        if not root.is_dir():
            raise PermissionError("workspace root is unavailable")
        return root

    def _redeem(self, payload: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
        """Redeem only the opaque receipt and effect data at the boundary."""

        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": "shell.execute",
                "authority": "shell.execute",
                "arguments": dict(arguments),
            },
        )
        if not isinstance(result, Mapping) or not result.get("authorized"):
            reason = result.get("reason") if isinstance(result, Mapping) else None
            raise PermissionError(str(reason or "shell authority denied"))


def create_shell_execute_operation(
    client: Any,
    *,
    host_context: object | None = None,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated shell execution."""

    return ShellExecuteService(client, host_context=host_context).invoke


class ShellExecuteV4Service:
    """Prepare and execute exact shell plans through authenticated V4 dispatch."""

    def __init__(
        self,
        *,
        profile_id: str,
        host_binding: Mapping[str, Any],
    ) -> None:
        self.profile_id = str(profile_id)
        self.host_binding = dict(host_binding)
        self.runner = HostBoundedProcessRunner()

    def invoke(
        self,
        client: Any,
        name: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Dispatch one explicit prepare or execute operation."""

        _reject_v4_client_authority(payload)
        if name == _V4_PREPARE_OPERATION:
            plan = self._build_plan(
                client,
                _v4_arguments(payload),
            )
            redacted_plan = _public_plan(plan)
            return {
                "redacted_plan": redacted_plan,
                "plan_digest": _combined_plan_digest(plan, redacted_plan),
                "executed": False,
            }
        if name != _V4_EXECUTE_OPERATION:
            raise ValueError(f"unknown shell V4 operation: {name}")
        return self._execute(client, payload)

    def _execute(
        self,
        client: Any,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        redacted_plan = payload.get("redacted_plan")
        if not isinstance(redacted_plan, Mapping):
            raise ValueError("redacted canonical shell plan is required")
        expected_digest = str(payload.get("plan_digest") or "")
        _require_v4_digest(expected_digest)
        if redacted_plan.get("plan_version") != _V4_PLAN_VERSION:
            raise PermissionError("shell plan version is invalid")
        if redacted_plan.get("profile_id") != self.profile_id:
            raise PermissionError("shell plan profile binding changed")
        raw_arguments = payload.get("arguments")
        if not isinstance(raw_arguments, Mapping):
            raise ValueError("shell execution arguments are required")
        _reject_v4_client_authority(raw_arguments)
        rebuilt = self._build_plan(client, _v4_arguments(raw_arguments))
        rebuilt_redacted = _public_plan(rebuilt)
        rebuilt_digest = _combined_plan_digest(rebuilt, rebuilt_redacted)
        if rebuilt_redacted != dict(redacted_plan) or not compare_digest(
            rebuilt_digest, expected_digest
        ):
            raise PermissionError("shell plan changed after prepare")

        execution = rebuilt["execution"]
        argv = [str(item) for item in execution["argv"]]
        cwd = Path(str(execution["cwd_path"]))
        environment = {str(key): str(value) for key, value in execution["environment"].items()}
        process_policy = _process_policy(argv, cwd, environment)
        started = time.monotonic()
        result = self.runner.run_local(
            argv=argv,
            cwd=cwd,
            stdin=None,
            timeout_seconds=int(rebuilt["request"]["timeout"]),
            environment=environment,
            policy=process_policy,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        return {
            "command": rebuilt_redacted["request"]["command_name"],
            "command_digest": rebuilt_redacted["request"]["command_digest"],
            "workspace_id": rebuilt["workspace"]["workspace_id"],
            "cwd": execution["cwd"],
            "classification": rebuilt["policy"]["classification"],
            "risk_reasons": rebuilt["policy"]["risk_reasons"],
            "approval_required": rebuilt["policy"]["approval_required"],
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "timed_out": result.timed_out,
            "duration_ms": duration_ms,
            "executed": result.transport_error is None,
            "transport_error": result.transport_error,
            "plan_digest": expected_digest,
            "attestation": _attestation(result.attestation),
        }

    def _build_plan(
        self,
        client: Any,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw_argv = _argv(arguments["command"])
        if len(raw_argv) > 256 or sum(len(item.encode("utf-8")) for item in raw_argv) > 64 * 1024:
            raise ValueError("command arguments exceed the bounded schema")
        environment = _environment(arguments["env"])
        environment["PATH"] = _v4_host_search_path()
        if (
            sum(
                len(key.encode("utf-8")) + len(value.encode("utf-8"))
                for key, value in environment.items()
            )
            > 64 * 1024
        ):
            raise ValueError("shell environment exceeds the bounded schema")
        policy = client.invoke(
            _V4_POLICY_CONTRACT,
            _V4_POLICY_OPERATION,
            {"operation": "classify", **arguments},
        )
        if not isinstance(policy, Mapping):
            raise PermissionError("shell policy response is invalid")
        if policy.get("shell_syntax") and not arguments["shell"]:
            raise PermissionError("shell syntax requires explicit shell mode")
        workspace = _v4_workspace(client, self.profile_id)
        root = Path(workspace["canonical_root"])
        cwd = _cwd(root, str(arguments["cwd"]))
        executable_identity = _v4_resolve_executable(raw_argv[0])
        argv = [str(executable_identity["path"]), *raw_argv[1:]]
        relative_cwd = cwd.relative_to(root).as_posix() if cwd != root else "."
        return {
            "plan_version": _V4_PLAN_VERSION,
            "profile_id": self.profile_id,
            "host_binding": dict(self.host_binding),
            "request": dict(arguments),
            "policy": _v4_policy(policy),
            "workspace": workspace,
            "execution": {
                "cwd": relative_cwd,
                "cwd_path": str(cwd),
                "cwd_identity": _directory_identity(cwd),
                "argv": argv,
                "executable_identity": executable_identity,
                "environment": dict(sorted(environment.items())),
            },
            "allowlist": {
                "executables": [argv[0]],
                "argv": [argv],
                "cwd_paths": [str(cwd)],
                "environment_keys": sorted(environment),
                "max_stdin_bytes": 1,
                "max_stdout_bytes": _MAX_OUTPUT_BYTES,
                "max_stderr_bytes": _MAX_OUTPUT_BYTES,
                "max_timeout_seconds": _MAX_TIMEOUT,
                "allow_path_search": False,
            },
        }


class ShellExecuteHostFactoryV4:
    """Bind one canonical shell Function to one exact Host V4 operation."""

    def __init__(self, *, function_id: str, operation_id: str) -> None:
        self.function_id = function_id
        self.operation_id = operation_id

    def capture(
        self,
        context: HostProviderCaptureContextV4,
    ) -> CapturedHostProviderV4:
        """Capture exactly one operation principal and its dependencies."""

        bindings = tuple(context.provider_bindings)
        if (
            len(bindings) != 1
            or bindings[0].function.function_id != self.function_id
            or bindings[0].operation.contract_id != _V4_CONTRACT_ID
            or bindings[0].operation.operation_id != self.operation_id
        ):
            raise PermissionError("shell V4 provider bindings are incomplete")

        captured_profile_id = context.profile_id
        activation_id = str(context.activation.get("activation_id") or "")
        if not activation_id:
            raise PermissionError("shell V4 activation binding is unavailable")
        service = ShellExecuteV4Service(
            profile_id=captured_profile_id,
            host_binding={
                "activation_id": activation_id,
                "activation_plan_digest": context.plan_digest,
                "security_epoch": context.security_epoch,
            },
        )

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            if operation_id != self.operation_id:
                raise PermissionError("shell V4 operation identity is invalid")
            if invocation.envelope.context.profile_id != captured_profile_id:
                raise PermissionError("shell V4 profile binding changed")
            client = invocation.contract_client(
                allowed_contract_ids=_V4_DEPENDENCIES,
                consumer_pack_id=SERVICE_PACK_ID,
            )
            return service.invoke(
                client,
                operation_id,
                payload,
            )

        contributions: list[HostProviderContributionV4] = []
        for binding in bindings:
            key = (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            )
            domain_id = context.domain_ids.get(key)
            if domain_id is None:
                raise PermissionError("shell V4 domain binding is unavailable")
            contributions.append(
                HostProviderContributionV4(
                    contract_id=binding.operation.contract_id,
                    contract_version=binding.operation.contract_version,
                    operation_id=binding.operation.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain_id,
                    invoke=invoke,
                )
            )
        return CapturedHostProviderV4(tuple(contributions), lambda: None)


HOST_PROVIDER_FACTORY = {
    _V4_PREPARE_FUNCTION_ID: ShellExecuteHostFactoryV4(
        function_id=_V4_PREPARE_FUNCTION_ID,
        operation_id=_V4_PREPARE_OPERATION,
    ),
    _V4_EXECUTE_FUNCTION_ID: ShellExecuteHostFactoryV4(
        function_id=_V4_EXECUTE_FUNCTION_ID,
        operation_id=_V4_EXECUTE_OPERATION,
    ),
}


def _reject_v4_client_authority(payload: Mapping[str, Any]) -> None:
    for field_name in _V4_UNTRUSTED_AUTHORITY_FIELDS:
        if field_name in payload:
            raise PermissionError(f"client shell authority field is denied: {field_name}")


def _require_v4_digest(value: str) -> None:
    if (
        len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError("expected shell plan digest is invalid")


def _v4_arguments(payload: Mapping[str, Any]) -> dict[str, Any]:
    arguments = _arguments(payload)
    if arguments["shell"]:
        raise PermissionError("shell mode is unavailable through V4 execution")
    arguments["shell"] = False
    return arguments


def _v4_workspace(client: Any, profile_id: str) -> dict[str, Any]:
    snapshot = client.invoke(
        _V4_WORKSPACE_CONTRACT,
        _V4_WORKSPACE_OPERATION,
        {"operation": "list", "profile_id": profile_id},
    )
    if not isinstance(snapshot, Mapping):
        raise PermissionError("workspace snapshot is invalid")
    snapshot_profile = snapshot.get("profile_id")
    if snapshot_profile not in (None, "", profile_id):
        raise PermissionError("workspace profile binding changed")
    workspace_id = str(snapshot.get("selected_workspace_id") or "").strip()
    snapshot_revision = str(snapshot.get("revision") or "").strip()
    if not workspace_id or not snapshot_revision:
        raise PermissionError("Host-selected workspace is unavailable")
    mount = client.invoke(
        _V4_WORKSPACE_CONTRACT,
        _V4_WORKSPACE_OPERATION,
        {
            "operation": "get",
            "profile_id": profile_id,
            "workspace_id": workspace_id,
        },
    )
    if not isinstance(mount, Mapping):
        raise PermissionError("workspace mount is unavailable")
    mount_id = str(mount.get("id") or mount.get("workspace_id") or workspace_id)
    if mount_id != workspace_id:
        raise PermissionError("workspace mount identity changed")
    unresolved = Path(str(mount.get("root_path") or ""))
    if not unresolved.is_absolute() or unresolved.is_symlink():
        raise PermissionError("workspace root binding is invalid")
    root = unresolved.resolve(strict=True)
    if not root.is_dir():
        raise PermissionError("workspace root is unavailable")
    identity = _directory_identity(root)
    mount_revision = str(
        mount.get("mount_revision")
        or mount.get("revision")
        or mount.get("updated_at_ms")
        or mount.get("updated_at")
        or ""
    )
    if not mount_revision:
        raise PermissionError("workspace mount revision is unavailable")
    return {
        "workspace_id": workspace_id,
        "snapshot_revision": snapshot_revision,
        "mount_revision": mount_revision,
        "canonical_root": str(root),
        "root_identity": identity,
    }


def _v4_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    risk_reasons = policy.get("risk_reasons")
    if risk_reasons is None:
        risk_reasons = []
    if isinstance(risk_reasons, (str, bytes)) or not isinstance(risk_reasons, Sequence):
        raise PermissionError("shell policy risk reasons are invalid")
    if len(risk_reasons) > 64 or any(
        not isinstance(item, str) or len(item.encode("utf-8")) > 512 for item in risk_reasons
    ):
        raise PermissionError("shell policy risk reasons are invalid")
    normalized_command = policy.get("normalized_command")
    if normalized_command is not None and not isinstance(normalized_command, (str, list, tuple)):
        raise PermissionError("shell policy command is invalid")
    if isinstance(normalized_command, tuple):
        normalized_command = list(normalized_command)
    if isinstance(normalized_command, str) and len(normalized_command.encode("utf-8")) > 64 * 1024:
        raise PermissionError("shell policy command is invalid")
    if isinstance(normalized_command, list):
        if (
            len(normalized_command) > 256
            or any(not isinstance(item, str) for item in normalized_command)
            or sum(len(item.encode("utf-8")) for item in normalized_command) > 64 * 1024
        ):
            raise PermissionError("shell policy command is invalid")
    for field_name in ("classification", "risk_level", "command_hash"):
        value = policy.get(field_name)
        if value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > 512):
            raise PermissionError(f"shell policy {field_name} is invalid")
    return {
        "classification": str(policy.get("classification") or "unknown"),
        "risk_level": str(policy.get("risk_level") or "unknown"),
        "risk_reasons": sorted({str(item) for item in risk_reasons}),
        "read_only": bool(policy.get("read_only")),
        "approval_required": bool(policy.get("approval_required")),
        "shell_syntax": bool(policy.get("shell_syntax")),
        "command_hash": str(policy.get("command_hash") or ""),
        "normalized_command": normalized_command,
    }


def _directory_identity(path: Path) -> dict[str, int | str]:
    metadata = path.stat()
    return {
        "device": str(metadata.st_dev),
        "inode": str(metadata.st_ino),
        "mode": int(metadata.st_mode),
    }


def _v4_resolve_executable(requested_name: str) -> dict[str, Any]:
    name = str(requested_name or "").strip()
    if not name or name != Path(name).name or name not in _V4_COMMAND_ALLOWLIST or "\x00" in name:
        raise PermissionError("Host executable is not allowlisted")
    resolved_value = shutil.which(name, path=_v4_host_search_path())
    if resolved_value is None:
        raise PermissionError("Host executable is unavailable")
    resolved = Path(resolved_value).resolve(strict=True)
    _require_host_controlled_executable(resolved)
    metadata = resolved.stat()
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "requested_name": name,
        "path": str(resolved),
        "sha256": "sha256:" + digest.hexdigest(),
        "size": int(metadata.st_size),
        "device": str(metadata.st_dev),
        "inode": str(metadata.st_ino),
        "mode": int(stat.S_IMODE(metadata.st_mode)),
    }


def _require_host_controlled_executable(executable: Path) -> None:
    metadata = executable.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and metadata.st_mode & 0o111 == 0)
        or metadata.st_mode & 0o022
    ):
        raise PermissionError("Host executable is not immutable")
    if os.name == "nt":
        system_root = Path(str(os.environ.get("SystemRoot") or "")).resolve()
        try:
            executable.relative_to(system_root / "System32")
        except ValueError as exc:
            raise PermissionError("Host executable root is not trusted") from exc
        return
    effective_uid = os.geteuid()
    current = executable
    while True:
        current_metadata = current.stat()
        if current_metadata.st_mode & 0o022 or (
            current_metadata.st_uid == effective_uid and current_metadata.st_mode & stat.S_IWUSR
        ):
            raise PermissionError("Host executable root is user-writable")
        if current.parent == current:
            break
        current = current.parent


def _v4_host_search_path() -> str:
    if os.name != "nt":
        return os.defpath
    system_root = Path(str(os.environ.get("SystemRoot") or ""))
    return str(system_root / "System32")


def _combined_plan_digest(
    execution_plan: Mapping[str, Any],
    redacted_plan: Mapping[str, Any],
) -> str:
    return canonical_digest(
        {
            "execution_plan": dict(execution_plan),
            "redacted_plan": dict(redacted_plan),
        }
    )


def _public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    request = plan["request"]
    policy = plan["policy"]
    workspace = plan["workspace"]
    execution = plan["execution"]
    executable = execution["executable_identity"]
    return {
        "plan_version": _V4_PLAN_VERSION,
        "profile_id": plan["profile_id"],
        "workspace": {
            "workspace_id": workspace["workspace_id"],
            "snapshot_revision": workspace["snapshot_revision"],
            "mount_revision": workspace["mount_revision"],
        },
        "request": {
            "command_name": executable["requested_name"],
            "command_digest": canonical_digest(request["command"]),
            "argument_count": len(execution["argv"]),
            "cwd": execution["cwd"],
            "timeout": request["timeout"],
            "shell": False,
            "environment_keys": sorted(execution["environment"]),
        },
        "policy": {
            "classification": policy["classification"],
            "risk_level": policy["risk_level"],
            "risk_reasons": policy["risk_reasons"],
            "read_only": policy["read_only"],
            "approval_required": policy["approval_required"],
            "command_hash": policy["command_hash"],
        },
        "executable": {
            "requested_name": executable["requested_name"],
            "path": executable["path"],
            "sha256": executable["sha256"],
        },
    }


def _arguments(payload: Mapping[str, Any]) -> dict[str, Any]:
    for field_name in _RAW_CREDENTIAL_FIELDS:
        if field_name in payload and payload.get(field_name) not in (None, ""):
            raise PermissionError(f"raw shell credential is denied: {field_name}")
    command = payload.get("command")
    if not isinstance(command, (str, list, tuple)) or not command:
        raise ValueError("command is required")
    timeout = max(1, min(_MAX_TIMEOUT, int(payload.get("timeout") or 30)))
    env = payload.get("env")
    if env is not None and not isinstance(env, Mapping):
        raise ValueError("shell environment must be an object")
    normalized_command: str | list[str]
    if isinstance(command, (list, tuple)):
        normalized_command = [str(item) for item in command]
    else:
        normalized_command = command
    return {
        "command": normalized_command,
        "cwd": str(payload.get("cwd") or "."),
        "timeout": timeout,
        "shell": bool(payload.get("shell", False)),
        "env": {str(key): str(value) for key, value in (env or {}).items()},
    }


def _cwd(root: Path, value: str) -> Path:
    raw = Path(str(value or "."))
    if raw.is_absolute():
        raise PermissionError("absolute shell cwd is denied")
    resolved = (root / raw).resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError("shell cwd escapes workspace") from exc
    if not resolved.is_dir():
        raise NotADirectoryError("shell cwd is not a directory")
    return resolved


def _argv(command: Any) -> list[str]:
    if isinstance(command, list):
        result = [str(item) for item in command]
    else:
        result = shlex.split(str(command), posix=sys.platform != "win32")
    if not result or any("\x00" in item for item in result):
        raise ValueError("command arguments are invalid")
    return result


def _execution_argv(
    arguments: Mapping[str, Any],
    cwd: Path,
    root: Path,
) -> list[str]:
    command = arguments["command"]
    if arguments["shell"]:
        if not isinstance(command, str) or "\x00" in command:
            raise ValueError("shell command must be text without NUL bytes")
        return _shell_argv(command, cwd, root)
    argv = _argv(command)
    argv[0] = _resolve_executable(argv[0], cwd, root)
    return argv


def _shell_argv(command: str, cwd: Path, root: Path) -> list[str]:
    del cwd, root
    if os.name == "nt":
        shell = Path(r"C:\Windows\System32\cmd.exe")
    else:
        shell = Path("/bin/sh")
    if not shell.is_file() or not os.access(shell, os.X_OK):
        raise PermissionError("Host shell executable is unavailable")
    return [str(shell), "/c" if os.name == "nt" else "-c", command]


def _resolve_executable(value: str, cwd: Path, root: Path) -> str:
    raw = str(value or "").strip()
    if not raw or "\x00" in raw:
        raise ValueError("process executable is invalid")
    if os.path.isabs(raw):
        candidate = Path(raw)
    elif "/" in raw or "\\" in raw:
        candidate = (cwd / raw).resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise PermissionError("relative executable escapes workspace") from exc
    else:
        resolved = shutil.which(raw, path=_host_search_path())
        if resolved is None:
            raise FileNotFoundError(f"Host executable is unavailable: {raw}")
        candidate = Path(resolved).resolve(strict=True)
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise PermissionError("Host executable is unavailable")
    return str(candidate)


def _host_search_path() -> str:
    """Return a deterministic search path without reading ambient PATH."""

    executable_dir = Path(sys.executable).resolve().parent
    entries = [str(executable_dir)]
    entries.extend(item for item in os.defpath.split(os.pathsep) if item)
    return os.pathsep.join(dict.fromkeys(entries))


def _environment(overrides: Mapping[str, str]) -> dict[str, str]:
    """Build an empty-by-default environment from an explicit allowlist."""

    environment: dict[str, str] = {}
    for raw_key, value in overrides.items():
        key = str(raw_key)
        upper = key.upper()
        if _secret_key(key):
            raise PermissionError(f"secret shell environment key is denied: {key}")
        if upper == "PATH":
            raise PermissionError("shell PATH is Host-owned")
        if upper not in _ENV_ALLOWLIST:
            raise PermissionError(f"shell environment key is denied: {key}")
        if "\x00" in key or "\x00" in value:
            raise ValueError("shell environment contains a NUL byte")
        environment[upper] = str(value)
    return environment


def _host_environment(overrides: Mapping[str, str]) -> dict[str, str]:
    environment = _environment(overrides)
    environment["PATH"] = _host_search_path()
    return environment


def _secret_key(key: str) -> bool:
    lower = key.casefold()
    return any(word in lower for word in _SECRET_ENV_WORDS)


def _process_policy(
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
) -> ProcessExecutionPolicy:
    exact_argv = tuple(argv)
    return ProcessExecutionPolicy(
        allowed_executables=frozenset({exact_argv[0]}),
        allowed_argv=(exact_argv,),
        allowed_cwds=(cwd,),
        allowed_environment=frozenset(environment),
        max_stdin_bytes=1,
        max_stdout_bytes=_MAX_OUTPUT_BYTES,
        max_stderr_bytes=_MAX_OUTPUT_BYTES,
        max_timeout_seconds=_MAX_TIMEOUT,
        allow_path_search=False,
    )


def _attestation(value: Any) -> dict[str, Any]:
    return {
        "authority": str(getattr(value, "authority", "")),
        "boundary": str(getattr(value, "boundary", "")),
        "sandboxed": bool(getattr(value, "sandboxed", False)),
        "process_tree_kill": str(getattr(value, "process_tree_kill", "")),
    }
