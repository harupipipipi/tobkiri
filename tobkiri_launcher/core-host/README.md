# Generic Launcher Core host (M5 preparation)

This adapter embeds `tobkiri-core` in the Launcher process. Its only internal
dependency is the Core facade. It owns no manifest parser, permissions, Pack
registry, provider selection, stream context or domain shape. The existing
Launcher startup remains the default; the separate `core-host` feature is an
experimental path awaiting M5 prerequisites and native verification.

The trusted frontend receives Core's attachment token, frozen hash and declared
entry. It creates at most 16 `MessageChannel`s and `iframe sandbox="allow-scripts"`
instances per Core session. The frame receives only its channel. Its calls contain a
shape, operation and arguments; identity and approval fields are rejected. The
host forwards the closure-bound token to `Session.ui_invoke_with_cancel`.
`ui.ports_used`, approval, immutable revision and epoch checks remain in Core.

The custom scheme reads only `Session.verified_ui_asset` bytes. It never opens a
source directory, infers an entry file, extracts an archive or installs a Pack.
URI decoding happens once before exact map lookup; traversal, encoded path
separators and invalid UTF-8 are refused. MIME types are finite, responses are
non-cacheable with `nosniff`, and every response has a closed CSP with
`connect-src 'none'`. CORS permits module scripts in the opaque frame to load
these public immutable assets; it grants no credential or IPC access.

The SDK result is Core's `{kind:"value",value}` or
`{kind:"stream",handle}`. A channel forwards `stream-pull`/`stream-cancel`
to the same Core UI token. Core owns the original request context, audit outcome,
deadline, cancellation, epoch, pull limit (32) and handle limits. Frame detach,
SDK close and Session stop revoke the attachment and close its streams.

## Bounded trusted-host transport

Pack UI request and response bodies are capped at 1 MiB of canonical JSON UTF-8.
The Launcher measures JSON size and validates StrictV1-compatible values before
serializing a request. It caps the whole Core host at four in-flight calls
across all frames, while each individual frame can have no more than four. Each Core
response reserves the worst-case JSON-escaped outer Tauri envelope of
`6 × 1 MiB + 256` bytes before Core work begins. The 256-byte allowance covers
fixed field names, the 71-byte process namespace, safe-integer generation and
ticket IDs, and Core's fixed `ui_` plus 26-character ULID receiver. Four tickets fit in the fixed
Launcher queue (`24 MiB + 1,024 bytes`). Saturation rejects admission immediately.
There is no retry buffer. The trusted frontend shares one bounded ledger across
all frames and reserves each request envelope plus its maximum response before
dispatch. With four maximum-size reservations this frontend ledger is at most
`48 MiB + 2,048 bytes`.

Core returns an opaque `CoreBuffer` containing canonical output bytes. The
Launcher checks the 1 MiB limit, validates UTF-8, and uses fallible exact reserve
to make a distinct host `String` while borrowing the CoreBuffer. The CoreBuffer
remains live until that copy is complete; its allocation ownership is never
transferred into a naked `Vec` or released by a host ticket. Core bounds stream
batch serialization before advancing the stream cursor, so an oversized result
cannot silently consume a frame.
For final host output the same 1 MiB raw-body ceiling counts the payload and
optional surface-label bytes together; the escaped event-envelope bound is
checked before copying.

Each response ticket uses a host-minted, monotonically increasing safe-integer
ticket ID, the fresh process namespace, the current Launcher generation, and
the actual Core-minted UI session/receiver. The namespace is a CSPRNG-backed
identifier, not a bearer secret. The frame's request ID is only correlation
metadata; it never chooses a ticket ID. The trusted frontend keeps a per-
attachment map of at most four namespace/generation/receiver/ticket tuples and
never forwards a ticket to the Pack frame. ACK, detach and stop target that
exact tuple; stale or duplicate ACKs do not release another ticket. Call and pull tickets remain
reserved until their response is delivered and ACKed; an explicit stream-cancel
is its own ackable command. Aborting a pending stream pull is an out-of-band
signal that creates no ACK ticket. ACK identifies one exact ticket; detach and
stop retire all tickets for their receiver or generation. ACK only completes
the logical queue reservation: it does not prove collection of a CoreBuffer, future, Tauri
serialization buffer, or WebKit/MessagePort IPC copy. An unacknowledged
receiver retains its bounded reservation until detach or Session stop. These
controls make Launcher queues finite without claiming a whole-process RSS
limit. Host terminal/display output shares the same ledger and allows at most
one unacknowledged output event; its event carries the actual Core generation,
process namespace, fixed `core-host` receiver and host-minted ticket ID only to
the trusted frontend, along with the bounded payload and optional surface
label. The label is presentation
text, never a receiver or authority selector. The host-only output ACK must
match namespace, generation, fixed receiver and ticket ID and cannot be sent by
an isolated Pack frame.

The trusted UI keeps at most four in-flight calls per frame. Its terminal and
display history is capped at 262,144 UTF-16 code units, with each event bounded
to 1 MiB before it is appended. Terminal prompts are limited to 4 KiB and
terminal replies to 64 KiB. All writes are text-only; Pack content never becomes
HTML or trusted approval UI.

The optional Tauri integration retains generic Open/Update/Quit tray controls,
the official-release updater, the existing cancellable folder picker and the
complete scrollable macOS native review sheet. Approval renders Core's complete trusted prompt, including the structured
effect and digest-bound context. Dangerous requests use a distinct special
warning, title/action wording and a Critical native alert; ordinary review
retains its existing warning style. Budgeted terminal/display CoreBuffers are
copied only into the bounded host output event; values stay text and never
become HTML or approval controls. The legacy canonical display callback is
fail-closed, and the legacy terminal callback accepts only the fixed Core
startup warning once. Native sheet/picker leases cancel only their owned
surface when an asynchronous caller is dropped. Closing the primary window hides and preserves its live webview; tray Open
and duplicate-launch handling restore that same window. The single-instance
plugin is registered first on desktop targets. Shutdown closes Core off the UI
thread, while an independent watchdog applies the existing 155-second budget.
Failure, worker loss or deadline logs final cleanup/audit completion as
unconfirmed before exit; a successful close return is not independent proof
that every asynchronous journal has drained. A complete native approval renderer on other platforms remains pending;
it returns Unavailable rather than substituting an incomplete confirmation.
The new non-macOS folder picker is also unavailable until its owned cancellation
handle is implemented; the legacy default picker is retained. Budgeted terminal
input reserves its CoreBuffer capacity before opening the bounded host prompt;
the trusted UI input string is checked before it is copied into that buffer.
An optional display surface travels as separately bounded presentation text;
it never selects a receiver, session, window or authority.

The new configuration explicitly selects only the bundled `core-host`
capability, disables global Tauri APIs, restricts commands through AppManifest,
and checks the top-level host window label and URL at every command.
That URL is not the invoking iframe origin and cannot establish frame isolation.
The Core-build AppManifest list is exactly the ten registered Core handlers,
including `pack_ui_ack` for generation-, receiver- and message-scoped Pack UI
flow tickets and `core_host_output_ack` for the single trusted-host output
ticket. Detach and stop retire those tickets; ACK never proves that
Tauri/WebKit/Pack copies or Core-owned allocations have been physically freed.
Tauri's frame invoke-key enforcement still needs native proof. Tauri documents
iframe-origin limits on Linux/Android, so platform-specific adversarial IPC,
storage, network, module/CSP and lifecycle tests are required before declaring
isolation complete. See [Tauri capabilities](https://v2.tauri.app/security/capabilities/)
and the [patched iframe IPC advisory](https://github.com/tauri-apps/tauri/security/advisories/GHSA-57fm-592m-34r7).

Cheap checks, using the shared target and existing frontend dependencies:

```sh
CARGO_TARGET_DIR="$PWD/../.work/cargo-target" CARGO_BUILD_JOBS=2 cargo test --offline --locked -p tobkiri-launcher-core-host
npm --prefix tobkiri_launcher/frontend run lint
cd tobkiri_launcher/frontend
node --import tsx --test src/core-host/*.test.ts
npm run build:core-host
```

The UI build writes only ignored `tobkiri_launcher/core-host-ui` assets; the
normal legacy frontend bundle is unaffected. A minimal macOS native library
check passed with Rust 1.94 and shared-target, offline/locked dependencies.
This typechecked the Core Tauri handlers, custom scheme and AppKit calls; it
did not link or run a native application or establish iframe isolation.
The check exposed and repaired the AppManifest static lifetime and retained
artifact-policy binding. Inspection then found tauri-build 2.5 also admitted
stale legacy permission files into its generated manifest. A fresh Core-only
producer scope now preserves those legacy files while excluding them from the
new manifest; this final scope change awaits recheck after the disk floor was
reached by shared work. Native feature compilation of that final change,
startup/guest-embed lifecycle, platform review
renderers, actual Profile management and migrated product parity remain M5
prerequisites. In particular, guest `request-ui-embed` has no registered Session
attachment yet; only Core's startup UI attachments are rendered. Non-startup slots are refused
before a frame reservation is minted. This host slot check is supplementary,
not caller-provenance proof; guest startup-slot spoofing must remain unavailable
in the Wasm host-io path until Core attachment bridging exists. Legacy
Python/PackVM/handover code and all open acceptance rows remain present.
