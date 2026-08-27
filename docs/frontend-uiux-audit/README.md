# rumiai frontend UI/UX audit

This directory is the reproducible entry point for the repository-wide frontend, UI/UX, accessibility, state-correctness, trust, privacy, and visual-quality audit.

The authoritative GitHub tracker is **#1069**. Detailed defects remain split into independently testable issues so each fix can be reviewed, validated, and closed with evidence.

## Audited surfaces

| Surface | Primary source root | Typical runtime |
|---|---|---|
| Tobkiri Launcher | `tobkiri_launcher/frontend` | React/Tauri/browser |
| defaultspack Web | `tobkiri_runtime/ecosystem/defaultspack/webapp` | React/browser/WebView |
| Search Home | `tobkiri_runtime/ecosystem/search_home_pack/webapp` | React/browser |
| Rumi Mobile | `tobkiri_runtime/ecosystem/tobkiri_mobile` | Flutter/iOS/Android |
| Browser Companion | `tobkiri_runtime/ecosystem/defaultspack/browser_extensions` | Chromium extension |
| Core setup/approval surfaces | `tobkiri_runtime/core_runtime` and related entry points | browser/WebView |

## What this change adds

- [`quality-contract.md`](quality-contract.md): non-negotiable product behavior and definition of done.
- [`visual-language.md`](visual-language.md): concrete rules for removing generic “AI template” styling and inconsistent micro-polish.
- [`manual-test-matrix.md`](manual-test-matrix.md): runtime evidence required beyond static review.
- [`issue-ledger.md`](issue-ledger.md): audit phases, priority order, and selected high-risk findings.
- `scripts/quality/audit_frontend_uiux.py`: a diff-aware static regression scanner.
- `scripts/quality/frontend_uiux_policy.json`: machine-readable scan scope.
- `scripts/quality/frontend_uiux_baseline.json`: expiring, issue-bound exceptions only.
- `.github/workflows/frontend-uiux-audit.yml`: CI for scanner tests and changed-line enforcement.

## Run locally

```bash
python scripts/quality/audit_frontend_uiux.py \
  --fail-on none \
  --json-output frontend-uiux-audit-report.json
```

Audit only lines changed from a base branch and fail on new high-risk findings:

```bash
git fetch origin soon
python scripts/quality/audit_frontend_uiux.py \
  --diff-from origin/soon \
  --fail-on error \
  --json-output frontend-uiux-audit-report.json
```

Run scanner tests:

```bash
cd scripts/quality
python -m unittest -v test_audit_frontend_uiux.py
```

## Baseline policy

A baseline entry is not a waiver. It must have:

1. one rule ID;
2. a repository-relative path glob;
3. an exact finding fingerprint or scoped source fragment;
4. a live GitHub issue;
5. an expiration date;
6. a specific reason.

Expired entries stop suppressing findings. Broad path-only exemptions are rejected by the scanner.

## What “complete” means

Code-complete does **not** mean that a component merely renders or that a happy-path test passes. A surface is complete only when:

- its user-visible state model is explicit;
- destructive and authority-bearing actions are revision-bound and recoverable;
- keyboard, focus, screen-reader, zoom, touch, reduced-motion, high-contrast, localization, and responsive behavior are covered where relevant;
- loading, empty, partial, stale, offline, conflict, authorization, and failure states are truthful;
- automated regression coverage exists;
- runtime evidence from the manual matrix is attached to the issue or PR.

## Static-audit boundary

The scanner finds risky source patterns and blocks new high-confidence regressions. It cannot prove computed contrast, actual focus order, native screen-reader output, WebView differences, animation smoothness, network race behavior, visual alignment, or every OS/browser combination. Those remain mandatory runtime checks rather than convenient optional garnish.
