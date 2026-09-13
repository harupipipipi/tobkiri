from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from audit_frontend_uiux import (
    AuditPolicy,
    AuditConfigurationError,
    BaselineEntry,
    ChangedLineMap,
    _apply_baseline,
    _is_excluded,
    _parse_diff,
    load_baseline,
    load_policy,
    scan_text,
    should_fail,
)


class FrontendUiUxAuditTests(unittest.TestCase):
    def test_production_policy_excludes_unit_fixtures_not_components(self) -> None:
        policy = load_policy(Path(__file__).with_name("frontend_uiux_policy.json"))
        root = Path("tobkiri_launcher/frontend/src/components/ui")
        self.assertTrue(_is_excluded(root / "Button.test.tsx", policy))
        self.assertFalse(_is_excluded(root / "Button.tsx", policy))

    def test_generated_bundle_glob_does_not_exclude_authored_source(self) -> None:
        policy = AuditPolicy(
            source_roots=("app",),
            extensions=frozenset({".js"}),
            exclude_path_parts=frozenset(),
            exclude_globs=("app/web/assets/**",),
        )

        self.assertTrue(_is_excluded(Path("app/web/assets/app-hash.js"), policy))
        self.assertFalse(_is_excluded(Path("app/frontend/src/App.js"), policy))

    def test_detects_high_risk_transport_and_noop_button(self) -> None:
        source = '''
export function Example() {
  window.postMessage({ query, candidates }, "*");
  return <button type="button" className="primary">Run</button>;
}
'''
        rules = {item.rule for item in scan_text("src/Example.tsx", source)}
        self.assertIn("security.wildcard-postmessage", rules)
        self.assertIn("ux.enabled-noop-button", rules)

    def test_action_button_is_not_reported_as_noop(self) -> None:
        source = '<button type="button" onClick={() => run()}>Run</button>'
        rules = {item.rule for item in scan_text("src/Example.tsx", source)}
        self.assertNotIn("ux.enabled-noop-button", rules)

    def test_qr_import_and_type_field_are_not_a_secret_payload(self) -> None:
        source = '''
import QRCode from "qrcode";
type MobilePairQrPayload = { pickupSecret: string };
const qr = QRCode.toDataURL("value");
'''
        rules = {item.rule for item in scan_text("src/Pairing.tsx", source)}
        self.assertNotIn("security.plaintext-secret-qr", rules)

    def test_expiring_mobile_pairing_secret_is_allowed(self) -> None:
        source = '''
import QRCode from "qrcode";
const qrPayload = {
  kind: "rumi_mobile_pair_v1",
  pickupSecret: pairing.pickup_secret,
  expiresAt: pairing.expires_at,
};
const qrValue = JSON.stringify(qrPayload);
QRCode.toDataURL(qrValue);
'''
        rules = {item.rule for item in scan_text("src/Pairing.tsx", source)}
        self.assertNotIn("security.plaintext-secret-qr", rules)

    def test_qr_secret_still_requires_the_pairing_contract(self) -> None:
        source = '''
import QRCode from "qrcode";
const qrPayload = { kind: "custom", pickupSecret: reusableSecret };
const qrValue = JSON.stringify(qrPayload);
QRCode.toDataURL(qrValue);
'''
        rules = {item.rule for item in scan_text("src/Pairing.tsx", source)}
        self.assertIn("security.plaintext-secret-qr", rules)

    def test_qr_api_key_is_still_reported(self) -> None:
        source = '''
import QRCode from "qrcode";
const qrPayload = { kind: "custom", apiKey: configuredApiKey };
const qrValue = JSON.stringify(qrPayload);
QRCode.toDataURL(qrValue);
'''
        rules = {item.rule for item in scan_text("src/Pairing.tsx", source)}
        self.assertIn("security.plaintext-secret-qr", rules)

    def test_direct_qr_encoder_secret_is_still_reported(self) -> None:
        source = 'QRCode.toDataURL(apiKey);'
        rules = {item.rule for item in scan_text("src/Pairing.tsx", source)}
        self.assertIn("security.plaintext-secret-qr", rules)

    def test_flutter_qr_encoder_secret_is_still_reported(self) -> None:
        source = 'QrImage(data: accessToken);'
        rules = {item.rule for item in scan_text("src/Pairing.dart", source)}
        self.assertIn("security.plaintext-secret-qr", rules)

    def test_quoted_qr_secret_field_is_still_reported(self) -> None:
        source = '''
const qrPayload = { "apiKey": configuredApiKey };
const qrValue = JSON.stringify(qrPayload);
QRCode.toDataURL(qrValue);
'''
        rules = {item.rule for item in scan_text("src/Pairing.tsx", source)}
        self.assertIn("security.plaintext-secret-qr", rules)

    def test_scripted_inline_document_requires_review(self) -> None:
        source = '<iframe sandbox="allow-scripts" srcDoc={untrustedHtml} />'
        rules = {item.rule for item in scan_text("src/Example.tsx", source)}
        self.assertIn("security.scripted-inline-document", rules)

    def test_opaque_origin_external_frame_is_not_an_inline_document(self) -> None:
        source = '<iframe src={isolatedPackUrl} sandbox="allow-scripts" />'
        rules = {item.rule for item in scan_text("src/Example.tsx", source)}
        self.assertNotIn("security.scripted-inline-document", rules)

    def test_icon_button_requires_name(self) -> None:
        source = '<button type="button" onClick={close}><X size={16} /></button>'
        rules = {item.rule for item in scan_text("src/Dialog.tsx", source)}
        self.assertIn("a11y.icon-button-name", rules)

    def test_diff_parser_keeps_only_new_line_numbers(self) -> None:
        parsed = _parse_diff(
            """diff --git a/src/a.tsx b/src/a.tsx
--- a/src/a.tsx
+++ b/src/a.tsx
@@ -2,0 +3,2 @@
+one
+two
@@ -10,1 +12,1 @@
-old
+new
"""
        )
        self.assertEqual(parsed.lines_by_path["src/a.tsx"], frozenset({3, 4, 12}))

    def test_changed_line_map_checks_multiline_overlap(self) -> None:
        finding = scan_text(
            "src/a.tsx",
            '<button\n type="button"\n className="x"\n>Run</button>',
        )[0]
        changed = ChangedLineMap({"src/a.tsx": frozenset({2})})
        self.assertTrue(changed.includes(finding))

    def test_baseline_requires_issue_expiry_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": [
                            {
                                "rule": "security.wildcard-postmessage",
                                "path": "src/**",
                                "issue": 1,
                                "expires": "2099-01-01",
                                "reason": "temporary",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(AuditConfigurationError):
                load_baseline(path, today=dt.date(2026, 7, 10))

    def test_scoped_baseline_marks_only_matching_finding(self) -> None:
        finding = next(
            item
            for item in scan_text("src/a.tsx", 'window.postMessage(payload, "*");')
            if item.rule == "security.wildcard-postmessage"
        )
        entry = BaselineEntry(
            rule=finding.rule,
            path="src/a.tsx",
            fingerprint=finding.fingerprint,
            contains=None,
            issue=123,
            expires=dt.date(2099, 1, 1),
            reason="tracked",
        )
        result = _apply_baseline(finding, [entry])
        self.assertTrue(result.baselined)
        self.assertEqual(result.baseline_issue, 123)

    def test_failure_threshold_ignores_baselined_findings(self) -> None:
        finding = next(
            item
            for item in scan_text("src/a.tsx", 'window.postMessage(payload, "*");')
            if item.rule == "security.wildcard-postmessage"
        )
        self.assertTrue(should_fail([finding], "error"))
        self.assertFalse(
            should_fail([finding.__class__(**{**finding.__dict__, "baselined": True})], "error")
        )


if __name__ == "__main__":
    unittest.main()
