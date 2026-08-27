from __future__ import annotations

import base64
import hashlib
import io
import os
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Mapping, Sequence

from ..cancellation import run_cancellable_subprocess
from ..errors import RUNTIME_PROVIDER_UNAVAILABLE, SandboxContractError
from ..guest.protocol import DesktopInputRequest, GuestExecRequest
from ..models import (
    Diagnostic,
    EnsureRuntimeRequest,
    OperationResult,
    ProgressEvent,
    ProviderInstance,
    ReconcileResult,
    RuntimeProviderStatus,
    RuntimeRequirements,
    SandboxCreateSpec,
    UninstallRuntimeRequest,
    UpdateRuntimeRequest,
    model_to_dict,
)
from ..policy import validate_workspace_relative_path
from ..isolation.lima_runtime import (
    LIMA_GUEST_WORKSPACE_ROOT,
    LIMA_GUEST_PACK_DATA_ROOT,
    build_guest_bwrap_argv,
    resolve_limactl_path,
    save_lima_runtime_state,
)
from .base import ProgressSink


MANAGED_UBUNTU_CAPABILITIES = frozenset(
    {
        "sandbox.exec",
        "sandbox.files",
        "sandbox.overlay_workspace",
        "sandbox.network_policy",
        "sandbox.resource_limits",
        "sandbox.desktop",
        "sandbox.desktop_input",
        "sandbox.snapshot",
    }
)
GUEST_WORKDIR = LIMA_GUEST_WORKSPACE_ROOT
GUEST_DEPS = (
    "Xvfb",
    "openbox",
    "xdotool",
    "import",
    "python3",
    "xterm",
    "unshare",
    "bwrap",
    "prlimit",
    "timeout",
)
APT_PACKAGES = (
    "xvfb",
    "openbox",
    "xdotool",
    "imagemagick",
    "python3",
    "xterm",
    "x11-utils",
    "ca-certificates",
    "coreutils",
    "util-linux",
    "bubblewrap",
)
DEFAULT_DISPLAY = ":98"
GUEST_DISPLAY_MIN = 98
GUEST_DISPLAY_MAX = 199
DEFAULT_WSL_RUNTIME_NAME = "RumiUbuntu"
WSL_ROOTFS_ENV = "RUMI_WSL_ROOTFS_TARBALL"
WSL_INSTALL_DIR_ENV = "RUMI_WSL_INSTALL_DIR"
WSL_ROOTFS_CACHE_DIR_ENV = "RUMI_WSL_ROOTFS_CACHE_DIR"
WSL_ROOTFS_URL_ENV = "RUMI_WSL_ROOTFS_URL"
MAX_FILE_PATCH_BYTES = 2 * 1024 * 1024
MAX_WORKSPACE_SEED_BYTES = 64 * 1024 * 1024
RESERVED_EXEC_ENV_KEYS = frozenset(
    {
        "HOME",
        "PATH",
        "RUMI_SANDBOX_ID",
        "RUMI_SANDBOX_INSTANCE",
        "RUMI_SANDBOX_WORKSPACE",
    }
)
DEFAULT_WSL_ROOTFS_URLS = {
    "amd64": "https://cloud-images.ubuntu.com/wsl/releases/22.04/current/ubuntu-jammy-wsl-amd64-wsl.rootfs.tar.gz",
    "arm64": "https://cloud-images.ubuntu.com/wsl/releases/22.04/current/ubuntu-jammy-wsl-arm64-wsl.rootfs.tar.gz",
}
GUEST_APP_PACKAGE_MAP = {
    "ca-certificates": ("ca-certificates",),
    "chromium": ("google-chrome-stable",),
    "chromium-browser": ("google-chrome-stable",),
    "firefox": ("firefox",),
    "google-chrome": ("google-chrome-stable",),
    "google-chrome-stable": ("google-chrome-stable",),
    "imagemagick": ("imagemagick",),
    "node": ("nodejs", "npm"),
    "nodejs": ("nodejs",),
    "npm": ("npm",),
    "openbox": ("openbox",),
    "python": ("python3", "python3-pip"),
    "python3": ("python3",),
    "python3-pip": ("python3-pip",),
    "x11-utils": ("x11-utils",),
    "xdotool": ("xdotool",),
    "xterm": ("xterm",),
    "xvfb": ("xvfb",),
}
SUDO_BOOTSTRAP_SCRIPT = (
    "if [ \"$(id -u)\" = '0' ]; then\n"
    "  RUMI_SUDO=''\n"
    "elif command -v sudo >/dev/null 2>&1; then\n"
    "  RUMI_SUDO='sudo'\n"
    "else\n"
    "  echo 'sudo is required for managed runtime package installation when not running as root' >&2\n"
    "  exit 126\n"
    "fi\n"
)


@dataclass(frozen=True)
class GuestCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    stdout_decoding: str = "injected-text"
    stdout_decoding_error: str | None = None
    stdout_truncated: bool = False
    stderr_present: bool | None = None


CommandRunner = Callable[[Sequence[str], str | None, float | None], GuestCommandResult]
RootfsDownloader = Callable[[str, str], None]
ChecksumFetcher = Callable[[str], str]


class _ManagedGuestProbeInvalid(SandboxContractError):
    """A retryable launcher probe failure that must not mean guest absence."""


def _wsl_distribution_names(output: str) -> tuple[str, ...]:
    normalized = str(output or "")
    if normalized.startswith("\ufeff"):
        normalized = normalized[1:]
    if "\ufeff" in normalized or any(
        unicodedata.category(character) == "Cc" and character not in {"\t", "\r", "\n"}
        for character in normalized
    ):
        raise ValueError("WSL distribution output contains control characters")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return tuple(line.strip() for line in normalized.split("\n") if line.strip())


class ManagedUbuntuProvider:
    """Command-backed managed Ubuntu runtime used by Lima and WSL providers."""

    provider_id: str
    _host_platform: str
    _launcher_command: str

    def __init__(
        self,
        *,
        command_path: str | None = None,
        runner: CommandRunner | None = None,
        runtime_name: str = "rumi-managed-runtime",
    ) -> None:
        self._configured_command_path = command_path
        self._runner = runner or _subprocess_runner
        self._runtime_name = runtime_name
        self._instances: dict[str, ProviderInstance] = {}

    def doctor(self, request: RuntimeRequirements) -> RuntimeProviderStatus:
        host_platform = platform.system().lower() or "unknown"
        platform_ok = host_platform == self._host_platform
        command_path = self._command_path()
        diagnostics: list[Diagnostic] = []
        missing: list[str] = []
        version: str | None = None

        if not platform_ok:
            missing.append(f"platform:{self._host_platform}")
            diagnostics.append(
                Diagnostic(
                    code=f"{self.provider_id.upper()}_PLATFORM_UNAVAILABLE",
                    message=f"{self.provider_id} requires {self._host_platform}.",
                    severity="info",
                )
            )
        if command_path is None:
            missing.append(f"command:{self._launcher_command}")
            diagnostics.append(
                Diagnostic(
                    code=f"{self.provider_id.upper()}_COMMAND_MISSING",
                    message=f"{self._launcher_command} was not found on PATH; this build cannot bootstrap that launcher itself.",
                    severity="error",
                )
            )
        elif platform_ok:
            version_result = self._version(command_path)
            version = (
                version_result.stdout.strip().splitlines()[0]
                if version_result.stdout.strip()
                else None
            )

        guest_ready = False
        missing_deps: tuple[str, ...] = ()
        if platform_ok and command_path is not None:
            guest_probe_error: _ManagedGuestProbeInvalid | None = None
            try:
                guest_ready = self._guest_exists(command_path)
            except _ManagedGuestProbeInvalid as exc:
                guest_probe_error = exc
            if guest_probe_error is not None:
                missing.append("managed_guest_probe")
                diagnostics.append(
                    Diagnostic(
                        code=guest_probe_error.code,
                        message=guest_probe_error.message,
                        severity="warning",
                        details=guest_probe_error.details,
                    )
                )
            elif not guest_ready:
                missing.append("managed_guest")
                diagnostics.append(
                    Diagnostic(
                        code=f"{self.provider_id.upper()}_GUEST_MISSING",
                        message="Managed Ubuntu guest is not created yet.",
                        severity="warning",
                    )
                )
            else:
                missing_deps = self._missing_guest_deps(command_path)
                if missing_deps:
                    missing.extend(f"guest_command:{name}" for name in missing_deps)
                    diagnostics.append(
                        Diagnostic(
                            code=f"{self.provider_id.upper()}_GUEST_DEPS_MISSING",
                            message="Managed Ubuntu guest is missing desktop helper packages.",
                            severity="warning",
                            details={"missing_commands": missing_deps},
                        )
                    )

        missing_capabilities = sorted(request.required_capabilities - MANAGED_UBUNTU_CAPABILITIES)
        if missing_capabilities:
            diagnostics.append(
                Diagnostic(
                    code=f"{self.provider_id.upper()}_CAPABILITY_UNSUPPORTED",
                    message="Managed Ubuntu provider does not advertise every requested runtime capability.",
                    severity="warning",
                    details={"missing_capabilities": missing_capabilities},
                )
            )
        missing.extend(missing_capabilities)
        launcher_available = platform_ok and command_path is not None
        ready = launcher_available and guest_ready and not missing_deps and not missing_capabilities
        return RuntimeProviderStatus(
            provider_id=self.provider_id,
            platform=self._host_platform,
            available=launcher_available,
            installed=launcher_available and guest_ready and not missing_deps,
            ready=ready,
            version=version,
            capabilities=MANAGED_UBUNTU_CAPABILITIES if launcher_available else frozenset(),
            missing_requirements=tuple(missing),
            requires_user_action=not ready,
            user_action=None
            if ready
            else self._setup_message(
                launcher_missing=platform_ok and command_path is None,
                missing_capabilities=missing_capabilities,
            ),
            reboot_required=False,
            diagnostics=tuple(diagnostics),
        )

    def ensure(self, request: EnsureRuntimeRequest, progress: ProgressSink) -> OperationResult:
        command_path = self._command_path()
        if command_path is None or platform.system().lower() != self._host_platform:
            status = self.doctor(request.requirements)
            return OperationResult(
                ok=False,
                provider_id=self.provider_id,
                operation_id=f"{self.provider_id}-ensure",
                status="failed",
                diagnostics=status.diagnostics,
                requires_user_action=True,
                user_action=status.user_action,
                reboot_required=status.reboot_required,
            )

        try:
            progress.emit(
                ProgressEvent(
                    operation_id=f"{self.provider_id}-ensure",
                    stage="guest",
                    message="Creating or starting managed Ubuntu guest",
                    percent=15,
                )
            )
            self._ensure_guest(command_path)
            progress.emit(
                ProgressEvent(
                    operation_id=f"{self.provider_id}-ensure",
                    stage="packages",
                    message="Installing managed runtime guest packages",
                    percent=55,
                )
            )
            self._install_guest_packages(command_path)
            status = self.doctor(request.requirements)
        except SandboxContractError as exc:
            return OperationResult(
                ok=False,
                provider_id=self.provider_id,
                operation_id=f"{self.provider_id}-ensure",
                status="failed",
                diagnostics=(
                    Diagnostic(
                        code=exc.code, message=exc.message, severity="error", details=exc.details
                    ),
                ),
                requires_user_action=True,
                user_action=exc.message,
            )

        if status.ready:
            progress.emit(
                ProgressEvent(
                    operation_id=f"{self.provider_id}-ensure",
                    stage="ready",
                    message="Managed Ubuntu runtime is ready",
                    percent=100,
                )
            )
            return OperationResult(
                ok=True,
                provider_id=self.provider_id,
                operation_id=f"{self.provider_id}-ensure",
                status="completed",
            )
        return OperationResult(
            ok=False,
            provider_id=self.provider_id,
            operation_id=f"{self.provider_id}-ensure",
            status="failed",
            diagnostics=status.diagnostics,
            requires_user_action=True,
            user_action=status.user_action,
        )

    def update(self, request: UpdateRuntimeRequest, progress: ProgressSink) -> OperationResult:
        del request
        command_path = self._require_command()
        progress.emit(
            ProgressEvent(
                operation_id=f"{self.provider_id}-update",
                stage="packages",
                message="Updating managed Ubuntu guest packages",
                percent=50,
            )
        )
        self._install_guest_packages(command_path, update=True)
        progress.emit(
            ProgressEvent(
                operation_id=f"{self.provider_id}-update",
                stage="ready",
                message="Managed Ubuntu runtime packages are current",
                percent=100,
            )
        )
        return OperationResult(
            ok=True,
            provider_id=self.provider_id,
            operation_id=f"{self.provider_id}-update",
            status="completed",
        )

    def uninstall(
        self, request: UninstallRuntimeRequest, progress: ProgressSink
    ) -> OperationResult:
        command_path = self._require_command()
        for instance in list(self._instances.values()):
            self.destroy(instance)
        self._stop_guest(command_path)
        if request.remove_state:
            self._delete_guest(command_path)
        progress.emit(
            ProgressEvent(
                operation_id=f"{self.provider_id}-uninstall",
                stage="stopped",
                message="Stopped managed Ubuntu runtime",
                percent=100,
            )
        )
        return OperationResult(
            ok=True,
            provider_id=self.provider_id,
            operation_id=f"{self.provider_id}-uninstall",
            status="completed",
        )

    def create(self, spec: SandboxCreateSpec) -> ProviderInstance:
        command_path = self._require_ready(spec.template.provider_requirements)
        sandbox_id = str(uuid.uuid4())
        provider_instance_id = f"{self.provider_id}-{sandbox_id}"
        desktop = spec.template.desktop
        width = int(desktop.width if desktop else 1440)
        height = int(desktop.height if desktop else 900)
        network_approved = bool(spec.metadata.get("network_approved"))
        opaque = {
            "command_path": command_path,
            "runtime_name": self._runtime_name,
            "guest_workspace": _instance_workspace_dir(provider_instance_id),
            "template_id": spec.template.template_id,
            "width": width,
            "height": height,
            "desktop_enabled": desktop is not None and desktop.enabled,
            "display": self._allocate_guest_display(command_path)
            if desktop is not None and desktop.enabled
            else "",
            "workspace_binding": model_to_dict(spec.workspace_binding),
            "network_policy": model_to_dict(spec.template.network),
            "network_approved": network_approved,
            "network_disabled": _guest_network_disabled(spec.template.network)
            and not network_approved,
            "resource_limits": model_to_dict(spec.template.resources),
            "template_packages": [model_to_dict(package) for package in spec.template.packages],
            "desktop_provisioning": spec.metadata.get("desktop_provisioning") or {},
            "desktop_rules": spec.metadata.get("desktop_rules") or {},
            "assigned_agent_id": spec.metadata.get("assigned_agent_id"),
            "startup": spec.metadata.get("startup") or {},
        }
        instance = ProviderInstance(
            provider_id=self.provider_id,
            provider_instance_id=provider_instance_id,
            sandbox_id=sandbox_id,
            runtime_id=self._runtime_name,
            state="stopped",
            opaque_state=opaque,
        )
        self._instances[instance.provider_instance_id] = instance
        return instance

    def start(self, instance: ProviderInstance) -> ProviderInstance:
        command_path = str(
            instance.opaque_state.get("command_path")
            or self._require_ready(MANAGED_UBUNTU_CAPABILITIES)
        )
        self._seed_workspace(command_path, instance)
        self._provision_instance(command_path, instance)
        if instance.opaque_state.get("desktop_enabled") is True:
            self._guest_shell(
                command_path,
                _desktop_start_script(
                    instance.provider_instance_id,
                    _instance_workspace_dir_for(instance),
                    _positive_int(instance.opaque_state.get("width"), 1440),
                    _positive_int(instance.opaque_state.get("height"), 900),
                    str(instance.opaque_state.get("display") or DEFAULT_DISPLAY),
                    _instance_network_disabled(instance),
                    instance.opaque_state.get("startup")
                    if isinstance(instance.opaque_state.get("startup"), Mapping)
                    else {},
                ),
                timeout=30,
            )
        else:
            self._guest_shell(
                command_path,
                f"mkdir -p {shlex.quote(_instance_workspace_dir_for(instance))}",
                timeout=15,
            )
        started = ProviderInstance(
            provider_id=instance.provider_id,
            provider_instance_id=instance.provider_instance_id,
            sandbox_id=instance.sandbox_id,
            runtime_id=instance.runtime_id,
            state="ready",
            opaque_state=instance.opaque_state,
            generation=instance.generation + 1,
        )
        self._instances[started.provider_instance_id] = started
        return started

    def stop(self, instance: ProviderInstance, *, force: bool = False) -> None:
        del force
        command_path = str(
            instance.opaque_state.get("command_path")
            or self._command_path()
            or self._launcher_command
        )
        self._guest_shell(
            command_path,
            _desktop_stop_script(instance.provider_instance_id),
            timeout=15,
            check=False,
        )
        stopped = ProviderInstance(
            provider_id=instance.provider_id,
            provider_instance_id=instance.provider_instance_id,
            sandbox_id=instance.sandbox_id,
            runtime_id=instance.runtime_id,
            state="stopped",
            opaque_state=instance.opaque_state,
            generation=instance.generation + 1,
        )
        self._instances[stopped.provider_instance_id] = stopped

    def destroy(self, instance: ProviderInstance) -> None:
        command_path = str(
            instance.opaque_state.get("command_path")
            or self._command_path()
            or self._launcher_command
        )
        self.stop(instance, force=True)
        self._guest_shell(
            command_path,
            _instance_destroy_script(
                instance.provider_instance_id, _instance_workspace_dir_for(instance)
            ),
            timeout=30,
            check=False,
        )
        self._instances.pop(instance.provider_instance_id, None)

    def reconcile(self, persisted: ProviderInstance) -> ReconcileResult:
        command_path = str(
            persisted.opaque_state.get("command_path")
            or self._command_path()
            or self._launcher_command
        )
        if persisted.opaque_state.get("desktop_enabled") is True:
            running = self._desktop_running(command_path, persisted.provider_instance_id)
        else:
            running = self._instance_exists(
                command_path, persisted.provider_instance_id, _instance_workspace_dir_for(persisted)
            )
        state = "ready" if running else "stopped"
        current = ProviderInstance(
            provider_id=persisted.provider_id,
            provider_instance_id=persisted.provider_instance_id,
            sandbox_id=persisted.sandbox_id,
            runtime_id=persisted.runtime_id,
            state=state,
            opaque_state=persisted.opaque_state,
            generation=persisted.generation,
        )
        self._instances[current.provider_instance_id] = current
        return ReconcileResult(instance=current, changed=current.state != persisted.state)

    def connect_agent(self, instance: ProviderInstance) -> "ManagedUbuntuGuestAgent":
        command_path = str(
            instance.opaque_state.get("command_path")
            or self._require_ready(MANAGED_UBUNTU_CAPABILITIES)
        )
        resources_value = (
            instance.opaque_state.get("resource_limits")
            if isinstance(instance.opaque_state.get("resource_limits"), Mapping)
            else {}
        )
        resources = dict(resources_value) if isinstance(resources_value, Mapping) else {}
        display = self._client_display(command_path, instance)
        return ManagedUbuntuGuestAgent(
            provider_id=self.provider_id,
            provider_instance_id=instance.provider_instance_id,
            command_path=command_path,
            command_prefix=self._guest_prefix(command_path),
            runner=self._runner,
            workspace_dir=_instance_workspace_dir_for(instance),
            display=display,
            width=_positive_int(instance.opaque_state.get("width"), 1440),
            height=_positive_int(instance.opaque_state.get("height"), 900),
            memory_mb=_optional_positive_int(resources.get("memory_mb")),
            cpu_count=_optional_positive_float(resources.get("cpu_count")),
            pids=_optional_positive_int(resources.get("pids")),
            output_bytes=_optional_positive_int(resources.get("output_bytes")),
            timeout_ms=_optional_positive_int(resources.get("timeout_ms")),
            network_disabled=_instance_network_disabled(instance),
        )

    def _client_display(self, command_path: str, instance: ProviderInstance) -> str:
        fallback = str(instance.opaque_state.get("display") or DEFAULT_DISPLAY)
        result = self._guest_shell(
            command_path,
            f"cat {_runtime_dir(instance.provider_instance_id)}/display.env 2>/dev/null",
            timeout=5,
            check=False,
        )
        candidate = result.stdout.strip()
        return candidate or fallback

    def _command_path(self) -> str | None:
        if self._configured_command_path:
            return self._configured_command_path
        return shutil.which(self._launcher_command)

    def _require_command(self) -> str:
        command_path = self._command_path()
        if command_path is None:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                f"{self._launcher_command} was not found on PATH.",
                status_code=503,
            )
        return command_path

    def _require_ready(self, required_capabilities: frozenset[str]) -> str:
        command_path = self._require_command()
        status = self.doctor(
            RuntimeRequirements(
                provider_id=self.provider_id, required_capabilities=required_capabilities
            )
        )
        if not status.ready:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                f"Managed runtime provider is not ready: {self.provider_id}",
                status_code=503,
                details={"missing_requirements": list(status.missing_requirements)},
            )
        return command_path

    def _version(self, command_path: str) -> GuestCommandResult:
        return self._run(self._version_command(command_path), timeout=5)

    def _run(
        self, command: Sequence[str], input_text: str | None = None, timeout: float | None = None
    ) -> GuestCommandResult:
        try:
            return self._runner(tuple(command), input_text, timeout)
        except TimeoutError as exc:
            return GuestCommandResult(returncode=124, stderr=str(exc))
        except OSError as exc:
            return GuestCommandResult(returncode=127, stderr=str(exc))

    def _guest_command(
        self,
        command_path: str,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> GuestCommandResult:
        result = self._run(
            (*self._guest_prefix(command_path), *argv), input_text=input_text, timeout=timeout
        )
        if check and result.returncode != 0:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Managed Ubuntu guest command failed.",
                status_code=503,
                details={"stderr": result.stderr.strip()[:1000], "argv": list(argv[:4])},
            )
        return result

    def _guest_shell(
        self,
        command_path: str,
        script: str,
        *,
        input_text: str | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> GuestCommandResult:
        return self._guest_command(
            command_path,
            ("bash", "-lc", script),
            input_text=input_text,
            timeout=timeout,
            check=check,
        )

    def _missing_guest_deps(self, command_path: str) -> tuple[str, ...]:
        script = "\n".join(
            f"command -v {name} >/dev/null 2>&1 || echo {name}" for name in GUEST_DEPS
        )
        result = self._guest_shell(command_path, script, timeout=10, check=False)
        if result.returncode != 0:
            return GUEST_DEPS
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    def _install_guest_packages(self, command_path: str, *, update: bool = False) -> None:
        packages = " ".join(APT_PACKAGES)
        script = (
            "set -e\n"
            "export DEBIAN_FRONTEND=noninteractive\n"
            f"{SUDO_BOOTSTRAP_SCRIPT}"
            "$RUMI_SUDO apt-get update\n"
            f"$RUMI_SUDO apt-get install -y {packages}\n"
            'RUMI_GUEST_USER="$(id -un)"\n'
            'RUMI_GUEST_GROUP="$(id -gn)"\n'
            "$RUMI_SUDO install -d -m 0755 /workspace\n"
            "$RUMI_SUDO install -d -m 0755 /data\n"
            f'$RUMI_SUDO install -d -m 0711 -o "$RUMI_GUEST_USER" '
            f'-g "$RUMI_GUEST_GROUP" {GUEST_WORKDIR}\n'
            f'$RUMI_SUDO install -d -m 0711 -o "$RUMI_GUEST_USER" '
            f'-g "$RUMI_GUEST_GROUP" {LIMA_GUEST_PACK_DATA_ROOT}\n'
        )
        if update:
            script += f"$RUMI_SUDO apt-get install --only-upgrade -y {packages} || true\n"
        self._guest_shell(command_path, script, timeout=600)

    def _seed_workspace(self, command_path: str, instance: ProviderInstance) -> None:
        workspace = _workspace_binding(instance.opaque_state)
        mode = str(workspace.get("mode") or "none")
        if mode not in {"read_only", "overlay"}:
            return
        root = str(workspace.get("root") or "")
        if not _usable_host_workspace_root(root):
            return
        payload = _workspace_seed_payload(root)
        self._guest_shell(
            command_path,
            _workspace_seed_script(mode, _instance_workspace_dir_for(instance)),
            input_text=payload,
            timeout=300,
        )

    def _provision_instance(self, command_path: str, instance: ProviderInstance) -> None:
        provisioning = _guest_provisioning_input(instance)
        if not provisioning:
            return
        apt_packages = _guest_provisioning_apt_packages(provisioning)
        mcp_servers = _guest_provisioning_mcp_servers(provisioning)
        if not apt_packages and not mcp_servers:
            return
        script = _guest_provisioning_script(
            instance.provider_instance_id,
            _instance_workspace_dir_for(instance),
            apt_packages,
            mcp_servers,
        )
        self._guest_shell(command_path, script, timeout=900)

    def _desktop_running(self, command_path: str, provider_instance_id: str) -> bool:
        result = self._guest_shell(
            command_path, _desktop_running_script(provider_instance_id), timeout=10, check=False
        )
        return result.returncode == 0

    def _instance_exists(
        self, command_path: str, provider_instance_id: str, workspace_dir: str
    ) -> bool:
        result = self._guest_shell(
            command_path,
            _instance_exists_script(provider_instance_id, workspace_dir),
            timeout=10,
            check=False,
        )
        return result.returncode == 0

    def _allocate_guest_display(self, command_path: str | None = None) -> str:
        used = {
            display
            for instance in self._instances.values()
            for display in [_normalized_guest_display(instance.opaque_state.get("display"))]
            if display
        }
        if command_path:
            used.update(self._guest_used_displays(command_path))
        for number in range(GUEST_DISPLAY_MIN, GUEST_DISPLAY_MAX + 1):
            display = f":{number}"
            if display not in used:
                return display
        raise SandboxContractError(
            RUNTIME_PROVIDER_UNAVAILABLE,
            "No free managed Ubuntu X11 DISPLAY number was available.",
            status_code=503,
            details={"display_min": GUEST_DISPLAY_MIN, "display_max": GUEST_DISPLAY_MAX},
        )

    def _guest_used_displays(self, command_path: str) -> set[str]:
        result = self._guest_shell(
            command_path, _guest_used_displays_script(), timeout=5, check=False
        )
        if result.returncode != 0:
            return set()
        return {
            display
            for line in result.stdout.splitlines()
            for display in [_normalized_guest_display(line)]
            if display
        }

    def _setup_message(
        self, *, launcher_missing: bool = False, missing_capabilities: Sequence[str] = ()
    ) -> str:
        if missing_capabilities:
            return (
                "Select a provider that supports "
                f"{', '.join(missing_capabilities)}; managed Ubuntu currently does not provide host port forwarding."
            )
        if launcher_missing:
            return (
                f"Install {self._launcher_command} first; this setup can create and provision "
                "the Rumi Ubuntu guest after the launcher is available."
            )
        return "Open the runtime setup flow to create and provision the Ubuntu guest."

    def _guest_exists(self, command_path: str) -> bool:
        raise NotImplementedError

    def _ensure_guest(self, command_path: str) -> None:
        raise NotImplementedError

    def _stop_guest(self, command_path: str) -> None:
        raise NotImplementedError

    def _delete_guest(self, command_path: str) -> None:
        raise NotImplementedError

    def _guest_prefix(self, command_path: str) -> tuple[str, ...]:
        raise NotImplementedError

    def _version_command(self, command_path: str) -> tuple[str, ...]:
        raise NotImplementedError


class BwrapHostProvider:
    """Diagnostic provider for the managed sandbox Bubblewrap/systemd boundary.

    It is intentionally separate from the managed Ubuntu desktop providers:
    Bubblewrap is the strong untrusted-pack execution boundary, while Lima/WSL
    managed Ubuntu remains a convenience desktop/runtime with shared guest
    namespaces.
    """

    provider_id = "bwrap_host"

    def doctor(self, request: RuntimeRequirements) -> RuntimeProviderStatus:
        del request
        diagnostics: list[Diagnostic] = []
        missing: list[str] = []
        try:
            from ..isolation import diagnose_sandbox_environment

            sandbox_diagnostics = diagnose_sandbox_environment()
        except Exception as exc:
            sandbox_diagnostics = {"ready": False, "checks": []}
            missing.append("managed_sandbox")
            diagnostics.append(
                Diagnostic(
                    code="SANDBOX_RUNTIME_UNAVAILABLE",
                    message=f"Managed sandbox diagnostics failed: {exc}",
                    severity="error",
                )
            )

        for check in sandbox_diagnostics.get("checks", []):
            if not isinstance(check, Mapping) or check.get("ok"):
                continue
            name = str(check.get("name") or "managed_sandbox")
            if name == "bubblewrap":
                requirement = "command:bwrap"
            elif name == "systemd_user_scope":
                requirement = "systemd:user_scope"
            elif name == "immutable_root":
                requirement = "rootfs:immutable_root"
            else:
                requirement = name
            missing.append(requirement)
            diagnostics.append(
                Diagnostic(
                    code=str(check.get("code") or "SANDBOX_RUNTIME_UNAVAILABLE"),
                    message=str(
                        check.get("message") or "Managed sandbox requirement is not satisfied"
                    ),
                    severity="warning",
                    details={
                        "check": name,
                        "path": check.get("path"),
                        "marker": check.get("marker"),
                        "returncode": check.get("returncode"),
                        "stderr": check.get("stderr"),
                    },
                )
            )
        if not _unprivileged_userns_available():
            missing.append("kernel:unprivileged_userns")
            diagnostics.append(
                Diagnostic(
                    code="SANDBOX_RUNTIME_UNAVAILABLE",
                    message="Unprivileged user namespaces are unavailable.",
                    severity="warning",
                    details={"check": "unprivileged_userns"},
                )
            )
        missing = list(dict.fromkeys(missing))
        command_path = shutil.which("bwrap")
        return RuntimeProviderStatus(
            provider_id=self.provider_id,
            platform="linux",
            available=platform.system().lower() == "linux" and command_path is not None,
            installed=command_path is not None,
            ready=platform.system().lower() == "linux" and command_path is not None and not missing,
            version=_command_version(command_path) if command_path else None,
            capabilities=frozenset(
                {
                    "sandbox.exec",
                    "sandbox.files",
                    "sandbox.overlay_workspace",
                    "sandbox.network_policy",
                    "sandbox.resource_limits",
                }
            )
            if command_path
            else frozenset(),
            missing_requirements=tuple(missing),
            requires_user_action=bool(missing),
            user_action=None
            if not missing
            else "Install Bubblewrap/systemd user scope and configure RUMI_SANDBOX_IMMUTABLE_ROOT.",
            diagnostics=tuple(diagnostics),
        )

    def ensure(self, request: EnsureRuntimeRequest, progress: ProgressSink) -> OperationResult:
        del progress
        status = self.doctor(request.requirements)
        return OperationResult(
            ok=status.ready,
            provider_id=self.provider_id,
            operation_id="bwrap-host-ensure",
            status="completed" if status.ready else "failed",
            diagnostics=status.diagnostics,
            requires_user_action=not status.ready,
            user_action=status.user_action,
        )


class MacLimaProvider(ManagedUbuntuProvider):
    provider_id = "mac_lima"
    _host_platform = "darwin"
    _launcher_command = "limactl"

    def _command_path(self) -> str | None:
        if self._configured_command_path:
            return self._configured_command_path
        return resolve_limactl_path()

    def _setup_message(
        self,
        *,
        launcher_missing: bool = False,
        missing_capabilities: Sequence[str] = (),
    ) -> str:
        if launcher_missing:
            return (
                "Install Lima with `brew install lima`, then open runtime setup "
                "to provision Tobkiri's hardened Ubuntu guest."
            )
        return super()._setup_message(
            launcher_missing=launcher_missing,
            missing_capabilities=missing_capabilities,
        )

    def ensure(self, request: EnsureRuntimeRequest, progress: ProgressSink) -> OperationResult:
        result = super().ensure(request, progress)
        if not result.ok:
            return result
        command_path = self._require_command()
        try:
            save_lima_runtime_state(
                command_path,
                self._runtime_name,
                runner=self._runner,
            )
        except (OSError, ValueError) as exc:
            return OperationResult(
                ok=False,
                provider_id=self.provider_id,
                operation_id=f"{self.provider_id}-ensure",
                status="failed",
                diagnostics=(
                    Diagnostic(
                        code=RUNTIME_PROVIDER_UNAVAILABLE,
                        message=f"Lima sandbox config attestation failed: {exc}",
                        severity="error",
                    ),
                ),
                requires_user_action=True,
                user_action=(
                    "Recreate the Tobkiri managed Lima runtime with the current "
                    "hardened configuration."
                ),
            )
        return result

    def _guest_exists(self, command_path: str) -> bool:
        result = self._run((command_path, "list", "--format", "{{.Name}}"), timeout=10)
        return result.returncode == 0 and self._runtime_name in {
            line.strip() for line in result.stdout.splitlines()
        }

    def _ensure_guest(self, command_path: str) -> None:
        if self._guest_exists(command_path):
            self._run((command_path, "start", self._runtime_name), timeout=120)
            return
        config_path = _write_lima_config()
        try:
            result = self._run(
                (command_path, "start", "--name", self._runtime_name, config_path), timeout=900
            )
        finally:
            _unlink(config_path)
        if result.returncode != 0:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Lima managed Ubuntu guest could not be created.",
                status_code=503,
                details={"stderr": result.stderr.strip()[:1000]},
            )

    def _stop_guest(self, command_path: str) -> None:
        self._run((command_path, "stop", "--force", self._runtime_name), timeout=60)

    def _delete_guest(self, command_path: str) -> None:
        self._run((command_path, "delete", "--force", self._runtime_name), timeout=120)

    def _guest_prefix(self, command_path: str) -> tuple[str, ...]:
        return (command_path, "shell", self._runtime_name, "--")

    def _version_command(self, command_path: str) -> tuple[str, ...]:
        return (command_path, "--version")


class WindowsWslProvider(ManagedUbuntuProvider):
    provider_id = "windows_wsl"
    _host_platform = "windows"
    _launcher_command = "wsl.exe"

    def __init__(
        self,
        *,
        command_path: str | None = None,
        runner: CommandRunner | None = None,
        runtime_name: str = DEFAULT_WSL_RUNTIME_NAME,
        rootfs_path: str | None = None,
        install_dir: str | None = None,
        rootfs_cache_dir: str | None = None,
        rootfs_url: str | None = None,
        rootfs_downloader: RootfsDownloader | None = None,
        checksum_fetcher: ChecksumFetcher | None = None,
    ) -> None:
        super().__init__(command_path=command_path, runner=runner, runtime_name=runtime_name)
        self._configured_rootfs_path = str(rootfs_path).strip() if rootfs_path else None
        self._configured_install_dir = str(install_dir).strip() if install_dir else None
        self._configured_rootfs_cache_dir = (
            str(rootfs_cache_dir).strip() if rootfs_cache_dir else None
        )
        self._configured_rootfs_url = str(rootfs_url).strip() if rootfs_url else None
        self._rootfs_downloader = rootfs_downloader or _download_file
        self._checksum_fetcher = checksum_fetcher or _fetch_text

    def _guest_exists(self, command_path: str) -> bool:
        result = self._run((command_path, "-l", "-q"), timeout=10)
        stderr_present = (
            result.stderr_present if result.stderr_present is not None else bool(result.stderr)
        )
        if (
            result.returncode != 0
            or result.stdout_decoding_error is not None
            or result.stdout_truncated
            or stderr_present
        ):
            raise self._invalid_guest_probe(result, stderr_present=stderr_present)
        try:
            names = _wsl_distribution_names(result.stdout)
        except ValueError as exc:
            raise self._invalid_guest_probe(
                result,
                stderr_present=stderr_present,
            ) from exc
        return self._runtime_name in names

    @staticmethod
    def _invalid_guest_probe(
        result: GuestCommandResult,
        *,
        stderr_present: bool,
    ) -> _ManagedGuestProbeInvalid:
        return _ManagedGuestProbeInvalid(
            code="WINDOWS_WSL_GUEST_PROBE_INVALID",
            message=(
                "WSL distribution discovery returned an invalid or ambiguous result; "
                "retry before creating a managed guest."
            ),
            status_code=503,
            details={
                "decode_method": result.stdout_decoding,
                "command_exit": result.returncode,
                "stderr_present": stderr_present,
                "stdout_truncated": result.stdout_truncated,
            },
        )

    def _ensure_guest(self, command_path: str) -> None:
        if self._guest_exists(command_path):
            return
        rootfs_path = self._rootfs_path_or_download()
        if not rootfs_path or not os.path.isfile(rootfs_path):
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "RumiUbuntu WSL rootfs tarball is not available.",
                status_code=503,
                details={
                    "env": WSL_ROOTFS_ENV,
                    "runtime_name": self._runtime_name,
                    "download_url": self._rootfs_url(),
                },
            )
        install_dir = self._install_dir()
        os.makedirs(install_dir, exist_ok=True)
        result = self._run(
            (
                command_path,
                "--import",
                self._runtime_name,
                install_dir,
                rootfs_path,
                "--version",
                "2",
            ),
            timeout=900,
        )
        if result.returncode != 0:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "WSL Ubuntu distribution could not be installed.",
                status_code=503,
                details={"stderr": result.stderr.strip()[:1000]},
            )

    def _stop_guest(self, command_path: str) -> None:
        self._run((command_path, "--terminate", self._runtime_name), timeout=60)

    def _delete_guest(self, command_path: str) -> None:
        self._run((command_path, "--unregister", self._runtime_name), timeout=120)

    def _guest_prefix(self, command_path: str) -> tuple[str, ...]:
        return (command_path, "-d", self._runtime_name, "--")

    def _guest_shell(
        self,
        command_path: str,
        script: str,
        *,
        input_text: str | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> GuestCommandResult:
        return self._guest_command(
            command_path,
            ("bash", "-lc", str(script or "")),
            input_text=input_text,
            timeout=timeout,
            check=check,
        )

    def _version_command(self, command_path: str) -> tuple[str, ...]:
        return (command_path, "--version")

    def _rootfs_path(self) -> str | None:
        value = self._configured_rootfs_path or os.environ.get(WSL_ROOTFS_ENV)
        text = str(value or "").strip()
        return text or None

    def _rootfs_path_or_download(self) -> str:
        configured = self._rootfs_path()
        if configured:
            return configured
        return self._download_rootfs()

    def _download_rootfs(self) -> str:
        url = self._rootfs_url()
        if not url:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "RumiUbuntu WSL rootfs download URL could not be resolved for this host architecture.",
                status_code=503,
                details={"arch": _host_arch()},
            )
        cache_dir = self._rootfs_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        filename = (
            os.path.basename(urllib.parse.urlparse(url).path) or "rumi-ubuntu-wsl.rootfs.tar.gz"
        )
        destination = os.path.join(cache_dir, filename)
        sidecar_path = f"{destination}.sha256"
        cached_sha256 = _read_verified_sha256_sidecar(sidecar_path)
        if cached_sha256 and os.path.isfile(destination) and os.path.getsize(destination) > 0:
            actual_sha256 = _sha256_file(destination)
            if actual_sha256.casefold() == cached_sha256.casefold():
                return destination
        expected_sha256 = self._rootfs_expected_sha256(url, filename)
        if expected_sha256 is None:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Ubuntu SHA256SUMS did not provide a checksum for the selected rootfs.",
                status_code=503,
                details={"url": url, "filename": filename},
            )
        if os.path.isfile(destination) and os.path.getsize(destination) > 0:
            actual_sha256 = _sha256_file(destination)
            if actual_sha256.casefold() == expected_sha256.casefold():
                _write_verified_sha256_sidecar(sidecar_path, expected_sha256)
                return destination
            _unlink(destination)
            _unlink(sidecar_path)
        tmp_path = f"{destination}.tmp-{uuid.uuid4().hex}"
        try:
            self._rootfs_downloader(url, tmp_path)
            actual_sha256 = _sha256_file(tmp_path)
            if actual_sha256.casefold() != expected_sha256.casefold():
                raise SandboxContractError(
                    RUNTIME_PROVIDER_UNAVAILABLE,
                    "Downloaded RumiUbuntu WSL rootfs checksum did not match Ubuntu SHA256SUMS.",
                    status_code=503,
                    details={"url": url, "expected": expected_sha256, "actual": actual_sha256},
                )
            if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) <= 0:
                raise SandboxContractError(
                    RUNTIME_PROVIDER_UNAVAILABLE,
                    "Downloaded RumiUbuntu WSL rootfs was empty.",
                    status_code=503,
                    details={"url": url},
                )
            os.replace(tmp_path, destination)
            _write_verified_sha256_sidecar(sidecar_path, expected_sha256)
            return destination
        except SandboxContractError:
            raise
        except Exception as exc:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                f"RumiUbuntu WSL rootfs download failed: {exc}",
                status_code=503,
                details={"url": url, "cache_dir": cache_dir},
            ) from exc
        finally:
            _unlink(tmp_path)

    def _rootfs_url(self) -> str:
        configured = self._configured_rootfs_url or os.environ.get(WSL_ROOTFS_URL_ENV)
        if configured:
            return str(configured).strip()
        return DEFAULT_WSL_ROOTFS_URLS.get(_host_arch(), "")

    def _rootfs_cache_dir(self) -> str:
        if self._configured_rootfs_cache_dir:
            return self._configured_rootfs_cache_dir
        env_dir = str(os.environ.get(WSL_ROOTFS_CACHE_DIR_ENV) or "").strip()
        if env_dir:
            return env_dir
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        if local_app_data:
            return os.path.join(local_app_data, "Rumi AI", "wsl", "rootfs")
        return os.path.join(tempfile.gettempdir(), "rumi-ai", "wsl", "rootfs")

    def _rootfs_expected_sha256(self, url: str, filename: str) -> str | None:
        sums_url = urllib.parse.urljoin(url, "SHA256SUMS")
        try:
            text = self._checksum_fetcher(sums_url)
        except Exception as exc:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Could not fetch Ubuntu SHA256SUMS for RumiUbuntu WSL rootfs.",
                status_code=503,
                details={"url": sums_url, "error": str(exc)},
            ) from exc
        for line in text.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1].lstrip("*") == filename:
                return parts[0]
        raise SandboxContractError(
            RUNTIME_PROVIDER_UNAVAILABLE,
            "Ubuntu SHA256SUMS did not include the selected RumiUbuntu WSL rootfs.",
            status_code=503,
            details={"url": sums_url, "filename": filename},
        )

    def _install_dir(self) -> str:
        if self._configured_install_dir:
            return self._configured_install_dir
        env_dir = str(os.environ.get(WSL_INSTALL_DIR_ENV) or "").strip()
        if env_dir:
            return env_dir
        local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
        if local_app_data:
            return os.path.join(local_app_data, "Rumi AI", "wsl", self._runtime_name)
        return os.path.join(tempfile.gettempdir(), "rumi-ai", "wsl", self._runtime_name)


class ManagedUbuntuGuestAgent:
    def __init__(
        self,
        *,
        provider_id: str,
        provider_instance_id: str,
        command_path: str,
        command_prefix: Sequence[str],
        runner: CommandRunner,
        workspace_dir: str,
        display: str,
        width: int,
        height: int,
        memory_mb: int | None = None,
        cpu_count: float | None = None,
        pids: int | None = None,
        output_bytes: int | None = None,
        timeout_ms: int | None = None,
        network_disabled: bool = False,
    ) -> None:
        self._provider_id = provider_id
        self._provider_instance_id = provider_instance_id
        self._command_path = command_path
        self._command_prefix = tuple(command_prefix)
        self._runner = runner
        self._workspace_dir = workspace_dir
        self._display = display
        self._width = width
        self._height = height
        self._memory_mb = memory_mb
        self._cpu_count = cpu_count
        self._pids = pids
        self._output_bytes = output_bytes
        self._timeout_ms = timeout_ms
        self._network_disabled = network_disabled

    def exec(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        request = GuestExecRequest.from_payload(payload)
        timeout_ms = (
            min(request.timeout_ms, self._timeout_ms) if self._timeout_ms else request.timeout_ms
        )
        argv = _exec_argv(
            "/workspace",
            request.cwd,
            request.env,
            request.argv,
            sandbox_id=sandbox_id,
            provider_instance_id=self._provider_instance_id,
        )
        argv = _resource_limited_argv(
            argv, memory_mb=self._memory_mb, cpu_count=self._cpu_count, pids=self._pids
        )
        argv = self._sandbox_argv(
            sandbox_id,
            argv,
            network_enabled=not self._network_disabled,
        )
        result = self._run(argv, input_text=request.stdin, timeout=max(1, timeout_ms / 1000))
        stdout, stdout_truncated = _bounded_output(result.stdout, self._output_bytes)
        stderr, stderr_truncated = _bounded_output(result.stderr, self._output_bytes)
        return {
            "ok": True,
            "sandbox_id": sandbox_id,
            "argv": list(request.argv),
            "cwd": request.cwd,
            "resolved_cwd": _container_path(self._workspace_dir, request.cwd),
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "client_request_id": request.client_request_id,
            "provider_runtime": self._provider_id,
        }

    def apply_file_patch(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        operations = _file_patch_operations(payload)
        applied: list[dict[str, object]] = []
        for operation in operations:
            path = str(operation["path"])
            content = operation["content"]
            if not isinstance(content, bytes):
                raise SandboxContractError(
                    "INVALID_EXEC_REQUEST",
                    "Sandbox file patch content must be bytes.",
                    status_code=400,
                )
            parent = _container_parent("/workspace", path)
            if parent:
                mkdir = self._run(
                    self._sandbox_argv(
                        sandbox_id,
                        ("mkdir", "-p", parent),
                        network_enabled=False,
                    ),
                    timeout=30,
                )
                if mkdir.returncode != 0:
                    return _guest_error(
                        sandbox_id,
                        "SANDBOX_FILES_FAILED",
                        "Sandbox file patch could not create parent directory.",
                        mkdir,
                    )
            encoded = base64.b64encode(content).decode("ascii")
            script = (
                "import base64, pathlib, sys\n"
                "path = pathlib.Path(sys.argv[1])\n"
                "path.write_bytes(base64.b64decode(sys.stdin.read().encode('ascii')))\n"
            )
            write = self._run(
                self._sandbox_argv(
                    sandbox_id,
                    ("python3", "-c", script, _container_path("/workspace", path)),
                    network_enabled=False,
                ),
                input_text=encoded,
                timeout=60,
            )
            if write.returncode != 0:
                return _guest_error(
                    sandbox_id,
                    "SANDBOX_FILES_FAILED",
                    "Sandbox file patch could not write content.",
                    write,
                )
            applied.append({"path": path, "bytes": len(content)})
        return {
            "ok": True,
            "sandbox_id": sandbox_id,
            "applied": applied,
            "files_written": len(applied),
            "provider_runtime": self._provider_id,
        }

    def expose_port(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        if self._network_disabled and payload.get("_network_policy_approved") is not True:
            raise SandboxContractError(
                "SANDBOX_NETWORK_DENIED",
                "Sandbox port exposure is disabled by the template network policy.",
                status_code=403,
            )
        port = _port_number(payload.get("port"))
        protocol = str(payload.get("protocol") or "http").strip().lower()
        if protocol not in {"http", "https", "tcp"}:
            raise SandboxContractError(
                "INVALID_SANDBOX_PORT",
                "Sandbox port protocol must be http, https, or tcp.",
                status_code=400,
            )
        probe = self._run(_port_probe_argv(port), timeout=5)
        if probe.returncode != 0:
            return _guest_error(
                sandbox_id,
                "SANDBOX_PORTS_NOT_READY",
                "Sandbox port exposure could not verify a listening guest service.",
                probe,
                status_code=503,
            )
        scheme = "http" if protocol == "tcp" else protocol
        url = f"{scheme}://127.0.0.1:{port}"
        return {
            "ok": False,
            "sandbox_id": sandbox_id,
            "port": port,
            "protocol": protocol,
            "url": url,
            "target_url": url,
            "host_reachable": False,
            "forwarding": "unavailable",
            "code": "SANDBOX_PORT_FORWARD_UNAVAILABLE",
            "error": "Managed Ubuntu host port forwarding is not implemented for this provider.",
            "status_code": 501,
            "provider_runtime": self._provider_id,
        }

    def capture_frame(self, sandbox_id: str, seat_id: str) -> dict[str, object]:
        result = self._run(
            (
                "env",
                f"DISPLAY={self._display}",
                "bash",
                "-lc",
                "import -window root png:- | base64 -w0",
            ),
            timeout=30,
        )
        if result.returncode != 0:
            return _guest_error(
                sandbox_id, "SANDBOX_SCREENSHOT_FAILED", "Desktop frame capture failed.", result
            )
        try:
            data = base64.b64decode(result.stdout.strip(), validate=True)
        except Exception as exc:
            raise SandboxContractError(
                "SANDBOX_SCREENSHOT_FAILED",
                "Desktop frame capture returned invalid image data.",
                status_code=502,
            ) from exc
        return {
            "ok": True,
            "sandbox_id": sandbox_id,
            "seat_id": seat_id,
            "content_type": "image/png",
            "data": data,
            "width": self._width,
            "height": self._height,
            "source": self._provider_id,
        }

    def desktop_input(
        self,
        sandbox_id: str,
        seat_id: str,
        payload: Mapping[str, object],
        *,
        actor: str = "human",
    ) -> dict[str, object]:
        request = DesktopInputRequest.from_payload(
            payload, width=self._width, height=self._height, require_lease=False
        )
        result = self._dispatch_input(request)
        return {
            "ok": result.returncode == 0,
            "sandbox_id": sandbox_id,
            "seat_id": seat_id,
            "action": request.action,
            "actor": actor,
            "error": result.stderr or None,
            "provider_runtime": self._provider_id,
        }

    def _dispatch_input(self, request: DesktopInputRequest) -> GuestCommandResult:
        prefix = ("env", f"DISPLAY={self._display}", "xdotool")
        if request.action == "move":
            return self._run(
                (*prefix, "mousemove", str(int(request.x or 0)), str(int(request.y or 0))), timeout=10
            )
        if request.action == "click":
            return self._run(
                (
                    *prefix,
                    "mousemove",
                    str(int(request.x or 0)),
                    str(int(request.y or 0)),
                    "click",
                    _button(request.button),
                ),
                timeout=10,
            )
        if request.action == "double_click":
            return self._run(
                (
                    *prefix,
                    "mousemove",
                    str(int(request.x or 0)),
                    str(int(request.y or 0)),
                    "click",
                    "--repeat",
                    "2",
                    _button(request.button),
                ),
                timeout=10,
            )
        if request.action == "drag":
            return self._run(
                (
                    *prefix,
                    "mousemove",
                    str(int(request.x or 0)),
                    str(int(request.y or 0)),
                    "mousedown",
                    _button(request.button),
                    "mousemove",
                    str(int(request.to_x or 0)),
                    str(int(request.to_y or 0)),
                    "mouseup",
                    _button(request.button),
                ),
                timeout=15,
            )
        if request.action == "scroll":
            clicks = max(1, abs(int(request.delta_y or request.delta_x or 1)))
            button = "5" if int(request.delta_y or 0) >= 0 else "4"
            return self._run(
                (
                    *prefix,
                    "mousemove",
                    str(int(request.x or 0)),
                    str(int(request.y or 0)),
                    "click",
                    "--repeat",
                    str(clicks),
                    button,
                ),
                timeout=10,
            )
        if request.action == "type_text":
            return self._run((*prefix, "type", "--", str(request.text or "")), timeout=30)
        if request.action == "key":
            return self._run((*prefix, "key", str(request.key or "")), timeout=10)
        return GuestCommandResult(returncode=1, stderr="Unsupported desktop input action.")

    def _sandbox_argv(
        self,
        sandbox_id: str,
        argv: Sequence[str],
        *,
        network_enabled: bool,
    ) -> tuple[str, ...]:
        return build_guest_bwrap_argv(
            workspace=self._workspace_dir,
            cwd="/workspace",
            argv=argv,
            env={
                "HOME": "/home",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "RUMI_SANDBOX_ID": sandbox_id,
            },
            network_enabled=network_enabled,
        )

    def _run(
        self, argv: Sequence[str], input_text: str | None = None, timeout: float | None = None
    ) -> GuestCommandResult:
        try:
            return self._runner((*self._command_prefix, *argv), input_text, timeout)
        except TimeoutError as exc:
            return GuestCommandResult(returncode=124, stderr=str(exc))
        except OSError as exc:
            return GuestCommandResult(returncode=127, stderr=str(exc))


def _subprocess_runner(
    command: Sequence[str], input_text: str | None, timeout: float | None
) -> GuestCommandResult:
    argv = tuple(command)
    executable_name = os.path.basename(str(argv[0]).replace("\\", "/")).casefold()
    stdout_decoding = (
        "windows-wsl-list"
        if executable_name == "wsl.exe" and argv[1:] == ("-l", "-q")
        else "utf-8"
    )
    try:
        completed = run_cancellable_subprocess(
            argv,
            input_text=input_text,
            timeout=timeout,
            stdout_decoding=stdout_decoding,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(str(exc)) from exc
    return GuestCommandResult(
        returncode=int(completed.returncode),
        stdout=str(completed.stdout or ""),
        stderr=str(completed.stderr or ""),
        stdout_decoding=completed.stdout_decoding,
        stdout_decoding_error=completed.stdout_decoding_error,
        stdout_truncated=completed.stdout_truncated,
        stderr_present=completed.stderr_present,
    )


def _command_version(command_path: str) -> str | None:
    try:
        result = run_cancellable_subprocess((command_path, "--version"), timeout=2)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    text = (result.stdout or result.stderr or "").strip()
    return text.splitlines()[0] if text else None


def _unprivileged_userns_available() -> bool:
    try:
        with open("/proc/sys/kernel/unprivileged_userns_clone", encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return platform.system().lower() != "linux"
    return value not in {"0", "false", "False"}


def _write_lima_config() -> str:
    content = """minimumLimaVersion: "2.0.0"
vmType: vz
images:
- location: https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img
  arch: x86_64
- location: https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-arm64.img
  arch: aarch64
mounts: []
networks: []
containerd:
  system: false
  user: false
ssh:
  forwardAgent: false
  forwardX11: false
  forwardX11Trusted: false
propagateProxyEnv: false
hostResolver:
  enabled: false
portForwards:
- guestIP: 0.0.0.0
  guestPortRange: [1, 65535]
  proto: any
  ignore: true
provision:
- mode: system
  script: |
    #!/bin/sh
    set -e
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y \
      xvfb openbox xdotool imagemagick python3 xterm x11-utils \
      ca-certificates coreutils util-linux bubblewrap
    install -d -m 0755 /workspace
    install -d -m 0755 /data
    install -d -m 0711 -o {{.User}} -g {{.User}} /var/lib/rumi/workspaces
    install -d -m 0711 -o {{.User}} -g {{.User}} /var/lib/rumi/pack-data
"""
    handle = tempfile.NamedTemporaryFile(prefix="rumi-lima-", suffix=".yaml", delete=False)
    try:
        handle.write(content.encode("utf-8"))
        return handle.name
    finally:
        handle.close()


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _download_file(url: str, destination: str) -> None:
    with urllib.request.urlopen(url, timeout=900) as response, open(destination, "wb") as handle:
        shutil.copyfileobj(response, handle)


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_verified_sha256_sidecar(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip().split()[0]
    except (OSError, IndexError):
        return None
    if len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value):
        return value
    return None


def _write_verified_sha256_sidecar(path: str, digest: str) -> None:
    tmp_path = f"{path}.tmp-{uuid.uuid4().hex}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(f"{digest.strip().lower()}\n")
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
    finally:
        _unlink(tmp_path)


def _host_arch() -> str:
    value = (
        os.environ.get("PROCESSOR_ARCHITECTURE")
        or os.environ.get("PROCESSOR_ARCHITEW6432")
        or platform.machine()
    )
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"amd64", "x86_64", "x64"}:
        return "amd64"
    if normalized in {"arm64", "aarch64"}:
        return "arm64"
    return normalized


def _workspace_binding(opaque_state: Mapping[str, object]) -> Mapping[str, object]:
    workspace = opaque_state.get("workspace_binding")
    return workspace if isinstance(workspace, Mapping) else {}


def _normalized_guest_display(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(":") and text[1:].isdigit():
        return text
    if text.isdigit():
        return f":{text}"
    return ""


def _usable_host_workspace_root(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return os.path.isabs(value) and os.path.isdir(value)


def _workspace_seed_payload(root: str) -> str:
    root_path = os.path.abspath(root)
    total_bytes = 0
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))
            )
            rel_dir = os.path.relpath(dirpath, root_path)
            if rel_dir != ".":
                info = archive.gettarinfo(dirpath, arcname=rel_dir)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                archive.addfile(info)
            for filename in sorted(filenames):
                host_path = os.path.join(dirpath, filename)
                if os.path.islink(host_path) or not os.path.isfile(host_path):
                    continue
                size = os.path.getsize(host_path)
                total_bytes += size
                if total_bytes > MAX_WORKSPACE_SEED_BYTES:
                    raise SandboxContractError(
                        "MANAGED_UBUNTU_WORKSPACE_TOO_LARGE",
                        "Managed Ubuntu workspace seed is too large.",
                        status_code=413,
                        details={"max_bytes": MAX_WORKSPACE_SEED_BYTES},
                    )
                arcname = filename if rel_dir == "." else f"{rel_dir}/{filename}"
                info = archive.gettarinfo(host_path, arcname=arcname)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                with open(host_path, "rb") as handle:
                    archive.addfile(info, handle)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _workspace_seed_script(mode: str, workspace_dir: str) -> str:
    quoted_workspace = shlex.quote(workspace_dir)
    python_script = (
        "import base64, io, os, pathlib, sys, tarfile\n"
        "data = base64.b64decode(sys.stdin.read().encode('ascii'))\n"
        "root = pathlib.Path(sys.argv[1]).resolve()\n"
        "written = []\n"
        "with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:\n"
        "    for member in archive.getmembers():\n"
        "        target = (root / member.name).resolve()\n"
        "        if target != root and root not in target.parents:\n"
        "            raise SystemExit('unsafe workspace archive member')\n"
        "        if member.isdir():\n"
        "            target.mkdir(parents=True, exist_ok=True)\n"
        "            written.append(target)\n"
        "        elif member.isfile():\n"
        "            target.parent.mkdir(parents=True, exist_ok=True)\n"
        "            source = archive.extractfile(member)\n"
        "            if source is None:\n"
        "                raise SystemExit('workspace archive file missing payload')\n"
        "            target.write_bytes(source.read())\n"
        "            written.append(target)\n"
        "if os.environ.get('RUMI_WORKSPACE_SEED_MODE') == 'read_only':\n"
        "    for target in sorted(written, key=lambda item: len(item.parts), reverse=True):\n"
        "        try:\n"
        "            target.chmod(target.stat().st_mode & ~0o222)\n"
        "        except OSError:\n"
        "            pass\n"
    )
    return (
        "set -e\n"
        f"mkdir -p {quoted_workspace}\n"
        f"chmod -R u+w {quoted_workspace} >/dev/null 2>&1 || true\n"
        f"find {quoted_workspace} -mindepth 1 -maxdepth 1 ! -name .rumi -exec rm -rf {{}} +\n"
        f"RUMI_WORKSPACE_SEED_MODE={shlex.quote(mode)} python3 -c {shlex.quote(python_script)} {quoted_workspace}\n"
    )


def _desktop_start_script(
    provider_instance_id: str,
    workspace_dir: str,
    width: int,
    height: int,
    display: str,
    network_disabled: bool,
    startup: Mapping[str, object] | None = None,
) -> str:
    runtime_dir = _runtime_dir(provider_instance_id)
    quoted_workspace = shlex.quote(workspace_dir)
    startup = startup or {}
    starter = str(startup.get("starter") or "empty").strip().lower()
    browser_url = str(startup.get("browser_url") or "").strip()
    network_flag = "1" if network_disabled else "0"
    script = (
        "set -e\n"
        f"mkdir -p {quoted_workspace} {runtime_dir}\n"
        f"DISPLAY_ID={display!r}\n"
        f"RUMI_NETWORK_DISABLED={network_flag!r}\n"
        f"RUMI_SANDBOX_INSTANCE={shlex.quote(provider_instance_id)}\n"
        f"RUMI_SANDBOX_WORKSPACE={quoted_workspace}\n"
        'DISPLAY_NUM="${DISPLAY_ID#:}"\n'
        "rumi_run() {\n"
        "  if [ \"$RUMI_NETWORK_DISABLED\" = '1' ]; then\n"
        "    command -v unshare >/dev/null 2>&1 || { echo 'unshare is required for sandbox network policy' >&2; return 126; }\n"
        '    unshare --user --map-root-user --net -- "$@"\n'
        "  else\n"
        '    "$@"\n'
        "  fi\n"
        "}\n"
        "if [ \"$RUMI_NETWORK_DISABLED\" = '1' ]; then rumi_run true; fi\n"
        "RUMI_X11_TCP=0\n"
        "mkdir -p /tmp/.X11-unix\n"
        "chmod 1777 /tmp/.X11-unix 2>/dev/null || RUMI_X11_TCP=1\n"
        "XVFB_TRANSPORT_ARGS='-nolisten tcp'\n"
        'CLIENT_DISPLAY="$DISPLAY_ID"\n'
        "if [ \"$RUMI_X11_TCP\" = '1' ]; then\n"
        "  XVFB_TRANSPORT_ARGS='-nolisten local -listen tcp'\n"
        '  CLIENT_DISPLAY="127.0.0.1:${DISPLAY_NUM}.0"\n'
        "fi\n"
        "run_ui() {\n"
        '  if [ "$RUMI_X11_TCP" = \'1\' ] && [ "$RUMI_NETWORK_DISABLED" != \'1\' ]; then "$@"; else rumi_run "$@"; fi\n'
        "}\n"
        "run_display_service() {\n"
        '  if [ "$RUMI_X11_TCP" = \'1\' ]; then "$@"; else rumi_run "$@"; fi\n'
        "}\n"
        "run_detached() {\n"
        '  pidfile="$1"\n'
        "  shift\n"
        '  run_ui setsid -f sh -c \'echo $$ > "$1"; shift; exec "$@"\' rumi-detached "$pidfile" "$@"\n'
        "}\n"
        "rumi_process_matches_instance() {\n"
        '  pid="$1"\n'
        '  command_name="$2"\n'
        "  case \"$pid\" in ''|*[!0-9]*) return 1;; esac\n"
        '  kill -0 "$pid" >/dev/null 2>&1 || return 1\n'
        '  [ "$(cat "/proc/$pid/comm" 2>/dev/null || true)" = "$command_name" ] || return 1\n'
        "  tr '\\0' '\\n' < \"/proc/$pid/environ\" 2>/dev/null | grep -qx \"RUMI_SANDBOX_INSTANCE=$RUMI_SANDBOX_INSTANCE\"\n"
        "}\n"
        "rumi_pidfile_alive() {\n"
        '  pidfile="$1"\n'
        '  command_name="$2"\n'
        '  [ -f "$pidfile" ] || return 1\n'
        '  pid="$(cat "$pidfile" 2>/dev/null || true)"\n'
        '  rumi_process_matches_instance "$pid" "$command_name"\n'
        "}\n"
        "rumi_find_instance_pid() {\n"
        '  command_name="$1"\n'
        "  for envfile in /proc/[0-9]*/environ; do\n"
        '    [ -r "$envfile" ] || continue\n'
        '    pid="${envfile#/proc/}"\n'
        '    pid="${pid%/environ}"\n'
        '    rumi_process_matches_instance "$pid" "$command_name" || continue\n'
        '    echo "$pid"\n'
        "    return 0\n"
        "  done\n"
        "  return 1\n"
        "}\n"
        "if [ ! -s /etc/machine-id ] && [ -w /etc ]; then\n"
        "  RUMI_MACHINE_ID=\"$(tr -d '-' < /proc/sys/kernel/random/uuid | head -c 32)\"\n"
        '  if [ -n "$RUMI_MACHINE_ID" ]; then printf \'%s\' "$RUMI_MACHINE_ID" > /etc/machine-id; fi\n'
        "fi\n"
        f'echo "$CLIENT_DISPLAY" > {runtime_dir}/display.env\n'
        'if [ -n "$DISPLAY_NUM" ] && [ ! -S "/tmp/.X11-unix/X$DISPLAY_NUM" ]; then rm -f "/tmp/.X${DISPLAY_NUM}-lock"; fi\n'
        f"if ! rumi_pidfile_alive {runtime_dir}/xvfb.pid Xvfb; then\n"
        f'  existing_xvfb="$(rumi_find_instance_pid Xvfb || true)"\n'
        f'  if [ -n "$existing_xvfb" ]; then echo "$existing_xvfb" > {runtime_dir}/xvfb.pid; fi\n'
        "fi\n"
        f"if ! rumi_pidfile_alive {runtime_dir}/xvfb.pid Xvfb; then\n"
        f'  run_display_service setsid env RUMI_SANDBOX_INSTANCE="$RUMI_SANDBOX_INSTANCE" RUMI_SANDBOX_WORKSPACE="$RUMI_SANDBOX_WORKSPACE" Xvfb {display} -screen 0 {width}x{height}x24 $XVFB_TRANSPORT_ARGS >{runtime_dir}/xvfb.log 2>&1 & echo $! > {runtime_dir}/xvfb.pid\n'
        "  sleep 0.5\n"
        f'  launched_xvfb="$(rumi_find_instance_pid Xvfb || true)"\n'
        f'  if [ -n "$launched_xvfb" ]; then echo "$launched_xvfb" > {runtime_dir}/xvfb.pid; fi\n'
        "fi\n"
        f"if ! rumi_pidfile_alive {runtime_dir}/xvfb.pid Xvfb; then\n"
        "  echo 'Desktop Xvfb failed to stay running.' >&2\n"
        f"  cat {runtime_dir}/xvfb.log >&2 || true\n"
        "  exit 126\n"
        "fi\n"
        f"if ! rumi_pidfile_alive {runtime_dir}/openbox.pid openbox; then\n"
        f'  existing_openbox="$(rumi_find_instance_pid openbox || true)"\n'
        f'  if [ -n "$existing_openbox" ]; then echo "$existing_openbox" > {runtime_dir}/openbox.pid; fi\n'
        "fi\n"
        f"if ! rumi_pidfile_alive {runtime_dir}/openbox.pid openbox; then\n"
        f'  run_display_service setsid env RUMI_SANDBOX_INSTANCE="$RUMI_SANDBOX_INSTANCE" RUMI_SANDBOX_WORKSPACE="$RUMI_SANDBOX_WORKSPACE" DISPLAY="$CLIENT_DISPLAY" openbox >{runtime_dir}/openbox.log 2>&1 & echo $! > {runtime_dir}/openbox.pid\n'
        "  sleep 0.2\n"
        f'  launched_openbox="$(rumi_find_instance_pid openbox || true)"\n'
        f'  if [ -n "$launched_openbox" ]; then echo "$launched_openbox" > {runtime_dir}/openbox.pid; fi\n'
        "fi\n"
        f"if ! rumi_pidfile_alive {runtime_dir}/openbox.pid openbox; then\n"
        "  echo 'Desktop openbox failed to stay running.' >&2\n"
        f"  cat {runtime_dir}/openbox.log >&2 || true\n"
        "  exit 126\n"
        "fi\n"
    )
    if starter == "terminal":
        script += (
            f"if command -v xterm >/dev/null 2>&1; then\n"
            f'  run_detached {runtime_dir}/starter-terminal.pid env RUMI_SANDBOX_INSTANCE="$RUMI_SANDBOX_INSTANCE" RUMI_SANDBOX_WORKSPACE="$RUMI_SANDBOX_WORKSPACE" DISPLAY="$CLIENT_DISPLAY" xterm -title \'Rumi Desktop\' -e bash -lc \'cd "$1"; exec bash\' rumi-terminal {quoted_workspace} >{runtime_dir}/starter-terminal.log 2>&1\n'
            "else\n"
            f"  echo 'xterm is not installed; terminal starter skipped' >{runtime_dir}/starter-terminal.log\n"
            "fi\n"
        )
    elif starter in {"browser", "browser_url"}:
        if network_disabled and browser_url:
            script += f"echo 'browser_url starter skipped by sandbox network policy' >{runtime_dir}/starter-browser.log\n"
        else:
            script += (
                _browser_url_assignment_script(browser_url) + "BROWSER_BIN=''\n"
                "BROWSER_CANDIDATES='google-chrome-stable google-chrome chromium chromium-browser firefox'\n"
                'if [ -n "$BROWSER_URL" ]; then BROWSER_CANDIDATES="$BROWSER_CANDIDATES xdg-open"; fi\n'
                "for candidate in $BROWSER_CANDIDATES; do\n"
                '  if command -v "$candidate" >/dev/null 2>&1; then BROWSER_BIN="$candidate"; break; fi\n'
                "done\n"
                'if [ -n "$BROWSER_BIN" ]; then\n'
                "  mkdir -p " + runtime_dir + "/browser-profile\n"
                "  if [ \"$BROWSER_BIN\" = 'xdg-open' ]; then\n"
                f'    run_detached {runtime_dir}/starter-browser.pid env RUMI_SANDBOX_INSTANCE="$RUMI_SANDBOX_INSTANCE" RUMI_SANDBOX_WORKSPACE="$RUMI_SANDBOX_WORKSPACE" DISPLAY="$CLIENT_DISPLAY" "$BROWSER_BIN" "$BROWSER_URL" >{runtime_dir}/starter-browser.log 2>&1\n'
                '  elif [ -n "$BROWSER_URL" ]; then\n'
                f'    run_detached {runtime_dir}/starter-browser.pid env RUMI_SANDBOX_INSTANCE="$RUMI_SANDBOX_INSTANCE" RUMI_SANDBOX_WORKSPACE="$RUMI_SANDBOX_WORKSPACE" DISPLAY="$CLIENT_DISPLAY" "$BROWSER_BIN" --no-sandbox --no-first-run --disable-dev-shm-usage --user-data-dir={runtime_dir}/browser-profile "$BROWSER_URL" >{runtime_dir}/starter-browser.log 2>&1\n'
                "  else\n"
                f'    run_detached {runtime_dir}/starter-browser.pid env RUMI_SANDBOX_INSTANCE="$RUMI_SANDBOX_INSTANCE" RUMI_SANDBOX_WORKSPACE="$RUMI_SANDBOX_WORKSPACE" DISPLAY="$CLIENT_DISPLAY" "$BROWSER_BIN" --no-sandbox --no-first-run --disable-dev-shm-usage --user-data-dir={runtime_dir}/browser-profile >{runtime_dir}/starter-browser.log 2>&1\n'
                "  fi\n"
                "else\n"
                f"  echo 'No browser executable found; browser starter skipped' >{runtime_dir}/starter-browser.log\n"
                "fi\n"
            )
    return script


def _browser_url_assignment_script(browser_url: str) -> str:
    quoted_url = shlex.quote(str(browser_url or "").strip())
    rewrite_script = (
        "import sys\n"
        "from urllib.parse import urlsplit, urlunsplit\n"
        "raw = sys.argv[1]\n"
        "host_alias = sys.argv[2].strip()\n"
        "parsed = urlsplit(raw)\n"
        "host = (parsed.hostname or '').lower()\n"
        "if parsed.scheme in {'http', 'https'} and host in {'127.0.0.1', 'localhost'} and host_alias:\n"
        "    if parsed.port:\n"
        "        netloc = f'{host_alias}:{parsed.port}'\n"
        "    else:\n"
        "        netloc = host_alias\n"
        "    raw = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))\n"
        "print(raw)\n"
    )
    return (
        f"BROWSER_URL_ORIGINAL={quoted_url}\n"
        "RUMI_HOST_LOOPBACK_ALIAS=''\n"
        "for candidate in host.lima.internal host.docker.internal; do\n"
        '  if command -v getent >/dev/null 2>&1 && getent hosts "$candidate" >/dev/null 2>&1; then\n'
        '    RUMI_HOST_LOOPBACK_ALIAS="$candidate"\n'
        "    break\n"
        "  fi\n"
        "done\n"
        'if [ -z "$RUMI_HOST_LOOPBACK_ALIAS" ] && [ -r /etc/resolv.conf ]; then\n'
        "  RUMI_HOST_LOOPBACK_ALIAS=\"$(awk '/^nameserver[[:space:]]+/ {print $2; exit}' /etc/resolv.conf)\"\n"
        "fi\n"
        'BROWSER_URL="$(python3 - "$BROWSER_URL_ORIGINAL" "$RUMI_HOST_LOOPBACK_ALIAS" <<\'PY\'\n'
        f"{rewrite_script}"
        "PY\n"
        ')"\n'
    )


def _guest_used_displays_script() -> str:
    return (
        "set +e\n"
        "rumi_emit_display() {\n"
        '  value="$1"\n'
        '  value="${value#:}"\n'
        '  value="${value#.X}"\n'
        '  value="${value#X}"\n'
        '  value="${value%-lock}"\n'
        '  value="${value%%.*}"\n'
        "  case \"$value\" in ''|*[!0-9]*) return 0;; esac\n"
        '  echo ":$value"\n'
        "}\n"
        "for path in /tmp/.X11-unix/X* /tmp/.X[0-9]*-lock; do\n"
        '  [ -e "$path" ] || continue\n'
        '  rumi_emit_display "$(basename "$path")"\n'
        "done\n"
        "for cmdline in /proc/[0-9]*/cmdline; do\n"
        '  [ -r "$cmdline" ] || continue\n'
        "  args=\"$(tr '\\0' ' ' < \"$cmdline\" 2>/dev/null || true)\"\n"
        '  case "$args" in *Xvfb*) ;; *) continue;; esac\n'
        "  for arg in $args; do\n"
        '    case "$arg" in :[0-9]*|:[0-9]*.*) rumi_emit_display "$arg";; esac\n'
        "  done\n"
        "done | sort -u\n"
    )


def _guest_provisioning_script(
    provider_instance_id: str,
    workspace_dir: str,
    apt_packages: Sequence[str],
    mcp_servers: Sequence[str],
) -> str:
    runtime_dir = _runtime_dir(provider_instance_id)
    quoted_workspace = shlex.quote(workspace_dir)
    package_list = " ".join(shlex.quote(package) for package in apt_packages)
    mcp_lines = "\n".join(str(server) for server in mcp_servers)
    mcp_payload = shlex.quote(mcp_lines)
    marker_key = shlex.quote("|".join((*apt_packages, "--", *mcp_servers)))
    script = (
        "set -e\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        f"{SUDO_BOOTSTRAP_SCRIPT}"
        f"mkdir -p {quoted_workspace}/.rumi {runtime_dir}\n"
        f"PROVISION_MARKER={runtime_dir}/provisioning.key\n"
        f"PROVISION_KEY={marker_key}\n"
        'if [ "$(cat "$PROVISION_MARKER" 2>/dev/null || true)" != "$PROVISION_KEY" ]; then\n'
    )
    if apt_packages:
        script += (
            f"  RUMI_APT_PACKAGES={shlex.quote(package_list)}\n"
            "  if printf '%s\n' \"$RUMI_APT_PACKAGES\" | grep -qw google-chrome-stable; then\n"
            '    if [ "$(dpkg --print-architecture 2>/dev/null || true)" = amd64 ]; then\n'
            "      $RUMI_SUDO apt-get update\n"
            "      $RUMI_SUDO apt-get install -y ca-certificates wget gnupg\n"
            "      wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor | $RUMI_SUDO tee /usr/share/keyrings/google-linux-signing-keyring.gpg >/dev/null\n"
            "      echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux-signing-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main' | $RUMI_SUDO tee /etc/apt/sources.list.d/google-chrome.list >/dev/null\n"
            "    else\n"
            "      echo 'google-chrome-stable provisioning requires amd64; chromium-browser apt package is not a usable fallback in managed Ubuntu' >&2\n"
            "      exit 1\n"
            "    fi\n"
            "  fi\n"
            "  $RUMI_SUDO apt-get update\n"
            "  $RUMI_SUDO apt-get install -y $RUMI_APT_PACKAGES\n"
        )
    if mcp_servers:
        script += (
            f"  printf '%s\n' {mcp_payload} > {quoted_workspace}/.rumi/mcp_servers.txt\n"
            f"  if grep -qx playwright {quoted_workspace}/.rumi/mcp_servers.txt && command -v npm >/dev/null 2>&1; then\n"
            "    $RUMI_SUDO npm install -g @playwright/mcp || echo 'playwright mcp install failed' >>"
            + runtime_dir
            + "/provisioning.log\n"
            "  fi\n"
        )
    script += '  printf \'%s\n\' "$PROVISION_KEY" > "$PROVISION_MARKER"\nfi\n'
    return script


def _guest_provisioning_input(instance: ProviderInstance) -> dict[str, object]:
    provisioning = instance.opaque_state.get("desktop_provisioning")
    merged: dict[str, object] = dict(provisioning) if isinstance(provisioning, Mapping) else {}
    packages: list[object] = []
    template_packages = instance.opaque_state.get("template_packages")
    if isinstance(template_packages, Sequence) and not isinstance(template_packages, (str, bytes)):
        packages.extend(template_packages)
    declared_packages = merged.get("packages")
    if isinstance(declared_packages, Sequence) and not isinstance(declared_packages, (str, bytes)):
        packages.extend(declared_packages)
    if packages:
        merged["packages"] = packages
    return merged


def _guest_provisioning_apt_packages(provisioning: Mapping[str, object]) -> tuple[str, ...]:
    requested: list[str] = []
    raw_packages = provisioning.get("packages")
    if isinstance(raw_packages, Sequence) and not isinstance(raw_packages, (str, bytes)):
        for item in raw_packages:
            if isinstance(item, Mapping):
                requested.append(str(item.get("name") or ""))
            else:
                requested.append(str(item or ""))
    raw_apps = provisioning.get("apps")
    if isinstance(raw_apps, Sequence) and not isinstance(raw_apps, (str, bytes)):
        requested.extend(str(item or "") for item in raw_apps)

    packages: list[str] = []
    for name in requested:
        normalized = _normalize_guest_package_name(name)
        if not normalized:
            continue
        for package in GUEST_APP_PACKAGE_MAP.get(normalized, ()):
            if package not in packages:
                packages.append(package)
    return tuple(packages)


def _guest_provisioning_mcp_servers(provisioning: Mapping[str, object]) -> tuple[str, ...]:
    raw_servers = provisioning.get("mcp_servers")
    if not isinstance(raw_servers, Sequence) or isinstance(raw_servers, (str, bytes)):
        return ()
    servers: list[str] = []
    for item in raw_servers:
        normalized = _normalize_guest_package_name(str(item or ""))
        if normalized and normalized not in servers:
            servers.append(normalized)
    return tuple(servers)


def _normalize_guest_package_name(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if not normalized:
        return ""
    if not all(ch.isalnum() or ch in {"-", "."} for ch in normalized):
        return ""
    return normalized


def _desktop_stop_script(provider_instance_id: str) -> str:
    runtime_dir = _runtime_dir(provider_instance_id)
    instance = shlex.quote(provider_instance_id)
    return (
        "set +e\n"
        "rumi_kill_pidfile() {\n"
        '  signal="$1"\n'
        '  pidfile="$2"\n'
        '  [ -f "$pidfile" ] || return 0\n'
        '  pid="$(cat "$pidfile" 2>/dev/null || true)"\n'
        '  case "$pid" in \'\'|*[!0-9]*) rm -f "$pidfile"; return 0;; esac\n'
        '  kill -"$signal" -- "-$pid" >/dev/null 2>&1 || kill -"$signal" "$pid" >/dev/null 2>&1 || true\n'
        '  rm -f "$pidfile"\n'
        "}\n"
        f"for pidfile in {runtime_dir}/starter-browser.pid {runtime_dir}/starter-terminal.pid {runtime_dir}/openbox.pid {runtime_dir}/xvfb.pid {runtime_dir}/procs/*.pid; do\n"
        '  rumi_kill_pidfile TERM "$pidfile"\n'
        "done\n"
        "sleep 0.2\n"
        f"for pidfile in {runtime_dir}/starter-browser.pid {runtime_dir}/starter-terminal.pid {runtime_dir}/openbox.pid {runtime_dir}/xvfb.pid {runtime_dir}/procs/*.pid; do\n"
        '  rumi_kill_pidfile KILL "$pidfile"\n'
        "done\n"
        f"RUMI_SANDBOX_INSTANCE={instance}\n"
        "rumi_kill_instance_processes() {\n"
        '  signal="$1"\n'
        "  for envfile in /proc/[0-9]*/environ; do\n"
        '    [ -r "$envfile" ] || continue\n'
        '    pid="${envfile#/proc/}"\n'
        '    pid="${pid%/environ}"\n'
        '    [ "$pid" = "$$" ] && continue\n'
        "    tr '\\0' '\\n' < \"$envfile\" 2>/dev/null | grep -qx \"RUMI_SANDBOX_INSTANCE=$RUMI_SANDBOX_INSTANCE\" || continue\n"
        '    kill -"$signal" "$pid" >/dev/null 2>&1 || true\n'
        "  done\n"
        "}\n"
        "rumi_kill_instance_processes TERM\n"
        "sleep 0.2\n"
        "rumi_kill_instance_processes KILL\n"
    )


def _instance_destroy_script(provider_instance_id: str, workspace_dir: str) -> str:
    return (
        _desktop_stop_script(provider_instance_id)
        + f"rm -rf {shlex.quote(_runtime_dir(provider_instance_id))} {shlex.quote(workspace_dir)}\n"
    )


def _desktop_running_script(provider_instance_id: str) -> str:
    runtime_dir = _runtime_dir(provider_instance_id)
    instance = shlex.quote(provider_instance_id)
    return (
        "set +e\n"
        f"RUMI_SANDBOX_INSTANCE={instance}\n"
        "rumi_process_matches_instance() {\n"
        '  pid="$1"\n'
        '  command_name="$2"\n'
        "  case \"$pid\" in ''|*[!0-9]*) return 1;; esac\n"
        '  kill -0 "$pid" >/dev/null 2>&1 || return 1\n'
        '  [ "$(cat "/proc/$pid/comm" 2>/dev/null || true)" = "$command_name" ] || return 1\n'
        "  tr '\\0' '\\n' < \"/proc/$pid/environ\" 2>/dev/null | grep -qx \"RUMI_SANDBOX_INSTANCE=$RUMI_SANDBOX_INSTANCE\"\n"
        "}\n"
        "rumi_pidfile_alive() {\n"
        '  pidfile="$1"\n'
        '  command_name="$2"\n'
        '  [ -f "$pidfile" ] || return 1\n'
        '  pid="$(cat "$pidfile" 2>/dev/null || true)"\n'
        '  rumi_process_matches_instance "$pid" "$command_name"\n'
        "}\n"
        "rumi_find_instance_pid() {\n"
        '  command_name="$1"\n'
        "  for envfile in /proc/[0-9]*/environ; do\n"
        '    [ -r "$envfile" ] || continue\n'
        '    pid="${envfile#/proc/}"\n'
        '    pid="${pid%/environ}"\n'
        '    rumi_process_matches_instance "$pid" "$command_name" || continue\n'
        '    echo "$pid"\n'
        "    return 0\n"
        "  done\n"
        "  return 1\n"
        "}\n"
        f"if ! rumi_pidfile_alive {runtime_dir}/xvfb.pid Xvfb; then\n"
        f'  pid="$(rumi_find_instance_pid Xvfb || true)"\n'
        f'  [ -n "$pid" ] && echo "$pid" > {runtime_dir}/xvfb.pid\n'
        "fi\n"
        f"if ! rumi_pidfile_alive {runtime_dir}/openbox.pid openbox; then\n"
        f'  pid="$(rumi_find_instance_pid openbox || true)"\n'
        f'  [ -n "$pid" ] && echo "$pid" > {runtime_dir}/openbox.pid\n'
        "fi\n"
        f"rumi_pidfile_alive {runtime_dir}/xvfb.pid Xvfb && rumi_pidfile_alive {runtime_dir}/openbox.pid openbox\n"
    )


def _instance_exists_script(provider_instance_id: str, workspace_dir: str) -> str:
    return f"test -d {shlex.quote(_runtime_dir(provider_instance_id))} || test -d {shlex.quote(workspace_dir)}"


def _runtime_dir(provider_instance_id: str) -> str:
    return f"/tmp/rumi-managed-runtime/{_safe_instance_name(provider_instance_id)}"


def _instance_workspace_dir(provider_instance_id: str) -> str:
    return f"{GUEST_WORKDIR}/{_safe_instance_name(provider_instance_id)}"


def _instance_workspace_dir_for(instance: ProviderInstance) -> str:
    workspace = str(instance.opaque_state.get("guest_workspace") or "").strip()
    if workspace.startswith(f"{GUEST_WORKDIR}/"):
        return workspace
    return _instance_workspace_dir(instance.provider_instance_id)


def _safe_instance_name(provider_instance_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in provider_instance_id)


def _exec_argv(
    workspace_dir: str,
    cwd: str,
    env: Mapping[str, str],
    argv: Sequence[str],
    *,
    sandbox_id: str,
    provider_instance_id: str,
) -> tuple[str, ...]:
    cwd_path = _container_path(workspace_dir, cwd)
    runtime_dir = _runtime_dir(provider_instance_id)
    env_pairs = _exec_env_pairs(
        env,
        workspace_dir=workspace_dir,
        sandbox_id=sandbox_id,
        provider_instance_id=provider_instance_id,
    )
    return (
        "env",
        "-i",
        *env_pairs,
        "bash",
        "-lc",
        (
            'cd "$1" || exit; shift; runtime_dir="$1"; shift; mkdir -p "$runtime_dir/procs" || exit; '
            'pidfile="$runtime_dir/procs/exec-$$.pid"; setsid "$@" & child=$!; '
            'echo "$child" > "$pidfile"; wait "$child"; status=$?; rm -f "$pidfile"; exit "$status"'
        ),
        "rumi-exec",
        cwd_path,
        runtime_dir,
        *argv,
    )


def _exec_env_pairs(
    env: Mapping[str, str],
    *,
    workspace_dir: str,
    sandbox_id: str,
    provider_instance_id: str,
) -> tuple[str, ...]:
    reserved = sorted(str(key) for key in env if str(key) in RESERVED_EXEC_ENV_KEYS)
    if reserved:
        raise SandboxContractError(
            "INVALID_EXEC_REQUEST",
            "Sandbox exec env cannot override reserved runtime variables.",
            status_code=400,
            details={"reserved_env": reserved},
        )
    base = {
        "HOME": f"{_runtime_dir(provider_instance_id)}/home",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "RUMI_SANDBOX_ID": sandbox_id,
        "RUMI_SANDBOX_INSTANCE": provider_instance_id,
        "RUMI_SANDBOX_WORKSPACE": workspace_dir,
    }
    merged = {**base, **{str(key): str(value) for key, value in env.items()}}
    return tuple(f"{key}={merged[key]}" for key in sorted(merged))


def _network_disabled_argv(argv: Sequence[str]) -> tuple[str, ...]:
    return ("unshare", "--user", "--map-root-user", "--net", "--", *argv)


def _resource_limited_argv(
    argv: Sequence[str],
    *,
    memory_mb: int | None,
    cpu_count: float | None,
    pids: int | None,
) -> tuple[str, ...]:
    memory_kb = int(memory_mb * 1024) if memory_mb and memory_mb > 0 else 0
    pids_limit = int(pids) if pids and pids > 0 else 0
    cpu_affinity = _cpu_affinity_list(cpu_count)
    if memory_kb <= 0 and pids_limit <= 0 and not cpu_affinity:
        return tuple(argv)
    script = (
        "set -e\n"
        'if [ "$1" != \'0\' ]; then ulimit -v "$1"; fi\n'
        'if [ "$2" != \'0\' ]; then ulimit -u "$2"; fi\n'
        'RUMI_CPUSET="$3"\n'
        "shift 3\n"
        'if [ -n "$RUMI_CPUSET" ]; then\n'
        "  command -v taskset >/dev/null 2>&1 || { echo 'taskset is required for sandbox CPU limits' >&2; exit 126; }\n"
        '  exec taskset -c "$RUMI_CPUSET" "$@"\n'
        "fi\n"
        'exec "$@"\n'
    )
    return (
        "bash",
        "-lc",
        script,
        "rumi-resource-limit",
        str(memory_kb),
        str(pids_limit),
        cpu_affinity,
        *argv,
    )


def _cpu_affinity_list(cpu_count: float | None) -> str:
    if cpu_count is None or cpu_count <= 0:
        return ""
    cores = max(1, int(cpu_count))
    return "0" if cores == 1 else f"0-{cores - 1}"


def _instance_network_disabled(instance: ProviderInstance) -> bool:
    value = instance.opaque_state.get("network_disabled")
    if isinstance(value, bool):
        return value
    policy = instance.opaque_state.get("network_policy")
    return _guest_network_disabled(policy if isinstance(policy, Mapping) else {})


def _guest_network_disabled(policy: object) -> bool:
    open_modes = {
        "open",
        "on",
        "allow",
        "allowed",
        "bridge",
        "host_shared",
        "shared",
        "internet",
        "full",
    }
    if isinstance(policy, Mapping):
        mode = str(policy.get("mode") or "off").strip().lower()
        approval_required = bool(policy.get("approval_required") or policy.get("requires_approval"))
        allowlist = policy.get("allowlist") or ()
    else:
        mode = str(getattr(policy, "mode", "off") or "off").strip().lower()
        approval_required = bool(getattr(policy, "approval_required", False))
        allowlist = getattr(policy, "allowlist", ()) or ()
    if approval_required or tuple(allowlist):
        return True
    return mode not in open_modes


def _container_path(workspace_dir: str, path: str) -> str:
    if path == ".":
        return PurePosixPath(workspace_dir).as_posix()
    return (PurePosixPath(workspace_dir) / path).as_posix()


def _container_parent(workspace_dir: str, path: str) -> str | None:
    parent = PurePosixPath(_container_path(workspace_dir, path)).parent
    return (
        None if parent.as_posix() == PurePosixPath(workspace_dir).as_posix() else parent.as_posix()
    )


def _file_patch_operations(payload: Mapping[str, object]) -> list[dict[str, object]]:
    raw_items = payload.get("files")
    if raw_items is None:
        raw_items = payload.get("patch")
    if raw_items is None:
        raw_items = [payload]
    if not isinstance(raw_items, list) or not raw_items:
        raise SandboxContractError(
            "INVALID_SANDBOX_FILE_PATCH",
            "Sandbox file patch requires at least one file operation.",
            status_code=400,
        )

    operations: list[dict[str, object]] = []
    total_bytes = 0
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise SandboxContractError(
                "INVALID_SANDBOX_FILE_PATCH",
                "Sandbox file patch operations must be objects.",
                status_code=400,
            )
        path = validate_workspace_relative_path(raw.get("path"), field="path")
        op = str(raw.get("op") or raw.get("operation") or "write").strip().lower()
        if op not in {"write", "replace", "create", "upsert"}:
            raise SandboxContractError(
                "INVALID_SANDBOX_FILE_PATCH",
                "Sandbox file patch only supports write-style operations.",
                status_code=400,
            )
        content = _patch_content(raw)
        total_bytes += len(content)
        if total_bytes > MAX_FILE_PATCH_BYTES:
            raise SandboxContractError(
                "SANDBOX_FILE_PATCH_TOO_LARGE",
                "Sandbox file patch payload is too large.",
                status_code=413,
            )
        operations.append({"path": path, "content": content})
    return operations


def _patch_content(raw: Mapping[str, object]) -> bytes:
    if "content_base64" in raw:
        value = raw.get("content_base64")
        if not isinstance(value, str):
            raise SandboxContractError(
                "INVALID_SANDBOX_FILE_PATCH", "content_base64 must be a string.", status_code=400
            )
        try:
            return base64.b64decode(value, validate=True)
        except Exception as exc:
            raise SandboxContractError(
                "INVALID_SANDBOX_FILE_PATCH", "content_base64 is invalid.", status_code=400
            ) from exc
    value = raw.get("content")
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise SandboxContractError(
        "INVALID_SANDBOX_FILE_PATCH",
        "Sandbox file patch requires content or content_base64.",
        status_code=400,
    )


def _port_number(value: object) -> int:
    if isinstance(value, bool):
        raise SandboxContractError(
            "INVALID_SANDBOX_PORT", "Sandbox port must be an integer.", status_code=400
        )
    try:
        port = int(_numeric_value(value) or 0)
    except (TypeError, ValueError) as exc:
        raise SandboxContractError(
            "INVALID_SANDBOX_PORT", "Sandbox port must be an integer.", status_code=400
        ) from exc
    if port < 1 or port > 65535:
        raise SandboxContractError(
            "INVALID_SANDBOX_PORT", "Sandbox port must be between 1 and 65535.", status_code=400
        )
    return port


def _port_probe_argv(port: int) -> tuple[str, ...]:
    script = (
        "import socket, sys\n"
        "port = int(sys.argv[1])\n"
        "with socket.create_connection(('127.0.0.1', port), timeout=1.0):\n"
        "    pass\n"
    )
    return ("python3", "-c", script, str(int(port)))


def _positive_int(value: object, fallback: int) -> int:
    try:
        parsed = int(_numeric_value(value) or 0)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _optional_positive_int(value: object) -> int | None:
    try:
        parsed = int(_numeric_value(value) or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_positive_float(value: object) -> float | None:
    try:
        parsed = float(_numeric_value(value) or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _numeric_value(value: object) -> int | float | str:
    return value if isinstance(value, (int, float, str)) else 0


def _bounded_output(value: str, max_bytes: int | None) -> tuple[str, bool]:
    if not max_bytes or max_bytes <= 0:
        return value, False
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value, False
    return encoded[:max_bytes].decode("utf-8", errors="replace"), True


def _button(value: object) -> str:
    return {"left": "1", "middle": "2", "right": "3"}.get(str(value or "left").lower(), "1")


def _guest_error(
    sandbox_id: str,
    code: str,
    message: str,
    result: GuestCommandResult,
    *,
    status_code: int = 502,
) -> dict[str, object]:
    return {
        "ok": False,
        "sandbox_id": sandbox_id,
        "code": code,
        "error": message,
        "status_code": status_code,
        "details": {"stderr": result.stderr.strip()[:1000]},
    }
