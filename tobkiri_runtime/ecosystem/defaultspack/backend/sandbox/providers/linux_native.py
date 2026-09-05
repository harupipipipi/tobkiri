from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from typing import Any, Callable, Mapping, Sequence

from ..cancellation import run_cancellable_subprocess
from ..errors import INVALID_EXEC_REQUEST, RUNTIME_PROVIDER_UNAVAILABLE, SandboxContractError
from ..guest.protocol import DesktopInputRequest
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
from .base import ProgressSink


DESKTOP_CAPABILITIES = frozenset({"sandbox.desktop", "sandbox.desktop_input", "sandbox.snapshot"})
LINUX_NATIVE_COMMAND_PACKAGES = {
    "Xvfb": "xvfb",
    "openbox": "openbox",
    "xdotool": "xdotool",
    "import": "imagemagick",
}
LINUX_NATIVE_APT_PACKAGES = ("xvfb", "openbox", "xdotool", "imagemagick", "x11-utils", "ca-certificates", "xterm")
LINUX_NATIVE_BROWSER_CANDIDATES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "firefox", "xdg-open")


@dataclass(frozen=True)
class LinuxCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


LinuxRunner = Callable[[Sequence[str], str | None, float | None], LinuxCommandResult]


class LinuxNativeProvider:
    """Provider for a Rumi-owned Linux Xvfb/Openbox desktop seat.

    This provider deliberately does not advertise sandbox.exec/files until a
    real Linux isolation layer is wired. The desktop path runs only the owned
    X11 helper and never falls back to arbitrary host command execution.
    """

    provider_id = "linux_native"

    def __init__(
        self,
        *,
        session_factory: Any | None = None,
        runner: LinuxRunner | None = None,
        apt_get_path: str | None = None,
        sudo_path: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runner = runner or _subprocess_runner
        self._configured_apt_get_path = str(apt_get_path).strip() if apt_get_path else None
        self._configured_sudo_path = str(sudo_path).strip() if sudo_path else None
        self._instances: dict[str, ProviderInstance] = {}
        self._sessions: dict[str, Any] = {}

    def doctor(self, request: RuntimeRequirements) -> RuntimeProviderStatus:
        platform_available = sys.platform.startswith("linux")
        capabilities = DESKTOP_CAPABILITIES if platform_available else frozenset()
        missing: list[str] = []
        diagnostics: list[Diagnostic] = []
        installed = False

        if not platform_available:
            missing.append("linux_platform")
            diagnostics.append(
                Diagnostic(
                    code="LINUX_NATIVE_PLATFORM_UNAVAILABLE",
                    message="Linux native desktops require a Linux host or guest runtime.",
                    severity="warning",
                )
            )
        else:
            try:
                session = self._new_session()
            except SandboxContractError as exc:
                missing.append("v4_desktop_host_session")
                diagnostics.append(
                    Diagnostic(
                        code="LINUX_NATIVE_V4_SESSION_UNAVAILABLE",
                        message=str(exc),
                        severity="warning",
                    )
                )
            else:
                missing_commands = list(session.missing_commands())
                installed = not missing_commands
                if missing_commands:
                    missing.extend(f"command:{name}" for name in missing_commands)
                    diagnostics.append(
                        Diagnostic(
                            code="LINUX_NATIVE_COMMANDS_MISSING",
                            message="Linux native desktop helper commands are not available in the runtime.",
                            severity="warning",
                            details={"missing_commands": missing_commands},
                        )
                    )

        missing_capabilities = sorted(request.required_capabilities - capabilities)
        missing.extend(missing_capabilities)
        if missing_capabilities:
            diagnostics.append(
                Diagnostic(
                    code="LINUX_NATIVE_CAPABILITIES_UNSUPPORTED",
                    message="Linux native desktop runtime does not provide every requested sandbox capability.",
                    severity="warning",
                    details={"missing_capabilities": missing_capabilities},
                )
            )
        ready = platform_available and installed and not missing_capabilities
        return RuntimeProviderStatus(
            provider_id=self.provider_id,
            platform="linux",
            available=platform_available and installed,
            installed=installed,
            ready=ready,
            version=None,
            capabilities=capabilities,
            missing_requirements=tuple(missing),
            requires_user_action=bool(missing),
            user_action=None
            if ready
            else _linux_native_user_action(
                available=platform_available,
                missing_capabilities=missing_capabilities,
            ),
            reboot_required=False,
            diagnostics=tuple(diagnostics),
        )

    def ensure(self, request: EnsureRuntimeRequest, progress: ProgressSink) -> OperationResult:
        operation_id = "linux-native-ensure"
        progress.emit(ProgressEvent(operation_id=operation_id, stage="doctor", message="Checking Linux native desktop runtime"))
        status = self.doctor(request.requirements)
        if status.ready:
            progress.emit(ProgressEvent(operation_id=operation_id, stage="ready", message="Linux native desktop runtime is ready", percent=100))
            return OperationResult(ok=True, provider_id=self.provider_id, operation_id=operation_id, status="completed")
        unsupported_capabilities = sorted(request.requirements.required_capabilities - DESKTOP_CAPABILITIES)
        if unsupported_capabilities or not sys.platform.startswith("linux"):
            return _failed_from_status(self.provider_id, operation_id, status)

        install_result = self._install_desktop_packages(progress, operation_id=operation_id, update=False)
        if install_result is not None:
            return install_result

        status = self.doctor(request.requirements)
        if status.ready:
            progress.emit(ProgressEvent(operation_id=operation_id, stage="ready", message="Linux native desktop runtime is ready", percent=100))
            return OperationResult(ok=True, provider_id=self.provider_id, operation_id=operation_id, status="completed")
        return OperationResult(
            ok=False,
            provider_id=self.provider_id,
            operation_id=operation_id,
            status="failed",
            diagnostics=status.diagnostics,
            requires_user_action=status.requires_user_action,
            user_action=status.user_action,
            reboot_required=status.reboot_required,
        )

    def update(self, request: UpdateRuntimeRequest, progress: ProgressSink) -> OperationResult:
        del request
        operation_id = "linux-native-update"
        status = self.doctor(RuntimeRequirements(provider_id=self.provider_id))
        if not sys.platform.startswith("linux"):
            return _failed_from_status(self.provider_id, operation_id, status)
        progress.emit(ProgressEvent(operation_id=operation_id, stage="packages", message="Updating Linux native desktop helper packages", percent=20))
        install_result = self._install_desktop_packages(progress, operation_id=operation_id, update=True)
        if install_result is not None:
            return install_result
        status = self.doctor(RuntimeRequirements(provider_id=self.provider_id))
        if status.installed:
            progress.emit(ProgressEvent(operation_id=operation_id, stage="ready", message="Linux native desktop helper packages are ready", percent=100))
            return OperationResult(ok=True, provider_id=self.provider_id, operation_id=operation_id, status="completed")
        return _failed_from_status(self.provider_id, operation_id, status)

    def uninstall(self, request: UninstallRuntimeRequest, progress: ProgressSink) -> OperationResult:
        del request
        for instance in list(self._instances.values()):
            self.destroy(instance)
        progress.emit(ProgressEvent(operation_id="linux-native-uninstall", stage="stopped", message="Stopped Linux native desktop sessions", percent=100))
        return OperationResult(ok=True, provider_id=self.provider_id, operation_id="linux-native-uninstall", status="completed")

    def create(self, spec: SandboxCreateSpec) -> ProviderInstance:
        if spec.template.desktop is None or not spec.template.desktop.enabled:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Linux native provider only supports desktop templates in this build.",
                status_code=503,
            )
        sandbox_id = str(uuid.uuid4())
        session = self._new_session(width=spec.template.desktop.width, height=spec.template.desktop.height)
        instance = ProviderInstance(
            provider_id=self.provider_id,
            provider_instance_id=f"linux-native-{sandbox_id}",
            sandbox_id=sandbox_id,
            runtime_id="linux-native-x11",
            state="stopped",
            opaque_state={
                "template_id": spec.template.template_id,
                "width": spec.template.desktop.width,
                "height": spec.template.desktop.height,
                "workspace_binding": model_to_dict(spec.workspace_binding),
                "network_policy": model_to_dict(spec.template.network),
                "resource_limits": model_to_dict(spec.template.resources),
                "desktop_provisioning": spec.metadata.get("desktop_provisioning") or {},
                "desktop_rules": spec.metadata.get("desktop_rules") or {},
                "assigned_agent_id": spec.metadata.get("assigned_agent_id"),
                "startup": spec.metadata.get("startup") or {},
            },
        )
        self._instances[instance.provider_instance_id] = instance
        self._sessions[instance.provider_instance_id] = session
        return instance

    def start(self, instance: ProviderInstance) -> ProviderInstance:
        session = self._sessions.get(instance.provider_instance_id)
        if session is None:
            session = self._new_session(
                width=_positive_int(instance.opaque_state.get("width"), 1440),
                height=_positive_int(instance.opaque_state.get("height"), 900),
            )
            self._sessions[instance.provider_instance_id] = session
        status = session.start()
        if not status.get("running"):
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                str(status.get("reason") or "Linux native desktop session did not start."),
                status_code=503,
                details={"status": status},
            )
        startup_status = self._apply_startup(session, instance)
        opaque_state = {**dict(instance.opaque_state), "display": session.display}
        if startup_status:
            opaque_state["startup_status"] = startup_status
        else:
            opaque_state.pop("startup_status", None)
        session_metadata = _session_owned_metadata(session)
        if session_metadata:
            opaque_state["x11_session"] = session_metadata
        started = ProviderInstance(
            provider_id=instance.provider_id,
            provider_instance_id=instance.provider_instance_id,
            sandbox_id=instance.sandbox_id,
            runtime_id=instance.runtime_id,
            state="ready",
            opaque_state=opaque_state,
            generation=instance.generation + 1,
        )
        self._instances[started.provider_instance_id] = started
        return started

    def stop(self, instance: ProviderInstance, *, force: bool = False) -> None:
        del force
        session = self._sessions.get(instance.provider_instance_id)
        if session is not None:
            session.stop()
        else:
            _cleanup_persisted_x11_session(instance.opaque_state)
        self._instances[instance.provider_instance_id] = ProviderInstance(
            provider_id=instance.provider_id,
            provider_instance_id=instance.provider_instance_id,
            sandbox_id=instance.sandbox_id,
            runtime_id=instance.runtime_id,
            state="stopped",
            opaque_state=_without_x11_runtime_state(instance.opaque_state),
            generation=instance.generation + 1,
        )

    def destroy(self, instance: ProviderInstance) -> None:
        session = self._sessions.pop(instance.provider_instance_id, None)
        if session is not None:
            session.stop()
        else:
            _cleanup_persisted_x11_session(instance.opaque_state)
        self._instances.pop(instance.provider_instance_id, None)

    def reconcile(self, persisted: ProviderInstance) -> ReconcileResult:
        current = self._instances.get(persisted.provider_instance_id)
        if current is None:
            if persisted.state != "stopped":
                _cleanup_persisted_x11_session(persisted.opaque_state)
            current = ProviderInstance(
                provider_id=persisted.provider_id,
                provider_instance_id=persisted.provider_instance_id,
                sandbox_id=persisted.sandbox_id,
                runtime_id=persisted.runtime_id,
                state="stopped",
                opaque_state=_without_x11_runtime_state(persisted.opaque_state),
                generation=persisted.generation + (0 if persisted.state == "stopped" else 1),
            )
        return ReconcileResult(instance=current, changed=current != persisted)

    def connect_agent(self, instance: ProviderInstance) -> "LinuxNativeGuestAgent":
        return LinuxNativeGuestAgent(self._require_session(instance))

    def _require_session(self, instance: ProviderInstance) -> Any:
        session = self._sessions.get(instance.provider_instance_id)
        if session is None:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Linux native desktop session is not available in this process.",
                status_code=503,
            )
        return session

    def _new_session(self, *, width: int | None = None, height: int | None = None) -> Any:
        if self._session_factory is not None:
            return self._session_factory(width=width, height=height)
        del width, height
        raise SandboxContractError(
            RUNTIME_PROVIDER_UNAVAILABLE,
            "Linux native desktop requires an injected v4 desktop host session.",
            status_code=503,
        )

    def _apply_startup(self, session: Any, instance: ProviderInstance) -> dict[str, Any]:
        startup = instance.opaque_state.get("startup") if isinstance(instance.opaque_state, Mapping) else {}
        if not isinstance(startup, Mapping):
            return {}
        starter = str(startup.get("starter") or "empty").strip().lower()
        if starter in {"", "empty"}:
            return {"starter": "empty", "skipped": True, "reason": "No desktop starter was requested."}
        if starter == "terminal":
            terminal = shutil.which("xterm")
            if not terminal:
                return {"starter": starter, "skipped": True, "reason": "xterm is not installed in the Linux native runtime."}
            return self._launch_session(session, "terminal", [terminal, "-title", "Rumi Desktop"], stdout_name="starter-terminal.log")
        if starter in {"browser", "browser_url"}:
            browser_url = str(startup.get("browser_url") or "").strip()
            if starter == "browser_url" and not browser_url:
                return {"starter": starter, "skipped": True, "reason": "No browser_url was provided."}
            if browser_url and not _linux_native_network_allows_startup(instance.opaque_state):
                return {"starter": starter, "skipped": True, "reason": "Network policy requires approval or is disabled for browser startup."}
            candidates = LINUX_NATIVE_BROWSER_CANDIDATES if browser_url else tuple(name for name in LINUX_NATIVE_BROWSER_CANDIDATES if name != "xdg-open")
            browser = _first_available_command(candidates)
            if browser is None:
                return {"starter": starter, "skipped": True, "reason": "No supported browser command was found."}
            command_name, browser_path = browser
            return self._launch_session(
                session,
                starter,
                _browser_launch_args(command_name, browser_path, browser_url, session),
                stdout_name="starter-browser.log",
            )
        return {"starter": starter, "skipped": True, "reason": f"Unsupported Linux native desktop starter: {starter}"}

    @staticmethod
    def _launch_session(session: Any, name: str, args: list[str], *, stdout_name: str) -> dict[str, Any]:
        launch = getattr(session, "launch", None)
        if not callable(launch):
            return {
                "starter": name,
                "skipped": True,
                "reason": "Linux native session does not support background starter launch.",
            }
        result = launch(name, args, stdout_name=stdout_name)
        return {
            "starter": name,
            "executed": bool(result.get("executed")),
            "skipped": not bool(result.get("executed")),
            "reason": result.get("reason") or result.get("stderr") or "",
            "command": result.get("command") or args,
            "pid": result.get("pid"),
            "process": result.get("process"),
            "log_path": result.get("log_path"),
        }

    def _install_desktop_packages(self, progress: ProgressSink, *, operation_id: str, update: bool) -> OperationResult | None:
        apt_get = self._apt_get_path()
        if apt_get is None:
            return _failed_operation(
                self.provider_id,
                operation_id,
                Diagnostic(
                    code="LINUX_NATIVE_PACKAGE_MANAGER_MISSING",
                    message="apt-get was not found; Linux native desktop dependencies cannot be installed automatically.",
                    severity="error",
                ),
                user_action="Install xvfb, openbox, xdotool, and ImageMagick manually or use a managed Ubuntu provider.",
            )

        prefix_result = self._apt_prefix(apt_get, operation_id=operation_id)
        if isinstance(prefix_result, OperationResult):
            return prefix_result
        update_command, install_command = prefix_result

        progress.emit(
            ProgressEvent(
                operation_id=operation_id,
                stage="apt_update",
                message="Refreshing Linux package indexes",
                percent=35 if not update else 30,
            )
        )
        updated = self._run(update_command, timeout=300)
        if updated.returncode != 0:
            return _command_failed(self.provider_id, operation_id, "LINUX_NATIVE_APT_UPDATE_FAILED", "Linux native package index refresh failed.", updated, update_command)

        package_details = {
            "packages": list(LINUX_NATIVE_APT_PACKAGES),
            "command_packages": dict(LINUX_NATIVE_COMMAND_PACKAGES),
        }
        progress.emit(
            ProgressEvent(
                operation_id=operation_id,
                stage="apt_install",
                message="Installing Linux native desktop helper packages",
                percent=70,
                details=package_details,
            )
        )
        installed = self._run(install_command, timeout=600)
        if installed.returncode != 0:
            return _command_failed(
                self.provider_id,
                operation_id,
                "LINUX_NATIVE_PACKAGE_INSTALL_FAILED",
                "Linux native desktop helper package installation failed.",
                installed,
                install_command,
            )
        return None

    def _apt_prefix(self, apt_get: str, *, operation_id: str) -> tuple[list[str], list[str]] | OperationResult:
        if _is_root():
            return [apt_get, "update"], ["env", "DEBIAN_FRONTEND=noninteractive", apt_get, "install", "-y", *LINUX_NATIVE_APT_PACKAGES]
        sudo = self._sudo_path()
        if sudo is None:
            return _failed_operation(
                self.provider_id,
                operation_id,
                Diagnostic(
                    code="LINUX_NATIVE_SUDO_MISSING",
                    message="sudo was not found; package installation requires root privileges.",
                    severity="error",
                ),
                user_action="Run the setup from a root-capable Linux environment or install the desktop helper packages manually.",
            )
        return [sudo, "-n", apt_get, "update"], [sudo, "-n", "env", "DEBIAN_FRONTEND=noninteractive", apt_get, "install", "-y", *LINUX_NATIVE_APT_PACKAGES]

    def _apt_get_path(self) -> str | None:
        return self._configured_apt_get_path or shutil.which("apt-get")

    def _sudo_path(self) -> str | None:
        return self._configured_sudo_path or shutil.which("sudo")

    def _run(self, command: Sequence[str], *, input_text: str | None = None, timeout: float | None = None) -> LinuxCommandResult:
        try:
            return self._runner(tuple(str(part) for part in command), input_text, timeout)
        except TimeoutError as exc:
            return LinuxCommandResult(returncode=124, stderr=str(exc))
        except OSError as exc:
            return LinuxCommandResult(returncode=127, stderr=str(exc))


class LinuxNativeGuestAgent:
    def __init__(self, session: Any) -> None:
        self._session = session

    def exec(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        del sandbox_id, payload
        raise SandboxContractError(
            INVALID_EXEC_REQUEST,
            "Linux native desktop provider does not expose sandbox exec in this build.",
            status_code=501,
        )

    def capture_frame(self, sandbox_id: str, seat_id: str) -> dict[str, object]:
        screenshot = self._session.screenshot()
        screenshot_path = str(screenshot.get("path") or "")
        data_url = str(screenshot.get("data_url") or "")
        response: dict[str, object] | None = None
        try:
            if not data_url.startswith("data:image/png;base64,"):
                return {
                    "ok": False,
                    "sandbox_id": sandbox_id,
                    "seat_id": seat_id,
                    "error": str(screenshot.get("reason") or screenshot.get("error") or "Desktop frame capture failed."),
                }
            data = base64.b64decode(data_url.split(",", 1)[1])
            response = {
                "ok": True,
                "sandbox_id": sandbox_id,
                "seat_id": seat_id,
                "content_type": "image/png",
                "data": data,
                "width": int(getattr(self._session.config, "width", 0) or 0),
                "height": int(getattr(self._session.config, "height", 0) or 0),
                "source": "linux_native_x11",
                "metadata": {
                    "display": getattr(self._session, "display", None),
                    "path": screenshot_path or None,
                    "path_deleted": False,
                },
            }
            return response
        finally:
            path_deleted = _unlink_capture_file(screenshot_path)
            metadata = response.get("metadata") if response is not None else None
            if isinstance(metadata, dict):
                metadata["path_deleted"] = path_deleted

    def desktop_input(
        self,
        sandbox_id: str,
        seat_id: str,
        payload: Mapping[str, object],
        *,
        actor: str = "human",
    ) -> dict[str, object]:
        request = DesktopInputRequest.from_payload(
            payload,
            width=int(getattr(self._session.config, "width", 0) or 0),
            height=int(getattr(self._session.config, "height", 0) or 0),
            require_lease=False,
        )
        result = self._dispatch_input(request)
        return {
            "ok": bool(result.get("executed")),
            "sandbox_id": sandbox_id,
            "seat_id": seat_id,
            "action": request.action,
            "actor": actor,
            "error": result.get("reason") or result.get("error") or result.get("stderr"),
        }

    def _dispatch_input(self, request: DesktopInputRequest) -> dict[str, Any]:
        if request.action == "move":
            return self._session.move(int(request.x or 0), int(request.y or 0))
        if request.action == "click":
            return self._session.click(int(request.x or 0), int(request.y or 0), button=str(request.button or "left"))
        if request.action == "double_click":
            return self._session.double_click(int(request.x or 0), int(request.y or 0), button=str(request.button or "left"))
        if request.action == "drag":
            return self._session.drag(int(request.x or 0), int(request.y or 0), int(request.to_x or 0), int(request.to_y or 0), button=str(request.button or "left"))
        if request.action == "scroll":
            direction = "down" if int(request.delta_y or 0) >= 0 else "up"
            clicks = max(1, abs(int(request.delta_y or request.delta_x or 1)))
            return self._session.scroll(int(request.x or 0), int(request.y or 0), direction=direction, clicks=clicks)
        if request.action == "type_text":
            return self._session.type(str(request.text or ""))
        if request.action == "key":
            return self._session.keypress(str(request.key or ""))
        return {"executed": False, "reason": "Unsupported desktop input action."}


def _unlink_capture_file(path: str) -> bool:
    if not path:
        return False
    try:
        capture_path = Path(path)
        existed = capture_path.exists()
        capture_path.unlink(missing_ok=True)
        return existed and not capture_path.exists()
    except OSError:
        return False


def _session_owned_metadata(session: Any) -> dict[str, Any]:
    metadata = getattr(session, "owned_session_metadata", None)
    if not callable(metadata):
        return {}
    try:
        value = metadata()
    except Exception:
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _cleanup_persisted_x11_session(opaque_state: Mapping[str, Any]) -> dict[str, Any]:
    metadata = opaque_state.get("x11_session") if isinstance(opaque_state, Mapping) else None
    if not isinstance(metadata, Mapping):
        return {"cleaned": False}
    return _cleanup_owned_x11_session(metadata)


def _cleanup_owned_x11_session(metadata: Mapping[str, Any]) -> dict[str, Any]:
    del metadata
    return {
        "cleaned": False,
        "reason": "Linux native cleanup requires the owning v4 desktop host session.",
    }


def _without_x11_runtime_state(opaque_state: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(opaque_state)
    cleaned.pop("display", None)
    cleaned.pop("x11_session", None)
    cleaned.pop("startup_status", None)
    return cleaned


def _failed_from_status(provider_id: str, operation_id: str, status: RuntimeProviderStatus) -> OperationResult:
    return OperationResult(
        ok=False,
        provider_id=provider_id,
        operation_id=operation_id,
        status="failed",
        diagnostics=status.diagnostics,
        requires_user_action=status.requires_user_action,
        user_action=status.user_action,
        reboot_required=status.reboot_required,
    )


def _failed_operation(provider_id: str, operation_id: str, diagnostic: Diagnostic, *, user_action: str | None = None) -> OperationResult:
    return OperationResult(
        ok=False,
        provider_id=provider_id,
        operation_id=operation_id,
        status="failed",
        diagnostics=(diagnostic,),
        requires_user_action=True,
        user_action=user_action or diagnostic.message,
    )


def _command_failed(
    provider_id: str,
    operation_id: str,
    code: str,
    message: str,
    result: LinuxCommandResult,
    command: Sequence[str],
) -> OperationResult:
    return _failed_operation(
        provider_id,
        operation_id,
        Diagnostic(
            code=code,
            message=message,
            severity="error",
            details={
                "argv": list(command[:4]),
                "exit_code": result.returncode,
                "stdout": result.stdout.strip()[:1000],
                "stderr": result.stderr.strip()[:1000],
            },
        ),
    )


def _linux_native_user_action(*, available: bool, missing_capabilities: Sequence[str]) -> str:
    if not available:
        return "Run Linux native desktops on a Linux host or select a managed Ubuntu provider."
    if missing_capabilities:
        return "Select the desktop.linux_native template for the linux_native provider, or use Lima/WSL/Docker for sandbox exec/files."
    return "Open the managed runtime setup flow to install the Linux desktop helper packages."


def _first_available_command(candidates: Sequence[str]) -> tuple[str, str] | None:
    for command_name in candidates:
        command_path = shutil.which(command_name)
        if command_path:
            return command_name, command_path
    return None


def _browser_launch_args(command_name: str, browser_path: str, browser_url: str, session: Any) -> list[str]:
    profile_dir = _browser_profile_dir(session)
    if command_name == "xdg-open":
        return [browser_path, browser_url] if browser_url else [browser_path]
    if command_name == "firefox":
        args = [browser_path, "--no-remote", "--new-instance"]
        if profile_dir is not None:
            args.extend(["--profile", str(profile_dir)])
        if browser_url:
            args.append(browser_url)
        return args
    args = [browser_path, "--no-first-run", "--disable-dev-shm-usage", "--new-window"]
    if profile_dir is not None:
        args.append(f"--user-data-dir={profile_dir}")
    if browser_url:
        args.append(browser_url)
    return args


def _browser_profile_dir(session: Any) -> Path | None:
    session_dir = getattr(session, "session_dir", None)
    if not session_dir:
        return None
    profile_dir = Path(session_dir) / "browser-profile"
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return profile_dir


def _linux_native_network_allows_startup(opaque_state: Mapping[str, Any]) -> bool:
    policy = opaque_state.get("network_policy") if isinstance(opaque_state, Mapping) else {}
    if not isinstance(policy, Mapping):
        return False
    mode = str(policy.get("mode") or "off").strip().lower()
    if mode in {"", "off", "none", "deny", "denied", "disabled"}:
        return False
    return not bool(policy.get("approval_required"))


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    try:
        return int(geteuid()) == 0
    except OSError:
        return False


def _subprocess_runner(command: Sequence[str], input_text: str | None, timeout: float | None) -> LinuxCommandResult:
    try:
        completed = run_cancellable_subprocess(
            [str(part) for part in command],
            input_text=input_text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        return LinuxCommandResult(returncode=124, stdout=stdout, stderr=stderr or str(exc))
    return LinuxCommandResult(returncode=int(completed.returncode), stdout=completed.stdout or "", stderr=completed.stderr or "")


def _positive_int(value: object, fallback: int) -> int:
    try:
        parsed = int(_numeric_value(value) or 0)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _numeric_value(value: object) -> int | float | str:
    return value if isinstance(value, (int, float, str)) else 0
