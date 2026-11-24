# Release workflow controls

The release workflow has two explicit modes:

- `production` is the only mode that can create a release package. It requires
  synchronized application versions and the exact `v<version>` tag. macOS
  requires a Developer ID Application identity, notarization, stapling, and
  Gatekeeper assessment; `.app` bundles are archived for `notarytool` and the
  ticket is stapled back onto the app bundle;
  Windows requires Authenticode signing and verification with an HTTPS
  timestamp service when a Windows packaging job is enabled. Linux has no
  platform certificate requirement.
- `local-dev` is an explicit debug-only path in `build-and-sign.sh`. Its
  unsigned or ad-hoc output is written under the debug build target and is not
  sealed, inventoried, attested, or eligible for upload.

`release_gate.py` is the canonical version and signing-policy gate used by the
workflow and `build-and-sign.sh`. It validates secret presence and tool
availability without printing secret values.

Each matrix target writes one immutable `release-target.json` containing the
actual checked-out source SHA, target triple, platform, architecture, and
SHA-256/byte-size records. The `gather` job receives the exact enabled target
set from the workflow's `TOBKIRI_REQUIRED_RELEASE_TARGETS` JSON array and
rejects missing, duplicate, replaced, or unexpected assets before writing one
sorted `release-inventory.json`. The gather create and verify commands expand
that same array into repeated `--required-target` arguments, and the array
must be non-empty and duplicate-free. GitHub artifact provenance attests that
single inventory subject exactly once. Only after the inventory is verified
and attested does the workflow create the reviewable draft release.

The current tag workflow packages `aarch64-apple-darwin`. Adding Windows,
Linux, or macOS x86_64 requires adding the real packaging/signing matrix job and
the same target to `TOBKIRI_REQUIRED_RELEASE_TARGETS` in one change. The
release inventory library retains and tests the complete four-target default
contract for compatibility; it does not claim an artifact was built when no
matrix job produced it. The current one-target publication is intentional:
the repository's sealed release packaging guard does not yet support the
other targets, so expanding the matrix without those real packaging jobs
would make the tag workflow fail rather than produce valid releases.

Required production credentials are supplied through GitHub Actions secrets and
are never hard-coded:

- macOS: `APPLE_CERTIFICATE_BASE64`, `APPLE_CERTIFICATE_PASSWORD`,
  `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, and
  `APPLE_TEAM_ID`.
- Windows: `WINDOWS_CERTIFICATE_BASE64`, `WINDOWS_CERTIFICATE_PASSWORD`, and
  the fixed HTTPS timestamp service configured by the workflow.

Local production runs need the same credentials and platform tools. No actual
credential-backed signing is performed by the repository tests; tests use
mocked tool calls and synthetic target artifacts.
