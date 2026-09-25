from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
EXTENSION_ROOT = (
    ROOT / "ecosystem" / "defaultspack" / "browser_extensions" / "rumi_browser_companion"
)


class _PopupMarkupParser(HTMLParser):
    """Collect popup controls and accessibility attributes for smoke checks."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: dict[str, dict[str, str]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {name: value or "" for name, value in attrs}
        element_id = values.get("id")
        if element_id:
            self.elements[element_id] = {"tag": tag, **values}


def test_browser_companion_toolbar_manifest_and_accessibility_contract() -> None:
    """The toolbar action must always resolve to an accessible local popup."""
    manifest = json.loads((EXTENSION_ROOT / "manifest.json").read_text(encoding="utf-8"))
    popup = (EXTENSION_ROOT / "popup.html").read_text(encoding="utf-8")
    parser = _PopupMarkupParser()
    parser.feed(popup)

    assert manifest["name"] == "Tobkiri Browser Companion"
    assert manifest["action"]["default_title"] == "Tobkiri Browser Companion"
    assert manifest["action"]["default_popup"] == "popup.html"
    assert (EXTENSION_ROOT / manifest["action"]["default_popup"]).is_file()

    assert parser.elements["status-message"]["role"] == "status"
    assert parser.elements["status-message"]["aria-live"] == "polite"
    assert parser.elements["popup-error"]["role"] == "alert"
    for control_id in ("poll-now", "open-settings", "open-help"):
        assert parser.elements[control_id]["tag"] == "button"
        assert parser.elements[control_id]["type"] == "button"
    assert "Current-tab scope" in popup
    assert "Last successful poll" in popup


def test_browser_companion_background_exposes_safe_toolbar_status_contract() -> None:
    """Popup state and badges remain informational and never expose credentials."""
    background = (EXTENSION_ROOT / "background.js").read_text(encoding="utf-8")
    popup_state_body = background[
        background.index("async function getPopupState") : background.index(
            "function popupStatusForDisplay"
        )
    ]

    assert 'case "rumi:get-popup-state"' in background
    assert 'state: "connecting"' in background
    assert "LAST_SUCCESSFUL_POLL_KEY" in background
    assert "lastSuccessfulPollAt" in background
    assert "popupStatusForDisplay(status)" in popup_state_body
    assert "pairingToken:" not in popup_state_body
    assert 'badgeText: "OK"' in background
    assert 'badgeText: "SET"' in background
    assert 'badgeText: "ERR"' in background
    assert "chrome.action.setBadgeText" in background
    assert "chrome.action.setTitle" in background
    assert 'parsed.username = ""' in background
    assert 'parsed.password = ""' in background
    assert 'parsed.search = ""' in background
    assert 'parsed.hash = ""' in background
    assert "approved browser command" in background
    assert "approval_token" not in popup_state_body


def test_browser_companion_badge_and_public_projection_runtime_smoke() -> None:
    """Exercise badge mappings and credential-safe popup projection as JavaScript."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the Browser Companion badge smoke")

    script = textwrap.dedent(
        r"""
        const fs = require("fs");
        const vm = require("vm");
        const background = fs.readFileSync(process.argv[1], "utf8");
        const actionBlock = background.slice(
          background.indexOf("async function updateActionIndicator"),
          background.indexOf("async function getPopupState")
        );
        const projectionBlock = background.slice(
          background.indexOf("function popupStatusForDisplay"),
          background.indexOf("function normalizePollInterval")
        );
        const calls = [];
        const chrome = {
          action: {
            async setBadgeText(value) { calls.push(["badge", value.text]); },
            async setBadgeBackgroundColor(value) { calls.push(["color", value.color]); },
            async setTitle(value) { calls.push(["title", value.title]); }
          }
        };
        const context = { chrome, console, Object, String, URL };
        vm.createContext(context);
        vm.runInContext(`${actionBlock}\n${projectionBlock}`, context);
        const expect = (condition, message) => {
          if (!condition) throw new Error(message);
        };

        (async () => {
          for (const state of ["connected", "connecting", "not_configured", "bridge_error"]) {
            context.__state = state;
            await vm.runInContext("updateActionIndicator({ state: __state })", context);
          }
          const badges = calls.filter(([kind]) => kind === "badge").map(([, value]) => value);
          expect(JSON.stringify(badges) === JSON.stringify(["OK", "…", "SET", "ERR"]),
            `unexpected badges: ${JSON.stringify(badges)}`);

          context.__rawStatus = {
            ok: false,
            state: "bridge_error",
            message: "Sensitive credential-marker from raw bridge response",
            updatedAt: "2026-08-24T00:00:01.000Z",
            lastSuccessfulPollAt: "2026-08-24T00:00:00.000Z"
          };
          const projected = vm.runInContext("popupStatusForDisplay(__rawStatus)", context);
          expect(!JSON.stringify(projected).includes("credential-marker"), "raw bridge error leaked");
          expect(projected.state === "bridge_error", "error state was not preserved");
          expect(projected.lastSuccessfulPollAt === "2026-08-24T00:00:00.000Z",
            "last successful poll was not preserved");

          context.__endpoint = "http://user:password@127.0.0.1:8766/path?debug=private#fragment";
          const endpoint = vm.runInContext("endpointForDisplay(__endpoint)", context);
          expect(endpoint === "http://127.0.0.1:8766/path", `unsafe endpoint: ${endpoint}`);
        })().catch((error) => {
          console.error(error.stack || error);
          process.exitCode = 1;
        });
        """
    )
    subprocess.run(
        [node, "-e", script, str(EXTENSION_ROOT / "background.js")],
        cwd=ROOT,
        check=True,
        text=True,
    )


def test_browser_companion_toolbar_activation_and_status_display_smoke() -> None:
    """Execute the real popup script against a mocked MV3 browser surface."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the Browser Companion popup smoke")

    script = textwrap.dedent(
        r"""
        const fs = require("fs");
        const vm = require("vm");

        class MockElement {
          constructor(id) {
            this.id = id;
            this.textContent = "";
            this.hidden = false;
            this.disabled = false;
            this.dataset = {};
            this.listeners = {};
          }
          addEventListener(type, listener) {
            this.listeners[type] = listener;
          }
        }

        const ids = [
          "connection-card", "status-symbol", "status-title", "status-message",
          "endpoint", "profile", "last-success", "scope-title", "scope-origin",
          "scope-detail", "popup-error", "poll-now", "open-settings", "open-help"
        ];
        const elements = Object.fromEntries(ids.map((id) => [id, new MockElement(id)]));
        const documentListeners = {};
        const document = {
          getElementById(id) {
            return elements[id];
          },
          addEventListener(type, listener) {
            documentListeners[type] = listener;
          }
        };

        const messages = [];
        const openedTabs = [];
        let settingsOpens = 0;
        const popupState = {
          ok: true,
          status: {
            ok: true,
            state: "connected",
            message: "The local bridge responded to the latest poll.",
            lastSuccessfulPollAt: "2026-08-24T00:00:00.000Z"
          },
          configured: true,
          endpoint: "http://127.0.0.1:8766",
          profileLabel: "Work",
          currentTab: {
            available: true,
            label: "Example",
            origin: "https://example.com",
            detail: "Can inspect page content and act on this tab only when Tobkiri sends an approved browser command."
          }
        };
        const chrome = {
          runtime: {
            async sendMessage(message) {
              messages.push(message);
              if (message.type === "rumi:get-popup-state") return popupState;
              if (message.type === "rumi:poll-now") return popupState.status;
              throw new Error(`unexpected message: ${message.type}`);
            },
            async openOptionsPage() {
              settingsOpens += 1;
            },
            getURL(path) {
              return `chrome-extension://test-extension/${path}`;
            }
          },
          tabs: {
            async create(options) {
              openedTabs.push(options);
            }
          }
        };
        const context = {
          chrome,
          console,
          Date,
          document,
          Error,
          Number,
          Object,
          String,
          setImmediate
        };
        vm.createContext(context);
        vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context, {
          filename: "popup.js"
        });

        const flush = () => new Promise((resolve) => setImmediate(resolve));
        const expect = (condition, message) => {
          if (!condition) throw new Error(message);
        };

        (async () => {
          documentListeners.DOMContentLoaded();
          await flush();
          expect(elements["status-title"].textContent === "Connected", "connected state missing");
          expect(elements["status-symbol"].textContent === "✓", "connected symbol missing");
          expect(elements.endpoint.textContent === "http://127.0.0.1:8766", "endpoint missing");
          expect(elements.profile.textContent === "Work", "profile missing");
          expect(elements["scope-title"].textContent === "Example", "tab label missing");
          expect(elements["scope-origin"].textContent === "https://example.com", "origin missing");
          expect(elements["last-success"].textContent !== "Never", "last success missing");

          const states = [
            ["connecting", "Connecting", "…"],
            ["not_configured", "Setup required", "?"],
            ["bridge_error", "Connection error", "!"],
            ["connected", "Connected", "✓"]
          ];
          for (const [state, title, symbol] of states) {
            context.__status = { state };
            vm.runInContext("renderStatus(__status)", context);
            expect(elements["status-title"].textContent === title, `${state} title missing`);
            expect(elements["status-symbol"].textContent === symbol, `${state} symbol missing`);
          }

          elements["open-settings"].listeners.click();
          elements["open-help"].listeners.click();
          elements["poll-now"].listeners.click();
          await flush();
          await flush();
          expect(settingsOpens === 1, "settings action did not run");
          expect(openedTabs[0].url.endsWith("/help.html"), "help action did not run");
          expect(messages.some((item) => item.type === "rumi:poll-now"), "manual poll did not run");
          expect(elements["poll-now"].disabled === false, "manual poll stayed disabled");

          vm.runInContext('renderUnavailable(new Error("background stopped"))', context);
          expect(elements["popup-error"].hidden === false, "error alert stayed hidden");
          expect(elements["status-title"].textContent === "Connection error", "error state missing");
        })().catch((error) => {
          console.error(error.stack || error);
          process.exitCode = 1;
        });
        """
    )
    subprocess.run(
        [node, "-e", script, str(EXTENSION_ROOT / "popup.js")],
        cwd=ROOT,
        check=True,
        text=True,
    )
