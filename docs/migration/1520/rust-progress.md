# Issue 1520 progress

Current migration scope is recorded in [acceptance status](acceptance-status.md)
and the [inventory](../../../migration/inventory.json): 840 rows, including all
839 original units and the separated private record helper. One canonical unit
is closed and 839 rows remain TODO. The complete issue, original GUI flow,
1B/provider/API requirement and native Python/VM retirement remain incomplete.
See [the native component boundary](native-harness-path-bridge.md) and
[PR #1521](https://github.com/harupipipipi/tobkiri/pull/1521) for later source and
CI evidence; source presence and provisional targets do not prove parity.

The reports below are historical, receipt-bound snapshots. Their final numbered
execution cutoff is **587**, with 316 validation records, 16 focused FS records
and the then-current 1/837 closed / 836 TODO census. The exclusion of later
588+ executions applies to those receipts, not to today's goal or inventory.
The [587 heading](#current-execution-boundary-through-587) is retained for links.

Historical boundary through368 (the detailed historical boundary continues through587):

The goal remains active. This is a draft checkpoint for review.
The finalized execution boundary is 368. Published HEAD remains the five-file
`62866f621f2b62ca4f19e0e42037fdad5e18e47c` checkpoint in PR 1521; all runtime/CSP/
Process Gate work below remains uncommitted. Goal active: 1/837 closed, 836 TODO.
All 235 earlier validation records are preserved; seven actual guard results
append, for 242 records. The 15 focused FS records remain unchanged.

Strict workspace/all-targets Clippy 351 passes its 791 unchanged inputs before
new live UI test 360 and Process Gate 365. Native build 352 passes in about 44
seconds, with a 115,232,608-byte historical binary (`09dfcdd3...`) and 791 inputs;
17 library and 13 build-script warnings remain. This is build acceptance only.
Whole-workspace 359 stops after about 71 seconds compiling at the unchanged
capacity floor/1 GiB allowance guard, with zero test bodies executed. NOT PASS.

Actual native GUI 355 uses seven normally reviewed Packs and normal secret import.
CSS, JS and SDK-ready are visible, but ordinary Send fails at Flow `load` /
`unavailable`, stays pending save and produces no answer. The 170-second timeout
ends with SIGTERM; no normal Stop/Quit or GUI PASS is claimed. Closed metadata 358
shows a completed failed/internal PortCall before the load/unavailable failure.
Source-only 363 finds audit-only stringification erases the typed error: `internal`
does not identify a SQL cause. No raw DB/audit, private IDs or secret data is public.
Historical native 289 CSP FAIL remains unchanged.

Explicitly enabled live UI regression 361 passes one test in 19.13 seconds using
the same seven-Pack structure, normal secret admission and actual real 1B provider:
received guest submit reaches emit/save/complete and the persisted conversation
tail. This direct current-thread Session path does not reproduce 355. Native
spawn/multithread, process state and lifecycle remain uncovered; the native cause
is unknown. Stage 349 and actual Devin 307 nine-probe Node/native execution remain
pending; preparation 364 is not an executed probe result.

Actual Devin 322 finishes at the 19:29 UTC minute, exit 0, returning 54,435 bytes.
Root applies exactly five allowed files in 365 plus mechanical rustfmt. Their
hashes match both subsequent guards. New Store process-launch target 366 executes
eight tests: 7 PASS / 1 FAIL (watermark expected 1, actual 4), not eight PASS.
Repair 367 is running and unapplied at this boundary. CoreHost 368 passes all 52,
including native review redaction. Physical Proc/PTY remains Unavailable with no
executable/cwd FD proof. Q54 remains adviser design only, not dispatched code.

No execution after 368 is accepted here. Safe extraction 376 only confirms
historical 352–355 metadata. Historical 49/69/159/209 proofs stay unchanged; full
workspace, native GUI/App, Linux CI and later-source Python/dedicated real1B replay
remain open. Clippy 351 does not validate later 360/365 source.

## Historical status at boundary 344

Published Naming checkpoint `a400d47c185877d919a955476b2d3f5114e72bcf` is committed and pushed for
only 16 Naming files. Its runtime base remains historical draft `d8ec5a481`;
ongoing Rust runtime changes are separate source-bound work. Clean Naming 196
passes 8 presentation, 33 generator and 46 tooling tests plus Ruff/mypy for 571
files, with all 10,959 source hashes unchanged.

The finalized execution boundary is 344. CoreHost 321 passes 51 tests, including
actual active-Pack bytes/policy and both canonical URI schemes. The original,
uninstrumented full Core command passes all 133 in 328, with provider fixture
`c0781c9a...` unchanged. The cause of the historical intermittent failure in 301
remains unknown; the later PASS is not a claimed fix or reclassification.

Published PR [1521](https://github.com/harupipipipi/tobkiri/pull/1521) contains only
five files at commit `62866f621f2b62ca4f19e0e42037fdad5e18e47c`, exact tested tree
`b7a48c201729a32689d2a86ad08aa5f0ef3c1b75` from clean candidate 297. Its scope is
27 Python contracts, 46 tooling tests and Ruff/mypy for 571 files. Clean Git has
10,959 tracked sources; historical execution guards have 10,963 entries including
four private helper/support inputs. No new 327 guard count is invented. Runtime
and CSP WIP are excluded from the publication.

Partial copy 318 is rejected after three files with no tests; the helper then
prevalidates the whole list, rejects a 320 typo, and coherently copies 14 files in
320 retry1. Native/frontend guard scope 319 retains actual inputs and caller
extras. Frontend 330 fails before Node on external fixture keys; two exact pins
then enter a separate inventory. Node 336 has 22 PASS / 1 FAIL for missing HTML;
338 copies and guards it. Node 342 passes all 23, and frontend production build
344 passes at 19:28:38 UTC with ten transformed modules. These are library, Node
and build results, not a new native WebView execution; actual 289 FAIL remains.

Strict Clippy 326 fails four argument-count lints; borrowed three-field UiRoute
329 keeps the wire unchanged. Clippy 332 fails five boolean assertion lints;
equivalent assertions 335 advance to two Flow test-support failures in 337.
Equivalent source fixes 343 have no accepted Clippy replay by 344. Capacity 339
rejects before launch with zero tests; the floor and 1 GiB allowance stay fixed.

Root deletes 20 exact old generated test leaves in 325/334 retry1, reclaiming
1,796,403,200 allocated bytes after metadata, lsof/PS/helper-reference and lease
checks. Main Git and newer counterparts are retained; authority, source, models,
keys, user data and native build outputs are preserved. Inventory 340 permits
zero library deletions; ten test candidates remain undeleted.

Actual Devin 307 source finishes at 19:15:28 and two files are applied at 19:27.
Official manifest 341 is source-only: raw manifest SHA `bac8ce...` differs from
canonical Pack pin `2c956...`; no deployment or actual native probe run occurs.
Q53 is approved adviser design only. Actual Devin 322 coding starts at 19:20:15
using frozen 317 doc context, without an applied/tested result here. Goal active:
1/837 closed, 836 TODO. No result after 344, full workspace, native GUI/App, strict
Clippy or Linux CI PASS is accepted. Historical 49/69/159/209 remain unchanged.

## Historical status at boundary 317

The finalized execution boundary is 317. Full Core 301 executes133 tests with
132 PASS /1 FAIL:the cancel-winner retry/deadline case remains unresolved. The
exact-selected303 pass and temporary diagnostic 308: 30 PASS with exact restoration
do not replace that full production result. Selected Flow+Gate+Store315 passes
243 (Flow 53,Gate 62,Store 128);strict Clippy 317 still exits101 with two Wasm lib-test
errors (mixed inner/outer cfg attributes and unused RunTaskCompletion).

Clean candidate e04a4062 contains only five native separation files. Its normal
closure298,Python contracts 299: 27 PASS,tooling 304: 46 PASS,and Ruff/mypy 305: 571-file
PASS share10,963 unchanged source hashes. They do not establish equality with
broader runtime Snapshot or later Main WIP. Cleanup314 preserves all 51,272 ignored
bytes/33 files and the local commit ref before removing that clean worktree;Main
is unchanged.

Preallocated labels260/261 execute late,not before296. Actual real1B260 starts
18:35 UTC and passes one Rust test:37 deltas,persistence,cancellation and four
secret dispatch/outcome pairs,475 source/input hashes unchanged. Python261 starts
18:37 UTC and passes the original91 corpus cases in one Rust test with446 unchanged
hashes and matched frozen component/corpus. Historical public49/69 and earlier159/
209 proofs are unchanged and never relabeled. These replays predate later binding
and Main CSP changes;no secret/private metadata or raw audit/conversation is read.

Actual Devin252 binding-only source is applied/copied310;offline lock preparation
311 adds existing hmac/zeroize direct edges,including Native Cargo.lock. All11
ProcessBinding tests pass313. Proc/PTY actual launch remains Unavailable. Actual
Devin293 ten-file CSP source and independent probes are present on Main but have
no Snapshot/native replay acceptance.316 updates only the official Harness UI
canonical pin,not Pack approval or GUI acceptance. The actual 289 native CSP FAIL
remains;module/CSS and cross-Pack,remote,IPC,navigation denial remain unproved.

Goal active:1/837 closed,836 TODO. Native GUI/App,full workspace,strict Clippy,Linux
CI and later-source Python/real1B remain open. No execution after317 is accepted.

## Historical status at boundary 296

The finalized execution boundary is 296. CLI 278 builds, then fresh private
Profile 279 succeeds through seven normal Pack reviews and normal Core secret
import succeeds. Public metadata excludes private IDs, paths, secret names/values,
tokens, raw approval/conversation and screenshots.

Store 281 passes four focused exact-wire tests. UI 282 still fails journal/run
payload encoding and UI 285 still finds actual 205 versus expected 199 envelope
bytes. Fixture 287 uses999820+205=1000025 above the unchanged1000000-byte batch
bound. Strict original UI 288 passes both cases, including batchTooLarge, removed
handle, Session close and real Flow persistence. Earlier failures stay recorded.

Native 286 builds with 788 complete recorded inputs, including Tauri core_host,
legacy and debug_scope. Caller-extra union284 does not reclassify265's omission.
Core-library dep-info has six sources and excludes legacy modules;17 library and
13 build-script warnings remain. Actual GUI 289 loads static Harness but fails
script/style CSP self sources under the opaque sandbox. GUI executed is not GUI
accepted. Q52 is approved exact verified-Pack hash/header-CSP design;293 is only
implementation-prompt preparation. Module/CSS and cross-Pack,remote,IPC,navigation
denial evidence remain unproved.

FS mutators290 pass all 15 actual verified-guest cases291. Wasm292 has98 PASS/2 FAIL
from fixture-declined FsWrite. Legitimate exact approval295 precedes full Wasm 296:
138 PASS (100 library+38 integration),including both real Gate/Store committed-
append regressions with immutable winner/no replay. Python contracts283 remain
actual pytest1 FAIL/24 setup errors;Rust guard zero is not acceptance. Clippy 294
advances past the earlier26 Core errors but still fails one collapsible-if lint;
mechanical cleanup295 is not a passing strict replay.

Goal active:1/837 closed,836 TODO. Public Python 49/1B 69 and earlier159 proofs remain
unchanged. Full workspace,native GUI/App,strict Clippy,Linux CI and fresh later-
source Python/real1B remain open. ProcessBinding 252 retry1 is actual coding without
output after a retryable upstream connection failure,not quota. No later candidate
or execution beyond296 is accepted here.

## Historical status at boundary 277

The finalized execution boundary is 277. Actual CoreHost 254 passes 47 tests,
including nine new debug-scope parser cases and private-root Core construction.
Foundations+CLI 255 passes 141 executions. CLI+Gate 277 passes 74 actual executions:
22 unique top-level CLI cases, one additional fork subtest and 51 Gate cases,
including the new reentrant-waker regression. All earlier failures, zero-test
selections and diagnostics remain recorded.

Actual Devin 238 native isolation is applied in 247 after exact context repair.
Frontend 258, native core-host 265 and CLI 266 builds succeed. Native 259 times out,
262 is manually stopped for monitor repair, and 263 compile-fails before the
borrow correction 264; those attempts remain NOT PASS. Native 265 has 1,488
Launcher library warnings and no GUI execution. Subsequent five-file legacy
isolation 269 is applied with byte-exact legacy body, but is uncompiled/untested;
build 265 cannot validate that later source. Its 449-entry guard manifest also
omits the Tauri `core_host.rs` wrapper;source preparation264 separately records
the borrow repair. Actual build completion is confirmed,without a claim of
complete native build-input closure.

Root stages seven native-proof Packs in 244. Current CLI 267 matches every frozen
canonical pin with 58 unchanged hashes, without Core activation or Keychain
access. Private Profile creation 268 fails with exit 1, zero stdout ID bytes,
no adopted ID and no GUI launch. CLI 273 repairs independent TTY input flags;
274 still fails its write-history-flag fixture before warmup 276 and passing 277.
Gate 272 stops during compilation on the existing capacity guard with zero tests.

Typed business/physical/delivery code 270 is applied, but UI 271 remains 1 PASS /
1 FAIL. The prior cleanup-unknown misclassification is no longer its observed
failure: original provider unavailable persists and expected batch-too-large is
not reached. Restored Snapshot-only diagnostic 275 localizes storage-sql-query /
store-quota; it is not production acceptance. Later Store exact-size and Q51
mutator work remain unvalidated TODO, with caps and original expectations intact.
Strict Clippy 248 fails one Wasm lint, then 250 advances past Wasm and fails 26
Core lints. Six-file Core preparation 257 has no strict-Clippy replay here.

The goal stays active: 1 of 837 closed, 836 TODO. Historical public Python 49 and
1B 69, and earlier 159 evidence, are unchanged; later-source replay remains
pending. Native GUI/App, full workspace, strict Clippy and Linux workflow remain
unaccepted. Q49/Q50/Q51 are adviser design answers; actual implementation and
execution are recorded separately. ProcessBinding 252 has an upstream connection
failure/empty diff and remains unapplied. No newer runtime commit, legacy-source
deletion or later Root execution is accepted by this boundary.

## Current contract

[resource-boundaries.md](resource-boundaries.md) is authoritative where earlier
Astra answers or prototype plans differ. Core/Profile/load/task/transport control
uses bounded native control accounts. Guest Wasm memory, ABI values, value
graphs and output retain producing-owner admission and actual lifetime custody.
Ordinary Wasmtime async/WASI constructors are allowed within the approved
limits; complete interception of every native allocator is optional hardening.
Reachable effects still require the approved bindings and Gate.

Guest DATA may use native SQLite/rusqlite with Profile/Pack/tier isolation,
current authority, SQL/execution limits, transaction custody and grouped
DB/WAL/journal/SHM/temp quotas. SQLite-in-Wasm is an optional experiment. A
whole-process RSS guarantee or exact filesystem-block accounting is not claimed.

Python authoring may use pinned official componentize-py wheels at development
time. Product hosts remain Rust/in-process and execute verified Wasm without
invoking native Python or a compiler. Exact WIT imports/exports, resource
identity/lifetimes, private-initializer hiding and runtime Gate-controlled entropy
remain required. A custom patched CPython producer and identical Wasm bytes
across compiler builds are optional. R11.4 canonical/seal/digest compatibility
is unchanged.

## Latest measured evidence, 2026-10-15

Private receipts live in the parent workspace's `.work/evidence/`. Each applies
to its recorded source; later peer changes require fresh validation.
The portable [draft validation receipt](checkpoint-validation-20261015.json)
publishes the measured results, runtime source hashes and unchanged Pack inputs
without private credentials or workspace paths.

| Receipt | Observed result | Acceptance limit |
| --- | --- | --- |
| `snapshot-generation3-product-check4-20261015.json` | Offline locked Core, CLI and Launcher core-host check passed; source identity matched before/after. | Compiles these product crates; does not execute CLI or native UI behavior. |
| `snapshot-e2e-xhigh-check-20261015.json` | Offline locked `tobkiri-e2e --tests` check passed; source identity matched before/after. | Test compilation, without test-body execution. |
| `snapshot-wasm-current-lib-structure-lifts3-20261015.json` | 32 Wasm library, six component-structure and two static-lift tests passed. | Predates the later DATA journal and current ABI diagnostic edits; not a complete workspace result. |
| `snapshot-store-full-journal8-review-20261015.json` | 110 of 111 Store tests passed, including the two new move-admission regressions and eight actual process-kill checkpoints. | The real SQLite quota test failed again; the previous 109-pass run is not a fix. Process-kill tests do not prove power-loss recovery. |
| `snapshot-store-quota-bounded-repeats20-20261015.json` | The quota case passed 20 standalone repetitions; the first parallel Store library run reproduced failure with a bounded `open-lease` diagnostic at the initial insert. | This localizes an unresolved concurrency issue; no production fix is claimed. |
| `snapshot-store-quota-lease-repeats25-20261015.json` and `snapshot-store-full-lease26-20261015.json` | After explicit process-bound lease release, 20 standalone quota repetitions, 20 parallel complete Store-library repetitions and all 113 Store tests passed. | This tests the reproduced inherited-FD failure and retained unknown-close/unlock custody. It does not promise hostile same-UID protection or safe use of inherited SQLite connections after fork. |
| `snapshot-store-cleanup-isolated39-20261015.json` | All 121 Store tests passed, including eight new actual cleanup-snapshot binding, epoch/deadline, contention, funding and teardown cases. | The readonly Store/Gate primitive is implemented; full stream cause-selection wiring and child census are unfinished. |
| `snapshot-host-gate-profile-flow-current1-20261015.json` | All 137 Host, Gate, Profile and Flow tests passed. | Applies to the recorded source; not a complete workspace result. |
| `snapshot-canon-all-current3-20261015.json` | All 32 canonical/vector/ownership tests passed, with no ignored tests. | Closes only the mapped global canonical mechanism; other modules remain unresolved. |
| `snapshot-manifest-q19-resolver12-20261015.json` | All 37 Manifest tests passed, including aggregate directory/archive admission, independently bound error contracts and same-package dependency documents. | Applies to this admitted source; not a complete runtime acceptance result. |
| `snapshot-wasm-q19-reached13-20261015.json` | All 51 Wasm library tests passed, including provider-vector bounds, real confined filesystem backend tests and payloadless cancel replies. | Filesystem guest-effect/Core picker integration and full child cleanup remain incomplete. |
| `snapshot-fs-session-fixture76-20261015.json` | All four actual Core filesystem registry/picker/open tests passed, including protected roots, unapproved/ambient candidates, root escape and current epoch. | Focused source-bound registry evidence; full FS migration and later main acceptance remain open. |
| `snapshot-fs-registry-regressions94-20261015.json` | All ten actual registry cases passed: the four existing cases plus six regressions for pinned FD/root replacement, leaf symlink, request, epoch, fresh root identity and protected-root moves. Source identity matched before/after; the capacity floor was preserved. | Preparation receipt 93 is not another execution. This source still precedes actual census wiring; it does not certify newer main. |
| `snapshot-devin-fs-guest90-20261015.json` | Both verified-component/public-Core FS ABI cases passed: registered-root read returned `1384`; without the Pack FS limit the actual picker returned Denied marker `100`. Source identity matched before/after. | Fixture patch 73 and source update 89 are preparation records, not extra executions. This predates later main A2/FS/census integration and does not close the FS migration. |
| `snapshot-census-q29-79-20261015.json` | All 12 child-census module tests passed. | The module was unwired in this snapshot. No production child census, actual engine-Join integration or full physical-cleanup acceptance is established. |
| `joint-fs-census117-20261015.json` | All 12 combined FS cases passed on the joint census/cache source: ten registry and two verified-guest cases. Recorded source identity matched before/after. | Focused FS acceptance after integration 109/112/115; full structured child/cleanup custody and newer-main acceptance remain open. |
| `snapshot-core-lib-q19-reached14-20261015.json` | 57 of 59 Core library tests passed, including actual Native Input Flow dispatch, source projection and mediated context narrowing. The registration callback deadlock no longer occurs. | This earlier snapshot failed the static-denial fixture and UI handle-capacity cleanup case. The later 61-case Core replay is recorded below. |
| `snapshot-core-input-static-runtime-negative23-20261015.json` | The corrected five-boundary negative passed: one real static rejection before runtime creation and four actual source-specific runtime denials with zero dispatch. | The whole Core library and subsequent authority changes need their own replay. |
| `snapshot-core-factory-current41-20261015.json` | 60 of 61 Core tests passed, including the two real control-failure/publication regressions and all original Native Input authority cases. | UI handle-capacity/session-close still reports cleanup completion unknown. No all-Core pass is claimed. |
| `snapshot-port-proxy-migration98-20261015.json` and `snapshot-port-proxy-private99-20261015.json` | Migrated Wasm proxy cases finished 17 PASS / 2 FAIL; Core-private compatibility cases finished 0 PASS / 3 FAIL. | Historical failures retained; neither target passed as a whole. |
| `snapshot-core-session-vm100-20261015.json` | The command exited zero but selected zero tests, with 76 filtered out. | This is not PASS or test acceptance. |
| `snapshot-private-mediator-budget102-20261015.json` and `snapshot-core-session-vm103-20261015.json` | After fixture correction 101, both P20 same-entry mediator-budget cases passed. The actual Session group finished 13 PASS / 1 FAIL. | Session handle-capacity/close remained failed; the two-case fixture result is not all-Core acceptance. |
| `joint-core-lib118-20261015.json` | The joint census/cache Core library finished 73 PASS / 3 FAIL, with recorded source identity matched before/after. | Cancel-winner compatibility, P07 final Session close and UI handle-capacity/session-close still fail. No all-Core pass is claimed. |
| `devin-admission123-20261015.json` and `devin-owned-join129-20261015.json` | Devin's admission tests passed 12 cases, then the owned-Join followup passed 13. | Actual source-bound implementation evidence, including reconciliation and latched unknown-Join behavior; not an all-Core pass. |
| `core-current-guests-devin126-20261015.json` and `core-confirmed-cleanup138-20261015.json` | Core progressed from 78 PASS / 2 FAIL to 80 PASS / 1 FAIL. All three original private compatibility cases pass in 138. | The remaining failure is UI 64-capacity/session-close. Later joint Q34 compilation is not a new Core test-body replay. |
| `snapshot-provider-29-q19-reached16-20261015.json` | 20 of 29 real Core provider tests passed, including actual Native Input, guest Flow entry and same-reference provider contract negatives. | This earlier snapshot had nine failures. The latest 30-case provider replay is recorded below; the old 22 Wasm tests remain intact. |
| `snapshot-provider-current-30-45-20261015.json` | 23 of 30 actual Core provider tests passed, with source identity matched before/after and no capacity-guard stop. | This earlier run had seven failures. Callback reentry passes in the later standalone and 30-case replays below; no all-provider pass is claimed. |
| `snapshot-devin-callback-reentry58-20261015.json` | The actual callback-reentry regression passed. | One focused case; later source changes need replay. |
| `snapshot-wasm-core-typed-proxy60-20261015.json` | Both actual Wasm public-Core typed-proxy cases passed: provider/durable-audit success and denial without effective permission. | This target is not all-Wasm or full provider lifecycle acceptance. |
| `snapshot-provider-devin-callback-api64-20261015.json` | 24 of 30 actual Core provider cases passed, including callback reentry. Source identity matched before/after, with no capacity-guard stop. | This earlier snapshot failed six cases. The later VM-only experimental replay is recorded below; actual census integration remains separate. |
| `snapshot-provider-vm-corrections87-20261015.json` | The VM-only A2 experimental snapshot 83 plus Root's two explicit dereference corrections 86 passed 27 of 30 actual provider cases. Source identity matched before/after; no capacity guard stopped it. | Durable cancellation-winner retry, epoch revocation and expired stream/callback cases still fail. This is pre-census-integration evidence and does not certify newer main. |
| `snapshot-flow-census110-20261015.json`, `snapshot-wasm-census111-20261015.json` and `snapshot-native-mailbox113-20261015.json` | Flow library passed 15 cases, child-census module passed 13, and actual native mailbox passed two. | Integration 109 and assertion update 112 are source-preparation records. The tested Join/mailbox paths do not establish all structured real-child plus cleanup custody required by Q34/Q36. |
| `snapshot-provider-native-census114-20261015.json` | Actual census/mailbox provider replay finished 28 PASS / 2 FAIL. The epoch-revocation case passes after actual physical proof, retaining Cause `Revoked`. | This earlier run failed durable cancel-winner retry and expired stream/callback. Later current-fixture and joint Q34 results are recorded below. |
| `snapshot-devin-closed-stream116-20261015.json` | This earlier P07 cache run passed its empty-pull assertion but finished 0 PASS / 1 FAIL. | Final Session close failed with stale Barrier health (`Gate.Persistence`). Later Devin admission/Join and Core 138 pass the original case; this earlier partial result remains failed. |
| `current-provider-guests-rebuild121-20261015.json` | The actual current consumer and provider rebuilt successfully after the stale consumer was found to lack `open-callback`. | Subsequent guards bind 11 fixture/source inputs and two rebuilt components. The build's own 16-entry source guard is separate; build success is not test-body acceptance. |
| `provider-current-guests-devin125-20261015.json` | The current-fixture provider target finished 29 PASS / 1 FAIL; both previous failures passed. | Plain-thread cancellation failed on this earlier source. The later joint result is separate. |
| `devin-immutable-retry134-20261015.json`, `devin-expired-negative135-20261015.json` and `port-proxy-public136-20261015.json` | Original private P09 passed after Devin 132's deadline-family fix; the real expired negative passed; all original 19 public proxy cases passed. | Original positive and negative cases are preserved; none implies whole-Core or newer-source acceptance. |
| `q34-flow-task131-20261015.json` and `q34-flow-all140-20261015.json` | Isolated Flow snapshot 130 passed 16 task cases; with canonical seam 139, all 23 Flow library cases passed. | Flow-only evidence before the later abandonment hook. This is not joint Core/Host or newest-main validation. |
| `q34-joint-compile144-20261015.json` | Production Q34 manifest 143 compiled Core library test binaries with `--no-run`. | Compile-only. Manifest 143 has 12 exact source entries; Root's minor unused-import update is 145. Source-operation physical custody remains incomplete. |
| `q34-wasm-lib146-20261015.json` | Wasm library finished 69 PASS / 1 FAIL. All six native-mailbox and two Host binding-catalog cases passed; child census finished 12 PASS / 1 FAIL. | This failed run is retained. The later deterministic manual-poll test correction changes no production code; actual replay 149 is recorded separately. |
| `q34-wasm-lib149-20261015.json` | All 70 Wasm library cases passed, including all six native-mailbox and 13 census cases. Source identity matched before/after. | Exact production Q34 source 143, minor import update 145 and test-only correction 148. Independent newer Q39 work on Main and pending UI preadmission remain outside this snapshot. |
| `q34-provider147-20261015.json` | All 30 actual provider cases passed on exact joint 143 plus minor update 145, with the current 11 fixture inputs and two artifacts bound. Native guest/Core Flow, cancellation, plain-thread, deadline, revocation and audit-retry paths passed. | This closes this provider target on its recorded source. SourceOperationScope Q39, UI preadmission Q40, full workspace tests and native App acceptance remain open. |
| `ui-capacity-trace141-20261015.json` | The snapshot-only diagnostic intentionally failed the existing UI capacity case. Its 64 bounded trace records showed Retired, uncertainty false, deadline settlement false, Failed outcome and Store disposed. | Not PASS or a production trace. Core-import source was restored exactly to `86dc77f128fde6217c6ccc442be0d8f9abad96911cb8a7d7614d61e86832d483`; Main and staging were untouched. |
| `snapshot-devin-profile-flow66-20261015.json` | All 28 Profile compiler cases passed, including three actual regressions for exact dynamic-Flow selection, no minor widening and exactly one selected implementation. | Compiler acceptance on this source; runtime/native acceptance remains separately measured. |
| `snapshot-cli-portable-profile-review15-20261015.json` | All eight CLI Profile-input tests passed, including a real separate-process working-directory change and URL/UNC rejection. | Input normalization acceptance only; full CLI command and native acceptance remain open. |
| `snapshot-flow-completion18-20261015.json` | All 32 Flow tests passed, including four actual interpreter completion/custody, abandoned close, failure and channel-loss regressions. | Does not establish the parent component's scoped child census or cancellation-cause ordering. |
| `snapshot-canon-flow-incremental32-20261015.json` | All 70 Canon and Flow tests passed after bounded incremental stream collection replaced the bytes-as-element-capacity preallocation. | Includes new admission, growth, ownership, encoded-overhead and actual awaited-cleanup regressions; not a native UI proof. |
| `snapshot-wasm-warm-fiber-fix4-20261015.json` and `snapshot-provider-float-warm-fix4-20261015.json` | Actual repeated cached-fiber calls and the real tiny float provider passed. | The fix preserves physical stack custody while avoiding new charges to a retired invocation owner; not full ABI acceptance. |
| `python-producer-sol-xhigh-20261015.json` | All 54 Python SDK tests passed, including 29 producer/composition checks; installed RECORD consistency and console launcher selection passed. | No component rebuild or Rust replay in this checkpoint. Archive verification remains honestly false when no archive was supplied. |
| `python-official-shell-policy-build3-20261015.json` | Current pinned official-wheel producer built the 21,475,543-byte shell-policy outer component with exact ABI/WIT checks. | Source/build provenance is not current guest execution acceptance. |
| `snapshot-python-current-parity6-nominal-20261015.json` | The actual 21,475,543-byte current Python component passed all 91 unchanged differential cases through Rust Core. | Classifier/tokenizer scope only; no other Python Pack, full DB-API or Harness acceptance is implied. |
| `snapshot-python-current-q19-factory49-20261015.json` | This earlier Python replay passed the same 91 classifier/tokenizer cases through the actual Rust host: one Rust test passed, with unchanged component/corpus identity and matched source identity. | The published portable receipt stays frozen at these 412 source hashes. Later replay 120 passes census/cache; post-Q34/Q39 replay remains pending. |
| `snapshot-python-after-fs104-20261015.json` | All original 91 cases passed again in one actual Rust test, before actual census integration 109. Recorded source identity matched before/after. | This does not cover later census/cache or upcoming Q34. Eight private absolute source-input keys are published as opaque labels with unchanged hashes; the original receipt hash binds that manifest. |
| `joint-python-census120-20261015.json` | All original 91 cases passed after census/cache integration in one actual Rust test. Recorded source identity matched before/after. | Before production Q34 source 143. Published Python 49 remains historical and unchanged; later source still requires replay. |
| `snapshot-e2e-deterministic-q19-flow21-20261015.json` | All four selected test targets compiled and executed with the actual budgeted HostIo fixture. | This earlier run failed all ten tests: most reached `generate / limit-exceeded`; UI returned provider unavailable. The later incremental-collection and phase-diagnostic runs are recorded below. |
| `snapshot-llama-current-q19-22-20261015.json` | The opt-in real-1B test executed with the declared localhost server and real encrypted Vault import. | This earlier run failed at `generate / limit-exceeded`; the later real-1B acceptance is recorded below. |
| `snapshot-e2e-incremental-collection36-20261015.json` | All eight chat, exact Native Input and real local SSE-provider tests passed, including persistence/restart, partial fanout failure and cancellation. | Both UI tests still fail with provider unavailable. The current real-model replay is separate. |
| `snapshot-llama-current-incremental38-20261015.json` | The actual current Gemma 3 1B test passed: Harness input → Flow → Wasm provider → authenticated localhost llama.cpp, 37 streamed deltas, persistence/next-turn read, cancellation without save, and server stop within three seconds. | This is Core Harness acceptance. Native Launcher/WebView, all provider lifecycle checks, CI and the remaining migration remain open. |
| `snapshot-devin-ui-phase47-20261015.json` | Both actual UI tests ran and failed at `submit` with provider unavailable: zero passed, two failed. The oversized fixture completed both append calls and their success assertions before submit failed. Source identity matched before/after; no capacity guard stopped the run. | This earlier phase diagnostic is retained. The later UI replay below passes the happy path; oversized cleanup and native WebView acceptance remain open. |
| `snapshot-devin-ui-flow-resolution67-20261015.json` | The actual scoped UI stream → Flow → verified persistence test passed. The two-case target finished with one pass and one failure, with source identity matched before/after. | The oversized transport case reports `stream cleanup completion unknown` instead of the expected bounded failure. Complete UI cleanup and native WebView acceptance are not established. |
| `snapshot-ui-vm-corrections88-20261015.json` | On the later VM-only experimental source, the happy scoped UI Flow/persistence case passed again; the target finished with one pass and one failure. | Oversized transport still reports `stream cleanup completion unknown`. Actual census integration and native WebView acceptance remain unproved. |
| `snapshot-native-provider-cli-verify50-20261015.json` and `snapshot-native-ui-cli-verify52-20261015.json` | Actual CLI `pack verify <staged Pack>` passed for both staged native-acceptance Packs and reported their pinned manifest hashes. | `<staged Pack>` replaces the private absolute path in the published command. This verifies Pack admission; native execution and WebView presentation remain unproved. |
| `sdk-current-regenerated-pack-client61-20261015.json` | All 12 JavaScript SDK/regenerated Pack-client tests passed, with recorded source identity matched before/after. | This is separate from the 54 Python SDK/producer tests; it is not native UI acceptance. |
| `frontend-core-host62-20261015.json` | All 21 focused Launcher frontend Core-host contract/isolation tests passed, with recorded source identity matched before/after. | Frontend contract evidence; actual native WebView presentation remains unproved. |
| `snapshot-llama-after-devin-inputs69-20261015.json` | The real 1B Core Harness replay passed with 37 deltas, persisted/next-turn history, cancellation without save and server stop. All 51 declared live Pack inputs were included in the matching execution guards. | The portable current-1B receipt now records this frozen Devin snapshot before A2/FS/census changes. Other Packs, latest main, native WebView and full workspace/CI remain outside this replay. |
| `joint-llama-census119-20261015.json` | Actual real 1B replay passed with 37 deltas, persistence, cancellation and four secret dispatch/outcome pairs. All 470 recorded hashes, including 51 live Pack inputs, matched before/after. | Exact snapshot 109 / Flow 112 / Devin prefunded cache 115, before upcoming Q34. Historical public 1B 69 stays unchanged; no newest-main, native WebView or whole-workspace claim. |
| `snapshot-launcher-host-vm105-20261015.json` | All 37 Launcher Core-host library cases passed on their recorded source. | This was before actual census integration and did not execute Tauri or a native WebView. |
| `snapshot-workspace-compile107-20261015.json` | Locked offline workspace test compilation stopped at the 1 GiB growth allowance. | The free-space floor was not breached. No whole-workspace compile or test pass is established. |
| `workspace-compile-retry127-20261015.json` | Whole locked offline workspace test compilation succeeded with `--no-run`, with recorded source identity matched before/after. | All test binaries compiled; test bodies, native App/Tauri/WebView and CI were not accepted by this command. |
| `committed-tooling-checkpoint42-20261015.json` and `committed-lint-checkpoint43-20261015.json` | On the clean `ec8a0688` draft checkpoint, `just tooling-test` passed 46 tests; `just lint` passed Ruff and mypy for 571 source files. Both recorded source identities matched before/after. | These legacy quality checks do not establish Rust workspace, provider lifecycle, CI or native UI acceptance. |
| `committed-devin-tooling81-20261015.json` and `committed-devin-lint82-20261015.json` | On the clean committed `d8ec5a481` Devin checkpoint, required `just tooling-test` passed 46 tests and `just lint` passed Ruff and mypy for 571 source files. Source identity matched before/after both commands. | Clean committed-checkpoint quality evidence; not acceptance of uncommitted experiments, Rust workspace, CI or native WebView. |
| `clean-validation-worktree-removed84-20261015.json` | The validation checkout was clean at `d8ec5a481` before removal after the required checks. | Checkout cleanup metadata, not another test execution or physical runtime-cleanup receipt. |
| `rustfmt-after-devin-code48-20261015.log` | Root reports that the formatting check passed. The retained log is empty. | The empty log does not independently record the command, exit status or source manifest; it is not behavior acceptance. |
| `q34-public-port-compat151-20261015.json` and `q34-private-port-compat152-20261015.json` | Original public proxy target finished 18 PASS / 1 FAIL at P11 plain-thread cancellation; all three original Core-private cases passed. | Public target remains failed, independently of provider 147's earlier 30 PASS. |
| `q34-flow-full153-20261015.json` | Flow unit tests passed 23; integration hung at the first pending-cancel case and the 170-second wall guard stopped execution. | NOT PASS. Partial unit success does not accept the integration target. |
| `p11-trace154-20261015.json` and trace 160/166/172 attempt receipts | Snapshot-only 154 passed 19 cases; repeated160,166 and 172 diagnostics each eventually reproduced P11. The last trace localized actual `BEGIN IMMEDIATE` / SQLite `DatabaseBusy` before callback. | Diagnostics are not production acceptance. All instrumented snapshot files were restored exactly; Main was not edited by these traces. |
| `ui-preadmission-core-full157-20261015.json` | All 82 Core library tests passed after actual Devin 142 patch 3, Root context/format repair and source 156, including UI 64 capacity/close and scalar-at-full. | Definitive acceptance of this Core target on 156; later FS 162/168 and Q39 source 173 remain outside it. No native App/WebView execution. |
| `q34-flow-fixture-full158-20261015.json` and `q34-flow-full163-20261015.json` | Earlier158 passed 23 unit and 23 integration cases but failed five. After source 162, full163 passed 23 unit plus 28 integration cases, 51 total. | The scheduled-task count honestly includes two actual tasks. Original refund and close assertions remain; the historical failure and timeout remain recorded. |
| `q34-ui-current-llama159-20261015.json` | Actual real 1B passed with 37 deltas, persistence, cancellation and four secret dispatch/outcome pairs. All 470 source/input hashes matched, including 51 live Pack inputs. | Exact Q34/UI 156 snapshot, before Flow 162 and Q39. Historical public 1B receipt 69 stays unchanged; no later-source/native App claim. |
| `fs-readonly-guest164-20261015.json` | Eight verified-guest FS cases finished 6 PASS / 2 FAIL. | On this historical source, the symlink expected code was wrong and the list-count path failed. Correction 168 and preflight 173 later pass in 180. |
| `fs-count-diagnostic169-20261015.json` and `fs-count-diagnostic170-20261015.json` | 169 exited zero with zero tests selected; 170 used the fully qualified exact selector and failed its one actual case at the 5-second probe deadline. | Zero selection is NOT ACCEPTANCE; neither run supplies a passing count-boundary proof. Actual Devin 165 prestart-terminal implementation is still pending. |
| `q39-native-fs-preflight173-20261015.json` | Source preparation records nine files: eight Q39 custody files plus the FS backend preflight file. | Source-only. Own-VM/native-operation subtree completion remains separate from full-generation completion; legacy physical completion remains unknown. |
| `q39-wasm-lib174-20261015.json` and `q39-wasm-lib176-20261015.json` | Both replay attempts stopped at the capacity guard before executing any tests. Target growth was 397,312 bytes and 9,867,264 bytes; 176's minimum free space stayed above the 5.5 GiB floor. | NOT PASS; the 1 GiB free-space variance allowance was not relaxed. No Q39 test acceptance is claimed. |
| `capacity-inventory167-20261015.json` | Private read-only capacity metadata separated old generated candidates from user data, models, runtime state, resource copies and proofs; no deletion was performed by 167. | This is capacity planning only. The identified candidates do not fund the additional 13–17 GB Python compiler requirement or close any migration unit. |
| `capacity-generated-cleanup178-20261015.json` | Root removed 418,037,760 allocated bytes from four strict old generated directories after clean-source, zero-tracked-file and no-open-user checks; source remained clean afterward. | Active target, models, user data, keys, resources and evidence were preserved. Capacity metadata only; no 13–17 GB compiler funding or migration credit. |
| `q39-fs-guest180-20261015.json` | All eight actual FS guest cases passed on coherent Q39/native-preflight 173, retaining the original 5-second count deadline, 1025 entries and healthy following stat. Source identity matched before/after. | Focused current FS guest acceptance; historical 164/170 failures remain recorded. Full Core, Provider, post-Q39 real 1B and complete FS migration remain pending. |
| `q39-wasm-lib181-20261015.json` | All 75 Wasm library cases passed, including all 11 Host native-mailbox and five new actual StoreDrop/own-subtree cases. Source identity matched before/after. | Tested Q39 Wasm scope. Root reports jobs=1; minimum free 6,970,888,192 bytes stayed above 5,905,580,032-byte hard floor with the 1 GiB allowance unchanged. Core 182 is running, not accepted here. |
| `q39-core-full182-20261015.json` | Core finished86 PASS / 2 FAIL out of 88; UI 64 capacity/close and scalar-at-full failed. | Historical failed target on Q39 source 173; source 192/193 is separate. |
| `q39-provider183-20261015.json` | All 30 actual Core provider cases passed. | Coherent Q39 source 173 only; later Gate/Store, FS 189 and fixed-index 192 need their own Provider replay. |
| `harness-display-frontend184-20261015.json` | Actual Node runner passed 36 frontend/display cases. The Rust-oriented guard counter is 0 and `requires_executed_tests=false`. | Positive Node evidence, not a zero-test Rust acceptance or native App proof. |
| `native-busy-store186-20261015.json` and `native-busy-store188-20261015.json` |186 failed Gate compilation with E0308 before tests. After Root conversion 187,188 passed all 41 Store library cases, including typed mutex/SQL-writer Busy and post-callback teardown. | Actual Devin 175 Gate/Store slice is applied; Wasm 175 is not applied. Library41 is not a fresh whole-Store 121 or Wasm/runtime result. |
| `devin-fs-prestart-custody189-20261015.json` and `devin-fs-prestart-guest190-20261015.json` | Actual Devin 165 patch 2 was applied and formatted in 189;190 passed 9 guest cases, the original 8 plus saturated Task-admission denial. |189 is preparation, not another execution. The added case is not precise handoff-fault injection and does not establish the backend consume-gap fix. |
| `harness-base-generator191-20261015.json` | Presentation passed 8; all 33 runtime cases errored during setup because strict packaged source closure differed from the manifest. | No runtime behavior PASS. Source changes alone did not satisfy the closure guard. |
| `q39-fixed-index-q40-lifecycle192-20261015.json` and `q39-fixed-index-core193-20261015.json` |192 prepares11 files;193 finishes91 PASS / 1 FAIL out of 92. Original64-capacity/scalar-at-full, 257 scalar entries and two typed own-WouldBlock/poison tests pass. | The input-authority test newly fails at `register_inputs` with Core control capacity. Pending A2 frame-custody repair is not validated; caps are unchanged. Six legacy Wasm adapter tests remain unreplayed. |
| `harness-base-generator194-20261015.json` | Presentation again passed 8; runtime again had33 setup errors. The source-closure mismatch is fixed; the first failure now requires a clean checkout while Main is dirty. | Distinct cause from191. Clean-checkout replay is pending; the fixture is not weakened and runtime behavior is not accepted. |
| `harness-naming-clean-validation196-20261015.json` and commit197 | Clean Naming candidate passed 8 presentation,33 generator,46 tooling and Ruff/mypy for 571 files; all 10,959 source hashes matched. The tested tree became commit `a400d47c`. | Only16 Naming files committed/pushed on runtime base `d8ec5a481`; previous191/194 setup failures remain historical. This does not accept Rust runtime experiments. |
| `q40-opaque-lease-wasm198-20261015.json` | All 83 Wasm library cases passed. | Recorded opaque-lease/Q40 snapshot before native-Busy200 and Q42 funding207. |
| `native-control-capacity-trace199-20261015.json` | Temporary instrumented snapshot passed 92 Core cases; both modified files were restored to their original hashes. | Diagnostic only, not production approval. Actual uninstrumented204 is separate. |
| `devin-native-busy-wasm200-20261015.json`,201 and 202 | Actual Devin 175 repaired three Wasm files; Root added four corrections.201 passed 89 Wasm cases;202 passed all 19 unchanged original public proxy cases. |200 is application/provenance, not another test run. Earlier rejected patches and failures stay recorded. |
| `native-input-control-isolation203-20261015.json` and 204 | After independent CoreControl fixture isolation 203, regular Core 204 passed all 92 cases. | Original authority assertions and capacities are retained. This production-source run is not temporary trace 199 acceptance. |
| `devin-native-busy-p11-repeats206-20261015.json` | All 32 guarded executions passed the one unchanged original exact-selected P11 case; all 32 individual receipts are retained. | Actual production-source repetitions, not zero selections. A bounded pass does not prove every concurrency schedule. |
| `q42-dynamic-operation-funding207-20261015.json` and 208 | Q42 source 207 is preparation; actual 208 passed all 91 Wasm library cases. | Source-bound acceptance; later Gate/FS/Q46 changes require replay. |
| `q42-devin-native-current-llama209-20261015.json` | Fresh actual real 1B passed with 37 deltas, persistence, cancellation and four secret dispatch/outcome pairs; all 470 source/input hashes matched. | Private metadata stayed 0600 and its contents are not published. Historical 69/159 proofs are unchanged; no latest-Main/native App or new public artifact-file claim. |
| `q42-workspace-full210-20261015.json` | Completed Core 92, Provider 30 and chat 3 groups passed; two Native Input cases failed `flow-ambiguous`, then the 170-second guard stopped the unfinished command with exit -15. Guard count 182. | NOT PASS. Completed-summary and unfinished-target counts are partial, not a whole-workspace result. |
| `gate-owned-consumption212-unit-20261015.json` and 214 |212 compile-failed with E0716 before tests. After test-only guard-lifetime repair 213,214 passed all 50 Gate library cases, including 10 owned-consumption and 9 cleanup-callback cases. | No guard stop/source changes during214. Public/production Gate/Store/Core integration remains pending.215 is later Native Input fixture preparation only. |
| `native-input-fanout216-20261015.json` | All four actual Native Input cases passed after fixture-only 215. | Historical workspace210 failures remain; not full workspace/native App acceptance. |
| `gate-store-consumption217-20261015.json` and 220 |217 finished155 PASS / 1 FAIL from a20ms nonce duplicate/expiry fixture. Deterministic private fixture 219 led to security 28 PASS in 220, but terminal expiry/watermark still failed:173 PASS / 1 FAIL total. | Failures retained; consumed expiry and expiry-before-consume are different states. |
| `q42-owned-consumption-python218-20261015.json` | Current original 91 corpus cases passed in one Rust test on Q42+Gate 211/213; corpus/component/eight Pack hashes unchanged,441 source hashes identical. | No private Pack paths/IDs published; public Python 49 stays historical. |
| `gate-store-consumption223-20261015.json` | After read-only real-permit stored-expiry fixture 222, all 174 Gate+Store tests passed. |219/222 are fixture preparation, not extra executions. Full workspace/Core remains separate. |
| `devin-fs-owned-consumption225-source-20261015.json` and 226 | Actual 49 KB Devin 205 patch2 applied with context-only 224 correction, two Root pre-Gate checks and test-borrow fix;226 passed 11 guest FS cases. | Old native pool. Later pool/fault failures237 are not accepted by this pass. |
| `issue1520-ci227-helper-20261015.json` | All seven helper tests passed with five source hashes identical. | Linux workflow itself was not executed. Rust guard0/requires=false does not erase actual helper tests. |
| `rust-clippy221-current-20261015.json`,229 and 235 | Strict Clippy failed Gate 12, then Store9, then Flow 13 production lints. Gate production passed its stage after 228; Store production passed after 234. | Historical failures retained. Source 239 is preparation; actual243 later advances past Flow and fails at Wasm28. No whole strict-Clippy pass. |
| `native-task-pool231-wasm-20261015.json` and 233 |231 compile-failed with undeclared `futures`/inference errors and zero tests. Existing-dependency/std correction232 led to233:94 PASS, including 300 retained sequential receipts,monitor panic and multiple observers. | Focused source-bound success; latest237 failure remains visible. |
| `fs-owned-native-pool237-libs-20261015.json` |148 executed:Gate 50 PASS, Wasm 95 PASS / 3 FAIL. Two IO cases expected Tasks1 but actual funding has Prepared64 KiB plus preflight16 KiB/two charges; census observed 7904 final bytes instead of 0. | Historical failure remains unchanged. After two IO assertion corrections239 and test-only census observer quiescence241, all three cases pass in242. No early production refund. |
| `devin-native-core-isolation238-20261015.inputs.json` | Actual normal SWE-2 Max implementation diff received: startup-only native debug isolation, three existing files plus new `debug_scope.rs`. | Exact context mismatch leaves it unapplied/unvalidated at cutoff246. Normal DataRoot/SingleInstance/WKWebView/Keychain/existing Profiles are outside its intended override. Native GUI unrun. |
| `core-owned-native240-all-20261015.json` | All125 Core tests pass:94 library,one journal,30 provider-proxy. |448 source hashes match before/after; no guard stop. Source after239, not native GUI or whole-workspace proof. |
| `native-metadata241-test-quiescence-20261015.json` and `wasm-owned-native242-all-20261015.json` |241 is test-only census observer quiescence preparation. Actual242 passes136 Wasm cases:98 library+38 integration, including all three historical237 failures and all four FS consumed-supervisor fault cases. |239 corrected the two IO assertions to actual two funded charges;241 waits for test quiescence. Source matches before/after, with no early physical refund or cap increase. |
| `rust-clippy243-flow-fixed-20261015.json` | Flow production passes its stage, then strict workspace/all-targets Clippy fails with28 Wasm production errors. |Zero runtime tests, requires-executed=false. This is a lint failure. Eight later Main fix files are frozen but unvalidated. |
| `flow-gate-store245-all-20261015.json` | All225 selected tests pass:Flow51 (23 library+28 integration),Gate50,Store124. |448 source hashes match before/after; no guard stop. Whole-workspace210 stays failed/stopped. |
| `e2e-owned-native246-all-20261015.json` |Ten cases execute:9 PASS /1 FAIL, with four additional ignored cases. Oversized UI expects a transport failure but gets `Flow: component: stream cleanup completion unknown`. |Exit101, no guard stop,448 source hashes unchanged. The remaining UI failure is not accepted; this is not full E2E or workspace PASS. |
| `wasm-clippy-native247-source-20261015.json` and native-isolation application | Actual Devin238 is applied after exact context repair247; the source copy includes eight Wasm lint files and four native-isolation files. | Preparation/application, not another execution. Historical unapplied status at246 remains valid for that earlier boundary. |
| `rust-clippy248-wasm-native-fixed-20261015.json` and250 |248 still fails one Wasm lint. After249 map correction,250 advances past Wasm then fails26 Core production lints. |Zero runtime tests. Six-file Core 257 preparation has no strict-Clippy replay; whole strict Clippy is not PASS. |
| `ui-cleanup253-diagnostic-20261015.json` |Two Snapshot-only UI diagnostic cases execute:1 PASS/1 FAIL. |Two source files restored to exact original hashes; not production acceptance. |
| `native-scope254-corehost-20261015.json` |All47 CoreHost tests pass, including nine new debug-scope parser cases and explicit private-root Core construction. |449 source hashes unchanged. Actual implementation applied247; native GUI remains unrun. |
| `rust-foundations255-current-20261015.json` |All141 selected Canon/Profile/Manifest/WIT/CLI executions pass. |Includes one child-process subtest in completed summaries. Source-bound target evidence, not whole workspace. |
| `native-frontend258-core-build-20261015.json` |Core-host frontend build succeeds with nine transformed modules. |Build only,zero tests; not native GUI acceptance. |
| `native-debug259-build-20261015.json`,262 and263 |259 stops on170-second timeout;262 is manually stopped for allocated-block monitor repair. Both exit-15 with zero tests.263 compile-fails because HostOutputEvent lacks Clone. |All remain NOT PASS. Monitor repair preserves floor,1GiB allowance and170-second limit. |
| `native-debug265-borrow-fixed-build-20261015.json` and `current-cli266-native-prep-build-20261015.json` |After264 borrow repair,native core-host build265 succeeds with1488 Launcher library warnings;current CLI 266 builds. |Build acceptance only. No GUI;later legacy269 is not compiled or validated by265. |
| `staging.receipt.json`, `canonical-verification267.json` and `profile-create268-failure.json` |Root stages seven Packs;267 matches all frozen pins with58 unchanged hashes,no Core/Keychain.268 fails exit1 with stdout ID0,no adopted ID and no GUI launch. |Safe metadata only;private paths,Profile IDs,nonce,keys and raw approval data omitted. Failed creation does not prove absence of earlier side effects. |
| `devin-native-legacy269-exact-materialized-20261016.applied.json` |Actual Devin five-file legacy isolation applied;legacy body SHA-256 is `21223664d4f860bfe97914c870a16a9d8fe5d2e2a786856834ed34351857cd46`,byte-exact. |Uncompiled/untested after265. No GUI or new cleanup proof. |
| `q50-gate-cancel270-source-20261016.json` and `q50-ui271-actual-20261016.json` |270 applies eight typed business/physical/delivery files plus one Gate outside-lock cancellation file.271 executes2:1 PASS/1 FAIL. |Previous Unknown misclassification is no longer the observed failure,but original provider unavailable prevents expected batch-too-large. Not UI PASS. |
| `gate272-cancel-wake-all-20261016.json` |Compilation stops on the existing capacity floor/allowance guard,exit-15,zero tests. |NOT PASS;no capacity relaxation. |
| `gate-cli274-all-20261016.json` and277 |274 has22 PASS/1 FAIL actual CLI runs including fork subtest;Gate never executes. After276 fixture warmup,277 passes74:22 unique top-level CLI cases,one fork subtest,Gate 51. |Production273 independent-TTY fix is separate from276 write-history-flag fixture setup. New reentrant-waker regression passes;original approval-turn/flag assertions remain. |
| `ui-provider275-diagnostic-20261016.json` |One exact-selected Snapshot diagnostic fails and localizes storage-sql-query/store-quota. |Exact original source restored;Main untouched. Not a production quota fix or UI acceptance. |
| `devin-process-binding252-20261015-failure.json` |Actual Devin implementation request fails retryably upstream with empty diff;continuation is pending. |No applied ProcessBinding code. Q49 is design-only;Proc/PTY stay Unavailable. |
| `current-cli278-tty-fixed-build-20261016.json` and private279 metadata |CLI build278 succeeds;fresh private Profile 279 succeeds through seven normal reviews and normal Core secret import. |Safe counts/booleans/hashes only;no IDs,paths,secrets,tokens or raw approval material. Not GUI acceptance. |
| `native-legacy-store280-source-20261016.json` and `store281-exact-typed-wire-20261016.json` |Six-file copy280 includes five-file actual legacy separation269 plus Store exact sizing. All four selected typed_sql_ tests pass281. |Focused slice,not whole Store/workspace acceptance. |
| `ui282-exact-wire-actual-20261016.json` and285 |Both runs are1 PASS/1 FAIL.282 fails journal/run payload encoding;285 finds actual 205 versus expected 199 guest-envelope bytes. |Failures retained;strict transport expectation unchanged. |
| `native-contracts283-legacy-split-20261016.json` |Actual pytest1 FAIL/24 setup errors:stale Signed parser and dirty packaged-fixture checkout. |Rust guard0/requires=false is not PASS. Later parser/clean candidate work does not replace this result. |
| `guard284-native-input-union-20261016.json` and `native286-core-host-legacy-separated-build-20261016.json` |284 preserves caller-extra inputs;286 native build passes with 788 complete recorded inputs,including core_host/legacy/debug_scope. |265 omission stays historical. Library dep-info has six sources/no legacy modules;17 library+13 build warnings remain. |
| `ui287-guest-envelope-fixture-20261016.json` and `ui288-real-batch-boundary-20261016.json` |Fixture999820+205=1000025 exceeds unchanged1000000 batch bound;288 passes both original UI cases. |Strict batchTooLarge,removed handle,Session close,real Flow persistence pass. Rust/in-process scope,not native GUI. |
| `native289-process.json` and Root Inspector observation |Actual native GUI loads static Harness but fails script/style self sources under opaque-sandbox CSP. |Partial/failing GUI proof. Only safe artifact hashes/counts public;no PID,path,Profile,nonce,logs or screenshot. |
| `source290-fs-mutators-cli-unused-20261016.json` and `core291-fs-mutators-current-20261016.json` |Actual mutator copy290 precedes all 15 verified-guest FS passes291. |Exact Core filter,not full Core/FS migration. |
| `wasm292-fs-mutators-and-custody-20261016.json` |100 library tests execute:98 PASS/2 FAIL from Denied user declined in new committed-append fixtures. |Integration targets never execute. Historical failures retained before295 legitimate exact FsWrite approval. |
| `clippy294-current-all-targets-20261016.json` |Earlier26 Core errors advance to one remaining collapsible-if production lint. |Zero runtime tests;strict Clippy is not PASS.295 mechanical cleanup has no accepted strict replay here. |
| `source295-fs-prompt-and-tracked-style-20261016.json` and `wasm296-fs-approval-fixture-20261016.json` |Legitimate exact FsWrite approval295 precedes all 138 Wasm passes296:100 library+38 integration. |Both committed-append real Gate/Store regressions retain immutable winner/no replay. No cap/authority relaxation or whole-workspace claim. |
| `native298-clean-source-closure-20261016.json` and299 |Normal closure298 and actual Python27 contracts299 pass on clean five-file candidate e04a4062. |Same10,963 hashes as304/305;Rust guard0 is not the non-Rust test count. Not broader Snapshot/Main equality. |
| `core301-full-current-20261016.json` |133 execute:132 PASS/1 FAIL (Core library102,journal1,provider29 PASS/1 FAIL). |Cancel-winner retry/deadline failure remains unresolved.303 focused1 PASS is not substitute full acceptance. |
| `core303-cancel-winner-focused-20261016.json` |One exact-selected test passes. |Does not erase301 or prove every concurrency schedule. |
| `native304-clean-tooling-test-20261016.json`,305 and306 |Same clean candidate:tooling46 PASS,Ruff/mypy571-file PASS;306 summarizes these runs. |10,963 matching source hashes.306 is metadata,not another execution;later WIP equivalence is unclaimed. |
| `current-source-llama260-20261016.json` |LATE actual18:35:14–18:35:58 UTC:one real1B test PASS,37 deltas,persist/cancel,four secret dispatch/outcome pairs,475 hashes unchanged. |After296 despite label260. Historical69/159/209 unmodified/unrelabeled;no private metadata/key/raw audit read. |
| `current-source-python261-20261016.json` |LATE actual18:37:09–18:37:59 UTC:one Rust test passes original91 cases,446 hashes unchanged,frozen component/corpus match. |After296 despite label261. Public49 remains unchanged;later binding/CSP source needs replay. |
| `core308-DIAGNOSTIC-provider-stages-20261016.json` and restoration |Temporary provider-stage instrumentation passes30;original SHA restored exactly,Main unchanged. |Diagnostic-only,not production acceptance or a resolution of301. |
| `clippy309-current-all-targets-20261016.json` |One E2E network MutexGuard-across-await lint blocks strict Clippy. |Source312 lexically scopes the guard;309 remains failed,zero runtime tests. |
| Actual252 retry1 application,source310,lock311 and `gate313-devin-process-binding-20261016.json` |Actual Devin binding foundation is applied;313 passes all 11 selected tests.311 adds existing hmac/zeroize direct edges only. |Binding generation/verification only. Offline lock preparation is not a build/test;Proc/PTY launch remains Unavailable. |
| `cleanup314-clean-native-contract-candidate-20261016.json` |Owned clean candidate worktree removed after all 51,272 ignored bytes/33 files are preserved;local commit ref kept. |Main unchanged;no model,key or native Profile deletion. Cleanup is not runtime acceptance. |
| `state315-current-flow-gate-store-20261016.json` |All243 selected tests pass:Flow 53,Gate 62,Store 128,450 hashes unchanged. |Not full Core 301 acceptance or validation of Main CSP293/native GUI. |
| Actual293 ten-file CSP application and `native316-harness-csp-canonical-20261016.json` |Main code/probes are present;316 changes only official Harness UI canonical pin,with six live Pack/Profile51 inputs unchanged. |No Snapshot/native replay,Pack approval/activation or GUI PASS.289 failure remains. |
| `clippy317-process-and-network-20261016.json` |Exit101 with two Wasm lib-test errors:mixed inner/outer attributes and unused RunTaskCompletion. |Zero tests;not101 errors and not strict-Clippy PASS. Fixes pending beyond317. |
| `native318-partial-source-copy-rejected-20261016.json`, 319 and 320 retry1 |Partial copy of three files is rejected with no tests; whole-list prevalidation rejects the typo before coherent 14-file retry. Guard 319 includes actual frontend/native/caller extras. |Preparation, not acceptance. CoreHost 321 is the separately executed proof. |
| `native321-csp-core-host-20261016.json` |All 51 CoreHost cases pass, including actual active Pack byte map/policy and both canonical URI schemes; 790 guard entries unchanged. |Library scope only; actual native 289 failure is not replaced. |
| `cleanup325-old-generated-tests-20261016.json` and 334 retry1 |20 exact old test leaves removed; 1,796,403,200 allocated bytes reclaimed. Main Git/newer counterparts retained after metadata/open-FD/PS/helper/lease checks. |No source, authority, model, key, user data or native build-output deletion; no migration credit. |
| `clippy326-csp-and-test-style-20261016.json`, 332 and 337 |Four CoreHost argument-count errors, then five bool-assert errors, then two Flow test-support errors. |Borrowed UiRoute 329 and equivalent assertions 335 advance stages. Equivalent fixes 343 remain un-replayed by 344; strict Clippy is NOT PASS. |
| `native327-published-candidate-20261016.json` |Commit 62866f621 / PR 1521 publishes five files with exact tested b7a48 tree. Python27/tooling46/Ruff+mypy571 scope only. |Tracked sources 10,959 versus historical guard entries 10,963; runtime/CSP WIP excluded. |
| `core328-full-original-confirmation-20261016.json` |Original uninstrumented command passes all 133: library102, journal1, provider30; fixture c078 unchanged. |Historical 301 intermittent failure cause remains unknown. No diagnosed-fix or old-failure reclassification claim. |
| `frontend330-core-host-ts-20261016.json`, 336 and 338 |330 rejects before Node; exact two external pins get a separate inventory.336 executes Node23:22 PASS/1 FAIL missing HTML;338 copies/guards it. |Failures retained; Rust guard zero is not the actual Node count. |
| `capacity339-preflight-rejected-20261016.json` and 340 |339 starts no command/tests on unchanged floor+1 GiB rule.340 permits zero library deletions and lists ten undeleted test candidates. |NOT test PASS, no capacity relaxation or speculative reclamation. |
| Actual307 source and `native341-devin-csp-probes-canonical-20261016.json` |Two probe files applied; raw manifest SHA bac8ce and canonical Pack pin2c956 are distinct. |Source-only, not deployed or physically native executed. Actual289 FAIL remains. |
| `frontend342-core-host-ts-complete-inputs-20261016.json` |All 23 actual Node tests pass; source/support/two external-pin inventories unchanged. |Not native WebView proof. |
| `frontend344-core-host-build-csp-20261016.json` |Actual frontend production build passes, finished19:28:38 UTC, ten modules transformed. |Build only, zero tests; unchanged floor/1 GiB/170s/jobs1 rules. No post344 acceptance. |
| Strict workspace Clippy 351 | PASS | 791 unchanged inputs; zero test bodies; before 360/365. |
| Native core-host build 352 | Build PASS | About 44 seconds, 115232608-byte historical binary, 791 inputs; 17+13 warnings, no GUI acceptance. |
| Actual native GUI 355 | FAIL / timeout | CSS/JS/SDK ready; Send load/unavailable, pending save, no answer; 170-second SIGTERM, no normal Stop/Quit. |
| Whole workspace 359 | NOT PASS | Capacity guard after about 71 seconds compiling; zero tests, unchanged floor/1 GiB allowance. |
| Real received-UI live regression 361 | 1 PASS | Real1B emit/save/complete/persisted tail on direct current-thread Session; not native355 reproduction. |
| Store process launch 366 | 7 PASS / 1 FAIL | Watermark expected1, actual4; exact five-file365 source, repair367 pending/unapplied. |
| CoreHost process review 368 | 52 PASS | Includes native redaction; no physical Proc/PTY or native GUI proof. |

Root preserved 26 files of the validated Devin checkpoint in private evidence
and committed them with five documentation files as `d8ec5a481`. The portable
validation receipt records their relative paths and hashes. Subsequent VM-only,
registry, census-module and verified-guest FS experiments are separate records.
Each execution applies to its own source; a module pass does not imply wiring.
The later snapshot 109 adopts actual census/Join paths, 112 updates a Flow
notification assertion and 115 applies Devin's prefunded cache. Production Q34
manifest 143 and minor145 have provider 147 and Wasm 149 passes after test-only 148;
historical Wasm 146 remains failed. Actual Devin 142 patch 3 was applied with Root
context/format repair and source 156, then all 82 Core cases passed in 157. The
process-funded Core cleanup registry has 256 slots, independent of UI 64/1024.
Flow 163 passes 51 cases after 162; actual 1B receipt 159 passes the earlier Q34/UI 156 snapshot.
Gate/Store owned-consumption integration223 passes 174 after expiry fixtures219/222;
current original Python 218 passes 91 unchanged cases on Q42+Gate 211/213. Actual
Devin FS 205 patch2 is applied225 and guest 226 passes 11 on the old native pool.
Native pool 230 first compile-fails231, then source 232 uses existing dependencies
and std only, allowing Wasm 233:94 PASS. Fault tests236 append four tests with the
production prefix unchanged per Root source report. Combined237 then fails three
cases. Source239 prepares coherent Flow/Core changes and corrects two IO assertions
to actual charged custody;241 adds test-only census observer quiescence. Actual242
passes all136 Wasm cases, including those three failures. Keep funded observer
custody until physical completion; no early production refund is claimed. Core240
passes125 and selected Flow+Gate+Store245 passes225 on their exact source manifests.

Historical Clippy243/248/250 failures remain. Actual294 advances the26 Core
errors to one collapsible-if;295 mechanical cleanup has no strict replay. UI271,
282 and285 failures remain before strict2882 PASS after Store sizing281 and actual
envelope fixture287. FS 291 passes15 andWasm 296 passes138 after292 fixture denials
are corrected with legitimate exact FsWrite approval295. These scopes do not
establish latest-Main/full-workspace acceptance.
Native238 applies247 and CoreHost254 passes47. Legacy separation269 is copied280;
native286 builds the core-host branch with 788 complete recorded inputs and no
legacy modules in library dep-info. Actual GUI 289 launches but fails opaque-
sandbox CSP. Prior265's guard omission stays historical. Linux workflow is unrun.
Q48/Q49 remain design-only;actual252 continuation is coding without output after
retryable upstream failure. Proc/PTY stay unavailable. Q50–Q52 contracts are in
[resource-boundaries.md](resource-boundaries.md);bridge293 is preparation only.

The focused Launcher frontend contract/isolation run passed 21 tests. Current
Ruff and mypy checks passed for 571 files. The native acceptance fixture now
contains bounded iframe probes and a separate Dangerous storage request. Its
real 101,309-byte component now builds offline with its isolated locked dependency
graph; both fixture Packs were staged with actual manifest pins. CLI verification
now passes for both staged Packs; actual native WebView presentation remains
unrun. The ordinary foundation workflow no longer downloads or builds the
optional private SQLite experiment; that job requires an explicit manual input.

The Python artifact receipt is
`python-official-current3/shell-policy.build.json`, with SHA-256
`1d8571d4419aca7c8ee8e05d9a0a426ba976c5a638fd757f094f3dc27cf886f2`.
The original 91-case corpus/order/pins remain intact. Producer hardening covers
annotated WIT rejection, exact directory dependency closure, case-folded and
generated-path collisions, installed RECORD consistency, Windows tool paths,
compiler post-run identity and dependency symlinks. Passing source tests do not
establish guest execution acceptance.

The [published historical Python runtime receipt](../../../crates/tobkiri-e2e/evidence/python-current-parity-20261015.json)
remains frozen at receipt49. Earlier104/120 evidence remains historical. Actual
218 freshly passes all 91 original classifier/tokenizer cases in one Rust test
on Q42 plus Gate 211/213, with unchanged corpus,21,475,543-byte component and eight
Pack hashes. Its441 source hashes match before/after. This is Rust/in-process
verified-Wasm execution, without launching native Python or a compiler. No
private Pack/Profile identity is published, and later source requires replay.

The portable [global canonical receipt](../../../crates/tobkiri-canon/evidence/global-contracts-canonical-20261015.json)
records the frozen source, generator, corpus and Rust source identities. Both
original Python functions were replayed directly against all 885 LegacyFloat
rows: 872 exact byte/digest matches and 13 matching rejections. The inventory
marks only `core_runtime/global_contracts/canonical.py` as `core+vectors`.

The [published historical 1B receipt](../../../crates/tobkiri-e2e/evidence/current-1b-20261015.json)
records runtime and Pack identities, the actual 806,058,240-byte GGUF hash,
37 streamed deltas, saved history, cancelled-turn exclusion and four successful
secret dispatch/outcome pairs from receipt 69. Its 51 Pack input hashes match the
actual before/after execution guards. This frozen Devin-source replay predates
A2/FS/census changes. It contains no credential or private model path.
Actual replay119 passes 109/112/115;159 later passes Q34/UI156, before Q39.
Fresh actual 209 then passes Q42/native-Busy source with 37 deltas, persistence,
cancellation and four secret pairs under 470 unchanged source/input hashes.
The validation receipt records these distinct executions; public 69 and earlier
159 evidence remain unchanged. Private209 metadata contents are not published.
The later Gate/FS/Q46 source and native App still require their own acceptance.

The [current FS receipt](../../../crates/tobkiri-core/evidence/current-fs-20261015.json)
records registry runs of four and ten cases, plus two actual verified-guest ABI
cases, with their exact before/after source identities. The ten-case registry
run includes the earlier four; these are not sixteen distinct tests. It preserves
the fixture patch and snapshot provenance, without private paths or raw audit
payloads. The later combined run 117 passes ten registry and two guest cases on
census/cache integration source. Later eight-case guest replay 164 fails6 PASS / 2 FAIL;
symlink expected-code correction168 alone was not a pass. Zero-test169 and exact
count-probe 170 retain their original nonacceptance/failure. On coherent Q39 and
bounded native preflight173, actual 180 passes eight cases and 190 passes nine.
Actual Devin 205 patch2/225 then passes all 11 verified-guest cases226 on the old
native pool. Historical237 adds focused consumed-supervisor faults and fails two
IO Task-charge expectations plus the census final-byte assertion. Source239
corrects the two assertions to actual charged custody and241 adds test-only census
observer quiescence. Actual242 passes136 Wasm tests, including all four FS faults
and the earlier census failure. Physical-deallocation custody remains required;
this focused pass does not close FS migration or broader runtime acceptance.

Later mutator source290 passes15 actual verified-guest cases291. Wasm292 has98 PASS/
2 FAIL from fixture-declined FsWrite;legitimate exact approval295 precedes296 all 138
PASS,including both committed-append real Gate/Store regressions. Actual custody,
immutable winner and no replay remain required;full FS migration is still open.

## Historical evidence retained

The five frozen legacy heads are ancestors of two-parent merge
`7260658b22dae1412fd2ac187ddd7cc0c0cafd84`. Repairs are `682b8e8a0` and
`4c2a5c6f6`; verification documentation is `9f732bcb3`.
[merge-m0.md](merge-m0.md) records the exact commands, commits and receipts.
The final legacy source cluster had 311 passes and one existing Windows
MAX_PATH skip. All 16 original boundary failures passed on that source; the
broader 362-case boundary batch passed at the previous repair commit. Ruff and
mypy passed for 571 files, nine producer freshness checks passed, and tooling,
Launcher and Defaults focused checks had 46, 61 and 191 passes respectively.
These results apply to the legacy checkpoints. The 69-row ledger still has open
acceptance work, including eight browser failures.

Earlier Rust foundation checkpoints exercised canonical/signing vectors, the
17 core WIT interfaces, manifest/Profile compilation, Flow/overlays, durable
Gate/epoch/CAS, storage isolation, secret AAD, Wasm limits and cancellation,
generation-bound handles, and output/cleanup custody. Their focused counts are
not a current full-workspace result. In particular, the historical decision to
ignore two native SQL positives while that path was unavailable does not describe
the now-approved native DATA architecture.

[m2-final.json](../../../crates/tobkiri-e2e/evidence/m2-final.json) preserves four
passing deterministic tests and one real Gemma 3 1B test. The live path was
Harness input → Flow → Wasm provider → authenticated local llama.cpp → 37 deltas
→ SQLite persistence; the next turn loaded the saved messages. Cancellation
stopped inference within three seconds without saving the cancelled turn, and
secret dispatches each had one terminal outcome without plaintext leakage.
Source identity matched before/after that batch. It predates strict authority,
SQL 1.1, allocation/control, stream/UI lifecycle and outbox changes and does not
prove current runtime availability.

The Pi hello and protected-path component proofs in
[pi-producer-runtime-002.json](../../../crates/tobkiri-e2e/evidence/pi-producer-runtime-002.json)
also predate strict User policy. Python outer resource ABI proofs and
[python-outer-producer-pending-002.json](../../../crates/tobkiri-e2e/evidence/python-outer-producer-pending-002.json)
remain historical evidence. The source-preparation 001/002 receipts record
patch application and syntax/contracts only; they do not prove a compiled
patched CPython/compiler, guest activation or current 91-case parity. The
private fixed-16-MiB SQLite module and Linux SDK34 producer recipe likewise
remain optional prototype evidence, without a current Linux CI receipt.

Launcher adapter/frontend/native typecheck and vendored Wasmtime checkpoints
retain their original source limits. Newer lifecycle/ACK, runtime/control and
native Input/Flow entry changes require current behavior tests. CoreBuffer
custody and bounded final host copies do not claim whole-WebView RSS or physical
browser deallocation on ACK.

## Work remaining for acceptance

- Preserve the unexplained historical Core 301 intermittence despite original
  full 328 PASS; no diagnosed repair is claimed.
- Diagnose actual native 355 `load` / `unavailable`. Closed metadata and typed-error
  erasure do not identify SQL or another backend cause. Direct current-thread
  live UI 361 PASS does not cover native spawn/multithread or GUI lifecycle.
- Complete actual 367 repair and replay the Store process target: 366 is 7 PASS /
  1 FAIL. CoreHost 368 redaction 52 PASS does not replace that failure. Physical
  Proc/PTY stays Unavailable; executable/cwd FD ownership is still unproved.
- Execute actual Devin 307's nine-probe Node/native closure after source plans
  349/364 and normally reapproved official Packs. Actual 355 module/CSS/SDK-ready
  observations do not establish the cross-Pack, remote, IPC and navigation denial
  matrix or normal Stop/Quit.
- Replay strict Clippy and full coherent workspace after 360/365. Historical 351
  lint PASS predates them; 359 stops compilation with zero tests at unchanged
  capacity limits. Replay unchanged Python/dedicated real1B and Linux CI on later
  source. Published PR 1521 remains the exact clean five-file tree only.
- Keep Q54 Core-owned FD registration and bounded CoreControl memory/Registration
  as approved adviser design, not implemented or validated work. Preserve all
  historical failures, diagnostics, zero169 and 49/69/159/209 proofs.
- Execute the Linux workflow; actual helper 227:7 PASS is only helper-unit evidence.
- Preserve the executed Devin admission/owned-Join and Q37 durable handoff fixes:
  use the existing Gate's exact pending ACK while retaining the business error.
  Keep Q30/Q35's narrow sink-origin recovery and latched unknown Join; no replay
  or immutable-winner replacement. Approved remaining designs need actual replay.
- Replay unchanged Python after later runtime changes. Actual 120 predates Q34;
  fresh real 1B 209 covers its Q42 source, while public 49/69 and earlier 159 remain
  historical. Later Gate/FS/Q46 source needs appropriate replay.
- Preserve the passing Store 121-case and bounded quota-repetition receipts;
  continue current private/public/readonly, persistence, cancellation, contention
  and crash checks when DATA or cleanup authority changes.
- Exercise current User/Flow and exact Native Input routes, streamed progress,
  hostile ABI sizes, limits, cancellation and unfinished cleanup custody.
- Run current CLI operations, Launcher/WebView isolation and lifecycle paths,
  complete Pack/SDK linking and remaining Harness tool-loop paths. Preserve the
  recorded fresh209 Core/provider/local API1B result on its own source.
- Run the locked workspace test bodies after compile 127, strict Clippy,
  formatting and Linux CI;
  migrate the remaining 836 inventory units and satisfy the final gate. Full DB-API,
  Harness tool-loop/protected-file effects, mediator conversions and complete
  Pi/extension compatibility remain open.

Implementation workers use GPT-6.1 Sol xhigh without Fast. Devin CLI SWE-2 Max
performs scoped implementation, with Root integrating changes and running all
actual builds and tests. Current Devin tasks are implementation only; no
additional code-review tasks are requested. Astra HIGH supplies design answers
only, without agent tools. Historical runtime, E2E and producer reviews led to
fixes. Earlier cached-fiber and parser reviews found no
confirmed actionable defects in their supplied fixes. The DATA journal review
identified repeated journal validation work; full-caller inspection rejected
repair bypasses and confirmed existing transaction and private-alias guarantees.
That narrow optimization and its tests remain under validation. Each review
applies to its supplied source snapshot.

Heavy builds and replay run serially under one measured capacity lease with a
5.5 GiB free-space guard. Source, private data, models, credentials and recovery
evidence are preserved. A draft checkpoint does not establish completion.

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

## Current execution boundary through 496

This update appends 21 actual execution records to the [validation checkpoint](checkpoint-validation-20261015.json), for 280 records. All 259 earlier objects,including 49/69/159/209 history,retain their exact bytes. The [focused FS receipt](../../../crates/tobkiri-core/evidence/current-fs-20261015.json) adds one 15-case replay while preserving all 15 earlier objects. Later executions are excluded. Goal remains active: 1/837 closed, 836 TODO;completion false.

| Current evidence | Observed result | Scope |
| --- | --- | --- |
| ProcessCatalog | Four production files applied;13 real tests PASS. | No executable/cwd FD or physical Proc/PTY availability. |
| Canon recovery | Full crate 41 PASS;strict Canon Clippy PASS. | No fresh strict full-workspace claim. |
| Native preparation | Owned Send future/busy-retry and test trait import corrected;21 tests PASS. | Earlier compile failures retained;no GUI acceptance. |
| Actual real1B multithread routes | Direct UI guest 1 PASS;Launcher 1 FAIL. | Direct pass does not cover Launcher/WebView GUI. |
| Focused runtime | FS 15 PASS;port proxy 20 PASS;Core --lib 116 PASS before three new native-frame tests;the three later tests PASS. | No full coherent workspace or diagnosed cleanup repair;full atomic native-ID/control binding unproved. |
| Stop-control frontend | Actual Devin source/seven tests;Node 30 PASS,typecheck 1 FAIL,one-line String assertion repair,typecheck PASS,Node 30 PASS again,Vite build PASS. | Build regenerates UI;no later native execution/GUI Stop acceptance. |

Actual native build succeeds,but GUI Send times out without answer/save. Stop removes the iframe while the host label stays Profile Running. Cmd+Q exits normally with 0. HTTP/secret arrival,physical descendant cleanup and GUI cause remain unknown/unproved. No GUI Send/normal Session Stop PASS is claimed.

A temporary Snapshot-only Launcher diagnostic fails one test:exact UiOperationCloseControl admission.try_lock returns WouldBlock and outer pull maps to Core. No in-band Flow failure is observed. Exact source bytes are restored with fresh mtime and Main unchanged. This diagnoses that instrumented reproduction only;the prior Launcher failure and actual GUI cause remain unproved.

Required tooling fails 46 setup errors because the official fixture requires a clean checkout;the combined command never reaches lint. A private clean-candidate procedure is prepared,but its provisional 94-path inventory predates later Core/Stop edits and must be re-frozen. No new clean candidate/required-check PASS exists. Published HEAD62866f621 remains unchanged;runtime source is uncommitted WIP.

Actual census output has multiple correctness findings and is held from Main pending corrective code. Guest audit remains private virtual reconstruction;owned Phase2 is pending,not Main-applied/runtime-proved. Physical Proc/PTY stays Unavailable. Atomic binding,cleanup diagnosis,native Send/truthful Stop,Linux CI and later-source dedicated compiler/runtime acceptance remain open. Floor 5,905,580,032 bytes,1 GiB growth,170 seconds,jobs1 and serial capacity lease remain unchanged.

## Current execution boundary through 587

This heading and its counts describe the historical 587 snapshot only. They do
not state the current published source, inventory size or final acceptance.

The goal remains active: **1/837 closed, 836 TODO**. Runtime migration is uncommitted WIP; published HEAD remains `62866f621`. This boundary adds 36 receipt-bound records to the [validation checkpoint](checkpoint-validation-20261015.json), for 316. All previous 280 objects retain their exact bytes, including 49/69/159/209 history. The [focused FS evidence](../../../crates/tobkiri-core/evidence/current-fs-20261015.json) remains 16 records; no new FS runtime scope is inferred. Later 588+ executions are excluded.

| Area | Established through this cutoff | Remaining acceptance |
| --- | --- | --- |
| Secret work | Store 4 new tests/full 140; SecretC 8; D1 fourteen custody tests and strict Wasm Clippy pass. D1 uses closed errors and source-derived admission funding. | Outer Box, task-cell and returned-error lifetimes, final owned-resource ABI handoff and current real 1B remain open. |
| Core operation custody | Settled notification metadata is distinguished from unfinished delivery; Q79 funds new strong/weak handle storage. The focused operation-intent suite passes 10. | Prepared-context/durable cause-wake and Host integration are pending; Launcher 474 is not repaired or replayed here. |
| Core cleanup | Full runs preserve 138 PASS/2 FAIL and 141 PASS/1 FAIL; the exact scalar case separately passes 1. Temporary diagnostic 575 also reports 141/1; refined diagnostic 585/586 passes 146. | The 146 pass is instrumented Snapshot evidence, with no timeout/probe observed, not an intermittent fix or ordinary current-Core full acceptance. |
| Harness tool/Pi | Private A1 native pure tests pass 9; two wasm32-wasip2 debug components build. Run 583 observes artifact headers/hashes only. | A1 is not applied to Main; targeted producer integration and actual Core tool Flow/permission/FS E2E are pending. Build/metadata are not runtime acceptance. |
| Owned-resource ABI | One vendor rollback file is applied with four owner-denial tests included. | Those tests are unexecuted at 587; the final guest-delivery commit seam remains unproved. |
| Required checks | Historical clean SecretB generator check, tooling 46, Ruff and mypy 571 pass without fixture bypass. | Later C/CoreA/D1/Q79/vendor edits are outside that tree. Current Main needs a new frozen clean candidate and required checks. |

The temporary diagnostics restore exact Host/core-import bytes with fresh mtime and leave Main unchanged. Run 585 also verifies 638 unrelated Snapshot sources unchanged. Diagnostic 575 locates receipt-wait after VM completion while the original cleanup Join is unfinished, but does not identify which inner receipt/guard owner is responsible. Run 586 passes without reproducing the failure, so its 146 passes supply no missing failure observation.

The two actual direct 1B passes 526/527 cover Input/Flow/secret/network/stream/save on SecretB. They predate later C/CoreA/D1/Q79 and do not establish current-source, Launcher-facade or native WebView acceptance. Earlier GUI Send timeout/no save and iframe removal with Profile Running remain unresolved; no new GUI replay or later native rebuild is included.

Workspace 511 stops at 170 seconds after 223 completed passes/7 ignored; Store 140, UI 2 and six-package 252 shards pass separately. These source checkpoints are not combined into a single coherent workspace PASS. Strict workspace Clippy 500 also predates the later changes. The actual CoreHost package is `tobkiri-launcher-core-host`.

The clean SecretB checkout is retired after preserving recovery evidence and ignored files, with Main source/HEAD/index unchanged. Its checks remain valid for that historical tree. Audit and Census returned code remains privately staged, uncompiled and unapplied to Main. ProcessCatalog retains real registered executable/cwd Files; this is registration custody, not immutable-object or physical Proc/PTY acceptance. Native Send/save/truthful Stop, current runtime/compiler replay, Linux CI and full migration remain open. The floor 5,905,580,032 bytes, 1 GiB growth, 170 seconds, jobs1 and serial capacity lease are unchanged.

## Applied Audit/Census checkpoint 1041

Main1040 adopts the coherent Audit910/Census930 source set and its normal lock
update: 23 source/configuration files plus the workspace lock. Snapshot1041 is
refreshed to the same identities. This supersedes the earlier private/unapplied
Audit/Census status for this adopted slice; historical entries keep their own
source and execution scopes.

| Coherent validation before identical Main adoption | Actual result | Scope |
| --- | --- | --- |
| Core library | 169 PASS; 0 ignored, 0 filtered | All library tests run without skips, including the three real-WAT Audit cases and the historical scalar/UI-capacity cases. |
| CoreHost library | 61 PASS; 0 ignored, 0 filtered | All library tests in `tobkiri-launcher-core-host`; no native GUI acceptance follows. |
| Strict Clippy | PASS; zero tests | All targets of Core, CoreHost, Wasm, Store and Canon, with warnings denied. This is not a full-workspace test or lint result. |

Owned Audit append/read now runs through genuine Core frames, the original
nested Audit permit and the final canonical Value-delivery hook. Tests preserve
provenance, authenticated immutable acknowledgement, a single committed append
after late canonical bounds failure, and original outbox repair without
reappend. Census adoption retains concrete bounded admissions and funding
through actual body/monitor, Trailer-backed Waker and weak-owner lifetimes.

The [validation checkpoint](checkpoint-validation-20261015.json) adds only these
three coherent executions: 316 historical records become 319, with every old
raw object, including the earlier 280, intact. This is a selective current-source
checkpoint, not a claim that every intervening execution is catalogued. Focused
FS remains 16 records; no new FS-specific replay is inferred.

Both previously intermittent scalar/UI-capacity cases pass in this run. That
observation supplies no causal-fix or permanent nonrecurrence claim. Earlier
instrumented 902 (164 PASS / 2 FAIL) and standalone 937 (165 PASS / 1 FAIL) remain
separate historical results. Opaque Tokio/OS/TLS byte-exact accounting, complete
native RSS and universal no-abort under arbitrary trusted Rust Waker double panic
are documented limitations, not mandatory gates for this adopted slice. Concrete
admission controls and their actual custody funding remain required.

The goal remains unfinished. Harness/default ordinary migration, resource C,
Host B2, current-source real1B, native GUI Send/save/truthful Stop, coherent
workspace and required clean-candidate checks remain pending. Physical Proc/PTY
is still Unavailable. No earlier inventory unit or historical proof is closed
or broadened by this checkpoint.
