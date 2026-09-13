from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from core_runtime.bounded_process_runner import (
    HostBoundedProcessRunner,
    ProcessExecutionPolicy,
)
from ..errors import (
    SANDBOX_RESOURCE_CONTROLLER_UNAVAILABLE,
    SANDBOX_RUNTIME_UNAVAILABLE,
)
from ..policy import validate_workspace_relative_path
from .bubblewrap_builder import build_bubblewrap_argv
from .cgroup import build_systemd_run_argv, probe_systemd_user_scope
from .lima_runtime import (
    LIMA_GUEST_WORKSPACE_ROOT,
    LIMA_GUEST_PACK_DATA_ROOT,
    build_guest_bwrap_argv,
    resolve_attested_lima_runtime,
)
from .spec import BubblewrapSandboxSpec, CgroupLimits, WorkspaceMount


MAX_SANDBOX_OUTPUT_BYTES = 1024 * 1024
MAX_SANDBOX_TERMINAL_OUTPUT_BYTES = 256 * 1024
MAX_STAGE_FILES = 1024
MAX_STAGE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_STAGE_FILE_BYTES = 2 * 1024 * 1024
MAX_CODING_WORKSPACE_EXPORT_BYTES = 128 * 1024 * 1024
MAX_CODING_WORKSPACE_EXPORT_FILES = 8000
MAX_CODING_WORKSPACE_EXPORT_FILE_BYTES = 4 * 1024 * 1024
GUEST_TIMEOUT_EXIT_CODE = 124
SANDBOX_ROOT_MARKER = ".rumi-sandbox-root"
PACK_DATA_MIGRATION_MARKER = ".rumi-host-pack-data-migration-v1"
CHILD_PROCESS_POLICY_ENV = "RUMI_SANDBOX_DENY_CHILD_PROCESS"


def _run_bounded_process(
    command: Sequence[str],
    *,
    input_data: str | bytes | None = None,
    timeout: float,
    cwd: Path | str | None = None,
    environment: Mapping[str, str] | None = None,
    max_stdin_bytes: int = 64 * 1024 * 1024,
    max_stdout_bytes: int = MAX_SANDBOX_OUTPUT_BYTES,
    max_stderr_bytes: int = MAX_SANDBOX_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Execute one exact Host transport command through the shared boundary."""
    argv = tuple(str(item) for item in command)
    if not argv:
        raise ValueError("sandbox transport command is empty")
    executable = argv[0]
    if not Path(executable).is_absolute():
        resolved_executable = (
            shutil.which(executable, path=environment.get("PATH"))
            if environment is not None
            else shutil.which(executable)
        )
        if resolved_executable is None:
            raise FileNotFoundError(argv[0])
        executable = resolved_executable
    executable = str(Path(executable).resolve())
    argv = (executable, *argv[1:])
    process_cwd = Path(cwd or Path.cwd()).resolve()
    source_environment = environment if environment is not None else os.environ
    process_environment = {
        str(key): str(value)
        for key, value in source_environment.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and key
        and "=" not in key
        and "\x00" not in key
        and "\x00" not in value
    }
    bounded_timeout = min(max(float(timeout), 1.0), 3600.0)
    result = HostBoundedProcessRunner().run_local(
        argv=argv,
        cwd=process_cwd,
        stdin=input_data,
        timeout_seconds=bounded_timeout,
        environment=process_environment,
        policy=ProcessExecutionPolicy(
            allowed_executables=frozenset({executable}),
            allowed_argv=(argv,),
            allowed_cwds=(process_cwd,),
            allowed_environment=frozenset(process_environment),
            max_stdin_bytes=max_stdin_bytes,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            max_timeout_seconds=bounded_timeout,
        ),
    )
    if result.timed_out:
        raise subprocess.TimeoutExpired(
            cmd=list(argv),
            timeout=bounded_timeout,
            output=result.stdout,
            stderr=result.stderr,
        )
    if result.exit_code is None:
        raise RuntimeError(result.transport_error or "sandbox transport failed")
    completed = subprocess.CompletedProcess(
        args=list(argv),
        returncode=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
    )
    completed.stdout_truncated = result.stdout_truncated  # type: ignore[attr-defined]
    completed.stderr_truncated = result.stderr_truncated  # type: ignore[attr-defined]
    return completed


def _run_bounded_process_to_file(
    command: Sequence[str],
    *,
    stdout_path: Path,
    timeout: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int = MAX_SANDBOX_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Stream one exact Host transport stdout into a bounded new file."""
    argv = tuple(str(item) for item in command)
    if not argv:
        raise ValueError("sandbox transport command is empty")
    executable = argv[0]
    if not Path(executable).is_absolute():
        resolved_executable = shutil.which(executable)
        if resolved_executable is None:
            raise FileNotFoundError(argv[0])
        executable = resolved_executable
    executable = str(Path(executable).resolve())
    argv = (executable, *argv[1:])
    cwd = Path.cwd().resolve()
    environment = {
        str(key): str(value)
        for key, value in os.environ.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and key
        and "=" not in key
        and "\x00" not in key
        and "\x00" not in value
    }
    bounded_timeout = min(max(float(timeout), 1.0), 3600.0)
    result = HostBoundedProcessRunner().run_local_to_file(
        argv=argv,
        cwd=cwd,
        stdin=None,
        timeout_seconds=bounded_timeout,
        environment=environment,
        policy=ProcessExecutionPolicy(
            allowed_executables=frozenset({executable}),
            allowed_argv=(argv,),
            allowed_cwds=(cwd,),
            allowed_environment=frozenset(environment),
            max_stdin_bytes=1,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            max_timeout_seconds=bounded_timeout,
        ),
        stdout_path=stdout_path.resolve(),
    )
    if result.timed_out:
        raise subprocess.TimeoutExpired(
            cmd=list(argv),
            timeout=bounded_timeout,
            output="",
            stderr=result.stderr,
        )
    completed = subprocess.CompletedProcess(
        args=list(argv),
        returncode=result.exit_code if result.exit_code is not None else 1,
        stdout="",
        stderr=result.stderr,
    )
    completed.stdout_truncated = result.stdout_truncated  # type: ignore[attr-defined]
    return completed


class ManagedSandboxSupervisor:
    """Execute untrusted functions inside Bubblewrap plus a systemd cgroup."""

    def __init__(self, provider_registry: Any | None = None) -> None:
        self.provider_registry = provider_registry

    def available(self) -> bool:
        return bool(diagnose_sandbox_environment()["ready"])

    def execute_capability(self, request: dict[str, Any]) -> dict[str, Any]:
        if platform.system().lower() == "darwin":
            return self._execute_capability_lima(request)
        diagnostics = diagnose_sandbox_environment(request)
        if not diagnostics["ready"]:
            failed = _first_failed_sandbox_check(diagnostics)
            message = str(failed.get("message") or "Managed sandbox runtime is unavailable")
            code = str(failed.get("code") or SANDBOX_RUNTIME_UNAVAILABLE)
            if code == SANDBOX_RESOURCE_CONTROLLER_UNAVAILABLE:
                return self._unavailable(
                    request,
                    message,
                    error_type=SANDBOX_RESOURCE_CONTROLLER_UNAVAILABLE,
                    diagnostics=diagnostics,
                )
            return self._unavailable(
                request,
                message,
                error_type=SANDBOX_RUNTIME_UNAVAILABLE,
                diagnostics=diagnostics,
            )

        timeout = _bounded_timeout(request.get("timeout_seconds"))
        sandbox_id = _sandbox_id(request)
        with tempfile.TemporaryDirectory(prefix=f"{sandbox_id}-") as tmp:
            temp_root = Path(tmp)
            workspace = temp_root / "workspace"
            function_target = workspace / "function"
            workspace.mkdir(mode=0o700)

            module_rel, callable_name, stage_audit = self._stage_function(
                request=request,
                function_target=function_target,
            )
            runner_path = self._stage_runner(request, workspace)
            input_path = workspace / "input.json"
            context_payload: dict[str, Any] = (
                request["context"] if isinstance(request.get("context"), dict) else {}
            )
            args_payload: dict[str, Any] = (
                request["args"] if isinstance(request.get("args"), dict) else {}
            )
            input_path.write_text(
                _runner_payload(
                    module_path=f"/workspace/function/{module_rel.as_posix()}",
                    callable_name=callable_name,
                    context=context_payload,
                    args=args_payload,
                ),
                encoding="utf-8",
            )

            immutable_root = _immutable_root(request)
            seccomp_profile = str(request.get("seccomp_profile") or "").strip()
            try:
                seccomp_profile_path = _required_file(seccomp_profile, "seccomp_profile") if seccomp_profile else None
                spec = BubblewrapSandboxSpec(
                    sandbox_id=sandbox_id,
                    profile_id=str(request.get("profile_runtime") or request.get("principal_id") or "default"),
                    immutable_root=immutable_root,
                    workspace=WorkspaceMount(source=workspace, read_only=False),
                    argv=("python3", f"/workspace/{runner_path.name}", "--input-file", "/workspace/input.json"),
                    env={
                        "RUMI_PROFILE_RUNTIME": str(request.get("profile_runtime") or ""),
                        CHILD_PROCESS_POLICY_ENV: "1",
                    },
                    network_enabled=False,
                )
                bwrap_argv = build_bubblewrap_argv(spec)
                sandbox_command, stdout_path, stderr_path, returncode_path = _sandbox_wrapper_command(
                    temp_root=temp_root,
                    bwrap_argv=bwrap_argv,
                    seccomp_profile=seccomp_profile_path,
                )
                unit_name = f"rumi-sandbox-{sandbox_id}"
                command = build_systemd_run_argv(
                    unit_name,
                    CgroupLimits(runtime_max_sec=int(timeout)),
                    sandbox_command,
                )
                systemd_proc = _run_bounded_process(
                    command,
                    timeout=timeout + 2,
                )
                proc = _completed_from_wrapper_files(
                    command=command,
                    systemd_proc=systemd_proc,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    returncode_path=returncode_path,
                )
            except subprocess.TimeoutExpired:
                return {
                    "success": False,
                    "ok": False,
                    "error": "Managed sandbox execution timed out",
                    "error_type": "timeout",
                    "execution_boundary": "managed_sandbox",
                }

            return self._response_from_process(proc, stage_audit=stage_audit)

    def _execute_capability_lima(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            limactl, instance = resolve_attested_lima_runtime()
        except ValueError as exc:
            return self._unavailable(
                request,
                str(exc),
                error_type=SANDBOX_RUNTIME_UNAVAILABLE,
                diagnostics=diagnose_sandbox_environment(request),
            )
        timeout = _bounded_timeout(request.get("timeout_seconds"))
        sandbox_id = _sandbox_id(request)
        remote_root = (
            f"{LIMA_GUEST_WORKSPACE_ROOT}/.rumi-capability-{uuid.uuid4().hex}"
        )
        try:
            with tempfile.TemporaryDirectory(prefix=f"{sandbox_id}-") as tmp:
                workspace = Path(tmp) / "workspace"
                function_target = workspace / "function"
                workspace.mkdir(mode=0o700)
                module_rel, callable_name, stage_audit = self._stage_function(
                    request=request,
                    function_target=function_target,
                )
                runner_path = self._stage_runner(request, workspace)
                context_payload: dict[str, Any] = (
                    request["context"] if isinstance(request.get("context"), dict) else {}
                )
                args_payload: dict[str, Any] = (
                    request["args"] if isinstance(request.get("args"), dict) else {}
                )
                (workspace / "input.json").write_text(
                    _runner_payload(
                        module_path=f"/workspace/function/{module_rel.as_posix()}",
                        callable_name=callable_name,
                        context=context_payload,
                        args=args_payload,
                    ),
                    encoding="utf-8",
                )
                import_proc = _lima_import_workspace(
                    limactl=limactl,
                    instance=instance,
                    remote_root=remote_root,
                    archive=_tar_directory(workspace),
                    timeout=timeout,
                )
                if import_proc.returncode != 0:
                    return self._unavailable(
                        request,
                        _decode_bytes(import_proc.stderr) or "Could not stage the Lima sandbox workspace",
                        error_type=SANDBOX_RUNTIME_UNAVAILABLE,
                    )
                guest_argv = build_guest_bwrap_argv(
                    workspace=remote_root,
                    cwd="/workspace",
                    argv=("python3", f"/workspace/{runner_path.name}", "--input-file", "/workspace/input.json"),
                    env={
                        "HOME": "/home",
                        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONNOUSERSITE": "1",
                        CHILD_PROCESS_POLICY_ENV: "1",
                        "RUMI_PROFILE_RUNTIME": str(request.get("profile_runtime") or ""),
                        "RUMI_SANDBOX_ID": sandbox_id,
                    },
                    network_enabled=False,
                )
                guest_argv = _guest_resource_limited_argv(
                    guest_argv,
                    timeout=timeout,
                    memory_mb=512,
                    pids=128,
                )
                proc = _run_bounded_process(
                    [limactl, "shell", instance, "--", *guest_argv],
                    timeout=timeout + 2,
                )
                if proc.returncode == GUEST_TIMEOUT_EXIT_CODE:
                    return {
                        "success": False,
                        "ok": False,
                        "error": "Managed sandbox execution timed out",
                        "error_type": "timeout",
                        "execution_boundary": "managed_sandbox",
                    }
                return self._response_from_process(proc, stage_audit=stage_audit)
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "ok": False,
                "error": "Managed sandbox execution timed out",
                "error_type": "timeout",
                "execution_boundary": "managed_sandbox",
            }
        except Exception as exc:
            return self._unavailable(request, str(exc), error_type=SANDBOX_RUNTIME_UNAVAILABLE)
        finally:
            _lima_remove_workspace(limactl, instance, remote_root)

    def execute_coding_terminal(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run a coding command inside an isolated staged workspace."""
        system = platform.system().lower()
        if system == "darwin":
            return self._execute_coding_terminal_lima(request)
        return self._execute_coding_terminal_bwrap(request)

    def execute_pack_process(self, request: dict[str, Any]) -> dict[str, Any]:
        """Stage one Pack tree and run its declared process entrypoint."""
        pack_dir = _required_dir(request.get("pack_dir"), "pack_dir")
        pack_id = str(request.get("pack_id") or pack_dir.name).strip()
        module = str(request.get("module") or "").strip()
        if not pack_id or not module:
            raise ValueError("pack_id and module are required")
        active_profile_id = _validated_profile_context(
            request.get("active_profile_id")
        )
        data_scope = f"{active_profile_id or 'unbound'}--{pack_id}"
        with tempfile.TemporaryDirectory(prefix="rumi-pack-process-") as tmp:
            workspace = Path(tmp)
            target = workspace / "ecosystem" / pack_id
            target.parent.mkdir(parents=True, mode=0o700)
            stage_audit = _stage_regular_tree(pack_dir, target)
            runtime_root = pack_dir.parent.parent
            core_runtime = _required_dir(runtime_root / "core_runtime", "core_runtime")
            core_audit = _stage_regular_tree(core_runtime, workspace / "core_runtime")
            stage_audit = {
                "files": stage_audit["files"] + core_audit["files"],
                "bytes": stage_audit["bytes"] + core_audit["bytes"],
            }
            if stage_audit["files"] > MAX_STAGE_FILES:
                raise ValueError("Pack process stage has too many files")
            if stage_audit["bytes"] > MAX_STAGE_TOTAL_BYTES:
                raise ValueError("Pack process stage is too large")
            response = self.execute_coding_terminal(
                {
                    "workspace_root": str(workspace),
                    "cwd": ".",
                    "argv": ["python3", "-B", "-s", "-E", "-m", module],
                    "stdin": str(request.get("stdin") or ""),
                    "timeout_seconds": request.get("timeout_seconds"),
                    "network_enabled": False,
                    "immutable_root": request.get("immutable_root"),
                    "profile_runtime": request.get("profile_runtime"),
                    "active_profile_id": request.get("active_profile_id"),
                    "host_user_data_dir": request.get("host_user_data_dir"),
                    "host_pack_data_dir": request.get("host_pack_data_dir"),
                    "pack_id": pack_id,
                    "guest_data_dir": (
                        f"{LIMA_GUEST_PACK_DATA_ROOT}/{_safe_guest_name(data_scope)}"
                    ),
                    # This workspace is an ephemeral staged Pack tree. Pack
                    # state persists only through guest_data_dir, so copying
                    # the staged tree back to Host has no valid consumer.
                    "export_workspace": False,
                }
            )
            if response.get("stdout_truncated") is True:
                response.update(
                    {
                        "success": False,
                        "ok": False,
                        "exit_code": None,
                        "returncode": None,
                        "stdout": "",
                        "error_type": "response_too_large",
                        "error": "Pack process response exceeded the output limit",
                    }
                )
            response["sandbox_stage"] = stage_audit
            return response

    def _execute_coding_terminal_bwrap(self, request: dict[str, Any]) -> dict[str, Any]:
        diagnostics = diagnose_sandbox_environment(request)
        if not diagnostics["ready"]:
            failed = _first_failed_sandbox_check(diagnostics)
            return self._unavailable(
                request,
                str(failed.get("message") or "Managed sandbox runtime is unavailable"),
                error_type=str(failed.get("code") or SANDBOX_RUNTIME_UNAVAILABLE),
                diagnostics=diagnostics,
            )
        timeout = _bounded_timeout(request.get("timeout_seconds"))
        sandbox_id = _sandbox_id(request)
        workspace = _required_dir(request.get("workspace_root"), "workspace_root")
        cwd = validate_workspace_relative_path(request.get("cwd", "."), field="cwd")
        command_argv = _coding_command_argv(request)
        try:
            immutable_root = _immutable_root(request)
            host_pack_data = str(request.get("host_pack_data_dir") or "").strip()
            data_mount = None
            sandbox_env = _coding_sandbox_env(sandbox_id)
            if host_pack_data:
                data_path = _required_dir(host_pack_data, "host_pack_data_dir")
                if data_path.is_symlink():
                    raise ValueError("host_pack_data_dir must not be a symlink")
                data_mount = WorkspaceMount(
                    source=data_path,
                    target="/data",
                    read_only=False,
                )
                sandbox_env["RUMI_USER_DATA"] = "/data"
            spec = BubblewrapSandboxSpec(
                sandbox_id=sandbox_id,
                profile_id=str(request.get("profile_runtime") or request.get("principal_id") or "coding"),
                immutable_root=immutable_root,
                workspace=WorkspaceMount(source=workspace, read_only=False),
                argv=tuple(command_argv),
                data=data_mount,
                env=sandbox_env,
                network_enabled=bool(request.get("network_enabled") is True),
            )
            bwrap_argv = build_bubblewrap_argv(spec)
            if cwd != ".":
                marker = bwrap_argv.index("--chdir")
                bwrap_argv[marker + 1] = "/workspace/" + cwd
            with tempfile.TemporaryDirectory(prefix=f"{sandbox_id}-term-") as tmp:
                temp_root = Path(tmp)
                sandbox_command, stdout_path, stderr_path, returncode_path = _sandbox_wrapper_command(
                    temp_root=temp_root,
                    bwrap_argv=bwrap_argv,
                    seccomp_profile=None,
                )
                command = build_systemd_run_argv(
                    f"rumi-sandbox-terminal-{sandbox_id}",
                    CgroupLimits(runtime_max_sec=int(timeout)),
                    sandbox_command,
                )
                systemd_proc = _run_bounded_process(
                    command,
                    timeout=timeout + 2,
                )
                proc = _completed_from_wrapper_files(
                    command=command,
                    systemd_proc=systemd_proc,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    returncode_path=returncode_path,
                )
        except subprocess.TimeoutExpired:
            return _coding_terminal_response(
                sandbox_id=sandbox_id,
                command=request.get("command") or request.get("argv"),
                returncode=None,
                stdout="",
                stderr="Managed sandbox terminal timed out",
                timed_out=True,
            )
        except Exception as exc:
            return self._unavailable(
                request,
                str(exc),
                error_type=SANDBOX_RUNTIME_UNAVAILABLE,
            )
        return _coding_terminal_response(
            sandbox_id=sandbox_id,
            command=request.get("command") or request.get("argv"),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            timed_out=False,
        )

    def _execute_coding_terminal_lima(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            limactl, instance = resolve_attested_lima_runtime()
        except ValueError as exc:
            return self._unavailable(
                request,
                str(exc),
                error_type=SANDBOX_RUNTIME_UNAVAILABLE,
            )
        sandbox_id = _sandbox_id(request)
        timeout = _bounded_timeout(request.get("timeout_seconds"))
        workspace = _required_dir(request.get("workspace_root"), "workspace_root")
        cwd = validate_workspace_relative_path(request.get("cwd", "."), field="cwd")
        remote_root = f"{LIMA_GUEST_WORKSPACE_ROOT}/.rumi-coding-{uuid.uuid4().hex}"
        guest_data_dir = str(request.get("guest_data_dir") or "").strip() or None
        try:
            if guest_data_dir is not None:
                _lima_ensure_data_dir(
                    limactl=limactl,
                    instance=instance,
                    data_dir=guest_data_dir,
                    timeout=timeout,
                )
                host_user_data_dir = str(
                    request.get("host_user_data_dir") or ""
                ).strip()
                pack_id = str(request.get("pack_id") or "").strip()
                if host_user_data_dir and pack_id:
                    _lima_migrate_pack_data(
                        limactl=limactl,
                        instance=instance,
                        data_dir=guest_data_dir,
                        host_user_data_dir=Path(host_user_data_dir),
                        pack_id=pack_id,
                        timeout=timeout,
                    )
            archive = _tar_directory(workspace)
            import_proc = _lima_import_workspace(
                limactl=limactl,
                instance=instance,
                remote_root=remote_root,
                archive=archive,
                timeout=timeout,
            )
            if import_proc.returncode != 0:
                return _coding_terminal_response(
                    sandbox_id=sandbox_id,
                    command=request.get("command") or request.get("argv"),
                    returncode=import_proc.returncode,
                    stdout="",
                    stderr=_decode_bytes(import_proc.stderr),
                    timed_out=False,
                    success=False,
                )
            remote_cwd = "/workspace" if cwd == "." else "/workspace/" + cwd
            sandbox_env = _coding_sandbox_env(sandbox_id)
            if guest_data_dir is not None:
                sandbox_env["RUMI_USER_DATA"] = "/data"
            guest_argv = build_guest_bwrap_argv(
                workspace=remote_root,
                cwd=remote_cwd,
                argv=_coding_command_argv(request),
                env=sandbox_env,
                network_enabled=bool(request.get("network_enabled") is True),
                data_dir=guest_data_dir,
            )
            guest_argv = _guest_resource_limited_argv(
                guest_argv,
                timeout=timeout,
                memory_mb=_bounded_positive_int(request.get("memory_mb"), 512, 128, 8192),
                pids=_bounded_positive_int(request.get("pids"), 128, 16, 1024),
            )
            proc = _run_bounded_process(
                [limactl, "shell", instance, "--", *guest_argv],
                input_data=str(request.get("stdin") or ""),
                timeout=timeout + 2,
                max_stdout_bytes=MAX_SANDBOX_TERMINAL_OUTPUT_BYTES,
                max_stderr_bytes=MAX_SANDBOX_TERMINAL_OUTPUT_BYTES,
            )
            if proc.returncode == GUEST_TIMEOUT_EXIT_CODE:
                return _coding_terminal_response(
                    sandbox_id=sandbox_id,
                    command=request.get("command") or request.get("argv"),
                    returncode=None,
                    stdout=proc.stdout or "",
                    stderr="Lima sandbox terminal timed out",
                    timed_out=True,
                    provider_id="lima_ubuntu",
                )
            if request.get("export_workspace") is not False:
                with tempfile.TemporaryDirectory(prefix=f"{sandbox_id}-lima-export-") as export_tmp:
                    export_path = Path(export_tmp) / "workspace.tar"
                    export_proc = _run_bounded_process_to_file(
                        [
                            limactl,
                            "shell",
                            instance,
                            "--",
                            "tar",
                            "-cf",
                            "-",
                            "-C",
                            remote_root,
                            ".",
                        ],
                        stdout_path=export_path,
                        timeout=timeout + 2,
                        max_stdout_bytes=MAX_CODING_WORKSPACE_EXPORT_BYTES,
                    )
                    if export_proc.returncode == 0:
                        if getattr(export_proc, "stdout_truncated", False) is True:
                            return _coding_terminal_response(
                                sandbox_id=sandbox_id,
                                command=request.get("command") or request.get("argv"),
                                returncode=1,
                                stdout=proc.stdout or "",
                                stderr="Lima sandbox export exceeded workspace size quota",
                                timed_out=False,
                                success=False,
                                provider_id="lima_ubuntu",
                            )
                        _replace_directory_from_tar(workspace, export_path)
        except subprocess.TimeoutExpired:
            return _coding_terminal_response(
                sandbox_id=sandbox_id,
                command=request.get("command") or request.get("argv"),
                returncode=None,
                stdout="",
                stderr="Lima sandbox terminal timed out",
                timed_out=True,
            )
        except Exception as exc:
            return self._unavailable(request, str(exc), error_type=SANDBOX_RUNTIME_UNAVAILABLE)
        finally:
            _lima_remove_workspace(limactl, instance, remote_root)
        return _coding_terminal_response(
            sandbox_id=sandbox_id,
            command=request.get("command") or request.get("argv"),
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            timed_out=False,
            provider_id="lima_ubuntu",
        )

    def _stage_function(self, *, request: dict[str, Any], function_target: Path) -> tuple[Path, str, dict[str, int]]:
        function_dir = _required_dir(request.get("function_dir"), "function_dir")
        main_py_path = request.get("main_py_path")
        entrypoint = str(request.get("entrypoint") or "main.py:run")
        entry_file, callable_name = (
            entrypoint.rsplit(":", 1) if ":" in entrypoint else (entrypoint, "run")
        )
        if main_py_path:
            main_path = Path(str(main_py_path)).expanduser().resolve()
        else:
            main_path = (function_dir / entry_file).resolve()
        try:
            module_rel = main_path.relative_to(function_dir)
        except ValueError as exc:
            raise ValueError("Sandbox function entrypoint escapes function directory") from exc
        if not main_path.is_file():
            raise ValueError("Sandbox function entrypoint not found")
        stage_audit = _stage_regular_tree(function_dir, function_target)
        return module_rel, callable_name or "run", stage_audit

    def _stage_runner(self, request: dict[str, Any], workspace: Path) -> Path:
        source = _required_file(request.get("runner_path"), "runner_path")
        target = workspace / "function_runner.py"
        shutil.copy2(source, target)
        return target

    def _response_from_process(self, proc: subprocess.CompletedProcess[str], *, stage_audit: dict[str, int] | None = None) -> dict[str, Any]:
        stdout = proc.stdout or ""
        stderr = (proc.stderr or "").strip()
        if (
            getattr(proc, "stdout_truncated", False) is True
            or len(stdout.encode("utf-8")) > MAX_SANDBOX_OUTPUT_BYTES
        ):
            return {
                "success": False,
                "ok": False,
                "error": "Managed sandbox response too large",
                "error_type": "response_too_large",
                "execution_boundary": "managed_sandbox",
                "sandbox_stage": dict(stage_audit or {}),
            }
        output = stdout.strip()
        parsed: Any = None
        if output:
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError:
                return {
                    "success": False,
                    "ok": False,
                    "error": "Managed sandbox output is not valid JSON",
                    "error_type": "invalid_json_output",
                    "execution_boundary": "managed_sandbox",
                    "sandbox_stage": dict(stage_audit or {}),
                }
        if proc.returncode != 0:
            if isinstance(parsed, dict) and parsed.get("error"):
                error_text = str(parsed.get("error") or "")
                error_type = str(parsed.get("error_type") or "function_execution_error")
            else:
                error_text = f"Managed sandbox exited {proc.returncode}: {stderr}"[:1000]
                error_type = "function_execution_error"
            return {
                "success": False,
                "ok": False,
                "error": error_text,
                "error_type": error_type,
                "execution_boundary": "managed_sandbox",
                "sandbox_stage": dict(stage_audit or {}),
            }
        return {
            "success": True,
            "ok": True,
            "output": parsed,
            "execution_boundary": "managed_sandbox",
            "sandbox_stage": dict(stage_audit or {}),
        }

    def _unavailable(
        self,
        request: dict[str, Any],
        message: str,
        *,
        error_type: str = SANDBOX_RUNTIME_UNAVAILABLE,
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "success": False,
            "ok": False,
            "error": message,
            "error_type": error_type,
            "execution_boundary": "managed_sandbox",
            "exit_code": None,
            "returncode": None,
            "stdout": "",
            "stderr": message,
            "timed_out": error_type == "timeout",
            "request": {
                "profile_runtime": request.get("profile_runtime"),
                "pack_id": request.get("pack_id"),
                "function_id": request.get("function_id"),
                "calling_convention": request.get("calling_convention"),
            },
        }
        if diagnostics is not None:
            payload["diagnostics"] = diagnostics
        return payload


def diagnose_sandbox_environment(request: dict[str, Any] | None = None) -> dict[str, Any]:
    request = request if isinstance(request, dict) else {}
    if platform.system().lower() == "darwin":
        try:
            limactl, instance = resolve_attested_lima_runtime()
        except ValueError as exc:
            return {
                "ready": False,
                "checks": [
                    {
                        "name": "lima_guest_sandbox",
                        "ok": False,
                        "code": SANDBOX_RUNTIME_UNAVAILABLE,
                        "message": str(exc),
                    }
                ],
            }
        return {
            "ready": True,
            "checks": [
                {
                    "name": "lima_guest_sandbox",
                    "ok": True,
                    "path": limactl,
                    "instance": instance,
                    "code": SANDBOX_RUNTIME_UNAVAILABLE,
                    "message": "Attested Lima guest sandbox is ready",
                }
            ],
        }
    checks: list[dict[str, Any]] = []

    bwrap_path = shutil.which("bwrap")
    checks.append(
        {
            "name": "bubblewrap",
            "ok": bwrap_path is not None,
            "path": bwrap_path,
            "code": SANDBOX_RUNTIME_UNAVAILABLE,
            "message": (
                "Bubblewrap sandbox runtime is available"
                if bwrap_path is not None
                else "Bubblewrap sandbox runtime is not installed"
            ),
        }
    )

    systemd_probe = probe_systemd_user_scope()
    systemd_check: dict[str, Any] = {
        "name": "systemd_user_scope",
        "ok": systemd_probe.ok,
        "path": systemd_probe.path,
        "code": SANDBOX_RESOURCE_CONTROLLER_UNAVAILABLE,
        "message": systemd_probe.message,
    }
    if systemd_probe.returncode is not None:
        systemd_check["returncode"] = systemd_probe.returncode
    if systemd_probe.stderr:
        systemd_check["stderr"] = systemd_probe.stderr
    checks.append(systemd_check)

    try:
        immutable_root = _immutable_root(request)
        checks.append(
            {
                "name": "immutable_root",
                "ok": True,
                "path": str(immutable_root),
                "marker": str(immutable_root / SANDBOX_ROOT_MARKER),
                "code": SANDBOX_RUNTIME_UNAVAILABLE,
                "message": "Immutable sandbox root is configured",
            }
        )
    except Exception as exc:
        checks.append(
            {
                "name": "immutable_root",
                "ok": False,
                "path": str(request.get("immutable_root") or os.environ.get("RUMI_SANDBOX_IMMUTABLE_ROOT") or ""),
                "marker": SANDBOX_ROOT_MARKER,
                "code": SANDBOX_RUNTIME_UNAVAILABLE,
                "message": _sandbox_root_error_message(exc),
            }
        )

    return {
        "ready": all(bool(check.get("ok")) for check in checks),
        "checks": checks,
    }


def _first_failed_sandbox_check(diagnostics: dict[str, Any]) -> dict[str, Any]:
    checks = diagnostics.get("checks") if isinstance(diagnostics, dict) else []
    if isinstance(checks, list):
        for check in checks:
            if isinstance(check, dict) and not bool(check.get("ok")):
                return check
    return {
        "name": "managed_sandbox",
        "ok": False,
        "code": SANDBOX_RUNTIME_UNAVAILABLE,
        "message": "Managed sandbox runtime is unavailable",
    }


def _sandbox_root_error_message(exc: Exception) -> str:
    text = str(exc)
    prefix = "SANDBOX_RUNTIME_UNAVAILABLE:"
    if text.startswith(prefix):
        text = text[len(prefix):].strip()
    if "not configured" in text:
        return (
            "Immutable sandbox root is not configured; set "
            "RUMI_SANDBOX_IMMUTABLE_ROOT or pass a server-side immutable_root"
        )
    if text:
        return f"Immutable sandbox root is invalid: {text}"
    return "Immutable sandbox root is invalid"


def _runner_payload(*, module_path: str, callable_name: str, context: dict[str, Any], args: dict[str, Any]) -> str:
    return json.dumps(
        {
            "module_path": module_path,
            "callable_name": callable_name,
            "context": context,
            "args": args,
        },
        ensure_ascii=False,
        default=str,
    )


def _sandbox_wrapper_command(
    *,
    temp_root: Path,
    bwrap_argv: list[str],
    seccomp_profile: Path | None,
) -> tuple[list[str], Path, Path, Path]:
    wrapper = temp_root / "run_bwrap_with_seccomp.py"
    argv_file = temp_root / "bwrap_argv.json"
    stdout_path = temp_root / "sandbox.stdout"
    stderr_path = temp_root / "sandbox.stderr"
    returncode_path = temp_root / "sandbox.returncode"
    argv_file.write_text(
        json.dumps(
            {
                "argv": bwrap_argv,
                "seccomp_profile": str(seccomp_profile) if seccomp_profile is not None else "",
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "returncode_path": str(returncode_path),
                "output_limit": MAX_SANDBOX_TERMINAL_OUTPUT_BYTES + 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    wrapper.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import json",
                "import os",
                "import subprocess",
                "import sys",
                "import threading",
                "payload = json.load(open(sys.argv[1], encoding='utf-8'))",
                "argv = list(payload['argv'])",
                "pass_fds = ()",
                "profile = str(payload.get('seccomp_profile') or '')",
                "if profile:",
                "    fd = os.open(profile, os.O_RDONLY)",
                "    os.set_inheritable(fd, True)",
                "    pass_fds = (fd,)",
                "    try:",
                "        index = argv.index('--')",
                "    except ValueError:",
                "        index = len(argv)",
                "    argv[index:index] = ['--seccomp', str(fd)]",
                "limit = int(payload['output_limit'])",
                "def drain(source, target):",
                "    written = 0",
                "    while True:",
                "        chunk = source.read(8192)",
                "        if not chunk:",
                "            break",
                "        remaining = max(0, limit - written)",
                "        if remaining:",
                "            accepted = chunk[:remaining]",
                "            target.write(accepted)",
                "            target.flush()",
                "            written += len(accepted)",
                "with open(payload['stdout_path'], 'xb') as out, open(payload['stderr_path'], 'xb') as err:",
                "    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, pass_fds=pass_fds)",
                "    threads = [threading.Thread(target=drain, args=(proc.stdout, out)), threading.Thread(target=drain, args=(proc.stderr, err))]",
                "    [thread.start() for thread in threads]",
                "    returncode = proc.wait()",
                "    [thread.join() for thread in threads]",
                "open(payload['returncode_path'], 'w', encoding='utf-8').write(str(returncode))",
                "raise SystemExit(returncode)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(wrapper, 0o700)
    os.chmod(argv_file, 0o600)
    return [sys.executable, str(wrapper), str(argv_file)], stdout_path, stderr_path, returncode_path


def _completed_from_wrapper_files(
    *,
    command: list[str],
    systemd_proc: subprocess.CompletedProcess[str],
    stdout_path: Path,
    stderr_path: Path,
    returncode_path: Path,
) -> subprocess.CompletedProcess[str]:
    if not returncode_path.is_file():
        return subprocess.CompletedProcess(
            command,
            systemd_proc.returncode,
            stdout=systemd_proc.stdout,
            stderr=systemd_proc.stderr,
        )
    try:
        returncode = int(returncode_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        returncode = systemd_proc.returncode
    stdout = _read_text_if_present(stdout_path, fallback=systemd_proc.stdout)
    stderr = _read_text_if_present(stderr_path, fallback=systemd_proc.stderr)
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def _read_text_if_present(path: Path, *, fallback: str | None = "", max_bytes: int = MAX_SANDBOX_TERMINAL_OUTPUT_BYTES + 1) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(max(1, int(max_bytes)) + 1)
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return fallback or ""


def _coding_command_argv(request: dict[str, Any]) -> list[str]:
    argv = request.get("argv")
    if isinstance(argv, list) and argv:
        return [str(item) for item in argv]
    command = str(request.get("command") or "").strip()
    if not command:
        raise ValueError("command or argv is required")
    return ["/bin/sh", "-lc", command]


def _coding_sandbox_env(sandbox_id: str) -> dict[str, str]:
    return {
        "HOME": "/home",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "RUMI_SANDBOX_ID": sandbox_id,
    }


def _validated_profile_context(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if (
        len(candidate) > 255
        or "\x00" in candidate
        or "/" in candidate
        or "\\" in candidate
        or ".." in candidate
    ):
        raise ValueError("active profile context is invalid")
    return candidate


def _coding_terminal_response(
    *,
    sandbox_id: str,
    command: Any,
    returncode: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool,
    success: bool | None = None,
    provider_id: str = "bwrap_host",
) -> dict[str, Any]:
    clipped_stdout, stdout_truncated = _clip_output(stdout)
    clipped_stderr, stderr_truncated = _clip_output(stderr)
    ok = returncode == 0 and not timed_out if success is None else bool(success)
    return {
        "success": ok,
        "ok": ok,
        "sandbox_id": sandbox_id,
        "execution_boundary": "managed_sandbox",
        "provider_id": provider_id,
        "command": command,
        "exit_code": returncode,
        "returncode": returncode,
        "stdout": clipped_stdout,
        "stderr": clipped_stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "timed_out": timed_out,
        "process_failed": returncode not in (0, None),
    }


def _clip_output(text: Any) -> tuple[str, bool]:
    value = str(text or "")
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= MAX_SANDBOX_TERMINAL_OUTPUT_BYTES:
        return value, False
    clipped = raw[:MAX_SANDBOX_TERMINAL_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return clipped + "\n[output truncated]\n", True


def _decode_bytes(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _lima_import_workspace(
    *,
    limactl: str,
    instance: str,
    remote_root: str,
    archive: bytes,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    if not _is_lima_workspace_path(remote_root):
        raise ValueError("invalid Lima sandbox workspace path")
    import_script = (
        f"rm -rf {shlex.quote(remote_root)} && "
        f"mkdir -p {shlex.quote(remote_root)} && "
        f"chmod 700 {shlex.quote(remote_root)} && "
        f"tar -xf - -C {shlex.quote(remote_root)}"
    )
    return _run_bounded_process(
        [limactl, "shell", instance, "--", "sh", "-lc", import_script],
        input_data=archive,
        timeout=timeout + 2,
        max_stdin_bytes=MAX_CODING_WORKSPACE_EXPORT_BYTES,
    )


def _lima_remove_workspace(limactl: str, instance: str, remote_root: str) -> None:
    if not _is_lima_workspace_path(remote_root):
        return
    try:
        _run_bounded_process(
            [limactl, "shell", instance, "--", "rm", "-rf", "--", remote_root],
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _is_lima_workspace_path(value: str) -> bool:
    path = Path(value)
    return (
        path.is_absolute()
        and path.parent == Path(LIMA_GUEST_WORKSPACE_ROOT)
        and path.name.startswith(".rumi-")
        and path.name not in {".rumi-", ".", ".."}
    )


def _lima_ensure_data_dir(
    *,
    limactl: str,
    instance: str,
    data_dir: str,
    timeout: float,
) -> None:
    data_path = Path(data_dir)
    if (
        not data_path.is_absolute()
        or data_path.parent != Path(LIMA_GUEST_PACK_DATA_ROOT)
        or data_path.name in {"", ".", ".."}
    ):
        raise ValueError("invalid Lima Pack data path")
    proc = _run_bounded_process(
        [
            limactl,
            "shell",
            instance,
            "--",
            "sh",
            "-lc",
            f"mkdir -p {shlex.quote(data_dir)} && chmod 700 {shlex.quote(data_dir)}",
        ],
        timeout=timeout + 2,
    )
    if proc.returncode != 0:
        raise ValueError(
            _decode_bytes(proc.stderr) or "Could not prepare Lima Pack data"
        )


def _lima_migrate_pack_data(
    *,
    limactl: str,
    instance: str,
    data_dir: str,
    host_user_data_dir: Path,
    pack_id: str,
    timeout: float,
) -> None:
    """Atomically import one legacy Host-owned Pack subtree into Lima."""
    data_path = Path(data_dir)
    if (
        not data_path.is_absolute()
        or data_path.parent != Path(LIMA_GUEST_PACK_DATA_ROOT)
        or data_path.name in {"", ".", ".."}
    ):
        raise ValueError("invalid Lima Pack data path")
    if not pack_id or not all(
        character.isascii()
        and (character.isalnum() or character in {"-", "_", "."})
        for character in pack_id
    ):
        raise ValueError("Pack ID is invalid for Host data migration")
    host_root = host_user_data_dir.expanduser().resolve()
    if not host_root.is_dir() or host_root.is_symlink():
        raise ValueError("Host user-data root is unavailable for Pack migration")
    legacy_source = host_root / "packs" / pack_id
    with tempfile.TemporaryDirectory(prefix="rumi-pack-data-migration-") as tmp:
        staged_source = Path(tmp) / "staged"
        staged_source.mkdir(mode=0o700)
        if legacy_source.exists():
            if not legacy_source.is_dir() or legacy_source.is_symlink():
                raise ValueError("Legacy Pack data source is unsafe")
            _stage_regular_tree(legacy_source, staged_source)
        archive = _tar_directory(staged_source)

    transaction_id = uuid.uuid4().hex
    staging = f"{LIMA_GUEST_PACK_DATA_ROOT}/.migration-stage-{transaction_id}"
    backup = f"{LIMA_GUEST_PACK_DATA_ROOT}/.migration-backup-{transaction_id}"
    destination = f"{data_dir}/packs/{pack_id}"
    marker = f"{data_dir}/{PACK_DATA_MIGRATION_MARKER}"
    lock = f"{data_dir}/.migration-lock"
    import_script = (
        f"rm -rf {shlex.quote(staging)} && "
        f"mkdir -p {shlex.quote(staging)} && "
        f"chmod 700 {shlex.quote(staging)} && "
        f"tar -xf - -C {shlex.quote(staging)}"
    )
    imported = _run_bounded_process(
        [limactl, "shell", instance, "--", "sh", "-lc", import_script],
        input_data=archive,
        timeout=timeout + 2,
        max_stdin_bytes=MAX_STAGE_TOTAL_BYTES,
    )
    if imported.returncode != 0:
        raise ValueError(
            _decode_bytes(imported.stderr) or "Could not stage legacy Pack data"
        )

    committed = _run_bounded_process(
        [
            limactl,
            "shell",
            instance,
            "--",
            "sh",
            "-lc",
            _pack_data_migration_commit_script(),
            "rumi-pack-data-migration",
            data_dir,
            destination,
            staging,
            backup,
            marker,
            lock,
        ],
        timeout=timeout + 2,
    )
    if committed.returncode != 0:
        _lima_remove_pack_migration_path(
            limactl=limactl,
            instance=instance,
            path=staging,
        )
        raise ValueError(
            _decode_bytes(committed.stderr)
            or "Could not commit legacy Pack data migration"
        )


def _pack_data_migration_commit_script() -> str:
    """Return the transaction used both by Lima and local rollback tests."""
    return """
set -eu
data_dir=$1
destination=$2
staging=$3
backup=$4
marker=$5
lock=$6
if [ -f "$marker" ]; then
    rm -rf "$staging"
    exit 0
fi
if ! mkdir "$lock" 2>/dev/null; then
    rm -rf "$staging"
    echo "Pack data migration is already in progress" >&2
    exit 1
fi
had_backup=0
rollback() {
    rm -rf "$destination" || true
    if [ "$had_backup" -eq 1 ] && [ -e "$backup" ]; then
        mv "$backup" "$destination" || true
    fi
    rm -f "$marker.tmp" 2>/dev/null || true
    rm -rf "$staging" "$lock" || true
}
trap rollback EXIT HUP INT TERM
mkdir -p "$data_dir/packs"
if [ -e "$destination" ]; then
    mv "$destination" "$backup"
    had_backup=1
fi
mv "$staging" "$destination"
printf '%s\n' '{"version":1,"source":"host-pack-subtree"}' > "$marker.tmp"
chmod 600 "$marker.tmp"
mv "$marker.tmp" "$marker"
trap - EXIT HUP INT TERM
rm -rf "$backup" "$lock"
""".strip()


def _lima_remove_pack_migration_path(
    *,
    limactl: str,
    instance: str,
    path: str,
) -> None:
    candidate = Path(path)
    if (
        not candidate.is_absolute()
        or candidate.parent != Path(LIMA_GUEST_PACK_DATA_ROOT)
        or not candidate.name.startswith(".migration-stage-")
    ):
        return
    try:
        _run_bounded_process(
            [limactl, "shell", instance, "--", "rm", "-rf", "--", path],
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _safe_guest_name(value: str) -> str:
    canonical = str(value)
    safe = "".join(
        character
        if character.isascii() and (character.isalnum() or character in {"-", "_", "."})
        else "-"
        for character in canonical
    ).strip(".-")
    prefix = (safe or "pack")[:60]
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}--{digest}"


def _guest_resource_limited_argv(
    argv: tuple[str, ...],
    *,
    timeout: float,
    memory_mb: int,
    pids: int,
) -> tuple[str, ...]:
    return (
        "timeout",
        "--signal=TERM",
        "--kill-after=1s",
        f"{max(1.0, float(timeout)):g}s",
        "prlimit",
        f"--as={int(memory_mb) * 1024 * 1024}",
        f"--nproc={int(pids)}",
        f"--cpu={max(1, int(timeout))}",
        "--",
        *argv,
    )


def _bounded_positive_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _tar_directory(root: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
            for file_name in files:
                path = current_path / file_name
                if not path.is_file() or path.is_symlink():
                    continue
                archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    return buffer.getvalue()


def _replace_directory_from_tar(root: Path, archive: bytes | Path) -> None:
    with tempfile.TemporaryDirectory(prefix="rumi-sandbox-export-") as tmp:
        target = Path(tmp) / "work"
        target.mkdir(mode=0o700)
        target_root = target.resolve()
        if isinstance(archive, Path):
            tar_context = tarfile.open(archive, mode="r:*")
        else:
            tar_context = tarfile.open(fileobj=io.BytesIO(archive), mode="r:*")
        with tar_context as tar:
            file_count = 0
            total_bytes = 0
            for member in tar:
                member_path = (target / member.name).resolve()
                try:
                    member_path.relative_to(target_root)
                except ValueError as exc:
                    raise ValueError("sandbox export attempted path traversal") from exc
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    continue
                if member.isfile():
                    file_count += 1
                    total_bytes += int(member.size)
                    if file_count > MAX_CODING_WORKSPACE_EXPORT_FILES:
                        raise ValueError("sandbox export has too many files")
                    if member.size > MAX_CODING_WORKSPACE_EXPORT_FILE_BYTES:
                        raise ValueError("sandbox export contains an oversized file")
                    if total_bytes > MAX_CODING_WORKSPACE_EXPORT_BYTES:
                        raise ValueError("sandbox export is too large")
                tar.extract(member, target, filter="data")
        for item in root.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        for item in target.iterdir():
            shutil.move(str(item), str(root / item.name))


def _sandbox_id(request: dict[str, Any]) -> str:
    raw = str(request.get("sandbox_id") or "").strip()
    if raw and "/" not in raw and "\x00" not in raw:
        return raw
    return "sbx_" + uuid.uuid4().hex[:24]


def _bounded_timeout(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 60.0
    return min(max(parsed, 1.0), 60.0)


def _immutable_root(request: dict[str, Any]) -> Path:
    raw = request.get("immutable_root") or os.environ.get("RUMI_SANDBOX_IMMUTABLE_ROOT")
    if not str(raw or "").strip():
        raise RuntimeError("SANDBOX_RUNTIME_UNAVAILABLE: immutable sandbox root is not configured")
    root = _required_dir(raw, "immutable_root")
    if root == Path("/").resolve():
        raise RuntimeError("SANDBOX_RUNTIME_UNAVAILABLE: host root cannot be used as sandbox root")
    marker = root / SANDBOX_ROOT_MARKER
    if not marker.is_file():
        raise RuntimeError("SANDBOX_RUNTIME_UNAVAILABLE: immutable sandbox root marker is missing")
    root_mode = root.stat().st_mode
    if root_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError("SANDBOX_RUNTIME_UNAVAILABLE: immutable sandbox root is writable by group/other")
    mode = marker.stat().st_mode
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError("SANDBOX_RUNTIME_UNAVAILABLE: immutable sandbox root marker is writable by group/other")
    return root


def _stage_regular_tree(source_root: Path, target_root: Path) -> dict[str, int]:
    source_root = source_root.resolve()
    file_count = 0
    total_bytes = 0
    for current, dirs, files in os.walk(source_root, topdown=True, followlinks=False):
        current_path = Path(current)
        rel_dir = current_path.relative_to(source_root)
        if "__pycache__" in rel_dir.parts:
            dirs[:] = []
            continue
        target_dir = target_root / rel_dir
        target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for dir_name in dirs:
            dir_path = current_path / dir_name
            _reject_special_or_link(dir_path, source_root)
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for file_name in files:
            source = current_path / file_name
            _reject_special_or_link(source, source_root)
            if file_name.endswith(".pyc"):
                continue
            try:
                source.relative_to(source_root)
            except ValueError as exc:
                raise ValueError("Sandbox function staging path escapes function directory") from exc
            target = target_dir / file_name
            src, stat_result = _open_regular_source_for_stage(source)
            size = int(stat_result.st_size)
            try:
                if size > MAX_STAGE_FILE_BYTES:
                    raise ValueError("Sandbox function staging file is too large")
                file_count += 1
                total_bytes += size
                if file_count > MAX_STAGE_FILES:
                    raise ValueError("Sandbox function staging has too many files")
                if total_bytes > MAX_STAGE_TOTAL_BYTES:
                    raise ValueError("Sandbox function staging tree is too large")
                with src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            except Exception:
                src.close()
                raise
            os.chmod(target, stat_result.st_mode & 0o700)
    return {"files": file_count, "bytes": total_bytes}


def _open_regular_source_for_stage(source: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise ValueError("Sandbox function staging could not open path") from exc
    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            raise ValueError("Sandbox function staging only accepts regular files")
        if stat_result.st_nlink > 1:
            raise ValueError("Sandbox function staging rejects hardlinked files")
        return os.fdopen(fd, "rb"), stat_result
    except Exception:
        os.close(fd)
        raise


def _reject_special_or_link(path: Path, source_root: Path) -> None:
    try:
        lstat_result = path.lstat()
    except OSError as exc:
        raise ValueError("Sandbox function staging could not inspect path") from exc
    if stat.S_ISLNK(lstat_result.st_mode):
        raise ValueError("Sandbox function staging rejects symlinks")
    if stat.S_ISFIFO(lstat_result.st_mode) or stat.S_ISSOCK(lstat_result.st_mode):
        raise ValueError("Sandbox function staging rejects special files")
    if stat.S_ISCHR(lstat_result.st_mode) or stat.S_ISBLK(lstat_result.st_mode):
        raise ValueError("Sandbox function staging rejects device files")
    try:
        path.resolve(strict=False).relative_to(source_root)
    except ValueError as exc:
        raise ValueError("Sandbox function staging path escapes function directory") from exc


def _required_dir(value: Any, label: str) -> Path:
    path = Path(str(value or "")).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    return path


def _required_file(value: Any, label: str) -> Path:
    path = Path(str(value or "")).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{label} must be an existing file")
    return path
