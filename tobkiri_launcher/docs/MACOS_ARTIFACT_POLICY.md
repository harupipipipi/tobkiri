# macOS artifact trust domains

Tobkiri Launcher has two explicit macOS artifact policies. They are selected
at build time and compiled into the launcher; runtime flags cannot change the
selection.

`production-v1` is the only publishable policy. Release-profile builds require
an exact Apple Team ID. Startup accepts sealed Python only after strict outer
code/resource validation succeeds against the production bundle identifier,
Apple's Developer ID chain, the Developer ID Application certificate OID, and
the compiled Team ID. The release workflow additionally notarizes the result.

`ci-e2e-v1` exists only to exercise a packaged app when no Apple identity is
available. It uses the visibly distinct `Tobkiri Launcher CI E2E` name and
`dev.tobkiri.launcher.ci-e2e` identifier and contains signed non-publishable
policy markers. Each workflow run creates an ephemeral self-signed Ed25519
certificate and key in `RUNNER_TEMP` without importing either into a keychain
or changing trust settings. The certificate SHA-256 and public key are compiled
into the launcher. Before the final ad-hoc outer signature, the workflow signs
the fixed startup-critical file identities. Runtime requires strict outer
code/resource integrity, an ad-hoc outer signature, the exact certificate
bytes, the exact signed file list, and a valid Ed25519 attestation.

The CI certificate does not grant production authority. Production verification
rejects the CI identifier and every CI marker, certificate, and attestation.
The CI workflow artifact name also contains `non-publishable-ci-e2e`, and the
release workflow accepts only its independently produced Developer ID artifacts.
