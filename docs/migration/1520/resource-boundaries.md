# Active resource boundaries for issue #1520

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
