"""Tests for computer_use/main.py action_map completeness."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_funcs_dir = str(Path(__file__).resolve().parent.parent / "ecosystem" / "rumi_default_tools_pack" / "functions")
if _funcs_dir not in sys.path:
    sys.path.insert(0, _funcs_dir)


EXPECTED_ACTIONS = [
    "screenshot", "click", "type", "key", "scroll", "context",
    "apps", "windows", "select_app", "select_window", "show_app", "move", "drag",
    "open_url", "browser_open_url",
    "clipboard", "clipboard_read", "clipboard_write", "clipboard_clear", "backspace",
    "ocr", "ax_tree", "click_text",
    "observe", "semantic_action", "press", "pid_event", "doctor", "diagnose",
]


def test_action_map_keys_exist():
    main_path = Path(_funcs_dir) / "computer_use" / "main.py"
    source = main_path.read_text()
    for action in EXPECTED_ACTIONS:
        assert f'"{action}"' in source, f"action_map missing key: {action}"


def test_action_map_observe_maps_correctly():
    main_path = Path(_funcs_dir) / "computer_use" / "main.py"
    source = main_path.read_text()
    assert '"observe": "computer.observe"' in source


def test_action_map_ocr_ax_tree_and_click_text_map_correctly():
    main_path = Path(_funcs_dir) / "computer_use" / "main.py"
    source = main_path.read_text()
    assert '"ocr": "computer.ocr"' in source
    assert '"ax_tree": "computer.ax_tree"' in source
    assert '"accessibility_tree": "computer.ax_tree"' in source
    assert '"click_text": "computer.click_text"' in source


def test_action_map_semantic_action_maps_correctly():
    main_path = Path(_funcs_dir) / "computer_use" / "main.py"
    source = main_path.read_text()
    assert '"semantic_action": "computer.semantic_action"' in source


def test_action_map_press_maps_to_semantic_action():
    main_path = Path(_funcs_dir) / "computer_use" / "main.py"
    source = main_path.read_text()
    assert '"press": "computer.semantic_action"' in source


def test_action_map_doctor_maps_correctly():
    main_path = Path(_funcs_dir) / "computer_use" / "main.py"
    source = main_path.read_text()
    assert '"doctor": "computer.doctor"' in source
    assert '"diagnose": "computer.doctor"' in source


def test_computer_use_manifest_exposes_new_actions():
    manifest_path = Path(_funcs_dir).parent / "tools" / "computer_use" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actions = manifest["config"]["schema"]["parameters"]["properties"]["action"]["enum"]
    for action in ["ocr", "ax_tree", "click_text", "observe", "semantic_action", "press", "pid_event", "doctor", "diagnose"]:
        assert action in actions
    properties = manifest["config"]["schema"]["parameters"]["properties"]
    for key in [
        "query",
        "text_query",
        "match_text",
        "element_id",
        "role",
        "include_ocr",
        "include_ax_tree",
        "confidence_threshold",
    ]:
        assert key in properties


def test_computer_use_manifest_exposes_include_screenshot_flag():
    manifest_path = Path(_funcs_dir).parent / "tools" / "computer_use" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    properties = manifest["config"]["schema"]["parameters"]["properties"]

    assert properties["include_screenshot"]["type"] == "boolean"


def test_computer_use_manifest_tells_models_to_continue_with_keyboard_mouse():
    manifest_path = Path(_funcs_dir).parent / "tools" / "computer_use" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    action_description = manifest["config"]["schema"]["parameters"]["properties"]["action"]["description"]
    joined = " ".join(
        [
            manifest["description"],
            manifest["config"]["summary"],
            action_description,
        ]
    )

    assert "open_url is setup" in joined
    assert "command+l" in joined
    assert "type/key/click/scroll" in joined


def test_browser_computer_manifest_exposes_new_actions():
    manifest_path = Path(_funcs_dir).parent / "tools" / "browser_computer" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actions = manifest["config"]["schema"]["parameters"]["properties"]["action"]["enum"]
    for action in [
        "computer.ocr",
        "computer.ax_tree",
        "computer.click_text",
        "computer.observe",
        "computer.semantic_action",
        "computer.press",
        "computer.pid_event",
        "computer.doctor",
        "computer.diagnose",
    ]:
        assert action in actions


def test_browser_computer_manifest_tells_models_to_continue_with_keyboard_mouse():
    manifest_path = Path(_funcs_dir).parent / "tools" / "browser_computer" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    action_description = manifest["config"]["schema"]["parameters"]["properties"]["action"]["description"]
    payload_description = manifest["config"]["schema"]["parameters"]["properties"]["payload"]["description"]
    joined = " ".join(
        [
            manifest["description"],
            manifest["config"]["summary"],
            action_description,
            payload_description,
        ]
    )

    assert "browser.open_url is setup" in joined
    assert "computer.key key_combo=command+l" in joined
    assert "physical=true" in joined


def test_action_map_open_url_maps_to_browser_open_url(monkeypatch):
    from computer_use import main as computer_use_main

    captured = {}

    def fake_run_browser_computer(context, args):
        captured["context"] = context
        captured["args"] = args
        return {"status": "ok"}

    monkeypatch.setattr(computer_use_main, "_run_browser_computer", fake_run_browser_computer)

    result = computer_use_main.run(
        {"conversation_workspace_dir": "/tmp/workspace"},
        {"action": "open_url", "url": "https://example.com", "app": "Google Chrome"},
    )

    assert result == {"status": "ok"}
    assert captured["args"]["action"] == "browser.open_url"
    assert captured["args"]["payload"]["url"] == "https://example.com"
    assert captured["args"]["payload"]["app"] == "Google Chrome"


def test_action_map_open_url_preserves_profile_and_target_app(monkeypatch):
    from computer_use import main as computer_use_main

    captured = {}

    def fake_run_browser_computer(context, args):
        captured["args"] = args
        return {"status": "ok"}

    monkeypatch.setattr(computer_use_main, "_run_browser_computer", fake_run_browser_computer)

    result = computer_use_main.run(
        {"conversation_workspace_dir": "/tmp/workspace"},
        {
            "action": "browser_open_url",
            "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "profile_id": "default",
            "persistent": False,
            "target_app": "Vivaldi",
            "approval_token": "tok",
        },
    )

    assert result == {"status": "ok"}
    assert captured["args"]["action"] == "browser.open_url"
    assert captured["args"]["payload"]["url"] == "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    assert captured["args"]["payload"]["profile_id"] == "default"
    assert captured["args"]["payload"]["persistent"] is False
    assert captured["args"]["payload"]["target_app"] == "Vivaldi"
    assert captured["args"]["payload"]["approval_token"] == "tok"


def test_action_map_click_preserves_include_screenshot(monkeypatch):
    from computer_use import main as computer_use_main

    captured = {}

    def fake_run_browser_computer(context, args):
        captured["args"] = args
        return {"status": "ok"}

    monkeypatch.setattr(computer_use_main, "_run_browser_computer", fake_run_browser_computer)

    result = computer_use_main.run(
        {"conversation_workspace_dir": "/tmp/workspace"},
        {
            "action": "click",
            "app": "Vivaldi",
            "normalized_x": 362,
            "normalized_y": 539,
            "include_screenshot": False,
        },
    )

    assert result == {"status": "ok"}
    assert captured["args"]["action"] == "computer.click"
    assert captured["args"]["payload"]["include_screenshot"] is False


def test_action_map_browser_open_url_maps_correctly():
    main_path = Path(_funcs_dir) / "computer_use" / "main.py"
    source = main_path.read_text()
    assert '"browser_open_url": "browser.open_url"' in source


def test_browser_computer_open_url_promotes_value_url(monkeypatch):
    from browser_computer import main as browser_computer_main

    captured = {}

    def fake_run_computer_action(action, payload, context=None, **kwargs):
        captured["action"] = action
        captured["payload"] = dict(payload)
        return {"action": action, "opened": True}

    monkeypatch.setattr(browser_computer_main, "_run_computer_action", lambda: fake_run_computer_action)

    result = browser_computer_main.run(
        {},
        {
            "action": "browser.open_url",
            "payload": {"app": "Vivaldi", "value": "https://www.youtube.com"},
            "tool_name": "computer_use",
        },
    )

    assert result["is_error"] is False
    assert captured["action"] == "browser.open_url"
    assert captured["payload"]["url"] == "https://www.youtube.com"
    assert captured["payload"]["value"] == "https://www.youtube.com"


def test_browser_computer_open_url_promotes_tool_argument_text_url(monkeypatch):
    from browser_computer import main as browser_computer_main

    captured = {}

    def fake_run_computer_action(action, payload, context=None, **kwargs):
        captured["action"] = action
        captured["payload"] = dict(payload)
        return {"action": action, "opened": True}

    monkeypatch.setattr(browser_computer_main, "_run_computer_action", lambda: fake_run_computer_action)

    result = browser_computer_main.run(
        {},
        {
            "action": "browser.open_url",
            "payload": {"app": "Vivaldi"},
            "tool_name": "computer_use",
            "tool_arguments": {"action": "browser_open_url", "text": "open https://www.youtube.com"},
        },
    )

    assert result["is_error"] is False
    assert captured["action"] == "browser.open_url"
    assert captured["payload"]["url"] == "https://www.youtube.com"


def test_browser_computer_context_defaults_physical_clicks_for_mouse_keyboard_intent():
    from browser_computer import main as browser_computer_main

    payload = browser_computer_main._payload_with_context_defaults(
        "computer.click",
        {"x": 120, "y": 240},
        {
            "computer_use_target_app": "Vivaldi",
            "computer_use_target_title": "YouTube",
            "computer_use_physical_clicks": True,
        },
    )

    assert payload == {
        "x": 120,
        "y": 240,
        "app": "Vivaldi",
        "title": "YouTube",
        "physical": True,
    }


def test_action_map_preserves_clipboard_and_repeat_payload(monkeypatch):
    from computer_use import main as computer_use_main

    captured = {}

    def fake_run_browser_computer(context, args):
        captured["args"] = args
        return {"status": "ok"}

    monkeypatch.setattr(computer_use_main, "_run_browser_computer", fake_run_browser_computer)

    computer_use_main.run(
        {},
        {"action": "backspace", "count": 4, "key_combo": "retrun", "content": "hello"},
    )

    assert captured["args"]["action"] == "computer.backspace"
    assert captured["args"]["payload"]["count"] == 4
    assert captured["args"]["payload"]["key_combo"] == "retrun"
    assert captured["args"]["payload"]["content"] == "hello"


def test_action_map_preserves_text_target_alias_payload(monkeypatch):
    from computer_use import main as computer_use_main

    captured = {}

    def fake_run_browser_computer(context, args):
        captured["args"] = args
        return {"status": "ok"}

    monkeypatch.setattr(computer_use_main, "_run_browser_computer", fake_run_browser_computer)

    computer_use_main.run(
        {},
        {
            "action": "click_text",
            "query": "Save",
            "text_query": "Save",
            "match_text": "Save",
            "element_id": "AX-1",
            "role": "button",
            "include_ocr": True,
            "include_ax_tree": True,
            "confidence_threshold": 0.73,
        },
    )

    assert captured["args"]["action"] == "computer.click_text"
    payload = captured["args"]["payload"]
    assert payload["query"] == "Save"
    assert payload["text_query"] == "Save"
    assert payload["match_text"] == "Save"
    assert payload["element_id"] == "AX-1"
    assert payload["role"] == "button"
    assert payload["include_ocr"] is True
    assert payload["include_ax_tree"] is True
    assert payload["confidence_threshold"] == 0.73
