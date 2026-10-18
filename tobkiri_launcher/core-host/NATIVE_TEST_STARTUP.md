# Isolated native Core host startup

The optional `core-host` native host accepts `--test-data-root <absolute path>`
only in a build with debug assertions. The process parses this before Core
startup; it is not a renderer command, environment fallback or live root switch.
No option preserves the existing platform-local `Tobkiri` data root. Production
builds explicitly reject the test flag. Unknown, duplicate, missing, relative,
parent-traversing, non-Unicode, control-containing and oversized arguments fail
closed without echoing the path. Core retains all existing DataRoot ownership,
filesystem, recovery, permission and approval checks.

The trusted native integration calls
`native_startup::parse(std::env::args_os().skip(1))` before single-instance
handling or state creation. On debug builds, an explicit path selects
`LauncherCore::open_for_test_data_root_with_tickets(host, tickets, path)`;
otherwise it uses the existing `open_with_tickets(host, tickets)` constructor.
A second launch must not claim to switch the root of an existing process.
The explicit debug/test launch skips single-instance redirection. It delays the
configured host window until setup and supplies an absolute sibling WebView
data directory named `<DATA basename>.tobkiri-webview` through the native builder.
A filesystem-root DATA path fails before Core opens because it has no sibling
cache name. The ordinary startup configuration is unchanged. Actual cache
isolation is subject to each WebView platform's support; Windows native
acceptance is recorded separately and is not proof for other hosts.

For a model-free UI test, create an ordinary public Profile with the normal CLI
in the same isolated data root, launch this host with that absolute root, and
manually enter the resulting Profile ID in the trusted host form. Profile
creation/import, Pack installation and Run recovery are not startup operations.
Starting the prepared UI does not authorize model work. The native invocation
must select the `core-host` feature, `tauri.core-host.conf.json` and the
`build:core-host` frontend. Windows native approval, actual WebView frame
isolation, UI restore behavior and clean shutdown require separate native
acceptance; parser and runtime fixture tests do not establish those results.

On Windows the native review renders the complete bounded Core prompt in a
read-only control, verifies its retained UTF-16 text before enabling approval,
and keeps one owned review through cancellation and parent restoration. Closing,
Escape, owner loss and unconfirmed cleanup do not approve a request. The existing
four-minute review and five-minute Core request limits are unchanged. Every
observed main-document load retires the existing or not-yet-published session;
the initial page-load callback is not assumed to arrive after host setup.

The two ignored Windows renderer fixtures below each require a fresh process
and a single exact selector. They open a JavaScript-disabled `about:blank`
parent with a fresh cache and a 512 KiB Unicode test view. A human operator must
choose a response; no Core, Gate, Profile, DATA, model or action is invoked.
Their returned boolean records a renderer interaction, never a Gate approval.
Ordinary unit-test and broad ignored-test commands do not open these fixtures.

```text
cargo test --manifest-path tobkiri_launcher/src-tauri/Cargo.toml --features core-host --lib --locked native_plan_review_windows::manual_gui::manual_full_unicode_review_normal -- --ignored --exact --test-threads=1 --nocapture
cargo test --manifest-path tobkiri_launcher/src-tauri/Cargo.toml --features core-host --lib --locked native_plan_review_windows::manual_gui::manual_full_unicode_review_dangerous -- --ignored --exact --test-threads=1 --nocapture
```

Focused Root checks (not executed by the source author):

```text
cargo test --offline --locked -p tobkiri-launcher-core-host test_data_root_open
cargo test --offline --locked --manifest-path tobkiri_launcher/src-tauri/Cargo.toml --features core-host native_startup::tests
```
