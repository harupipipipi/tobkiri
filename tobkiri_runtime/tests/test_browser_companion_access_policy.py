from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
EXTENSION_ROOT = (
    ROOT
    / "ecosystem"
    / "defaultspack"
    / "browser_extensions"
    / "rumi_browser_companion"
)


def test_browser_companion_access_policy_fails_closed() -> None:
    """Exercise paused, unpaired, origin, permission, and incognito policy."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to exercise the browser access policy")

    policy_path = EXTENSION_ROOT / "browser_access_policy.js"
    script = textwrap.dedent(
        """
        require(process.argv[1]);
        const policy = globalThis.TobkiriBrowserAccessPolicy;
        const paired = {
          enabled: true,
          consentAcknowledged: true,
          pairingToken: "token",
          allowedOrigins: ["https://allowed.example"],
          deniedOrigins: ["https://denied.example"]
        };

        const expectReason = (actual, reason) => {
          if (actual.allowed || actual.reason !== reason) {
            throw new Error(`expected ${reason}, got ${JSON.stringify(actual)}`);
          }
        };

        expectReason(policy.canPoll({ ...paired, enabled: false }), "paused");
        expectReason(policy.canPoll({ ...paired, pairingToken: "" }), "unpaired");
        expectReason(
          policy.evaluateUrl("https://unknown.example/page", paired, {}),
          "origin_not_allowed"
        );
        expectReason(
          policy.evaluateUrl("https://denied.example/page", {
            ...paired,
            allowedOrigins: ["https://denied.example"]
          }, {}),
          "origin_denied"
        );
        expectReason(
          policy.evaluateUrl("https://allowed.example/page", paired, {
            hasHostPermission: false
          }),
          "host_permission_denied"
        );
        expectReason(
          policy.evaluateUrl("https://allowed.example/page", paired, {
            incognito: true,
            hasHostPermission: true
          }),
          "incognito_blocked"
        );
        expectReason(
          policy.evaluateUrl("chrome://settings", paired, {
            hasHostPermission: true
          }),
          "unsupported_scheme"
        );

        const allowed = policy.evaluateUrl(
          "https://allowed.example/page",
          paired,
          { hasHostPermission: true }
        );
        if (!allowed.allowed || allowed.origin !== "https://allowed.example") {
          throw new Error(`allowed origin was rejected: ${JSON.stringify(allowed)}`);
        }
        """
    )
    subprocess.run(
        [node, "-e", script, str(policy_path)],
        cwd=ROOT,
        check=True,
        text=True,
    )


def test_browser_companion_uses_optional_site_access_and_visible_controls() -> None:
    """Keep broad browsing access optional and expose persistent controls."""
    manifest = json.loads(
        (EXTENSION_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    background = (EXTENSION_ROOT / "background.js").read_text(encoding="utf-8")
    options_html = (EXTENSION_ROOT / "options.html").read_text(encoding="utf-8")
    options_js = (EXTENSION_ROOT / "options.js").read_text(encoding="utf-8")

    assert manifest["optional_host_permissions"] == ["http://*/*", "https://*/*"]
    assert "<all_urls>" not in manifest.get("host_permissions", [])
    assert all(
        "<all_urls>" not in script.get("matches", [])
        for script in manifest.get("content_scripts", [])
    )
    assert "scripting" in manifest["permissions"]

    assert 'import "./browser_access_policy.js"' in background
    assert "TobkiriBrowserAccessPolicy.canPoll(settings)" in background
    assert "authorizeTargetTab" in background
    assert "recordActivity" in background
    assert "chrome.action.setBadgeText" in background
    assert "chrome.permissions.onRemoved.addListener" in background

    for control_id in (
        'id="control-enabled"',
        'id="consent-acknowledged"',
        'id="allowed-origins"',
        'id="denied-origins"',
        'id="authorized-instance"',
        'id="active-scope"',
        'id="last-poll"',
        'id="recent-activity"',
    ):
        assert control_id in options_html

    assert "chrome.permissions.request" in options_js
    assert "Permission was not granted" in options_js
    assert "renderActivity" in options_js
