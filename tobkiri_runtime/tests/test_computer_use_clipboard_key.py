from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_computer_key_normalizes_retrun_typo_on_macos():
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController()

    assert controller._apple_script("computer.key", {"key": "retrun"}) == 'tell application "System Events" to key code 36'


def test_computer_backspace_repeat_generates_repeated_key_code_on_macos():
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    controller = BrowserComputerController()
    script = controller._apple_script("computer.key", {"key": "backspace", "count": 3})

    assert "repeat 3 times" in script
    assert 'key code 51' in script


def test_computer_clipboard_preview_projects_authorized_result(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    from ecosystem.rumi_default_tools_pack.domain.tool import host_contract_adapter

    calls = []

    def dispatch(action, payload, **kwargs):
        calls.append((action, payload, kwargs))
        return {"success": True, "text": "clip text", "written": True}

    monkeypatch.setattr(host_contract_adapter, "run_host_contract_action", dispatch)
    controller = BrowserComputerController(artifact_root=tmp_path)
    read = controller.run("clipboard", {}, yolo_mode=True)
    full = controller.run("clipboard", {"include_content": True}, yolo_mode=False)
    write = controller.run("clipboard_write", {"content": "new text"}, yolo_mode=True)
    controller.run("clipboard_clear", {}, yolo_mode=True)

    assert read["content_preview"] == "clip text"
    assert read["content_included"] is False
    assert "content" not in read
    assert full["content"] == "clip text"
    assert full["content_included"] is True
    assert write["written"] is True
    assert [call[0] for call in calls] == [
        "computer.clipboard.read", "computer.clipboard.read",
        "computer.clipboard.write", "computer.clipboard.clear",
    ]
    assert all(call[2] == {"source_function_id": "browser_computer"} for call in calls)


def test_computer_clipboard_boolean_cannot_override_host_denial(tmp_path, monkeypatch):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController
    from ecosystem.rumi_default_tools_pack.domain.tool import host_contract_adapter

    denied = {"success": False, "error_type": "approval_required"}
    monkeypatch.setattr(host_contract_adapter, "run_host_contract_action", lambda *a, **k: denied)
    controller = BrowserComputerController(artifact_root=tmp_path)
    for action in ("clipboard", "clipboard_write", "clipboard_clear"):
        for yolo in (True, False):
            assert controller.run(action, {"approved": True}, yolo_mode=yolo) == denied


def test_computer_clipboard_direct_helpers_are_retired():
    import pytest
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import BrowserComputerController

    with pytest.raises(PermissionError, match="legacy_clipboard_execution_retired"):
        BrowserComputerController._system_clipboard_read()
    with pytest.raises(PermissionError, match="legacy_clipboard_execution_retired"):
        BrowserComputerController._system_clipboard_write("text")
