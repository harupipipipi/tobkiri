from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_default_di_registers_desktop_capability_handler():
    from core_runtime.di_container import get_container, reset_container

    reset_container()
    try:
        container = get_container()
        assert container.has("desktop_capability_handler")
        assert container.get("desktop_capability_handler").__class__.__name__ == "DesktopCapabilityHandler"
    finally:
        reset_container()


def test_permissions_config_maps_core_desktop_capability():
    config = json.loads((ROOT / "core_runtime" / "config" / "permissions.json").read_text(encoding="utf-8"))
    assert config["core_function_handlers"]["core_desktop_capability"] == "desktop_capability_handler"


def test_core_desktop_execute_manifest_uses_dot_permission_id():
    manifest = json.loads(
        (
            ROOT
            / "core_runtime"
            / "core_pack"
            / "core_desktop_capability"
            / "functions"
            / "execute"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["requires"] == ["desktop_app.execute"]
    assert manifest["grant_config"]["permission_id"] == "desktop_app.execute"


def test_runtime_registers_desktop_launch_handlers():
    from core_runtime.kernel_handlers_runtime import KernelRuntimeHandlersMixin

    class Stub(KernelRuntimeHandlersMixin):
        pass

    handlers = Stub()._register_runtime_handlers()
    assert handlers["kernel:desktop.launch"].__name__ == "_h_desktop_launch"
    assert handlers["kernel:desktop.stop"].__name__ == "_h_desktop_stop"


def test_runtime_desktop_launch_requires_granted_principal(monkeypatch):
    from core_runtime import capability_grant_manager
    from core_runtime.kernel_handlers_runtime import KernelRuntimeHandlersMixin

    class Stub(KernelRuntimeHandlersMixin):
        pass

    class FakeGrantManager:
        def check(self, principal_id, permission_id):
            assert principal_id == "malicious"
            assert permission_id == "desktop_app.execute"
            return SimpleNamespace(
                allowed=False,
                reason="desktop_app.execute denied",
                config={},
            )

    monkeypatch.setattr(
        capability_grant_manager,
        "get_capability_grant_manager",
        lambda: FakeGrantManager(),
    )

    with mock.patch("core_runtime.desktop_app_manager.DesktopAppManager.launch_app") as direct_launch:
        result = Stub()._h_desktop_launch(
            {"pack_id": "victim"},
            {"_principal_id": "malicious"},
        )

    assert result == {"success": False, "error": "desktop_app.execute denied"}
    direct_launch.assert_not_called()


def test_runtime_desktop_launch_uses_scoped_capability_path(monkeypatch):
    from core_runtime import capability_grant_manager, di_container
    from core_runtime.kernel_handlers_runtime import KernelRuntimeHandlersMixin

    class Stub(KernelRuntimeHandlersMixin):
        pass

    class FakeGrantManager:
        def check(self, principal_id, permission_id):
            assert principal_id == "defaultspack"
            assert permission_id == "desktop_app.execute"
            return SimpleNamespace(
                allowed=True,
                reason="Granted",
                config={"allowed_packs": ["defaultspack"]},
            )

    class FakeDesktopCapabilityHandler:
        def __init__(self):
            self.calls = []

        def handle_execute(self, principal_id, args, grant_config):
            self.calls.append((principal_id, args, grant_config))
            return {
                "token": "scoped-desktop-token",
                "app": {"success": True, "status": "launched", "pid": 123},
            }

    handler = FakeDesktopCapabilityHandler()

    class FakeContainer:
        def get_or_none(self, name):
            assert name == "desktop_capability_handler"
            return handler

    monkeypatch.setattr(
        capability_grant_manager,
        "get_capability_grant_manager",
        lambda: FakeGrantManager(),
    )
    monkeypatch.setattr(di_container, "get_container", lambda: FakeContainer())

    with mock.patch("core_runtime.desktop_app_manager.DesktopAppManager.launch_app") as direct_launch:
        result = Stub()._h_desktop_launch(
            {"pack_id": "defaultspack"},
            {"_principal_id": "defaultspack"},
        )

    assert result == {"success": True, "data": {"success": True, "status": "launched", "pid": 123}}
    assert handler.calls == [
        (
            "defaultspack",
            {"pack_id": "defaultspack", "action": "launch"},
            {"allowed_packs": ["defaultspack"]},
        )
    ]
    assert "token" not in result["data"]
    direct_launch.assert_not_called()


def test_defaultspack_ecosystem_registers_desktop_app_metadata():
    """The v4 authority artifacts replace legacy ecosystem metadata."""
    manifest = json.loads(
        (DEFAULTSPACK_ROOT / "pack.v4.json").read_text(encoding="utf-8")
    )
    artifact_index = json.loads(
        (DEFAULTSPACK_ROOT / "artifact-index.v4.json").read_text(encoding="utf-8")
    )

    assert manifest["pack_api_version"] == "io.tobkiri.pack.v4"
    assert manifest["pack"]["id"] == "defaultspack"
    assert manifest["pack"]["artifact_digest"] == artifact_index["artifact_set_digest"]
    assert artifact_index["pack_id"] == manifest["pack"]["id"]
    assert artifact_index["integrity_seal"]["algorithm"] == "sha256-canonical-v1"
    assert {
        artifact["path"] for artifact in artifact_index["artifacts"]
    } == {
        "pack.v4.json",
        "contracts.v4.json",
        "executables.v4.json",
        "runtime/conversation.py",
    }
    executable_sidecars = [
        artifact
        for artifact in artifact_index["artifacts"]
        if artifact["path"] == "executables.v4.json"
    ]
    assert len(executable_sidecars) == 1
    assert executable_sidecars[0]["role"] == "sidecar"
    assert executable_sidecars[0]["digest"] == (
        "sha256:"
        + hashlib.sha256(
            (DEFAULTSPACK_ROOT / "executables.v4.json").read_bytes()
        ).hexdigest()
    )
    assert not (DEFAULTSPACK_ROOT / "ecosystem.json").exists()


def test_desktop_capability_can_launch_registered_pack_with_issued_token():
    from core_runtime.desktop_capability import DesktopCapabilityHandler

    handler = DesktopCapabilityHandler()
    with mock.patch("core_runtime.desktop_app_manager.DesktopAppManager.launch_app_with_env") as mock_launch:
        mock_launch.return_value = {"success": True, "status": "launched", "pid": 123}
        result = handler.handle_execute(
            principal_id="defaultspack",
            args={"pack_id": "defaultspack", "action": "launch"},
            grant_config={"allowed_packs": ["defaultspack"], "max_token_lifetime": 300},
        )

    assert result["expires_in"] == 300
    assert result["app"]["status"] == "launched"
    mock_launch.assert_called_once()
    assert mock_launch.call_args.kwargs["api_token"] == result["token"]


def test_desktop_capability_uses_runtime_port_from_env(monkeypatch):
    from core_runtime.desktop_capability import DesktopCapabilityHandler

    monkeypatch.setenv("RUMI_PORT", "8767")
    handler = DesktopCapabilityHandler()

    result = handler.handle_execute(
        principal_id="defaultspack",
        args={"pack_id": "defaultspack"},
        grant_config={"allowed_packs": ["defaultspack"]},
    )

    assert result["port"] == 8767


def test_desktop_capability_invalid_runtime_port_uses_grant_fallback(monkeypatch):
    from core_runtime.desktop_capability import DesktopCapabilityHandler

    monkeypatch.setenv("RUMI_PORT", "not-a-port")
    handler = DesktopCapabilityHandler()

    result = handler.handle_execute(
        principal_id="defaultspack",
        args={"pack_id": "defaultspack"},
        grant_config={"allowed_packs": ["defaultspack"], "port": 8770},
    )

    assert result["port"] == 8770


def test_desktop_capability_rejects_invalid_target_pack_id():
    from core_runtime.desktop_capability import DesktopCapabilityHandler

    handler = DesktopCapabilityHandler()
    with mock.patch("core_runtime.desktop_app_manager.DesktopAppManager.launch_app_with_env") as mock_launch:
        result = handler.handle_execute(
            principal_id="defaultspack",
            args={"pack_id": "../user_data/evil", "action": "launch"},
            grant_config={"allowed_packs": ["*"]},
        )

    assert result == {"error": "Invalid pack_id for desktop app execution: ../user_data/evil"}
    mock_launch.assert_not_called()

def test_desktop_capability_denies_empty_allowed_packs():
    from core_runtime.desktop_capability import DesktopCapabilityHandler

    handler = DesktopCapabilityHandler()
    result = handler.handle_execute(
        principal_id="defaultspack",
        args={"pack_id": "defaultspack", "action": "launch"},
        grant_config={"allowed_packs": []},
    )

    assert result == {"error": "Pack not allowed for desktop app execution: defaultspack"}


def test_desktop_capability_allows_only_explicit_pack_list():
    from core_runtime.desktop_capability import DesktopCapabilityHandler

    handler = DesktopCapabilityHandler()

    with mock.patch("core_runtime.desktop_app_manager.DesktopAppManager.launch_app_with_env") as mock_launch:
        mock_launch.return_value = {"success": True, "status": "launched", "pid": 123}
        allowed = handler.handle_execute(
            principal_id="defaultspack",
            args={"pack_id": "defaultspack", "action": "launch"},
            grant_config={"allowed_packs": ["defaultspack"]},
        )
        denied = handler.handle_execute(
            principal_id="otherpack",
            args={"pack_id": "otherpack", "action": "launch"},
            grant_config={"allowed_packs": ["defaultspack"]},
        )

    assert allowed["app"]["success"] is True
    assert denied == {"error": "Pack not allowed for desktop app execution: otherpack"}


def test_desktop_capability_explicit_wildcard_allows_any_pack():
    from core_runtime.desktop_capability import DesktopCapabilityHandler

    handler = DesktopCapabilityHandler()
    result = handler.handle_execute(
        principal_id="otherpack",
        args={"pack_id": "otherpack", "action": "token"},
        grant_config={"allowed_packs": ["*"]},
    )

    assert result["expires_in"] == DesktopCapabilityHandler.DEFAULT_TOKEN_LIFETIME
    assert handler.verify_token(result["token"])["pack_id"] == "otherpack"


def test_desktop_capability_launch_then_stop_uses_same_manager(tmp_path):
    from core_runtime.desktop_app_manager import DesktopAppManager
    from core_runtime.desktop_capability import DesktopCapabilityHandler

    repo_dir = tmp_path / "repo"
    apps_dir = repo_dir / "user_data" / "apps"
    apps_dir.mkdir(parents=True)
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (apps_dir / "defaultspack.json").write_text(
        json.dumps(
            {
                "pack_id": "defaultspack",
                "command": (
                    f"{shlex.quote(sys.executable)} -c "
                    "\"import time; time.sleep(60)\""
                ),
                "pack_dir": str(pack_dir),
                "requires_api_token": True,
                "env": {},
                "working_dir": str(pack_dir),
            }
        ),
        encoding="utf-8",
    )

    manager = DesktopAppManager(repo_dir=str(repo_dir))
    handler = DesktopCapabilityHandler(desktop_app_manager=manager)

    launch = handler.handle_execute(
        principal_id="defaultspack",
        args={"pack_id": "defaultspack", "action": "launch"},
        grant_config={"allowed_packs": ["defaultspack"]},
    )
    assert launch["app"]["success"] is True

    stop = handler.handle_execute(
        principal_id="defaultspack",
        args={"pack_id": "defaultspack", "action": "stop"},
        grant_config={"allowed_packs": ["defaultspack"]},
    )
    assert stop["app"] == {"success": True, "status": "stopped"}


def test_desktop_direct_launch_uses_current_python_and_hidden_console(tmp_path):
    from core_runtime import desktop_app_manager as manager_module
    from core_runtime.desktop_app_manager import DesktopAppManager

    manager = DesktopAppManager(repo_dir=str(tmp_path / "repo"))
    captured = {}

    class FakeProcess:
        pid = 123

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    with mock.patch("core_runtime.desktop_app_manager.subprocess.Popen", side_effect=fake_popen):
        result = manager._launch_direct(
            "defaultspack",
            "python defaultspack/desktop_app.py",
            str(DEFAULTSPACK_ROOT),
            {"PATH": ""},
        )

    assert result["success"] is True
    assert captured["args"][0] == manager_module._runtime_python_for_app()
    if sys.platform == "win32":
        assert captured["kwargs"]["creationflags"] == manager_module.subprocess.CREATE_NO_WINDOW


def test_desktop_launch_sets_default_log_dir_from_user_data(tmp_path):
    from core_runtime.desktop_app_manager import DesktopAppManager

    repo_dir = tmp_path / "repo"
    apps_dir = repo_dir / "user_data" / "apps"
    apps_dir.mkdir(parents=True)
    pack_dir = tmp_path / "defaultspack"
    pack_dir.mkdir()
    user_data = tmp_path / "app-data" / "user_data"
    user_data.mkdir(parents=True)
    (apps_dir / "defaultspack.json").write_text(
        json.dumps(
            {
                "pack_id": "defaultspack",
                "command": "python defaultspack/desktop_app.py",
                "pack_dir": str(pack_dir),
                "requires_api_token": True,
                "env": {},
                "working_dir": str(pack_dir),
            }
        ),
        encoding="utf-8",
    )
    manager = DesktopAppManager(repo_dir=str(repo_dir))
    captured = {}

    class FakeProcess:
        pid = 123

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    with mock.patch.dict(os.environ, {"RUMI_USER_DATA": str(user_data)}, clear=True):
        with mock.patch("core_runtime.desktop_app_manager.subprocess.Popen", side_effect=fake_popen):
            result = manager.launch_app_with_env("defaultspack", api_token="token")

    assert result["success"] is True
    assert captured["kwargs"]["env"]["RUMI_LOG_DIR"] == str(user_data.parent / "logs")


@pytest.mark.parametrize("action", ["stop", "status"])
def test_desktop_capability_delegates_non_launch_actions(action):
    from core_runtime.desktop_capability import DesktopCapabilityHandler

    handler = DesktopCapabilityHandler()
    if action == "stop":
        target = "core_runtime.desktop_app_manager.DesktopAppManager.stop_app"
        expected_key = "status"
        expected_value = "stopped"
        return_value = {"success": True, "status": "stopped"}
    else:
        target = "core_runtime.desktop_app_manager.DesktopAppManager.list_registered_apps"
        expected_key = "registered_apps"
        expected_value = []
        return_value = []

    with mock.patch(target, return_value=return_value) as delegated:
        result = handler.handle_execute(
            principal_id="defaultspack",
            args={"pack_id": "defaultspack", "action": action},
            grant_config={"allowed_packs": ["defaultspack"]},
        )

    assert result["app"][expected_key] == expected_value
    delegated.assert_called_once()
