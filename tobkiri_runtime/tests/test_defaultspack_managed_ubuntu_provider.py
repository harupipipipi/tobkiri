from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import tarfile
from collections.abc import Sequence

import pytest

from ecosystem.defaultspack.backend.sandbox.models import (
    DesktopSpec,
    EnsureRuntimeRequest,
    FilesystemPolicy,
    LifecyclePolicy,
    NetworkPolicy,
    PackageSpec,
    ResolvedSandboxTemplate,
    ResourceLimits,
    RuntimeRequirements,
    SandboxCreateSpec,
    SecretsPolicy,
    WorkspaceBinding,
)
from ecosystem.defaultspack.backend.sandbox.errors import SandboxContractError
from ecosystem.defaultspack.backend.sandbox.providers.base import NullProgressSink
from ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu import (
    BwrapHostProvider,
    DEFAULT_DISPLAY,
    DEFAULT_WSL_RUNTIME_NAME,
    GuestCommandResult,
    MANAGED_UBUNTU_CAPABILITIES,
    MacLimaProvider,
    WSL_ROOTFS_ENV,
    WindowsWslProvider,
)


@pytest.fixture(autouse=True)
def _isolated_lima_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "RUMI_SANDBOX_LIMA_STATE",
        str(tmp_path / "lima-runtime.json"),
    )


class FakeManagedUbuntuCli:
    def __init__(self, *, mode: str, runtime_name: str) -> None:
        self.mode = mode
        self.runtime_name = runtime_name
        self.calls: list[tuple[list[str], str | None, float | None]] = []
        self.guest_scripts: list[str] = []
        self.guest_exists = False
        self.deps_installed = False
        self.desktop_running = False
        self.desktop_start_result: GuestCommandResult | None = None
        self.guest_displays_in_use: set[str] = set()
        self.imported_rootfs_path: str | None = None
        self.imported_install_dir: str | None = None
        self.port_probe_returncode = 0
        self.wsl_list_stdout: str | None = None

    def __call__(
        self,
        command: Sequence[str],
        input_text: str | None,
        timeout: float | None,
    ) -> GuestCommandResult:
        cmd = list(command)
        self.calls.append((cmd, input_text, timeout))
        if self.mode == "lima":
            return self._lima(cmd, input_text)
        return self._wsl(cmd, input_text)

    def command_containing(self, *parts: str) -> list[str]:
        for command, _input_text, _timeout in self.calls:
            if all(part in command for part in parts):
                return command
        raise AssertionError(f"command containing {parts!r} was not called")

    def _lima(self, cmd: list[str], input_text: str | None) -> GuestCommandResult:
        if cmd[1:] == ["--version"]:
            return GuestCommandResult(returncode=0, stdout="limactl version 1.0\n")
        if cmd[1:3] == ["list", "--format"]:
            return GuestCommandResult(
                returncode=0, stdout=f"{self.runtime_name}\n" if self.guest_exists else ""
            )
        if cmd[1:3] == ["list", self.runtime_name] and cmd[3:] == ["--format", "json"]:
            if not self.guest_exists:
                return GuestCommandResult(returncode=1, stderr="instance not found")
            return GuestCommandResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "name": self.runtime_name,
                        "status": "Running",
                        "vmType": "vz",
                        "arch": "aarch64",
                        "config": {
                            "vmType": "vz",
                            "mounts": [],
                            "networks": [],
                            "containerd": {"system": False, "user": False},
                            "ssh": {
                                "forwardAgent": False,
                                "forwardX11": False,
                                "forwardX11Trusted": False,
                            },
                            "propagateProxyEnv": False,
                            "hostResolver": {"enabled": False},
                            "portForwards": [
                                {
                                    "guestIP": "0.0.0.0",
                                    "guestPortRange": [1, 65535],
                                    "ignore": True,
                                }
                            ],
                        },
                    }
                ),
            )
        if cmd[1:4] == ["start", "--name", self.runtime_name]:
            self.guest_exists = True
            self.deps_installed = True
            return GuestCommandResult(returncode=0)
        if cmd[1:3] == ["start", self.runtime_name]:
            return GuestCommandResult(returncode=0)
        if cmd[1:3] == ["stop", "--force"]:
            return GuestCommandResult(returncode=0)
        if cmd[1:3] == ["delete", "--force"]:
            self.guest_exists = False
            self.deps_installed = False
            return GuestCommandResult(returncode=0)
        if cmd[1:4] == ["shell", self.runtime_name, "--"]:
            return self._guest(cmd[4:], input_text)
        return GuestCommandResult(returncode=1, stderr=f"unexpected lima command: {cmd}")

    def _wsl(self, cmd: list[str], input_text: str | None) -> GuestCommandResult:
        if cmd[1:] == ["--version"]:
            return GuestCommandResult(returncode=0, stdout="WSL version: 2.0\n")
        if cmd[1:] == ["-l", "-q"]:
            if self.wsl_list_stdout is not None:
                return GuestCommandResult(returncode=0, stdout=self.wsl_list_stdout)
            return GuestCommandResult(
                returncode=0, stdout=f"{self.runtime_name}\n" if self.guest_exists else ""
            )
        if len(cmd) >= 7 and cmd[1:3] == ["--import", self.runtime_name]:
            self.guest_exists = True
            self.imported_install_dir = cmd[3]
            self.imported_rootfs_path = cmd[4]
            return GuestCommandResult(returncode=0)
        if cmd[1:3] == ["--terminate", self.runtime_name]:
            return GuestCommandResult(returncode=0)
        if cmd[1:3] == ["--unregister", self.runtime_name]:
            self.guest_exists = False
            self.deps_installed = False
            return GuestCommandResult(returncode=0)
        if cmd[1:4] == ["-d", self.runtime_name, "--"]:
            return self._guest(cmd[4:], input_text)
        return GuestCommandResult(returncode=1, stderr=f"unexpected wsl command: {cmd}")

    def _guest(self, argv: list[str], input_text: str | None) -> GuestCommandResult:
        if argv[:1] == ["bwrap"] and "--" in argv:
            return self._guest(argv[argv.index("--") + 1 :], input_text)
        if argv[:5] == ["unshare", "--user", "--map-root-user", "--net", "--"]:
            return self._guest(argv[5:], input_text)
        if argv[:2] == ["env", "-i"]:
            env: dict[str, str] = {}
            index = 2
            while index < len(argv) and "=" in argv[index]:
                key, value = argv[index].split("=", 1)
                env[key] = value
                index += 1
            command = argv[index:]
            if command[:2] == ["bash", "-lc"] and "rumi-exec" in command:
                marker_index = command.index("rumi-exec")
                cwd = command[marker_index + 1]
                return self._guest_exec(command[marker_index + 3 :], cwd=cwd, env=env)
            return self._guest_exec(command, cwd="", env=env)
        if argv[:2] == ["bash", "-lc"]:
            script = argv[2]
            self.guest_scripts.append(script)
            if "rumi-resource-limit" in argv and "ulimit -v" in script and "ulimit -u" in script:
                marker_index = argv.index("rumi-resource-limit")
                return self._guest(argv[marker_index + 4 :], input_text)
            if "PROVISION_MARKER" in script:
                return GuestCommandResult(returncode=0)
            if "apt-get install" in script:
                self.deps_installed = True
                return GuestCommandResult(returncode=0)
            if "rumi_emit_display" in script and "/tmp/.X11-unix" in script:
                stdout = "".join(f"{display}\n" for display in sorted(self.guest_displays_in_use))
                return GuestCommandResult(returncode=0, stdout=stdout)
            if "DISPLAY_ID=" in script and "Xvfb" in script and "openbox" in script:
                for line in script.splitlines():
                    if "DISPLAY_ID=" in line:
                        display = line.split("DISPLAY_ID=", 1)[1].strip().strip("'\"")
                        if display:
                            self.guest_displays_in_use.add(display)
                if self.desktop_start_result is not None:
                    return self.desktop_start_result
                self.desktop_running = True
                return GuestCommandResult(returncode=0)
            if "command -v" in script:
                if self.deps_installed:
                    return GuestCommandResult(returncode=0)
                return GuestCommandResult(
                    returncode=0, stdout="Xvfb\nopenbox\nxdotool\nimport\npython3\n"
                )
            if "kill -0" in script:
                return GuestCommandResult(returncode=0 if self.desktop_running else 1)
            return GuestCommandResult(returncode=0)
        if argv[:2] == ["mkdir", "-p"]:
            return GuestCommandResult(returncode=0)
        if argv[:2] == ["python3", "-c"]:
            if len(argv) > 2 and "socket.create_connection" in argv[2]:
                return GuestCommandResult(
                    returncode=self.port_probe_returncode,
                    stderr="connection refused" if self.port_probe_returncode else "",
                )
            assert input_text
            return GuestCommandResult(returncode=0)
        if argv[:3] == ["env", "DISPLAY=:98", "bash"]:
            return GuestCommandResult(returncode=0, stdout=base64.b64encode(b"png").decode("ascii"))
        if argv[:3] == ["env", "DISPLAY=:98", "xdotool"]:
            return GuestCommandResult(returncode=0)
        if argv[:1] == ["emit-long"]:
            return GuestCommandResult(returncode=0, stdout="0123456789", stderr="abcdefghij")
        if argv[:2] == ["echo", "hello"]:
            return GuestCommandResult(returncode=0, stdout="hello\n")
        return GuestCommandResult(returncode=0)

    def _guest_exec(self, argv: list[str], *, cwd: str, env: dict[str, str]) -> GuestCommandResult:
        if argv[:1] == ["emit-long"]:
            return GuestCommandResult(returncode=0, stdout="0123456789", stderr="abcdefghij")
        if argv[:2] == ["echo", "hello"]:
            return GuestCommandResult(returncode=0, stdout="hello\n")
        if argv[:1] == ["pwd"]:
            return GuestCommandResult(returncode=0, stdout=f"{cwd}\n")
        if argv[:1] == ["printenv"] and len(argv) >= 2:
            key = argv[1]
            if key in env:
                return GuestCommandResult(returncode=0, stdout=f"{env[key]}\n")
            return GuestCommandResult(returncode=1)
        return GuestCommandResult(returncode=0)


def test_managed_provider_without_launcher_does_not_advertise_capabilities(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.resolve_limactl_path",
        lambda: None,
    )
    provider = MacLimaProvider()

    status = provider.doctor(RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES))

    assert status.available is False
    assert status.ready is False
    assert status.capabilities == frozenset()
    assert "command:limactl" in status.missing_requirements
    assert "brew install lima" in str(status.user_action)


def test_bwrap_doctor_versions_launcher_through_bounded_host_runner(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Linux",
    )
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.shutil.which",
        lambda command: sys.executable if command == "bwrap" else None,
    )
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu._unprivileged_userns_available",
        lambda: True,
    )

    status = BwrapHostProvider().doctor(RuntimeRequirements())

    assert status.version is not None
    assert status.version.startswith("Python ")


def test_managed_provider_does_not_advertise_host_port_forwarding(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    fake.guest_exists = True
    fake.deps_installed = True
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)

    status = provider.doctor(
        RuntimeRequirements(required_capabilities=frozenset({"sandbox.port_forward"}))
    )

    assert status.available is True
    assert status.installed is True
    assert status.ready is False
    assert "sandbox.port_forward" not in status.capabilities
    assert "sandbox.port_forward" in status.missing_requirements
    assert "host port forwarding" in str(status.user_action)


def _template(
    *,
    desktop: bool = True,
    output_bytes: int = 4096,
    timeout_ms: int | None = None,
    memory_mb: int = 2048,
    cpu_count: float | None = 1,
    pids: int | None = None,
    network_mode: str = "limited_or_approval_gated",
    network_approval_required: bool = True,
    packages: tuple[PackageSpec, ...] = (),
) -> ResolvedSandboxTemplate:
    requirements = (
        MANAGED_UBUNTU_CAPABILITIES if desktop else frozenset({"sandbox.exec", "sandbox.files"})
    )
    return ResolvedSandboxTemplate(
        template_id="desktop.ubuntu" if desktop else "coding.python",
        template_version="1",
        runtime_os="linux",
        provider_requirements=requirements,
        packages=packages,
        desktop=DesktopSpec(enabled=True, width=800, height=600) if desktop else None,
        filesystem=FilesystemPolicy(),
        network=NetworkPolicy(mode=network_mode, approval_required=network_approval_required),
        secrets=SecretsPolicy(),
        resources=ResourceLimits(
            memory_mb=memory_mb,
            cpu_count=cpu_count,
            pids=pids,
            output_bytes=output_bytes,
            timeout_ms=timeout_ms,
        ),
        lifecycle=LifecyclePolicy(),
        allowed_operations=requirements,
        source_template_ids=("test",),
    )


def _create_spec(
    template: ResolvedSandboxTemplate,
    *,
    startup: dict[str, object] | None = None,
    provisioning: dict[str, object] | None = None,
    workspace_binding: WorkspaceBinding | None = None,
    network_approved: bool = False,
) -> SandboxCreateSpec:
    return SandboxCreateSpec(
        name="Managed Ubuntu",
        template=template,
        provider_id="auto",
        workspace_binding=workspace_binding
        or WorkspaceBinding(workspace_id="workspace-1", mode="read_only"),
        metadata={
            "startup": startup or {"starter": "terminal"},
            "desktop_provisioning": provisioning or {},
            "network_approved": network_approved,
        },
    )


def _workspace_seed_call(fake: FakeManagedUbuntuCli, mode: str) -> tuple[str, str]:
    marker = f"RUMI_WORKSPACE_SEED_MODE={mode}"
    for command, input_text, _timeout in fake.calls:
        if len(command) >= 2 and command[-2] == "-lc" and marker in command[-1]:
            assert input_text
            return command[-1], input_text
    raise AssertionError(f"workspace seed call for {mode!r} was not made")


def _workspace_seed_member(payload: str, member_name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload)), mode="r:gz") as archive:
        member = archive.extractfile(member_name)
        assert member is not None
        return member.read()


def test_mac_lima_provider_ensure_and_guest_desktop_flow(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)

    before = provider.doctor(requirements)
    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    after = provider.doctor(requirements)
    instance = provider.create(
        _create_spec(_template(network_mode="host_shared", network_approval_required=False))
    )
    started = provider.start(instance)
    agent = provider.connect_agent(started)
    executed = agent.exec(
        started.sandbox_id, {"argv": ["echo", "hello"], "cwd": ".", "client_request_id": "exec-1"}
    )
    patched = agent.apply_file_patch(
        started.sandbox_id, {"path": "src/app.py", "content": "print('hello')\n"}
    )
    exposed = agent.expose_port(started.sandbox_id, {"port": 3000, "protocol": "http"})
    frame = agent.capture_frame(started.sandbox_id, started.sandbox_id)
    click = agent.desktop_input(
        started.sandbox_id,
        started.sandbox_id,
        {"action": "click", "client_action_id": "click-1", "x": 1, "y": 2},
    )

    assert before.ready is False
    assert "managed_guest" in before.missing_requirements
    assert ensured.ok is True
    assert after.ready is True
    assert started.state == "ready"
    assert executed["stdout"] == "hello\n"
    assert patched["ok"] is True
    assert exposed["ok"] is False
    assert exposed["code"] == "SANDBOX_PORT_FORWARD_UNAVAILABLE"
    assert exposed["target_url"] == "http://127.0.0.1:3000"
    assert exposed["forwarding"] == "unavailable"
    assert any(
        "socket.create_connection" in " ".join(command) for command, _input, _timeout in fake.calls
    )
    assert frame["data"] == b"png"
    assert click["ok"] is True
    install_script = next(
        script for script in fake.guest_scripts if "$RUMI_SUDO apt-get install -y xvfb" in script
    )
    assert "id -u" in install_script
    assert "RUMI_SUDO='sudo'" in install_script
    assert "sudo apt-get" not in install_script
    assert any("xterm -title 'Rumi Desktop'" in script for script in fake.guest_scripts)
    assert fake.command_containing("shell", "rumi-managed-runtime", "--", "echo", "hello")[-2:] == [
        "echo",
        "hello",
    ]
    assert started.opaque_state["guest_workspace"].startswith("/var/lib/rumi/workspaces/mac_lima-")


def test_managed_ubuntu_guest_agent_rejects_exec_cwd_before_guest_command(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    provider.ensure(
        EnsureRuntimeRequest(
            provider_id="mac_lima",
            requirements=RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES),
        ),
        NullProgressSink(),
    )
    started = provider.start(
        provider.create(
            _create_spec(_template(network_mode="host_shared", network_approval_required=False))
        )
    )
    agent = provider.connect_agent(started)
    fake.calls.clear()

    with pytest.raises(SandboxContractError) as exc:
        agent.exec(
            started.sandbox_id,
            {"argv": ["pwd"], "cwd": "../outside", "client_request_id": "exec-cwd"},
        )

    assert exc.value.code == "INVALID_EXEC_REQUEST"
    assert fake.calls == []


def test_managed_ubuntu_guest_agent_rejects_file_patch_path_before_guest_command(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    provider.ensure(
        EnsureRuntimeRequest(
            provider_id="mac_lima",
            requirements=RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES),
        ),
        NullProgressSink(),
    )
    started = provider.start(
        provider.create(
            _create_spec(_template(network_mode="host_shared", network_approval_required=False))
        )
    )
    agent = provider.connect_agent(started)
    fake.calls.clear()

    with pytest.raises(SandboxContractError) as exc:
        agent.apply_file_patch(
            started.sandbox_id, {"path": "/tmp/outside.py", "content": "print('outside')"}
        )

    assert exc.value.code == "INVALID_EXEC_REQUEST"
    assert fake.calls == []


def test_managed_ubuntu_desktops_get_distinct_guest_displays(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    first = provider.start(
        provider.create(
            _create_spec(_template(network_mode="host_shared", network_approval_required=False))
        )
    )
    second = provider.start(
        provider.create(
            _create_spec(_template(network_mode="host_shared", network_approval_required=False))
        )
    )
    desktop_scripts = [
        script
        for script in fake.guest_scripts
        if "DISPLAY_ID=" in script and "Xvfb" in script and "openbox" in script
    ]

    assert ensured.ok is True
    assert first.opaque_state["display"] == DEFAULT_DISPLAY
    assert second.opaque_state["display"] == ":99"
    assert len({first.opaque_state["display"], second.opaque_state["display"]}) == 2
    assert "DISPLAY_ID=':98'" in desktop_scripts[-2]
    assert "Xvfb :98" in desktop_scripts[-2]
    assert "DISPLAY_ID=':99'" in desktop_scripts[-1]
    assert "Xvfb :99" in desktop_scripts[-1]


def test_managed_ubuntu_desktop_create_skips_guest_occupied_displays(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Windows",
    )
    fake = FakeManagedUbuntuCli(mode="wsl", runtime_name=DEFAULT_WSL_RUNTIME_NAME)
    fake.guest_exists = True
    fake.deps_installed = True
    fake.guest_displays_in_use.update({":98", ":99"})
    provider = WindowsWslProvider(command_path="C:/Windows/System32/wsl.exe", runner=fake)

    instance = provider.create(
        _create_spec(_template(network_mode="host_shared", network_approval_required=False))
    )
    started = provider.start(instance)
    desktop_script = next(script for script in fake.guest_scripts if "DISPLAY_ID=':100'" in script)

    assert instance.opaque_state["display"] == ":100"
    assert started.state == "ready"
    assert "Xvfb :100" in desktop_script
    assert "\\$DISPLAY_ID" not in desktop_script
    assert "\\${DISPLAY_ID#:}" not in desktop_script
    assert ":100" in fake.guest_displays_in_use


def test_windows_wsl_desktop_start_fails_when_xvfb_does_not_survive(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Windows",
    )
    fake = FakeManagedUbuntuCli(mode="wsl", runtime_name=DEFAULT_WSL_RUNTIME_NAME)
    fake.guest_exists = True
    fake.deps_installed = True
    fake.desktop_start_result = GuestCommandResult(
        returncode=126,
        stderr=(
            "Desktop Xvfb failed to stay running.\n"
            "_XSERVTransmkdir: Mode of /tmp/.X11-unix should be set to 1777\n"
        ),
    )
    provider = WindowsWslProvider(command_path="C:/Windows/System32/wsl.exe", runner=fake)
    instance = provider.create(
        _create_spec(_template(network_mode="host_shared", network_approval_required=False))
    )

    with pytest.raises(SandboxContractError) as exc:
        provider.start(instance)

    desktop_script = next(script for script in fake.guest_scripts if "DISPLAY_ID=':98'" in script)
    assert exc.value.code == "RUNTIME_PROVIDER_UNAVAILABLE"
    assert "Managed Ubuntu guest command failed" in str(exc.value)
    assert "Desktop Xvfb failed to stay running" in str(exc.value.details)
    assert fake.desktop_running is False
    assert provider.reconcile(instance).instance.state == "stopped"
    assert "mkdir -p /tmp/.X11-unix" in desktop_script
    assert "chmod 1777 /tmp/.X11-unix" in desktop_script
    assert 'rm -f "/tmp/.X${DISPLAY_NUM}-lock"' in desktop_script
    assert "rumi_pidfile_alive" in desktop_script
    assert "Desktop Xvfb failed to stay running." in desktop_script
    assert "Desktop openbox failed to stay running." in desktop_script


def test_windows_wsl_provider_ensure_imports_rumi_owned_distribution(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Windows",
    )
    rootfs = tmp_path / "rumi-ubuntu-rootfs.tar"
    rootfs.write_bytes(b"rootfs")
    install_dir = tmp_path / "RumiUbuntu"
    fake = FakeManagedUbuntuCli(mode="wsl", runtime_name=DEFAULT_WSL_RUNTIME_NAME)
    provider = WindowsWslProvider(
        command_path="C:/Windows/System32/wsl.exe",
        runner=fake,
        rootfs_path=str(rootfs),
        install_dir=str(install_dir),
    )
    requirements = RuntimeRequirements(
        required_capabilities=frozenset({"sandbox.exec", "sandbox.files"})
    )

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="windows_wsl", requirements=requirements),
        NullProgressSink(),
    )
    status = provider.doctor(requirements)
    instance = provider.create(_create_spec(_template(desktop=False)))
    started = provider.start(instance)
    agent = provider.connect_agent(started)
    executed = agent.exec(
        started.sandbox_id, {"argv": ["echo", "hello"], "cwd": ".", "client_request_id": "exec-1"}
    )

    assert ensured.ok is True
    assert status.ready is True
    assert fake.guest_exists is True
    assert fake.deps_installed is True
    assert fake.imported_rootfs_path == str(rootfs)
    assert fake.imported_install_dir == str(install_dir)
    assert executed["stdout"] == "hello\n"
    assert fake.command_containing(
        "--import", DEFAULT_WSL_RUNTIME_NAME, str(install_dir), str(rootfs), "--version", "2"
    )
    assert fake.command_containing("-d", DEFAULT_WSL_RUNTIME_NAME, "--", "echo", "hello")[-2:] == [
        "echo",
        "hello",
    ]
    assert started.opaque_state["guest_workspace"].startswith(
        "/var/lib/rumi/workspaces/windows_wsl-"
    )
    install_script = next(
        script for script in fake.guest_scripts if "$RUMI_SUDO apt-get install -y xvfb" in script
    )
    assert "\\$RUMI_SUDO" not in install_script


def test_windows_wsl_guest_shell_preserves_guest_variable_expansion(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Windows",
    )
    fake = FakeManagedUbuntuCli(mode="wsl", runtime_name=DEFAULT_WSL_RUNTIME_NAME)
    provider = WindowsWslProvider(command_path="C:/Windows/System32/wsl.exe", runner=fake)
    script = (
        "set -e\n"
        "$RUMI_SUDO apt-get update\n"
        'DISPLAY_NUM="${DISPLAY_ID#:}"\n'
        'echo "$DISPLAY_ID" "$CLIENT_DISPLAY" "$@"\n'
    )

    provider._guest_shell("C:/Windows/System32/wsl.exe", script)

    command = fake.command_containing("-d", DEFAULT_WSL_RUNTIME_NAME, "--", "bash", "-lc")
    assert command[-1] == script
    assert fake.guest_scripts[-1] == script
    assert "$RUMI_SUDO apt-get update" in script
    assert "${DISPLAY_ID#:}" in script
    assert "$CLIENT_DISPLAY" in script
    assert "$@" in script
    assert "\\$RUMI_SUDO" not in script
    assert "\\$DISPLAY_ID" not in script
    assert "\\${DISPLAY_ID#:}" not in script
    assert "\\$CLIENT_DISPLAY" not in script
    assert "\\$@" not in script


def test_managed_ubuntu_exec_defaults_to_instance_workspace_and_clean_env(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)

    provider.ensure(
        EnsureRuntimeRequest(
            provider_id="mac_lima",
            requirements=RuntimeRequirements(
                required_capabilities=frozenset({"sandbox.exec", "sandbox.files"})
            ),
        ),
        NullProgressSink(),
    )
    started = provider.start(provider.create(_create_spec(_template(desktop=False))))
    agent = provider.connect_agent(started)

    pwd = agent.exec(
        started.sandbox_id, {"argv": ["pwd"], "cwd": ".", "client_request_id": "exec-pwd"}
    )
    env = agent.exec(
        started.sandbox_id,
        {
            "argv": ["printenv", "RUMI_TEST"],
            "cwd": ".",
            "env": {"RUMI_TEST": "from-request"},
            "client_request_id": "exec-env",
        },
    )
    ambient = agent.exec(
        started.sandbox_id,
        {"argv": ["printenv", "HOST_SECRET"], "cwd": ".", "client_request_id": "exec-ambient"},
    )
    exec_call = next(
        call for call in fake.calls if "exec-env" not in str(call) and "printenv" in call[0]
    )

    assert pwd["stdout"] == "/workspace\n"
    assert pwd["resolved_cwd"] == started.opaque_state["guest_workspace"]
    assert env["stdout"] == "from-request\n"
    assert ambient["exit_code"] == 1
    assert "env" in exec_call[0]
    assert "-i" in exec_call[0]
    assert "rumi-exec" in exec_call[0]
    assert f"/tmp/rumi-managed-runtime/{started.provider_instance_id}" in exec_call[0]
    assert f"RUMI_SANDBOX_INSTANCE={started.provider_instance_id}" in exec_call[0]
    assert "RUMI_SANDBOX_WORKSPACE=/workspace" in exec_call[0]
    assert all(not part.startswith("HOST_SECRET=") for part in exec_call[0])


def test_managed_ubuntu_exec_rejects_reserved_env_override(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    provider.ensure(
        EnsureRuntimeRequest(
            provider_id="mac_lima",
            requirements=RuntimeRequirements(
                required_capabilities=frozenset({"sandbox.exec", "sandbox.files"})
            ),
        ),
        NullProgressSink(),
    )
    started = provider.start(provider.create(_create_spec(_template(desktop=False))))
    agent = provider.connect_agent(started)

    with pytest.raises(SandboxContractError) as excinfo:
        agent.exec(
            started.sandbox_id,
            {
                "argv": ["true"],
                "cwd": ".",
                "env": {"RUMI_SANDBOX_INSTANCE": "spoofed"},
                "client_request_id": "exec-reserved-env",
            },
        )

    assert excinfo.value.code == "INVALID_EXEC_REQUEST"
    assert excinfo.value.details["reserved_env"] == ["RUMI_SANDBOX_INSTANCE"]


def test_managed_ubuntu_non_desktop_reconcile_keeps_instance_ready_for_cleanup(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    provider.ensure(
        EnsureRuntimeRequest(
            provider_id="mac_lima",
            requirements=RuntimeRequirements(
                required_capabilities=frozenset({"sandbox.exec", "sandbox.files"})
            ),
        ),
        NullProgressSink(),
    )
    started = provider.start(provider.create(_create_spec(_template(desktop=False))))

    reconciled = provider.reconcile(started)
    provider.stop(reconciled.instance)
    stop_script = fake.guest_scripts[-1]

    assert reconciled.instance.state == "ready"
    assert f"{started.opaque_state['guest_workspace']}" in fake.guest_scripts[-2]
    assert f"{started.provider_instance_id}" in stop_script
    assert "/procs/*.pid" in stop_script
    assert 'kill -"$signal" -- "-$pid"' in stop_script


def test_managed_ubuntu_instances_bind_agent_operations_to_distinct_workspaces(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    root_a = tmp_path / "workspace-a"
    root_b = tmp_path / "workspace-b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "app.txt").write_text("A\n", encoding="utf-8")
    (root_b / "app.txt").write_text("B\n", encoding="utf-8")
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    provider.ensure(
        EnsureRuntimeRequest(
            provider_id="mac_lima",
            requirements=RuntimeRequirements(
                required_capabilities=frozenset({"sandbox.exec", "sandbox.files"})
            ),
        ),
        NullProgressSink(),
    )

    first = provider.start(
        provider.create(
            _create_spec(
                _template(desktop=False),
                workspace_binding=WorkspaceBinding(
                    workspace_id="workspace-a", mode="overlay", root=str(root_a)
                ),
            )
        )
    )
    second = provider.start(
        provider.create(
            _create_spec(
                _template(desktop=False),
                workspace_binding=WorkspaceBinding(
                    workspace_id="workspace-b", mode="overlay", root=str(root_b)
                ),
            )
        )
    )
    first_agent = provider.connect_agent(first)
    second_agent = provider.connect_agent(second)
    first_agent.apply_file_patch(
        first.sandbox_id, {"path": "src/app.py", "content": "print('a')\n"}
    )
    second_agent.apply_file_patch(
        second.sandbox_id, {"path": "src/app.py", "content": "print('b')\n"}
    )
    python_writes = [
        call
        for call in fake.calls
        if len(call[0]) > 2
        and call[0][-3:-1]
        == [
            "-c",
            "import base64, pathlib, sys\npath = pathlib.Path(sys.argv[1])\npath.write_bytes(base64.b64decode(sys.stdin.read().encode('ascii')))\n",
        ]
    ]

    assert first.opaque_state["guest_workspace"] != second.opaque_state["guest_workspace"]
    assert first.opaque_state["guest_workspace"].startswith("/var/lib/rumi/workspaces/mac_lima-")
    assert second.opaque_state["guest_workspace"].startswith("/var/lib/rumi/workspaces/mac_lima-")
    seed_scripts = [
        script for script in fake.guest_scripts if "RUMI_WORKSPACE_SEED_MODE=overlay" in script
    ]
    assert len(seed_scripts) >= 2
    assert str(first.opaque_state["guest_workspace"]) in seed_scripts[-2]
    assert str(second.opaque_state["guest_workspace"]) in seed_scripts[-1]
    assert any(
        str(first.opaque_state["guest_workspace"]) in call[0] and "/workspace/src/app.py" in call[0]
        for call in python_writes
    )
    assert any(
        str(second.opaque_state["guest_workspace"]) in call[0]
        and "/workspace/src/app.py" in call[0]
        for call in python_writes
    )


def test_windows_wsl_provider_does_not_claim_existing_user_ubuntu_distribution(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Windows",
    )
    fake = FakeManagedUbuntuCli(mode="wsl", runtime_name="Ubuntu")
    fake.guest_exists = True
    provider = WindowsWslProvider(command_path="C:/Windows/System32/wsl.exe", runner=fake)

    status = provider.doctor(RuntimeRequirements(required_capabilities=frozenset({"sandbox.exec"})))

    assert status.ready is False
    assert "managed_guest" in status.missing_requirements


def test_windows_wsl_provider_detects_nul_separated_rumi_distribution(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Windows",
    )
    fake = FakeManagedUbuntuCli(mode="wsl", runtime_name=DEFAULT_WSL_RUNTIME_NAME)
    fake.guest_exists = True
    fake.deps_installed = True
    fake.wsl_list_stdout = (
        "\ufeffd\x00o\x00c\x00k\x00e\x00r\x00-\x00d\x00e\x00s\x00k\x00t\x00o\x00p\x00\n\x00"
        "R\x00u\x00m\x00i\x00U\x00b\x00u\x00n\x00t\x00u\x00\n\x00"
    )
    provider = WindowsWslProvider(command_path="C:/Windows/System32/wsl.exe", runner=fake)

    status = provider.doctor(RuntimeRequirements(required_capabilities=frozenset({"sandbox.exec"})))

    assert status.ready is True
    assert "managed_guest" not in status.missing_requirements
    assert not any("--import" in command for command, _input_text, _timeout in fake.calls)


def test_windows_wsl_provider_downloads_rumi_rootfs_when_not_configured(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Windows",
    )
    monkeypatch.delenv(WSL_ROOTFS_ENV, raising=False)
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    install_dir = tmp_path / "RumiUbuntu"
    cache_dir = tmp_path / "rootfs-cache"
    fake = FakeManagedUbuntuCli(mode="wsl", runtime_name=DEFAULT_WSL_RUNTIME_NAME)
    downloaded: list[tuple[str, str]] = []

    def downloader(url: str, destination: str) -> None:
        downloaded.append((url, destination))
        with open(destination, "wb") as handle:
            handle.write(b"rootfs")

    def checksum_fetcher(url: str) -> str:
        digest = hashlib.sha256(b"rootfs").hexdigest()
        return f"{digest}  ubuntu-jammy-wsl-amd64-wsl.rootfs.tar.gz\n"

    provider = WindowsWslProvider(
        command_path="C:/Windows/System32/wsl.exe",
        runner=fake,
        install_dir=str(install_dir),
        rootfs_cache_dir=str(cache_dir),
        rootfs_downloader=downloader,
        checksum_fetcher=checksum_fetcher,
    )

    ensured = provider.ensure(
        EnsureRuntimeRequest(
            provider_id="windows_wsl",
            requirements=RuntimeRequirements(required_capabilities=frozenset({"sandbox.exec"})),
        ),
        NullProgressSink(),
    )

    assert ensured.ok is True
    assert fake.guest_exists is True
    assert fake.deps_installed is True
    assert len(downloaded) == 1
    assert (
        downloaded[0][0]
        == "https://cloud-images.ubuntu.com/wsl/releases/22.04/current/ubuntu-jammy-wsl-amd64-wsl.rootfs.tar.gz"
    )
    assert downloaded[0][1].startswith(
        str(cache_dir / "ubuntu-jammy-wsl-amd64-wsl.rootfs.tar.gz.tmp-")
    )
    assert fake.imported_rootfs_path == str(cache_dir / "ubuntu-jammy-wsl-amd64-wsl.rootfs.tar.gz")
    assert fake.imported_install_dir == str(install_dir)


def test_windows_wsl_provider_replaces_corrupt_cached_rootfs(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Windows",
    )
    monkeypatch.delenv(WSL_ROOTFS_ENV, raising=False)
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    cache_dir = tmp_path / "rootfs-cache"
    cache_dir.mkdir()
    cached_rootfs = cache_dir / "ubuntu-jammy-wsl-amd64-wsl.rootfs.tar.gz"
    cached_rootfs.write_bytes(b"corrupt")
    fake = FakeManagedUbuntuCli(mode="wsl", runtime_name=DEFAULT_WSL_RUNTIME_NAME)
    downloaded: list[str] = []

    def downloader(url: str, destination: str) -> None:
        downloaded.append(url)
        with open(destination, "wb") as handle:
            handle.write(b"rootfs")

    def checksum_fetcher(url: str) -> str:
        digest = hashlib.sha256(b"rootfs").hexdigest()
        return f"{digest}  {cached_rootfs.name}\n"

    provider = WindowsWslProvider(
        command_path="C:/Windows/System32/wsl.exe",
        runner=fake,
        rootfs_cache_dir=str(cache_dir),
        rootfs_downloader=downloader,
        checksum_fetcher=checksum_fetcher,
    )

    ensured = provider.ensure(
        EnsureRuntimeRequest(
            provider_id="windows_wsl",
            requirements=RuntimeRequirements(required_capabilities=frozenset({"sandbox.exec"})),
        ),
        NullProgressSink(),
    )

    assert ensured.ok is True
    assert downloaded == [
        "https://cloud-images.ubuntu.com/wsl/releases/22.04/current/ubuntu-jammy-wsl-amd64-wsl.rootfs.tar.gz"
    ]
    assert cached_rootfs.read_bytes() == b"rootfs"
    assert fake.imported_rootfs_path == str(cached_rootfs)


def test_windows_wsl_provider_uses_verified_cached_rootfs_offline(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Windows",
    )
    monkeypatch.delenv(WSL_ROOTFS_ENV, raising=False)
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    cache_dir = tmp_path / "rootfs-cache"
    cache_dir.mkdir()
    cached_rootfs = cache_dir / "ubuntu-jammy-wsl-amd64-wsl.rootfs.tar.gz"
    cached_rootfs.write_bytes(b"rootfs")
    (cache_dir / f"{cached_rootfs.name}.sha256").write_text(
        hashlib.sha256(b"rootfs").hexdigest() + "\n",
        encoding="utf-8",
    )
    fake = FakeManagedUbuntuCli(mode="wsl", runtime_name=DEFAULT_WSL_RUNTIME_NAME)

    def unexpected_downloader(url: str, destination: str) -> None:
        raise AssertionError(f"unexpected rootfs download: {url} -> {destination}")

    def unexpected_checksum_fetcher(url: str) -> str:
        raise AssertionError(f"unexpected SHA256SUMS fetch: {url}")

    provider = WindowsWslProvider(
        command_path="C:/Windows/System32/wsl.exe",
        runner=fake,
        rootfs_cache_dir=str(cache_dir),
        rootfs_downloader=unexpected_downloader,
        checksum_fetcher=unexpected_checksum_fetcher,
    )

    ensured = provider.ensure(
        EnsureRuntimeRequest(
            provider_id="windows_wsl",
            requirements=RuntimeRequirements(required_capabilities=frozenset({"sandbox.exec"})),
        ),
        NullProgressSink(),
    )

    assert ensured.ok is True
    assert fake.imported_rootfs_path == str(cached_rootfs)


def test_managed_ubuntu_exec_enforces_template_output_and_timeout_limits(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(
        required_capabilities=frozenset({"sandbox.exec", "sandbox.files"})
    )

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(
        _create_spec(
            _template(
                desktop=False, output_bytes=5, timeout_ms=2_000, memory_mb=64, cpu_count=2, pids=32
            )
        )
    )
    started = provider.start(instance)
    agent = provider.connect_agent(started)
    executed = agent.exec(
        started.sandbox_id,
        {
            "argv": ["emit-long"],
            "cwd": ".",
            "client_request_id": "exec-long",
            "timeout_ms": 600_000,
        },
    )
    exec_call = next(call for call in fake.calls if "emit-long" in call[0])

    assert ensured.ok is True
    assert executed["stdout"] == "01234"
    assert executed["stderr"] == "abcde"
    assert executed["stdout_truncated"] is True
    assert executed["stderr_truncated"] is True
    assert exec_call[2] == 2
    assert "bwrap" in exec_call[0]
    assert "--unshare-net" in exec_call[0]
    assert any("ulimit -v" in part for part in exec_call[0])
    assert any("ulimit -u" in part for part in exec_call[0])
    assert any("taskset -c" in part for part in exec_call[0])
    assert "65536" in exec_call[0]
    assert "0-1" in exec_call[0]
    assert "32" in exec_call[0]
    assert exec_call[0][-1] == "emit-long"


def test_managed_ubuntu_desktop_browser_url_starter_is_projected_to_guest(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(
        _create_spec(
            _template(network_mode="host_shared", network_approval_required=False),
            startup={"starter": "browser_url", "browser_url": "https://example.com"},
        )
    )
    started = provider.start(instance)
    start_script = next(
        script for script in fake.guest_scripts if "BROWSER_URL_ORIGINAL=" in script
    )

    assert ensured.ok is True
    assert started.state == "ready"
    assert "BROWSER_URL_ORIGINAL=https://example.com" in start_script
    assert (
        'BROWSER_URL="$(python3 - "$BROWSER_URL_ORIGINAL" "$RUMI_HOST_LOOPBACK_ALIAS"'
        in start_script
    )
    assert (
        "BROWSER_CANDIDATES='google-chrome-stable google-chrome chromium chromium-browser firefox'"
        in start_script
    )
    assert 'BROWSER_CANDIDATES="$BROWSER_CANDIDATES xdg-open"' in start_script
    assert "run_detached()" in start_script
    assert "setsid -f sh -c" in start_script
    assert "/etc/machine-id" in start_script
    assert (
        '"$BROWSER_BIN" --no-sandbox --no-first-run --disable-dev-shm-usage --user-data-dir='
        in start_script
    )
    assert "starter-browser.log" in start_script


def test_managed_ubuntu_desktop_browser_url_starter_rewrites_host_loopback(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(
        _create_spec(
            _template(network_mode="host_shared", network_approval_required=False),
            startup={
                "starter": "browser_url",
                "browser_url": "http://127.0.0.1:8766/chat?chat=qa-loop",
            },
        )
    )
    started = provider.start(instance)
    start_script = next(
        script for script in fake.guest_scripts if "BROWSER_URL_ORIGINAL=" in script
    )

    assert ensured.ok is True
    assert started.state == "ready"
    assert "BROWSER_URL_ORIGINAL='http://127.0.0.1:8766/chat?chat=qa-loop'" in start_script
    assert "host.lima.internal host.docker.internal" in start_script
    assert "/etc/resolv.conf" in start_script
    assert "host in {'127.0.0.1', 'localhost'}" in start_script
    assert "netloc = f'{host_alias}:{parsed.port}'" in start_script
    assert "BROWSER_URL=http://127.0.0.1:8766/chat" not in start_script
    assert (
        '"$BROWSER_BIN" --no-sandbox --no-first-run --disable-dev-shm-usage --user-data-dir='
        in start_script
    )


def test_managed_ubuntu_desktop_browser_starter_opens_browser_without_url(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(_create_spec(_template(), startup={"starter": "browser"}))
    started = provider.start(instance)
    start_script = next(
        script for script in fake.guest_scripts if "BROWSER_URL_ORIGINAL=" in script
    )

    assert ensured.ok is True
    assert started.state == "ready"
    assert "BROWSER_URL_ORIGINAL=''" in start_script
    assert 'BROWSER_CANDIDATES="$BROWSER_CANDIDATES xdg-open"' in start_script
    assert 'elif [ -n "$BROWSER_URL" ]; then' in start_script
    assert "run_detached" in start_script
    assert (
        '"$BROWSER_BIN" --no-sandbox --no-first-run --disable-dev-shm-usage --user-data-dir='
        in start_script
    )
    assert "starter-browser.pid" in start_script


def test_managed_ubuntu_stop_cleans_desktop_starter_processes(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(
        _create_spec(
            _template(network_mode="host_shared", network_approval_required=False),
            startup={"starter": "browser_url", "browser_url": "https://example.com"},
        )
    )
    started = provider.start(instance)
    provider.stop(started)
    stop_script = fake.guest_scripts[-1]

    assert ensured.ok is True
    assert "starter-browser.pid" in stop_script
    assert "starter-terminal.pid" in stop_script
    assert stop_script.index("starter-browser.pid") < stop_script.index("openbox.pid")
    assert stop_script.index("starter-terminal.pid") < stop_script.index("xvfb.pid")
    assert f"RUMI_SANDBOX_INSTANCE={started.provider_instance_id}" in stop_script
    assert "/proc/[0-9]*/environ" in stop_script
    assert "rumi_kill_instance_processes TERM" in stop_script
    assert "rumi_kill_instance_processes KILL" in stop_script


def test_managed_ubuntu_browser_url_starter_respects_network_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(
        _create_spec(
            _template(), startup={"starter": "browser_url", "browser_url": "https://example.com"}
        )
    )
    started = provider.start(instance)
    start_script = next(script for script in fake.guest_scripts if "starter-browser.log" in script)

    assert ensured.ok is True
    assert started.state == "ready"
    assert "RUMI_NETWORK_DISABLED='1'" in start_script
    assert "browser_url starter skipped by sandbox network policy" in start_script
    assert (
        "google-chrome-stable google-chrome chromium chromium-browser firefox xdg-open"
        not in start_script
    )


def test_managed_ubuntu_browser_url_starter_runs_after_approved_create(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(
        _create_spec(
            _template(),
            startup={"starter": "browser_url", "browser_url": "https://example.com"},
            network_approved=True,
        )
    )
    started = provider.start(instance)
    start_script = next(script for script in fake.guest_scripts if "starter-browser.log" in script)

    assert ensured.ok is True
    assert started.state == "ready"
    assert started.opaque_state["network_disabled"] is False
    assert "RUMI_NETWORK_DISABLED='0'" in start_script
    assert "browser_url starter skipped by sandbox network policy" not in start_script
    assert "google-chrome-stable google-chrome chromium chromium-browser firefox" in start_script
    assert (
        '"$BROWSER_BIN" --no-sandbox --no-first-run --disable-dev-shm-usage --user-data-dir='
        in start_script
    )


def test_managed_ubuntu_port_exposure_respects_network_policy(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(_create_spec(_template()))
    started = provider.start(instance)
    agent = provider.connect_agent(started)

    assert ensured.ok is True
    with pytest.raises(SandboxContractError) as excinfo:
        agent.expose_port(started.sandbox_id, {"port": 3000, "protocol": "http"})
    approved = agent.expose_port(
        started.sandbox_id,
        {"port": 3000, "protocol": "http", "_network_policy_approved": True},
    )
    assert getattr(excinfo.value, "code", "") == "SANDBOX_NETWORK_DENIED"
    assert approved["ok"] is False
    assert approved["code"] == "SANDBOX_PORT_FORWARD_UNAVAILABLE"
    assert approved["target_url"] == "http://127.0.0.1:3000"
    assert approved["host_reachable"] is False
    assert approved["forwarding"] == "unavailable"


def test_managed_ubuntu_port_exposure_requires_listening_guest_service(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    fake.port_probe_returncode = 1
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(
        _create_spec(_template(network_mode="host_shared", network_approval_required=False))
    )
    started = provider.start(instance)
    agent = provider.connect_agent(started)
    exposed = agent.expose_port(started.sandbox_id, {"port": 3000, "protocol": "http"})

    assert ensured.ok is True
    assert exposed["ok"] is False
    assert exposed["code"] == "SANDBOX_PORTS_NOT_READY"
    assert exposed["status_code"] == 503
    assert exposed["details"]["stderr"] == "connection refused"


def test_managed_ubuntu_desktop_provisioning_installs_declared_apps_and_mcp(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)
    provisioning = {
        "packages": [{"name": "google-chrome-stable"}, {"name": "python"}],
        "apps": ["xterm", "code-editor"],
        "mcp_servers": ["playwright"],
    }

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(_create_spec(_template(), provisioning=provisioning))
    started = provider.start(instance)
    provision_script = next(script for script in fake.guest_scripts if "PROVISION_MARKER" in script)

    assert ensured.ok is True
    assert started.state == "ready"
    assert "google-chrome-stable" in provision_script
    assert "python3" in provision_script
    assert "python3-pip" in provision_script
    assert "xterm" in provision_script
    assert "code-editor" not in provision_script
    assert f"{started.opaque_state['guest_workspace']}/.rumi/mcp_servers.txt" in provision_script
    assert "@playwright/mcp" in provision_script
    assert "$RUMI_SUDO apt-get install -y" in provision_script
    assert "$RUMI_SUDO npm install -g @playwright/mcp" in provision_script
    assert "sudo apt-get" not in provision_script
    assert "sudo npm" not in provision_script


def test_managed_ubuntu_template_packages_are_guest_provisioned(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)
    template = _template(
        packages=(
            PackageSpec(name="node", version="20+", source="guest"),
            PackageSpec(name="python", version="3.11+", source="guest"),
            PackageSpec(name="not-a-known-app", source="guest"),
        )
    )

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(_create_spec(template))
    started = provider.start(instance)
    provision_script = next(script for script in fake.guest_scripts if "PROVISION_MARKER" in script)

    assert ensured.ok is True
    assert started.state == "ready"
    assert "nodejs" in provision_script
    assert "npm" in provision_script
    assert "python3" in provision_script
    assert "python3-pip" in provision_script
    assert "not-a-known-app" not in provision_script


def test_managed_ubuntu_browser_template_uses_launchable_chrome_package(monkeypatch) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(required_capabilities=MANAGED_UBUNTU_CAPABILITIES)
    template = _template(
        packages=(PackageSpec(name="chromium", version="managed", source="guest"),)
    )

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(_create_spec(template))
    started = provider.start(instance)
    provision_script = next(script for script in fake.guest_scripts if "PROVISION_MARKER" in script)
    install_line = next(
        line
        for line in provision_script.splitlines()
        if "apt-get install -y $RUMI_APT_PACKAGES" in line
    )

    assert ensured.ok is True
    assert started.state == "ready"
    assert "google-chrome-stable" in provision_script
    assert "chromium-browser" not in install_line
    assert "chromium-browser apt package is not a usable fallback" in provision_script


def test_managed_ubuntu_seeds_trusted_workspace_read_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    workspace_root = tmp_path / "workspace"
    (workspace_root / "src").mkdir(parents=True)
    app_path = workspace_root / "src" / "app.py"
    app_path.write_text("print('seeded')\n", encoding="utf-8")
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(
        required_capabilities=frozenset({"sandbox.exec", "sandbox.files"})
    )

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(
        _create_spec(
            _template(desktop=False),
            workspace_binding=WorkspaceBinding(
                workspace_id="workspace-1", mode="read_only", root=str(workspace_root)
            ),
        )
    )
    started = provider.start(instance)
    seed_script, payload = _workspace_seed_call(fake, "read_only")

    assert ensured.ok is True
    assert started.state == "ready"
    assert (
        f"find {started.opaque_state['guest_workspace']} -mindepth 1 -maxdepth 1 ! -name .rumi"
        in seed_script
    )
    assert "& ~0o222" in seed_script
    assert _workspace_seed_member(payload, "src/app.py") == app_path.read_bytes()


def test_managed_ubuntu_seeds_trusted_workspace_overlay(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu.platform.system",
        lambda: "Darwin",
    )
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    package_path = workspace_root / "package.json"
    package_path.write_text('{"scripts":{"test":"true"}}\n', encoding="utf-8")
    fake = FakeManagedUbuntuCli(mode="lima", runtime_name="rumi-managed-runtime")
    provider = MacLimaProvider(command_path="/usr/bin/limactl", runner=fake)
    requirements = RuntimeRequirements(
        required_capabilities=frozenset({"sandbox.exec", "sandbox.files"})
    )

    ensured = provider.ensure(
        EnsureRuntimeRequest(provider_id="mac_lima", requirements=requirements), NullProgressSink()
    )
    instance = provider.create(
        _create_spec(
            _template(desktop=False),
            workspace_binding=WorkspaceBinding(
                workspace_id="workspace-1", mode="overlay", root=str(workspace_root)
            ),
        )
    )
    started = provider.start(instance)
    seed_script, payload = _workspace_seed_call(fake, "overlay")

    assert ensured.ok is True
    assert started.state == "ready"
    assert "RUMI_WORKSPACE_SEED_MODE=overlay" in seed_script
    assert _workspace_seed_member(payload, "package.json") == package_path.read_bytes()


def test_default_sandbox_api_registers_cross_platform_runtime_providers(monkeypatch) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    monkeypatch.delenv("RUMI_CLOUDFLARE_SANDBOX_BRIDGE_URL", raising=False)
    monkeypatch.delenv("RUMI_CLOUDFLARE_SANDBOX_API_KEY", raising=False)
    service = api._SandboxApiService()
    provider_ids = set(service.provider_registry.provider_ids())
    cloudflare_status = service.provider_registry.doctor("cloudflare_sandbox_bridge")

    assert {"linux_native", "mac_lima", "windows_wsl", "docker", "cloudflare_sandbox_bridge"} <= provider_ids
    assert cloudflare_status.ready is False
    api._reset_service_for_tests(None)
