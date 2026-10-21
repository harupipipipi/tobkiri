# Proc/PTY next-slice handoff

## Preauthorization snapshot checkpoint, 2026-10-21

The [current focused replay](../../../crates/tobkiri-core/evidence/process-snapshot-20261021.json)
passes all 21 process-catalog tests. Core now has a private, non-Clone
`PreparedProcessSnapshot`: it owns bounded, zeroizing argv/environment under the
original allocation owner, retained executable/cwd File duplicates, and the
existing keyed binding. Copying and descriptor duplication occur after input
validation and capacity admission; failed preparation releases the new custody.
Tests cover original-owner retention, private verification, foreign-key refusal,
input/budget bounds, real descriptor-duplication failure and unused Drop.

This snapshot can exist before approval so that approval and later dispatch can
refer to the same candidate. Its private verification is not authorization. It
does not consume a Grant, establish an immutable native executable, launch a
process, or enable Proc/PTY. A later move-only authorized handoff and admitted
native physical owner remain necessary. Earlier dated sections below retain
their own execution cutoffs; their missing-snapshot statements are historical.


Latest execution cutoff: **587**. See the [current boundary below](#current-process-and-native-boundary-through-587). Earlier sections retain their historical source and execution boundaries. Current counts: 316 validation records, 16 focused FS records, 1/837 closed and 836 TODO. Later588+ executions are excluded.

Status: **incomplete; no Proc/PTY backend availability is established here**.
The original2026-10-15 read-only handoff is historical. This update incorporates
approved Astra HIGH Q48, which is design only. Q48 supersedes the old assumption
that the EffectRequest enum must be extended and the old FS-Unavailable status.
Proc/PTY implementation and native process availability remain unestablished.

## Existing interfaces and hooks

`crates/tobkiri-wit/wit/core/core.wit` (1.0.0) and
`crates/tobkiri-wit/wit/core-v1-1/core.wit` (1.1.0) have identical Proc/PTY APIs:

```text
proc.spawn(program: string, args: list<string>,
  cwd: option<borrow<fs.dir>>, env: list<tuple<string, string>>)
  -> result<process, error>
process: read-stdout(max: u32), read-stderr(max: u32)
  -> result<option<list<u8>>, error>
process: write-stdin(data: list<u8>) -> result<_, error>
process: close-stdin(); kill(); wait() -> result<s32, error>

pty.spawn(program: string, args: list<string>,
  cwd: option<borrow<fs.dir>>, env: list<tuple<string, string>>,
  cols: u16, rows: u16) -> result<terminal, error>
terminal: read(max: u32) -> result<option<list<u8>>, error>
terminal: write(data: list<u8>), resize(cols: u16, rows: u16)
  -> result<_, error>
terminal: kill(); wait() -> result<s32, error>
```

- Current `effects.rs` lines179/229 link the actual `FsDirectory` as
  `HostDirectory` for fs/proc/pty `dir`;705–706 dispatch FS to
  `filesystem_imports::invoke`. FS is wired and has actual guest evidence226.
  Proc `process` and PTY `terminal` still link unavailable resource types at
  230–231, and 708–710 return backend unavailable for proc/pty. This corrects the
  historical FS-Unavailable statement; it does not close the FS migration.
  Existing resource adapters and `Nominal<T,0/1>` still preserve exact version
  identity; same-version fs.dir imports share the actual Rust type.
- `EffectValues` already provides owned byte/result conversion. A wait result
  can use `codec.ok(Some(Val::S32(code)))`; no signed-value primitive is missing.
  The generic unkeyed value `args_hash` helper is not the Q48 launch commitment.
  The common Gate `argsDigest` slot may carry the new keyed commitment without
  changing the Proc/Pty enum. Bind pinned FD cwd identity, not a table number.
- `crates/tobkiri-gate/src/protocol.rs` has only
  `Proc { program: String }` and `Pty { program: String }`. Permission checks
  require exact `PermissionSet.proc` membership; PTY also requires `pty`.
  Both currently have Elevated risk and per-effect trusted approval.
  `ApprovalPrompt` contains the complete effect; `engine.rs::prompt_for`
  supplies no process-specific redaction.
- Existing authority path: `Gate::authorize` → `Grant::consume` → narrowed
  `CallCx` → `Gate::{narrow_lease, watch_lease, check_context}`. The permit
  exposes `matches_context`, `deadline`, `limits`, and its exact effect/args.
  `record_outcome` and `settle_deadline` preserve immutable terminal decisions;
  deadline settlement does not establish physical cleanup.
- `ResourceLimits` supplies Wasm fuel/memory/table, wall, and output bounds.
  It is not a native process RSS/jail policy.

## Missing boundaries to implement

1. **Exact launch and private snapshot.** There is no Rust Proc executable catalog,
   executable-to-launch binding, native process/PTY backend, or process session
   supervisor. Q16 requires the actual bounded executable/argv/environment/cwd
   snapshot. Bind the complete launch through Q48's versioned/domain-separated
   keyed commitment in common `argsDigest`; extending the existing Proc/Pty
   EffectRequest variants is not mandatory. A native-only `ProcessLaunchBinding`
   supplies commitment method/key version and trusted display/count/cwd/PTY/limit
   evidence under that same commitment, retaining the exact permission/catalog
   ID. Keep arbitrary
   strings private; prompts show trusted catalog identity, counts, bounds, and
   explicitly displayable fields. Represent explicitly Dangerous unsandboxed
   execution accordingly. A checked path followed by an unrelated path-based
   launch does not establish this binding.
2. **Keyed commitments.** Workspace HMAC-SHA256 and the legacy
   `tobkiri_canon::seal` exist, but no versioned, domain-separated process
   commitment API or its Core-private key custody exists.
   `Vault::secret_digest(name)` is an unkeyed name digest, not this primitive.
   Core owns the key/catalog/prepared snapshot. A public Rust key constructor
   within the trusted Core boundary does not itself require extra Gate factory
   proof; guest/caller-key self-verification fallback is forbidden.
   Preserve existing canonical/seal semantics. Q16 adds no public-key signing
   requirement.
3. **Native cwd capability.** The separate filesystem work exposes
   `FsRegistry::{register_root, protect_root, issue_open_token, prepare_open}`
   and `FsDirectory::{root_id, rights, prepare}`. `FsDirectory` retains a
   private pinned `File` and bound scope; it exposes no checked native cwd
   accessor. Coordinate that small Core-private capability boundary with the
   filesystem owner. Do not reconstruct it from a root string, inode tuple,
   table rep, or pathname. A provisioned pinned empty cwd is also missing.
   `tobkiri-store::DataRoot::directory_capability` is private to Store and is
   not a Wasm cwd API.
4. **Physical lifecycle.** Reserve Core task/registration/queue control before
   construction and move actual child/FD/snapshot/input/output/permit custody
   into an owning-runtime supervisor before awaiting guest delivery. Resource
   Drop signals that owner; importer cancellation cannot destroy unfinished
   work. `host.rs::discard_instance` and
   `tobkiri-core/src/tracked_stream.rs` illustrate retained task/receipt and
   immutable-delivery patterns; neither is a reusable native process backend.

## I/O and completion boundary

Use bounded owner-funded stdin and stdout/stderr/PTY storage with finite
per-operation and aggregate output bounds. `CoreBuffer::builder` and
`CoreBufferBuilder::write_chunk` can reserve the complete borrowed region
before synchronous native reads. Retain submitted stdin through completed or
uncertain writes and output through delivery/discard. Service both process
output streams so an unread pipe cannot deadlock wait; enforce wall/output
limits and process-group cleanup even when the guest does not read.

One consumed spawn grant may cover that exact session's bounded methods under
the original epoch/deadline. Success requires exit, settled output, and reap.
An immutable unknown outcome, dropped resource, or returned timeout must not
refund unfinished native work or allow launch/partial-write replay. Keep actual
custody through cleanup and terminal handling; metadata delivery may retry the
same proposal without repeating execution.

## Minimum phased ownership

| Phase | Owner and files | Boundary |
| --- | --- | --- |
| 1 | Root: `crates/tobkiri-gate/src/{protocol,engine}.rs`, necessary Cargo changes | Native ProcessLaunchBinding plus common argsDigest keyed commitment and trusted approval display; enum extension and public WIT changes are not required by Q48. |
| 2 | Filesystem owner: `crates/tobkiri-wasm/src/filesystem.rs` | Checked retained cwd capability; coordinate trusted empty-cwd provisioning with Root. |
| 3 | Proc implementer: new `crates/tobkiri-wasm/src/process.rs` and focused backend tests | Actual launch, bounded I/O, process/PTY session, cancellation, settlement, reap and native custody. |
| 4 | Root/integration owner: `crates/tobkiri-wasm/src/{effects,host,lib}.rs` and focused adapter tests | Trusted configuration, nominal resources, owned ABI conversion and session dispatch. |

Do not modify the filesystem owner's module concurrently. Unsupported native
platform paths remain unavailable. Enforce explicitly requested stronger OS
limits or reject the launch. Q16/R2.5 do not add universal OS jail, native RSS,
or detached-descendant containment prerequisites. True Native typed-port
launch remains incomplete; a Wasm CLI adapter uses Proc. Q48 below is the
current design decision and is not an implementation receipt.

## Q48 approval and launch boundary (design only)

Use a versioned, domain-separated KEYED commitment over the full actual
executable identity/digest, ordered argv and environment, pinned FD cwd identity,
PTY parameters, limits and a non-secret summary. Persist that commitment through
common `argsDigest`. Existing `Proc { program }` and `Pty { program }` need not be
extended solely to store these bindings; their program-only stub is insufficient
for an executable grant. Reject any executable bare-program compatibility entry
before presenting approval. Current backend Unavailable is not this future
preapproval implementation.

The native-only `ProcessLaunchBinding` binds the method/key version and
commitment to trusted catalog display, argv/env counts, trusted cwd label,
PTY/limits and explicit unsandboxed Dangerous presentation. Approval obtains its
display from this binding under the same commitment. The old Elevated program
stub does not establish that future Dangerous unsandboxed presentation. Keep raw
argv/env and arbitrary strings private.

Core owns key/catalog, pinned cwd and prepared snapshot. Gate checks binding and
commitment format, permission and the approved frame. Before consume, the native
backend verifies that exact snapshot against the binding/permit, then performs
actual FD-based launch; a new path lookup is not the approved identity. Retain
all existing authority, cancellation, lifetime and physical-completion custody.
A public Rust key constructor inside trusted Rust Core does not itself require
a separate Gate factory-proof mechanism. A guest/caller may never bring a key
and validate its own chosen commitment as a fallback. Q48 adds no OS jail,
whole-process RSS or same-UID requirement. This entire section is design, not
implemented Proc/PTY behavior.

## Historical source-fact boundary

The documentation check observed the following source identities; later edits
need a fresh check. FS wiring is an implemented fact, while Proc/PTY are still
unavailable. These hashes are source facts, not a native process proof.

| Source | SHA-256 |
| --- | --- |
| `crates/tobkiri-wasm/src/effects.rs` | `10ee71dc6e9347404361ae15df3d0fcc9dcdedce10790c676d840954aae0cde6` |
| `crates/tobkiri-gate/src/protocol.rs` | `d778f1dd3243e0ff486acd7e9381a957be794803a6378a204062882aaeb8060b` |
| `crates/tobkiri-wasm/src/filesystem.rs` | `c1081ac37acd646b05a52398005b06e19c55594b46f079008f2199454e605ad9` |

## Historical execution boundary through 246

Actual Core240 passes125, Wasm242 passes136 and selected Flow+Gate+Store245
passes225. The historical237 failures remain recorded;239 corrects two IO test
assertions to the actual two funded charges, and241 adds test-only census observer
quiescence before242 passes the prior three failures. This is focused FS/runtime
evidence with actual physical custody retained, without early production refund.

Strict Clippy243 advances past Flow then fails28 Wasm lints; eight later Main fix
files are frozen but unvalidated. E2E246 remains9 PASS/1 FAIL out of10 executed,
with oversized UI cleanup completion unknown. Native238's diff was received but
exact context mismatch leaves it unapplied/unverified; no native GUI was run.
Q48 remains a design answer: Proc/PTY are still unwired, and the tests above do
not establish native process availability. Historical public Python 49/1B 69 stay
unchanged. The goal is active,1 of837 closed and836 TODO; no later Root execution
is included in this cutoff.

## Q49 bounded binding foundation (approved design only)

Start with binding generation and verification only;Proc/PTY remain Unavailable.
Before executable grants,integrate bare-request rejection before approval and a
dedicated Core launch entry. Borrow secret argv/env directly into streaming HMAC,
without copying them into the binding. Use checked arithmetic to bound counts,
total bytes and fixed fields. Commit explicit type tags,lengths,order,optional
presence,identity method/version/purpose,and every displayed summary field. Core
creates labels from trusted information. Zeroize keys;key/snapshot secrets must
not appear in Debug.

An FD-independent bounded native identity is sufficient for this foundation. It
does not establish actual executable/cwd descriptor correspondence;later Core
validates and retains descriptors. A checked-looking input type name supplies no
proof. Per-process random keys may start the design,with key identity changing
on restart independently of encoding version. Do not reuse or reapprove an old
snapshot after restart. Durable digest/terminal/outbox recovery and delivery
retry do not need the old HMAC key;a persistent Vault key is not assumed.

Actual Devin ProcessBinding 252 request returns a retryable upstream connection
failure and empty diff;continuation is pending and no implementation is applied.
Q48/Q49 are approved adviser answers,not written code,FD proof or native process
availability. Q50's separate physical/business/delivery report and Q51's FS
mutation disposition rules are recorded in [resource-boundaries.md](resource-boundaries.md)
as approved design;they do not activate Proc/PTY.

## Historical finalized execution boundary through 277

Applied native238/247 has CoreHost 254: 47 PASS,including nine scope-parser cases and
private-root Core construction. Frontend258,native 265 and CLI 266 builds succeed;
no native GUI is run. Native 259/262 are stopped and263 compile-fails before264's
borrow repair. Actual legacy269 is five-file applied,byte-exact preservation of
the legacy body,but uncompiled/untested after265;the older build is not its proof.
Native267 matches seven canonical Pack pins with58 unchanged hashes,no Core or
Keychain. Private Profile268 fails exit1 with zero stdout ID,no adopted ID and
no GUI launch;no private path,identifier,nonce,key or raw approval data is exposed.

Typed-report270 is applied,while focused UI 271 remains1 PASS/1 FAIL:original
provider unavailable prevents expected batch-too-large. Snapshot275 localizes
storage-sql-query/store-quota,then restores exact source;not production proof.
Actual277 CLI+Gate passes74 executions (22 top-level CLI cases,one fork subtest,
Gate 51) after276 fixture warmup;this is not Proc/PTY launch acceptance. Strict
Clippy250 remains26 Core errors after Wasm advances. Later Q51 mutator/Store
exact-size work is unvalidated TODO. Prior failures and public Python 49/1B 69/159
history stay intact. Goal active:1/837 closed,836 TODO;no newer runtime commit,
full workspace,native App/GUI,strict Clippy or Linux CI PASS is claimed.

## Historical finalized execution boundary through 296

Actual ProcessBinding 252 retry1 is coding without output after a retryable upstream
connection error,not quota. No binding implementation or Proc/PTY availability is
established. Q48/Q49 remain design decisions.

Native 286 builds the core-host branch after legacy separation269/source280 with 788
complete recorded inputs. Library dep-info has six sources/no legacy modules;17
library+13 build warnings remain. Prior265 input omission stays historical. Fresh
private Profile 279 and normal Core secret import succeed without public IDs,paths,
secret names/values,tokens or raw approval material. Actual GUI 289 loads static
Harness but fails script/style CSP under opaque sandbox;GUI is not accepted.

Q52 approves exact canonical verified-Pack hash/header CSP with unchanged sandbox
and directives:no scheme-wide source,unsafe-inline or allow-same-origin. Remove
conflicting meta only in rebuilt/reapproved official Packs;no host HTML rewrite.
Same-Pack module/CSS and cross-Pack,remote,IPC,navigation denials remain unproved.
Bridge293 is prompt preparation,not implemented code.

Strict original UI 288 passes2 after999820+205=1000025 exceeds the unchanged1000000
batch bound. FS 291 passes15 and full Wasm 296 passes138 after legitimate exact
FsWrite fixture approval295;both committed-append real Gate/Store regressions pass.
Prior 292: 98 PASS/2 user-declined failures remain. These scopes do not implement
Proc/PTY or prove native process launch. Python contracts283 remain1 FAIL/24 setup
errors and strict Clippy 294 one Core lint;no later candidate/replay is accepted.
Public49/69 and earlier159 proofs stay unchanged. Goal active:1/837 closed,836 TODO;
full workspace,native GUI/App,Linux CI and fresh Python/real1B remain open.

## Historical binding foundation and boundary through 317

Actual Devin252 retry1 returns applied binding generation/verification source;
310 copies its three Gate files. Root311 offline lock metadata adds existing hmac
and zeroize direct dependency edges,including Native Cargo.lock.313 passes all 11
selected ProcessBinding tests. This is the Q49 first-slice foundation,not actual
Proc/PTY launch. Backend stays Unavailable. Actual approval/dedicated-entry/
descriptor custody duties from Q48/Q49 remain;the pure tests do not create a
bare-program executable compatibility grant. Original upstream empty-diff failure
remains historical and was not quota.

Selected state315 passes243 (Flow 53,Gate 62,Store 128). Full Core 301 still has132 PASS/
1 FAIL out of133;focused 303: 1 PASS and restored diagnostic 308: 30 PASS do not replace
it.317 strict Clippy exits101 with two Wasm lib-test errors;no strict PASS.

Actual293 ten-file Main CSP source plus independent probes is applied but not
Snapshot/native replayed.316 updates only official Harness UI canonical pin;not
new approval/activation or GUI acceptance.289 actual native CSP FAIL remains.
Q52's verified-Pack header authority,unchanged sandbox/directives and no host
rewrite remain;module/CSS and cross-Pack,remote,IPC,navigation denials are unproved.
No process-launch proof follows from this UI source change.

Clean candidate e04a4062 alone passes27 Python contracts,46 tooling tests and Ruff/
mypy571 files with 10,963 matching hashes.314 preserves all 51,272 ignored bytes and
local commit ref before worktree removal;Main unchanged. Late260 real1B at18:35
and261 original Python at18:37 execute after296 despite labels. Their passes leave
historical49/69/159/209 unchanged and do not accept later Main binding/CSP source.
No post317 execution is included. Goal active:1/837 closed,836 TODO;full workspace,
native GUI/App,strict Clippy,Linux CI and actual Proc/PTY launch remain open.

## Q53 dedicated authorization slice (approved design only)

Use additive native-only `Gate::authorize_process_launch(cx, request, binding)`,
default Unavailable. Validate Proc/Pty kind, program identity and PTY consistency;
args identity comes only from binding. Shared authorization rejects bare Proc/Pty
before reservation/mediator/prompt with existing trusted-context denial audit.
No EffectRequest/schema change. Exact committed requested limits must fit every
applicable ceiling including mediator: exact limits or deny, no silent clamp.
Deadline, revocation and actual-capacity checks remain independent.

Gate still owns ordinary context/effective policy. Binding establishes data
consistency, not executable/cwd FD ownership or current caller-frame provenance.
Future Core/backend owns those proofs; no extra public FD-proof token is needed
in this slice. Physical backends remain Unavailable. Actual approval/risk must
be Dangerous, not an Elevated path carrying a Dangerous label. Mediators cannot
replace required native/device consent.

Display uses bounded redacted binding fields. Generic display serialization must
not reveal raw program. Root selects only CLI terminal `effect_preview` and
native-contract `trusted_review` helper/test projections to redact before copying/
serialization. Authoritative EffectRequest, prompt, mediator, attestation, durable
hash and exact grant/consume/immutable-terminal semantics stay unchanged. These
are Astra HIGH questions-only answers, not adviser code. Actual Devin 322 coding
starts19:20:15 with frozen317 context; no applied/tested result is accepted by344.

## Historical finalized execution boundary through 344

Binding252/313 remains the generation/verification foundation, not physical launch.
CoreHost 321 passes51 active-Pack/policy cases;original full Core 328 passes133 with
uninstrumented provider c078 unchanged. Historical301 failure cause remains unknown.
Actual Node 342 passes23 and frontend344 builds;neither is native WebView proof.
Actual289 native CSP FAIL remains.307 adds actual source-only probes/app.js and
verify-source;341 canonical pin2c956 differs from raw manifest bac8ce, no deployment
or actual native probe run.

Published327 commit62866f621/PR 1521 has exact tested b7a48 five-file tree only:
Python27/tooling46/Ruff+mypy571. Tracked 10,959 and historical guard 10,963 entries are
separate counts;runtime/CSP WIP excluded. Clippy337 still fails2 Flow test-support
lints after earlier326/332 failures;343 equivalent fixes have no accepted replay.
339 preflight has0 tests and preserves floor/1GiB.325/334 remove only20 exact old
test leaves1796403200B;340 permits0 library deletions and ten test candidates stay
undeleted. No post344 result. Goal active1/837 closed,836 TODO;full workspace,
native GUI/App,strict Clippy,Linux CI and actual Proc/PTY backend remain open.

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

## Current process and native boundary through 496

Actual Devin ProcessCatalog production is applied across four files plus lock updates;13 real tests pass. Canon recovery has 41 full-crate passes/strict Canon Clippy success. These do not establish executable/cwd FD custody or physical Proc/PTY launch;backend stays Unavailable. No inventory unit closes:1/837 closed, 836 TODO;completion false.

Native preparation gains owned Send-future/busy-retry handling and21 passing mailbox/preparation tests after a test-only trait import repair. Direct multithread real1B UI guest passes one; Launcher multithread fails one. A temporary Snapshot-only diagnostic identifies its own UiOperationCloseControl admission.try_lock WouldBlock;outer pull Core,no in-band Flow failure. Exact bytes/fresh mtime are restored and Main unchanged. Prior Launcher/GUI cause remains unproved.

Native build passes,but GUI Send times out without answer/persistence. Stop removes iframe while Profile Running remains;normal Quit exits0. No GUI Send/truthful Session Stop acceptance exists. Actual Devin Stop frontend/seven cases yield30 Node passes;one test-type error is repaired,then typecheck/30 Node cases pass again and Vite builds. No later native replay is included.

FS 15,port-proxy 20 and Core --lib 116 pass on guarded source. Three later genuine native-frame tests pass separately;full atomic native-ID/control binding and a diagnosed cleanup fix remain unproved. Census corrective code is held;guest audit owned Phase2 remains private/pending.

The [validation checkpoint](checkpoint-validation-20261015.json) adds 21 actual records for 280 while preserving all 259 historical objects byte-for-byte. FS preserves 15 old objects/adds one replay. Required tooling fails 46 clean-checkout setup errors; lint is not reached. Clean-candidate work is preparation only;refresh source inventory after current edits. Published HEAD62866f621/earlier49/69/159/209 stay unchanged;runtime WIP is uncommitted. Linux CI,fresh dedicated runtime/compiler replay,coherent workspace,native GUI and physical process acceptance remain open. Floor 5,905,580,032 bytes,1 GiB growth,170 seconds,jobs1 and serial lease remain fixed.

## Current process and native boundary through 587

**Physical Proc/PTY remains Unavailable.** ProcessCatalog genuinely retains the registered executable and cwd Files with recorded identity/digest and the per-Core key. Its 13 tests prove the bounded registration foundation, not immutable execution bytes, prepared launch authority, platform execution or physical cleanup. Preparation/platform work supplies no new availability in this cutoff.

Core operation custody now distinguishes settled retained notification metadata from unfinished delivery and uses Q79 funded strong/weak handles. Ten operation-intent tests pass. The prepared-context/durable cause-wake bridge and Host integration are still pending; neither Launcher 474 nor the actual GUI cause is repaired or replayed here.

Full Core 566 reports 141 PASS/1 FAIL at scalar Session close; 567 passes that exact case alone. Instrumented 575 also reports 141/1 and observes receipt-wait after VM completion with unfinished original cleanup Join. Refined temporary 585/586 passes 146 without reproducing the timeout; exact Host/core-import bytes and fresh mtime are restored, 638 unrelated Snapshot sources remain unchanged and Main is untouched. This does not diagnose an inner receipt owner, prove a fix, or establish ordinary current-source full-Core acceptance.

Secret D1 has 14 actual custody passes/strict Wasm Clippy success; outer Box/task-cell/returned-error funding and final owned-resource ABI handoff remain open. Vendor 587 is applied with four tests included but unexecuted. Private Harness pure tests 9/component build/artifact metadata establish no Core tool runtime; Audit/Census code remains private and unapplied.

The actual direct 1B 526/527 Input/Flow/secret/network/stream/save passes belong to earlier SecretB. Historical clean checks 532–534 likewise apply only to that clean candidate; the checkout is retired with recovery preserved. Later C/CoreA/D1/Q79/vendor work needs a new clean candidate and current runtime replay. Earlier native Send timeout/no save and iframe-removed/Running-label behavior remain unresolved; no new native binary/GUI acceptance is included.

The [validation checkpoint](checkpoint-validation-20261015.json) appends 36 records for 316, preserving the earlier 280 objects exactly. Focused FS remains 16. Later 588+ results are excluded. Published HEAD 62866f621, historical 49/69/159/209 scope and goal 1/837 closed/836 TODO stay unchanged. Physical processes, coherent workspace, current native/runtime/compiler replay and Linux CI remain open under the unchanged capacity floor/growth/wall/jobs1/serial lease.

## Adopted runtime source checkpoint 1041

Main1040 now includes the validated Audit/Census integration; Snapshot1041 has
the same 24 source/configuration/lock identities. Its coherent pre-adoption
validation passes all 169 Core and 61 CoreHost library tests without skips,
plus strict all-targets checks for Core/CoreHost/Wasm/Store/Canon. The historical
scalar/UI-capacity cases pass in that execution, without proving their cause
or a permanent repair.

This advances the shared runtime foundation and original Audit dispatch/ACK
custody. It does not make a process backend available: physical Proc/PTY remains
Unavailable. Host B2, resource C, Harness/default migration, actual current1B,
native GUI Send/persistence/truthful Stop and coherent workspace acceptance
remain pending. Opaque native accounting/arbitrary trusted-Waker double-panic
limits are documented scope limits; concrete bounded custody funding remains
mandatory. Goal completion and inventory closure do not change.
