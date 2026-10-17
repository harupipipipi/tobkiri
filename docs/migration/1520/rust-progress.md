# Issue 1520 progress

The goal remains active. This is a draft checkpoint for review.
Draft source checkpoint `ec8a0688f4104052b0ccc29f0afc0fb4ea687f43` has been
pushed. Later UI, callback and cancellation-cause/control fixes remain under
implementation or partial validation. The latest recorded Devin snapshot passes
callback reentry and the happy UI Flow/persistence path; oversized UI cleanup
and six provider lifecycle cases still fail. Newer main A2/FS/census changes are
outside those results and require their own recorded replay.
The final migration gate remains red. Current Core Harness execution with the
real 1B model now passes; complete migration, passing CI and native WebView
acceptance are not claimed. Of 837 migration
inventory units, 836 remain unresolved. The two-function global canonical
mechanism has checked Core/vector evidence; no legacy source has been deleted.

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
| `snapshot-core-lib-q19-reached14-20261015.json` | 57 of 59 Core library tests passed, including actual Native Input Flow dispatch, source projection and mediated context narrowing. The registration callback deadlock no longer occurs. | This earlier snapshot failed the static-denial fixture and UI handle-capacity cleanup case. The later 61-case Core replay is recorded below. |
| `snapshot-core-input-static-runtime-negative23-20261015.json` | The corrected five-boundary negative passed: one real static rejection before runtime creation and four actual source-specific runtime denials with zero dispatch. | The whole Core library and subsequent authority changes need their own replay. |
| `snapshot-core-factory-current41-20261015.json` | 60 of 61 Core tests passed, including the two real control-failure/publication regressions and all original Native Input authority cases. | UI handle-capacity/session-close still reports cleanup completion unknown. No all-Core pass is claimed. |
| `snapshot-provider-29-q19-reached16-20261015.json` | 20 of 29 real Core provider tests passed, including actual Native Input, guest Flow entry and same-reference provider contract negatives. | This earlier snapshot had nine failures. The latest 30-case provider replay is recorded below; the old 22 Wasm tests remain intact. |
| `snapshot-provider-current-30-45-20261015.json` | 23 of 30 actual Core provider tests passed, with source identity matched before/after and no capacity-guard stop. | This earlier run had seven failures. Callback reentry passes in the later standalone and 30-case replays below; no all-provider pass is claimed. |
| `snapshot-devin-callback-reentry58-20261015.json` | The actual callback-reentry regression passed. | One focused case; later source changes need replay. |
| `snapshot-wasm-core-typed-proxy60-20261015.json` | Both actual Wasm public-Core typed-proxy cases passed: provider/durable-audit success and denial without effective permission. | This target is not all-Wasm or full provider lifecycle acceptance. |
| `snapshot-provider-devin-callback-api64-20261015.json` | 24 of 30 actual Core provider cases passed, including callback reentry. Source identity matched before/after, with no capacity-guard stop. | Six cancellation/cause-selection, expiry/revocation and owning-runtime cleanup cases still fail. This precedes the newer A2/FS/census changes. |
| `snapshot-devin-profile-flow66-20261015.json` | All 28 Profile compiler cases passed, including three actual regressions for exact dynamic-Flow selection, no minor widening and exactly one selected implementation. | Compiler acceptance on this source; runtime/native acceptance remains separately measured. |
| `snapshot-cli-portable-profile-review15-20261015.json` | All eight CLI Profile-input tests passed, including a real separate-process working-directory change and URL/UNC rejection. | Input normalization acceptance only; full CLI command and native acceptance remain open. |
| `snapshot-flow-completion18-20261015.json` | All 32 Flow tests passed, including four actual interpreter completion/custody, abandoned close, failure and channel-loss regressions. | Does not establish the parent component's scoped child census or cancellation-cause ordering. |
| `snapshot-canon-flow-incremental32-20261015.json` | All 70 Canon and Flow tests passed after bounded incremental stream collection replaced the bytes-as-element-capacity preallocation. | Includes new admission, growth, ownership, encoded-overhead and actual awaited-cleanup regressions; not a native UI proof. |
| `snapshot-wasm-warm-fiber-fix4-20261015.json` and `snapshot-provider-float-warm-fix4-20261015.json` | Actual repeated cached-fiber calls and the real tiny float provider passed. | The fix preserves physical stack custody while avoiding new charges to a retired invocation owner; not full ABI acceptance. |
| `python-producer-sol-xhigh-20261015.json` | All 54 Python SDK tests passed, including 29 producer/composition checks; installed RECORD consistency and console launcher selection passed. | No component rebuild or Rust replay in this checkpoint. Archive verification remains honestly false when no archive was supplied. |
| `python-official-shell-policy-build3-20261015.json` | Current pinned official-wheel producer built the 21,475,543-byte shell-policy outer component with exact ABI/WIT checks. | Source/build provenance is not current guest execution acceptance. |
| `snapshot-python-current-parity6-nominal-20261015.json` | The actual 21,475,543-byte current Python component passed all 91 unchanged differential cases through Rust Core. | Classifier/tokenizer scope only; no other Python Pack, full DB-API or Harness acceptance is implied. |
| `snapshot-python-current-q19-factory49-20261015.json` | The latest recorded Python replay passed the same 91 classifier/tokenizer cases through the actual Rust host: one Rust test passed, with unchanged component/corpus identity, source identity matched before/after and no capacity-guard stop. | The portable runtime receipt carries this run's 412 source hashes. Later frozen Devin and A2/FS/census sources await Python replay. Other Python Pack, full DB-API and whole-Harness acceptance remain open. |
| `snapshot-e2e-deterministic-q19-flow21-20261015.json` | All four selected test targets compiled and executed with the actual budgeted HostIo fixture. | This earlier run failed all ten tests: most reached `generate / limit-exceeded`; UI returned provider unavailable. The later incremental-collection and phase-diagnostic runs are recorded below. |
| `snapshot-llama-current-q19-22-20261015.json` | The opt-in real-1B test executed with the declared localhost server and real encrypted Vault import. | This earlier run failed at `generate / limit-exceeded`; the later real-1B acceptance is recorded below. |
| `snapshot-e2e-incremental-collection36-20261015.json` | All eight chat, exact Native Input and real local SSE-provider tests passed, including persistence/restart, partial fanout failure and cancellation. | Both UI tests still fail with provider unavailable. The current real-model replay is separate. |
| `snapshot-llama-current-incremental38-20261015.json` | The actual current Gemma 3 1B test passed: Harness input → Flow → Wasm provider → authenticated localhost llama.cpp, 37 streamed deltas, persistence/next-turn read, cancellation without save, and server stop within three seconds. | This is Core Harness acceptance. Native Launcher/WebView, all provider lifecycle checks, CI and the remaining migration remain open. |
| `snapshot-devin-ui-phase47-20261015.json` | Both actual UI tests ran and failed at `submit` with provider unavailable: zero passed, two failed. The oversized fixture completed both append calls and their success assertions before submit failed. Source identity matched before/after; no capacity guard stopped the run. | This earlier phase diagnostic is retained. The later UI replay below passes the happy path; oversized cleanup and native WebView acceptance remain open. |
| `snapshot-devin-ui-flow-resolution67-20261015.json` | The actual scoped UI stream → Flow → verified persistence test passed. The two-case target finished with one pass and one failure, with source identity matched before/after. | The oversized transport case reports `stream cleanup completion unknown` instead of the expected bounded failure. Complete UI cleanup and native WebView acceptance are not established. |
| `snapshot-native-provider-cli-verify50-20261015.json` and `snapshot-native-ui-cli-verify52-20261015.json` | Actual CLI `pack verify <staged Pack>` passed for both staged native-acceptance Packs and reported their pinned manifest hashes. | `<staged Pack>` replaces the private absolute path in the published command. This verifies Pack admission; native execution and WebView presentation remain unproved. |
| `sdk-current-regenerated-pack-client61-20261015.json` | All 12 JavaScript SDK/regenerated Pack-client tests passed, with recorded source identity matched before/after. | This is separate from the 54 Python SDK/producer tests; it is not native UI acceptance. |
| `frontend-core-host62-20261015.json` | All 21 focused Launcher frontend Core-host contract/isolation tests passed, with recorded source identity matched before/after. | Frontend contract evidence; actual native WebView presentation remains unproved. |
| `snapshot-llama-after-devin-inputs69-20261015.json` | The real 1B Core Harness replay passed with 37 deltas, persisted/next-turn history, cancellation without save and server stop. All 51 declared live Pack inputs were included in the matching execution guards. | The portable current-1B receipt now records this frozen Devin snapshot before A2/FS/census changes. Other Packs, latest main, native WebView and full workspace/CI remain outside this replay. |
| `committed-tooling-checkpoint42-20261015.json` and `committed-lint-checkpoint43-20261015.json` | On the clean `ec8a0688` draft checkpoint, `just tooling-test` passed 46 tests; `just lint` passed Ruff and mypy for 571 source files. Both recorded source identities matched before/after. | These legacy quality checks do not establish Rust workspace, provider lifecycle, CI or native UI acceptance. |
| `rustfmt-after-devin-code48-20261015.log` | Root reports that the formatting check passed. The retained log is empty. | The empty log does not independently record the command, exit status or source manifest; it is not behavior acceptance. |

Root preserved 26 files of the validated Devin checkpoint in private evidence
before refreshing the joint A2/FS snapshot. The portable validation receipt
records their relative paths and hashes. Each execution still applies to its
own recorded source; these results do not certify the newer main A2/FS/census
implementation or turn an in-progress joint run into a pass.

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

The [current Python runtime receipt](../../../crates/tobkiri-e2e/evidence/python-current-parity-20261015.json)
records the actual successful 91-case command, unchanged corpus identity,
component identity and runtime source hashes from receipt 49, finished at
`2026-10-15T10:54:07.804126+00:00`. The execution used verified Wasm in the Rust
product host; it launched no native Python or compiler. The earlier 91-case
receipt remains historical evidence; the corpus and component bytes were not
changed for this replay. Replay after the later frozen Devin and A2/FS/census
changes remains pending; the 91-case pass is not acceptance of newer main.

The portable [global canonical receipt](../../../crates/tobkiri-canon/evidence/global-contracts-canonical-20261015.json)
records the frozen source, generator, corpus and Rust source identities. Both
original Python functions were replayed directly against all 885 LegacyFloat
rows: 872 exact byte/digest matches and 13 matching rejections. The inventory
marks only `core_runtime/global_contracts/canonical.py` as `core+vectors`.

The [current 1B receipt](../../../crates/tobkiri-e2e/evidence/current-1b-20261015.json)
records runtime and Pack identities, the actual 806,058,240-byte GGUF hash,
37 streamed deltas, saved history, cancelled-turn exclusion and four successful
secret dispatch/outcome pairs from receipt 69. Its 51 Pack input hashes match the
actual before/after execution guards. This frozen Devin-source replay predates
A2/FS/census changes. It contains no credential or private model path.

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

- Finish the six provider lifecycle failures and oversized UI cleanup failure,
  including cancellation-cause ordering and unfinished cleanup custody. Preserve
  the recorded callback and happy UI Flow/persistence passes while validating
  later A2/FS/census changes. The 91-case Python replay passes only at receipt
  49; retain its unchanged corpus/component and run the pending later replay.
- Preserve the passing Store 121-case and bounded quota-repetition receipts;
  continue current private/public/readonly, persistence, cancellation, contention
  and crash checks when DATA or cleanup authority changes.
- Exercise current User/Flow and exact Native Input routes, streamed progress,
  hostile ABI sizes, limits, cancellation and unfinished cleanup custody.
- Run current CLI operations, Launcher/WebView isolation and lifecycle paths,
  complete Pack/SDK linking and remaining Harness tool-loop paths. Preserve the
  passing current Core/provider/local API 1B receipt.
- Complete the locked workspace tests, strict Clippy, formatting and Linux CI;
  migrate the remaining 836 inventory units and satisfy the final gate. Full DB-API,
  Harness tool-loop/protected-file effects, mediator conversions and complete
  Pi/extension compatibility remain open.

Implementation workers use GPT-6.1 Sol xhigh without Fast. Devin CLI SWE-2 Max
also writes scoped fixes in private source copies, with Root integrating changes
and running all actual builds and tests. New Devin tasks perform implementation
at the user's instruction. Astra high supplies
design answers only; its current Devin adviser quota is exhausted. Root owns the
SWE-2 reviews through the re-enabled Devin CLI. Completed runtime, E2E and
producer reviews have led to fixes. Cached-fiber and parser reviews found no
confirmed actionable defects in their supplied fixes. The DATA journal review
identified repeated journal validation work; full-caller inspection rejected
repair bypasses and confirmed existing transaction and private-alias guarantees.
That narrow optimization and its tests remain under validation. Each review
applies to its supplied source snapshot.

Heavy builds and replay run serially under one measured capacity lease with a
5.5 GiB free-space guard. Source, private data, models, credentials and recovery
evidence are preserved. A draft checkpoint does not establish completion.
