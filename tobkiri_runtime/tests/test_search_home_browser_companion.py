from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
SEARCH_HOME_ROOT = ROOT / "ecosystem" / "search_home_pack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_search_home_frontend_never_delivers_or_persists_route_state() -> None:
    """Sensitive route decisions remain in component memory only."""
    app = (SEARCH_HOME_ROOT / "webapp" / "src" / "App.tsx").read_text(
        encoding="utf-8"
    )
    api = (SEARCH_HOME_ROOT / "webapp" / "src" / "api.ts").read_text(
        encoding="utf-8"
    )
    router_types = (
        SEARCH_HOME_ROOT / "webapp" / "src" / "routerTypes.ts"
    ).read_text(encoding="utf-8")

    assert "postMessage(" not in app
    assert "sessionStorage.getItem(" not in app
    assert "sessionStorage.setItem(" not in app
    assert "persistRouteStateRemotely" not in app
    assert "loadRouteState" not in app
    assert "api/route-state" not in api
    assert "buildBrowserCompanionRouteMessage" not in router_types
    assert "persistRouteSessionState" not in router_types
    assert "sessionStorage.removeItem(key)" in app


def test_browser_companion_does_not_accept_or_store_search_home_routes() -> None:
    """The extension has no page-message or global-hotkey route bridge."""
    extension_root = (
        DEFAULTSPACK_ROOT / "browser_extensions" / "rumi_browser_companion"
    )
    background = (extension_root / "background.js").read_text(encoding="utf-8")
    content = (extension_root / "content_script.js").read_text(encoding="utf-8")
    manifest = json.loads(
        (extension_root / "manifest.json").read_text(encoding="utf-8")
    )

    for message_type in (
        "rumi:search-home:set-route-state",
        "rumi:search-home:get-route-state",
        "rumi:search-home:advance-candidate",
    ):
        assert message_type not in background
        assert message_type not in content
    assert "SEARCH_HOME_MESSAGE_SOURCE" not in content
    assert "searchHomeRouteStateExpiresAt" not in content
    assert "normalizeSearchHomeRouteState" not in background
    assert "chrome.tabs.update(tabId, { url })" not in background
    assert "x_rumi_search_home_origins" not in manifest
    assert not (extension_root / "search_home_destination_policy.js").exists()


def test_browser_companion_deletes_legacy_route_storage_on_lifecycle() -> None:
    """Extension install and startup remove previously retained route payloads."""
    background = (
        DEFAULTSPACK_ROOT
        / "browser_extensions"
        / "rumi_browser_companion"
        / "background.js"
    ).read_text(encoding="utf-8")

    assert "LEGACY_SEARCH_HOME_ROUTE_STATE_KEY" in background
    assert "clearLegacySearchHomeRouteState();" in background
    assert (
        "chrome.storage.local.remove(LEGACY_SEARCH_HOME_ROUTE_STATE_KEY)"
        in background
    )


def test_search_home_backend_has_delete_only_legacy_route_state() -> None:
    """The local server cannot write or restore sensitive route decisions."""
    desktop = (SEARCH_HOME_ROOT / "desktop_app.py").read_text(encoding="utf-8")

    assert "persist_route_state" not in desktop
    assert "load_route_state" not in desktop
    assert '("GET", "/api/route-state")' not in desktop
    assert '("POST", "/api/route-state")' not in desktop
    assert "clear_route_state(root=" in desktop
    assert "path.unlink()" in desktop
