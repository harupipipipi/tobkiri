from __future__ import annotations

import base64
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Mapping, Sequence

from ..cancellation import run_cancellable_subprocess
from ..errors import RUNTIME_PROVIDER_UNAVAILABLE, SandboxContractError
from ..guest.protocol import GuestExecRequest
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
from .base import ProgressSink


DOCKER_CAPABILITIES = frozenset(
    {
        "sandbox.exec",
        "sandbox.files",
        "sandbox.overlay_workspace",
        "sandbox.port_forward",
        "sandbox.network_policy",
        "sandbox.resource_limits",
        "sandbox.container",
    }
)
DEFAULT_DOCKER_IMAGE = "ubuntu:22.04"
CODING_PYTHON_IMAGE = "python:3.11-slim"
CODING_NODE_IMAGE = "node:20-bookworm-slim"
CONTAINER_WORKDIR = "/workspace"
MAX_FILE_PATCH_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class DockerCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


DockerRunner = Callable[[Sequence[str], str | None, float | None], DockerCommandResult]
PortForwarderFactory = Callable[[str, str, int], "DockerPortForwarder"]


class DockerProvider:
    """Optional container runtime provider for non-desktop sandbox execution."""

    provider_id = "docker"

    def __init__(
        self,
        *,
        docker_path: str | None = None,
        runner: DockerRunner | None = None,
        port_forwarder_factory: PortForwarderFactory | None = None,
    ) -> None:
        self._configured_docker_path = docker_path
        self._runner = runner or _subprocess_runner
        self._instances: dict[str, ProviderInstance] = {}
        self._port_forwarder_factory = port_forwarder_factory or DockerPortForwarder
        self._port_forwarders: dict[tuple[str, int], DockerPortForwarder] = {}

    def doctor(self, request: RuntimeRequirements) -> RuntimeProviderStatus:
        docker_path = self._docker_path()
        diagnostics: list[Diagnostic] = []
        missing: list[str] = []
        version: str | None = None
        installed = False

        if docker_path is None:
            missing.append("command:docker")
            diagnostics.append(
                Diagnostic(
                    code="DOCKER_COMMAND_MISSING",
                    message="Docker CLI was not found on PATH.",
                    severity="info",
                )
            )
        else:
            result = self._run([docker_path, "info", "--format", "{{.ServerVersion}}"], timeout=5)
            installed = result.returncode == 0
            version = result.stdout.strip() or None
            if not installed:
                missing.append("docker_daemon")
                diagnostics.append(
                    Diagnostic(
                        code="DOCKER_DAEMON_UNAVAILABLE",
                        message="Docker CLI is present, but the Docker daemon is not reachable.",
                        severity="warning",
                        details={"stderr": result.stderr.strip()[:500]},
                    )
                )

        missing_capabilities = sorted(request.required_capabilities - DOCKER_CAPABILITIES)
        missing.extend(missing_capabilities)
        ready = docker_path is not None and installed and not missing_capabilities
        return RuntimeProviderStatus(
            provider_id=self.provider_id,
            platform=platform.system().lower() or "unknown",
            available=docker_path is not None,
            installed=installed,
            ready=ready,
            version=version,
            capabilities=DOCKER_CAPABILITIES if docker_path is not None else frozenset(),
            missing_requirements=tuple(missing),
            requires_user_action=not ready,
            user_action=None if ready else "Start Docker or choose a managed platform provider.",
            diagnostics=tuple(diagnostics),
        )

    def ensure(self, request: EnsureRuntimeRequest, progress: ProgressSink) -> OperationResult:
        progress.emit(ProgressEvent(operation_id="docker-ensure", stage="doctor", message="Checking Docker runtime"))
        status = self.doctor(request.requirements)
        if status.ready:
            progress.emit(ProgressEvent(operation_id="docker-ensure", stage="ready", message="Docker runtime is ready", percent=100))
            return OperationResult(ok=True, provider_id=self.provider_id, operation_id="docker-ensure", status="completed")
        return OperationResult(
            ok=False,
            provider_id=self.provider_id,
            operation_id="docker-ensure",
            status="failed",
            diagnostics=status.diagnostics,
            requires_user_action=status.requires_user_action,
            user_action=status.user_action,
        )

    def update(self, request: UpdateRuntimeRequest, progress: ProgressSink) -> OperationResult:
        del request
        progress.emit(ProgressEvent(operation_id="docker-update", stage="skipped", message="Docker is managed outside Rumi", percent=100))
        return OperationResult(ok=True, provider_id=self.provider_id, operation_id="docker-update", status="skipped")

    def uninstall(self, request: UninstallRuntimeRequest, progress: ProgressSink) -> OperationResult:
        del request
        for instance in list(self._instances.values()):
            self.destroy(instance)
        progress.emit(ProgressEvent(operation_id="docker-uninstall", stage="stopped", message="Stopped Docker sandbox containers", percent=100))
        return OperationResult(ok=True, provider_id=self.provider_id, operation_id="docker-uninstall", status="completed")

    def create(self, spec: SandboxCreateSpec) -> ProviderInstance:
        if spec.template.desktop is not None and spec.template.desktop.enabled:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Docker provider currently supports non-desktop sandbox templates only.",
                status_code=503,
            )
        docker_path = self._require_docker_ready(spec.template.provider_requirements)
        sandbox_id = str(uuid.uuid4())
        image = _image_for_spec(spec)
        name = _container_name(sandbox_id)
        instance = ProviderInstance(
            provider_id=self.provider_id,
            provider_instance_id=name,
            sandbox_id=sandbox_id,
            runtime_id="docker",
            state="stopped",
            opaque_state={
                "docker_path": docker_path,
                "container_name": name,
                "image": image,
                "network_mode": _docker_network_mode(spec),
                "memory_mb": spec.template.resources.memory_mb,
                "cpu_count": spec.template.resources.cpu_count,
                "pids": spec.template.resources.pids,
                "output_bytes": spec.template.resources.output_bytes,
                "template_id": spec.template.template_id,
                "workspace_binding": model_to_dict(spec.workspace_binding),
                "network_policy": model_to_dict(spec.template.network),
                "resource_limits": model_to_dict(spec.template.resources),
                "desktop_provisioning": spec.metadata.get("desktop_provisioning") or {},
                "desktop_rules": spec.metadata.get("desktop_rules") or {},
                "assigned_agent_id": spec.metadata.get("assigned_agent_id"),
                "startup": spec.metadata.get("startup") or {},
                "requires_port_forward": _requires_port_forward(spec),
            },
        )
        self._instances[instance.provider_instance_id] = instance
        return instance

    def start(self, instance: ProviderInstance) -> ProviderInstance:
        docker_path = str(instance.opaque_state.get("docker_path") or self._require_docker_ready(frozenset({"sandbox.exec"})))
        name = str(instance.opaque_state.get("container_name") or instance.provider_instance_id)
        state = self._inspect_state(docker_path, name)
        if state == "running":
            return self._started(instance)
        if state in {"created", "exited", "paused"}:
            result = self._run([docker_path, "start", name], timeout=30)
            if result.returncode != 0:
                raise _docker_error("DOCKER_START_FAILED", "Docker sandbox container did not start.", result, status_code=503)
            if _requires_port_forward_from_state(instance.opaque_state):
                self._ensure_port_forward_helper(docker_path, name)
            return self._started(instance)

        command = _docker_run_command(docker_path, name, instance.opaque_state)
        result = self._run(command, timeout=120)
        if result.returncode != 0:
            raise _docker_error("DOCKER_START_FAILED", "Docker sandbox container did not start.", result, status_code=503)
        self._seed_overlay_workspace(docker_path, name, instance.opaque_state)
        if _requires_port_forward_from_state(instance.opaque_state):
            self._ensure_port_forward_helper(docker_path, name)
        return self._started(instance)

    def stop(self, instance: ProviderInstance, *, force: bool = False) -> None:
        docker_path = str(instance.opaque_state.get("docker_path") or self._docker_path() or "docker")
        name = str(instance.opaque_state.get("container_name") or instance.provider_instance_id)
        self._stop_port_forwarders(name)
        command = [docker_path, "kill", name] if force else [docker_path, "stop", name]
        self._run(command, timeout=30)
        self._instances[instance.provider_instance_id] = ProviderInstance(
            provider_id=instance.provider_id,
            provider_instance_id=instance.provider_instance_id,
            sandbox_id=instance.sandbox_id,
            runtime_id=instance.runtime_id,
            state="stopped",
            opaque_state=instance.opaque_state,
            generation=instance.generation + 1,
        )

    def destroy(self, instance: ProviderInstance) -> None:
        docker_path = str(instance.opaque_state.get("docker_path") or self._docker_path() or "docker")
        name = str(instance.opaque_state.get("container_name") or instance.provider_instance_id)
        self._stop_port_forwarders(name)
        self._run([docker_path, "rm", "-f", name], timeout=30)
        self._instances.pop(instance.provider_instance_id, None)

    def reconcile(self, persisted: ProviderInstance) -> ReconcileResult:
        docker_path = str(persisted.opaque_state.get("docker_path") or self._docker_path() or "docker")
        name = str(persisted.opaque_state.get("container_name") or persisted.provider_instance_id)
        state = self._inspect_state(docker_path, name)
        reconciled_state = "ready" if state == "running" else "stopped"
        current = ProviderInstance(
            provider_id=persisted.provider_id,
            provider_instance_id=persisted.provider_instance_id,
            sandbox_id=persisted.sandbox_id,
            runtime_id=persisted.runtime_id,
            state=reconciled_state,
            opaque_state=persisted.opaque_state,
            generation=persisted.generation,
        )
        self._instances[current.provider_instance_id] = current
        return ReconcileResult(instance=current, changed=current.state != persisted.state)

    def connect_agent(self, instance: ProviderInstance) -> "DockerGuestAgent":
        docker_path = str(instance.opaque_state.get("docker_path") or self._require_docker_ready(frozenset({"sandbox.exec"})))
        name = str(instance.opaque_state.get("container_name") or instance.provider_instance_id)
        return DockerGuestAgent(
            docker_path=docker_path,
            container_name=name,
            runner=self._runner,
            output_bytes=_positive_int(instance.opaque_state.get("output_bytes")),
            port_forwarders=self._port_forwarders,
            port_forwarder_factory=self._port_forwarder_factory,
        )

    def _stop_port_forwarders(self, container_name: str) -> None:
        for key, forwarder in list(self._port_forwarders.items()):
            if key[0] != container_name:
                continue
            forwarder.stop()
            self._port_forwarders.pop(key, None)

    def _started(self, instance: ProviderInstance) -> ProviderInstance:
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

    def _inspect_state(self, docker_path: str, name: str) -> str | None:
        result = self._run([docker_path, "inspect", "--format", "{{.State.Status}}", name], timeout=10)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _seed_overlay_workspace(self, docker_path: str, name: str, opaque_state: Mapping[str, object]) -> None:
        workspace = _workspace_binding(opaque_state)
        if workspace.get("mode") != "overlay":
            return
        root = str(workspace.get("root") or "")
        if not _usable_host_workspace_root(root):
            return
        result = self._run([docker_path, "cp", os.path.join(root, "."), f"{name}:{CONTAINER_WORKDIR}"], timeout=300)
        if result.returncode != 0:
            raise _docker_error(
                "DOCKER_WORKSPACE_SEED_FAILED",
                "Docker sandbox workspace seed failed.",
                result,
                status_code=503,
            )

    def _require_docker_ready(self, required_capabilities: frozenset[str]) -> str:
        docker_path = self._docker_path()
        if docker_path is None:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Docker CLI was not found on PATH.",
                status_code=503,
            )
        status = self.doctor(RuntimeRequirements(provider_id=self.provider_id, required_capabilities=required_capabilities))
        if not status.ready:
            raise SandboxContractError(
                RUNTIME_PROVIDER_UNAVAILABLE,
                "Docker runtime is not ready.",
                status_code=503,
                details={
                    "missing_requirements": list(status.missing_requirements),
                    "diagnostics": [diagnostic.code for diagnostic in status.diagnostics],
                },
            )
        return docker_path

    def _ensure_port_forward_helper(self, docker_path: str, name: str) -> None:
        result = self._run(_docker_port_helper_check_command(docker_path, name), timeout=10)
        if result.returncode != 0:
            raise _docker_error(
                "DOCKER_PORT_FORWARD_HELPER_UNAVAILABLE",
                "Docker sandbox port forwarding requires python3 in the container image.",
                result,
                status_code=503,
            )

    def _docker_path(self) -> str | None:
        if self._configured_docker_path:
            return self._configured_docker_path
        return shutil.which("docker")

    def _run(self, command: Sequence[str], *, timeout: float | None = None, input_text: str | None = None) -> DockerCommandResult:
        try:
            return self._runner(tuple(command), input_text, timeout)
        except TimeoutError as exc:
            return DockerCommandResult(returncode=124, stderr=str(exc))
        except OSError as exc:
            return DockerCommandResult(returncode=127, stderr=str(exc))


class DockerGuestAgent:
    def __init__(
        self,
        *,
        docker_path: str,
        container_name: str,
        runner: DockerRunner,
        output_bytes: int | None,
        port_forwarders: dict[tuple[str, int], "DockerPortForwarder"],
        port_forwarder_factory: PortForwarderFactory,
    ) -> None:
        self._docker_path = docker_path
        self._container_name = container_name
        self._runner = runner
        self._output_bytes = output_bytes
        self._port_forwarders = port_forwarders
        self._port_forwarder_factory = port_forwarder_factory

    def exec(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        request = GuestExecRequest.from_payload(payload)
        command = [self._docker_path, "exec"]
        if request.stdin is not None:
            command.append("--interactive")
        for key, value in sorted(request.env.items()):
            command.extend(["--env", f"{key}={value}"])
        command.extend(["--workdir", _container_cwd(request.cwd), self._container_name, *request.argv])
        try:
            result = self._runner(tuple(command), request.stdin, max(1, request.timeout_ms / 1000))
        except TimeoutError:
            return {
                "ok": False,
                "sandbox_id": sandbox_id,
                "code": "SANDBOX_EXEC_TIMEOUT",
                "error": "Sandbox exec timed out.",
                "status_code": 504,
                "client_request_id": request.client_request_id,
            }
        stdout, stdout_truncated = _bounded_output(result.stdout, self._output_bytes)
        stderr, stderr_truncated = _bounded_output(result.stderr, self._output_bytes)
        return {
            "ok": True,
            "sandbox_id": sandbox_id,
            "argv": list(request.argv),
            "cwd": request.cwd,
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "client_request_id": request.client_request_id,
            "provider_runtime": "docker",
        }

    def apply_file_patch(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        operations = _file_patch_operations(payload)
        applied: list[dict[str, object]] = []
        for operation in operations:
            path = str(operation["path"])
            content = operation["content"]
            if not isinstance(content, bytes):
                raise SandboxContractError(
                    "INVALID_SANDBOX_FILE_PATCH",
                    "Sandbox file patch content must be bytes.",
                    status_code=400,
                )
            parent = _container_parent(path)
            if parent:
                mkdir = self._runner(
                    (self._docker_path, "exec", self._container_name, "mkdir", "-p", parent),
                    None,
                    30,
                )
                if mkdir.returncode != 0:
                    return _guest_operation_error(sandbox_id, "SANDBOX_FILES_FAILED", "Sandbox file patch could not create parent directory.", mkdir)
            tmp_path = _write_temp_patch_file(content)
            try:
                copy = self._runner(
                    (self._docker_path, "cp", tmp_path, f"{self._container_name}:{_container_path(path)}"),
                    None,
                    60,
                )
            finally:
                _unlink_tmp(tmp_path)
            if copy.returncode != 0:
                return _guest_operation_error(sandbox_id, "SANDBOX_FILES_FAILED", "Sandbox file patch could not copy content into the sandbox.", copy)
            applied.append({"path": path, "bytes": len(content)})
        return {
            "ok": True,
            "sandbox_id": sandbox_id,
            "applied": applied,
            "files_written": len(applied),
            "provider_runtime": "docker",
        }

    def expose_port(self, sandbox_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        port = _port_number(payload.get("port"))
        protocol = str(payload.get("protocol") or "http").strip().lower()
        if protocol not in {"http", "https", "tcp"}:
            raise SandboxContractError("INVALID_SANDBOX_PORT", "Sandbox port protocol must be http, https, or tcp.", status_code=400)
        helper = self._probe_port_helper()
        if helper.returncode != 0:
            return _guest_operation_error(
                sandbox_id,
                "DOCKER_PORT_FORWARD_HELPER_UNAVAILABLE",
                "Docker sandbox port forwarding requires python3 in the container image.",
                helper,
                status_code=503,
            )
        probe = self._probe_container_loopback(port)
        if probe.returncode != 0:
            return _guest_operation_error(
                sandbox_id,
                "SANDBOX_PORTS_NOT_READY",
                "Sandbox port exposure could not verify a listening container service.",
                probe,
                status_code=503,
            )
        key = (self._container_name, port)
        forwarder = self._port_forwarders.get(key)
        if forwarder is None:
            forwarder = self._port_forwarder_factory(self._docker_path, self._container_name, port)
            self._port_forwarders[key] = forwarder
        if not _forwarder_host_probe(forwarder):
            forwarder.stop()
            self._port_forwarders.pop(key, None)
            return {
                "ok": False,
                "sandbox_id": sandbox_id,
                "port": port,
                "protocol": protocol,
                "code": "DOCKER_PORT_FORWARD_UNREACHABLE",
                "error": "Docker sandbox port forwarding could not be reached from the host.",
                "status_code": 502,
                "host_reachable": False,
                "forwarding": "docker_exec_proxy",
                "provider_runtime": "docker",
            }
        scheme = "http" if protocol == "tcp" else protocol
        url = f"{scheme}://{forwarder.host}:{forwarder.host_port}"
        container_url = f"{scheme}://127.0.0.1:{port}"
        return {
            "ok": True,
            "sandbox_id": sandbox_id,
            "port": port,
            "host": forwarder.host,
            "host_port": forwarder.host_port,
            "protocol": protocol,
            "url": url,
            "target_url": container_url,
            "container_url": container_url,
            "host_reachable": True,
            "forwarding": "docker_exec_proxy",
            "provider_runtime": "docker",
        }

    def _probe_container_loopback(self, port: int) -> DockerCommandResult:
        script = (
            "import socket, sys\n"
            "port = int(sys.argv[1])\n"
            "with socket.create_connection(('127.0.0.1', port), timeout=1.0):\n"
            "    pass\n"
        )
        return self._runner(
            (
                self._docker_path,
                "exec",
                self._container_name,
                "python3",
                "-c",
                script,
                str(int(port)),
            ),
            None,
            10,
        )

    def _probe_port_helper(self) -> DockerCommandResult:
        return self._runner(_docker_port_helper_check_command(self._docker_path, self._container_name), None, 10)

    def capture_frame(self, sandbox_id: str, seat_id: str) -> dict[str, object]:
        return {
            "ok": False,
            "sandbox_id": sandbox_id,
            "seat_id": seat_id,
            "code": "SANDBOX_DESKTOP_NOT_AVAILABLE",
            "error": "Docker sandbox provider does not expose desktop capture.",
            "status_code": 501,
        }

    def desktop_input(
        self,
        sandbox_id: str,
        seat_id: str,
        payload: Mapping[str, object],
        *,
        actor: str = "human",
    ) -> dict[str, object]:
        del payload, actor
        return {
            "ok": False,
            "sandbox_id": sandbox_id,
            "seat_id": seat_id,
            "code": "SANDBOX_DESKTOP_NOT_AVAILABLE",
            "error": "Docker sandbox provider does not expose desktop input.",
            "status_code": 501,
        }


def _subprocess_runner(command: Sequence[str], input_text: str | None, timeout: float | None) -> DockerCommandResult:
    try:
        completed = run_cancellable_subprocess(
            command,
            input_text=input_text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(str(exc)) from exc
    return DockerCommandResult(
        returncode=int(completed.returncode),
        stdout=str(completed.stdout or ""),
        stderr=str(completed.stderr or ""),
    )


class DockerPortForwarder:
    host = "127.0.0.1"

    def __init__(self, docker_path: str, container_name: str, target_port: int) -> None:
        self._docker_path = docker_path
        self._container_name = container_name
        self._target_port = int(target_port)
        self._closed = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, 0))
        self._listener.listen(64)
        self.host_port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, name=f"rumi-docker-port-{self.host_port}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._closed.set()
        try:
            self._listener.close()
        except OSError:
            pass

    def verify_host_reachable(self, *, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((self.host, self.host_port), timeout=timeout):
                return True
        except OSError:
            return False

    def _serve(self) -> None:
        while not self._closed.is_set():
            try:
                client, _addr = self._listener.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._handle_client, args=(client,), daemon=True)
            thread.start()

    def _handle_client(self, client: socket.socket) -> None:
        with client:
            try:
                proc = subprocess.Popen(
                    (
                        self._docker_path,
                        "exec",
                        "--interactive",
                        self._container_name,
                        "python3",
                        "-c",
                        _docker_exec_proxy_script(),
                        str(self._target_port),
                    ),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return
            try:
                _relay_socket_process(client, proc)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()


def _relay_socket_process(client: socket.socket, proc: subprocess.Popen[bytes]) -> None:
    if proc.stdin is None or proc.stdout is None:
        return
    stop = threading.Event()

    def client_to_process() -> None:
        try:
            while not stop.is_set():
                chunk = client.recv(65536)
                if not chunk:
                    break
                assert proc.stdin is not None
                proc.stdin.write(chunk)
                proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass
            stop.set()

    def process_to_client() -> None:
        try:
            while not stop.is_set():
                assert proc.stdout is not None
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                client.sendall(chunk)
        except OSError:
            pass
        finally:
            stop.set()

    left = threading.Thread(target=client_to_process, daemon=True)
    right = threading.Thread(target=process_to_client, daemon=True)
    left.start()
    right.start()
    while not stop.is_set() and proc.poll() is None:
        left.join(timeout=0.1)
        right.join(timeout=0.1)
    stop.set()


def _docker_exec_proxy_script() -> str:
    return (
        "import select, socket, sys\n"
        "target = socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=10)\n"
        "target.setblocking(False)\n"
        "stdin = sys.stdin.buffer\n"
        "stdout = sys.stdout.buffer\n"
        "while True:\n"
        "    readable, _, _ = select.select([target, stdin], [], [], 30)\n"
        "    if not readable:\n"
        "        continue\n"
        "    for source in readable:\n"
        "        data = source.recv(65536) if source is target else source.read1(65536)\n"
        "        if not data:\n"
        "            raise SystemExit(0)\n"
        "        if source is target:\n"
        "            stdout.write(data); stdout.flush()\n"
        "        else:\n"
        "            target.sendall(data)\n"
    )


def _docker_run_command(docker_path: str, name: str, opaque_state: Mapping[str, object]) -> list[str]:
    command = [
        docker_path,
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        "rumi.managed_runtime=true",
        "--workdir",
        CONTAINER_WORKDIR,
        "--network",
        str(opaque_state.get("network_mode") or "none"),
    ]
    memory_mb = _positive_int(opaque_state.get("memory_mb"))
    if memory_mb:
        command.extend(["--memory", f"{memory_mb}m"])
    cpu_count = _positive_float(opaque_state.get("cpu_count"))
    if cpu_count:
        command.extend(["--cpus", str(cpu_count)])
    pids = _positive_int(opaque_state.get("pids"))
    if pids:
        command.extend(["--pids-limit", str(pids)])
    workspace = _workspace_binding(opaque_state)
    if workspace.get("mode") == "read_only" and _usable_host_workspace_root(workspace.get("root")):
        command.extend([
            "--mount",
            f"type=bind,source={workspace['root']},target={CONTAINER_WORKDIR},readonly",
        ])
    command.extend([str(opaque_state.get("image") or DEFAULT_DOCKER_IMAGE), "sleep", "infinity"])
    return command


def _docker_port_helper_check_command(docker_path: str, name: str) -> tuple[str, ...]:
    return (
        docker_path,
        "exec",
        name,
        "python3",
        "-c",
        "import select, socket, sys",
    )


def _forwarder_host_probe(forwarder: "DockerPortForwarder") -> bool:
    verify = getattr(forwarder, "verify_host_reachable", None)
    if callable(verify):
        return bool(verify(timeout=2.0))
    try:
        with socket.create_connection((str(forwarder.host), int(forwarder.host_port)), timeout=2.0):
            return True
    except (OSError, TypeError, ValueError):
        return False


def _requires_port_forward(spec: SandboxCreateSpec) -> bool:
    return "sandbox.port_forward" in spec.template.provider_requirements or "sandbox.port.expose" in spec.template.allowed_operations


def _requires_port_forward_from_state(opaque_state: Mapping[str, object]) -> bool:
    return opaque_state.get("requires_port_forward") is True


def _workspace_binding(opaque_state: Mapping[str, object]) -> Mapping[str, object]:
    workspace = opaque_state.get("workspace_binding")
    return workspace if isinstance(workspace, Mapping) else {}


def _usable_host_workspace_root(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return os.path.isabs(value) and os.path.isdir(value)


def _image_for_spec(spec: SandboxCreateSpec) -> str:
    requested = str(spec.metadata.get("image") or "").strip()
    if spec.template.template_id == "coding.python" and (not requested or requested == DEFAULT_DOCKER_IMAGE):
        return CODING_PYTHON_IMAGE
    if spec.template.template_id == "coding.node" and (not requested or requested == DEFAULT_DOCKER_IMAGE):
        return CODING_NODE_IMAGE
    return requested or DEFAULT_DOCKER_IMAGE


def _docker_network_mode(spec: SandboxCreateSpec) -> str:
    if spec.template.network.mode in {"off", "deny", "none"}:
        return "none"
    if spec.template.network.approval_required:
        return "none"
    if spec.template.network.allowlist:
        return "none"
    if "sandbox.port.expose" in spec.template.allowed_operations or "sandbox.port_forward" in spec.template.provider_requirements:
        return "bridge"
    return "bridge"


def _container_name(sandbox_id: str) -> str:
    return f"rumi-sandbox-{sandbox_id}"


def _container_cwd(cwd: str) -> str:
    if cwd == ".":
        return CONTAINER_WORKDIR
    return (PurePosixPath(CONTAINER_WORKDIR) / cwd).as_posix()


def _container_path(path: str) -> str:
    return (PurePosixPath(CONTAINER_WORKDIR) / path).as_posix()


def _container_parent(path: str) -> str | None:
    parent = PurePosixPath(_container_path(path)).parent
    return None if parent.as_posix() == CONTAINER_WORKDIR else parent.as_posix()


def _file_patch_operations(payload: Mapping[str, object]) -> list[dict[str, object]]:
    raw_items = payload.get("files")
    if raw_items is None:
        raw_items = payload.get("patch")
    if raw_items is None:
        raw_items = [payload]
    if not isinstance(raw_items, list) or not raw_items:
        raise SandboxContractError("INVALID_SANDBOX_FILE_PATCH", "Sandbox file patch requires at least one file operation.", status_code=400)

    operations: list[dict[str, object]] = []
    total_bytes = 0
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            raise SandboxContractError("INVALID_SANDBOX_FILE_PATCH", "Sandbox file patch operations must be objects.", status_code=400)
        path = validate_workspace_relative_path(raw.get("path"), field="path")
        op = str(raw.get("op") or raw.get("operation") or "write").strip().lower()
        if op not in {"write", "replace", "create", "upsert"}:
            raise SandboxContractError("INVALID_SANDBOX_FILE_PATCH", "Sandbox file patch only supports write-style operations.", status_code=400)
        content = _patch_content(raw)
        total_bytes += len(content)
        if total_bytes > MAX_FILE_PATCH_BYTES:
            raise SandboxContractError("SANDBOX_FILE_PATCH_TOO_LARGE", "Sandbox file patch payload is too large.", status_code=413)
        operations.append({"path": path, "content": content})
    return operations


def _patch_content(raw: Mapping[str, object]) -> bytes:
    if "content_base64" in raw:
        value = raw.get("content_base64")
        if not isinstance(value, str):
            raise SandboxContractError("INVALID_SANDBOX_FILE_PATCH", "content_base64 must be a string.", status_code=400)
        try:
            return base64.b64decode(value, validate=True)
        except Exception as exc:
            raise SandboxContractError("INVALID_SANDBOX_FILE_PATCH", "content_base64 is invalid.", status_code=400) from exc
    value = raw.get("content")
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise SandboxContractError("INVALID_SANDBOX_FILE_PATCH", "Sandbox file patch requires content or content_base64.", status_code=400)


def _write_temp_patch_file(content: bytes) -> str:
    handle = tempfile.NamedTemporaryFile(prefix="rumi-sandbox-patch-", delete=False)
    try:
        handle.write(content)
        return handle.name
    finally:
        handle.close()


def _unlink_tmp(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _port_number(value: object) -> int:
    if isinstance(value, bool):
        raise SandboxContractError("INVALID_SANDBOX_PORT", "Sandbox port must be an integer.", status_code=400)
    try:
        port = int(_numeric_value(value) or 0)
    except (TypeError, ValueError) as exc:
        raise SandboxContractError("INVALID_SANDBOX_PORT", "Sandbox port must be an integer.", status_code=400) from exc
    if port < 1 or port > 65535:
        raise SandboxContractError("INVALID_SANDBOX_PORT", "Sandbox port must be between 1 and 65535.", status_code=400)
    return port


def _guest_operation_error(
    sandbox_id: str,
    code: str,
    message: str,
    result: DockerCommandResult,
    *,
    status_code: int = 502,
) -> dict[str, object]:
    return {
        "ok": False,
        "sandbox_id": sandbox_id,
        "code": code,
        "error": message,
        "status_code": status_code,
        "details": {"exit_code": result.returncode, "stderr": result.stderr.strip()[:1000]},
    }


def _docker_error(code: str, message: str, result: DockerCommandResult, *, status_code: int) -> SandboxContractError:
    return SandboxContractError(
        code,
        message,
        status_code=status_code,
        details={"exit_code": result.returncode, "stderr": result.stderr.strip()[:1000]},
    )


def _bounded_output(value: str, max_bytes: int | None) -> tuple[str, bool]:
    if not max_bytes or max_bytes <= 0:
        return value, False
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value, False
    clipped = encoded[:max_bytes].decode("utf-8", errors="replace")
    return clipped, True


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(_numeric_value(value) or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(_numeric_value(value) or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _numeric_value(value: object) -> int | float | str:
    return value if isinstance(value, (int, float, str)) else 0
