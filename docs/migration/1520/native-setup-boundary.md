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

## Current source coverage, 2026-10-31

The current [captured method list](../../../tools/python313-producer/harness_native/source_package.py),
[Python dispatcher](../../../tools/python313-producer/harness_native/dispatcher.py) and
[WIT world](../../../tools/python313-producer/harness_native/native.wit) define
19 original runtime methods plus init and seven transport operations. Older
17/6 descriptions below precede the two root getters and `native-inputs`.

| Surface | Captured source / WIT | Current native runner binding | Execution qualification |
| --- | --- | --- | --- |
| Original runtime | 19 methods plus init | init, bootstrap-profile-id, host-resource-root, bundled-profile-root | Full original method corpus and Setup adoption remain incomplete. |
| Value transport | Seven operations | describe-value | Complete transport execution, original value/error restoration and production custody remain incomplete. |
| Worker lifecycle | Native Rust mechanism | Tracked worker, cancellation, finish, actual Join and original exit paths | Library mechanism CI is separate from original Launcher and GUI qualification. |

The [native runner](../../../tobkiri_launcher/harness-native/src/bootstrap_runtime.rs)
binds those actual methods. The earlier arbitrary callback could clone owned
budget/result handles despite its HRTB borrow; a detached channel alone was not
a lease non-escape proof. The new finite application entry accepts only the
three current getter choices followed by actual description. It keeps all
intermediate references inside one worker/Store and rejects unbound choices
before owner/Engine creation. The fixed multi-getter sequence exists only in
tests; it defines no new Setup sequence. All nine finite regressions passed
the actual Launcher pipeline in source38f CI41197404494 (635/123/18 Rust cases,
plus existing packaging/cold-boot/DMG checks). That pipeline still uses product
Python/PackVM and is not original Setup/GUI or full guest qualification.
Unbound operations must fail explicitly before execution; neither a stub nor
the presence of a WIT declaration counts as original behavior. The original
GUI and full 19/7 qualification remain required before adoption.

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

The whole-Setup source capsule generator captures all 17 required original DATA
methods, the original decision dataclass, the complete 13-definition resolver,
protocol/schema sources and actual package initializers with byte hashes and
source extents. It does not synthesize namespace initializers or rewrite method
bodies. Nested and lazy native imports in the selected methods and resolver
must match the declared adapter inventory, including external Pack catalogs
and content-projection sources, roots and inventory verification. Unexpected
native imports fail staging. Native effects remain outside the capsule; their
sources are hash-pinned and their required behavior is recorded for explicit
integration. This is a source input to the whole nativeKit artifact, not an
executable dependency closure, a Wasm build or whole-path compatibility proof.

The separate native-input-stack prerequisite builds the original synchronous
Python artifact verifier and captured reader with only the sealed range-read
import. An independent original-filesystem reference also checks the original
captured adapter and records all repeated read ranges. The supported macOS
consumer freezes actual APFS bytes, changes the source, explicitly initializes
the component, and checks success, the original digest-denial DATA and dropping
a call while its native kernel work is admitted. It distinguishes actual IO
admission from ordinary fuel/epoch yielding before asserting stack suspension.
Decoded guest observations use the same original funded graph, not an unowned
host JSON copy. This probe still needs actual CI results; it is not the whole
Setup artifact or the future ordered native admission/inspection interface.
# Portable native exception DATA

The inactive `tobkiri-harness-native` nativeKit library and the guest
`harness_native/exception_graph.py` materializer share the closed
`tobkiri.native-exception/1` frame. This application adapter is outside the
generic Core/Pack result decoder. It adds no product native Python runtime,
native operation, grant, Profile, or alternate approval path.

Rust retains the original funded CoreBuffer envelopes and parses into CoreValue
using the same invocation's allocation owner. The code, message and details
share the original 4 MiB envelope; semantic traversal consumes original host
work. The original 64 MiB/fuel/deadline bounds still apply. Node IDs represent
shared or cyclic cause/context references; suppression is independent. Their
graph IDs are scoped DATA identities, never effect capabilities.

The guest uses explicitly supplied original class objects, with fixed tags and
no dynamic class/module lookup. It reconstructs all nodes before linking their
chains, then restores suppression last. The original already-formatted
SchemaValidationError arguments and diagnostics are restored without invoking
its formatting constructor again. JSONDecodeError positions count Unicode
characters. Path-free, two-argument OSError representations preserve concrete
classes independently of the native/Wasm numeric errno mapping. In particular,
the absent native filename pointers must stay absent: assigning Python None to
those fields changes the displayed error even though their properties compare
equal to None.

Unknown native classes, unmodeled fields or cause objects, host filenames,
Windows-specific fields and other unrepresentable observations remain explicit
unsupported migration paths. They are not redacted, substituted with a generic
exception, or replayed after a native operation. Native ports must establish
portable message/field provenance; scanning arbitrary text cannot prove that a
message lacks confidential host paths. Arbitrary exceptions already created in
guest Python remain actual objects there. Rust panics, capacity refusal and
unexpected host failures do not become modeled Python domain errors.

This library and its source/unit checks do not prove connected native inspection
ports, actual guest materialization, or whole Setup parity. The complete existing
Launcher/Setup/approval/settings/conversation path remains the product default.

## Test-only import diagnosis preserves the original denial

The separate import-audit diagnostic uses the unchanged original input-stack
app and calculation sources behind a thin wrapper. Its only extra import is
`tobkiri-ui:diagnostics/import-trace@1.0.0`. The wrapper binds the callable
before installing one audit hook per instance and records only import module
names. It neither replaces the loader nor enables a filesystem fallback.

The native trace module, Store field, binding and linker implementation are
compiled only with cfg(test). Ordinary Pack admission rejects the reserved
`tobkiri-ui:diagnostics/` namespace even if a caller lists it. Production native
admission has no diagnostic capability. The test-only binding is one-shot and
constructed from the Store's actual original invocation and allocation owners.

At most 128 immutable, originally funded CoreBuffers retain ASCII module names
of at most 128 bytes with a native-assigned sequence. The fixed row table and
mutex have prepaid CoreControl storage. Recording and later borrowed snapshot
serialization use original prepaid work; buffers remain charged through Store
retirement and actual cleanup. No fresh guest copy is made to serialize them.
Malformed, overflowing or failed recording is sticky and inconclusive; complete
recording also requires the recorded and observed event counts to agree.

The baseline test refuses the diagnostic environment and keeps all three
original cases. A distinct ignored diagnostic test uses the same original
64 MiB/fuel/30-second deadline, performs actual IO cleanup on errors, streams
its retained trace, and always returns failure. Unexpected original success is
also inconclusive. No diagnostic job can contribute an acceptance pass. A last
module name alone does not establish the missing dependency: its sequence must
be correlated with the original denied import and actual Wasm stack. The tracer
can itself change snapshot import closure, which must also be accounted for.

### Export object lifetime in the inactive native input-stack probe

The [pinned componentizer](https://github.com/bytecodealliance/componentize-py/blob/c0949b19d464f5d70bc1051195a3ae0e6a012df9/runtime/src/lib.rs)
constructs a Python adapter object for every exported function. The thin probe
adapter therefore retains its one `Logic` in the guest
module, shared by the `init` and `run` adapter objects within that interpreter.
Construction does not initialize the domain, and it never resets accepted state.
The original Logic, captured reader and verifier bodies remain unchanged.

The original three-case native stack check additionally exercises invalid init
followed by valid init, and rejected replacement with the opposite original
configuration. A separate actual-component check keeps two Stores alive in the
same original 64 MiB native account, with distinct real invocations and APFS
snapshot sources. It initializes A before B, then runs B/A/B against the original
success and wrong-digest reference results. Both snapshots contain the same
fixture bytes; this check asserts configuration/state separation and native
ownership, not behavior with different input content. Each invocation retains
its original fuel and deadline through post-return and physical IO cleanup.

These are CI assertions until their actual jobs pass. The exact not-initialized
exception through the current plain-string `run` ABI remains unproven; a trap
while formatting an uncaught exception is not evidence of Python exception
compatibility. The separate import diagnostic always fails and cannot satisfy a
product acceptance gate. No original GUI flow or default runtime is changed.

### Whole source package and native-selected inspection work

The source package assembler places all seventeen original composition methods
inside their original qualified Python module. `SetupActivationDecision` remains
a single original class in `core_runtime.profile_runtime_port`, imported by the
composition module. Resolver, protocol, Schema and mixed projection sources are
copied verbatim. A module being assembled or statically present does not prove
that its dependency closure executes in the pinned Wasm interpreter. Missing
native adapters and package dependencies remain explicit; they are not filled
with synthetic classes, namespaces or empty modules.

Projection inventory, metadata, declarations, hashes, selection order and
duplicate occurrence handling remain original guest DATA calculation. The
mixed projection modules are retained in the capsule for this purpose. Real IO,
descriptor identity, admission and authority still require native bindings at
the original reachpoints. No native precomputed projection result substitutes
for these original calculations.

`NativeDataIo::inspect` runs a native-selected callback on the existing funded
worker/monitor and one-inflight slot. Its borrowed `NativeInspectionOutput`
fixes the original owner and four-MiB total envelope, including framing and error
DATA. `write_chunk` reserves original DATA space before exposing a bounded tail
to an IO producer. A producer's typed error is preserved for the nativeKit
adapter; it is not automatically a portable exception graph. Allocation,
accounting, invalid-length or original-scope failures are sticky and cannot
publish a successful partial result. Callback failure and late completion also
cannot publish the staged bytes. Actual IO Joins and physical destruction retain
their original cleanup path after Store retirement.

This generic worker does not select or interpret paths, operations, Pack IDs,
phases or authority. NativeKit must retain and recheck the actual binding/owner
through worker execution, rather than capture a prior approval boolean. There
is no new guest import or ordinary Pack capability. Full ordered inspection
bindings, bound-path semantics, exception disclosure and the complete nativeKit
artifact remain to be integrated and executed before GUI adoption.

### Original DATA graph and whole method WIT contract

The inactive source capsule now also pins a closed value/reference transport
and a nativeKit-only WIT world. The world has the seventeen original method
exports plus `init`, and a separate six-function transport interface. It imports
no Core lifecycle, Gate, authority or ordinary Pack capability. The existing
upstream WIT parser test runs with the portable `tobkiri-wit` package; source
contract checks alone are not evidence that a component executes.

The registry holds actual original objects by identity within one account,
invocation, Store and generation. Equal but distinct values remain distinct;
released IDs are never reactivated. Original record classes come only from
trusted bootstrap code. Input frames construct only closed builtin DATA types
or reuse issued references, preserving nested aliases and supported cycles.
Descriptions are read views and cannot be re-imported as original values.
Opaque native bindings convey no authority and cannot be constructed by frames.

New input collections explicitly report every new held reference. NativeKit
must retain and release those holds at the correct original lifetime. Releasing
one hold does not destroy an object still reachable through another alias.
Only the needed, explicitly permitted DATA list append and dictionary set/copy
operations are available; re-importing a read-only reference cannot grant a
mutation permission. Failed registration removes partial map entries and its
transaction's new holds, including allocation failure during ownership tracking.

The portable Python source tests exercise identity, aliases, exact scalar bits,
cycles, invalid/foreign references, release, rollback and mutation boundaries.
They use explicit portable record fixtures and do not substitute fake Host
modules for the original classes. The complete method dispatcher, trusted
original-class bootstrap, native ordered inspection bindings, exact raised
exception encoding, component producer and actual nativeKit execution remain
unconnected. This slice does not change the default Launcher or GUI flow and
does not satisfy whole-path parity. Original four-MiB envelope and native
64-MiB account, fuel and deadline limits remain in force.

The next source slice adds the fixed dispatcher and thin two-interface app.
All generated per-export adapter instances use one module-global dispatcher.
Strict initialization validates DATA scope before committing state and never
replaces accepted state. The actual native owner remains an independent
nativeKit obligation; a self-reported Scope does not create it. Call frames carry
only ordered positional and keyword references, never a module, class or export
selector. Validation does not remove, fill or reorder arguments; Python's
original argument binder raises its original `TypeError` when appropriate.

Known raised exceptions stay as actual original objects in the same Store.
`method-raised` details carry a scoped reference, with an empty message meaning
**undisclosed text**, not an originally empty message. A normally returned
exception remains a successful value reference. The dispatcher does not format
exceptions or publish their fields; its bootstrap rejects exception field
descriptors until dedicated provenance-aware extraction is connected. This
retains class/args/chain/identity internally but does not establish their
cross-boundary observation or reconstruction. Unknown concrete failures remain
explicitly unsupported. Memory and recursion execution failures escape rather
than becoming successful domain replies.

Import and description results report every new held reference; existing refs
do not silently acquire new ownership or mutation permission. Input/result
encoding failure rolls back newly registered holds. Unit collection helpers
retain no hidden `None` result, and preserve the difference between an invalid
transport reference and an original dictionary `TypeError`.

The bootstrap names the original catalog, resolved Profile, decision, active
view and exception classes. The source assembler supplies their service DATA
facade below; native binding creation, ordered IO/admission ports and the complete
dependency closure remain unconnected. Native retained origins must remain
opaque capabilities, not reconstructed dataclasses. These source adapters have
not yet executed as a whole component; an import/closure failure remains a
failure. The next integration must supply the native bound-path/retained-origin
seam and the actual producer/native oracle.

### Explicit original service DATA facade

The source assembler now supplies the original qualified service module and a
nonempty package initializer for its DATA exports. It copies the original
`ActiveDefaultProfile` frozen dataclass, the three original reconfirmation/timeout
exception classes and `project_runtime_launch_selector` verbatim, with complete
decorators, bases, annotations and class defaults. The launch selector is DATA:
it checks existing plan/lock/activation records and canonical digests and returns
the existing contribution with its activation identity. It performs no IO,
authority publication or input mutation. It remains unconnected to a dedicated
whole-component invocation until that path is implemented and executed.

Original imports retain the same catalog, resolver, denial and canonical objects.
The source definition of `ResolvedDefaultProfile` and `ProfileResolutionDenied`
remains in `profile_resolution`, including its original compatibility `__module__`
assignments. Service and package modules reexport those objects rather than
defining substitutes. Generated files, original definition ranges/hashes, class
generation dependencies and static alias routes are recorded separately. Static
source checks do not prove those aliases in the pinned Wasm interpreter.

The original complete package initializer is preserved as inert custody input;
the generated initializer explicitly exports its original DATA names. Only the
native `ActivationStore` package export is omitted, without a replacement,
fallback or fake success. Its persistence, approval, recovery and registration
behavior remains required on the real Rust owner. Other excluded service
imports, caches and helpers still need a placement audit: their proximity to
native state does not establish that all are native. The package manifest keeps
native integration and complete service/Setup compatibility false. Bound-path
and retained-origin creation, ordered inspection/admission imports, dependency
closure, actual whole-component execution and original GUI adoption remain open.

### Fixed native inspection imports

`native_setup_store_with_imports` installs a static native-selected table before
`Store::new`. Each entry binds a dedicated `tobkiri-native:` interface/function,
an exact zero-parameter or one-`payload`-string signature and a function pointer.
The result is one DATA string. Duplicate entries, unknown functions, interface
shape changes and ordinary Core identities are refused. The existing constructor
retains its empty import surface. Table storage is prepaid control metadata with
physical destruction before reservation release; static names/code are borrowed.

The typed context must retain the same actual `NativeSetupInvocation`; serialized
Scope fields do not establish custody. Callback factories receive only their
registered typed binding state, original funded input, an actual-custody comparison
and a consumed `inspect` constructor. They cannot extract raw Store/Linker/Gate/IO,
return arbitrary futures/buffers or renew an account. `inspect` prepares one sealed
operation using the existing `NativeDataIo::inspect` and charged concrete
`BudgetedFuture`. Its private ticket is compared by actual shared identity, so an
operation from another call or invocation cannot be substituted. No worker starts
until kernel acceptance. The operation exposes no public Future/map/catch path;
factory allocation and writer failures prevent successful publication.

The kernel checks activity before dispatch, after the original worker/actual Join,
and before lowering. Input, concrete future, captured work, output and ABI copy
retain their original owners. The input is automatically held by the worker as
well as by the host call. UTF-8 and original envelope limits remain enforced.
Domain/exception DATA is explicit callback output; outer inspection/accounting,
panic, deadline and cancellation failures remain execution failures. Inventory
may use the same worker for metadata only, without filesystem IO or prefetch.

This API cannot fund an arbitrary `T` or its deep captures merely by charging its
outer handle. Registered nativeKit implementations must use charged constructors
for new deep storage and retain existing funded owners. Their worker must check
actual owner, target, operation, phase, occurrence and deadline at the original
reachpoint. Preparation must not consume an unexecuted occurrence as success or
retry canceled work. The kernel supplies no application path/Pack/authority rule.
Portable actual-Wasm tests are routed through CI for this mechanism; nativeKit's
bound paths, retained origins, full producer/oracle and original GUI integration
still remain unconnected.

The whole-native WIT now declares the exact separate
`tobkiri-native:harness/bindings@1.0.0` dependency: `inventory()` and
`inspect(payload: string)` each return one DATA string. These are the only
native imports; the seventeen original methods and six DATA helpers remain
unchanged, and no ordinary Core/Gate/lifecycle capability is introduced. The
upstream parser test checks the dependency identity, complete function sets and
exact signatures. Source capsule custody pins both WIT files. This declaration
does not implement the nativeKit binding inventory or path/admission callbacks;
the actual full component still requires that adapter and a native oracle.
