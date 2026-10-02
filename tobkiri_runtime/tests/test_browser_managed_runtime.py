"""Managed browser lifecycle, real CDP behavior and sensitive-data boundaries."""

from __future__ import annotations

import base64
import json
import os
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ecosystem.rumi_browser_host_service_pack.runtime import cdp, devtools, managed
from ecosystem.rumi_browser_host_service_pack.runtime.cookies import parse_import, to_cdp
from ecosystem.rumi_browser_host_service_pack.runtime.extensions import prepare_extension
from ecosystem.rumi_browser_host_service_pack.runtime.managed import ManagedBrowser
from ecosystem.rumi_browser_host_service_pack.runtime.runner import BrowserHostRunner
from ecosystem.rumi_browser_host_service_pack.runtime.service import (
    create_browser_control,
    create_browser_observer,
)
from ecosystem.rumi_browser_host_service_pack.runtime.storage import (
    MAX_METADATA_BYTES,
    checked_path,
    write_json,
)


def extension_files() -> dict[str, str]:
    return {
        "manifest.json": json.dumps(
            {
                "manifest_version": 3,
                "name": "Tobkiri test extension",
                "version": "1.0",
                "permissions": ["storage"],
                "host_permissions": ["http://127.0.0.1/*"],
                "content_scripts": [{"matches": ["http://127.0.0.1/*"], "js": ["content.js"]}],
            }
        ),
        "content.js": "document.documentElement.dataset.tobkiriExtension = 'loaded';",
    }


class FakeConnection:
    """Small context-managed CDP fake preserving the command/event distinction."""

    calls: list[tuple[str, dict[str, Any]]]

    def __init__(
        self, results: dict[str, Any] | None = None, events: list[Any] | None = None
    ) -> None:
        self.results = results or {}
        self.events = deque(events or [])
        self.calls = []
        self.events_truncated = False

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, params or {}))
        result = self.results.get(method, {})
        if isinstance(result, Exception):
            raise result
        return result

    def event(self, _timeout: float) -> dict[str, Any] | None:
        return self.events.popleft() if self.events else None


@pytest.fixture
def fake_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ManagedBrowser, FakeConnection]:
    browser = ManagedBrowser(tmp_path / "browser_host")
    browser.root.mkdir(parents=True)
    metadata = {"profile_id": "default", "browser_path": "/devtools/browser/owned"}
    connection = FakeConnection(
        {
            "Target.createTarget": {"targetId": "page-2"},
            "Extensions.loadUnpacked": {"id": "a" * 32},
            "Extensions.getExtensions": {"extensions": [{"id": "a" * 32, "enabled": True}]},
        }
    )
    monkeypatch.setattr(
        browser,
        "_validated_runtime",
        lambda: (
            metadata.copy(),
            9222,
            "ws://127.0.0.1:9222/devtools/browser/owned",
            {"Browser": "Test Chromium"},
        ),
    )
    monkeypatch.setattr(managed, "CDPConnection", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(
        managed,
        "get_json",
        lambda *_args, **_kwargs: [
            {
                "id": "page-1",
                "type": "page",
                "title": "Local page",
                "url": "https://example.test/?token=secret",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/page-1",
            }
        ],
    )
    return browser, connection


def test_read_only_contract_cannot_capture_or_mutate() -> None:
    observer, control = create_browser_observer(), create_browser_control()
    for action in (
        "browser.runtime.start",
        "browser.extensions.install",
        "browser.devtools.evaluate",
        "browser.network.capture",
    ):
        assert observer.invoke(action, {})["status"] == "denied"
        assert control.invoke(action, {})["host_function_id"] == action
    for action in (
        "browser.runtime.status",
        "browser.extensions.list",
        "browser.devtools.inspect",
        "browser.capture.page",
    ):
        assert observer.invoke(action, {})["host_function_id"] == action
        assert control.invoke(action, {})["status"] == "denied"


def test_runner_cannot_self_approve_or_accept_browser_overrides(tmp_path: Path) -> None:
    runner = BrowserHostRunner(tmp_path)
    with pytest.raises(PermissionError, match="approval"):
        runner.run("browser.runtime.start", {}, viewer_host_approved=False)
    for key in ("approved", "approval_token", "endpoint", "executable", "user_data_dir"):
        with pytest.raises(PermissionError, match="forbidden"):
            runner.run("browser.runtime.start", {key: "untrusted"}, viewer_host_approved=True)
    assert not runner.root.exists()


def test_status_without_browser_is_local_and_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed, "find_executable", lambda: None)
    monkeypatch.setattr(
        managed, "get_json", lambda *_args, **_kwargs: pytest.fail("unexpected network")
    )
    status = ManagedBrowser(tmp_path / "browser_host").status()
    assert status["running"] is False
    assert status["executable_available"] is False
    assert "RUMI_BROWSER_EXECUTABLE" in status["message"]
    assert not (tmp_path / "browser_host").exists()


def test_start_uses_private_loopback_profile_and_headless_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = ManagedBrowser(tmp_path / "browser_host")
    commands: list[list[str]] = []
    options: list[dict[str, Any]] = []

    class Process:
        pid = 1234

        def poll(self) -> None:
            return None

    def launch(command: list[str], **kwargs: Any) -> Process:
        commands.append(command)
        options.append(kwargs)
        profile = Path(
            next(arg.split("=", 1)[1] for arg in command if arg.startswith("--user-data-dir="))
        )
        (profile / "DevToolsActivePort").write_text(
            "9222\n/devtools/browser/owned\n", encoding="utf-8"
        )
        return Process()

    monkeypatch.setattr(managed, "find_executable", lambda: Path("/trusted/chromium"))
    monkeypatch.setattr(managed.subprocess, "Popen", launch)
    monkeypatch.setattr(
        managed,
        "get_json",
        lambda *_args, **_kwargs: {
            "Browser": "Test Chromium",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/owned",
        },
    )
    monkeypatch.setattr(
        managed,
        "CDPConnection",
        lambda *_args, **_kwargs: FakeConnection(
            {
                "Browser.getBrowserCommandLine": {"arguments": commands[0]},
            }
        ),
    )
    result = browser.start("model", headless=True)
    assert result["running"] is True and result["headless"] is True
    assert result["profile_id"] == "model"
    command = commands[0]
    assert "--remote-debugging-port=0" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert "--headless=new" in command
    assert "--no-sandbox" not in command
    assert options[0]["shell"] is False
    assert str(browser.root / "profiles" / "model") in " ".join(command)
    assert "webSocketDebuggerUrl" not in json.dumps(result)
    assert "/devtools/" not in json.dumps(result)


def test_status_requested_profile_does_not_claim_other_profile_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BrowserHostRunner(tmp_path)
    runner.run("browser.profile.create", {"profile_id": "other"}, viewer_host_approved=True)
    monkeypatch.setattr(
        runner.managed, "status", lambda: {"running": True, "profile_id": "default"}
    )
    status = runner.run(
        "browser.runtime.status", {"profile_id": "other"}, viewer_host_approved=True
    )
    assert status["running"] is False
    assert status["running_profile_id"] == "default"
    monkeypatch.setattr(runner.managed, "stop", lambda: pytest.fail("closed the other profile"))
    stopped = runner.run("browser.runtime.stop", {"profile_id": "other"}, viewer_host_approved=True)
    assert stopped["is_error"] is True


def test_runtime_identity_and_discovery_cannot_target_another_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = ManagedBrowser(tmp_path / "browser_host")
    profile = browser.root / "profiles" / "default"
    profile.mkdir(parents=True)
    (profile / "DevToolsActivePort").write_text(
        "9222\n/devtools/browser/changed\n", encoding="utf-8"
    )
    write_json(
        browser.root,
        browser.runtime_path,
        {"profile_id": "default", "browser_path": "/devtools/browser/owned"},
    )
    monkeypatch.setattr(
        managed, "get_json", lambda *_args, **_kwargs: pytest.fail("discovery should not run")
    )
    with pytest.raises(PermissionError, match="changed"):
        browser._validated_runtime()
    for url in (
        "ws://example.test:9222/devtools/page/a",
        "ws://127.0.0.1:9223/devtools/page/a",
        "ws://u:p@127.0.0.1:9222/devtools/page/a",
        "ws://127.0.0.1:9222/devtools/page/a?token=x",
    ):
        with pytest.raises(PermissionError):
            cdp.validate_websocket_url(url, 9222)


def test_command_line_must_name_owned_profile(tmp_path: Path) -> None:
    profile = tmp_path / "owned"
    flags = ["--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"]
    assert ManagedBrowser._owned_command_line([*flags, f"--user-data-dir={profile}"], profile)
    assert not ManagedBrowser._owned_command_line(
        [*flags, f"--user-data-dir={tmp_path / 'personal'}"], profile
    )
    assert not ManagedBrowser._owned_command_line(
        [
            "--remote-debugging-address=0.0.0.0",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
        ],
        profile,
    )


def test_live_tabs_and_navigation_use_exact_target(
    fake_browser: tuple[ManagedBrowser, FakeConnection],
) -> None:
    browser, connection = fake_browser
    listed = browser.tabs({"profile_id": "default"})
    assert listed["tabs"][0]["tab_id"] == "page-1"
    assert "secret" not in listed["tabs"][0]["url"]
    opened = browser.navigate({"url": "https://example.test/new"})
    assert opened["tab_id"] == "page-2"
    assert connection.calls[-1] == ("Target.createTarget", {"url": "https://example.test/new"})
    with pytest.raises(KeyError, match="unavailable"):
        browser.inspect({"tab_id": "unknown"})
    with pytest.raises(ValueError, match="profile"):
        browser.tabs({"profile_id": "personal"})
    with pytest.raises(ValueError, match="credentials"):
        browser.navigate({"url": "https://user:password@example.test/"})


def test_disconnected_managed_navigation_does_not_fall_back_to_personal_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BrowserHostRunner(tmp_path)
    monkeypatch.setattr(runner.managed, "has_record", lambda: True)
    monkeypatch.setattr(
        runner.managed,
        "navigate",
        lambda *_args: (_ for _ in ()).throw(ConnectionError("disconnected")),
    )
    monkeypatch.setattr(
        "webbrowser.open", lambda *_args, **_kwargs: pytest.fail("personal browser opened")
    )
    with pytest.raises(ConnectionError):
        runner.run("browser.open_url", {"url": "https://example.test"}, viewer_host_approved=True)


def test_implicit_active_tab_reconciles_closed_target_but_explicit_id_fails(
    fake_browser: tuple[ManagedBrowser, FakeConnection], monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, _connection = fake_browser
    monkeypatch.setattr(
        browser,
        "_validated_runtime",
        lambda: (
            {
                "profile_id": "default",
                "browser_path": "/devtools/browser/owned",
                "active_tab_id": "closed",
            },
            9222,
            "ws://127.0.0.1:9222/devtools/browser/owned",
            {},
        ),
    )
    assert browser.tabs()["active_tab_id"] == "page-1"
    assert browser._tab_connection({"profile_id": "default"})[0]["id"] == "page-1"
    with pytest.raises(KeyError):
        browser._tab_connection({"tab_id": "closed"})


def test_canonical_navigation_stays_private_before_start_and_after_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BrowserHostRunner(tmp_path)
    monkeypatch.setattr(
        "webbrowser.open", lambda *_args, **_kwargs: pytest.fail("personal browser opened")
    )
    payload = {"url": "https://example.test", "_rumi_contract_operation": "browser.navigate"}
    for _phase in ("before_start", "after_stop"):
        result = runner.run("browser.open_url", payload, viewer_host_approved=True)
        assert result["is_error"] is True
        tabs = runner.run(
            "browser.tabs",
            {"_rumi_contract_operation": "browser.tabs.list"},
            viewer_host_approved=True,
        )
        assert tabs["tabs"] == [] and tabs["running"] is False
        selected = runner.run(
            "browser.select_tab",
            {"tab_id": "missing", "_rumi_contract_operation": "browser.tab.select"},
            viewer_host_approved=True,
        )
        assert selected["is_error"] is True
        runner.run("browser.runtime.stop", {}, viewer_host_approved=True)


def test_cookie_exports_apply_aliases_domains_and_netscape_http_only() -> None:
    parsed = parse_import(
        {
            "format": "netscape",
            "content": (
                "# Netscape HTTP Cookie File\n"
                "#HttpOnly_.example.test\tTRUE\t/\tTRUE\t0\tsid\tprivate\n"
                "unrelated.test\tFALSE\t/\tFALSE\t0\tother\tignored\n"
            ),
            "domains": ["example.test"],
        }
    )
    assert len(parsed) == 1
    assert parsed[0]["http_only"] is True
    assert parsed[0]["expires_at"] is None
    assert "expires" not in to_cdp(parsed[0])
    exported = parse_import(
        {
            "format": "json",
            "content": json.dumps(
                {
                    "cookies": [
                        {
                            "name": "sid",
                            "value": "secret",
                            "domain": ".example.test",
                            "httpOnly": False,
                            "sameSite": "none",
                            "expirationDate": 1_900_000_000,
                        }
                    ]
                }
            ),
        }
    )
    assert exported[0]["same_site"] == "None"
    assert exported[0]["http_only"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"content": "bad", "format": "unknown"},
        {"content": "[]", "cookies": []},
        {"cookies": ["not an object"]},
        {"cookies": [{"name": "sid", "domain": "https://example.test"}]},
        {"cookies": [{"name": "sid", "domain": "example.test", "expires": float("nan")}]},
        {"cookies": [{"name": "sid", "domain": "example.test", "secure": "false"}]},
        {"cookies": [{"name": "sid", "domain": "example.test", "value": "a" * 16_385}]},
    ],
)
def test_cookie_import_rejects_malformed_or_unbounded_input(payload: dict[str, Any]) -> None:
    with pytest.raises((ValueError, json.JSONDecodeError)):
        parse_import(payload)


def test_cookie_import_is_live_and_values_stay_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BrowserHostRunner(tmp_path)
    applied: list[Any] = []
    cookie = parse_import(
        {"cookies": [{"name": "old", "domain": "example.test", "value": "old-secret"}]}
    )[0]
    monkeypatch.setattr(runner.managed, "get_cookies", lambda _profile: [cookie])
    monkeypatch.setattr(
        runner.managed,
        "set_cookies",
        lambda profile, cookies, replace: applied.append((profile, cookies, replace)) or True,
    )
    imported = runner.run(
        "browser.cookies.import",
        {"cookies": [{"name": "new", "domain": "example.test", "value": "new-secret"}]},
        viewer_host_approved=True,
    )
    assert imported["applied_to_browser"] is True and imported["count"] == 2
    assert applied[0][0] == "default" and applied[0][2] is False
    assert runner._read_state()["cookies"]["default"] == []
    assert "new-secret" not in runner.state_path.read_text(encoding="utf-8")
    listed = runner.run("browser.cookies.list", {}, viewer_host_approved=True)
    assert "old-secret" not in json.dumps(listed)
    assert listed["source"] == "managed_browser"


def test_staged_cookie_import_is_consumed_once_after_successful_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BrowserHostRunner(tmp_path)
    monkeypatch.setattr(runner.managed, "get_cookies", lambda _profile: None)
    monkeypatch.setattr(runner.managed, "set_cookies", lambda *_args, **_kwargs: False)
    runner.run(
        "browser.cookies.import",
        {
            "replace": True,
            "cookies": [{"name": "sid", "domain": "example.test", "value": "old-session"}],
        },
        viewer_host_approved=True,
    )
    started: list[tuple[Any, Any]] = []

    def start(_profile: str, cookies: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        started.append((cookies, kwargs))
        return {"running": True, "profile_id": "default"}

    monkeypatch.setattr(runner.managed, "start", start)
    for _restart in range(2):
        runner.run("browser.runtime.start", {}, viewer_host_approved=True)
    assert started[0][0][0]["value"] == "old-session"
    assert started[0][1]["replace_cookies"] is True
    assert started[1][0] == []
    assert started[1][1]["replace_cookies"] is False
    assert "old-session" not in runner.state_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        "../outside.js",
        "/absolute.js",
        "C:/outside.js",
        "folder\\outside.js",
        "a/../outside.js",
        "CON",
        "a./b.js",
    ],
)
def test_extension_files_cannot_escape_package(tmp_path: Path, name: str) -> None:
    files = extension_files()
    files[name] = "code"
    with pytest.raises(ValueError, match="filenames"):
        prepare_extension(tmp_path, "default", {"files": files})
    assert not (tmp_path / "extensions").exists()


def test_extension_validation_reports_permissions_and_limits(tmp_path: Path) -> None:
    directory, metadata = prepare_extension(tmp_path, "default", {"files": extension_files()})
    assert directory.is_relative_to(tmp_path / "extensions" / "default")
    assert metadata["permissions"] == ["storage"]
    assert metadata["host_permissions"] == ["http://127.0.0.1/*"]
    assert "path" not in metadata
    files = extension_files()
    files["large.js"] = "a" * (1024 * 1024 + 1)
    with pytest.raises(ValueError, match="limit"):
        prepare_extension(tmp_path, "default", {"files": files})
    with pytest.raises(ValueError, match="256"):
        prepare_extension(tmp_path, "default", {"files": {str(index): "x" for index in range(257)}})


def test_extension_install_list_remove_use_owned_packages(
    fake_browser: tuple[ManagedBrowser, FakeConnection],
) -> None:
    browser, connection = fake_browser
    installed = browser.extension_install({"files": extension_files()})
    assert installed["installed"] is True
    package = installed["extension"]
    load = next(
        params for method, params in connection.calls if method == "Extensions.loadUnpacked"
    )
    assert Path(load["path"]).is_relative_to(browser.root / "extensions" / "default")
    assert browser.extensions_list()["extensions"][0]["loaded"] is True
    removed = browser.extension_remove({"extension_id": package["extension_id"]})
    assert removed["removed"] is True
    assert not Path(load["path"]).exists()
    assert browser.extensions_list()["extensions"] == []


def test_unsupported_extension_api_is_truthful_and_cleans_staging(
    fake_browser: tuple[ManagedBrowser, FakeConnection],
) -> None:
    browser, connection = fake_browser
    connection.results["Extensions.loadUnpacked"] = cdp.CDPError("Extensions.loadUnpacked", -32601)
    result = browser.extension_install({"files": extension_files()})
    assert result["is_error"] is True and result["supported"] is False
    assert not list((browser.root / "extensions" / "default").iterdir())
    connection.results["Extensions.loadUnpacked"] = cdp.CDPError("Extensions.loadUnpacked", -32000)
    with pytest.raises(cdp.CDPError):
        browser.extension_install({"files": extension_files()})


def test_saved_extensions_list_while_stopped_or_another_profile_runs(
    fake_browser: tuple[ManagedBrowser, FakeConnection], monkeypatch: pytest.MonkeyPatch
) -> None:
    browser, connection = fake_browser
    browser.extension_install({"files": extension_files()})
    for status in ({"running": False}, {"running": True, "profile_id": "another"}):
        monkeypatch.setattr(browser, "status", lambda: status)
        connection.calls.clear()
        listed = browser.extensions_list({"profile_id": "default"})
        assert listed["running"] is False
        assert listed["extensions"][0]["installed"] is True
        assert listed["extensions"][0]["loaded"] is False
        assert connection.calls == []


def test_unsupported_extension_inventory_preserves_saved_packages(
    fake_browser: tuple[ManagedBrowser, FakeConnection],
) -> None:
    browser, connection = fake_browser
    browser.extension_install({"files": extension_files()})
    connection.results["Extensions.getExtensions"] = cdp.CDPError(
        "Extensions.getExtensions", -32601
    )
    listed = browser.extensions_list()
    assert not listed.get("is_error")
    assert listed["supported"] is False
    assert listed["extensions"][0]["installed"] is True
    assert listed["extensions"][0]["loaded"] is False


def test_storage_rejects_symlinked_profile_or_metadata(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "managed"
    root.mkdir()
    link = root / "profiles"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Windows account cannot create symlinks")
    with pytest.raises(PermissionError, match="links"):
        checked_path(root, link / "default")


def test_oversized_metadata_write_preserves_existing_state(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    write_json(tmp_path, state, {"value": "initial"})
    original = state.read_bytes()
    with pytest.raises(ValueError, match="storage limit"):
        write_json(tmp_path, state, {"value": "a" * MAX_METADATA_BYTES})
    assert state.read_bytes() == original
    assert not list(tmp_path.glob(".metadata.*"))


def test_aggregate_cookie_queue_rejects_overflow_without_bricking_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = BrowserHostRunner(tmp_path)
    monkeypatch.setattr(runner.managed, "get_cookies", lambda _profile: None)
    monkeypatch.setattr(runner.managed, "set_cookies", lambda *_args, **_kwargs: False)
    first = [
        {"name": f"cookie-{index}", "value": "x" * 12_000, "domain": "example.test"}
        for index in range(200)
    ]
    second = [
        {"name": f"cookie-{index}", "value": "x" * 12_000, "domain": "example.test"}
        for index in range(200, 400)
    ]
    runner.run("browser.cookies.import", {"cookies": first}, viewer_host_approved=True)
    original = runner.state_path.read_bytes()
    with pytest.raises(ValueError, match="storage limit"):
        runner.run("browser.cookies.import", {"cookies": second}, viewer_host_approved=True)
    assert runner.state_path.read_bytes() == original
    assert (
        runner.run("browser.profiles.list", {}, viewer_host_approved=True)["active_profile_id"]
        == "default"
    )


def test_dom_inspection_excludes_form_values_and_url_credentials() -> None:
    connection = FakeConnection(
        {
            "DOM.getDocument": {
                "root": {
                    "nodeId": 1,
                    "nodeName": "INPUT",
                    "attributes": [
                        "type",
                        "password",
                        "value",
                        "private-password",
                        "href",
                        "https://u:p@example.test/?token=private-token",
                    ],
                }
            },
            "Page.getLayoutMetrics": {"cssContentSize": {"width": 800, "height": 600}},
        }
    )
    result = devtools.inspect_page(connection)
    serialized = json.dumps(result)
    assert "private-password" not in serialized
    assert "private-token" not in serialized
    assert result["dom"]["attributes"]["type"] == "password"


def test_network_capture_uses_one_connection_and_redacts_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "1",
                "type": "Fetch",
                "request": {
                    "url": "https://user:private-password@example.test/api?token=private-query#private-fragment",
                    "method": "POST",
                    "headers": {"Authorization": "private-header", "Cookie": "private-cookie"},
                    "postData": "private-body",
                },
            },
        },
        {
            "method": "Network.responseReceived",
            "params": {
                "requestId": "1",
                "response": {
                    "status": 200,
                    "mimeType": "application/json",
                    "headers": {"Set-Cookie": "private-response-cookie"},
                },
            },
        },
        {
            "method": "Network.loadingFinished",
            "params": {"requestId": "1", "encodedDataLength": 128},
        },
        {
            "method": "Runtime.consoleAPICalled",
            "params": {"type": "log", "args": [{"value": "private-console"}]},
        },
        {
            "method": "Runtime.exceptionThrown",
            "params": {
                "exceptionDetails": {
                    "text": "private-error",
                    "url": "https://example.test/?token=private-source",
                }
            },
        },
    ]
    connection = FakeConnection(events=events)
    clock = [0.0]

    def tick() -> float:
        clock[0] += 0.01
        return clock[0]

    monkeypatch.setattr(devtools.time, "monotonic", tick)
    result = devtools.capture_network(connection, {"duration_ms": 500, "reload": True})
    assert result["requests"][0]["status"] == 200
    assert result["requests"][0]["finished"] is True
    assert result["console"][0]["argument_count"] == 1
    assert "private-" not in json.dumps(result)
    assert [method for method, _params in connection.calls] == [
        "Network.enable",
        "Runtime.enable",
        "Log.enable",
        "Page.reload",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"duration_ms": 10_001},
        {"duration_ms": True},
        {"duration_ms": 99},
        {"max_entries": 501},
        {"max_entries": False},
        {"reload": "yes"},
    ],
)
def test_network_capture_limits_are_validated_before_commands(payload: dict[str, Any]) -> None:
    connection = FakeConnection()
    with pytest.raises(ValueError):
        devtools.capture_network(connection, payload)
    assert connection.calls == []


def test_evaluation_is_bounded_and_exceptions_do_not_leak_payload() -> None:
    connection = FakeConnection({"Runtime.evaluate": {"result": {"type": "number", "value": 2}}})
    assert devtools.evaluate_page(connection, {"expression": "1 + 1"})["value"] == 2
    assert connection.calls[0][1]["timeout"] == 5_000
    with pytest.raises(ValueError):
        devtools.evaluate_page(connection, {"expression": "x" * 16_385})
    connection.results["Runtime.evaluate"] = {"exceptionDetails": {"text": "private exception"}}
    result = devtools.evaluate_page(connection, {"expression": "throw Error()"})
    assert result["is_error"] is True and "private exception" not in json.dumps(result)


def test_cdp_command_preserves_events_on_the_same_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    import websocket

    messages = deque(
        [
            json.dumps({"method": "Network.requestWillBeSent", "params": {"requestId": "1"}}),
            json.dumps({"id": 1, "result": {"ready": True}}),
        ]
    )
    opened: list[Any] = []

    class Socket:
        def send(self, _value: str) -> None:
            pass

        def recv(self) -> str:
            return messages.popleft()

        def settimeout(self, _value: float) -> None:
            pass

        def close(self) -> None:
            opened.append("closed")

    def connect(*args: Any, **kwargs: Any) -> Socket:
        opened.append((args, kwargs))
        return Socket()

    monkeypatch.setattr(websocket, "create_connection", connect)
    with cdp.CDPConnection("ws://127.0.0.1:9222/devtools/page/a", port=9222) as connection:
        assert connection.call("Network.enable")["ready"] is True
        assert connection.event(0.1)["method"] == "Network.requestWillBeSent"
    assert len(opened) == 2
    assert opened[0][1]["suppress_origin"] is True


@pytest.mark.skipif(
    os.environ.get("RUMI_BROWSER_LIVE_TEST") != "1",
    reason="opt-in isolated installed-Chromium smoke",
)
def test_live_managed_chromium_end_to_end(tmp_path: Path) -> None:
    if managed.find_executable() is None:
        pytest.skip("No installed Chromium executable")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = (
                b"<!doctype html><title>Tobkiri smoke</title><h1>Model browser</h1>"
                b"<input type=password value=PRIVATE_FORM_VALUE>"
                b"<script>console.log('PRIVATE_CONSOLE_VALUE');"
                b"fetch('/api?token=PRIVATE_QUERY_VALUE',{headers:{Authorization:'PRIVATE_HEADER_VALUE'}});</script>"
                if not self.path.startswith("/api")
                else b'{"ok":true}'
            )
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/html" if not self.path.startswith("/api") else "application/json",
            )
            self.send_header("Set-Cookie", "server_sid=PRIVATE_SERVER_COOKIE; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runner = BrowserHostRunner(tmp_path)

    def invoke(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        result = runner.run(action, payload or {}, viewer_host_approved=True)
        assert not result.get("is_error"), result
        return result

    try:
        invoke("browser.profile.create", {"profile_id": "smoke", "label": "Smoke"})
        started = invoke("browser.runtime.start", {"profile_id": "smoke", "headless": True})
        assert started["running"] and started["headless"]
        reopened = BrowserHostRunner(tmp_path)
        assert reopened.run("browser.runtime.status", {}, viewer_host_approved=True)["running"]
        opened = invoke(
            "browser.open_url",
            {
                "profile_id": "smoke",
                "url": f"http://127.0.0.1:{server.server_port}/?token=PRIVATE_NAV_QUERY",
            },
        )
        tab_id = opened["tab_id"]
        page = {"profile_id": "smoke", "tab_id": tab_id}
        assert any(
            tab["tab_id"] == tab_id
            for tab in invoke("browser.tabs", {"profile_id": "smoke"})["tabs"]
        )
        invoke("browser.select_tab", page)
        invoke(
            "browser.devtools.evaluate",
            {
                **page,
                "expression": "new Promise(resolve => setTimeout(() => resolve(document.title), 100))",
            },
        )
        inspected = invoke("browser.devtools.inspect", page)
        assert inspected["title"] == "Tobkiri smoke"
        assert "PRIVATE_FORM_VALUE" not in json.dumps(inspected)
        screenshot = invoke("browser.capture.page", page)
        assert base64.b64decode(screenshot["screenshot"]["data_url"].split(",", 1)[1]).startswith(
            b"\x89PNG"
        )
        imported = invoke(
            "browser.cookies.import",
            {
                "profile_id": "smoke",
                "format": "json",
                "content": json.dumps(
                    [
                        {
                            "name": "imported_sid",
                            "value": "PRIVATE_IMPORTED_COOKIE",
                            "domain": "127.0.0.1",
                            "secure": False,
                            "httpOnly": False,
                        }
                    ]
                ),
                "domains": ["127.0.0.1"],
            },
        )
        assert imported["applied_to_browser"]
        listed = invoke("browser.cookies.list", {"profile_id": "smoke"})
        assert any(cookie["name"] == "imported_sid" for cookie in listed["cookies"])
        assert "PRIVATE_IMPORTED_COOKIE" not in json.dumps(listed)
        evaluated = invoke(
            "browser.devtools.evaluate",
            {
                **page,
                "expression": "document.cookie.includes('imported_sid=PRIVATE_IMPORTED_COOKIE')",
            },
        )
        assert evaluated["value"] is True
        installed = runner.run(
            "browser.extensions.install",
            {"profile_id": "smoke", "files": extension_files()},
            viewer_host_approved=True,
        )
        extensions_supported = not installed.get("is_error")
        if not extensions_supported:
            assert installed.get("supported") is False, installed
        captured = invoke("browser.network.capture", {**page, "duration_ms": 700, "reload": True})
        assert captured["requests"]
        assert "PRIVATE_" not in json.dumps(captured)
        assert any("duration_ms" in request for request in captured["requests"])
        invoke(
            "browser.devtools.evaluate",
            {
                **page,
                "expression": "document.cookie='imported_sid=ROTATED_COOKIE; Max-Age=3600; Path=/'",
            },
        )
        invoke("browser.runtime.stop", {"profile_id": "smoke"})
        restarted = invoke("browser.runtime.start", {"profile_id": "smoke", "headless": True})
        assert restarted["running"]
        assert runner._read_state()["cookies"]["smoke"] == []
        reopened = invoke(
            "browser.open_url",
            {"profile_id": "smoke", "url": f"http://127.0.0.1:{server.server_port}/"},
        )
        page = {"profile_id": "smoke", "tab_id": reopened["tab_id"]}
        rotated = invoke(
            "browser.devtools.evaluate",
            {
                **page,
                "expression": "new Promise(resolve => setTimeout(() => resolve(document.cookie.includes('imported_sid=ROTATED_COOKIE')), 150))",
            },
        )
        assert rotated["value"] is True
        if extensions_supported:
            assert invoke("browser.extensions.list", {"profile_id": "smoke"})["extensions"][0][
                "loaded"
            ]
            assert (
                invoke(
                    "browser.devtools.evaluate",
                    {**page, "expression": "document.documentElement.dataset.tobkiriExtension"},
                )["value"]
                == "loaded"
            )
            invoke(
                "browser.extensions.remove",
                {"profile_id": "smoke", "extension_id": installed["extension"]["extension_id"]},
            )
        invoke("browser.cookies.delete", {"profile_id": "smoke", "name": "imported_sid"})
        assert not any(
            cookie["name"] == "imported_sid"
            for cookie in invoke("browser.cookies.list", {"profile_id": "smoke"})["cookies"]
        )
        print(
            json.dumps(
                {
                    "browser": started["browser"],
                    "live_tabs": True,
                    "dom": True,
                    "screenshot": True,
                    "live_cookie_import": True,
                    "network_requests": len(captured["requests"]),
                    "extensions_supported": extensions_supported,
                    "restart_preserves_rotated_cookies": True,
                    "restart_restores_extensions": extensions_supported,
                }
            )
        )
    finally:
        runner.run("browser.runtime.stop", {}, viewer_host_approved=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert not runner.managed.status()["running"]
