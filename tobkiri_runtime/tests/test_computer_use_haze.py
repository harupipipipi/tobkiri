from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
RUMI_DEFAULT_TOOLS_ROOT = ROOT / "ecosystem" / "rumi_default_tools_pack"
RUMI_DEFAULT_TOOLS_FUNCTIONS = ROOT / "ecosystem" / "rumi_default_tools_pack" / "functions"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))


@contextmanager
def _default_tools_function_imports():
    isolated_roots = ("browser_computer", "browser_use", "computer_use", "functions")
    saved_path = list(sys.path)
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name in isolated_roots or any(name.startswith(f"{root}.") for root in isolated_roots)
    }
    sys.path.insert(0, str(RUMI_DEFAULT_TOOLS_FUNCTIONS))
    try:
        yield
    finally:
        sys.path[:] = saved_path
        for name in list(sys.modules):
            if name in isolated_roots or any(name.startswith(f"{root}.") for root in isolated_roots):
                if name in saved_modules:
                    sys.modules[name] = saved_modules[name]
                else:
                    sys.modules.pop(name, None)


def test_edge_haze_manager_noops_off_macos(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.computer.mac import edge_haze
    from ecosystem.rumi_default_tools_pack.domain.computer.mac.edge_haze import (
        ComputerUseEdgeHazeManager,
        EdgeHazeSettings,
    )

    monkeypatch.setattr(edge_haze.platform, "system", lambda: "Linux")
    manager = ComputerUseEdgeHazeManager(
        pack_root=tmp_path,
        source_path=tmp_path / "EdgeHaze.swift",
        binary_path=tmp_path / "edge_haze",
        settings=EdgeHazeSettings(enabled=True),
    )

    assert manager.start(action="computer.click", payload={"x": 1, "y": 2}) is False


def test_edge_haze_manager_compiles_starts_and_stops_on_macos(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.computer.mac import edge_haze
    from ecosystem.rumi_default_tools_pack.domain.computer.mac.edge_haze import (
        ComputerUseEdgeHazeManager,
        EdgeHazeSettings,
    )

    source = tmp_path / "EdgeHaze.swift"
    source.write_text("print(\"haze\")\n", encoding="utf-8")
    binary = tmp_path / "helpers" / "edge_haze"
    events: list[str] = []

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")

        def kill(self):
            events.append("kill")

    def fake_run(args, capture_output=False, timeout=None, check=False):
        events.append("compile")
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("binary", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    def fake_popen(args, **kwargs):
        events.append("start")
        return FakeProcess()

    monkeypatch.setattr(edge_haze.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(edge_haze.shutil, "which", lambda name: "/usr/bin/swiftc" if name == "swiftc" else None)
    monkeypatch.setattr(edge_haze.subprocess, "run", fake_run)
    monkeypatch.setattr(edge_haze.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("RUMI_COMPUTER_USE_HAZE", "1")

    manager = ComputerUseEdgeHazeManager(
        pack_root=tmp_path,
        source_path=source,
        binary_path=binary,
        settings=EdgeHazeSettings(enabled=True),
    )

    assert manager.start(action="computer.click", payload={"x": 1, "y": 2}) is True
    manager.stop()

    assert events == ["compile", "start", "terminate", "wait:1"]


def test_edge_haze_reuses_process_for_same_sequence_until_sequence_ends(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.computer.mac import edge_haze
    from ecosystem.rumi_default_tools_pack.domain.computer.mac.edge_haze import (
        ComputerUseEdgeHazeManager,
        EdgeHazeSettings,
    )

    source = tmp_path / "EdgeHaze.swift"
    source.write_text("print(\"haze\")\n", encoding="utf-8")
    binary = tmp_path / "helpers" / "edge_haze"
    events: list[str] = []
    popen_envs: list[dict[str, str]] = []

    class FakeProcess:
        pid = 2468

        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")

        def kill(self):
            events.append("kill")

    def fake_run(args, capture_output=False, timeout=None, check=False):
        events.append("compile")
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("binary", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    def fake_popen(args, **kwargs):
        events.append("start")
        popen_envs.append(dict(kwargs.get("env") or {}))
        return FakeProcess()

    monkeypatch.setattr(edge_haze.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(edge_haze.shutil, "which", lambda name: "/usr/bin/swiftc" if name == "swiftc" else None)
    monkeypatch.setattr(edge_haze.subprocess, "run", fake_run)
    monkeypatch.setattr(edge_haze.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ComputerUseEdgeHazeManager, "_pid_alive", staticmethod(lambda pid: pid == 2468))
    monkeypatch.setattr(
        ComputerUseEdgeHazeManager,
        "_terminate_pid",
        classmethod(lambda cls, pid: events.append(f"terminate_pid:{pid}")),
    )
    monkeypatch.setenv("RUMI_COMPUTER_USE_HAZE", "1")

    first = ComputerUseEdgeHazeManager(
        pack_root=tmp_path,
        source_path=source,
        binary_path=binary,
        settings=EdgeHazeSettings(enabled=True, linger_seconds=5),
    )
    second = ComputerUseEdgeHazeManager(
        pack_root=tmp_path,
        source_path=source,
        binary_path=binary,
        settings=EdgeHazeSettings(enabled=True, linger_seconds=5),
    )

    payload = {"computer_use_haze_sequence_id": "run_123"}
    assert first.start(action="computer.type", payload=payload) is True
    first.stop()
    assert second.start(action="computer.key", payload=payload) is True
    second.stop()
    assert events.count("start") == 1
    assert "terminate_pid:2468" not in events
    assert popen_envs[0]["RUMI_EDGE_HAZE_SEQUENCE_ID"] == "run_123"
    lease = json.loads(Path(popen_envs[0]["RUMI_EDGE_HAZE_LEASE_PATH"]).read_text(encoding="utf-8"))
    assert 60 < lease["deadline_epoch"] - time.time() <= 121
    assert lease["status_text"] == "考え中"
    assert lease["active"] is False

    second.end_sequence("other_run")
    assert "terminate_pid:2468" not in events

    second.end_sequence("run_123")
    assert "terminate_pid:2468" in events


def test_edge_haze_standalone_active_lease_has_floor(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.computer.mac import edge_haze
    from ecosystem.rumi_default_tools_pack.domain.computer.mac.edge_haze import (
        ComputerUseEdgeHazeManager,
        EdgeHazeSettings,
    )

    source = tmp_path / "EdgeHaze.swift"
    source.write_text("print(\"haze\")\n", encoding="utf-8")
    binary = tmp_path / "helpers" / "edge_haze"

    class FakeProcess:
        pid = 9753

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

        def kill(self):
            pass

    def fake_run(args, capture_output=False, timeout=None, check=False):
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("binary", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(edge_haze.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(edge_haze.shutil, "which", lambda name: "/usr/bin/swiftc" if name == "swiftc" else None)
    monkeypatch.setattr(edge_haze.subprocess, "run", fake_run)
    monkeypatch.setattr(edge_haze.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(ComputerUseEdgeHazeManager, "_pid_alive", staticmethod(lambda pid: pid == 9753))
    monkeypatch.setenv("RUMI_COMPUTER_USE_HAZE", "1")

    manager = ComputerUseEdgeHazeManager(
        pack_root=tmp_path,
        source_path=source,
        binary_path=binary,
        settings=EdgeHazeSettings(enabled=True, linger_seconds=1),
    )

    assert manager.start(action="computer.click", payload={}) is True

    lease = json.loads(manager._lease_path.read_text(encoding="utf-8"))
    remaining = lease["deadline_epoch"] - time.time()
    assert lease["sequence_id"] == "standalone"
    assert lease["status_text"] == "操作中"
    assert lease["active"] is True
    assert 25 <= remaining <= 31


def test_edge_haze_virtual_pointer_updates_lease(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.computer.mac import edge_haze
    from ecosystem.rumi_default_tools_pack.domain.computer.mac.edge_haze import (
        ComputerUseEdgeHazeManager,
        EdgeHazeSettings,
    )

    source = tmp_path / "EdgeHaze.swift"
    source.write_text("print(\"haze\")\n", encoding="utf-8")
    binary = tmp_path / "helpers" / "edge_haze"

    class FakeProcess:
        pid = 8642

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

        def kill(self):
            pass

    def fake_run(args, capture_output=False, timeout=None, check=False):
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("binary", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(edge_haze.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(edge_haze.shutil, "which", lambda name: "/usr/bin/swiftc" if name == "swiftc" else None)
    monkeypatch.setattr(edge_haze.subprocess, "run", fake_run)
    monkeypatch.setattr(edge_haze.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(ComputerUseEdgeHazeManager, "_pid_alive", staticmethod(lambda pid: pid == 8642))
    monkeypatch.setenv("RUMI_COMPUTER_USE_HAZE", "1")

    manager = ComputerUseEdgeHazeManager(
        pack_root=tmp_path,
        source_path=source,
        binary_path=binary,
        settings=EdgeHazeSettings(enabled=True, linger_seconds=1),
    )

    result = manager.update_virtual_pointer(
        {"x": 42.4, "y": 80.6, "origin": "top_left", "phase": "move"},
        action="computer.move",
        payload={
            "computer_use_haze_sequence_id": "cursor_run",
            "edge_haze_target_window": {
                "app": "Vivaldi",
                "pid": 1234,
                "window_id": 5678,
                "title": "Google",
                "x": 10,
                "y": 20,
                "width": 800,
                "height": 600,
            },
        },
    )

    lease = json.loads(manager._lease_path.read_text(encoding="utf-8"))
    assert result["started"] is True
    assert result["sequence_id"] == "cursor_run"
    assert lease["virtual_pointer"]["x"] == 42
    assert lease["virtual_pointer"]["y"] == 81
    assert lease["virtual_pointer"]["visible"] is True
    assert lease["target_window"] == {
        "app": "Vivaldi",
        "height": 600,
        "pid": 1234,
        "width": 800,
        "window_id": 5678,
        "window_title": "Google",
        "x": 10,
        "y": 20,
    }


def test_edge_haze_does_not_reuse_old_target_when_new_payload_has_none(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.computer.mac import edge_haze
    from ecosystem.rumi_default_tools_pack.domain.computer.mac.edge_haze import (
        ComputerUseEdgeHazeManager,
        EdgeHazeSettings,
    )

    source = tmp_path / "EdgeHaze.swift"
    source.write_text("print(\"haze\")\n", encoding="utf-8")
    binary = tmp_path / "helpers" / "edge_haze"
    events: list[str] = []

    class FakeProcess:
        pid = 97531

        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")

        def kill(self):
            events.append("kill")

    def fake_run(args, capture_output=False, timeout=None, check=False):
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("binary", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(edge_haze.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(edge_haze.shutil, "which", lambda name: "/usr/bin/swiftc" if name == "swiftc" else None)
    monkeypatch.setattr(edge_haze.subprocess, "run", fake_run)
    monkeypatch.setattr(edge_haze.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(ComputerUseEdgeHazeManager, "_pid_alive", staticmethod(lambda pid: pid == 97531))
    monkeypatch.setenv("RUMI_COMPUTER_USE_HAZE", "1")

    manager = ComputerUseEdgeHazeManager(
        pack_root=tmp_path,
        source_path=source,
        binary_path=binary,
        settings=EdgeHazeSettings(enabled=True, linger_seconds=1),
    )
    payload = {
        "computer_use_haze_sequence_id": "target_run",
        "edge_haze_target_window": {"app": "Safari", "pid": 111, "window_id": 222, "title": "Old"},
    }

    assert manager.start(action="computer.key", payload=payload) is True
    assert "target_window" in json.loads(manager._lease_path.read_text(encoding="utf-8"))

    assert manager.start(action="computer.key", payload={"computer_use_haze_sequence_id": "target_run"}) is True
    lease = json.loads(manager._lease_path.read_text(encoding="utf-8"))

    assert "target_window" not in lease


def test_browser_computer_haze_payload_includes_target_window(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    monkeypatch.setattr(
        controller,
        "_list_windows",
        lambda: (_ for _ in ()).throw(AssertionError("haze payload must not enumerate windows before action")),
    )
    monkeypatch.setattr(
        controller,
        "_window_at_point",
        lambda *args: (_ for _ in ()).throw(AssertionError("haze payload must not inspect point windows before action")),
    )

    payload = controller._edge_haze_payload("computer.key", {"app": "Vivaldi", "key_combo": "return"})

    assert payload["edge_haze_target_window"] == {"app": "Vivaldi"}


def test_browser_computer_haze_payload_preserves_explicit_target_window(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)

    payload = controller._edge_haze_payload(
        "computer.key",
        {
            "key_combo": "return",
            "edge_haze_target_window": {
                "app": "Vivaldi",
                "title": "Google - Vivaldi",
                "x": 0,
                "y": 37,
                "width": 1470,
                "height": 919,
                "window_id": 7112,
                "pid": 23721,
                "frame_window_ids": [7112, 7113],
            },
        },
    )

    assert payload["edge_haze_target_window"] == {
        "app": "Vivaldi",
        "frame_window_ids": [7112, 7113],
        "height": 919,
        "pid": 23721,
        "width": 1470,
        "window_id": 7112,
        "window_title": "Google - Vivaldi",
        "x": 0,
        "y": 37,
    }


def test_browser_computer_virtual_pointer_payload_includes_target_window(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.computer.mac import edge_haze
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    captured: dict[str, object] = {}

    class FakeManager:
        @classmethod
        def from_pack_root(cls, _pack_root):
            return cls()

        def update_virtual_pointer(self, pointer, *, action, payload):
            captured["pointer"] = pointer
            captured["action"] = action
            captured["payload"] = payload
            return {"started": True, "lease_path": "/tmp/lease.json", "sequence_id": "seq"}

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(edge_haze.ComputerUseEdgeHazeManager, "from_pack_root", FakeManager.from_pack_root)
    controller._write_computer_state(
        {
            "target_window": {
                "app": "Vivaldi",
                "title": "Google - Vivaldi",
                "x": 0,
                "y": 37,
                "width": 1470,
                "height": 919,
                "window_id": 7112,
                "pid": 23721,
            }
        }
    )

    result = controller._publish_virtual_pointer(
        {"x": 100, "y": 120, "origin": "top_left"},
        action="computer.move",
        payload={"x": 100, "y": 120, "coordinate_space": "screen"},
    )

    assert result["started"] is True
    assert captured["payload"]["edge_haze_target_window"]["pid"] == 23721
    assert captured["payload"]["edge_haze_target_window"]["window_id"] == 7112


def test_browser_computer_haze_payload_does_not_treat_coordinates_as_target(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController(artifact_root=tmp_path)
    target = controller._edge_haze_target_from_mapping({"x": 10, "y": 20, "width": 300, "height": 200})

    assert target is None


def test_browser_computer_injects_haze_sequence_from_context_without_overwriting_payload():
    with _default_tools_function_imports():
        from browser_computer import main as browser_computer_main

        injected = browser_computer_main._payload_with_sequence_defaults({}, {"run_id": "run_abc"}, {})
        explicit = browser_computer_main._payload_with_sequence_defaults(
            {"computer_use_haze_sequence_id": "explicit"},
            {"run_id": "run_abc"},
            {},
        )

    assert injected["computer_use_haze_sequence_id"] == "run_abc"
    assert explicit["computer_use_haze_sequence_id"] == "explicit"


def test_browser_computer_run_passes_context_sequence_to_global_contract(monkeypatch):
    with _default_tools_function_imports():
        from browser_computer import main as browser_computer_main

        captured: dict[str, object] = {}

        def fake_host_contract(action, payload, *, source_function_id):
            captured["action"] = action
            captured["payload"] = payload
            captured["source_function_id"] = source_function_id
            return {"action": action}

        monkeypatch.setattr(
            browser_computer_main,
            "run_host_contract_action",
            fake_host_contract,
        )

        browser_computer_main.run({"request_id": "req_ctx"}, {"action": "computer.type", "payload": {"text": "hi"}})

    assert captured["action"] == "computer.type"
    assert captured["payload"]["computer_use_haze_sequence_id"] == "req_ctx"
    assert captured["source_function_id"] == "browser_computer"


def test_browser_computer_run_ends_haze_sequence_and_removes_lease(tmp_path, monkeypatch):
    with _default_tools_function_imports():
        from browser_computer import main as browser_computer_main
        from ecosystem.rumi_default_tools_pack.domain.computer.mac.edge_haze import ComputerUseEdgeHazeManager

        terminated: list[int] = []
        monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user_data"))
        monkeypatch.setattr(
            ComputerUseEdgeHazeManager,
            "_terminate_pid",
            classmethod(lambda cls, pid: terminated.append(pid)),
        )
        monkeypatch.setattr(
            browser_computer_main,
            "run_host_contract_action",
            lambda action, payload, *, source_function_id: {
                "action": action,
                "executed": True,
            },
        )

        lease_path = tmp_path / "user_data" / "shared" / "helpers" / "edge_haze" / "edge_haze.lease.json"

        def write_lease(sequence_id: str, pid: int) -> None:
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease_path.write_text(
                json.dumps(
                    {
                        "schema": "rumi.edge_haze_lease.v1",
                        "pid": pid,
                        "sequence_id": sequence_id,
                        "deadline_epoch": time.time() + 120,
                    }
                ),
                encoding="utf-8",
            )

        write_lease("run_success", 9101)
        browser_computer_main.run({"request_id": "run_success"}, {"action": "computer.type", "payload": {"text": "hi"}})
        assert not lease_path.exists()

        def raise_timeout(*args, **kwargs):
            raise TimeoutError("timed out")

        write_lease("run_timeout", 9102)
        monkeypatch.setattr(browser_computer_main, "run_host_contract_action", raise_timeout)
        try:
            browser_computer_main.run({"request_id": "run_timeout"}, {"action": "computer.type", "payload": {"text": "hi"}})
        except TimeoutError:
            pass
        assert not lease_path.exists()

        def raise_cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        write_lease("run_cancelled", 9103)
        monkeypatch.setattr(browser_computer_main, "run_host_contract_action", raise_cancelled)
        try:
            browser_computer_main.run({"request_id": "run_cancelled"}, {"action": "computer.type", "payload": {"text": "hi"}})
        except asyncio.CancelledError:
            pass
        assert not lease_path.exists()

    assert terminated == [9101, 9102, 9103]


def test_browser_use_and_computer_use_preserve_sequence_payload(monkeypatch):
    with _default_tools_function_imports():
        from browser_use import main as browser_use_main
        from computer_use import main as computer_use_main

        captured: list[dict[str, object]] = []

        def fake_run_browser_computer(context, args):
            captured.append(args)
            return {"status": "ok"}

        monkeypatch.setattr(browser_use_main, "_run_browser_computer", fake_run_browser_computer)
        monkeypatch.setattr(computer_use_main, "_run_browser_computer", fake_run_browser_computer)

        browser_use_main.run(
            {"request_id": "ctx_request"},
            {"action": "click", "run_id": "run_from_browser", "x": 1, "y": 2},
        )
        computer_use_main.run(
            {"request_id": "ctx_request"},
            {"action": "type", "request_id": "req_from_computer", "text": "hi"},
        )

    assert captured[0]["payload"]["run_id"] == "run_from_browser"
    assert captured[1]["payload"]["request_id"] == "req_from_computer"


def test_edge_haze_swift_helper_watches_lease():
    source = ROOT / "ecosystem" / "rumi_default_tools_pack" / "domain" / "computer" / "mac" / "EdgeHaze.swift"
    text = source.read_text(encoding="utf-8")
    assert "RUMI_EDGE_HAZE_LEASE_PATH" in text
    assert "deadline_epoch" in text
    assert "status_text" in text
    assert "virtual_pointer" in text
    assert "target_window" in text
    assert "EdgeHazeController" in text
    assert "windowInfoMatches" in text
    assert "targetWindowDrawRect" in text
    assert "fallbackDrawRect" in text
    assert "frontmostApplication" in text
    assert "appKitRect(from:" in text
    assert "displayBounds(for:" in text
    assert "drawVirtualPointer" in text
    assert "考え中" in text
    assert "app.terminate(nil)" in text
    assert "snapshot.ownerPID != frontmostPID" not in text
    assert "visible non-frontmost target should draw target rect" in text


def test_edge_haze_draw_path_uses_cached_state():
    source = ROOT / "ecosystem" / "rumi_default_tools_pack" / "domain" / "computer" / "mac" / "EdgeHaze.swift"
    text = source.read_text(encoding="utf-8")
    draw_body = text.split("override func draw(_ dirtyRect: NSRect)", 1)[1].split("private func drawGlow", 1)[0]

    assert "currentLease()" not in draw_body
    assert "CGWindowListCopyWindowInfo" not in draw_body
    assert "controller.lease" in draw_body
    assert "controller.targetWindowDrawRect" in draw_body


def test_mac_swift_host_click_text_accepts_text_aliases():
    source = ROOT / "ecosystem" / "rumi_default_tools_pack" / "domain" / "computer" / "mac" / "ComputerUseHost.swift"
    text = source.read_text(encoding="utf-8")
    semantic_text_body = text.split("func semanticText(args: [String: Any]) -> String", 1)[1].split("struct AXCandidate", 1)[0]

    assert '"text_query"' in semantic_text_body
    assert '"match_text"' in semantic_text_body


def test_edge_haze_swift_self_test_passes(tmp_path):
    if sys.platform != "darwin":
        return
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return
    source = ROOT / "ecosystem" / "rumi_default_tools_pack" / "domain" / "computer" / "mac" / "EdgeHaze.swift"
    binary = tmp_path / "edge_haze_self_test"

    subprocess.run([swiftc, str(source), "-o", str(binary)], check=True, timeout=30)
    subprocess.run([str(binary), "--self-test"], check=True, timeout=10)


def test_browser_computer_wraps_visible_desktop_actions_with_haze(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    events: list[str] = []

    @contextlib.contextmanager
    def fake_haze(self, action, payload):
        events.append(f"enter:{action}")
        try:
            yield {"attempted": True, "started": True, "action": action, "sequence_id": "seq-test"}
        finally:
            events.append(f"exit:{action}")

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(BrowserComputerController, "_edge_haze", fake_haze)
    monkeypatch.setattr(BrowserComputerController, "_try_computer_seat_action", lambda self, action, payload, **kwargs: None)
    monkeypatch.setattr(BrowserComputerController, "_darwin_type", lambda self, payload: None)

    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    result = controller.run(
        "computer.type",
        {"text": "hi", "include_screenshot": False},
        yolo_mode=True,
    )

    assert result["executed"] is True
    assert result["edge_haze"] == {
        "attempted": True,
        "started": True,
        "action": "computer.type",
        "sequence_id": "seq-test",
    }
    assert events == [
        "enter:computer.type",
        "exit:computer.type",
        "enter:computer.type",
        "exit:computer.type",
        "enter:computer.type",
        "exit:computer.type",
    ]


def test_browser_computer_wraps_foreground_open_url_with_haze(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    events: list[str] = []

    @contextlib.contextmanager
    def fake_haze(self, action, payload):
        events.append(f"enter:{action}")
        try:
            yield {"attempted": True, "started": True, "action": action, "sequence_id": "seq-test"}
        finally:
            events.append(f"exit:{action}")

    monkeypatch.setattr(BrowserComputerController, "_edge_haze", fake_haze)
    monkeypatch.setattr(BrowserComputerController, "_open_url_foreground", staticmethod(lambda url, app_name="": True))
    monkeypatch.setattr(
        BrowserComputerController,
        "_darwin_open_url_with_target_app",
        lambda self, url, app_name: {"opened": True},
    )

    result = BrowserComputerController(artifact_root=tmp_path).run(
        "browser.open_url",
        {"url": "https://example.test", "app": "Google Chrome"},
        yolo_mode=True,
    )

    assert result["opened"] is True
    assert result["edge_haze"] == {
        "attempted": True,
        "started": True,
        "action": "browser.open_url",
        "sequence_id": "seq-test",
    }
    assert events == ["enter:browser.open_url", "exit:browser.open_url"]


def test_browser_computer_does_not_wrap_screenshot_with_haze(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    def fail_haze(self, action, payload):
        raise AssertionError("screenshot should not start haze")

    monkeypatch.setattr(BrowserComputerController, "_edge_haze", fail_haze)

    result = BrowserComputerController(artifact_root=tmp_path).run(
        "computer.screenshot",
        {"dry_run": True},
        yolo_mode=True,
    )

    assert result["action"] == "computer.screenshot"


def test_browser_computer_screenshot_uses_darwin_timeout(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool import browser_computer
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    calls: list[dict[str, object]] = []

    def fake_run(args, check=False, timeout=None, **kwargs):
        calls.append({"args": args, "check": check, "timeout": timeout, "kwargs": kwargs})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(browser_computer.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(browser_computer.subprocess, "run", fake_run)

    screenshot_path = tmp_path / "screenshot.png"
    controller = BrowserComputerController(artifact_root=tmp_path)
    controller._session_path = tmp_path / "shared" / "browser_sessions.json"
    result = controller._capture_screenshot(screenshot_path, {})

    assert result == {"platform": "Darwin", "target_window": None}
    assert calls == [
        {
            "args": ["screencapture", "-x", str(screenshot_path)],
            "check": True,
            "timeout": browser_computer._DARWIN_SCREENSHOT_TIMEOUT_SECONDS,
            "kwargs": {},
        }
    ]
