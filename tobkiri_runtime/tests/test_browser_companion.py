from __future__ import annotations

import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aKkAAAAASUVORK5CYII="
)


def _browser_companion_extension_root() -> Path:
    candidates = [
        DEFAULTSPACK_ROOT / "browser_extensions" / "rumi_browser_companion",
        ROOT.parent / "browser_extensions" / "rumi_browser_companion",
    ]
    for candidate in candidates:
        if (candidate / "content_script.js").is_file() and (candidate / "background.js").is_file():
            return candidate
    return candidates[0]


def _defaultspack_domain_module(module_name: str):
    """Import a pack-local domain module without leaking import globals.

    A few compatibility checks need to probe the defaultspack import root,
    but collected tests may already hold classes whose function globals point
    at the canonical ``domain.chat.store`` module.  Restoring the exact
    module and path bindings prevents that probe from leaving a detached
    ``ChatStore`` class for the next test.
    """
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "domain" or name.startswith("domain.")
    }
    saved_path = list(sys.path)
    try:
        sys.modules.pop("domain", None)
        for name in list(sys.modules):
            if name.startswith("domain."):
                sys.modules.pop(name, None)
        while str(DEFAULTSPACK_ROOT) in sys.path:
            sys.path.remove(str(DEFAULTSPACK_ROOT))
        sys.path.insert(0, str(DEFAULTSPACK_ROOT))
        return __import__(module_name, fromlist=["*"])
    finally:
        sys.path[:] = saved_path
        for name in list(sys.modules):
            if name == "domain" or name.startswith("domain."):
                if name not in saved_modules:
                    sys.modules.pop(name, None)
        for name, module in saved_modules.items():
            sys.modules[name] = module
        for name, module in saved_modules.items():
            if "." not in name:
                continue
            parent_name, attr_name = name.rsplit(".", 1)
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, attr_name, module)


def test_browser_companion_candidate_urls_match_defaultspack_default_port():
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion_bridge import candidate_base_urls

    urls = candidate_base_urls({})
    assert "http://127.0.0.1:8766" in urls
    assert "http://localhost:8766" in urls
    assert "http://127.0.0.1:8765" not in urls


def test_browser_companion_store_accepts_tabs_summary_alias(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion_bridge import BrowserCompanionBridgeStore

    store = BrowserCompanionBridgeStore(root=tmp_path / "bridge")
    record = store.upsert_client(
        {
            "client_id": "edge-1",
            "browser_name": "Microsoft Edge",
            "browser_profile_id": "edge-work-profile",
            "profile_label": "Work",
            "installation_id": "install-edge-1",
            "tabs_summary": [
                {"id": 17, "active": True, "title": "Example", "url": "https://example.com"},
            ],
        }
    )

    assert record["tabs"][0]["id"] == 17
    assert record["active_tab_id"] == 17
    assert record["browser_profile_id"] == "edge-work-profile"
    assert record["profile_label"] == "Work"
    assert record["installation_id"] == "install-edge-1"
    assert record["client_profile"]["browser_profile_id"] == "edge-work-profile"
    assert store.resolve_client(browser_profile_id="edge-work-profile")["client_id"] == "edge-1"
    assert store.resolve_client(installation_id="install-edge-1")["client_id"] == "edge-1"
    assert store.resolve_client(profile_label="wor")["client_id"] == "edge-1"


def test_browser_companion_no_client_returns_actionable_setup_state(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion import BrowserCompanionController
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion_bridge import BrowserCompanionBridgeStore

    store = BrowserCompanionBridgeStore(root=tmp_path / "bridge")
    controller = BrowserCompanionController(
        artifact_root=tmp_path / "artifacts",
        bridge_store=store,
    )

    session = controller.run("session", context={"base_url": "http://rumi.local"})
    setup_state = session["setup_state"]

    assert session["setup_required"] is True
    assert setup_state["status"] == "missing"
    assert "browser_companion_client" in setup_state["missing"]
    assert setup_state["ui"]["sidebar_item_id"] == "browser_companion"
    assert setup_state["ui"]["settings_field_id"] == "browser_companion_setup_guide"
    extension_path = setup_state["extension"]["path"]
    assert extension_path.endswith("browser_extensions\\rumi_browser_companion") or extension_path.endswith(
        "browser_extensions/rumi_browser_companion"
    )
    assert "http://rumi.local" in setup_state["server_urls"]

    result = controller.run(
        "page.snapshot",
        {"include_capture": True},
        context={"profile_policy": {"yolo_mode": True}},
    )

    assert result["is_error"] is True
    assert result["error_code"] == "BROWSER_COMPANION_CLIENT_MISSING"
    assert result["setup_required"] is True
    assert result["retry_after_setup"] is True
    assert result["setup_state"]["tool_actions"]["refresh_pairing"]["args"] == {"action": "bridge.pairing"}
    assert list((tmp_path / "bridge" / "commands").glob("*.json")) == []


def test_browser_companion_controller_round_trip_uses_active_tab_and_saves_capture(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion import BrowserCompanionController
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion_bridge import BrowserCompanionBridgeStore

    store = BrowserCompanionBridgeStore(root=tmp_path / "bridge")
    store.upsert_client(
        {
            "client_id": "edge-1",
            "browser_name": "Microsoft Edge",
            "browser_profile_id": "edge-work-profile",
            "profile_label": "Work",
            "installation_id": "install-edge-1",
            "tabs": [
                {"id": 17, "active": True, "title": "Example", "url": "https://example.com"},
            ],
            "active_tab_id": 17,
        }
    )
    controller = BrowserCompanionController(
        artifact_root=tmp_path / "artifacts",
        bridge_store=store,
    )

    def extension_worker():
        claimed = None
        deadline = time.time() + 3.0
        while claimed is None and time.time() < deadline:
            claimed = store.claim_next_command("edge-1")
            if claimed is None:
                time.sleep(0.05)
        assert claimed is not None
        request = claimed["request"]
        assert request["action"] == "page.snapshot"
        assert request["payload"]["tab_id"] == 17
        store.complete_command(
            "edge-1",
            claimed["command_id"],
            {
                "snapshot": {
                    "url": "https://example.com",
                    "title": "Example",
                    "nodes": [{"element_id": "rumi-el-1", "text": "Send"}],
                },
                "capture": {
                    "data_url": _PNG_DATA_URL,
                    "image_size": {"width": 1, "height": 1},
                },
            },
        )

    worker = threading.Thread(target=extension_worker, daemon=True)
    worker.start()
    result = controller.run(
        "page.snapshot",
        {"include_capture": True},
        context={"profile_policy": {"yolo_mode": True}},
    )
    worker.join(timeout=1.0)

    assert result["is_error"] is False
    assert result["client_id"] == "edge-1"
    assert result["browser_profile_id"] == "edge-work-profile"
    assert result["profile_label"] == "Work"
    assert result["installation_id"] == "install-edge-1"
    assert result["client_profile"]["browser_profile_id"] == "edge-work-profile"
    assert result["snapshot"]["url"] == "https://example.com"
    assert result["elements"][0]["element_id"] == "rumi-el-1"
    assert result["requires_foreground"] is True
    assert result["can_parallel_user_work"] is False
    assert result["path"].endswith(".png")
    assert Path(result["path"]).exists()


def test_browser_companion_controller_marks_dom_actions_parallel_safe(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion import BrowserCompanionController
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion_bridge import BrowserCompanionBridgeStore

    store = BrowserCompanionBridgeStore(root=tmp_path / "bridge")
    store.upsert_client(
        {
            "client_id": "edge-1",
            "browser_name": "Microsoft Edge",
            "tabs": [
                {"id": 17, "active": True, "title": "Example", "url": "https://example.com"},
            ],
            "active_tab_id": 17,
        }
    )
    controller = BrowserCompanionController(
        artifact_root=tmp_path / "artifacts",
        bridge_store=store,
    )

    def extension_worker():
        claimed = None
        deadline = time.time() + 3.0
        while claimed is None and time.time() < deadline:
            claimed = store.claim_next_command("edge-1")
            if claimed is None:
                time.sleep(0.05)
        assert claimed is not None
        assert claimed["request"]["action"] == "page.click"
        store.complete_command(
            "edge-1",
            claimed["command_id"],
            {"ok": True, "action": "click", "element_id": "rumi-el-1"},
        )

    worker = threading.Thread(target=extension_worker, daemon=True)
    worker.start()
    result = controller.run(
        "page.click",
        {"element_id": "rumi-el-1"},
        context={"profile_policy": {"yolo_mode": True}},
    )
    worker.join(timeout=1.0)

    assert result["is_error"] is False
    assert result["requires_foreground"] is False
    assert result["can_parallel_user_work"] is True


def test_browser_companion_page_action_requires_approval_before_queueing(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion import BrowserCompanionController
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion_bridge import BrowserCompanionBridgeStore

    store = BrowserCompanionBridgeStore(root=tmp_path / "bridge")
    store.upsert_client(
        {
            "client_id": "edge-1",
            "browser_name": "Microsoft Edge",
            "tabs": [
                {"id": 17, "active": True, "title": "Example", "url": "https://example.com"},
            ],
            "active_tab_id": 17,
        }
    )
    controller = BrowserCompanionController(
        artifact_root=tmp_path / "artifacts",
        bridge_store=store,
    )

    result = controller.run(
        "page.type",
        {"selector": "#amount", "text": "TRANSFER 1000"},
        context={},
    )

    assert result["requires_approval"] is True
    assert result["approval_required"] is True
    assert result["payload"] == {
        "action": "page.type",
        "client_id": "edge-1",
        "selector": "#amount",
        "tab_id": 17,
        "text": "TRANSFER 1000",
    }
    assert store.claim_next_command("edge-1") is None


def test_browser_companion_page_action_approval_token_allows_single_replay(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion import BrowserCompanionController
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion_bridge import BrowserCompanionBridgeStore

    store = BrowserCompanionBridgeStore(root=tmp_path / "bridge")
    store.upsert_client(
        {
            "client_id": "edge-1",
            "browser_name": "Microsoft Edge",
            "tabs": [
                {"id": 17, "active": True, "title": "Example", "url": "https://example.com"},
            ],
            "active_tab_id": 17,
        }
    )
    controller = BrowserCompanionController(
        artifact_root=tmp_path / "artifacts",
        bridge_store=store,
    )
    approval = controller.run("page.click", {"element_id": "rumi-el-1"}, context={})

    def extension_worker():
        claimed = None
        deadline = time.time() + 3.0
        while claimed is None and time.time() < deadline:
            claimed = store.claim_next_command("edge-1")
            if claimed is None:
                time.sleep(0.05)
        assert claimed is not None
        assert claimed["request"]["action"] == "page.click"
        assert "approval_token" not in claimed["request"]["payload"]
        store.complete_command(
            "edge-1",
            claimed["command_id"],
            {"ok": True, "action": "click", "element_id": "rumi-el-1"},
        )

    worker = threading.Thread(target=extension_worker, daemon=True)
    worker.start()
    result = controller.run(
        "page.click",
        {"element_id": "rumi-el-1", "approval_token": approval["approval_token"]},
        context={},
    )
    worker.join(timeout=1.0)

    assert result["is_error"] is False
    assert store.claim_next_command("edge-1") is None
    replay = controller.run(
        "page.click",
        {"element_id": "rumi-el-1", "approval_token": approval["approval_token"]},
        context={},
    )
    assert replay["requires_approval"] is True


def test_browser_companion_read_only_safety_blocks_write_actions(tmp_path):
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion import BrowserCompanionController
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion_bridge import BrowserCompanionBridgeStore

    store = BrowserCompanionBridgeStore(root=tmp_path / "bridge")
    store.upsert_client({"client_id": "edge-1", "active_tab_id": 17})
    controller = BrowserCompanionController(
        artifact_root=tmp_path / "artifacts",
        bridge_store=store,
    )

    result = controller.run(
        "page.type",
        {"selector": "#amount", "text": "TRANSFER 1000"},
        context={
            "tool_settings": {
                "browser_companion": {"values": {"safety": "read_only"}},
            },
        },
    )

    assert result["is_error"] is True
    assert "read-only" in result["reason"]
    assert store.claim_next_command("edge-1") is None


def test_browser_companion_executor_approval_scope_is_page_action():
    _tool_approval_scope = _defaultspack_domain_module("domain.tool.executor")._tool_approval_scope

    operation, approval_args = _tool_approval_scope(
        {"name": "browser_companion"},
        {"action": "page.type", "selector": "#amount", "text": "TRANSFER 1000"},
    )

    assert operation == "page.type"
    assert approval_args == {
        "action": "page.type",
        "selector": "#amount",
        "text": "TRANSFER 1000",
    }


def test_browser_companion_extension_focus_semantics_are_explicit():
    background = (
        ROOT
        / "ecosystem"
        / "defaultspack"
        / "browser_extensions"
        / "rumi_browser_companion"
        / "background.js"
    ).read_text(encoding="utf-8")
    send_element_body = background[
        background.index("async function sendElementCommand") : background.index("async function sendToTab")
    ]
    send_to_tab_body = background[
        background.index("async function sendToTab") : background.index("async function resolveTabId")
    ]
    capture_body = background[
        background.index("async function captureVisibleTab") : background.index("async function captureDomSnapshot")
    ]

    assert "chrome.tabs.update(tabId, { url: payload.url })" in background
    assert "return sendElementCommand(action, payload, settings);" in background
    assert "chrome.tabs.sendMessage(resolvedTabId, message)" in send_to_tab_body
    assert "chrome.tabs.update" not in send_element_body
    assert "chrome.windows.update" not in send_element_body
    assert "requires_foreground: true" in capture_body
    assert "can_parallel_user_work: false" in capture_body


def test_browser_companion_extension_semantic_dom_and_highlight_contract():
    extension_root = _browser_companion_extension_root()
    content = (extension_root / "content_script.js").read_text(encoding="utf-8")
    background = (extension_root / "background.js").read_text(encoding="utf-8")
    tool_manifest = (
        ROOT
        / "ecosystem"
        / "rumi_default_tools_pack"
        / "tools"
        / "browser_companion"
        / "manifest.json"
    ).read_text(encoding="utf-8")

    for needle in (
        'schema_version: "semantic_dom_v2"',
        'schema_id: "rumi.browser.semantic_dom_v2"',
        "semantic_id:",
        "accessible_name:",
        "labels,",
        "nearby_text:",
        "viewport_center:",
        "page_rect:",
        "page_center:",
        "action_hints:",
        "recognition_confidence:",
        "selector_hints:",
        "xpath_hint:",
        "function findSemanticTarget",
        "isBetterSemanticTarget(element, best, criteria, action)",
        "function semanticTargetSpecificityScore",
        "function isBroadSemanticContainer",
        "text_query",
        "accessible_name",
        "nearby_text",
        "typedTextValue(command)",
        "function highlightElement",
        "function clearHighlights",
    ):
        assert needle in content

    for needle in (
        "profileLabel",
        "browser_profile_id",
        "profile_label",
        "installation_id",
        "client_profile",
        "function topLevelResultFields",
        "elements,",
        "semantic_dom: true",
        "accessible_labels: true",
        "semantic_targeting",
        '"highlight"',
        '"clear_highlight"',
        'case "page.highlight"',
        'case "page.clear_highlight"',
    ):
        assert needle in background or needle in tool_manifest


def test_browser_companion_snapshot_forwards_snapshot_options_to_content_script():
    background = (_browser_companion_extension_root() / "background.js").read_text(encoding="utf-8")
    capture_body = background[
        background.index("async function captureDomSnapshot") : background.index("async function sendElementCommand")
    ]

    for needle in (
        "snapshotRequest.options = snapshotOptions",
        "snapshotOptions.includeHidden",
        "snapshotOptions.includeHtml",
        "snapshotOptions.includeAttributes",
        "snapshotOptions.attributeNames",
        "snapshotOptions.includeSemantics",
    ):
        assert needle in capture_body


def test_browser_companion_extension_keeps_pairing_token_in_local_storage():
    extension_root = _browser_companion_extension_root()
    background = (extension_root / "background.js").read_text(encoding="utf-8")
    options = (extension_root / "options.js").read_text(encoding="utf-8")
    options_html = (extension_root / "options.html").read_text(encoding="utf-8")

    assert "readLocalSettingsWithSyncMigration" in background
    assert "chrome.storage.local.set({ [STORAGE_KEY]: merged })" in background
    assert "chrome.storage.sync.remove(STORAGE_KEY)" in background
    assert 'areaName !== "local"' in background
    assert "chrome.storage.local.get(STORAGE_KEY)" in options
    assert "chrome.storage.local.set({ [STORAGE_KEY]: settings })" in options
    assert "profileLabel" in options
    assert 'name="profileLabel"' in options_html
    assert "chrome.storage.sync.set({ [STORAGE_KEY]: settings })" not in options


def test_browser_companion_bridge_routes_support_batch_results(tmp_path, monkeypatch):
    from blocks.tool import browser_companion_bridge as route_module
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion_bridge import BrowserCompanionBridgeStore

    store = BrowserCompanionBridgeStore(root=tmp_path / "bridge")
    token = store.ensure_pairing(rotate=True)["pairing_token"]
    store.create_command("edge-1", {"action": "browser.tabs", "payload": {}})

    monkeypatch.setattr(route_module, "BrowserCompanionBridgeStore", lambda: store)

    poll_response = route_module.run_poll(
        {
            "_headers": {"Authorization": f"Bearer {token}"},
            "client": {"client_id": "edge-1", "browser_name": "Microsoft Edge"},
        }
    )

    assert poll_response["status"] == "ok"
    command = poll_response["data"]["command"]
    assert command["action"] == "browser.tabs"
    assert poll_response["data"]["commands"][0]["command_id"] == command["command_id"]

    result_response = route_module.run_result(
        {
            "_headers": {"Authorization": f"Bearer {token}"},
            "client_id": "edge-1",
            "results": [
                {
                    "command_id": command["command_id"],
                    "ok": True,
                    "result": {"tabs": [{"id": 17, "title": "Example"}]},
                }
            ],
        }
    )

    completed = store.wait_for_command(command["command_id"], timeout_seconds=0.2)

    assert result_response["status"] == "ok"
    assert completed["status"] == "completed"
    assert completed["result"]["tabs"][0]["id"] == 17
    assert completed["result"]["is_error"] is False


def test_browser_companion_session_route_exposes_pairing_status(tmp_path, monkeypatch):
    from blocks.tool import browser_companion_bridge as route_module
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_companion_bridge import BrowserCompanionBridgeStore

    store = BrowserCompanionBridgeStore(root=tmp_path / "bridge")
    store.ensure_pairing(rotate=True)
    store.upsert_client(
        {
            "client_id": "edge-1",
            "browser_name": "Microsoft Edge",
            "tabs": [{"id": 17, "active": True, "title": "Example", "url": "https://example.com"}],
        }
    )

    monkeypatch.setattr(route_module, "BrowserCompanionBridgeStore", lambda: store)

    response = route_module.run_session({}, context={"base_url": "http://rumi.local"})

    assert response["status"] == "ok"
    data = response["data"]
    assert data["action"] == "session"
    assert data["pairing"]["pairing_token"]
    assert "http://rumi.local" in data["pairing"]["server_urls"]
    assert data["clients"][0]["client_id"] == "edge-1"
    assert data["active_client_id"] == "edge-1"
    assert data["setup_required"] is False


def test_browser_companion_pack_not_approved_does_not_fall_back_to_local(monkeypatch):
    ToolExecutor = _defaultspack_domain_module("domain.tool.executor").ToolExecutor

    called = {"local": False}

    def fake_execute_local(self, tool_name, arguments, context):
        called["local"] = True
        raise AssertionError("browser_companion must not bypass pack approval")

    class FakeResponse:
        success = False
        error_type = "pack_not_approved"

    monkeypatch.setattr(ToolExecutor, "_execute_local", fake_execute_local)

    result = ToolExecutor._fallback_function_call_if_first_party_unapproved(
        {"name": "browser_companion"},
        {
            "type": "function.call",
            "qualified_name": "rumi_default_tools_pack:browser_companion",
            "args": {"action": "page.snapshot", "include_capture": True},
        },
        {"user_requested_computer_use": True},
        FakeResponse(),
    )

    assert result is None
    assert called["local"] is False
