# Release workflow controls

## GitHub tag drafts

[The tag workflow](../.github/workflows/release.yml) requires synchronized
application versions and the exact `v<version>` tag. It builds only
`aarch64-apple-darwin` (Apple Silicon macOS). Tauri builds with `--no-sign`,
then the workflow ad-hoc signs the apps and the PackVM VZ helper and packages
the Launcher DMG. Draft artifacts have no Developer ID signature or Apple
notarization. A maintainer reviews the inventory and release notes before
publishing. See the [macOS distribution guide](../tobkiri_runtime/docs/macos-unsigned-distribution.md).

Each matrix target writes one immutable `release-target.json` containing the
actual checked-out source SHA, target triple, platform, architecture, and
SHA-256/byte-size records. The `gather` job receives the exact enabled target
set from the workflow's `TOBKIRI_REQUIRED_RELEASE_TARGETS` JSON array and
rejects missing, duplicate, replaced, or unexpected assets before writing one
sorted `release-inventory.json`. The gather create and verify commands expand
that same array into repeated `--required-target` arguments, and the array
must be non-empty and duplicate-free. GitHub build provenance attests the
single inventory subject exactly once before draft creation. The attestation
covers the inventory's build origin and the artifact digests it records.
Apple code signing and notarization are separate platform checks.

Adding Windows, Linux, or macOS x86_64 requires sealed packaging support, an
actual matrix job, and the same target in `TOBKIRI_REQUIRED_RELEASE_TARGETS`.
The release inventory library retains its four-target default contract for
compatibility.

## Manual builds

`scripts/build-and-sign.sh --mode production` is the manual macOS path. It
requires a Developer ID Application identity and performs notarization,
stapling, and Gatekeeper assessment. It archives `.app` bundles for `notarytool`
and staples the tickets to the original bundles. Supply the signing and
notarization credentials in the environment as `APPLE_CERTIFICATE_BASE64`,
`APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`,
`APPLE_PASSWORD` (the app-specific password), and `APPLE_TEAM_ID`.

`scripts/build-and-sign.sh --mode local-dev` creates unsigned or ad-hoc debug
output in the debug target directory. It exits before release sealing,
inventory, attestation, or upload.

`release_gate.py` checks versions and tags in the GitHub workflow and enforces
the signing policy and tool availability in the manual script without printing
secret values. Repository tests use mocked tool calls and synthetic target
artifacts.
