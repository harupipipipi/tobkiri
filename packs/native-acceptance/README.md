# Native iframe and Dangerous review acceptance fixture

This opt-in fixture uses the ordinary verified Pack/Profile, Core received-UI,
MessageChannel and native approval paths. It adds no host command, capability,
debug principal, approval flag, global IPC bridge or relaxed CSP. Source
preparation does not establish native acceptance.

`ui/pack.json` binds the actual UI asset bytes. The UI is a data Pack with one
declared provider Port and no filesystem, network, process, secret, native or
cross-Profile storage permission. It deliberately has no HTML CSP meta tag:
the network/IPC probes must observe the native host's enforced response CSP.
The copied `ports.js`, `float.js` and generic core WIT must remain byte-identical
to the reviewed SDK/core sources; the staging tool checks those copies.

The separate provider is a Wasm Pack owning
`local.native-acceptance-provider.review@1.0`. `ping` returns a fixed readiness
string. `open-global` calls the existing
`tobkiri:core/storage@1.0.0.open-user-global("native-acceptance-probe")`, then
drops the returned resource. It does not access application values. Approval
can create the dedicated account-root database and its schema. The provider
alone declares cross-Profile storage; the Profile ceiling and explicit provider
limit include that permission. The UI receives no borrowed provider permission.

Gate creates the actual `UserGlobalStorage { operation: Open }` effect, assigns
`Risk::Dangerous` and requires fresh approval on every call. NativeHost passes
the complete typed prompt to `trusted_review`, whose `dangerous` result selects
`NSAlertStyle::Critical` in the existing macOS sheet. The fixture never creates
an ApprovalPrompt or claims a browser flag can request the warning style.

## Root-owned preparation and execution

No component, dependency lock or native execution receipt is fabricated here.
The root build lease must resolve/review the isolated `provider/Cargo.toml` lock
and build its real component using the existing Rust 1.94.0 / wasm32-wasip2
toolchain. Its independent `[workspace]` leaves the shared host and Pack
workspaces/locks unchanged. For example, after producing the isolated lock:

```sh
cargo build --offline --locked \
  --manifest-path packs/native-acceptance/provider/Cargo.toml \
  --target wasm32-wasip2 --target-dir "$ACCEPTANCE_TARGET"
node packs/native-acceptance/stage.mjs \
  --component "$ACCEPTANCE_TARGET/wasm32-wasip2/debug/tobkiri_pack_native_acceptance_provider.wasm" \
  --output "$NEW_ACCEPTANCE_STAGING_DIRECTORY"
```

The output parent must exist; the output directory must not exist. Staging
copies only fixture assets and the supplied real component, hashes every
admitted file, creates complete Pack manifests, and pins a Profile to their
StrictV1 manifest hashes and absolute local sources. `stage-receipt.json`
records source/artifact identities but explicitly does not certify compiler,
exact component ABI, native execution or sheet appearance.

Use the actual Core CLI `pack verify` on both output Pack directories, then
`profile create --definition <output>/native-acceptance.profile.json
--name "Native acceptance"`. Inspect and answer the real CLI permission review.
The CLI creates a fresh Profile identity; use its returned ID in the native
Launcher. CLI consent for the provider's declared cross-Profile permission and
the later per-effect native Dangerous sheet are separate observations. No
native product DataRoot is launched or modified by the staging tool.

## Native observations to record

Open the actual opt-in Core Launcher and start the freshly created Profile.
Check that the fixture is a sandboxed `allow-scripts` iframe without
`allow-same-origin`, and that assets carry the native `PACK_UI_CSP` response
header. A browser preview, source check, mock or jsdom run does not prove this.

1. Run the iframe boundary probes. Parent document/IPC access and local/session
   storage should throw `SecurityError`. Global Tauri IPC objects should be
   absent. The fixed empty-body IPC and `.invalid` network requests must produce
   a trusted, enforced `connect-src` CSP violation. A failed fetch without that actual
   CSP event is recorded as **inconclusive**, never as a successful denial.
   No credential, referrer, request payload or application value is transmitted.
2. Check the declared provider Port. The actual one-use Pack channel must return
   `native-acceptance-provider-ready`. No Core session token, namespace, receiver
   or host ticket is exposed to the fixture.
3. Request the native Dangerous review and cancel it first. Record the complete
   effect, exact store name, visible special warning/Critical appearance,
   readable scrollable detail, both buttons and cancellation result separately.
   Confirm that cancellation does not create the dedicated user-global store.
4. Request it again. Confirm that a fresh sheet appears. Approve only after
   reviewing it. The fixed provider result establishes return after approval;
   independently inspect the real audit for `UserGlobalStorage/Open` and its
   successful terminal outcome. The fixture does not read the audit itself.
5. Check Escape/Return behavior, compact-window button visibility, exact sheet
   ownership, navigation/destruction cancellation, stop/restart and tray reopen
   in the real native app. Preserve screenshots and source/artifact/profile
   identities with the observations. These presentation/lifecycle checks are
   **untested** until that run is performed.

The bounded local report holds one result per named probe, limits each detail
to 256 characters, and displays plain text. It reports sheet presentation as
unobserved regardless of whether the provider call succeeds. A rejected Port
call is `denied-or-failed`, not an invented proof of native cancellation.

To refresh only the source UI manifest after a reviewed asset edit:

```sh
node packs/native-acceptance/stage.mjs --ui-manifest-only
```

The light producer/source suite uses only Node's standard library:

```sh
node --experimental-vm-modules --test packs/native-acceptance/verify-source.test.mjs
```

Its VM injects browser outcomes to test the report classification and explicit
button dispatch. It also checks actual asset hashes/source copies and the real
staging CLI's rejection of non-components before output creation. These tests
do not establish actual browser isolation, CSP enforcement or native review.
