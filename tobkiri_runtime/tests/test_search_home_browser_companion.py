from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_browser_companion_background_supports_search_home_candidate_navigation():
    extension_root = (
        DEFAULTSPACK_ROOT
        / "browser_extensions"
        / "rumi_browser_companion"
    )
    background = (extension_root / "background.js").read_text(encoding="utf-8")
    policy = (extension_root / "search_home_destination_policy.js").read_text(encoding="utf-8")

    assert 'import "./search_home_destination_policy.js"' in background
    assert "rumi:search-home:set-route-state" in background
    assert "rumi:search-home:get-route-state" in background
    assert "rumi:search-home:advance-candidate" in background
    assert "SEARCH_HOME_ROUTE_STATE_KEY" in background
    assert 'chrome.tabs.update(tabId, { url })' in background
    assert "trustedSearchHomeSourceOrigin" in background
    assert "isTrustedStoredSearchHomeRouteState(current)" in background
    assert 'result.verdict === "allow" ? RumiSearchHomeDestinationPolicy.safeForPersistence(result.url) : ""' in background
    assert "evaluateRedirect" in policy
    assert "embedded_credentials" in policy


def test_browser_companion_options_honor_reduced_motion():
    extension_root = (
        DEFAULTSPACK_ROOT
        / "browser_extensions"
        / "rumi_browser_companion"
    )
    styles = (extension_root / "options.css").read_text(encoding="utf-8")
    readme = (extension_root / "README.md").read_text(encoding="utf-8")

    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "animation-duration: 0.01ms !important" in styles
    assert "animation-iteration-count: 1 !important" in styles
    assert "scroll-behavior: auto !important" in styles
    assert "transition-duration: 0.01ms !important" in styles
    assert "## Motion accessibility" in readme


def test_browser_companion_content_script_captures_search_home_hotkeys():
    content = (
        DEFAULTSPACK_ROOT
        / "browser_extensions"
        / "rumi_browser_companion"
        / "content_script.js"
    ).read_text(encoding="utf-8")

    assert 'window.addEventListener("message"' in content
    assert 'type: "rumi:search-home:set-route-state"' in content
    assert 'message.source === SEARCH_HOME_MESSAGE_SOURCE' in content
    assert "event.origin === window.location.origin" in content
    assert "source_origin: event.origin" in content
    assert 'type: "rumi:search-home:get-route-state"' in content
    assert 'window.addEventListener(\n    "keydown"' in content
    assert 'event.key === "ArrowRight"' in content
    assert 'event.key === "ArrowLeft"' in content
    assert 'event.key === "Enter"' in content
    assert "searchHomeRouteStateExpiresAt = Number.isFinite(expiresAt) ? expiresAt : 0" in content
    assert "refreshSearchHomeRouteState();" in content
    assert 'action: event.key === "ArrowLeft" ? "prev" : event.key === "ArrowRight" ? "next" : "open"' in content
    assert "event.preventDefault()" in content


def test_browser_companion_destination_policy_rejects_tampered_state_and_requires_confirmation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to exercise the browser companion destination policy")

    policy_path = (
        DEFAULTSPACK_ROOT
        / "browser_extensions"
        / "rumi_browser_companion"
        / "search_home_destination_policy.js"
    )
    script = textwrap.dedent(
        """
        require(process.argv[1]);
        const policy = globalThis.RumiSearchHomeDestinationPolicy;
        const cases = [
          ["https://example.com/path", "allow"],
          ["http://example.com/path", "confirm"],
          ["https://例え.テスト/path", "confirm"],
          ["https://xn--r8jz45g.xn--zckzah/path", "confirm"],
          ["javascript:alert(1)", "block"],
          ["data:text/html,fake", "block"],
          ["file:///tmp/fake-secret", "block"],
          ["custom://example.com/path", "block"],
          ["/relative", "block"],
          ["https://fake-user:fake-password@example.com/", "block"],
          ["https://example.com/%0d%0aheader", "block"],
          ["http://localhost:3000/", "block"],
          ["http://2130706433/", "block"],
          ["http://169.254.169.254/latest/meta-data/", "block"],
          ["http://[::1]/", "block"],
        ];
        for (const [url, verdict] of cases) {
          const actual = policy.evaluate(url).verdict;
          if (actual !== verdict) throw new Error(`${url}: expected ${verdict}, got ${actual}`);
        }
        if (policy.evaluateRedirect("https://example.com/a", "https://example.net/b", true).verdict !== "confirm") {
          throw new Error("cross-origin redirect did not require confirmation");
        }
        if (policy.evaluateRedirect("https://example.com/a", "http://127.0.0.1/b", true).verdict !== "block") {
          throw new Error("unsafe redirect target was not blocked");
        }
        const trusted = ["http://127.0.0.1:8777"];
        if (!policy.isTrustedSearchHomeOrigin("http://127.0.0.1:8777", trusted)) {
          throw new Error("manifest Search Home origin was not trusted");
        }
        for (const origin of ["http://127.0.0.1:38777", "http://192.168.1.20:8777", "https://example.com"]) {
          if (policy.isTrustedSearchHomeOrigin(origin, trusted)) {
            throw new Error(`${origin} was trusted outside the manifest contract`);
          }
        }
        """
    )
    subprocess.run([node, "-e", script, str(policy_path)], cwd=ROOT, check=True, text=True)

    manifest = json.loads((policy_path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["x_rumi_search_home_origins"] == ["http://127.0.0.1:8777"]


def test_browser_companion_background_search_home_action_contract():
    background = (
        DEFAULTSPACK_ROOT
        / "browser_extensions"
        / "rumi_browser_companion"
        / "background.js"
    ).read_text(encoding="utf-8")

    assert "function normalizeSearchHomeRouteAction" in background
    assert 'value === "previous" || value === "prev" || value === "left"' in background
    assert 'value === "open" || value === "enter"' in background
    assert 'normalizedAction === "open"' in background


def test_browser_companion_content_script_restores_search_home_state_after_navigation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to exercise the browser companion content script lifecycle")

    content_path = (
        DEFAULTSPACK_ROOT
        / "browser_extensions"
        / "rumi_browser_companion"
        / "content_script.js"
    )
    script = textwrap.dedent(
        """
        const fs = require("fs");
        const vm = require("vm");

        const content = fs.readFileSync(process.argv[1], "utf8");
        const listeners = {};
        const runtimeMessages = [];
        const now = Date.now();
        const chrome = {
          runtime: {
            sendMessage(message, callback) {
              runtimeMessages.push(message);
              if (message.type === "rumi:search-home:get-route-state") {
                const response = { ok: true, active: true, expires_at: now + 60_000 };
                if (callback) {
                  setImmediate(() => callback(response));
                }
                return undefined;
              }
              if (message.type === "rumi:search-home:advance-candidate") {
                if (callback) {
                  callback({ ok: true });
                }
                return undefined;
              }
              if (callback) {
                callback({ ok: true });
              }
              return undefined;
            },
            onMessage: {
              addListener(listener) {
                listeners.runtime = listener;
              }
            }
          }
        };
        const window = {
          addEventListener(type, handler) {
            listeners[type] = listeners[type] || [];
            listeners[type].push(handler);
          },
          innerWidth: 1280,
          innerHeight: 720,
          scrollX: 0,
          scrollY: 0
        };
        const document = {
          title: "Search Home candidate",
          body: {},
          documentElement: {}
        };
        const context = {
          console,
          Date,
          setImmediate,
          setTimeout,
          clearTimeout,
          chrome,
          window,
          document,
          location: { href: "https://example.com/result" },
          NodeFilter: { SHOW_ELEMENT: 1 },
          HTMLElement: class HTMLElement {},
          Element: class Element {},
          MouseEvent: class MouseEvent {},
          KeyboardEvent: class KeyboardEvent {},
          Event: class Event {}
        };
        vm.createContext(context);
        vm.runInContext(content, context, { filename: "content_script.js" });

        setTimeout(() => {
          const keydown = (listeners.keydown || [])[0];
          if (!keydown) {
            throw new Error("keydown listener was not registered");
          }
          let prevented = false;
          let stopped = false;
          keydown({
            key: "ArrowRight",
            preventDefault() {
              prevented = true;
            },
            stopPropagation() {
              stopped = true;
            }
          });
          if (!runtimeMessages.some((message) => message.type === "rumi:search-home:get-route-state")) {
            throw new Error("content script did not restore Search Home state from background");
          }
          const advance = runtimeMessages.find((message) => message.type === "rumi:search-home:advance-candidate");
          if (!advance || advance.action !== "next") {
            throw new Error(`unexpected advance message: ${JSON.stringify(advance)}`);
          }
          if (!prevented || !stopped) {
            throw new Error("restored Search Home hotkey was not captured");
          }
        }, 20);
        """
    )
    subprocess.run(
        [node, "-e", script, str(content_path)],
        cwd=ROOT,
        check=True,
        text=True,
    )
