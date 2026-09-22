from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ecosystem.defaultspack.backend.sandbox.isolation import (
    supervisor as supervisor_module,
)
from ecosystem.defaultspack.backend.sandbox.isolation.lima_runtime import (
    LIMA_GUEST_WORKSPACE_ROOT,
    LIMA_GUEST_PACK_DATA_ROOT,
    build_guest_bwrap_argv,
    lima_instance_payload,
    resolve_attested_lima_runtime,
    validate_lima_instance_config,
)
from ecosystem.defaultspack.backend.sandbox.isolation.supervisor import (
    ManagedSandboxSupervisor,
    _guest_resource_limited_argv,
    _pack_data_migration_commit_script,
    _safe_guest_name,
    _validated_profile_context,
)


def _hardened_payload() -> dict[str, object]:
    return {
        "name": "rumi-managed-runtime",
        "status": "Running",
        "vmType": "vz",
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


def test_lima_attestation_rejects_host_bridges() -> None:
    payload = _hardened_payload()
    assert validate_lima_instance_config(payload) is None

    config = payload["config"]
    assert isinstance(config, dict)
    config["mounts"] = [{"location": "/Users"}]
    assert "mounts" in str(validate_lima_instance_config(payload))
    config["mounts"] = []
    config["propagateProxyEnv"] = True
    assert "proxy" in str(validate_lima_instance_config(payload))
    config["propagateProxyEnv"] = False
    config["portForwards"] = [
        {"guestPortRange": [22, 22], "ignore": False},
        {"guestPortRange": [1, 65535], "ignore": True},
    ]
    assert "port forwarding" in str(validate_lima_instance_config(payload))


def test_lima_attestation_rejects_missing_mount_measurement() -> None:
    payload = _hardened_payload()
    config = payload["config"]
    assert isinstance(config, dict)
    config.pop("mounts")

    assert "mounts" in str(validate_lima_instance_config(payload))


def test_guest_resource_limits_include_wall_clock_tree_kill() -> None:
    argv = _guest_resource_limited_argv(
        ("bwrap", "--", "python3"),
        timeout=2.5,
        memory_mb=512,
        pids=128,
    )

    assert argv[:5] == (
        "timeout",
        "--signal=TERM",
        "--kill-after=1s",
        "2.5s",
        "prlimit",
    )


def test_lima_attestation_rejects_network_attachments_and_missing_measurement() -> None:
    payload = _hardened_payload()
    config = payload["config"]
    assert isinstance(config, dict)
    config["networks"] = [{"lima": "shared"}]
    assert "network attachments" in str(validate_lima_instance_config(payload))

    config.pop("networks")
    assert "network attachments" in str(validate_lima_instance_config(payload))


def test_lima_payload_resolves_omitted_isolation_fields_from_owned_instance_yaml(
    tmp_path: Path,
) -> None:
    instance_dir = tmp_path / "rumi-managed-runtime"
    instance_dir.mkdir()
    (instance_dir / "lima.yaml").write_text(
        "mounts: []\nnetworks: []\n",
        encoding="utf-8",
    )
    payload = _hardened_payload()
    config = payload["config"]
    assert isinstance(config, dict)
    config.pop("mounts")
    config.pop("networks")
    payload["dir"] = str(instance_dir)

    resolved = lima_instance_payload(
        "limactl",
        "rumi-managed-runtime",
        runner=lambda *_args: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    assert resolved["config"]["mounts"] == []
    assert resolved["config"]["networks"] == []


def test_guest_bwrap_masks_backing_workspaces_and_network() -> None:
    source = f"{LIMA_GUEST_WORKSPACE_ROOT}/.rumi-sbx"
    argv = build_guest_bwrap_argv(
        workspace=source,
        cwd="/workspace",
        argv=("python3", "main.py"),
        env={"HOME": "/home"},
        network_enabled=False,
    )

    assert "--unshare-user" in argv
    assert "--unshare-pid" in argv
    assert "--unshare-net" in argv
    bind_index = argv.index(source)
    mask_index = argv.index(LIMA_GUEST_WORKSPACE_ROOT, bind_index + 1)
    assert bind_index < mask_index
    assert argv[argv.index("--bind") + 2] == "/workspace"
    assert LIMA_GUEST_PACK_DATA_ROOT in argv
    pack_mask = argv.index(LIMA_GUEST_PACK_DATA_ROOT)
    assert argv[pack_mask - 1] == "--tmpfs"


def test_guest_bwrap_rejects_pack_data_path_traversal() -> None:
    with pytest.raises(ValueError, match="Pack data"):
        build_guest_bwrap_argv(
            workspace=f"{LIMA_GUEST_WORKSPACE_ROOT}/.rumi-sbx",
            cwd="/workspace",
            argv=("true",),
            env={},
            network_enabled=False,
            data_dir=f"{LIMA_GUEST_PACK_DATA_ROOT}/pack/../../workspaces",
        )
    with pytest.raises(ValueError, match="sandbox paths"):
        build_guest_bwrap_argv(
            workspace=f"{LIMA_GUEST_WORKSPACE_ROOT}/.rumi-sbx",
            cwd="/workspace/../etc",
            argv=("true",),
            env={},
            network_enabled=False,
        )


def test_guest_pack_data_names_resist_normalization_and_prefix_collisions() -> None:
    shared_prefix = "pack-" + ("x" * 140)
    first = _safe_guest_name(shared_prefix + "/one")
    second = _safe_guest_name(shared_prefix + "/two")

    assert first != second
    assert len(first) <= 128
    assert len(second) <= 128
    assert first.split("--", 1)[0] == second.split("--", 1)[0]


def test_lima_profile_context_is_fail_closed() -> None:
    assert _validated_profile_context("work-profile") == "work-profile"
    with pytest.raises(ValueError, match="profile context"):
        _validated_profile_context("../escape")


def test_pack_data_migration_commit_is_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    destination = data_dir / "packs" / "sample_pack"
    destination.mkdir(parents=True)
    (destination / "state.json").write_text("current", encoding="utf-8")
    marker = data_dir / ".migration-complete"
    marker.write_text("done", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "state.json").write_text("legacy", encoding="utf-8")

    result = subprocess.run(
        [
            "sh",
            "-c",
            _pack_data_migration_commit_script(),
            "migration-test",
            str(data_dir),
            str(destination),
            str(staging),
            str(tmp_path / "backup"),
            str(marker),
            str(data_dir / ".migration-lock"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert (destination / "state.json").read_text(encoding="utf-8") == "current"
    assert not staging.exists()


def test_pack_data_migration_rolls_back_when_marker_commit_fails(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    destination = data_dir / "packs" / "sample_pack"
    destination.mkdir(parents=True)
    (destination / "state.json").write_text("current", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "state.json").write_text("legacy", encoding="utf-8")
    invalid_marker_parent = tmp_path / "not-a-directory"
    invalid_marker_parent.write_text("blocked", encoding="utf-8")

    result = subprocess.run(
        [
            "sh",
            "-c",
            _pack_data_migration_commit_script(),
            "migration-test",
            str(data_dir),
            str(destination),
            str(staging),
            str(tmp_path / "backup"),
            str(invalid_marker_parent / "marker"),
            str(data_dir / ".migration-lock"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert (destination / "state.json").read_text(encoding="utf-8") == "current"
    assert not (tmp_path / "backup").exists()
    assert not (data_dir / ".migration-lock").exists()


def test_lima_capability_injects_child_process_policy_only_into_pack_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function_dir = tmp_path / "function"
    function_dir.mkdir()
    main_py = function_dir / "main.py"
    main_py.write_text(
        "def run(context, args):\n    return {'safe': True}\n",
        encoding="utf-8",
    )
    runner_path = Path(__file__).resolve().parents[1] / "core_runtime" / "function_runner.py"
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        supervisor_module,
        "resolve_attested_lima_runtime",
        lambda: ("/opt/homebrew/bin/limactl", "rumi-managed-runtime"),
    )
    monkeypatch.setattr(
        supervisor_module,
        "_lima_import_workspace",
        lambda **_kwargs: SimpleNamespace(returncode=0, stderr=b""),
    )
    monkeypatch.setattr(
        supervisor_module,
        "_lima_remove_workspace",
        lambda *_args, **_kwargs: None,
    )

    def run(command, **_kwargs):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='{"safe": true}\n',
            stderr="",
        )

    monkeypatch.setattr(supervisor_module, "_run_bounded_process", run)

    result = ManagedSandboxSupervisor()._execute_capability_lima(
        {
            "pack_id": "third_party_pack",
            "function_id": "safe_operation",
            "function_dir": str(function_dir),
            "main_py_path": str(main_py),
            "entrypoint": "main.py:run",
            "runner_path": str(runner_path),
            "timeout_seconds": 10,
        }
    )

    assert result["success"] is True
    assert len(commands) == 1
    command = commands[0]
    policy_index = command.index("RUMI_SANDBOX_DENY_CHILD_PROCESS")
    assert command[policy_index - 1] == "--setenv"
    assert command[policy_index + 1] == "1"
    separator = command.index("--", policy_index)
    assert command[separator + 1 : separator + 3] == (
        "python3",
        "/workspace/function_runner.py",
    )


@pytest.mark.skipif(
    os.environ.get("RUMI_RUN_LIMA_INTEGRATION") != "1",
    reason="real Lima sandbox integration is opt-in",
)
def test_real_macos_lima_boundary_blocks_host_siblings_and_network(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    limactl, instance = resolve_attested_lima_runtime()
    sibling_secret_dir = f"{LIMA_GUEST_PACK_DATA_ROOT}/other_pack"
    sibling_secret = f"{sibling_secret_dir}/secret.txt"
    subprocess.run(
        [
            limactl,
            "shell",
            instance,
            "--",
            "sh",
            "-c",
            f"mkdir -p {sibling_secret_dir} && printf cross-pack-secret > {sibling_secret}",
        ],
        check=True,
        timeout=10,
    )
    request.addfinalizer(
        lambda: subprocess.run(
            [limactl, "shell", instance, "--", "rm", "-rf", sibling_secret_dir],
            check=False,
            timeout=10,
        )
    )
    function_dir = tmp_path / "function"
    function_dir.mkdir()
    main_py = function_dir / "main.py"
    main_py.write_text(
        "import os\n"
        "import socket\n"
        "\n"
        "def run(context, args):\n"
        "    try:\n"
        "        socket.create_connection(('1.1.1.1', 53), 0.25).close()\n"
        "        network = True\n"
        "    except OSError:\n"
        "        network = False\n"
        "    return {\n"
        "        'host_visible': os.path.exists('/Users'),\n"
        f"        'sibling_workspaces': os.listdir('{LIMA_GUEST_WORKSPACE_ROOT}'),\n"
        f"        'sibling_pack_secret': os.path.exists('{sibling_secret}'),\n"
        "        'network': network,\n"
        "    }\n",
        encoding="utf-8",
    )
    runner_path = Path(__file__).resolve().parents[1] / "core_runtime" / "function_runner.py"

    result = ManagedSandboxSupervisor().execute_capability(
        {
            "pack_id": "third_party_pack",
            "function_id": "boundary_probe",
            "function_dir": str(function_dir),
            "main_py_path": str(main_py),
            "entrypoint": "main.py:run",
            "runner_path": str(runner_path),
            "timeout_seconds": 10,
        }
    )

    assert result["success"] is True
    assert result["execution_boundary"] == "managed_sandbox"
    assert result["output"] == {
        "host_visible": False,
        "sibling_workspaces": [],
        "sibling_pack_secret": False,
        "network": False,
    }

    main_py.write_text(
        "import subprocess\n"
        "\n"
        "def run(context, args):\n"
        "    subprocess.run(['/bin/sh', '-c', 'exit 0'], check=True)\n"
        "    return {'unexpected': True}\n",
        encoding="utf-8",
    )
    child_process = ManagedSandboxSupervisor().execute_capability(
        {
            "pack_id": "third_party_pack",
            "function_id": "child_process_probe",
            "function_dir": str(function_dir),
            "main_py_path": str(main_py),
            "entrypoint": "main.py:run",
            "runner_path": str(runner_path),
            "timeout_seconds": 10,
        }
    )
    assert child_process["success"] is False
    assert child_process["execution_boundary"] == "managed_sandbox"
    assert child_process["error_type"] == "sandbox_policy_denied"
    assert child_process["error"] == (
        "Sandbox Pack functions cannot create child processes"
    )

    coding_workspace = tmp_path / "coding"
    coding_workspace.mkdir()
    coding = ManagedSandboxSupervisor().execute_coding_terminal(
        {
            "workspace_root": str(coding_workspace),
            "argv": [
                "python3",
                "-c",
                "import json,pathlib;"
                "pathlib.Path('proof.json').write_text("
                "json.dumps({"
                "'host':pathlib.Path('/Users').exists(),"
                f"'sibling_pack_secret':pathlib.Path('{sibling_secret}').exists()"
                "}))",
            ],
            "timeout_seconds": 10,
        }
    )
    assert coding["success"] is True
    assert json.loads((coding_workspace / "proof.json").read_text(encoding="utf-8")) == {
        "host": False,
        "sibling_pack_secret": False,
    }

    descendant_token = f"rumi-timeout-descendant-{uuid.uuid4().hex}"
    timeout_result = ManagedSandboxSupervisor().execute_coding_terminal(
        {
            "workspace_root": str(coding_workspace),
            "argv": [
                "python3",
                "-c",
                "import subprocess,time;"
                "subprocess.Popen(["
                "'python3','-c','import time;time.sleep(30)',"
                f"'{descendant_token}'"
                "]);"
                "time.sleep(30)",
            ],
            "timeout_seconds": 1,
            "export_workspace": False,
        }
    )
    assert timeout_result["success"] is False
    assert timeout_result["timed_out"] is True
    assert timeout_result["provider_id"] == "lima_ubuntu"

    time.sleep(1.5)
    guest_processes = subprocess.run(
        [limactl, "shell", instance, "--", "ps", "-eo", "args="],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert descendant_token not in guest_processes.stdout
