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
  retains seven complete confirmation/decision/response methods and the frozen decision
  invariant as verbatim Python-on-Wasm source. Its source tests compare the
  complete original application, and its separate CI build uses the existing
  isolated Python 3.13 producer. The original canonical digest implementation
  and its package dependencies are staged without rewrites; optional definition
  and Pack-pin predecessors retain their original digest binding. These are
  recorded DATA, never a native review handle or consent. This does not migrate listing/catalog checks,
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
and performed no implementation. The user directs unresolved design questions
to this adviser, with implementation and verification retained by the root
agent. Adding trusted configuration IPC to the same
window that hosts untrusted Pack iframes is not established as safe by the
current top-window guard.

The user confirmed a narrow Core-managed confirmation surface inside the
existing Setup, preserving its appearance, complete review and gesture count.
The application projection remains untrusted; this does not promote the full
React application into a privileged native UI. The trusted surface must retain
the exact native review owner and complete per-request approval targets, with
no Pack iframes or untrusted documents in that authority boundary. Pack UI
windows must not receive configuration capability. This boundary is authorized
but not implemented or platform-verified. A different window label alone is
insufficient: actual WebView/frame identity and capability enforcement, trusted
navigation/content, actual input provenance, and direct-invoke rejection must
be verified. Pack messages must never be relayed into User configuration
operations.

The first Gate seam now separates request review from a trusted decision.
`review_pack_permissions` and `review_user_permissions` retain the original
Gate, CallCx, reservation, mediator-narrowed maximum, exact prompt and control
charge in a move-only `PendingPermissionReview`. They do not request native
consent, dispatch, install policy or create a successful effect. Prompt facts
are descriptive and cannot be supplied back as approval. Dropping the pending
review uses ordinary abandonment while its charge remains held.
`request_approval` consumes that original value, rechecks cancellation, deadline
and epoch before showing the ordinary native prompt, and returns the existing
prepared effect only after that path succeeds. Existing `prepare_*` APIs retain
their ordinary behavior by invoking these two stages. Actual prompt String
capacity is retained under the same original control account.

The new `Core::save_profile_with_native_review` retains the exact candidate,
verified Pack/source custody, compiled plan, original Gate/CallCx, head/epoch
and pending targets in a single asynchronous save operation. Its private view
contains the full Profile definition, the existing bounded structural review,
precondition and plan digest, built from the same verified Packs. It does not
reload another catalog. View construction and actual wire capacity remain
charged to the original control account. An overlarge complete view fails
closed rather than displaying an incomplete candidate. These native-only
bytes can include private configuration/source paths: never log, publish, or
pass them to Pack UI.

`PendingPermissionReview::request_all` retains the same Gate and original
context for every member. Gate owns the bounded per-item response slots and
creates non-serializable native correspondence from each original prompt
snapshot. Missing, duplicate or foreign replies invalidate the entire review;
a later correct answer cannot clear that failure. Each device attestation
still passes the original per-request binding, signature, expiry and nonce
checks. A group decision is not copied into per-item attestation evidence.
Targets without additions produce no invented prompt or attestation. Incoming
array capacity, reply slots, variable answer storage and the completed array
retain original-account custody through cancellation and guard destruction.

The new trusted native callback defaults to unavailable and has no automatic
individual-prompt fallback. Its implementation must turn one actual trusted
user interaction into decisions for precisely the displayed requests. No
client checkbox, digest, shared "next prompt yes" state, or serialized review
handle supplies that evidence. The existing individual save APIs retain their
ordinary behavior. After all native decisions validate, the grouped operation
uses the same durable batch commit and final CAS; this commit section has no
await and retains the original typed uncertainty and recovery identity.

This is a native library seam, not the real Setup surface. Tauri configuration
IPC, actual frame/input provenance, complete display-to-effect correspondence,
and the native operation registry's retention of post-commit outcomes remain
unimplemented/unverified here. Closing UI after commit starts must not simply
abort and discard its operation. Current native Hosts leave the grouped
callback unavailable until that boundary is implemented and tested. The
ordinary product Setup/settings/chat path remains the original Launcher.

First-run domain projection remains separate: the public runtime still has no
pre-Profile Pack execution exemption. Astra high rechecked the issue text on
2026-10-25 and confirmed a distinction between ordinary Pack runtime operations
and the existing untrusted Setup UI's own candidate computation. Translating
that UI computation to Wasm does not create Pack execution authority or require
a specification exception. It must remain UI-local, with no Profile, Session,
HostIo, internal API or port authority. Its only source input is an immutable
capture from native User configuration inspection, not UI-selected arbitrary
filesystem paths or automatic source acquisition. Native code must enforce the
original owner and unchanged byte limits, finite execution time/fuel,
cancellation and output bounds. All outputs remain untrusted candidate DATA
for Core verification/compilation and the actual trusted review/Gate/CAS path.

`tobkiri_protocol/captured_inputs.py` now supplies the first read adapter for
that computation. A closed file inventory maps paths to ordinals; the reader
requests only an ordinal and bounded offset/length, never an OS path. Native
reads are at most 64 KiB, complete returned buffers at most 4 MiB, and derived
directory metadata is bounded before ancestor construction. Large inspected
artifacts stream through the unchanged artifact-tree/entrypoint hash and
header checks without copying the entire artifact into UI memory. The original
local descriptor/no-follow/fstat checks remain in place. Captured paths cannot
be passed to OS filesystem functions. Schema aliases and directory enumeration
use that same captured namespace. A request for actual macOS code-signature
verification refuses this data adapter and still requires native inspection.

The Python reader is untrusted UI code, not a native capability or custody
proof. The future native binding must independently validate capture ownership,
ordinal membership and ranges, retain immutable sources, and fund the actual
worker/buffers/metadata under the same original account. These objects alone
do not establish those guarantees. Seven small adapter tests compare the
original local/captured artifact algorithm, schema reads and refusal behavior.
The separate clean packaged-source regression executes the complete original
resolver, preview, bootstrap confirmation and validated listing for review,
active and denied states. All results match with ordinary filesystem reads,
network, database connections and subprocess execution blocked during the
captured computation. Together with the eight original streaming/TOCTOU
regressions, this comparison passes nine tests on the native Python host. Both
files are required by the existing CI contract cluster.

This establishes the original initial DATA computation's read closure, not an
actual Wasm worker, native capture custody, first-Profile approval, complete
recovery/handover, approval GUI or activation. Packaging the dependency closure
and enforcing the real worker's limits remain open. Neither the seven-method
component nor this request-review/read seam completes that integration.

The next native custody prerequisite is `DataRoot::native_setup_owner_lease`.
It has no Profile, principal or grant and is not called by ordinary Core open.
It shares the existing broker's descriptor-pinned, exclusive directory-inode
lock with ordinary allocation owners; no replaceable lock file or additional
approval is introduced. Clones and reopened/renamed roots retain the same
physical lease and original metadata funding. The original APFS/Darwin
platform checks and rejection of inherited process state remain in place.
This is an OS-custody seam, not a payload account or an executing UI worker.

Native broker bookkeeping is prepaid in the existing CoreControl account:
one permanent 4096-slot, 4096-byte-per-slot reservation covers bounded registry
nodes and Weak-held allocation slack through the process registry's lifetime.
Each live native owner also retains its original control slot. A last strong
lease drop does not falsely refund retained Weak/map storage. These are control
reservations within the unchanged control cap, not guest/ABI/output funding or
a whole-process RSS proof. The actual shared 64 MiB native Setup payload
account and per-invocation provenance now have an explicit native bridge;
The bounded native Store and owned codec adapters now use that account;
the captured-input callback and actual original UI runner still need
connection. Existing CapacityOwner and Gate-approved
Pack reserve/frame APIs are unchanged; inventing a Profile to fund Setup is
not an alternative.

`CapacityLedger::native_setup_account` shares one fixed 64 MiB Setup DATA
account in the actual root ledger. Its generation and invocation identities
are native accounting provenance, with no synthetic Profile, Gate revision or
request. Caller replacements do not supply another cap or reset occupancy.
Guest/stack/ABI/buffer reservations use that same account, and their shared
release plans refund after their own physical reservation storage is freed.
Cloning a reservation retains one allocation; a copied output must reserve
its own bytes. The account retains the real root proof and original control.

The cross-crate `NativeSetupLease` trait is a trusted native Rust contract,
not type-level authentication of arbitrary implementers. Store implements it
using its opaque actual root lease. No guest, WIT or IPC accepts a proof or
implementation choice, and no conversion to CapacityOwner or a Gate frame is
provided. Control metadata remains separate: each account and record is
prepaid; a bounded 4096-ledger carrier registry holds a permanent 4 MiB
control reservation from the original account so even Weak/permit-holder
bookkeeping remains funded through actual destruction. This consumes the
existing control allowance and does not raise either 64 MiB cap.

Six portable regressions cover shared capacity, original-control reuse,
retained output/Weak custody, domain/proof rejection, metadata refusal and
concurrent admission. The supported-platform integration uses the real root
lease and rename/reopen path without any Profile. These are logical funding
and custody tests, not evidence that a 64 MiB guest has executed or that the
original Setup surface is connected. Actual native/portable CI remains
required before claiming those tests pass.

The aggregate Wasm limiter now delegates physical growth and failed-growth
refunds through a private, authority-free interface. The original Pack
implementation still performs its unchanged Gate/frame checks and requires
its real CallCx. A native Setup implementation can use the same limiter
without inventing that context; it must retain an external physical owner
until after actual Store destruction. This interface alone does not create
the separate native Store or its owned input/output adapters.

The separate diagnostic-only schema stage profile executes the same original
workload under its existing standalone observer bounds. Actual source
`a6df72d80` records 149 markers and 38 completed compile intervals, totaling
1,582,004,935 fuel including marker overhead, before the unchanged 2-billion
fuel budget traps during distribution metadata. This justifies a separate,
default-off preinit code-cache experiment. It neither completes schema parity
nor changes the product UI's original 64 MiB cap. Moving compile audit events
to preinit and using runtime marshal decoding is an explicit behavioral
difference to evaluate, not audit-equivalence evidence.

The first genuine cache experiment builds successfully but still fuel-traps.
Its runtime repeatedly walks the complete cache to recalculate retained size.
The inactive candidate seals records, payloads and lookup rows as tuples
of immutable scalars/bytes, discards mutable construction dictionaries, and
accounts the final graph, report/index and conversion overlap before publishing
one generation-bound retained total. Runtime checks reuse that total while
still checking source/payload hashes and flags and creating a fresh marshal
code graph. A structural mismatch refuses cache execution; it does not omit
the retained allocation and continue. Actual source `1c8bb25ed11547dcc362300092de7a0955e68c0e`
in run `40239077602`, job `121306792547`, matches the full original Schema
observation, completes the original corpus and resource/package inventory,
retains both clock assertions, and rejects all four non-clock probes. It uses
676,193,417 of the original 2-billion continuous fuel budget and returns through
post-return. Returned guest DATA reports 38 cache hits, zero runtime compiles
and 38 fresh marshal loads. The final component is 28,191,814 bytes,
SHA-256 `f5b728bac48e6dde21aa49971f457e95a3a974f18fbfa169f68c9b30b56473a1`.
The workflow still deliberately fails after the comparison: this is diagnostic
evidence under the original standalone 128 MiB observer, not product activation,
audit equivalence, the 64 MiB product Store test or full GUI parity.

## Bounded native Store prototype

`WasmEngine::native_setup_store` is an inactive, separately owned native DATA
Store. It accepts the real native invocation and its original CoreControl,
without a fabricated Profile, principal, Gate frame or Pack initialization.
Its fixed prototype budgets are 2-billion fuel, 30 seconds from the original
invocation start, 65,536 aggregate table elements, 1,024 core instances,
16 memories/tables and a 512 KiB Wasm stack. Stack, guest memory and conservative
table bytes share the existing native 64 MiB account; metadata remains prepaid
through the original control account. An invocation's shared one-way claim
permits only one Store construction attempt, including across clones, failed
admission and Store destruction. Neither fuel nor deadline is renewed.

The outer owner and limiter closure retain physical/control custody through
actual Store/native destruction. Retirement only initiates cancellation; it
does not return still-live capacity. No raw Store extraction, linker or import
registration is public. Graph and component codec budgets share the same native
reservation and single monotonically decreasing host-work state; this work
counter neither replaces nor replenishes Wasm fuel. The original deadline,
cancellation and retirement apply to each admission. The existing bounded
custody registry retains the same concrete Arc across its graph/component/marker
trait views, including unexpected Weak handles, and releases its original
control funding after actual owner destruction. Expiry/cancellation do not
prevent cleanup of freed values. The 4 MiB JSON/output boundary, immutable
capture DATA interface, exact two clocks, actual original UI runner and trusted
GUI input/commit integration remain to be connected and verified.

The pinned Wasmtime 48 limiter contract permits a growth-failure callback only
after `memory_growing`/`table_growing` admitted that attempt. Two pre-admission
paths violated it: memory type rejection and table size overflow. Those paths
now return the ordinary guest growth failure without a callback; otherwise the
shared aggregate limiter could refund a previous successful allocation. Actual
post-admission allocation/maximum failure callbacks are unchanged. Eleven
portable CI tests cover real memory/table occupancy, shared host reservations,
Store destruction, continuous fuel/deadline, single-use claims, both rejection
orders, deterministic post-admission maximum failure refunds, real retained
graph/component copies, shared host work and Weak-held metadata. Source tests
and a portable injected root proof do not establish supported-platform OS proof
or product UI parity; actual execution results must be recorded separately.

The full React screens and logical contracts stay intact. Domain-specific
Setup/readiness/presentation orchestration belongs in Kit/compatibility Pack
code; the native Host supplies generic caller isolation and the existing Core
facade, without privileged Pack names or a localhost Core HTTP server. Pack
configuration data is an inert proposal until genuinely trusted native inputs,
frozen sources and the existing GUI approval/commit path establish authority.
No new approval ceremony, trust root, synthetic commitment receipt or silent
permission grant is introduced by this design note.

## Frozen disk input prerequisite

The original captured-input adapter streams large application artifacts. Its
4 MiB `read_bytes` bound and 64 KiB callback chunk do not impose a 64 MiB file
or total-artifact limit. Its 4,096-file/1 MiB namespace-metadata limits and
`size < 2**63` remain unchanged. Copying the entire capture into the shared
64 MiB heap would change those semantics. Astra high revised the earlier
heap-backing interpretation after these original source facts were supplied:
memory and disk resources must be accounted separately, without a new arbitrary
artifact-size quota.

The inactive `NativeSetupOwnerLease::capture_file_snapshot` prerequisite uses a
new, exclusive inode inside the descriptor-pinned native root. It opens the
readonly FD and unlinks the private name before reserving/copying disk content;
no published pathname or writable mapping is retained. On supported macOS it
uses [the documented `F_PREALLOCATE`/`F_ALLOCATEALL` operation](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fcntl.2.html)
and checks that the returned real allocation covers the complete request.
Logical truncation, sparse allocation and guessed COW extent costs are not
fallbacks. This is OS-space admission, separate from the SQLite-only disk quota.
It is not proof of an exact increase in free filesystem blocks after close.

The original pinned source reader still performs no-follow traversal, exact
length/EOF and dev/inode/mode/size/mtime/ctime checks. The copy's SHA-256 must
match the declared source; after the writer closes, the completed readonly
backing is hashed again. Only then can the opaque file snapshot be returned.
Its actual final FD is closed before root/invocation custody and the original
metadata permit are released. Clones share that FD/guard; no descriptor or
writer extraction is exposed. Original deadline/cancellation apply throughout,
and exact invocation custody prevents equal ledger-local counters from aliasing.

The streaming scratch/hash allowance is 80 KiB in the original native 64 MiB
account. Path-component growth, FD/guard/control-block metadata are prepaid in
the original CoreControl account. A fixed-range read consumes only the completed
backing, at most 64 KiB, with checked range arithmetic; it performs no source
rediscovery or re-verification. Its caller must prepay the output in the same
native account. These synchronous primitives require a funded managed IO worker;
timeout/cancellation is not evidence that an in-flight OS call finished or that
its capacity can be returned.

Four supported-platform tests are explicitly required by CI, including a real
artifact larger than 64 MiB, readonly/unlinked backing after source mutation,
failure cleanup and exact invocation identity. This code has not established
their execution success yet. The closed full-capture inventory, original
whole-source/native code-signature checks and auxiliary observations, managed
callback lifetime, WIT binding and original GUI integration remain required.
A per-file frozen hash is DATA, not source-approval or commit authority; this
prerequisite does not activate the new Launcher or remove the legacy runtime.

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

The inactive native DATA entry now retains one original Store across calls,
accepts only exact typed wall/monotonic clock imports and zero-or-one string
input with one string result, and refuses ordinary Core/Pack identities and
their generic invoke ABI. Trusted nativeKit borrows the export selector; this
does not create a guest/IPC selector or authenticate nativeKit by import shape.
Owned canonical calls finish post-return before an output envelope is checked.
Input, ABI values and retained CoreBuffer output share the original allocation
owners, fuel and absolute deadline. A suspended call owns its Store locally:
dropping the future destroys that Store and makes the entry unusable. Bounded
observation counters record actual clock admission/completion and fuel before
retirement; they are DATA and never authorization.

The closed captured-input catalog preserves the original 4096-file, 1MiB
namespace metadata and 64KiB range-read bounds, including UTF-8 accounting and
file/directory collision rejection. Entries bind only to already sealed file
snapshots from the exact same invocation. This catalog opens no host path and
is not yet a guest capture import or managed IO integration. The new portable
12 Store, 3 namespace and 11 actual-component entry regressions are explicitly
counted in CI; their source does not prove supported-OS capture or whole GUI
parity. The real Native Store 64MiB fullSchema comparison now matches in CI,
with the portable-owner scope described below; product GUI parity is incomplete.

The diagnostic code-cache workflow now builds its native observer from the same
checkout and pins the executable hash in the read-only sandbox control. An
explicit `--native-data-consumer` selects a controller adapter; the default
standalone observer and original `driver.py` remain unchanged. The adapter
retains the exact final component hash, original native oracle and full corpus,
clock windows/resolutions, inventories and four genuine unreachable denials.
Wasmtime's typed trap is sampled before its error conversion; a clock host
failure is never accepted as a denial. Each call uses the production native
entry and original 64MiB/2b/30s/4MiB bounds, then destroys the actual Store and
streams the still-funded output to its CI report. The injected owner is a
portable fixture: it provides no actual OS-root, GUI or product acceptance.

On source `4c942cf46ede321aef92738a3045dae34b9f0e7a`, run `40251486931`,
job `121344168991`, the full original observation and corpus match: 13 validation
cases, 8 formats, 20 resource entries and 5 distributions, both real clock
assertions and all four typed-unreachable denials. All seven actual native
invocation receipts preserve the same final component hash and original bounds,
post-return for successful calls and actual Store destruction. The full run
consumes 696,631,412 of 2,000,000,000 fuel, with 38 sealed-cache hits, no misses or
runtime compiles and 38 fresh marshal loads. The diagnostic controller writes
`strict_comparison_outcome=matched` and then deliberately fails without accepting
a production candidate. This is separate from the earlier standalone128MiB
comparison and still provides no actual OS-root or complete Setup GUI proof.
Source `37d723b64` passes all 12 Store, 3 namespace and 11 real-component entry
regressions; one needless borrow remains for strict lint and is fixed separately.

The nativeKit migration must include catalog/source inspection, Profile
resolution and complete Setup listing, beyond the existing six pure projection
exports. Preserve source additions, initialization, activation, recovery and
handover. Keep the existing GUI GET `/api/setup/packs` and POST
`/api/setup/packs/install` behavior and native approval/Core paths; a projected
success response cannot replace real registration/recovery/activation custody.
An independent nativeKit world/artifact loader is not implemented by the DATA
runner alone. Its typed errors must preserve the original values and GUI
semantics without importing ordinary Pack authority or disguising traps as
domain errors. Capture IO still requires bounded, prepaid native-only managed
custody through actual OS completion, even after timeout or Store destruction.
The original 4MiB captured-reader bound is per returned buffer, not cumulative;
large-artifact streaming and seek must remain available within original budgets.

The native entry also binds an exact borrowed result contract: successful string
or unit, and an error record with the nativeKit's exact field names and ordered
enum cases. This does not import ordinary Pack nominal types. Both success and
normal domain error complete owned canonical post-return and retain the same
original budgets and instance; code/message copies remain funded CoreBuffers.
Domain errors are DATA and do not implement the Core/Gate error interface.
Unknown enum values, ABI mismatch, oversized strings, traps and post-return
failures are execution errors and destroy the Store. Nine additional real
component regressions cover these paths; execution remains a CI requirement.

Guest unreachable and post-return failure are distinguished by Wasmtime's
existing typed `CanonicalCallStage` before error conversion. Observer format
`tobkiri.native-data-schema-observation/2` requires that distinction: a cleanup
trap must never be accepted as an original non-clock IO denial. Historical
format-1 evidence remains unchanged and scoped to its actual frozen source.

`BundledCatalog.load_from_reader` shares the original document validator with
the native loader. It consumes the original lock bytes and a reader bound to
each lock-entry occurrence, including executable-catalog rereads. It retains
original path text and read order; it does not normalize aliases into one key.
Native `.load` still owns its original path resolution, root and symlink checks,
and artifact-root resolution occurs only after all documents and pins validate.
The DATA reader accepts presentation roots without converting them to OS paths.
Successful DATA parsing does not authenticate the reader or authorize effects.

The new candidate-only catalog regression includes load, original resolver,
preview, confirmation and all three listing states with external IO forbidden
and schema/validation caches initially empty. The unchanged original packaged
fixture and independent native baseline remain required. Native capture mapping,
same-invocation sealing, Wasm execution and GUI integration need separate proof;
this shared parser and its captured-input test do not establish them.

`NativeDataProgram` binds a nativeKit-selected fixed table of exact function
contracts before instantiating the same component. A native caller selects a
bound slot; there is no guest/IPC export-name selector. Calls reuse the original
Store and instance and serialize through actual owned post-return. NativeKit
remains responsible for its explicit initialization and original domain-state
transitions, including retry behavior after a normal initialization error.
This API never skips an ordinary Pack lifecycle or renews an invocation lease.

The fixed function table is native control metadata: its complete Vec capacity,
shared block and simultaneous builder/program bookkeeping are prepaid by the
original CoreControl, and refunded only after actual storage destruction. The
graph account charges its original work/active checks with zero DATA bytes for
this table. Guest input/output, error strings and ABI copies still consume the
original shared 64MiB payload account. Seven actual-component/control regressions
are explicitly selected on CI; until their results are read, these are source
contracts, not runtime evidence. Whole Setup nativeKit, capture IO and GUI
integration remain incomplete.

`NativeDataIo` reuses the existing funded execution owner for native-selected
snapshot capture and sealed ordinal/range reads. It never fabricates a Profile,
Pack frame, grant or path supplied by a guest. Its single slot remains occupied
through an unconsumed completion. The original blocking task and monitor Joins,
FDs, DATA buffers and control reservations remain owned until actual task and
storage destruction. Store drop stops admission and late publication; the Core
owner must independently retain the returned IO handle and await
`retire_and_wait` during cleanup. A blocked OS operation is still pending after
the original DATA deadline; cleanup neither renews that deadline nor pretends
that cancellation finished IO. The task allowances retain the existing kernel's
scope and do not establish exact Tokio allocator, RSS, stack or TLS accounting.

The dedicated `tobkiri-ui:inputs/captured@1.0.0` import exposes only
`read-range(ordinal: u32, offset: u64, length: u32) -> list<u8>`. It requires both
the IO binding and an immutable capture from the same actual invocation before
instantiation, including an empty capture. Signature changes, extra exports and
cross-invocation seals are refused. There is no discovery, pathname, raw FD,
write or general filesystem import. Host callback state and simultaneous source,
destination and canonical ABI copies use the original payload owner. IO failure
is an execution failure, not a normal domain error or a genuine guest
unreachable-body denial. Neither captured bytes nor their successful parsing
authorize a Core effect.

Nine portable blocking-task tests and four portable component-import tests are
explicitly selected on CI. One additional supported macOS/APFS component test
captures actual bytes, mutates the source, reads the frozen bytes through the
real Wasm import and verifies owned post-return. The portable probes establish
task/control and source-FD custody only. Actual CI results, the supported OS test,
and whole nativeKit/GUI integration remain separate requirements; source tests
and this inactive mechanism do not complete the product migration.

NativeKit error binding can explicitly select an additional third string field
for structured exception details. The two-field and three-field records are
separate exact contracts fixed before instantiation; a missing or differently
typed details field never falls back to the older contract. Code, message and
details remain opaque funded DATA, with their combined UTF-8 length inside the
original 4MiB error envelope. The owned details survive actual canonical
post-return and Store destruction; ownership transfer includes every buffer.

The nativeKit must separately validate its versioned closed details schema,
finite exception kinds, original args and safe fields, required cause/context,
and the code/details combination before using them. It must preserve ordered
finite DATA distinctions and the original adapter's semantic error conversion.
Unknown or unrepresentable errors are ABI failures, not silently generic domain
errors. No arbitrary Python class import, pickle, object reconstruction, field
truncation or new host-path/traceback disclosure is permitted. Details decoding
and all simultaneous copies still require the original work, deadline,
cancellation and payload accounting; this opaque runner does not perform that
application-specific decoding or grant any authority. Four additional actual
component regressions cover the new ABI, but their current CI result must be
read before treating them as execution evidence.
