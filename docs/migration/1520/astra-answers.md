> Active resource policy: [resource-boundaries.md](resource-boundaries.md). The 2026-10-15 corrections supersede earlier guest-CodeOwner accounting requirements for trusted native control/compile/Tokio/Wasmtime infrastructure and mandatory SQLite-in-Wasm. Native SQLite and ordinary bounded Wasmtime constructors are allowed. Historical answers below do not override these corrections.

# Astra design answers for Issue 1520

On 2026-10-15 the user replaced the Opus adviser with `gpt-6-astra`, initially
at `max` effort and subsequently at `high`. All new consultations use `high`.
Astra answers design questions only, uses no tools and performs no implementation.
These decisions, including the follow-up answers below, supersede parts of the earlier
[Rust contract](rust-contract.md). They do not claim implementation or parity.

## Explicit port authority

Manifest consumes, exposed endpoints, compiled routes and invocation authority
are separate. Resolve each original declaration, ceiling, explicit Pack limit
and approval independently against exact views in the frozen revision; intersect
them afterward. Bindings must never create rights. Absent and empty port sets
deny; a missing explicit Pack limit denies. Provider minor compatibility may
serve an authorized caller view but cannot expose operations added outside it.
An ambiguous or unrepresentable view fails closed.

User, Input and UI principals require explicit authority. UI `ports_used` only
narrows it. A synthetic principal without a supported explicit policy remains
denied. The existing Native Input projection remains limited to its verified
own source and trigger-connected targets, with the original literal declaration,
explicit ceiling, explicit Pack limit and hash approval. Own-provided aliases
cannot introduce other targets.

An optional Profile `user_permissions` field represents explicit User policy.
Absent policy denies; old Profiles acquire no synthetic User rights. Published
Profiles may carry policy, but never its local approvals. User approval is scoped
to the exact Profile document hash, compiled revision and security epoch, in a
separate User approval domain. It must not use a fabricated Pack hash. Resolve
each policy source from its original declaration; do not resolve an already
expanded binding map as a new source of authority.

Literal `StepCall` and fixed `StreamRoute` emit targets in a hash-bound, frozen
Flow AST may provide structural call provenance without a manifest `consumes`
entry. They still require the original ceiling, explicit limit, approval and
exact view/operation. Computed targets and dynamic guest calls receive no such
exception. An owned Flow's virtual `implements` endpoint has structural
provenance, while the existing Native Input projection remains narrowly scoped.

## Lossless SQL cells and metadata

Introduce a versioned generic storage contract; do not silently change existing
unversioned JSON-cell calls. Each cell has exactly `type` and `value`:

```json
{"type":"null","value":null}
{"type":"integer","value":"-9223372036854775808"}
{"type":"real","value":"1e0"}
{"type":"text","value":"1"}
{"type":"blob","value":[0,255]}
```

Integers are canonical signed 64-bit decimal strings, without leading zeros,
plus signs, negative zero, exponents or JSON numbers. REAL is a finite `f64`
scientific string under the frozen round-trip rule. Transport preserves `-0e0`;
SQLite's storage behavior needs separate evidence. TEXT remains Unicode text,
including NUL and record-looking strings; invalid UTF-8 is an error. BLOB is a
byte list, never an implicit base64 string. Plain records are not parameters.

Python maps the tags to `None`, bounded `int`, finite `float`, `str` and `bytes`.
Queries need ordered metadata even for zero rows, preserving aliases and
duplicate names. Executions need affected-row and last-insert information for
the accepted DB-API subset. Transactions need a pinned session-owned connection
or opaque transaction handle. Cancellation before commit rolls back; successful
commit followed by delivery failure cannot be described as rollback. Row/byte
limits include serialization overhead and fail instead of truncating. The exact
follow-up contract is below; actual backend parity remains open.

Keep Core 1.0 unchanged and introduce a separate Core 1.1 package with the same
17 interface names. Its generic storage result contains ordered `columns`,
`rows`, optional canonical integer `affected-rows` and optional
`last-insert-rowid`. Add an opaque `sql-transaction` resource and typed
`exec-typed`, `query-typed` and `begin` methods. Existing untyped calls retain
their meanings and must use the same safe backend when available.

Accept one positional-parameter statement. Reject scripts, guest transaction
control, named parameters and unsupported conversion modes. Bounded preparation
uses schema-read authority and must not cause recovery writes. Check SQLite's
readonly and column-count results instead of classifying an SQL text prefix.
Obtain live Write authority before the first mutating step, for the same prepared
statement and arguments; automatic reprepare repeats the checks. A readonly
`exec-typed("SELECT ...")` can work with Read authority. `query-typed` rejects
mutation before stepping. The explicit result policy is `allow-rows` or
`forbid-rows`; batch execution rejects a result-producing statement before any
step, even if it would produce zero rows.

Implicit mutations run in a transaction and roll back on resource/output failure.
An explicit deferred transaction pins its connection and excludes other Store
SQL use of it. Execution failure makes it rollback-only. Repeating the same
terminal decision is stable; the opposite decision conflicts. Dirty commit
rechecks live Write authority and the pending mutation identity. Readonly commit
does not need Write authority. Rollback uses reserved cleanup and does not renew
deadline, epoch or arbitrary work authority. Dropping a handle requests rollback;
it is not a completion receipt. An uncertain commit must not be replayed.

Use legacy `sqlite3_prepare` with `SQLITE_OMIT_AUTORESET`, not a v2/v3 path that
can automatically reprepare and step inside SQLite. Seal full SQL bytes,
bindings, result policy and transaction identity. Initial automatic retry is
limited to one explicit reprepare, only after `SQLITE_SCHEMA` and proof that the
attempt produced no mutation, exposed row or uncertain effect. Resetting a
statement to obtain a specific error is not that proof. Recheck readonly,
columns, result policy and live Write authority before stepping the replacement.
Other errors and uncertain commits are not replayed; use RollbackOnly/cleanup.

The supported Python DB-API subset uses one transaction for `executemany`, lazy
deferred transactions, fetch methods, seven-field descriptions and snapshot
rowcount/lastrowid metadata. Unsupported dict/script/custom conversion behavior
fails explicitly. SDK routing tests do not establish safe SQLite execution.

One component may import both exact Core 1.0.0 and 1.1.0 packages. Validate each
version's exact interfaces/operations and preserve separate nominal resource
identities. Only regular aliases within one package share identity; structural
similarity or the same major version does not allow cross-version conversion.
Both versions share the execution's authority, quotas and stable owner funds.
They share grant management, not reuse of an already consumed grant. Admission
binds the actual exact target, and 1.1 operations stay inaccessible through 1.0.

## Historical invalid signatures

Preserve legacy preimage, seal and fixture bytes, verifier version and historical
result. Invalid acceptance is inert evidence, distinguished as historical
verification, current strict rejection, quarantined migration and no authority.
It cannot authorize Pack execution, publisher binding or approval. Do not weaken
strict verification. Preserve all 37 fixtures; measure each current result rather
than extrapolating from the identity-key example. Preservation/quarantine proof
does not establish acceptance equivalence or close unrelated migration rows.

## Cumulative Flow accounting

Each top-level execution has its own run-budget scope. Descendants inherit
ancestor allowances and keep applicable local caps; child runs cannot refresh
ancestor budgets. Loop iteration counts belong to a loop activation, while its
descendant work consumes applicable ancestor cost allowances. Use the existing
cost metric and one identity per chargeable event. Enforcing child and ancestor
caps is not a second billable event; never charge an aggregate again at child
completion.

Reserve atomically across relevant scopes before bounded work; deny without
partial reservations. Refund only proven unused capacity. Streams retain their
originating scopes throughout their lifetime. Cleanup must remain possible after
user-work exhaustion without authorizing new arbitrary guest work. Existing
local limits, deadlines and depth are interim protections; cumulative accounting
remains open until its generic propagation/charge contract is implemented and
tested.

The Core creates an opaque top-level ledger. Pure Gate/Flow types carry its
active scopes; descendants inherit every applicable ancestor scope and may add
local caps. A nested run must not create new ancestor funds. The existing metric
charges one actual physical Port dispatch, one actual nested Flow invocation or
one forwarded stream frame. Top-level Flow creation and virtual Input submission
are not additional ordinary Port dispatches. Atomically debit all applicable
caps at the final dispatch boundary, after required live authorization and
preconditions, rather than at a wrapper or guard. Do not bill child aggregate
work again at completion. Exhausted streams close before exposing another frame.
Reserved cleanup can free Core resources after work exhaustion/cancellation,
without admitting new guest work or refreshing the original provenance.

## Compiled call provenance and final admission

The compiler seals descriptors for each frozen AST call site: actual code owner,
owner Pack hash, compiled revision, node kind, exact caller view/operation and
resolved target binding. The trusted interpreter asks a Core-private factory for
a noncloneable activation only when it reaches that node in a verified run/frame.
A reusable AST descriptor is not itself a permission. Guest arguments, a
`CallCx` origin boolean or serialized proof cannot mint an activation.

Code ownership and effective authority are separate. A User overlay cannot
relabel a base AST as its own code. `Next` restores the actual underlying owner
and obtains fresh call-site activation. Entering a guest, callee or callback
strips the caller's activation; guest calls always follow dynamic consumes rules.
Step calls and each forwarded stream emit receive distinct activations.

Ordinary UI and compiled calls share one private noncloneable admission object
containing the same unconsumed grant and immutable binding/context. Final claim
rechecks principal, Profile, request, document/revision/epoch, arguments, exact
view, operation, frozen target, current provider limits, deadline and all dispatch
preconditions. It consumes that same grant once and atomically charges active
Flow scopes immediately before the physical dispatch. A charge failure after
consumption records a durable Failed terminal decision and performs no effect.
One admitted dispatch owns one terminal outcome. Additional wrapper checks do
not mint another grant or charge another dispatch.

## SQLite aggregate memory

Value lengths, output quotas, progress handlers and usage measurements do not
prove a hard aggregate allocation bound. The contract must cover preparation,
bindings, registers, caches, temporary structures, results and serialization,
including owner aggregates across connections. Memory, output, persistent disk
and temporary/journal disk are distinct limits.

The present native untrusted SQL path must fail unavailable before preparation
where hard memory isolation is required. Neither a global SQLite heap limit nor
an unreviewed allocator or process worker is approved. A bounded in-process Wasm
SQLite engine with capability VFS is approved as a private Core implementation,
without making Store depend on Wasm or creating a crate cycle. It is not a Pack
or a public WIT interface. Earlier native-SQL Harness receipts remain historical
evidence, not proof of this bound.

Owner funds use stable Profile identity plus stable Pack identity, with the
existing effective `memory_mib` as a conservative aggregate admission cap. Do
not reinterpret it as a new global Profile field. Every live/proposed approval
cap applies; a new Pack hash, revision, epoch, request, session or Store creates
no new funds. Account for all guest initial/growing linear memories, reserved
maximum SQL linear memory, external runtime stacks/tables, ABI buffers, host/VFS
state, retained I/O and bookkeeping. MEMSYS heap and a C stack inside SQL linear
memory are included there and are not charged twice.

One bounded process registry identifies an authority root from a safely held
physical directory handle. Its owner ledgers are shared across Core instances
and path aliases, and cannot disappear while any allocation/output/I/O retains
them. Path strings alone are insufficient. Across processes, acquire an
OS-enforced exclusive ownership lease before funded allocations and retain it
until actual occupancy and unresolved cleanup are gone. There is no TTL/PID
stealing or takeover merely because a Core closes. Unsupported lock semantics
deny admission. Existing Gate/CAS connections remain possible.

The `high` follow-up approves a stronger collision scheme: lock the retained
physical root directory inode as a single cross-process bucket for all owners.
Per-Profile/Pack accounting stays separate. All operations below the root must
use that held handle; do not re-resolve its old path. Registry acquisition and
final release must be synchronized with new reservations/admission. The FD is
private and its fork/exec inheritance must be controlled. Support is established
per OS/filesystem combination, never inferred for network filesystems. Required
proof includes rename, unrelated FD close, simultaneous admission/final release,
output retention after Core/Session close, fork/exec and reacquisition after a
process terminates. Mutable lock-file paths may not create a second lock domain.
This is design approval; unproved runtime paths remain unavailable.

### User and interpreter allocations

The `high` answer permits an internal stable User account alongside Pack
accounts: `PackOwner(root, Profile, Pack)` and
`UserOwner(root, Profile, established User principal)`. With one User principal
per Profile there is one User account. Documents, revisions, epochs, sessions,
requests and Flow names do not subdivide its funds. Its cap comes only from an
explicit applicable User memory policy intersected with the original ceiling
and current User approval. An absent memory cap denies; another resource cap is
not reinterpreted, and no Pack or unlimited Core account sponsors it.

The trusted producing boundary selects funds before allocating. Verified Pack
Flow evaluation/intermediates/events use that original Pack account. User-owned
overlay expressions, documents and their generated envelopes use the User
account. A User overlay neither relabels the base AST nor changes an existing
value's owner. Pure Flow receives a trusted account-bound AllocationBudget and
switches it only at verified plan ownership boundaries.

Existing guest-derived nodes retain their original leases. New containers,
reference metadata and serialization buffers are charged to their producer;
shared node payload is not charged twice. A target transfer first fixes target
authority and reserves destination funds. Copies retain both allocations during
their peak. A shared node cannot be reassigned by just one of its references.
Funding transfers no permission or AST provenance.

CLI/host routing metadata identifies root, Profile and funding principal before
materializing owned input. Caller-owned bytes may be borrowed and routing may
use fixed-size scanning. Variable owned buffers/parse trees need advance funding.
A format requiring an uncharged graph merely to find its sponsor is rejected.
User graph execution stays unavailable when User memory authority is missing.
Outstanding User allocations retain the same root exclusion and actual-drop
lifetime as Pack allocations, including values outliving Core/Session.

### DATA DB exclusion and WAL

The next `high` answer selects one connection at a time per physical DATA DB,
with new databases fixed to DELETE journal. Acquire the DB lease before opening
a connection and retain it through statements, transactions, I/O and confirmed
cleanup. Native KV, seed/register/transfer, Wasm SQL and every StoreHandle use the
same admission. Logical private-store aliases share `private.sqlite` and therefore
share the exclusion domain. Same-process registry sharing and the stronger
cross-process root bucket cover this scope. Separate approval/audit/catalog/CAS
resources stay outside the DATA DB lease; any operation that actually opens a
DATA DB still acquires it. Transfers acquire all DB leases in a fixed order.

For a new DB, a retained parent identity plus fixed basename serializes creation
and is safely associated with the created physical DB. Replacements and aliases
may not create a second domain. Unknown existing connections deny admission.
These application leases supplement SQLite's ordinary file-lock protocol.

Copied Wasm SHM pages with barrier writeback are rejected, even with a single
connection. For an existing WAL DB, the only selected candidate sets SQLite
exclusive locking mode before its first DB access and uses SQLite's private
WAL-index without shared-memory VFS. Prove this for the pinned build across WAL
read, write, checkpoint, recovery and close; preserve committed WAL-only data.
Unexpected SHM callbacks fail. Until the proof exists, existing WAL is unavailable.
Do not silently checkpoint or change journal mode on open. A separate explicit
WRITE migration can be proposed when this exclusive WAL path cannot support a DB.

The broker rejects persistent mutations during READ, including hot-journal
recovery, mutating WAL recovery and checkpoint. Recovery needing writes obtains
live Write authority first. Read-only PUBLIC data opens only when a no-write path
is established. Recovery and conversion keep fuel/deadline/I/O limits. Unknown
cleanup retains DB exclusion and capacity, and is quarantined. A fresh Wasm
recovery instance requires confirmed old-instance inactivity, the same exclusion
domain and any necessary live Write authority. An uncertain commit is not replayed.

## Host ABI allocation ownership

A narrow source patch to pinned Wasmtime 47.0.4 is approved. Every supported
guest-influenced lift/lower, nested container, temporary, resource/proxy table,
async frame, conversion and serialization allocation must reserve before it
allocates. Check byte count, element count, depth, work and arithmetic overflow.
Repeated aliases charge each actual copy; resizing accounts for the old and new
payloads while both exist. No global allocator or TLS interception is approved.
Ownerless execution and unproved static/custom lift or extension paths deny.

Allocation-owned leases travel with budget-bearing Wasmtime payloads and opaque
immutable `CoreValue` graphs, including returned values that outlive a Store,
Session or Core. Borrowed graph views are permitted. Public guest paths may not
return uncharged owned `String`, `Vec`, `CanonValue` or ordinary `serde_json`
trees. Trusted metadata parsing is a separate use of ordinary `CanonValue`.
Cheap sharing retains existing graph ownership; a detached subvalue retains its
owner. Deep copies and controlled encoders/decoders are fallible and precharged.
A transfer reserves the destination before releasing the source. Partial failure
destroys payload before returning its lease. Accounting metadata is bounded and
prepaid or inline; an unaccounted control block is not a safe workaround.

The proposed private sorted-Vec object representation avoids infallible hidden
tree-node allocations. Supported serialization includes its buffer and peak
copies. Borrowing from a trusted Rust host does not establish accounting for
arbitrary copies that host makes outside the Core boundary. Actual production
adapters must use the supported budgeted API. Required tests include small caps,
deep/aliased values, overflow, injected allocation failure, async retention,
cancellation, resource identity, revision changes, final-drop release and
serialization/transfer peaks. A call-wide allowance or post-lift length check
does not prove these properties.

## Durable terminal decisions and audit delivery

Use the existing persisted expiry without extension. Enforce deadlines on
admission, pulls and blocking waits, and add startup recovery plus a runtime
deadline supervisor. Expiry revokes use and signals live cancellation; a database
transition cannot prove an external effect stopped. Unknown cleanup is recorded
as deadline failure with effect/cleanup unknown, never successful cancellation.

Atomically commit a CAS-protected terminal decision, immutable event identity,
reservation disposition and complete terminal-audit outbox entry in the grant
database. Deliver it to a sink that deduplicates the identity and rejects
conflicting payloads, then acknowledge delivery. Same-decision retries are
stable; conflicting proposals cannot overwrite the winner. Do not claim atomic
transactions across the grant and audit databases.

Explicit close awaits cleanup and reports durable-write/delivery failures. Sink
outage retains retryable outbox entries without replaying effects. Bound retained
outbox capacity and stop admission when outcomes cannot be safely retained.
Expired authorization bookkeeping is separate from resource occupancy; unknown
live resources remain occupied or quarantined until cleanup is established.
Without a surviving runtime, recovery/delivery waits for next Core startup;
Drop cannot report errors. Graceful shutdown attempts bounded draining.

The later `high` review answer retains full delivered receipts and the finite
outbox limit. Eventual saturation honestly denies new admission; deleting
delivered bodies and retaining only a sink reference/hash is not permitted.
Admission reserves future terminal capacity for every simultaneous active
operation, including the proven maximum body and row/index/receipt/disposition
overhead, acknowledgement and retry. A terminal CAS/insertion consumes its
already reserved slot and must not use the full-box new-admission rejection.
General work cannot spend these reservations. Actual media/disk failure remains
a persistence failure. Lossless bounded compression/sharing is possible only
when exact original bytes and all retry/conflict behavior remain reconstructable.
An external archive/reclamation policy is separate future work, not permission
to discard current evidence.

Distinguish transient delivery failure from integrity fault internally, even
if the public error remains Persistence. Retry transient busy/outage with bounded
backoff instead of terminating the supervisor. A conflicting event identity,
invalid outbox row or decision/receipt mismatch puts the affected authority
domain in a durable admission-denied state, shared by every Core connection and
checked before consumption. Reserve bounded fault-diagnostic capacity too.
Retain/quarantine the disputed event and never cross its recovery watermark as
Ready. Independent fully verified existing outcomes may still deliver where
order/hash-chain requirements permit; their success does not reopen admission.
Existing original deadlines, cancellation, valid terminalization and reserved
nondispatch cleanup continue. Never invent an outcome for a corrupted decision.

A predefined lossless repair may run automatically only when independent
authoritative evidence uniquely recovers original identity, full bytes, decision
and receipt; admission resumes only after all integrity/recovery barriers pass.
Choosing between conflicting payloads, deleting records, swapping the sink or
accepting a history gap requires separate explicit authorization. Such an action
does not make missing history fully verified.

Proof must cover expiry/finish races, repeated completion and recovery, crashes
around terminal commit/sink insertion/acknowledgement, sink outage, runtime loss,
stale epochs and unknown cleanup. Each needs one terminal decision, no further
authorized pull and honest resource accounting.

## Guest UI registration

Every guest `request-ui-embed` returns typed unavailable without calling the
native host until a real Core Session registration bridge exists. A guest slot,
including `startup`, cannot establish lifecycle provenance. The eventual bridge
validates principal, Pack/hash/revision, epoch, declared UI authority and permitted
ports before host attachment. Core/host tokens never enter guest content, and
notifications must match the registered frame/session. Preserve the opaque
iframe with `allow-scripts` and no `allow-same-origin`; no ambient host bypass.

## Original Python producer reproducibility

Patch the original pinned componentize-py 0.25 preinitializer; do not invent a
second initializer or replace the frozen 91 expected cases. Set and verify
`PYTHONHASHSEED=0` before CPython starts. Derive build-only deterministic entropy
from a public versioned seed and canonical declared-plan digest, use the pinned
epoch/monotonic clock rule, and expose only declared readonly input trees.
Normalize paths, inode/metadata values, enumeration, environment, locale and
scheduling. Reject ambient host entropy, time, home, network, package discovery
and writes. Pin source, patch, dependencies, CPython, tools, flags, WAC, plan and
both inner/outer output digests.

The snapshot's deterministic RNG state must not supply runtime secrets/nonces.
Use the live Core random provider and prove that retained CPython RNG state and
cached aliases cannot bypass it. Changing one import or reseeding one instance
is insufficient. Require independent clean environments and repeated fresh
processes with identical inner and outer bytes, perturbation/denial probes, the
91 frozen case replay and actual resource ABI identity/drop checks. Earlier
unequal producer hashes are evidence of an unresolved problem, not a success.
The full producer build remains capacity-limited; no reproducibility claim is
made by this design document.

The final `high` follow-up rejects Python monkeypatching as a safety proof and
approves a source guard in the original CPython/runtime entropy primitives.
Build-only `BUILD_PREINIT` is sealed to `DORMANT`; runtime can never return to
the build mode. Runtime entropy uses a private typed bridge to existing Core
`random.bytes`. Cover `_PyOS_URandom`, its nonblocking variant, `os.urandom` and
implicit `_random` seeds; remove time/PID/public-seed/ambient-WASI fallbacks.
Saved Python function references still encounter this primitive guard.

The initial supported surface is the audited pinned standard-library paths for
`os.urandom`, `SystemRandom`, `secrets`, `_random.Random` and `random.Random`.
Unproved extensions, alternate CSPRNGs and entropy caches are unavailable.
Track seed provenance at constructors/primitives: reseed retained implicitly
seeded instances and `random._inst` from live Core entropy. Preserve explicit
application seeds for noncryptographic reproducibility; build entropy cannot be
relabeled as an explicit seed. Runtime `seed(None)` requires live entropy.
Unknown retained RNG state rejects artifact admission. The public deterministic
hash seed is separate and is not reseeded underneath existing dictionaries.

Before any public lifecycle handler, request or callback, Core privately moves
`DORMANT -> ACTIVATING`, binds owner/epoch, obtains necessary entropy through
normal Gate permission/approval/quota/audit, and completes reseeding before
`ACTIVE`. Reentry during activation denies. Denial or partial failure runs no
public handler and discards/quarantines the instance. A supported subset that
needs no randomness does not acquire an artificial mandatory random permission.
Epoch change invalidates activation. Shutdown retains live checks until its
handler ends; deactivation then denies entropy even through retained aliases.
It never restores build entropy.

Proof includes cached imports/default arguments/secrets aliases, implicit and
explicit seeds, pre-activation/partial-failure/epoch/shutdown/deactivation paths,
no fallback on denial, unsupported extension rejection, and actual Core entropy
provenance. Merely observing different random outputs is insufficient. Keep the
independent byte-reproducibility and unchanged 91-case proofs as separate gates.

## Final deadline and reservation identity follow-up

At consume, persist the minimum of the original reservation expiry and the
Core-verified permit deadline in the same transaction. Keep the original expiry
as provenance. Only a trusted internal reduce-only update may narrow it again;
runtime and supervisor use the same effective deadline. No guest timestamp,
extended expiry, new epoch or fresh grant is introduced by narrowing.

Deadline settlement uses the same terminal CAS with verified deadline cause and
ExpiredCleanupUnknown disposition. At its linearization point, an elapsed
deadline rejects a new success/Cancelled proposal. Reaper/supervisor losers read
and deliver the immutable winner. A legitimate pre-deadline winner stays intact.
Explicit cancellation is separate and may claim Cancelled only when its required
completion is established before expiry. Unknown physical occupancy stays held.

Fresh reservation identities combine an authority namespace/generation,
persistent checked sequence and a fresh 256-bit Core-only OS CSPRNG nonce under
a domain separator. Sequence, identity and reservation commit together. Retry of
the same reservation uses its stored identity/nonce. Entropy failure, sequence
exhaustion or detected collision denies admission. This bounded housekeeping
does not need guest random permission or create funds.

Permission writes and their terminal decision/disposition/outbox commit in one
approval-database transaction, with current grant, epoch and deadline rechecked.
Define the order of any self-induced policy/epoch update in that transaction.
An existing expiry winner applies no permission effect; same-terminal retries
do not reapply it. Failure rolls everything back. Sink delivery and external
awaits stay outside the transaction.

The final scope correction explicitly withdraws a mandatory external hardware
antirollback/CAS anchor. The local threat boundary trusts the OS and authority-root
administrator not to rewrite/roll back live authority storage. Guest, Pack,
Flow and UI routes receive no such trust and cannot perform unauthorized restore,
epoch rollback, grant reuse or DB replacement. Existing durable epoch/CAS,
root ownership, authority intersections and immutable outbox protections apply
under that boundary. A nonce is not protection against old approval resurrection.

Supported backup/restore requires consistent acquisition, explicit administration
with affected connections/executions stopped, outstanding ownership/unknown I/O
retained, a fresh internal authority generation and required policy/Pack/User
reapproval. Generation changes do not reset stable allocation funds. Preserve old
terminal identities/bytes and never replay unknown operations. Report any possible
history gap. Normal open rejects detected inconsistency, but local state cannot
detect a privileged restoration of a fully consistent old snapshot; whole-OS/VM
and CSPRNG-state rollback are outside this claim. Lack of an antirollback backend
alone must not disable normal Mac execution. Other unproved paths still deny.

## Captured producer inputs and staged original builds

The `high` adviser approved an immutable captured filesystem for the original
producer. Before CPython begins, describe every admitted input by canonical
guest path, type, size, content and digest; capture the bytes once. Later reads
must use that capture rather than reopen a mutable host tree. Host absolute
paths do not enter the seed or snapshot. Sort directory enumeration, fix the
declared epoch and modes, and assign deterministic unique inode identities from
the complete canonical guest-path set with collision checks. Reject symlinks,
undeclared paths, home discovery, network and writes. Generate bindings before
capture and disable bytecode writes. Bound filesystem descriptors, iterators,
streams, buffers and metadata.

The producer may retain its original Wasmtime/WASI 46 nominal resource types
only when every open, read, stat, list, link, write and drop path uses the
captured adapter without an ambient fallback. Actual original preinitialization,
mutation-after-capture, denial, metadata perturbation, byte identity and resource
lifetime tests are required before removing the filesystem gate.

Stage the original native CPython build tool, WASI CPython, C libraries and
synchronous/asynchronous runtimes sequentially before invoking the outer
compiler's Cargo build. Use one shared target and profile; do not recursively
invoke Cargo while that target is locked or restore separate runtime targets.
The original packaging hook verifies actual staged files against receipts for
all source, patches, tools, flags, environment, outputs and digests. A cache
label is insufficient. Missing or mismatched stages deny; there is no download
or binary replacement fallback. Native CPython remains a build tool, and the
original guest preinitializer runs once.

Reusing the verified SDK 34 arm64 toolchain is approved with exact compiler,
linker, sysroot, tool hashes and flags in a new producer recipe. PATH overrides
deny. Two independent clean builds with that recipe must have identical inner
and outer bytes and pass the unchanged 91 cases and ABI/provenance checks.
Byte identity with an older SDK 33 artifact is not required. This approves the
recipe, not an unperformed compiler or guest execution.

## Active execution frames, Flow entry and overlays

Funding follows the verified allocation-producing CodeOwner's own approved
cap. Do not unconditionally intersect it with another effective principal's
numeric memory or fuel cap. Separately enforce applicable explicit transitive
constraints, inherited cost and active loop scopes. Keep the minimum cap over
live and proposed owner leases; a User overlay's cap does not become a new
Profile-global budget.

A registered owner alone cannot sponsor an allocation. The Core-issued opaque
active frame refers to the actual plan, node and execution activation, producing
reason, frozen provenance, current approval and entry/overlay/resume route.
The Core factory maintains frame state, continuation positions and reached-node
activations. Supplying a node ID is not proof of reaching it; each loop iteration
gets a fresh node activation. Returned budgets bind to that frame/node and
cannot fund another operation. These checks do not revoke destruction of an
existing allocation or already reserved cleanup.

The adviser froze the private adapter contracts:

- `enter_provided_flow(caller_frame, exact_target, actual_args_digest,
  consumed_flow_run_grant)` validates one admitted call and creates the callee
  frame with its own declaration, original ceiling, explicit limit and approval.
- `enter_verified_overlay(base_frame, frozen_chain_position)` enters only the
  fixed invocation's next wrapper, without another grant or cost.
- `resume_captured_base(overlay_frame, continuation)` restores the saved base
  authority and constraints; it does not infer authority from an owner name.
- `budget_at_reached_node(frame, node_id, allocation_reason)` returns only the
  budget for that verified current node activation.

Direct User Flow execution remains User authority even when it references a
Pack AST. A formal provided Pack Flow entry has Pack authority. Preserve explicit
transitive constraints, deadlines, cancellation, cost ledger and active loops
across entry. Strip caller structural exceptions and use the callee's own
frozen plan. Next restores either Pack or User according to its captured base.

Pure Flow entry does not need an additional guest-only WasmEntry grant. The
caller dispatcher owns the original FlowRun's terminalization both when callee
entry rejects after consumption and after callee execution. Independent effects
inside the callee retain their own grants and dispatchers. Overlay and Next do
not own another terminal result. A deadline CAS winner remains immutable.
Rejection before admission does not invent a terminal for an unconsumed grant.

## Internal input routing admission

Replace a fictional port operation named `submit` with the internal
`VirtualInputSubmit` effect, without adding a public Core WIT interface. An
actual declared utility submit operation remains an ordinary PortCall.

Its explicit compiled CoreIngressPolicy differs by origin. NativeInput uses the
verified source Pack's Input declaration, exact Profile trigger plan and current
source approval. User/UI uses a Profile-owned default-deny policy binding exact
source view/op, exact ordered routes, caller principal and actual approval
provenance. Keep User document hash and Profile revision/epoch as their distinct
approval subjects. A trigger binding is topology, not authorization.

Core builds the ingress identity, invocation, exact source/op, ordered frozen
routes, payload digest, principal/approval provenance and source evidence.
Native evidence is a private physical source-read frame/receipt matching the
actual payload. User/UI evidence is an approved ingress invocation matching its
payload, without forged physical-source provenance. Validate the destination's
exact structural implements and caller's existing literal rights separately.
The routing grant cannot substitute for downstream grants.

One virtual routing grant/audit costs zero. A physical source read has one
PortCall grant/audit and costs one. Each destination has its own FlowRun grant,
outcome and nested cost of one. A direct top-level Flow retains its existing
zero nested cost.

## Final trusted host transport boundary

Core ownership includes guest ABI/runtime allocation, CoreValue graphs and
projections, serialization metadata, controlled CoreBuffer output, and Core or
observable adapter buffers, queues and futures until physical destruction.
Output serialization funds its verified producing activation; the destination
UI cannot relabel that owner.

A final trusted Launcher/CLI/Tauri consumer may hold CoreBuffer while copying
into a distinct bounded host allocation. CoreBuffer is freed only when its last
actual Core reference is destroyed. Transferring it into a naked Vec and
refunding early is forbidden. Guest proxies and intermediate Flow processing
cannot use this final-consumer exception.

Bound encoded bytes, queue bytes, in-flight counts/bytes and serialization
expansion before allocating; reject or apply backpressure at capacity. Bind
message IDs and ACK/cancel to session generation and receiver. Stale/duplicate
ACK cannot release another ticket. Bound UI queues, display history and caches.
ACK completes a logical flow-control ticket, not physical browser GC. Known
adapter allocations with unknown cleanup retain/quarantine their leases.
Unobserved Tauri/Webview internal copies are outside the Core ledger's physical
memory claim; neither whole-host RSS caps nor collection after ACK are claimed.
Actual delayed-ACK, cancellation, receiver-crash, queue-saturation, expansion
and CoreBuffer-drop tests remain required.

## Node retirement and the final encoder

The final `high` answer adopts encoding within a live producing activation,
without adding a deferred-output continuation mechanism. RAII retirement closes
new reservations and admissions in synchronization with their checks. Unclaimed
admissions recheck scope liveness at final claim. Already claimed effects retain
terminalization, deadlines and reserved cleanup. Retirement is neither a terminal
outcome nor evidence of physical release. Await/yield does not retire a node;
future drop does not establish completion of underlying native I/O.

For a direct final-consumer PortCall, run the fixed bounded Core encoder inside
the provider activation before retirement and terminalization, returning a funded
CoreBuffer. This is not an arbitrary effect/reentrant callback. Encoding failure
does not undo an effect already performed; preserve its actual/unknown state and
do not replay PortCall as an encoding retry.

Intermediate Flow calls still return CoreValue. When Flow encodes its final
output, charge the new serialization metadata and CoreBuffer to the actual
verified Flow return/output node that creates them. The original graph retains
its original provider leases; both allocations remain funded during copying.
Do not revive a retired provider budget or choose an arbitrary registered sponsor.
Insufficient output-node funding denies. A completed CoreBuffer may remain held
or be resent through the approved bounded transport without reviving its producer;
re-encoding requires a live legitimate producing activation.


## High answers: funded admission metadata, cancellation, and exact Input codecs

The adviser answered questions only. Descriptor/registry storage that grows with Profile contents requires the actual compile/load reservation and retained owner lifetime. Runtime consumers borrow or clone prepaid handles without allocation. Compiler metadata is not an unpaid Arc exception. For invocation admission, borrow the incoming context, frozen descriptor and actual User/UI, read-entry or caller proof; verify and reserve the legitimate producing owner before fallible immutable storage. Final Gate claim precedes callee preparation. Initial admission must not require its future callee frame. Shared mutable context must not change a saved binding.

`tokio_util::CancellationToken::child_token()` has no approved allocation exception. Replace it with a private prepaid, fallible carrier. Parent cancellation propagates to descendants; child cancellation does not affect parent or siblings. Clone allocates nothing. Reserve child/link and waiter storage before construction or registration. Cancellation/registration races must not lose wakes; invoke wakers outside locks. A dropped waiting future unregisters its waiter, without proving native I/O cleanup. Keep deadline and explicit-cancel terminal dispositions distinct. Tokio adapters must expose the same fallible cancellation future and account for their own registration/waker storage.

Exact Native Input signatures permit direct typed returns, multiple parameters whose resolved WIT type is an explicit empty record, and unit/empty payloads. Construct and decode each according to the common exact WIT codec. Zero-result, empty record and result success without payload retain their codec distinctions; do not invent an ingress-only null/object representation. Error branches remain read failures. Required nonempty arguments are not synthesized. Exact selection, approval, grants and receipts do not change. Stream/callback lifetime and delivery remain unfinished compatibility work; denial does not fulfill that requirement.
