# Existing Launcher Setup integration boundary

The accepted product flow remains the full existing DMG Launcher Setup,
recovery/handover, internal `defaults` Profile, Launch, dedicated native approval,
API/key/base URL/model settings and conversation features. Display naming changes
to Harness. A CLI-created Profile or reduced replacement screen is not parity.
The original Launcher remains the default until that complete flow is verified.

The original approval sequence has now been traced in source. Setup's review
checkbox confirms the exact Profile/catalog/plan/authority/epoch and its
activation transaction. A `profile_grant` edge receives persistent authority;
an `interactive_only` edge does not. `launch_selected_presentation` revalidates
the existing active identity and performs its Shell handoff; it does not request
a blanket first-launch permission approval. The dedicated native window reviews
a specific subsequent interactive request. Preserve these distinct decisions,
including the visible Setup confirmation and later interactive review, rather
than introducing an unapproved Profile import or new blanket Launch ceremony.

## Current source facts

- The inactive [original Setup projection candidate](../../../packs/local.harness-setup-python/README.md)
  retains four complete decision/response methods and the frozen decision
  invariant as verbatim Python-on-Wasm source. Its source tests compare the
  complete original application, and its separate CI build uses the existing
  isolated Python 3.13 producer. This does not migrate listing/catalog checks,
  establish bootstrap authority, connect configuration IPC or certify a final
  activation receipt. Original `listing=None` acceptance and intermediate
  `restart_required:false` remain inert DATA for trusted Host processing.
- The candidate's separate native Core fixture uses an ordinary saved and
  approved Profile, actual `Session::call_user_port`, immutable source/build
  bindings and exact Gate terminals. It checks that Setup DATA cannot change
  Profile head/epoch or Pack/User policies and retains a read-only audit
  sidecar. This is not an execution exemption before first-run approval. The
  public Core currently exposes no pre-Profile application Kit runner; sealed
  presentation source alone does not create execution authority.
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
- The opt-in [actual native IPC observation](../../../tools/native-ipc-probe/README.md)
  builds the real macOS debug Tauri app and exercises these existing commands
  from its ordinary opaque iframe. Its strict controller requires positive
  root controls and independent native rejection counts, suppressing Tauri's
  key-bearing raw diagnostics. This is a scoped test path, not a new
  configuration channel, native approval test or full Setup integration.
- Core's `session/ui.rs` derives a `Principal::PackUi` from its retained
  attachment. A UI attachment proves Pack/session custody; it supplies no User
  configuration authority.
- The ordinary native Core facade already provides Profile save/create,
  add/remove Pack, permission approval and secret storage. `save_profile`
  performs source verification/compilation, actual native approvals, and
  head/security-epoch CAS. These operations must retain their existing meaning.
- `Core::save_profile_at_epoch` additionally binds a save to the security epoch
  shown in a prior native review. It checks the original `CallCx` before prompts;
  existing reservation and atomic Store checks retain that epoch across awaits.
  A same-head ABA cannot revive a stale review. This is only a freshness
  precondition: the caller still must bind the exact candidate/review and use
  ordinary native approvals. It does not certify the complete v4 confirmation.
- `Core::profile_save_precondition` captures the target, optional head and epoch
  under the pinned Profile lock. An unpublished target can already have a
  nonzero epoch; the UI must not infer zero from a missing head. A fresh target
  may initialize only its native directory and empty authority schema. The
  getter does not create a Gate/Session, approve a permission, write a revision
  or recover a pending save. These are save preconditions, not readiness or a
  complete application confirmation. Existing head-only observation is unchanged.
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

## Failed configuration writes

Core first verifies sources/compilation and obtains every Pack/User approval as
an inert, move-only preparation through the original native approval path.
Preparations retain their exact Gate/context, reservation, epoch and expiry;
their array, owned policy capacities and inline candidate digests use the same
CoreControl account. Refusal/cancellation before the decision installs no
permissions. Neither a Pack UI attachment nor a receipt-shaped Kit response
supplies User authority.

The save now holds the pinned Profile revision lock through one short SQL
**decision**, filesystem head completion and outcome settlement. The decision
atomically commits all policy effects, security epoch advancement, immutable
terminal evidence and an immutable save record. It records exact ordered batch
membership and a commitment to every original reservation/effect digest.
Recovery validates the complete membership, not mutable policy rows or a scan
of unrelated approvals sharing a request. Cancellation/expiry is rechecked
after all metadata writes immediately before COMMIT.

The filesystem head remains the selected-revision authority. A pending decision
fences new authority admission, coherent policy/funding reads and ordinary head
writers until that exact head is durably completed. Both policy getters select
the pending predicate and row in one SQLite snapshot. Reservation/dispatch/
consumption and direct policy writes check the fence inside their actual write
transaction. Existing consumed-lease cleanup, terminal/ACK/audit delivery,
expiry and later revocation remain available; revocation is never decreased by
recovery. Integrity-only cleanup checks remain separate from the new-admission
fence. Constructors, getters and cleanup callbacks never acquire a Profile
flock or perform filesystem recovery; lock order stays Profile → pinned DB →
SQLite.

All authority database connections request and verify `synchronous=EXTRA` so
later revocations and ACKs retain the decision's ordering. The coordinator also
checks DELETE journal mode and explicitly synchronizes the pinned directory
before head replacement and after outcome settlement. SQLite documents the
[additional DELETE-journal directory synchronization](https://www.sqlite.org/pragma.html#pragma_synchronous).
Revision verification streams a fixed 64 KiB buffer; commit/outcome custody
retains 128 KiB from the original CoreControl, without a second account or a
changed Pack/64 MiB catalog limit. Decision history is bounded by the existing
authority metadata budget (100 MiB / 4 KiB = 25,600 records); each header is at
most 2 KiB, and exact batch membership is bounded to 4,096 native terminal slots.
These are implementation bounds, not physical power-cut or all-platform proof.

A COMMIT error is indeterminate, never rollback success. Native
`profile-commit-uncertain` errors retain the exact Profile **and** Store-minted
User reservation identity, including failures from fresh Profile creation.
Their display omits source text. `ProfileCommitProgress` still distinguishes
an attempted epoch/decision COMMIT, observed advancement with unconfirmed head
replacement, and observed head rename with unconfirmed later durability.
Legacy head-only CAS errors have no coordinated operation identity.

Explicit Core recovery and Core startup/outer admission finish only an already
decided operation: they validate the immutable record and exact candidate,
accept only its recorded previous/new head, and complete the head/outcome.
They do not repeat prompts, permission effects or epoch advancement. Corrupt
batch evidence/candidates and unrelated heads remain fenced rather than being
replaced or silently repaired. Native outcome lookup returns an opaque past
save receipt with its own bookkeeping custody; matching the current head is
not a receipt, and this receipt is not product activation or replay authority.
Post-commit audit-delivery errors retain the same exact save identity.

New real-Store/Core regressions exercise late SQL failure, cancellation during
decision insertion, rejected COMMIT, postrename directory failure, outcome
settlement failure, restart, audit-delivery failure, concurrent revocation,
independent same-request approval, corrupt/missing batch evidence, unrelated
head and corrupt candidate. Source inspection and lightweight checks do not
prove those new native executions; supported-platform CI is required. No
whole-save/product acceptance is closed solely by this implementation. Full
original Setup receipt/recovery/handover, source lifecycle and native GUI
integration still require their own exact end-to-end evidence.

Before product activation, verify actual signed distribution bootstrap and the
complete original Setup/recovery/handover, Launch approval, provider settings
and conversation flow. Verify failure/restart/CAS behavior and malicious Pack
iframe attempts against the same native configuration boundary. This note
closes no GUI, bootstrap, approval, platform or VM-retirement acceptance item.
