# Active resource boundaries for issue #1520

## Shared core-module loading — source change, execution proof pending

`runtime.core_modules` maps exact root component import names to files in the
ordinary Pack inventory. The manifest signature and Pack hash cover the mapping
and each dependency's digest and size; Profile pins therefore change with this
closure. An absent or empty mapping keeps existing canonical manifests unchanged.
There are at most 256 bindings per Pack. Names cannot shadow Core or WASI
interfaces, and every binding must match an actual module import. Wasmtime's
ordinary linker checks complete module types before executing guest code.

The existing Host catalog prepays dependency-index metadata. Root source bytes
count separately for every logical Pack. Only explicit module dependencies share
compiled code by their actual captured SHA-256 and length, within one Engine.
Each Pack's distinct root-plus-module closure remains limited to 32 MiB; all roots
plus distinct dependencies remain limited to 64 MiB. These input limits do not
create another heap account. Reads and cache-hit revalidation use the original
source account and private snapshots, one temporary source buffer at a time.

Only immutable compiled modules are shared. Instantiation still uses each Pack's
own Store, memory, table, globals, restricted WASI, host callbacks and CallCx under
the existing initialization grant. No core instance or ambient host module is
cached. New regressions cover signature/pin changes, source limits, exact imports,
type rejection, and independent mutable state and cancellation. Their execution
results, transformed original five-Pack adoption and full production activation
remain pending; the original Launcher remains the default.

## Earlier execution record

Latest execution cutoff: **587**. See the [current boundary below](#current-resource-and-custody-boundary-through-587). Earlier sections retain their historical source and execution boundaries. Current counts: 316 validation records, 16 focused FS records, 1/837 closed and 836 TODO. Later588+ executions are excluded.

Issue #1520 is authoritative. Astra high corrected the resource boundary on 2026-10-15 after rereading R2.5, R6.1 and R7. Earlier answers that required every trusted native control allocation to be charged to a guest CodeOwner, a universal Wasmtime allocator, or a Wasm implementation of SQLite are superseded. These corrections do not declare implementation complete.

| Resource | Required accounting |
| --- | --- |
| Wasm memory, guest execution resources, guest-sized ABI values, value graphs and output buffers | The approved producing owner, admission before allocation, bounded work, and actual lifetime custody. Large guest data must not be relabeled metadata. |
| Profile compile/load metadata, bounded invocation control, Gate registration, Tokio task/scheduler/cancellation control | A separate bounded Core control account. Check size, depth, node/ID counts, retained revisions, active tasks/waiters/queues and registration counts before construction; apply backpressure and retain custody for work whose ending is unconfirmed. This grants no Pack or User authority. |
| Trusted host transport and Webview | Bounded input/retention/concurrency and the existing transport protocol. No whole-process RSS or GC release claim. |

Standard Arc, BTreeMap, Tokio and CancellationToken internal allocation is not by itself a reason to disable execution. A Tokio allocator fork, private cancellation implementation and fallible erasure of every control record are optional hardening, not completion prerequisites. Existing guest payload and permission checks remain mandatory.

Ordinary Wasmtime async and WASI constructors are allowed. Bound initial and growing Wasm memory, tables, instances, fuel and epoch/deadline execution. Bound component bytes, compilation concurrency, cache retention, host handles, tasks and queues through Core control policy. Exact producing-owner interception of every JIT object, fiber, host future, diagnostic, resource-table control object or destructor is optional. Preserve pre-lift protection against repeated descriptors expanding into excessive host lists or strings: checking only after lifting is insufficient. Constructor availability never authorizes ambient filesystem, network, environment or randomness access; reachable capabilities still pass the approved bindings and Gate.

Generated WASI bindings may use standard owned String/Vec types without replacing every generated trait. Admit cumulative bytes, elements, nodes, depth and expansion before materialization, counting every alias-induced copy, and bound concurrent calls and retained inputs. Operation-specific bounded paths, argv, headers and resource names use explicit native call/resource budgets; arbitrary file/network bodies and accumulated output remain owner-bound guest payload. Handles/tasks/fixed control state use CoreControl. A call envelope retains reservations until its arguments and actual host future are destroyed. Retained resource metadata carries its native reservation; retained payload carries a companion owner/lease record. Reserve before copying and transfer custody without gaps on moves. Removing a table entry does not refund reservations while a future/shared reference still retains data. Source-frame authority lifetime and allocation custody are distinct. Never strip existing CoreValue/CoreBuffer leases. Deny an individually unsupported retention path rather than disabling every standard owned type or every WASI constructor. These boundaries do not promise whole-process RSS accounting.

Rust Core may use native SQLite/rusqlite for guest DATA. SQLite-in-Wasm and its fixed 16 MiB heap/private WAL broker are optional experiments. Enforce Profile ownership, separate Pack/tier databases and the R7.2 access/approval rules. Guest SQL or path APIs must never reach authority, approvals, audit or catalog databases. Before preparing SQL, install supported length, complexity, column, value, result, cache/temp, connection, concurrency and execution limits. Enforce actual readonly statement classification and reprepare checks, disable extensions and unauthorized ATTACH/external-file/PRAGMA escapes, and bound busy waits with progress cancellation/interrupt. Enforce disk policy for the DB, WAL, journals and temporary files; a main-database page limit alone is insufficient. Preserve transaction custody, rollback, immutable outcomes and no replay after uncertain commit. Native SQLite's normal locking/WAL is allowed; constraints specific to a copied-SHM custom VFS are not universal requirements. Cache policy is not an exact native heap or whole-process RSS guarantee.

The guest DATA quota VFS delegates normal SQLite I/O and locking while bounding logical file extents plus outstanding reservations. Group the actual DB with WAL, journal, SHM and managed temporary files; UserGlobal group identity excludes the caller Profile. A short cross-process quota-domain lock makes scan/check, reservation, delegated size-changing I/O and actual-size reconciliation atomic; do not reacquire a long-held owner lock. Cover creation, delete-on-close, writes, truncation, SHM mapping/unmapping and file-control preallocation; disable unnecessary size/chunk hints and DB mmap. Count existing sidecars at restart. Partial failure uses actual size; unknown size retains reservations and quarantines the group. Unlink alone does not release custody while handles or mappings remain. Before mutation protect conservative rollback/recovery space and WAL checkpoint capacity from ordinary writes; quota exhaustion refuses new writes and never deletes recovery state. Apply safety caps together with actual Profile/Pack storage policy. This policy does not claim filesystem-block, metadata or all-OS disk accounting, nor protection against external disk exhaustion. A custom copied-SHM design and exact prediction of future SQL growth are not required.

A trusted load operation first admits bounded Core load capacity, then enforces input bytes, metadata size, AST nodes/depth and entries while compiling. One canonical shared CompiledProfile owns the AST. Flow borrows exact entries from that catalog, without per-Flow AST copies. Concurrent loads and retained old catalogs count against the same Core account. No initial guest sponsor or invented User cap is required.

A completion carries its actual live origin. A local overlay return followed by base resumption uses the eventual base output phase. An overlay that completes the invocation, including a final return after Next, uses that overlay's output phase. Retain the source graph's original lease, reserve the distinct serialization allocation under the real completing code owner, and encode before retiring that scope.

Flow progress must stream before completion. Bounded Core-generated progress/status/error summaries, event envelopes, batch records, queues and waiters belong to the actual invocation/stream's native Core control account, including bounded terminal draining. They need neither a synthetic guest AST node nor an extra Gate grant. Guest-valued event payloads retain their original CoreValue leases; producing or transforming guest values still requires the actual producing scope. Guest-derived error text remains a budgeted payload or a deliberately bounded summary.

The fixed RunEvent codec for a final trusted-host consumer may allocate a CoreBuffer from a bounded native transport budget, without a live FlowReturn. Bound event/payload size, encoded/batch size, queue/in-flight bytes and counts, and encoding concurrency before growth, including escaping expansion; retain separate source and encoded-buffer custody and apply backpressure. This exception permits progress, cancellation and error-only runs, not arbitrary guest computation or intermediate expansion. A successful Flow result's first serialization still uses its actual completion origin; later fixed framing/copying may use the transport budget while retaining the source buffer. Terminal draining never revives guest execution or retired scopes.

Per-factory registration retains exact factory/Profile/revision/registration identity. Session close synchronizes new-work closure and removes only its own registration. Old scope handles never attach to a replacement. Claimed terminal/deadline/reserved cleanup and unknown-operation custody survive logical unregistering.

Native Input fanout uses one Core aggregate handle and one zero-cost routing terminal with the original nonclone source-read receipt. Every frozen route has a separate FlowRun admission, terminal and actual execution frame. Sequential dispatch is allowed, with each child's progress streamed immediately and tagged by route. A failed route does not silently skip the others; cancellation, deadline or another applicable bound may stop new children. Final results preserve frozen route order in a fixed native host envelope, retaining each already-encoded child buffer and bounded error summary. Bound children, events, retained results and transport before admission. Overall success requires every route to succeed. Preserve the immutable deadline winner, represent unstarted routes, retain unfinished cleanup custody and never replay partial failure.

User-global DATA resolves to the same account-root physical store across Profiles. A Profile-relative path is not global storage. Every access still requires explicit dangerous cross-profile authorization, applicable caller limits/approval and store access rules. Key shared connection and disk quota coordination by actual global database identity, including WAL, journals and temporary data, independently of caller Profile. Native SQLite locking and bounded Core lifetime management are sufficient; no new global allocation-owner category is required. A retained root/DB/lock handle grants neither authority nor funding. Profile-scoped UserOwnerLease remains the guest allocation account. Prevent root/path/SQL aliasing of authority and audit databases.

Current evidence and its exact source limits are recorded in [rust-progress.md](rust-progress.md). Acceptance still requires normal Pack linking, hostile-size/cancellation cases, working private/public/readonly DATA and persistence, the full migration and actual Harness/provider/local API 1B replay. A passing historical checkpoint is not current runtime acceptance.


Python Pack migration may use a version/hash-pinned official componentize-py wheel at development time. Native Python used by the compiler or legacy compatibility oracle is not a product runtime. Product hosts remain Rust/in-process and ship and run verified Wasm components without invoking native Python or a compiler. Record actual source/manifest/generated-bindings/tool/options/artifact identities, verify real exact WIT imports/exports and resource own/borrow/drop/foreign-handle behavior, and hide the private initializer through typed outer composition. Initialization and every guest call remain subject to actual Wasm memory/table/instance/fuel/deadline limits, owned guest ABI payload limits and Gate-controlled generic imports. Runtime cryptographic entropy must use the actual gated random source, never a build-time/stub seed. Noncryptographic snapshot PRNG state is a separate issue. R11.4 canonical/seal/digest byte compatibility is not a requirement for two compiler builds to produce identical Wasm artifacts. The full custom CPython/componentize-py producer, complete native compiler/GC/cache owner instrumentation, independent source rebuild of LLVM/dyld closure and BUILD-02 staging are optional hardening/reproducibility prototypes, not completion prerequisites. Preserve them as prototypes; their unimplemented guards cannot block the ordinary verified-wheel path. Current Core execution of the new artifact, all original 91 differential cases, ABI/hostile-size/cancellation checks and actual migrated Pack/Harness paths remain required. Syntax checks and historical outer-component runs do not establish this acceptance.


## 2026-10-15 Astra high Q9–Q11: lifecycle custody and real User/Flow entries

This decision is authoritative over earlier proposals. Astra answered design questions only; workers implement and test the decisions.

- Native host navigation/destruction transfers the exact active `(generation, Arc<Session>)` into one bounded retiring-session slot under the same authoritative synchronized publication state. Request shutdown outside state/operation locks. A single bounded native cleanup task owns that exact Session and awaits `close`; it never calls a generic close-current operation. Start waits for cleanup. Failure or unknown completion retains the Arc and occupied slot; task scheduling failure must not lose custody. Synchronous shutdown closes new work and signals cancellation/factory closing, without claiming physical cleanup.
- Actual UI operations run in one bounded native task per Running record. IPC future drop signals cancellation; it never deletes the actual operation. Retire only after actual operation/required cleanup completion or an explicit verified custody transfer. Cancelled-but-unfinished tasks remain counted. Existing bounded Launcher native-control accounting is sufficient; no guest-funded task framework or universal native allocator rewrite is required. Transport ticket retirement is a logical boundary, not proof that Core I/O has stopped.
- A Core-private UserInvocationPreparation and FlowUserInvocation are approved for trusted CLI/User actions. Bind actual Session, registered User policy/document/revision/epoch, exact target/operation, bounded borrowed arguments, User-funded RuntimeValue and actual argument digest. Use the same single caller PortCall and existing ProviderEntryBridge; retain sole terminal custody, callee own activation/approval and declaration/ceiling/explicit Pack limits. Missing User cap/approval denies rather than charging the target Pack. Keep unprepared Direct entry closed. Trusted CLI argv/stdin JSON needs bounded host parsing and actual User payload funding, with no extra NativeRead or VirtualInputSubmit permission. This does not authorize guest stdin/filesystem effects. User document identity is the real registered policy document, not a synthetic Flow AST or fake UI source.
- `manifest.flows` plus the verified Flow document implements declaration is a real Flow endpoint declaration. Register a dedicated, revision-scoped frozen Flow endpoint catalog binding owner Pack/hash, exact manifest flow key, frozen Flow document/AST identity, validated implements view and entry signature, and compiled Profile/revision. Ordinary port bindings and ports.provides membership are not prerequisites. Do not synthesize a Port operation/PortCall. Multiple Flow endpoints sharing a view must remain distinguishable by exact key/route, never first-match resolution. NativeInput selects the exact frozen route and consumes one FlowRun per route. Guest run-flow uses the same catalog while retaining dynamic consumes, caller permission intersection, current approval, and actual Profile binding constraints. Catalog/route presence does not confer authority.


## 2026-10-15 Astra high Q12: durable exact Flow identity

Use `EffectRequest::FlowRun { implements, flow_owner, flow_key, target_digest }`. The target digest canonically binds the frozen target's stable owner/hash, exact key, plan/AST identity, full implements/input view and entry signature. Include Profile/revision when not already bound by the common durable grant; never include process/factory addresses or ephemeral handles. Preserve the actual argument digest as a separate admission/consume binding. Admission compares the current registered catalog descriptor with effect fields; consume/claim compares the saved target with actual dispatch; frame preparation compares the consumed target with Core-private proof and actual Flow plan/key. A private live witness alone is insufficient to distinguish durable grants for two same-owner/same-view Flows. Permission granularity remains the existing implements policy; exact target identity adds no permission. Every repeat still needs a fresh grant. Migrate the unpublished Rust request/fixtures without inferring missing old target identities. Existing canonical/digest algorithms remain unchanged.

## 2026-10-15 Astra high Q13: commit certainty and retained connections

A confirmed SQL commit and an observed guest reply are separate facts. Latch successful RELEASE or COMMIT before cancellation and deadline postchecks. A later typed-result allocation, cancellation or terminal-delivery failure requires a fixed no-replay result; it does not by itself require physical connection quarantine. Preserve the existing durable consumed grant and immutable terminal winner. Public `CommitUnknown` wording alone must not decide physical quarantine. Read-only failures and transactions that have not committed remain distinct. A small noncloneable, pre-admitted receipt retains the actual operation and store through typed result construction and terminal persistence; public result JSON cannot manufacture a native commit proof. Later canonical lowering and guest execution require their own failure evidence before claiming complete delivery coverage.

Actual SQL or close uncertainty retains the exact connection, VFS and owner custody in an already admitted bounded slot. Ordinary handle drop must not lose that custody or permit a same-database reopen. Reserve the slot before opening, serialize quarantine against work admission, and release it only after confirmed physical close. Reuse the guest DATA registration and physical quota domain; do not add an unbounded registry, self-Arc cycle or trusted-authority connection retention. Failure handling must not allocate recovery capacity, replay a mutation or overwrite an existing Gate deadline decision.

## 2026-10-15 Astra high Q14: finite component structure limits

Internal core instances created by a composed component are distinct from Pack instances. The fixed limit of 16 rejected the actual official Python component at its seventeenth internal instance. A finite native structure policy may raise only the demonstrated instance ceiling to 1,024 while retaining 16 memory objects, 16 table objects and all effective aggregate memory, table-element, fuel, deadline and output limits. Admit a bounded native Store control envelope before construction and retain it through actual native Store destruction, including user-data extraction. This is a native control allowance, not an exact JIT or process-RSS measurement. Existing component-size, compilation, catalog and cache bounds still apply. Exact resolved instantiation-graph analysis and a new permission schema are not prerequisites. Verify the supported component workload, over-limit rejection, aggregate guest limits, failed admission, cancellation and teardown; startup alone is insufficient.

## 2026-10-15 Astra high Q15: durable DATA move intent

An in-memory rollback guard does not cover process death between a DATA rename and its authority-catalog update. Admit a bounded, canonical move intent in the trusted catalog and durably persist PREPARED before moving the exact pinned database and its managed journals. Synchronize the affected directories before atomically committing ownership, seed versions and COMMITTED state in one catalog transaction. Delete the intent only after the outcome is established. A confirmed precommit rollback must restore and synchronize the exact files before clearing its intent; a committed or uncertain operation must never blindly reverse them.

Pending moves block both affected logical endpoints, including register, open, seed, metadata changes, orphaning and further transfer. Synchronize that check with the actual catalog admission, including independent catalog connections. Corrupt or unreadable intent state fails closed. The interim implementation may retain the files and deny affected operations until explicit repair; automatic reconciliation is not a prerequisite for this bounded fail-closed checkpoint. This does not claim complete crash recovery or power-loss safety. Verify real process termination at each protocol checkpoint, fresh-process reopen, preserved database/journal bytes and continued access to unrelated DATA.

## 2026-10-15 Astra high Q16: filesystem, processes and Wasm reviewers

These decisions specify the next implementation slices; they do not establish backend availability. A Core-issued non-reused filesystem root identity binds an actual pinned directory object and exact policy. A short-lived single-use open token binds that root, caller, Profile revision, Pack/hash, epoch, rights ceiling and expiry. It cannot add rights or a root to policy. A newly selected root needs an explicit configuration ceremony and resulting exact policy/hash approval before token issuance. `FsOperation::Open` requires an exact-root read or write permission; later I/O still requires its own exact grant. Operation digests use resolved root identity and canonical arguments, including write content and both rename paths, never a guest resource-table number.

Resolve every component relative to pinned descriptors with no symlink traversal; reject absolute/traversal paths, special files and hardlink aliases. Protect live Core authority, audit, catalog, approval and secret subtrees, their ancestors, both rename endpoints and replacement/staging objects. Serialize Core-mediated namespace mutation across overlapping grants. Whole-file non-append writes use confined atomic replacement; append can leave a prefix and needs a fixed no-replay uncertainty result. Retain actual native work, FDs, input leases and completion evidence after importer cancellation through physical completion and delivery handling.

Astra clarified that an OS jail or immutable broker namespace is not a prerequisite for externally edited user workspaces. The accepted threat boundary is a malicious Wasm guest using its declared capabilities, not an independently malicious same-UID host process. Trusted external editors can still move descriptors' objects or introduce aliases after checks; document those concurrency limits without claiming external serialization. This clarification supersedes the first Q16 answer's broader broker-namespace requirement. Unsupported platform/confinement paths remain unavailable until implemented.

Process launch uses a bounded private snapshot of the actual executable, argv, environment and cwd capability. Its durable effect binds executable identity/digest, ordered argument and environment commitments, cwd, terminal parameters, policy and limits. Use versioned, domain-separated keyed commitments for secret-bearing strings. Default prompt redaction hides arbitrary strings; show trusted catalog identity, counts, bounds and explicitly displayable schema fields. A missing cwd uses a provisioned pinned empty directory, never the Core process cwd. One consumed spawn grant can cover its exact process session's bounded I/O/control methods under the original deadline/epoch. Successful completion requires exit, settled output and reap; immutable unknown outcomes do not release unfinished tracked work or permit replay.

Bounded Core orchestration, owner-funded I/O, wall/output limits and process-group cleanup do not claim native RSS or containment of detached descendants. R2.5's Wasm limits do not impose a new universal native RSS/jail prerequisite on explicitly Dangerous unsandboxed execution. Enforce any explicitly requested stronger OS limits or reject that launch. True Native typed-port launch and platform variants remain incomplete until their protocol and verification-to-launch binding are specified; a Wasm CLI adapter is the Proc path.

Wasm reviewers require an approved ordered, bounded Profile list of exact Pack/hash, entry point, role and limits. Broker service ports do not automatically become reviewers. Native configuration approval prepares and atomically activates the list; failure prevents activation. Core-private review/init admission binds the real reservation or candidate record, reviewer, revision, position, input commitment, epoch, deadline and budget. Ordinary nested effects reenter policy; reject ancestry cycles instead of skipping an active reviewer or waiting on itself. A fresh opaque pending token binds the exact reservation/effect/args/reviewer/prompt identity, is invocation-scoped and accepts one verdict. Pass/Deny/Constrain can only narrow; native/device decisions alone provide trusted approval. Cancellation may stop the wait while a bounded task retains actual reviewer, terminal and cleanup custody; late verdicts cannot replace an immutable outcome. The existing empty chain, droppable review future and unavailable pending/attestation backend do not satisfy this migration.

## 2026-10-15 Astra high Q17: nominal Core error compatibility

A public generic call may preserve an existing Core error category only when both the actual caller-facing and provider-facing outer result error arms resolve, through aliases alone, to the frozen authoritative Core error type for a supported version. A matching record shape, field names, imported error-code enum, nested error or Core type elsewhere in the signature is insufficient. Bind the immutable proof to the verified package/interface, operation signature and actual provider revision; reject conflicting Core definitions, unsupported versions and inconsistent signatures.

Construct and freeze this proof once during bounded, admitted catalog preparation. Bound document bytes, parser syntax depth/work, expanded aliases and retained operation metadata before their corresponding construction. Calls consult the frozen binding without parsing WIT. For a valid nominal error, emit only the closed Core category and its fixed message; never forward guest text. A malformed payload, unknown code, lookalike or one-sided nominal binding remains a provider failure.

This changes interpretation of an already admitted typed result, without adding permission or a per-call ceremony. A projected `denied` remains a failed provider outcome, not a Gate denial, approval or authority proof. Acceptance includes the unchanged official Python 91-case replay and real public-Core alias-positive, lookalike and one-sided binding negatives, with distinctive guest messages absent from public errors and durable audit. Passing type inspection alone does not satisfy those runtime checks.

## 2026-10-15 Astra high Q18–Q21: cancellation and completion evidence

These answers specify required behavior. They do not claim that the complete
stream lifecycle or child census is implemented.

A manual cancellation records its intent synchronously on the exact existing
stream/execution cleanup record before waking cancellation. Bind the owning
execution, non-reused identity, original authority and generation. This intent
neither grants authority nor reserves a successful terminal outcome. Generic
token cancellation is only a wakeup: it cannot distinguish manual cancellation,
revocation, deadline and retirement. Session destruction and generation retirement
must record their actual cause before waking work, including admitted operations
that have not published a stream yet. A stream produced after cancellation stays
privately owned for cleanup and is not published for new pulls.

Cause selection must observe current persisted epoch, deadline, factory and
generation under one coherent authority/lifecycle operation. A cached epoch read
followed by an unrelated atomic update is insufficient. Existing bounded native
cleanup records are sufficient; no new durable intent table is required. Hold the
applicable lifecycle/generation guards and a logically read-only SQLite IMMEDIATE
transaction through the actual status check and one-time native cause selection,
using a consistent lock order and bounded acquisition. Release all guards and the
transaction before token wakeup, callbacks or physical cleanup waits. If this
ordering cannot be established, retain uncertainty. A process death may lose the
native cause record and recover as unknown.

An earlier established terminal winner stays immutable. Manual intent alone does
not preempt a deadline that wins before physical cleanup and terminal settlement.
Do not infer successful cleanup from a second close returning after an earlier
unknown result: repeated close observes the winning cleanup outcome. Unconfirmed
cleanup within the bounded public wait remains `Failed / cleanup-unknown`, with
actual unfinished work and capacity still retained.

Both supported payloadless source-cancel replies use the actual empty WIT result
tuple. Recognizing that reply does not prove native resources have ended. Keep
conservative Store disposal until trustworthy per-stream resource accounting
exists. Store disposal proves VM disposal; separately registered Flow work needs
its own physical-completion evidence.

`RunStream.close` must request cancellation and await the same actual RunHandle
completion. Its completion receipt belongs after interpreter termination and
release or verified transfer of execution custody. Setting `ended` or abandoning
a close await cannot skip the receipt or discard its only receiver. The bounded
outer cleanup wait may expire while the existing cleanup worker retains the real
close future, receiver, task/control state and occupied capacity. Terminal delivery
alone does not certify execution completion.

Full parent-component cleanup must coherently close child-registration admission
for the exact execution/generation and capture its registered children. Register
custody before starting work; cancel without holding registry/lifecycle locks;
dispose of the Store as needed; then await actual child and Store completion
receipts. Avoid dependencies on the parent's terminal delivery or reaper, exclude
the parent record itself, and retain evidence for completed children. A global
Shared handle scan or empty Store ResourceTable is insufficient. Until this scoped
census is implemented, VM-only evidence with retained child work requires cleanup
uncertainty, not a successful physical `Cancelled` claim.

## 2026-10-15 Astra high Q19: actual Pack error contracts

The global nominal ShapeRef proof remains necessary. It is insufficient when a
Pack's independently verified WIT dependency uses a structural lookalike under
the same reference. Freeze evidence for each actual verified Pack/hash, interface,
operation and import/export direction. A generic guest call needs its actual
caller's import and provider's export evidence; trusted Core User/Flow/UI/Native
entries use their real canonical caller contract. Never guess a caller contract
from a principal's Pack name. Bound the source documents, parse/resolve work,
provider vectors, total edges and retained evidence before construction. Missing
or conflicting evidence disables error projection without inventing a new Pack
load rejection. No per-call WIT parsing is permitted. This affects typed error
interpretation, not authority or whether the admitted provider can fail.

## 2026-10-15 Astra high Q22: pre-admission User rejection audit

An authenticated User request can fail frozen target resolution before argument
allocation, grant creation or provider activation, for example when the provider's
hash-bound execution limit is absent and therefore zero. At the received User
entry, wrap the existing pure target resolver with the existing asynchronous
denial audit. Preserve the original User, request, Profile/revision/epoch and the
closed `Unavailable` result. The existing bounded `port-call` denial payload is
sufficient; add no arbitrary target strings or argument data. If audit persistence
fails, preserve the existing `AuditFailure` carrying both errors. Successful
resolution continues without a success event or duplicate later denial. Keep
internal resolver rechecks pure and introduce no retry, terminal outbox or replay.

Acceptance requires that original User denial and zero provider initialization,
dispatch and host reads, with no fabricated provider principal, grant or Flow
outcome. Verify the real audit-sink failure behavior as well.

## 2026-10-15 Astra high Q23: bounded incremental stream collection

An output byte limit is not an array element capacity. Unknown-length stream
collection may pre-admit a small reference buffer under the actual Flow owner,
then fund bounded physical growth before allocating. Keep element count and
encoded-byte bounds separate, use checked size/capacity arithmetic and actual
capacity validation, and preserve conservative old-plus-new allocation credit.
Retain each child graph's real producing owner. Include array framing and
separators in cumulative encoded bytes before accepting an item; final encoding
cannot introduce an unaccounted duplicate buffer.

Initial admission failure must precede provider entry. Every failure after
entry, including reference growth, child adoption, count or encoded-byte limits,
uses the actual awaited source cleanup path. Keep the received item/batch funded
through that transition and failure cleanup. An outer timeout retains the real
cleanup task, source and occupied custody; it permits no replay or Drop-only
success claim. Fixed-capacity builders remain valid. Raising Profile budgets or
changing Pack pins is unnecessary to fix the bytes-versus-elements error.


## Astra HIGH Q24: object-safe cleanup snapshot exposure (2026-10-15)

The existing synchronous, sealed-permit `GateEngine::with_cleanup_snapshot` may be exposed through `Gate` with an object-safe signature and default `Unavailable`. `GateEngine` explicitly delegates to its tested inherent implementation; it does not duplicate validation or recursively call the trait method. This is trusted Core cleanup observation, with no guest export, fresh authorization or authority revival. Callers still hold the prescribed Core lifecycle, Wasm generation and independent cause-selection guards. The borrowed snapshot callback runs at most once while coherent and may not lock, reenter, notify or await. All guards are released before signalling. Errors before callback do not establish Manual. Errors after callback can leave a selected cause; they neither undo selection nor authorize reselection, and are not physical cleanup success evidence.

## Astra HIGH Q25: dynamic Flow consumes resolution (2026-10-15)

`ports.consumes` remains the component's structural declaration for dynamic Flow calls. Verified `ShapeKind::Port` retains ordinary Port-binding validation; verified `ShapeKind::Flow` resolves against selected, verified Flow implementations instead of requiring a Port provider. A required shape-based dynamic consume requires exactly one matching target; an optional consume allows zero or one, and multiple matches are a configuration conflict. NativeInput's exact Flow key remains independent and may share an implements view. Resolution grants no permissions: original declaration, Profile ceiling, explicit Pack limits, actual provider frame and a fresh FlowRun Gate still apply.

The current execution factory uses exact `ShapeRef` equality for dynamic Flow target selection. Compiler checks must use that same frozen Profile revision and exact rule. The `(ShapeName, major)` map is only an index; another minor version cannot satisfy an exact requested view. No optional-flag workaround, Port fallback or implicit version widening is permitted. An absent optional target remains unavailable at execution.


## Astra HIGH Q26: FS descriptor protection and registered-root picker (2026-10-15)

A trusted Rust connection API may clone DataRoot's original pinned directory descriptor with `try_clone`; it must never reopen its diagnostic path. Failure or unsupported platforms make the FS backend unavailable instead of continuing without protection. The returned File is an actual capability for Core protection plumbing and must not become a guest resource or selectable root. Protected-root registration is distinct from usable-root registration, which must reject forbidden protected identities, descendants and aliases. Existing workspace-ancestor policy and each operation's protected-descendant restrictions remain applicable.

Usable-root registration fixes a non-reused opaque ID, real descriptor identity and rights ceiling. It grants no authority. Access still requires that exact ID under the approved Profile declaration, ceiling and Pack limits. HostIo supplies an untrusted candidate ID: Core validates size, format, registration, validity and effective rights, then issues a single-use token bound to the actual request/revision/epoch. Host approval claims are ignored. Actual open/read perform the existing Gate and exact operation checks again. This slice supports previously registered roots; arbitrary new-directory native registration and approval UI remain unimplemented acceptance work.


## Astra HIGH Q27: independent physical native-work census (2026-10-15)

Physical completion must come from joining the actual task containing the work, independently of terminal/audit/reaper acknowledgement. A funded nonclone admission is registered before task start; reserved and unpublished slots remain part of the exact execution/generation census. Admission-to-Join ownership transfer has no cancellation/panic gap, and sealing prohibits new starts. A wrapper that detaches native/blocking work or merely sends a result cannot prove completion. Separate work is registered too, or its enclosing task awaits its actual termination.

The independent monitor and its control/task capacity are admitted in advance. Slot metadata funding does not replace actual FD/buffer/Flow-body ownership. A census succeeds only when sealed and every slot has a real completion; unknown slots cannot be removed or refunded to manufacture emptiness. Receiver loss, panic or Join errors retain unknown for unconfirmed work. Abandoned waits retain the same monitor, Join owner, receiver and custody, with no replay. Close hooks avoid strong parent cycles and run after synchronous guards are released. Current Flow RunResult custody drops alone do not substitute for the adopted actual-Join proof; until the real engine Join seam is wired, VM-only closure stays unknown.


## Astra HIGH Q28: census start and delayed first poll (2026-10-15)

Slot registration and task publication under the same census mutex define the start linearization point. A task published before seal may first poll afterward; it is existing admitted work, not new admission. The census alone does not require another pre-poll cancellation check. Unpolled work remains in cleanup and retains its payload, budget and actual Join owner. Close notification is remembered so an early signal is not lost; existing cancellation, deadline and fresh Gate rules still apply. Delayed execution can produce bounded-wait unknown without losing custody or replaying work. Registration, publication and monitor transfer have no ownership gap. Synchronous body execution, reentrant callbacks and payload destruction never occur under the mutex; signalling, awaiting and failure payload disposal happen outside all synchronous guards. Physical work and its monitor do not await parent terminal/reaper completion.


## Astra HIGH Q29: never-started payload completion (2026-10-15)

A synchronously proven unpublished body is distinct from using Drop to assert that a running task stopped. NeverStarted may count toward physical cleanup only if the captured payload owns unstarted work and synchronously releasable resources, destruction completes normally outside every guard, and slot/control ownership remains until that confirmation. Ordinary unused FD/buffer RAII release can satisfy this closed trusted-adapter contract. Already-running I/O, transactions, other tasks or asynchronous cleanup responsibilities require an additional typed completion receipt or remain unknown. A generic future cannot be inspected for these properties, so unknown payloads must not receive unconditional NeverStarted success. Payload destruction panic remains unknown.


## Astra HIGH Q30: classified final metadata reconciliation (2026-10-15)

This approved design answer specifies implementation work. It is not a passing
admission-health, census-integration or physical-cleanup receipt.

After admission stops, the actual supervisor Join must succeed before final
metadata reconciliation may resolve a transient admission-health failure. Under
the same Profile, epoch and existing leases, run `cancel_due_leases`, then
`recover_profile`; capture the resulting durable watermark and flush through it,
including records generated by cancellation and recovery. Confirm that no
records remain outstanding, `admission_status` is healthy and no integrity
failure is present. A failed final reconciliation remains a failure.

Clear only a failure whose typed origin and classification identify a transient
delivery or persistence problem that these observations actually resolved.
Compare the health generation before clearing so reconciliation cannot erase a
newer failure. Diagnostic-message matching does not supply a failure class;
the implementation needs explicit typed origin/classification before it can
make this distinction.

Do not clear integrity failures, supervisor panic or lost Join, unknown failure
classification, actual epoch revocation or cancellation, missing rights, or
unknown native physical completion. Healthy metadata does not prove that native
resources or asynchronous cleanup ended.

Metadata flush may repeat delivery of the same immutable durable winner only.
It cannot reexecute an effect, reissue a grant, overwrite the winner, revive
authority or manufacture a physical-cleanup claim. Existing unfinished-work
custody and capacity remain until the required actual completion is confirmed.


## Astra HIGH Q31: one-use mailbox and actual work completion (2026-10-15)

This approved design answer does not establish mailbox implementation or
integrated interpreter/FS completion acceptance.

A pre-admitted one-use mailbox task must directly await the actual interpreter
or FS future and retain its real Join. Bind the exact generation, activation and
request. Order submission and seal under one guard, with indivisible transfer of
payload ownership. Sealing must wake a receiver that has not received a payload.
Once submitted, the payload remains owned through actual cleanup. If constructing
a future already starts external work, that work needs its own retained custody.

Channel closure or normal mailbox-task exit alone never proves NeverStarted or
physical completion. RequiresCompletion remains unknown unless the actual work
future completed or Q29's trusted never-published payload contract confirms
synchronous destruction outside all guards. Keep the opaque per-work completion
on the same WorkRecord with its retained receiver. Do not create an own-self Join
or a cycle that waits for sealing its own generation.


## Astra HIGH Q32–Q36: terminal cache, physical results and cleanup tree (2026-10-15)

These are approved design answers. Focused census, mailbox and cache executions
do not establish complete structured child/cleanup integration; that work and
its typed physical result remain under implementation.

Q32 permits a prefunded empty-result cache for Manual-close compatibility.
Fund and retain the cache under the actual live owner before closing; a later
empty pull reuses that storage. This creates no new budget for a retired owner,
revoked or expired execution, and no global replacement budget. It neither
revives authority nor proves final Session cleanup. Cache behavior can improve
while the enclosing test still fails at Session close.

Q33 permits public `Cancelled` after actual physical-completion proof when the
selected Cause is `Revoked`. Keep that Cause as `Revoked`; do not relabel it
Manual. Without the required physical proof, retain cleanup uncertainty. This
result does not restore revoked authority or replace an immutable winner.

Q34 requires structured custody for actual child work and its cleanup, with a
typed physical result. A wrapper result or mailbox exit alone does not supply
that result. Retain the actual work, cleanup responsibility and completion
observation until their required ending is confirmed.

Q35 narrows Q30 reconciliation to a proven actual sink-origin `Unavailable`.
After the actual Join, reconcile the same Gate, outbox and durable watermark.
`JoinUnknown` remains latched. This does not change the general failure classes
or authorize clearing arbitrary `Unavailable`, diagnostic-string matches,
integrity failures or unknown physical completion. Metadata retry still delivers
only the same durable winner, without effect replay, grant reissue or overwrite.

Q36 makes the cleanup sibling/body tree immutable. Pre-register both the actual
body and its cleanup sibling before the parent settles. Do not create self or
cross-generation relationships, reparent work, or wait on ancestors. The typed
result must retain the real child and cleanup custody through this structure.


## Astra HIGH Q37–Q40: durable handoff, cleanup owner and source admission (2026-10-15)

These approved design answers are distinct from source-bound implementation
evidence. UI preadmission has actual Core 157 acceptance. Source-operation173
has actual FS 180 and Wasm 181 acceptance within those focused targets; earlier
174/176 stops remain historical, and broader runtime replays are pending.

Q37 permits durable handoff through the existing Gate's exact pending ACK while
preserving the business error. Use that existing durable identity; do not create
a replacement grant, replay an effect or overwrite the immutable winner. Durable
delivery and actual physical completion remain separate observations.

Q38 retains cleanup under an independent bounded Core owner or quarantine,
without an indefinitely running keepalive task or a self-cycle. The Core cleanup
registry has a process-funded 256-slot count. This is distinct from the UI's
64/1024 capacity limits; registry custody cannot manufacture extra UI capacity
or successful cleanup.

Q39 requires a SourceOperationScope to be minted at actual provider entry before
guest code runs, then bound to the later SourceOpen and pulls. Physical proof
must include independent actual Store drop and the source's scoped children.
Whole-generation evidence substitutes only when teardown is proven source-local.
The historical Q34 ComponentStream has no physical companion. New Q39 source
preflight 173 introduces an own-VM/native-operation subtree companion, with
full-generation completion kept separate and legacy physical completion unknown.
Guards 174/176 executed zero tests, then actual FS 180 passed all eight guest
cases and Wasm 181 passed all 75 library cases, including five new actual StoreDrop
and own-subtree cases. These test the source-local contract within their recorded
scope; a successful close or disposed Store alone still does not supply it.
Provider 183 later passes 30 on source 173. Newer source 192 has Core 193 at
91 PASS / 1 FAIL; later Wasm/adapter and post-Q39 real 1B replays remain pending.

Q40 admits UI pending plus live streams before execution using the verified
provided view. Keep the metadata charge through both pending and live custody.
A scalar call must remain usable when stream capacity is full. Missing
provided-view evidence must fail before effects. Actual Devin 142 patch 3 was
applied with Root context/format repair in 156; Core 157 passed all 82 cases,
including UI 64 capacity/close and scalar-at-full. Earlier unapplied patch 2 is
historical provenance. This Core result is not native App/WebView acceptance
and does not accept the later Q39 source.


## Astra HIGH Q48: Proc/PTY launch commitment (design only)

This answer is approved design, not implementation or native execution proof.
Extending the existing `Proc`/`Pty` EffectRequest variants is not mandatory. The
common `argsDigest` may durably bind a versioned, domain-separated KEYED
commitment covering full actual executable identity/digest, ordered argv/env,
FD-based cwd identity, PTY parameters, limits and a non-secret summary.

A native-only `ProcessLaunchBinding` must bind commitment method/key version and
commitment to the trusted catalog display, argv/env counts, trusted cwd label,
PTY/limits and explicit unsandboxed Dangerous presentation. It is the approval
presentation source; arbitrary guest strings are not that trusted display.
Bare-program-only executable-grant compatibility must reject before approval.
The current Proc/PTY backend remains unavailable/unwired.

Core owns the key, executable catalog, pinned cwd and prepared snapshot. Gate
checks binding/commitment format, permissions and approved frame. The backend
compares the same prepared snapshot before consuming the permit and launches
through the retained actual FD identity. Do not substitute an unrelated path
lookup after approval. A public Rust key constructor inside the trusted Rust
Core boundary is not, by itself, grounds for another Gate factory-proof rule.
Guest/caller-specified key self-verification fallback is forbidden. Q48 adds no
OS jail, native RSS or same-UID requirement. See [proc-next-slice.md](proc-next-slice.md).

## Historical source-bound execution cutoff through 246 (2026-10-15)

HEAD `a400d47c` is still Naming 16 only, with no new runtime commit. Native Input
216 passes four after fixture 215. Gate+Store 217 has 155 PASS / 1 FAIL at a 20ms nonce
fixture. Deterministic private expiry 219 leads to security 28 PASS in 220, but the
terminal expiry/watermark case still fails: 173 PASS / 1 FAIL total. A read-only real
permit's stored-expiry fixture 222 is followed by all 174 Gate+Store passes 223.
Historical failures remain; expiry-before-consume is not actual consumed expiry.

Current original Python 218 passes 91 unchanged corpus cases in one Rust test on
Q42+Gate211/213. Its 441 source hashes, eight Pack hashes and original component
and corpus are unchanged. Historical public Python 49/1B 69 and earlier 159 proofs
remain intact. Later source still requires replay.

Actual 49KB Devin 205 patch 2 is applied 225 after context-only 224 correction of two
nonexistent old comments. Root retains two pre-Gate checks and fixes a temporary
borrow in a test. Guest 226 passes 11 on the old native pool. Pool 230 compile-fails
231 before tests; existing-dependency/std repair 232 leads to 94 Wasm passes 233,
including 300 sequential retained receipts, monitor panic and multiple observers.
Four fault tests 236 are appended with the production prefix unchanged per Root.
Combined 237 then has 148 executed: Gate 50 PASS and Wasm 95 PASS / 3 FAIL. Two IO tests
expect one Task charge but actual custody is 64KiB Prepared plus 16KiB preflight,
two charges. Preparation239 corrects only those assertions. Census final7904
bytes versus0 comes from a transient funded observer clone per Root diagnosis.
Test-only241 adds observer quiescence; actual242 then passes all136 Wasm tests,
98 library plus38 integration, including the three historical failures and all
four FS consumed-supervisor fault cases. Retain actual physical credit until
completion; test quiescence is not an early production refund or a higher cap.
Historical237 remains failed in its original record.

Core240 passes all125 tests:94 library,one journal and30 provider-proxy. Selected
Flow+Gate+Store245 passes225:Flow51,Gate50,Store124. All five new execution
manifests240/242/243/245/246 contain448 unchanged source hashes. These selected
results do not replace the failed/stopped full-workspace210 command.

Strict Clippy221/229/235 retains its Gate12/Store9/Flow13 failures. Actual243
advances past Flow production and fails28 Wasm production lints. Eight later
Main fix files are frozen but unvalidated. E2E246 executes10 cases,9 PASS/1 FAIL,
with four additional ignored cases. Oversized UI expects a transport failure but
receives `Flow: component: stream cleanup completion unknown`; the failure remains
open. Neither strict Clippy nor full E2E is accepted.

Helper227 passes seven tests with five matching source hashes; Linux workflow
itself is unrun. Native debug238 is an actual normal SWE-2 Max coding job over
three existing files plus new `debug_scope.rs`, intended for startup proof
isolation only. Its diff was received, but exact context mismatch leaves it
unapplied/unverified at this boundary. Root has not run native GUI; normal
DataRoot, SingleInstance, WKWebView, Keychain and existing Profiles are outside
its intended override. Proc/PTY Q48 remains design-only and unimplemented.

Full workspace, native App/WebView, Linux CI and complete migration remain open.
The goal stays active:1 of837 closed,836 TODO. Historical public Python 49/1B 69
are unchanged and later-source replay remains pending. Later Root jobs are
outside cutoff246. No additional review-only job or automation change is part
of this documentation update.

## Q49 binding foundation (approved design only)

The first Proc/PTY slice generates and verifies bounded bindings only;the backend
remains Unavailable. Before actual executable grants,integrate bare-request
rejection before approval and the dedicated Core entry. Borrow secret argv/env
into streaming HMAC without copying them into the binding. Bound counts,total
bytes and fixed fields with checked arithmetic. Encode tags,lengths,order and
optional presence explicitly;commit every displayed summary field. Core derives
labels from trusted information. Zeroize keys and keep keys/snapshot secrets out
of Debug.

A bounded native-identity representation can avoid an FD dependency in this
module. Commit identity method,version and purpose to prevent executable/cwd
substitution. This module does not prove real FD correspondence;later Core must
validate and retain descriptors. A type name containing checked is not proof.
A per-process random key may start the design,with restart-changing key identity
separate from encoding version. Do not reuse or reapprove an old snapshot after
restart. Recover durable digest/terminal/outbox;delivery retry needs no old HMAC
key. A persistent Vault key is not assumed. Actual Devin252 produced an upstream
connection failure/empty diff and is pending continuation,not applied code.

## Q50 physical, business and delivery reports (approved design)

A close Err alone does not prove physical unknown. Keep independent physical
confirmation and typed error origin. The native-only optional report defaults to
None/Unproven. Positive minimum proof binds the exact source,operation and
generation;confirms source teardown,children and actual Join;resolves authoritative
cause and authority;and identifies the same reservation's immutable winner.
Distinguish ACK from the same durable-outbox handoff plus a confirmed successor
owner. Succeeded is not required and the selected winner never changes.

Even physical Ok plus durable state remains Unknown if cause is unselected,Busy
or post-callback uncertainty persists,authority fails,source identity differs,
children/scope/winner correspondence are unconfirmed,a pending owner is absent,
or integrity reconciliation is unknown. An exact confirmed handoff's delivery
error alone does not become physical unknown. Both receipts retain separate
business,physical and delivery evidence. Repeated close keeps classification and
errors. Existing CoreErrorCleanup keeps the original batch-too-large error and
all secondary origins structurally;do not stringify multiple origins or discard
one. Actual270 implementation and271's remaining failure are separate from this
adviser answer.

## Q51 FS mutation disposition and terminal rule (approved design only)

Preserve an existing terminal winner while handling guest/business CommitUnknown
and physical custody independently. Actual backend work owns disposition:Clean
only for rejection before mutation or a proved no-change failure;possible
mutation stays PartialUnknown until confirmed completion proves Committed.
Retain actual disposition through conversion,terminal selection and delivery.
Never reset a possibly changed operation to Clean because confirmation is absent.

If no terminal is selected,propose the existing commitunknown classification
once under atomic deadline/cancel/winner rules. An already selected Cancelled,
Failed or Succeeded is immutable;do not add a separate FS commitunknown proposal.
Retry only the same winner/event. Some(actual) is actual completion evidence;
Some(Clean) requires actual work or proven pre-IO evidence;None supplies no proof,
including lost Join. A mutator None yields business CommitUnknown and physical
CompletionUnknown with real custody retained. Never fill missing completion as
Clean. Later Q51 implementation remains unverified at boundary277.

## Historical finalized execution boundary through 277

Actual Devin238 native isolation applies247 after exact context repair. CoreHost 254
passes47 including nine parser cases and private-root Core construction;selected
foundations+CLI255 passes141 executions. Frontend258,native 265 after borrow264,
and CLI 266 builds pass,without GUI execution. Native 259 timeout,262 manual monitor
stop and263 Clone compile failure remain NOT PASS. Build265 has1488 Launcher
warnings and predates actual five-file legacy269;269 is applied with byte-exact
legacy body but uncompiled/untested. Native265's449-entry guard manifest omits
the Tauri `core_host.rs` wrapper;264 separately records the borrow repair. The
build completes,but full native build-input closure is not established here.

Native seven-Pack244 stage and canonical267 verify all pins with58 unchanged
hashes,no Core activation and no Keychain. Profile-create268 fails exit1 with
stdout ID0,no adopted ID and no GUI launch. Do not infer that no earlier approval
or storage side effect occurred. Private paths,Profile identities,keys,nonce and
raw conversation/approval material are excluded.

Applied270 contains eight typed-report files plus Gate's outside-lock cancellation
file. UI 271 executes2:1 PASS/1 FAIL;the previous Unknown misclassification no
longer masks the original provider unavailable,but expected batch-too-large is
unreached. Snapshot275 fails one exact-selected diagnostic at storage-sql-query /
store-quota and restores exact source. It is not production acceptance. Later
Store exact-size and Q51 mutator work remain unvalidated TODO;caps stay unchanged.

Gate 272 stops during compilation on existing capacity allowance/floor,zero tests.
CLI 274 has22 PASS/1 FAIL actual runs including its fork subtest;Gate never runs.
After276 write-history fixture warmup,277 passes74 actual executions:22 unique
top-level CLI cases,one additional fork subtest and51 Gate cases,including a new
reentrant-waker regression. The production273 independent-TTY fix is separate.
No allowance,floor or original flag/approval-turn assertion is weakened.

Strict Clippy248 fails one Wasm lint;after249,250 advances past Wasm then fails26
Core lints. Six-file Core 257 preparation is not a passing strict replay. Restored
UI diagnostic253 also remains1 PASS/1 FAIL and is not production proof. All earlier
records,including237 failures,zero169,stopped full-workspace210,and historical
public Python 49/1B 69 and earlier159 evidence remain unchanged. Later-source
Python/real1B,full workspace,native GUI/App,strict Clippy and Linux workflow remain
open. Goal active:1 of837 closed,836 TODO. Later Root executions are excluded.

## Q52 verified-Pack CSP response (approved design only)

Bind response-header CSP to the exact canonical verified-Pack hash. Keep the same
sandbox and directives;do not use scheme-wide sources,unsafe-inline or
allow-same-origin to make an opaque frame work. Remove conflicting meta CSP only
in rebuilt/reapproved official Packs;the host must not rewrite Pack HTML. This
is design approval,not implemented code. Actual same-Pack module/CSS load and
cross-Pack,remote,IPC,navigation denial remain unproved. Bridge293 is actual
implementation-prompt preparation only.

## Historical finalized execution boundary through 296

CLI 278 builds after TTY repair. Fresh private Profile 279 succeeds through seven
normal Pack reviews and normal Core secret import succeeds. Only safe receipt
hashes/counts/booleans are public;private IDs,paths,secret names/values,tokens,raw
approval/conversation and screenshots are excluded.

Six-file280 copies actual five-file legacy separation269 plus Store exact wire
sizing. Store 281 passes four typed_sql_ tests. UI282 still has1 PASS/1 FAIL at run
payload/journal encoding;UI285 still has1 PASS/1 FAIL from actual 205 versus
expected 199 envelope bytes. Truthful fixture287 uses999820+205=1000025 above the
unchanged1000000 batch bound. Strict original UI 288 passes both cases,including
batchTooLarge,removed handle,Session close and real Flow persistence. This is
Rust/in-process evidence,not native GUI acceptance.

Caller-extra union284 corrects guard inputs without reclassifying265. Native 286
build succeeds with 788 complete recorded inputs including core_host,legacy and
debug_scope. Library dep-info has six sources/no legacy modules;17 library and13
build-script warnings remain. Actual GUI 289 loads static Harness but fails
script/style self sources under opaque-sandbox CSP. Root own-application Inspector
observation is failing/partial GUI proof;no private screenshot/process identity
is published. Binary/dep-info hashes are safe metadata only.

Actual FS mutators290 are separate from adviser Q51 design. All15 verified-guest
cases pass291. Wasm292 executes100:98 PASS/2 FAIL from fixture-declined FsWrite.
Legitimate exact approval295 precedes296 full Wasm138 PASS (100 library+38
integration),including both committed-append real Gate/Store regressions. Immutable
winner,no replay and actual physical custody remain required. Missing completion
never becomes Clean;these focused scopes do not close every FS migration duty.

Python contracts283 have actual pytest1 FAIL/24 setup errors from stale Signed
parser and dirty packaged-fixture source. Rust guard0/requires=false is not PASS.
Later worker fixes/clean candidate work is not accepted by296. Clippy 294 advances
past earlier26 Core errors but fails one collapsible-if lint;mechanical295 cleanup
has no accepted strict replay.

ProcessBinding 252 retry1 is actual coding without output after a retryable upstream
connection failure,not quota. Bridge293 is preparation with no implementation.
Later candidates/executions are outside296. Historical failures,zero169 and public
Python 49/1B 69/earlier159 proofs stay intact. Goal active:1/837 closed,836 TODO. Full
workspace,native GUI/App,strict Clippy,Linux CI and later-source Python/real1B remain
open.

## Historical finalized execution boundary through 317

Treat actual UTC time as chronology. Preallocated260 executes18:35:14–18:35:58,
after296,and passes one real1B test with37 deltas,persistence/cancellation and four
secret dispatch/outcome pairs. Its475 hashes match. Preallocated261 executes
18:37:09–18:37:59 and passes original91 corpus cases in one Rust test with446
matching hashes and frozen component/corpus identity. Historical49/69/159/209 are
not changed or relabeled;private metadata,key,raw audit/conversation are not read.
Later binding/CSP source still needs replay.

Clean five-file candidate e04a4062 passes normal closure298,Python27 contracts299,
tooling 304: 46 and Ruff/mypy571-file305. All share10,963 identical source hashes;
306 is summary metadata,not another test. This candidate does not equal broader
Snapshot/Main WIP.314 preserves all 51,272 ignored bytes in33 files plus local
commit ref before clean worktree removal,with Main unchanged and no model/key/
native Profile deletion.

Full Core 301 remains132 PASS/1 FAIL out of133;the cancel-winner retry/deadline
failure is unresolved.303 focused1 PASS and temporary308 provider30 PASS with
exact original restoration are separate scopes,not substitute production
acceptance.309 strict Clippy fails one network await-lock lint;312 lexical guard
preparation does not rewrite that receipt.

Actual Devin252 binding generation/verification source is applied/copied310.
Root offline311 metadata adds existing hmac/zeroize direct edges,including Native
Cargo.lock;this is not runtime execution.313 passes11 binding tests.315 selected
Flow+Gate+Store passes243 (53/62/128),but actual Proc/PTY launch stays Unavailable.
Q48/Q49 launch approval,descriptor authority and lifetime duties remain separate
from the pure foundation tests. Adviser answers are not adviser-written code.

Actual Devin293 ten-file Main CSP implementation and independent probe tests are
present,but no Snapshot/native replay is accepted here.316 generates only the
official Harness UI canonical pin;the six live Pack/Profile51 inputs are unchanged,
with no component build or secret read. It is not approval,activation or GUI proof.
The actual 289 opaque-sandbox CSP failure remains. Exact verified-Pack header CSP,
unchanged sandbox/directives,no scheme-wide/unsafe-inline/allow-same-origin,and
no host rewrite remain Q52 requirements. Module/CSS and cross-Pack,remote,IPC,
navigation denial remain unproved.

317 strict Clippy exits101 with two Wasm lib-test errors:mixed inner/outer cfg
attributes and unused RunTaskCompletion. Fixes are pending beyond this boundary.
No full workspace,native GUI/App,strict Clippy or Linux CI PASS. Goal active:
1/837 closed,836 TODO. Every prior failure,diagnostic and zero169 is retained.

## Q53 dedicated Process Gate (approved design only)

Add native-only `Gate::authorize_process_launch(cx, request, binding)`, defaulting
to Unavailable. Validate Proc/Pty kind, program identity and PTY consistency;
derive args identity exclusively from the binding. Reject bare Proc/Pty in the
shared authorization path before reservation, mediator or prompt, using existing
trusted-context denial audit. No EffectRequest or schema change is required.

Committed limits are the exact requested execution configuration. They must fit
every applicable ceiling, including the mediator. Return those exact limits or
deny; never silently clamp. Independent deadline, revocation and actual-capacity
rules remain. Gate retains ordinary context/effective-policy responsibility.
Binding proves committed-data consistency, not exec/cwd FD ownership or current
caller-frame provenance. Future Core/backend retains those duties; this slice
needs no additional public FD-proof token. Physical backends stay Unavailable.

Dangerous must govern actual approval/risk handling, not only a summary label
while behavior remains Elevated. Mediators cannot replace required native/device
consent. Render bounded redacted binding fields; generic display serialization
must not expose raw program. Root chooses only the CLI terminal `effect_preview`
and native-contract `trusted_review` projection helper/test hunks to redact before
serialization/copy. Authoritative EffectRequest, prompt, mediator, attestation and
durable hash remain unchanged, as do exact grant/consume/immutable-terminal rules.
Astra HIGH supplied questions-only answers, without tools/source/implementation.
Actual Devin 322 coding starts19:20:15 on frozen317 context; no applied/tested result
is accepted by344.

## Historical finalized execution boundary through 344

Partial318 copy of three files is rejected without tests. Whole-list prevalidation
rejects a320 typo;320 retry1 coherently copies14 valid files. Guard319 includes
actual frontend/style/native/embedded/caller inputs.321 passes51 CoreHost tests
for actual active Pack bytes/policy and both canonical URI schemes.328 repeats
the original uninstrumented full Core command:133 PASS,provider fixture c078
unchanged. Historical301 intermittence cause remains unknown;later PASS is not a
claimed repair or reclassification.

327 publishes only five files in commit62866f621/PR 1521, exact clean297 tested
b7a48 tree. Scope:Python27 contracts,tooling46,Ruff/mypy571. Clean Git tracked
sources 10,959 differ from actual historical guards 10,963 including4 private helper/
support inputs. Runtime/CSP WIP is excluded;no new327 guard count is invented.

Strict Clippy326 argument-count4 fails;329 borrows UiRoute across3 files with no
wire change.332 bool-assert5 fails;335 equivalent assertions advance to337 Flow
support2 failures.343 equivalent source fixes are not Clippy-replayed here.
330 rejects external fixture keys before Node;helper pins exactly2 external
artifacts separately.336 actual Node23 is22 PASS/1 FAIL missing HTML.338 copies
and guards HTML;342 actual Node23 all PASS.344 frontend build passes at19:28:38 UTC,
ten transformed modules. Source/support/exact fixture inventories match;this is
not actual native WebView execution.289 CSP failure remains.

339 rejects before command with0 tests under unchanged floor5905580032 and1GiB
allowance1073741824;170s/jobs1 rules remain.325/334retry1 remove only20 exact old
test executables,1796403200 allocated bytes,after metadata/lsof/PS/helper/lease
checks. Main Git and newer counterparts remain;authority,source,models,keys,user
data and native build outputs are preserved.340 permits0 library deletions;
ten test candidates remain undeleted. Capacity housekeeping is not migration
completion or speculative future compiler funding.

Actual Devin307 code finishes19:15:28;Root applies two files19:27 (app.js and
verify-source).341 canonicalizes official source only:raw manifest bac8ce differs
from canonical Pack identity2c956. Likewise316 raw Harness manifest4c025 differs
from canonical6d6. No deployment or physical native probe run is accepted.
Goal active:1/837 closed,836 TODO. No post344 result,full workspace,native GUI/App,
strict Clippy or Linux CI PASS. Historical49/69/159/209 stay unchanged.

## Finalized execution boundary through 368

Strict workspace/all-targets Clippy 351 passes 791 unchanged inputs before new
live UI test 360 and Process Gate 365. Native 352 builds in about 44 seconds:
historical binary SHA `09dfcdd302bcb1bf1c46ba36662057caf3b222161accf554ef86571fdc0b4120`,
115,232,608 bytes and 791 inputs. Seventeen library and thirteen build-script
warnings remain. This is build acceptance only. Workspace 359 stops after about
71 seconds compiling at the unchanged capacity floor/1 GiB allowance, with zero
test bodies executed. NOT PASS; no guard relaxation.

Actual native 355 observes CSS/JS/SDK ready after seven normal reviews and normal
secret import. Ordinary Send fails static Flow `load` / `unavailable`, stays
pending save and produces no answer. The 170-second timeout ends with SIGTERM;
normal Stop/Quit and GUI PASS remain unproved. Historical 289 CSP failure remains.
Closed metadata 358 records a completed failed/internal PortCall before the
load/unavailable failure. Source-only 363 shows audit-only stringification erases
typed error provenance. `internal` is not SQL or root-cause proof. No raw DB/audit,
private IDs, paths, nonces, keys, approval or conversation content is published.

Explicitly enabled received-UI live test 361 passes one test in 19.13 seconds
using the same seven-Pack structure, normal secret admission and actual real1B
provider: guest submit reaches emit/save/complete and the persisted tail. Direct
current-thread Session does not reproduce 355 or cover native spawn/multithread/
process lifecycle. The native cause remains unknown. Stage 349 and actual 307
nine-probe Node/native execution stay pending; source plan 364 is not acceptance.

Actual Devin 322 completes at the 19:29 UTC minute, exit 0, returning 54,435 bytes.
Exact application 365 and mechanical rustfmt bind all five files to guards 366/368.
Store process target 366 executes eight tests: 7 PASS / 1 FAIL, watermark expected
1, actual 4. Production compiles, but eight PASS is not claimed. Repair 367 is
running, unapplied and unaccepted here. CoreHost 368 passes all 52, including
native review redaction. Proc/PTY backend stays Unavailable. Binding consistency
and redacted display do not establish executable/cwd FD authority or physical
launch. Q54 is Core-owned FD registration, bounded hashing/counters and truthful
CoreControl memory/Registration design only; implementation is not dispatched.

Published 62866/PR 1521 remains the exact clean five-file tree. Runtime/CSP/Process
Gate WIP is uncommitted. All 235 historical validation objects and 15 focused FS
records are unchanged; seven actual results append, for 242 checks. Execution
cutoff is 368. Safe extraction 376 only supplements historical 352–355 metadata;
no later execution is included. Goal active: 1/837 closed, 836 TODO. Historical
49/69/159/209 stay unchanged. Full workspace, native GUI/App, Linux CI and later
Python/dedicated real1B replay remain open. Clippy 351 predates source 360/365.

## Finalized execution boundary through 403

This documentation update410 includes stable results through403 only. Later404+
work is excluded. The prior242 validation objects are deeply unchanged; seventeen
actual guarded executions append, for259 records. All15 focused FS records remain
unchanged. Goal active:1/837 closed,836 TODO; completion remains false.

| Evidence | Observed result | Acceptance boundary |
| --- | --- | --- |
| CLI370 and dedicated Gate374 | 3 PASS and62 PASS. | Process review/binding checks; no physical Proc/PTY launch. |
| Node373 | 19 actual Node tests PASS on470 guarded inputs. | Rust guard counter0; not native Rust/WebView CSP proof. |
| Native375 | Build PASS on793 unchanged inputs;17 library and13 build-script warnings remain. | Build acceptance only. |
| Producer377, canonical379 and review381 | Nine canonical Pack pins and nine normal reviews complete. | Setup, not model/physical process acceptance. |
| Actual native382 / safe383 | Fifteen safe observations, ordinary declared describe PortCall, normal Stop/Quit and exit0. | No live1B Send, dangerous-sheet, active-served peer or physical descendant cleanup proof. |
| Test-only repair385 and Store386 |367 terminal/watermark fixture repair applied;8 PASS. | Earlier366 7 PASS/1 FAIL stays historical; no Proc/PTY backend availability. |
| Strict Clippy384 then388 | One-error failure retained, then strict workspace/all-targets PASS. | Zero test bodies; separate Native Tauri package is outside this workspace lint scope. |
| Whole workspace392 |161 executed:160 PASS/1 FAIL at Core UI-close two-second grace. | Cause/repair unproved; full workspace is NOT PASS. |
| Focused396 and workspace-filtered400 | One actual test PASS each. | Neither is a diagnosed fix or full workspace acceptance. |
| SDK unit389 / count395 |29 unittest tests PASS; current59-file unit source/import closure. | Rust-oriented guard0; no compiler execution or fresh runtime91 claim. |
| Non-Core workspace403 |595 executed, all PASS;5 ignored. | `--workspace --exclude tobkiri-core`; not full workspace PASS. |

Native397 advances `load` and `prompt`, then displays the fixed SDK label
`Port call timed out` without an answer or save. Host Stop removes the iframe
but status stays Profile Running. Quit exits0 without a guard stop; this does
not establish normal Session stop or Send PASS. Closed metadata398 records
load step-value82, prompt step-value88 and generate step-started89. Dispatches68
and90 have no recorded outcomes at capture; no emitted/completed/save or failed
Flow terminal is recorded. Expected fixed provider secret get/reveal dispatch
counts are0. HTTP arrival and the underlying cause remain unknown. Metadata is
not physical cleanup evidence. Historical355 failure and its audit-code
provenance limit remain intact. No private IDs, paths, key material, conversation,
returned values, raw statements or diagnostics are published.

Root cleanup391 removes ten exact old generated test executables,853,938,176
allocated bytes, after its lease/process/use/fingerprint checks. Libraries,
source, userdata, models and keys are preserved. Capacity floor5,905,580,032
bytes and1 GiB build allowance stay unchanged; cleanup is not code acceptance.

Roadmap390 has five planned code slices: a bounded guest audit bridge; one real
Harness tool Flow with Pi FS binding; a frozen narrowing mediator bridge;
Core/CLI orphan inspection and exact-shape takeover; and a descriptor-bound Proc
backend with PTY separate. These are plans at this boundary. No legacy row or
inventory unit closes from these results. Proc/PTY physical availability, full
workspace/Core replay, native complex Flow Send/normal Stop, Linux CI and fresh
dedicated compiler/runtime acceptance remain open. Historical49/69/159/209
proofs retain their original scope.

## Current resource and custody boundary through 496

The [validation checkpoint](checkpoint-validation-20261015.json) binds 21 further executions,retaining all 259 historical objects byte-for-byte. Focused FS adds15PASS without changing its 15 earlier records. Execution counts close no inventory unit:1/837 closed, 836 TODO;completion false.

Native preparation owns its Send future and bounded busy retry;21 mailbox/preparation cases pass after the test-only trait import correction. Direct multithread real1B UI guest passes one; Launcher multithread fails one. A temporary Snapshot-only diagnostic observes exact UiOperationCloseControl admission.try_lock WouldBlock,outer pull Core,no in-band Flow failure. It restores exact bytes with fresh mtime and Main unchanged. This proves neither a production repair nor the prior Launcher/native GUI cause.

Native build passes,but actual GUI Send times out without answer/save. Stop removes the iframe while Profile Running remains;normal Quit exits0. HTTP/secret arrival and physical descendants remain unknown/unproved. New frontend Stop code has seven cases within 30 passing Node tests;one test-type error is repaired,typecheck/30-case rerun pass,and Vite regenerates UI. No later native execution covers that new source;GUI Send/truthful Session Stop remain unaccepted.

Core --lib 116 passes before three genuine native-frame tests are added;those three separately pass. Own-contention/recovery,poisoned uncertainty and closed/retired uncertainty do not establish full atomic native-ID/control binding in one scope. Historical cleanup has no diagnosed fix;full coherent workspace remains open. Port-proxy 20 and FS 15 are focused passes only.

Census corrective output is held after multiple correctness findings. Audit output is private virtual source only;owned Phase2 pending,not Main-applied/runtime-proved. ProcessCatalog four-file integration/13 tests create no descriptor-owned executable/cwd or physical Proc/PTY authority;backend stays Unavailable.

Required tooling reports 46 clean-fixture setup errors; lint is not reached. Clean-candidate work is preparation only;refresh its provisional inventory after current edits. No bypass,closure,fast-mode or automation change follows. Preserve floor 5,905,580,032 bytes,1 GiB growth,170 seconds,jobs1,one serial capacity lease,and historical49/69/159/209 scope.

## Current resource and custody boundary through 587

The [checkpoint](checkpoint-validation-20261015.json) now holds 316 receipt-bound records, preserving all 280 earlier objects byte-for-byte. The focused FS receipt remains 16 records. No inventory unit closes: 1/837 closed, 836 TODO; completion false. Later 588+ executions are excluded.

Secret D1 replaces arbitrary error/layout handling with closed error projection and source-derived fixed admission storage, retaining the original consumed native work and blocking Join. Fourteen actual custody tests and strict Wasm Clippy pass. This is partial: outer supervisor Box, dedicated task-cell allocation, returned-error lifetime and final owned-resource ABI handoff remain open. Earlier Store 140 and SecretC 8 passes retain their source boundaries; direct 1B 526/527 predates D1 and later Core changes.

Core Q79 keeps the same allocation funder through new strong/weak handle lifetimes; ten operation-intent tests pass. The settled-metadata classification repair preserves physical cleanup, acknowledgement and slot-removal obligations. It does not establish the missing prepared-context/durable cause-wake/Host bridge or repair Launcher 474.

Core full-suite cleanup remains unresolved: 566 reports 141 PASS/1 FAIL, 567 passes the exact case in isolation, and temporary 575 again reports 141/1. The closed diagnostic observes receipt-wait after VM completion with unfinished original cleanup Join; the exact inner owner is unknown. Refined temporary 585/586 passes 146 without a timeout/probe. Exact bytes are restored with fresh mtime, 638 unrelated Snapshot sources remain unchanged and Main is unchanged. This is diagnostic evidence, not a proved intermittent fix or ordinary current-source full-Core PASS.

Private Harness A1 has 9 native pure passes and two component builds; artifact header/hash observation 583 is metadata only. Actual Core tool Flow/permission/FS runtime and producer integration remain pending; no focused FS evidence is added. Vendor 587 applies one owned-metadata rollback file and includes four owner-denial tests, but runs none at this cutoff. ReturningFromHost is not thereby established as final guest-delivery commit proof.

Historical clean SecretB checks 532–534 pass generator/tooling 46/Ruff/mypy 571 without bypass. Its checkout is safely retired with recovery preserved; these results do not cover later C/CoreA/D1/Q79/vendor changes. Current required checks, coherent workspace, ordinary Core/CoreHost and real 1B replay remain open. Audit/Census corrective code is private, not Main/runtime acceptance. Registered catalog Files are retained; physical Proc/PTY is still Unavailable. No current native GUI Send/save/truthful Stop acceptance exists. Preserve the same 5,905,580,032-byte floor, 1 GiB growth, 170 seconds, jobs1 and serial lease.

## Adopted Audit/Census custody checkpoint 1041

The coherent Audit910/Census930 set is applied to Main1040 and copied to
Snapshot1041 with the same 24 identities (23 source/config files plus lock).
Before adoption, all 169 Core library and 61 CoreHost library tests pass without
ignored or filtered cases. Strict all-targets Core/CoreHost/Wasm/Store/Canon
Clippy also passes, with zero tests. The three real-WAT Audit cases run within
that Core suite; focused historical Store26/Wasm2/Vendor26+11 evidence retains
its earlier source scope rather than becoming extra tests in this run.

Audit read validates the full history before returning its bounded projection.
Append keeps the original typed inputs, permit and funding through its SQL
transaction; all SQL handles are gone before terminal/ABI acknowledgement. The
resource-free Value hook follows whole canonical lowering/scope/reentry/task
deletion and authenticates the original nested Audit outcome. Late delivery
failure preserves the single committed append and no-replay custody. It does
not promise that guest realloc cannot observe intermediate memory writes.

Adoption keeps concrete Guest/Pack/UI task, handle and queue controls, original
body/monitor custody, actual Trailer-backed Waker destruction and weak-owner
storage funding mandatory. Byte-exact opaque Tokio TaskCell/OS/TLS accounting,
complete native RSS and a universal no-abort guarantee for arbitrary trusted
Rust Waker double panic are limitations; their absence alone is not an adoption
gate or a new universal completion guarantee. This scope does not waive the
concrete bounded lifetime obligations.

The historical scalar/UI-capacity tests pass this time; no causal cleanup fix
or permanent failure exclusion is established. Resource C, Host B2, Harness/
default migration, current real1B, native GUI and coherent workspace acceptance
remain pending. The checkpoint has 319 selectively recorded executions with
all 316 old objects preserved; focused FS stays 16, and goal completion stays
false. Historical failures and older resource boundaries retain their scope.
