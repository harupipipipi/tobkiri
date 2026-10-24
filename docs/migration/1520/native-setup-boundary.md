# Existing Launcher Setup integration boundary

The accepted product flow remains the full existing DMG Launcher Setup,
recovery/handover, internal `defaults` Profile, Launch, dedicated native approval,
API/key/base URL/model settings and conversation features. Display naming changes
to Harness. A CLI-created Profile or reduced replacement screen is not parity.
The original Launcher remains the default until that complete flow is verified.

## Current source facts

- The original React `frontend/src/pages/Setup.tsx` uses the Setup contracts
  through `frontend/src/lib/hostClient.ts` and `apiTransport.ts`. Its install,
  registry, readiness and presentation semantics are not supplied by
  `core_start_profile`.
- `src-tauri/src/core_host_commands.rs` exposes eleven generic running-session
  commands. It exposes no configuration proposal, source selection or commit.
  The native library's `check_profile_candidate` is structural checking, not
  an installed/approved/activated Profile or an IPC configuration channel.
- `core-host/src/isolation.rs` checks `HostCaller` against the actual native
  WebView label and trusted local origin. `src-tauri/src/core_host.rs` explicitly
  identifies this as a top-window check, not proof of the invoking iframe.
  Native iframe IPC denial remains a separate platform verification obligation.
- Core's `session/ui.rs` derives a `Principal::PackUi` from its retained
  attachment. A UI attachment proves Pack/session custody; it supplies no User
  configuration authority.
- The ordinary native Core facade already provides Profile save/create,
  add/remove Pack, permission approval and secret storage. `save_profile`
  performs source verification/compilation, actual native approvals, and
  head/security-epoch CAS. These operations must retain their existing meaning.
- The legacy signed Shell handoff authenticates its own frozen catalog,
  artifact, entry and execution bindings. It is not evidence that the new
  Rust Core has admitted an ordinary bootstrap Profile.

## Integration constraints

Astra high answered supplied source facts only on 2026-10-25; it used no tools
and performed no implementation. Adding trusted configuration IPC to the same
window that hosts untrusted Pack iframes is not established as safe by the
current top-window guard.

The existing trusted Launcher Setup can instead be retained in a native-owned
WebView with no Pack iframes or untrusted documents, and a separate capability
for only the required ordinary native configuration operations. Pack UI windows
must not receive that capability. This is a possible integration boundary, not
an implemented or platform-verified feature. A different window label alone is
insufficient: actual WebView/frame identity and capability enforcement, trusted
navigation/content, and direct-invoke rejection must be verified. Pack messages
must never be relayed into User configuration operations.

The full React screens and logical contracts stay intact. Domain-specific
Setup/readiness/presentation orchestration belongs in Kit/compatibility Pack
code; the native Host supplies generic caller isolation and the existing Core
facade, without privileged Pack names or a localhost Core HTTP server. Pack
configuration data is an inert proposal until genuinely trusted native inputs,
frozen sources and the existing GUI approval/commit path establish authority.
No new approval ceremony, trust root, synthetic commitment receipt or silent
permission grant is introduced by this design note.

Before product activation, verify actual signed distribution bootstrap and the
complete original Setup/recovery/handover, Launch approval, provider settings
and conversation flow. Verify failure/restart/CAS behavior and malicious Pack
iframe attempts against the same native configuration boundary. This note
closes no GUI, bootstrap, approval, platform or VM-retirement acceptance item.
