# Issue #1520 acceptance status

Scope rechecked: 2026-10-31. The 20 items below still follow the full acceptance
list in [issue #1520](https://github.com/harupipipipi/tobkiri/issues/1520).
The issue is normative; [resource boundaries](resource-boundaries.md)
record the later implementation decisions. This is a checklist, not a new
execution receipt or permission to remove legacy code.

The [migration inventory](../../../migration/inventory.json) has **840 rows:
1 `core+vectors`, 839 `todo`**. It preserves all 839 original units and adds the
separated private record helper as an explicit TODO. Only the legacy
global-contracts canonical module is closed. Source and test links establish
where implementation and coverage
live, not that those tests passed on the current tree. An unchecked item means
this audit does not establish its complete current acceptance, not that all of
its implementation is missing.

The original DMG/Setup/Launch/native approval/settings/conversation flow remains
the product requirement. The native Harness component is still an opt-in
candidate: its complete original operation corpus, production adoption
and final GUI/model/provider/API evidence are incomplete. Test-only Profile
capture, a component build, mechanism replay or provisional inventory target
does not close these requirements. Keep legacy removal and final acceptance
blocked by the actual full completion gate, without waiving that gate.

The [original Authority comparison at `2cb833cf8`](https://github.com/harupipipipi/tobkiri/actions/runs/41237506970)
passed the finite native operation on macOS: 77 material cases and 16 snapshot
cases matched the independently pinned original, including typed failures,
logical database state, existing keys and physical cleanup. Snapshot canonical
processing runs before the original Store scope closes. Linux passed the
original 93-case reference and its explicit native-owner refusal; it does not
provide native database parity. This result does not yet connect the operation
to the original Setup review, establish complete IO-failure parity, or close
an acceptance item. The candidate remains default-off.

The [publication-prelude comparison at `54c6446c9`](https://github.com/harupipipipi/tobkiri/actions/runs/41247998757)
also passed: macOS matched 48 fresh original cases for journal/lock/read,
strict JSON, existing signing keys, legacy HMAC and predecessor validation,
including independently inspected file effects, key retention and actual
cleanup/OS-lock release. Linux passed the original reference; both platforms
passed strict library validation and locked dependency resolution. The source
connects a finite native ReviewListing caller, but verified-pointer/catalog
continuations are explicitly unbound. Application-denial types in this
comparison use a finite CI factory binding. Product factory/GUI integration,
full recovery and listing remain unqualified; no acceptance item is closed.

The [same-source Launcher distribution CI41248377655](https://github.com/harupipipipi/tobkiri/actions/runs/41248377655)
completed successfully: 638 library tests (all 12 finite caller cases confirmed
in individual logs), 123 build-script and 18 resource tests, sealed packaging,
cold boot, DMG creation and mounted verification. The 280,138,053-byte installer
artifact was inspected through metadata only. Product Python and PackVM remain;
this is not full native GUI or model/provider acceptance.

The [recovery slice at `8b8001962`](https://github.com/harupipipipi/tobkiri/actions/runs/41252586559)
keeps the same journal guard through actual current
pointer/snapshot verification and stale-intent discard/retention, ending before
the bootstrap-identity helper. Its independent v2 comparison retains the original
48 inputs and adds 29, including an intent installed between the outer and inner
recovery calls. All 77 fresh source/native cases matched on macOS, including
typed failures, independently inspected file effects, existing keys and actual
close/OS-lock release/capacity release/owner reopen. Linux passed the original
77-case reference; both platforms passed strict Clippy and locked resolution.
The [same-source Authority regression](https://github.com/harupipipipi/tobkiri/actions/runs/41252586573)
also passed its existing 93-case comparison.
[Desktop CI41253253796](https://github.com/harupipipipi/tobkiri/actions/runs/41253253796)
compiled the actual caller and passed the existing 638 library tests, including
all 12 previous finite cases. The three new pointer/snapshot caller cases failed
with a 30-second channel-receive timeout. Sol traced the test Source's bypassed
owner check; the isolated test-only fix at `bc3e84aa0` passed all641 library tests,
including all15 finite caller cases,123 build-script/18 resource tests and the
sealed cold-boot/DMG/mounted checks in CI41256412808. The failed8b run remains
recorded; product code, timeouts and limits were not changed by this test fix.
Bootstrap-identity getter/factory/canonical semantics, catalog and subsequent
recovery/listing, product adoption and the original GUI remain unqualified.

The next finite denial-factory implementation connects the existing import,
factory and limited-description exports on the same Store. Its original eight
reference cases passed, including the application's actual denial classifier.
Five transport and four worker/funding regressions plus an independent actual
component driver were prepared. The [actual factory comparison at `178c6341b`](https://github.com/harupipipipi/tobkiri/actions/runs/41260909785)
passed all eight same-Store cases and physical finish/data release. The existing
allocator custody record is collected through its real uniqueness-checked reap;
charged bytes and reservations both reach zero, with source destruction and
owner reopen confirmed. The new four Tauri cases and all19 finite caller cases
passed in [Desktop CI41262201810](https://github.com/harupipipipi/tobkiri/actions/runs/41262201810):
645 library,123 build-script and18 resource tests, sealed packaging, cold boot,
DMG creation and mounted verification all succeeded. The280,225,007-byte
artifact was metadata-only; distribution still contains product Python/PackVM.
The Host's
cause/context remain separate from the Guest exception's observed fields.
This does not yet connect async recovery or qualify the product factory/GUI.

[Unicode primitive CI41264808102](https://github.com/harupipipipi/tobkiri/actions/runs/41264808102)
at `821231531` passed on Linux and macOS:11 regressions,36,482 official NFC
sequences,32 context cases, all1,114,112 scalar NFC/lower observations and
128-codepoint-block repr over the full domain, plus strict library validation.
Fresh pinned CPython3.13.16/Unicode15.1 generation matched the committed static
data. These modules remain test-only; actual invocation scratch/work funding,
canonical-validation integration and recovery are not qualified by this run.
[Original identity CI41265632295](https://github.com/harupipipipi/tobkiri/actions/runs/41265632295)
separately passed69 controlled raw-getter inputs through the unchanged original
validator/factory/Host raise on both platforms. This is an original reference,
without native comparison, publication guard or product adoption.

The App canonical adapter at `835ed9818` subsequently passed8 unit tests and
all58 returned-getter comparisons in
[CI41266515519](https://github.com/harupipipipi/tobkiri/actions/runs/41266515519)
on both platforms. It preserves exact ProtocolError messages and input identity;
the11 raw getter errors are outside this adapter.
[Actual component CI41266520979](https://github.com/harupipipipi/tobkiri/actions/runs/41266520979)
also passed137 Harness tests and actual construction, then observed the unmodified
default getter on one real Store: fuel decreased, the original deadline and
scope were preserved, and actual finish/data release/reap left0 bytes and0
reservations with source destruction and owner reopen. The same-guard recovery
continuation is implemented at `c6d700124` and its two actual default-getter
operations (valid and candidate mismatch) passed in
[CI41275395353](https://github.com/harupipipipi/tobkiri/actions/runs/41275395353).
Both preserve the original lock through getter/validation/factory/comparison,
match original state/file/key observations, and prove actual lock release,
Source destruction and0 bytes/0 reservations after DATA release and reap. This
does not qualify controlled69/native147, the catalog/Authority bodies or GUI adoption.
The26 finite Launcher caller cases passed in
[Desktop CI41267778512](https://github.com/harupipipipi/tobkiri/actions/runs/41267778512),
with652 library,123 build-script and18 resource tests, packaging, cold boot and DMG
checks. The9 later cases also passed, with all35 source-matched finite cases,
661 library/123 build-script/18 resource tests and packaging/coldboot/DMG in
[Desktop CI41279048043](https://github.com/harupipipipi/tobkiri/actions/runs/41279048043)
at `7cdb66411`. This remains a candidate CI result, without complete native GUI adoption.
[Original continuous-boundary CI41266973919](https://github.com/harupipipipi/tobkiri/actions/runs/41266973919)
passed147 original cases at `cf4acd29f`:77 previous inputs,69 controlled getters
and one candidate mismatch. It reaches the original catalog call entry after
workspace Path/runtime preparation, without entering catalog/Authority bodies.
This reference does not establish native continuation, product adoption or any
new completed acceptance item.

The next original reference at `a0971c4b5` passed9 catalog-prefix resource fixtures
on both platforms in [CI41282428075](https://github.com/harupipipipi/tobkiri/actions/runs/41282428075).
It executes unchanged original getters/catalog to a constructor-entry sentinel,
with no ProfileDefinitionStore body or outer Authority. Native implementation and
comparison of that larger boundary are still pending; fixture artifact directories
do not establish actual installed executable or GUI qualification. The same-guard
Rust catalog adapter, fixed same-Store manifest parse and independent actual-component
comparison driver are now implemented in source; their compilation and execution
were exercised in [CI41322731594](https://github.com/harupipipipi/tobkiri/actions/runs/41322731594).
All7 new native mechanism tests and existing init/metadata/preparation/policy/owner
checks passed, but the actual catalog call returned `unsupported-value` at slot7.
State/key immutability, actual cleanup, Source destruction and0-byte/0-reservation
reap were observed; catalog completion remains unqualified. The bounded phase/reason
diagnostic at `ec1736d87` subsequently identified an outside-fixed-method refusal
during the original call in [CI41326572035](https://github.com/harupipipipi/tobkiri/actions/runs/41326572035).
The original captured loader execution and owner identity are restored in source at
`b61ebc7e0`, with six independent loader integration cases and finite builtin tags.
Its current component build, all154 Harness tests and existing native init/metadata/
preparation/policy/owner checks passed in
[CI41330673707](https://github.com/harupipipipi/tobkiri/actions/runs/41330673707).
The actual catalog call still failed with the bounded tag `outside_builtin=unknown`;
its exact underlying failure remains under investigation. Cleanup, state/key,
Source destruction and zero-capacity reap passed independently. The source fix
does not establish catalog success or whole-closure compatibility.
Actual-component constructor/migration bodies and outer Authority remain unqualified.
The next fixed diagnostic cohort at `4ef660dae` passed156 Harness tests, the current
component build and existing native checks in
[CI41333994269](https://github.com/harupipipipi/tobkiri/actions/runs/41333994269).
The actual catalog failure is now an exact `ModuleNotFoundError`, with inspection
counts7→10 and the same RootSymlink reachpoint. Source investigation is examining
the captured service's transitive Host-only imports; no dependency or IO fallback
is enabled by these diagnostics. Cleanup/reap still passed independently.
The next candidate implements a catalog-only fixed dependency view for the real
selected service's two unused Host-persistence imports. The original service,
initializer and catalog source bytes remain intact; whole DataFacade execution
remains disabled. Unconnected operations refuse, and the translated exception
has a distinct private identity without method-error/cause qualification.
The actual installation return and fixed class objects are borrowed after real
seal through private bootstrap parameters; name/hash/duck attributes cannot select
an owner. Independent Source review closed an initial responder-forgery defect.
Thirteen new Source tests include normal/failure catalog paths and owner/cleanup/
drift refusals. Compilation, runtime and resource limits await CI. This qualifies
only bodies under an explicit dependency replacement if verified, rather than
the original import environment, initialization effects or real persistence.
At `943a4b102`, [CI41342226932](https://github.com/harupipipipi/tobkiri/actions/runs/41342226932)
passed169 Harness source tests, the frozen16 checkpoint, the current component
build and existing native checks. Actual catalog exhausted the original2B Wasm
fuel and trapped after26.33 seconds; it did not reach constructor entry. Finish,
Source destruction, OS-lock release and reap0/0 were observed. The early-trap
driver did not capture its existing cleanup probe, so `cleanup_verified=false`
remains unqualified rather than being inferred from those separate observations.
Fuel profiling and early cleanup custody are the next work; limits remain unchanged.

At `3e1179917`, [CI41349560999](https://github.com/harupipipipi/tobkiri/actions/runs/41349560999)
passed171 Harness tests and built/compiled the current component. Five negative
catalog cases independently matched original error graphs/parser fields/files/key
and budgets. Four positive cases still exhausted2B fuel after the first pack read;
entry into the first schema check or cold42 loop remains unproved. Actual cleanup,
Source destruction, FD release, owner reopen and reap0/0 were verified for all9;
maximum peak was56,254,129 bytes within64MiB. All-nine agreement remains5/9.
The strengthened policy test mistook a pre-Guest admission refusal for retirement;
test-only fix `7f7685cb7` preserves that refusal and separately exercises a hostile
READY export under the same account. Its fresh execution is pending. No product
route or limit changed, and no whole-catalog/GUI/constructor qualification follows.

The independent original constructor/list reference subsequently passed13 controlled
cases on each OS in [CI41323891367](https://github.com/harupipipipi/tobkiri/actions/runs/41323891367):
4 positive migration-entry boundaries and9 invalid-state/file cases, with the actual
definition lock released while the journal stayed held. Only the two first-migration
entries use sentinels; no migration body or native/factory qualification is claimed.
The subsequent native direct comparison at `89e20ab26` ran all13 fixtures in
[CI41329148126](https://github.com/harupipipipi/tobkiri/actions/runs/41329148126).
Portable mechanism6 tests and strict Clippy passed on both OSes. Native macOS matched7
cases and failed6 at the unchanged64MiB capacity gate, because the old per-byte
allocation reservation exceeds87MB for a real approximately190KB Profile index.
All13 cleanup, zero-capacity reap and physical-owner reopen checks passed, but this
does not qualify the failed cases. A private allocation-bound correction now covers
partial parsing, actual Vec growth, fixed model insertions and diagnostic scratch,
with four new focused cases and strengthened existing model checks.
Old strict decoder/catalog reservations and original expectations are unchanged.
At `4e5fb0bb2`, [CI41332712410](https://github.com/harupipipipi/tobkiri/actions/runs/41332712410)
passed original13, focused4, normal6 and strict Clippy on both OSes. Native macOS
matched all13 cases and the independent comparison passed13/13. All cleanup,
zero-byte/zero-reservation reap and physical-owner reopen checks passed, with
48,532,334-byte maximum peak below64MiB. Selected JSON277,315 bytes was retained
from verified RAM-only archives. The previous failure remains historical evidence.
Actual AppHandle/Wasm, exception-factory and async-Join parity remain separate.
Fresh Authority93, publication77 and cancellation/same-invocation regressions at the
same source passed in [Authority CI41329148085](https://github.com/harupipipipi/tobkiri/actions/runs/41329148085),
[publication CI41329148081](https://github.com/harupipipipi/tobkiri/actions/runs/41329148081) and
[handoff CI41329148083](https://github.com/harupipipipi/tobkiri/actions/runs/41329148083).
The next [original recovery-error reference CI41333706776](https://github.com/harupipipipi/tobkiri/actions/runs/41333706776)
passed10 cases on both OSes at `257a45d40`: controlled actual original cause classes,
unchanged recovery catch, actual denied factory and journal finally. Native-origin
class projection plus actual same-Store Guest denied/Host-edge comparison is being
implemented separately; native custom causes do not require Guest class regeneration.
At `4ce888ec7`, [CI41335501380](https://github.com/harupipipipi/tobkiri/actions/runs/41335501380)
passed the updated original10, native portable6, all11 graph cases and strict Clippy
on both OSes. An actual-component eight-case driver and independent comparer now
cover six real factory calls and two raw-registry cases in source, with the original
budget, separate Host cause edges and unchanged returned intrinsics.
[Actual CI41336707940](https://github.com/harupipipipi/tobkiri/actions/runs/41336707940)
at `aa017d720` completed successfully: old factory8, controlled recovery8 and both
independent comparisons. Six fresh references share one Store, inspections stay7→7,
fuel decreases1,441,118,112→1,400,550,752 and the original deadline is preserved.
Actual finish/DATA release/reap0/0, Source destruction and owner reopen passed.
Selected JSON72,864 bytes was retained from a verified RAM-only archive; the
9,910,225-byte component remained metadata-only. Same-source portable6/graph11/
strict Clippy also passed on both OSes in CI41336708015.
The two controlled builtin-timeout inputs remain explicitly excluded;
actual native IO origins, factory throws, catalog and GUI adoption are separate gaps.

Saved Project parity source at `eb5dabff0` restores existing owner-based membership
and exact mutation acknowledgements while preserving the conversation execution
workspace and inert bindings. Frontend59 tests, type/boundary checks and actual UI
build passed in [CI41277008561](https://github.com/harupipipipi/tobkiri/actions/runs/41277008561).
Its generation job also verified canonical Pack/executable/Profile/bundle artifacts
and strict integrity. The38 generated text files were imported with source/hash/path
checks. Initial backend attempts stopped on stale then dirty source. After preserving
the original workspace-replacement PermissionError, clean snapshot `7cdb66411`
passed frontend59 and backend341 on each OS in
[CI41279045948](https://github.com/harupipipipi/tobkiri/actions/runs/41279045948).
The generated source also reproduced with zero differences in
[CI41277582684](https://github.com/harupipipipi/tobkiri/actions/runs/41277582684).
Foreign-helper captured-code ownership and detached DATA subsequently passed
real-loader, old-owner ABI and saved-owner regressions in
[CI41283852793](https://github.com/harupipipipi/tobkiri/actions/runs/41283852793):
440 tests on each OS, including16 new capture cases, plus frontend59/type/boundary
checks. Legacy global capture retire and comprehensive funding remain open;
these results do not establish full GUI or native adoption.

The active saved-history200 guards were then removed at `c2037a10f`, preserving
the whole durable graph, branch selection and existing input/read budgets.
After the CI-generated protocol source hash was imported at `02dcbdf25`,
[CI41288040466](https://github.com/harupipipipi/tobkiri/actions/runs/41288040466)
passed483 backend tests on each OS (the existing440 plus43 new history cases),
frontend59 and type/boundary checks. Tests exercise actual saved Host/Guest/Store
and continuation exchange with finite model adapters, including201+ history,
next sends, unselected branches, duplicate/lost acknowledgements, intermediate
read shapes and input limits. Graphs exceeding the existing512KiB full-ID read
budget still require a later owner-lookup migration; this is not whole-graph,
native Wasm, real-provider or full-GUI qualification.

Historical focused checks from the 2026-10-18 slice: foundation gate **0 failures**; complete gate
**1,044 failures**. Gate regressions **13**, CI-helper regressions **7**, and
inventory tests **9** passed. That snapshot reported 837 units and
`deletion_ready: false`. The Pack-specific fixture assertion was moved out of
host mechanism tests, and inventory/deletion validation now rejects stale
completed-source digests, omitted current units, and escaping evidence/fixtures.
It retains historical TODO evidence and evidenced rows after source removal.
These results do not constitute Rust, native UI, or migration acceptance.
The earlier [Audit/Census checkpoint 1041](rust-progress.md#applied-auditcensus-checkpoint-1041)
is historical, source-bound evidence, not a current full-workspace result.

## Credential transport source audit, 2026-10-23

The [credential transport source audit](credential-transport-parity.md)
records original enrollment, policy-witness and lifetime-attempt obligations
that remain open in the native path. Execution accounting alone does not
establish these existing credential semantics. No acceptance item is closed by
that source audit.

## Bounded Python-on-Wasm checkpoint, 2026-10-20

The [finalization trial](../../../tools/python313-producer/trials/2026-10-20-finalization/)
preserves actual current-source component builds, 33-case replays for each
ordered bridge version, and a ten-case five-component development composition.
The separate [current caller trial](../../../tools/python313-producer/trials/2026-10-20-current-caller/)
preserves the thirty-case original-Python caller/bridge replay. Declared
invalid/nonfinite transport boundaries and exclusions remain explicit; these
are not full legacy HTTP-error, retry or billing parity claims.

The [two-Pack static trial](../../../tools/python313-producer/trials/2026-10-20-native-caller-static/)
then validates the two separate actual components through production WIT
inspection and Profile compilation. It also retains the failed split-WIT
attempt and its reversible packaging correction. It opens no Core or Session
and executes no guest. The [native replay route](../../../tools/python313-producer/TWO_PACK_NATIVE_CHECK.md)
is a separate requirement, not a result implied by static validation.

At source checkpoint `fb8f87b0ab9e277f42e91a63687a11051478755b`, the foundation
gate remains at zero failures and the complete gate remains at **1,044**:
208 source findings, 836 unresolved inventory entries, and zero dependency
findings. No inventory unit or acceptance item is closed by these trials.

Two-Pack component source is 35,798,851 bytes. The five separate finalization
components total 89,513,931 bytes and exceed the unchanged 64 MiB compilation
catalog limit. A successful development composition is not proof that this
five-Pack set can be admitted by Core. Five-Pack native Core/Gate replay, deterministic
in-flight guest cancellation, production registration/activation, complete
Gateway migration and Host retirement remain open. The shutdown-admission
negative test is deliberately not described as in-flight cancellation.

## Additional two-Pack native checkpoint, 2026-10-21

The [native caller trial](../../../tools/python313-producer/trials/2026-10-21-native-caller/)
now records an actual arm64 macOS 15.6 / Darwin 24.6.0 / local APFS Core/Gate
replay of the separate caller and bridge Packs. All 30 cases matched; removing
only the caller's bridge permission prevented provider dispatch, and a Session
shutdown request rejected a subsequent call before dispatch. All created
Sessions closed successfully. The archived execution patch and supervision
bind this result to its exact source tree, not to arbitrary later changes.

Both guests reported CPython 3.13.16; the frozen source oracle records CPython
3.13.5. The receipt labels these independently. Its Host and data root are test
fixtures, so this does not accept the Launcher GUI approval path or production
Gate wiring. The unchanged five-Pack size limit, in-flight cancellation,
production activation, full original UI functionality and final retirement
obligations above remain open. No inventory unit or checklist item is closed.

## Coherent original five-Pack native checkpoint, 2026-10-25

The [coherent producer job](https://github.com/harupipipipi/tobkiri/actions/runs/40131893888/job/120985161408)
rebuilt all five original Python Packs with the same authenticated CLI from
their unchanged source plans. This restores the same-Core Python baseline
that was unavailable at the earlier checkpoint. Exact extraction and sharing
produced thirteen distinct code modules; complete catalog source, including
ordinary noncomponent files, is **62,877,304 bytes**. The 32 MiB Pack and
64 MiB catalog limits remain unchanged. The preceding mixed old/new component
measurement exceeded the catalog limit and is retained as a failed capacity
gate, despite that measurement job itself completing successfully.

The [actual five-Pack Core/Gate replay](https://github.com/harupipipipi/tobkiri/actions/runs/40132836084/job/120988037902)
attempted all **215** original cases: 94 provider, 81 bridge, 30 caller and
ten finalizer observations. All five runtime probes reported actual CPython
3.13.16 / Unicode 15.1.0, and Session close succeeded. The replay **failed with
six differences**:

- The same three original oversized provider inputs are rejected at the outer
  CoreStrictV1 argument boundary before guest dispatch. Their original direct
  adapter expectations remain unchanged and are not counted as matched guest
  outcomes. Separate reporting of these entrance rejections awaits the user's
  acceptance decision; the strict replay remains failed.
- Two in-budget stream inputs that expand past output byte/node limits trap
  before returning the expected typed provider error. A subsequent typed
  diagnostic run reports `fuel-exhausted` for both, with about 290 seconds of
  the existing deadline remaining. The diagnostic's allocation-context
  precedence regression failed separately and is being corrected; the next
  run must revalidate classification with that correction in place.
- A finalizer stream case returns the exact original output, but its dependency
  audit includes an extra stream initialization after the preceding traps.
  Its original no-extra-initialization assertion remains failed.

The [following native job](https://github.com/harupipipipi/tobkiri/actions/runs/40133079330/job/120988778146)
passed all three shared-code integrity tests and the corrected durable ACK
regression, then reproduced the same six replay differences. The new
[typed-diagnostic job](https://github.com/harupipipipi/tobkiri/actions/runs/40134248100/job/120992269152)
completed with the same six replay differences. One diagnostic unit test
exposed a real classification bug: traversing standard error sources misses
`anyhow`'s typed context, so a contextual allocation limit lost precedence to
its fuel-trap cause. The correction checks borrowed context-aware downcasts
before retaining the existing standard-source fallback. No private cause is
formatted and execution/error-code policy is unchanged. The new CI route also
builds the five existing Rust candidates and compares them with the unchanged
215 source observations on the same Core and resource limits, retaining its
strict mismatch failures and separate outer-transport evidence. That corrected
diagnostic, current supervisor regressions and Rust comparison remain pending.
These are fixture results, not production activation, GUI/settings/chat parity,
full Gateway migration or VM retirement. No inventory unit or acceptance item
is closed by this checkpoint.

The [same-Core Python/Rust comparison](https://github.com/harupipipipi/tobkiri/actions/runs/40135550828/job/120996193030)
then completed at `6005720ca79273c38582b642667a7b0b70b9d269`. Its actual five
Rust components total 1,385,842 bytes. Both implementations attempted all 215
unchanged observations with the same Core and limits. Python retained six
differences; Rust matched **212**, retaining only the same three outer-input
limit mismatches. Both stream output-limit cases and the finalizer dependency
audit matched under Rust. All five Rust runtime/initialization observations
matched and both Sessions closed. The strict replays remain **failed**: no
expectation was changed or oversized case silently counted as guest parity.
Separate outer transport admission passed all six synthetic boundaries and
three pinned original oversized inputs. Shared-code integrity (three tests),
typed diagnostics (two), durable ACK (one) and concurrent-ACK/corruption
interleavings (two) also passed on the actual supported native platform.
This does not establish production registration or full original GUI parity.

The [current complete native workspace](https://github.com/harupipipipi/tobkiri/actions/runs/40135590037/job/120996494980)
at `278eeb0c13efabe2f81b1caa4c58371a8da86370` failed only the Core Audit round
trip: **238/239** Core library cases passed. That original five-second case
passed in isolation, as did all six independent native observations, strict
workspace lint and the original/transformed Python replays. Its failed Store
execution segments took approximately 1.92 seconds for append and 2.86 seconds
for read; the latter reached the original deadline before return conversion.
Those segments include budget admission and allocation work as well as SQL,
so they do not establish a SQL bottleneck. Debug-only, opt-in synchronous
allocation/admission timing is being added to distinguish these costs without
relaxing validation, funding or deadlines. Its integration and new helper
regressions remain pending. The complete portable foundation and strict lint
passed at that head. No stability or full-workspace acceptance is inferred
from the successful isolated case.

At `f38fa7f445b682066ea56907d54bcaf6beef88f9`, the
[PR-merge native job](https://github.com/harupipipipi/tobkiri/actions/runs/40138452640/job/121005060540)
passed the complete workspace (Core 239/239), all six native observations,
strict lint and all original/transformed Python replays. Both new synchronous
timing-helper regressions passed. Its isolated Audit observation recorded
1,844 append reservations (1,429 with zero bytes) and 3,469 read reservations
(2,734 with zero bytes). Frame-admission time was 976,367 microseconds of the
985,666-microsecond append Store segment and 1,941,632 microseconds of the
1,955,752-microsecond read segment. Admission is included in reservation time;
these measurements must not be added together. They identify repeated actual
funding/authority checks as the dominant measured cost, without establishing
which admission sub-operation is responsible or a repair of earlier deadline
failures.

The Gate compiler candidate retains every validation, debug assertion,
overflow check and original deadline. Its bounded early observations are now
available: the [709 native run](https://github.com/harupipipipi/tobkiri/actions/runs/40141017650/job/121012789387)
still exceeded the five-second complete-workspace Audit deadline (5,008,166
microseconds); the isolated invocation passed in 3,533,679 microseconds. The
source-equivalent [96 native run](https://github.com/harupipipipi/tobkiri/actions/runs/40141506723/job/121014223086)
passed that workspace step and observed 2,753,715 microseconds in isolation.
Both observed the same 1,844/3,469 reservations. This variation does not
establish a repeatable improvement or a causal repair. The complete native
jobs are still running their later checks.

The [879 foundation check](https://github.com/harupipipipi/tobkiri/actions/runs/40142696143/job/121017562535)
rejected a string-copy source optimization because the historical Gateway
observations bind the original canonical codec source hash. That guard remains
intact: the candidate codec changes have been reverted to the exact original
source; no historical observation, source pin or expected value is rewritten.
The next compiler candidate instead optimizes the unchanged canonical codec
for workspace dev/test builds, retaining debug assertions and overflow checks.
The Gate checks, permission reads, representation rules and deadlines remain
unchanged. This does not establish that codec work is the dominant admission
sub-operation or that the performance issue has been repaired.

Two additional regressions cover all ASCII control escapes, mixed Unicode and
escaped runs, invalid/truncated strings and escaped duplicate keys across all
three codecs. The existing Python byte/digest/seal corpora remain the acceptance
oracle. All 21 preserved Gateway transport/fixture tests pass locally after
restoring the original codec source; the source-binding guards are unchanged.
Actual Rust regressions and native performance remain pending CI. No
local Rust/guest build or model server was started. The bounded early artifact
makes no pass or authority claim; production GUI/Setup and all acceptance items
remain open.

At `654788a011a40cef4f1817b5a5e2bd8887f8e3f5`, the
[complete portable foundation and strict lint](https://github.com/harupipipipi/tobkiri/actions/runs/40143004093/job/121018475162)
passed, including both new string regressions, all three original codec corpora
and seal/lease-token checks. Its bounded native observation recorded an
isolated Audit round trip of 1,316,906 microseconds, with the same 1,844 append
and 3,469 read reservations and unchanged authority checks/deadlines. Admission
times were 420,994 and 775,433 microseconds respectively. The complete workspace
step and all six isolated native observations passed; later native replays are
still running. These observations do not establish permanent timing stability.
The strict five-Pack comparison still reports Python 209/215 and Rust 212/215,
retaining the same mismatches; nine transport admission vectors and every
Session close passed. No expectation or source pin was rewritten.

## Profile commit uncertainty and original Setup, 2026-10-25

The [Setup boundary note](native-setup-boundary.md) now distinguishes the real
Setup configuration confirmation from later individual native approvals. No
blanket first-Launch review or privileged Pack configuration bridge is added.
Core's existing failed-save path can retain earlier approval effects; an old
head does not prove whole-addition rollback. R2.8 remains unfulfilled in that
respect, without inferring an immediate execution bypass from retained rows.

Profile CAS errors now preserve whether epoch COMMIT was attempted, the epoch
advanced, or the real head rename occurred before durability confirmation
failed. This typed observation survives the Core facade and native error
mapping as `profile-commit-uncertain`; it does not claim a current head, a
committed activation, rollback, or permission to retry. Its displayed error
omits free-form source text. Update order, fresh checks and permission decisions
are unchanged. Two real-SQLite commit-boundary regressions, the actual
post-rename fsync regression, two Core error propagation/privacy regressions,
and the actual Store-to-Core-to-native adapter regressions await CI. Formatting,
foundation boundary checks and the 21 preserved Gateway
transport/fixture tests pass locally. Full native GUI configuration integration,
whole-addition rollback and all broader acceptance items remain open.

## Native Rust HostIo cancellation checkpoint, 2026-10-21

A separate [focused receipt](../../../crates/tobkiri-core/evidence/host-read-cancellation-20261021.json)
records a real missing terminal: after a started Wasm `terminal-read-line`,
Session close dropped the read but left its dispatched HostIo grant without an
outcome. The generation now retains an owner for that actual read and original
grant. The failed export releases its Store and instance lock before awaiting
only the same operation's marked read completions.

The corrected focused test passed, then all 32 provider-proxy regressions
passed. An additional real SQLite outcome-write fault test passed: the caller
cannot claim success, the original cancelled receipt remains unacknowledged,
and flushing the outbox after fault removal delivers that same receipt once.
Both bounded Session close and the pending caller join are observed. This does
not prove a standalone close joins every plain call, Python guest in-flight
cancellation, native GUI Stop/Quit, or full Gateway cleanup. Those broader
requirements and every unchecked acceptance item remain open.

## Rust provider and terminal-owner checkpoint, 2026-10-22

These are local candidate trials. Production registration, the complete original
Launcher/Setup/GUI approval/settings/chat path, and VM retirement remain open.
The original user-facing flow has not been replaced with the migration fixtures.

The independent original-Python oracles in
[`python_provider_compat`](../../../tools/quality/python_provider_compat/) and
[`python_tool_bridge_compat`](../../../tools/quality/python_tool_bridge_compat/)
ran under CPython 3.13.9 / Unicode 15.1.0. Native Rust replays matched all 175
observations: 41 usage-cost, 53 stream-normalizer and 81 bridge-v2 cases. Scalar,
strict codec and opaque-JSON tests also passed. The frozen CPython 3.13.5 and
bridge-v1 expectations remain separate, unchanged references.

Five Rust Wasm candidates built successfully, totaling 1,386,704 component
bytes under the unchanged 64 MiB catalog limit. An actual five-Pack Core/Gate
fixture attempted all 185 cases. All 81 bridge and ten finalizer observations,
and 91 of 94 provider observations, matched. All five runtime identities reported
Rust and the Session closed successfully. This includes actual typed imports;
native finalizer dependency-unavailable results are not counted as guest parity.
The fixture uses scripted native approvals and does not establish GUI acceptance.
A fresh current-source five-component build and replay subsequently attempted
all 215 cases, adding the unchanged thirty-case caller corpus. All thirty caller
cases, 81 bridge cases, ten finalizer cases, and 91 provider cases matched:
**212 matches, three mismatches**, with successful Session close. Every actual
runtime reported Rust. The replay remains failed, not full guest parity.

The three remaining cases were rejected while Core checked the outer StrictV1
argument record. Their complete arguments are 4,194,369, 4,194,337 and 4,194,363
bytes, exceeding the existing 4,194,304-byte limit by 65, 33 and 59 bytes. Their
expected direct-adapter errors are therefore unobserved through this Core path.
Every attempt and mismatch is retained; no case or expected value was removed,
and the replay remains failed. The harness's unavailable argument digest is its
own digest calculation, not Core-internal instrumentation. Old Python components
and their pinned producer inputs are not presently available for the same-Core
baseline, so these observations do not establish absence of a migration regression.

A separate actual two-component admission test passed all nine observations:
for each provider, independently encoded outer records at 4 MiB minus one byte,
exactly 4 MiB, and 4 MiB plus one byte, followed by the same three pinned original
oversized inputs. Accepted arguments reached a User dispatch and terminal outcome
for the same Grant; guest success is a separate observation. Oversized arguments
returned the exact argument-verification limit error without an audit delta.
Source-bound validation order establishes that rejection precedes guest dispatch;
an empty audit alone is not used as its proof. This transport result does not
resolve or rewrite any of the three original replay mismatches.

The separate
[`finalizer-normalization observer`](../../../tools/quality/python_finalizer_normalize_compat/)
ran 24 original `_normalize_result` inputs under the explicitly recorded 3.13.9
runtime. It preserves dictionary key types and order, and records ordered-DATA
serialization failures. Its native Rust consumer now passed all 24 string-key
projections and the non-string-key marker/native Terminal-versus-Unavailable
checks. This does not establish full numeric-key intermediate dictionary parity,
actual dependency-import timing, or Wasm acceptance for those added observations.

Terminal-owner validation passed all 165 Wasm library tests, including eighteen
metadata/reaper regressions. Funding precedes consume; the original physical
cleanup report and the original metadata Join are both required. Acknowledging
metadata cannot repair a panic, missing report or physical failure. Transient
audit-delivery failures and reported source failures retain their diagnostic
classification separately from physical completion.

The first full Core trial completed 219 of 222 tests successfully and exposed
three recovery errors. The corrected candidate passed those three in the next
trial, but that run was interrupted at the disk safety floor after other deadline,
cleanup-grace and uncertain-cleanup failures. Audit callbacks reached 6.47 seconds
and the 64-source audit total reached 224.8 seconds. The cause of this latency is
not established. Neither the deadlines nor the safety floor were increased.
After the user freed disk space, a resource-stable run passed all **222 Core
library tests** in 57.483 seconds, including the earlier recovery and callback
failures. Minimum free space was 20.479 GB; deadlines, cleanup grace periods and
resource limits were unchanged. The exact source then received one nonsemantic
`then`-to-`then_some` lint correction. Current Core/Flow/Store/Wasm/manifest and
six native candidates/five Wasip2 candidates passed strict Clippy; the expanded
Core replay/admission harness passed its own strict check.

The [portable checkpoint](../../../crates/tobkiri-e2e/evidence/rust-provider-translation-20261022.json)
records source and artifact hashes, actual receipt hashes, counts, mismatches,
and the distinct scopes above. No acceptance item or inventory unit is closed.

## Pure provider settings compiler checkpoint, 2026-10-22

The additive
[`provider-model-access Rust candidate`](../../../packs/local.provider-model-access-rust/README.md)
translates eight pure functions from the original `model_access.py` and
`native_filters.py`. The independent
[`source observer`](../../../tests/fixtures/issue1520/provider-model-access/README.md)
recorded 80 observations under CPython 3.13.9 / Unicode 15.1.0, with integer
conversion limited to 4,300 digits. All 79 finite inputs matched in native Rust
and again through the actual Wasm/Core route. The nonfinite NaN observation
remains labelled source-only; it was neither converted into a finite input nor
counted as a Core attempt.

The current component is 359,075 bytes. Each of the 79 Core attempts required
the same User Grant, request and effect arguments to reach `Outcome::Succeeded`;
Session close and evidence writes also succeeded. Comparisons retain exact ABI
bytes, source exceptions, recursive type/float-bit observations and key order.
The [separate portable checkpoint](../../../crates/tobkiri-e2e/evidence/provider-model-access-20261022.json)
binds the original observations, candidate source, build and actual results.
Native and Wasip2 strict Clippy passed for this candidate; the entire Rust root
workspace and all its targets also passed strict Clippy at the recorded source.
This static check is not a complete workspace test run.

The helper is unpromoted and owns no Registry, credentials, network transport,
approval state or GUI. Its finite observation boundary does not establish the
fuller Python domain with non-string dictionary keys or independent URL edge
coverage. Original Provider/Model Registry ownership, revision checks,
credential creation/revocation, scoped bindings and failure recovery still need
faithful integration. The complete original Setup, dedicated GUI approval,
API/model settings and chat flow remain the required acceptance path. This
checkpoint closes no inventory unit or acceptance item and does not resolve
the three oversized cases in the earlier five-component replay.

## Acceptance checklist

Status after the focused CI replays (2026-11-13): items 2, 3, 4, 6, 8, 9, 11,
12, 13 and 18 are checked (10 of 20). The aggregate
`issue-1520-rust.yml` currently fails earlier at an unrelated Python fixture
step, so it is not the evidence; the focused workflows
`issue-1520-acceptance-9-11-12.yml` and `issue-1520-acceptance-13-16.yml` are.
Open points: 11 lacks the 「未確認の接続」 UI label; 12 uses synthetic WIT; 13
has R2.10 gaps (no 「持ち主のいないデータ」 marking in Core removal, `policy-view`
over-rejection); 14 needs guest/runtime replay and existing-workspace migration;
15 needs physical Proc/PTY; 16 needs the populated vault and other storage tiers
covered in the same publication replay.

On the integration branch, [CI 43374564400](https://github.com/harupipipipi/tobkiri/actions/runs/43374564400)
(`fd78392dd`) passed four compiler tests, two Flow engine tests, one Store
prompt test and 17 actual Core tests, plus strict Clippy. These use verified
Wasm and data Packs with a recording trusted Host. They show that open-role
and User overlays execute through the Gate, denied or cancelled effects do not
continue to the original step, and conflicts keep the previous head. The
repair cases exercise a bound offer and explicit candidate application; they
do not execute an AI generator or verify the native approval window.
[CI 43374564506](https://github.com/harupipipipi/tobkiri/actions/runs/43374564506)
also reran the focused Profile/manifest/Flow checks for items 9, 11 and 12 on
that source.
[CI 43374564477](https://github.com/harupipipipi/tobkiri/actions/runs/43374564477)
passed all 19 focused authority, storage, compiler and Core checks for items
13–16, plus strict Clippy. This does not fill the physical runtime or CLI gaps
listed below.
[CI 43375008696](https://github.com/harupipipipi/tobkiri/actions/runs/43375008696)
passed four actual Wasm chat tests, one local SSE provider replay and two
Core UI port checks. The same physical input produced different outputs after a
reviewed Flow edit while retaining conversation history. The local API is a
test fixture; this run does not establish native WebView or llama.cpp 1B parity.

The R8.5 import fix passed 20 tests and strict Clippy. The actual Core tests
checked that narrowing a published ceiling also bounds each Pack's fresh
approval and rejects invalid original policies and mismatched pins.
[CI 43378298205](https://github.com/harupipipipi/tobkiri/actions/runs/43378298205)

1. [ ] **CI rejects Pack IDs and Pack-owned shape names in core and hosts.**
   [Checker](../../../tools/quality/issue_1520_gate.py),
   [negative fixtures](../../../tools/quality/test_issue_1520_gate.py), and
   [CI](../../../.github/workflows/issue-1520-rust.yml) exist. The focused
   foundation gate passes without weakening production checks; the final host
   scan still includes substantial legacy Pack knowledge.

2. [x] **Core-defined shapes are restricted to R1.2.**
   The committed WIT packages expose the same 17 generic interfaces.
   Five WIT tests and the exact-version manifest admission test passed.
   [WIT registry](../../../crates/tobkiri-wit/src/lib.rs)
   · [CI 43379294915](https://github.com/harupipipipi/tobkiri/actions/runs/43379294915)

3. [x] **A non-Defaults Pack set can chat.**
   The macOS run rebuilt and admitted the independent Echo Packs. The real
   input, Flow, provider and store produced two turns, and the second turn
   loaded the first after reopening Core. [Chat E2E](../../../crates/tobkiri-e2e/tests/chat.rs)
   · [CI 43375008696](https://github.com/harupipipipi/tobkiri/actions/runs/43375008696)

4. [x] **CLI alone creates, starts, publishes, and imports Profiles.**
   On macOS the real CLI created and ran an Echo Profile, published it, refused
   an import without committing its head, then imported with fresh approval.
   Both Profiles ran a real Flow and kept separate SQLite conversations.
   The imported Profile produced an identical re-export.
   [Replay](../../../tools/quality/issue_1520_cli_roundtrip.py)
   · [CI 43378931498](https://github.com/harupipipipi/tobkiri/actions/runs/43378931498)

5. [ ] **All input, including chat, traverses Flow; editing Flow changes behavior.**
   [Native input E2E](../../../crates/tobkiri-e2e/tests/native_input.rs),
   [chat E2E](../../../crates/tobkiri-e2e/tests/chat.rs), and
   [input forwarding tests](../../../crates/tobkiri-core/src/input_forwarding_guest_tests.rs)
   exercise the new entry path. Legacy fixed routes and old engines remain;
   native Send/save and all migrated input surfaces need current end-to-end proof.

6. [x] **Foreign Packs insert, replace, or delete by shape only where allowed.**
   The Core replay used verified Wasm and data Packs to run insert, wrap,
   replace and delete. It checked the Flow result and original-step dispatches,
   and refused closed targets, direct foreign step IDs and missing permissions.
   [Core tests](../../../crates/tobkiri-core/src/acceptance_overlay_tests.rs)
   · [CI 43378298202](https://github.com/harupipipipi/tobkiri/actions/runs/43378298202)

7. [ ] **Users can overlay any step.**
   [Compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs) include
   `user_overlay_can_override_private_closed_flow` and user-overlay witnesses.
   Complete current host/CLI configuration and execution coverage while retaining
   the separately approved User policy and original Pack authority for `next`.

8. [x] **Neither Pack nor user overlays bypass the Gate.**
   The actual Core replay checked separate User, Flow owner and overlay Pack
   approvals. Revoked approval, a changed epoch and cancellation blocked the
   run, with audit checks confirming the original step was never dispatched.
   [Core tests](../../../crates/tobkiri-core/src/acceptance_overlay_tests.rs)
   · [CI 43378298202](https://github.com/harupipipipi/tobkiri/actions/runs/43378298202)

9. [x] **Profiles without default loop bounds fail validation.**
   [Compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs) include
   `default_loop_bounds_and_explicit_error_policy_keys_are_required`;
   [engine tests](../../../crates/tobkiri-flow/tests/engine.rs) reject missing or
   widened limits. Replayed in [run 42939221536](https://github.com/harupipipipi/tobkiri/actions/runs/42939221536) (focused workflow
   `issue-1520-acceptance-9-11-12.yml`, commit `fea29488e`, shared-branch
   `8da910009..a08ce1da6`): `profile_without_default_loop_bounds_fails_parse_and_compile`,
   `flow_document_must_take_loop_bounds_from_the_profile_default`,
   `default_loop_bounds_and_explicit_error_policy_keys_are_required`,
   `loop_cannot_widen_defaults_or_run_without_profile_limits` and
   `iteration_cost_and_wall_limits_stop_run` all printed `ok`, 0 ignored.

10. [ ] **Ambiguous single ports/unordered overlays reject changes, preserve the
    old configuration, and offer a repair Pack.**
    [Compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs),
    [Core save/remove/inspect](../../../crates/tobkiri-core/src/core.rs), and
    [CLI repair](../../../crates/tobkiri-cli/src/commands.rs) implement pieces.
    Prove one integrated conflict-to-repair flow, unchanged head on failure,
    stale original-hash rejection, and no automatic permission expansion.

11. [x] **A port without WIT is rejected unless explicitly `untyped`.**
    [Manifest validation](../../../crates/tobkiri-manifest/src/package.rs) and
    [compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs) distinguish
    explicit untyped ports from typed required slots. Replayed in [run 42939221536](https://github.com/harupipipipi/tobkiri/actions/runs/42939221536):
    `typed_port_without_wit_shape_is_rejected_and_explicit_untyped_loads`,
    `optional_and_untyped_ports_cannot_satisfy_required_typed_slots` (compiler) and
    `explicit_untyped_ports_load_and_malformed_declarations_are_rejected`
    (manifest contracts), all `ok`. The acceptance sentence is about load-time
    rejection and is met. **Open gap (R3.5):** the UI label 「未確認の接続」 is not
    implemented anywhere, so the user-facing display of untyped ports is unproven.

12. [x] **Compatible minor versions connect; different majors do not.**
    [Manifest tests](../../../crates/tobkiri-manifest/tests/contracts.rs) cover
    trailing-option projection and enum directionality;
    [compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs) cover
    verified minor snapshots and major mismatch. Replayed in [run 42939221536](https://github.com/harupipipipi/tobkiri/actions/runs/42939221536):
    `minor_difference_connects_both_ways_and_major_difference_needs_an_adapter`,
    `compatible_minors_bind_from_verified_snapshots_unknown_minors_are_rejected`,
    `subflow_implements_ambiguity_and_major_mismatch_fail` (compiler),
    `compatible_trailing_options_project_both_directions` and
    `enum_minor_extension_is_input_only_and_width_changes_need_adapter`
    (contracts), all `ok`. Unknown minors are rejected, not assumed compatible.
    **Scope limit:** the fixtures use synthetic WIT, not real typed components.

13. [x] **Updates approve only added permissions; removal cannot break required ports.**
    [Permission diff](../../../crates/tobkiri-gate/src/permissions.rs),
    [update authority tests](../../../crates/tobkiri-store/tests/authority.rs), and
    [Core removal](../../../crates/tobkiri-core/src/core.rs). Replayed in [run 42941462516](https://github.com/harupipipipi/tobkiri/actions/runs/42941462516)
    (workflow `issue-1520-acceptance-13-16.yml`, commit `605397f71`, shared-branch
    `ab4fd131f..ba0027367`, success): gate `update_diff_contains_only_added_authority`
    and `four_way_intersection_cannot_add_an_origin_or_dangerous_permission`;
    store `a_pack_hash_update_prompts_only_for_additions_and_other_profiles_have_no_approval`;
    Core `pack_update_asks_only_for_permissions_added_since_the_previous_version`
    and `removing_a_provider_of_another_packs_required_port_fails_and_keeps_data`.
    The replay also exposed and fixed a product bug in `ab4fd131f`:
    `Core::remove_pack` now drops the removed Pack's own `pack_limits` entry;
    before, removing any Pack that had limits failed with `permission-widening`.
    **Open (R2.10, not part of this sentence):** Core removal does not mark the
    removed Pack's stores as 「持ち主のいないデータ」 (`orphan_pack` exists only
    at the store layer); removing the last provider of a shape referenced by the
    Profile ceiling fails with `policy-view` even when no required port breaks,
    which may be stricter than R2.10. See cleanup-notes 028.

14. [ ] **Three storage tiers are Profile-isolated; public writes approve once
    per writer Pack/store pair.**
    [Tiered Store](../../../crates/tobkiri-store/src/tiered.rs),
    [security tests](../../../crates/tobkiri-store/tests/security.rs), and
    [native quota tests](../../../crates/tobkiri-store/tests/native_quota.rs).
    [run 42941462516](https://github.com/harupipipipi/tobkiri/actions/runs/42941462516): store `public_write_approval_is_once_per_writer_and_store_and_reads_do_not_prompt`
    and `three_tiers_are_isolated_per_profile_and_public_approval_is_per_writer_store_pair`
    passed. The store-layer replay writes real key/value data in all three
    tiers. Wasm guest replay and migration of existing Profile workspaces remain.

15. [ ] **Native Packs and cross-Profile storage require explicit dangerous permission.**
    [Gate permissions](../../../crates/tobkiri-gate/src/permissions.rs),
    [process launch tests](../../../crates/tobkiri-store/tests/process_launch.rs),
    and [global storage tests](../../../crates/tobkiri-store/tests/authority.rs).
    [run 42941462516](https://github.com/harupipipipi/tobkiri/actions/runs/42941462516): store `dangerous_global_storage_can_share_data_only_after_explicit_permissions_and_prompt`,
    `native_and_cross_profile_storage_need_explicit_permission_and_warn_as_dangerous`,
    `client_approved_payload_and_dangerous_declaration_do_not_override_ceiling`;
    profile `user_declaration_is_optional_published_and_intersected_with_ceiling`;
    Core `native_pack_requires_the_profile_to_allow_native_and_is_prompted_as_dangerous`
    and `cross_profile_storage_is_only_asked_for_when_the_profile_ceiling_allows_it`
    passed. Left unchecked: the denial and prompt side is evidenced, but physical
    Proc/PTY is still [Unavailable](../../../crates/tobkiri-wasm/src/effects.rs),
    so an approved native Pack cannot actually run.

16. [ ] **Published Profiles exclude approvals, data, and secrets.**
    [Published allowlist](../../../crates/tobkiri-profile/src/lib.rs),
    [serialization tests](../../../crates/tobkiri-profile/tests/compiler.rs), and
    [fresh import approval tests](../../../crates/tobkiri-core/src/core_tests.rs).
    [run 42941462516](https://github.com/harupipipipi/tobkiri/actions/runs/42941462516): profile `published_allowlist_excludes_local_identity_and_rejects_authority_fields`,
    Core `published_profile_excludes_populated_approvals_data_secrets_and_user_global_storage`
    and `published_user_policy_import_requires_fresh_profile_scoped_approval` passed.
    The real CLI export/import replay also verifies populated private SQLite
    history stays with the source Profile. A publication replay covering an
    actual populated vault and all storage tiers remains.
    [CLI replay 43378931498](https://github.com/harupipipipi/tobkiri/actions/runs/43378931498)

17. [ ] **Pack UI is isolated and can access data only through gated ports.**
    [Core UI E2E](../../../crates/tobkiri-e2e/tests/ui.rs),
    [native probes](../../../packs/native-acceptance/verify-source.test.mjs), and
    [host integration](../../../tobkiri_launcher/src-tauri/src/core_host.rs)
    cover different layers. Actual WebView origin/IPC/navigation denial plus
    ordinary Send, persistence, and truthful Stop/Quit remain unaccepted; source
    checks, Node tests, and Core stream tests cannot substitute for that run.

18. [x] **At least one unchanged pi extension runs through an adapter Pack.**
    CI built both wrappers from pinned, unchanged sources and ran both
    through the actual adapter and Gate. The hello tool returned its greeting.
    Protected-paths produced five expected decisions and three gated warnings.
    The latter replay covers the guard decision and warning effects.
    [Pi E2E](../../../crates/tobkiri-e2e/tests/pi.rs)
    · [CI 43377714365](https://github.com/harupipipipi/tobkiri/actions/runs/43377714365)

19. [ ] **Python runtime and PackVM are removed; distributions contain no Python.**
    This is **blocked**, not closeable by changing inventory labels.
    [Legacy runtime](../../../tobkiri_runtime/core_runtime),
    [Launcher Python support](../../../tobkiri_launcher/src-tauri/src/python_env.rs),
    and legacy mechanisms remain. All 839 open rows need replacement parity or
    authorized retirement; then run the [deletion gate](../../../migration/inventory.py)
    and inspect actual distribution contents. Development-only compilers do not
    justify shipping a Python product runtime.

20. [ ] **Canonical/seal/digest match frozen Python vectors byte-for-byte.**
    [Vector tests](../../../crates/tobkiri-canon/tests/vectors.rs),
    [additional seal families](../../../crates/tobkiri-canon/tests/extra_seals.rs),
    and [canonical evidence](../../../crates/tobkiri-canon/evidence/global-contracts-canonical-20261015.json)
    cover the codec, seal and digest formats. All 12 tests passed in the current
    replay, including the test that records deliberate weak Ed25519 key
    rejection where Python accepted it. Remaining persisted recipes and
    signature differences need explicit mappings.
    [CI 43379294915](https://github.com/harupipipipi/tobkiri/actions/runs/43379294915)

## Next bounded slices

1. On one frozen current source tree, build required guest fixtures and run the
   deterministic compiler/Flow/Store/Core/CLI/E2E suites with actual test bodies,
   then strict workspace Clippy and the normal clean-candidate checks.
2. Complete native UI lifecycle and Pi tool/FS acceptance as separate observable
   runs. Proc/PTY needs an owned physical backend, not an availability flag flip.
3. Port legacy units in bounded, frozen-parity slices. Keep the final migration
   gate red and retain legacy sources until the full replacement evidence exists.

Mobile/P2P local-gate preservation, the three legacy mediator conversions, seven
published preset examples, and contradictory legacy-document cleanup remain
issue requirements even though they are not separate entries in its 20-item
acceptance list. A green checklist subset cannot silently waive those obligations.

## Original component sharing investigation, 2026-10-23

A bounded, read-only section/hash inventory of the preserved original Python
caller and bridge found 26 byte-identical embedded core modules totaling
6,865,307 bytes. The two complete components total 35,798,851 bytes. The
[inventory tool](../../../tools/quality/component_module_inventory.py) records
full module hashes and nesting locations, and CI retains its report alongside
the authenticated original graph replay. It checks framing, not Wasm validity,
transformation equivalence, admission or execution. Its extraction lower bound
does not include new module-type/import overhead and is not an acceptance claim.

The existing engine supports core-module linker imports, while the current
Host deliberately rejects non-interface imports. A generic shared-code path
would need exact declared dependency closure, authenticated transformed
artifacts, shared immutable bytes from the reading stage, prepaid cache custody,
and independent per-Pack instances, memory, tables, globals, WASI and Gate
contexts. Sharing an interpreter service Pack would change the CodeOwner.
The original 32 MiB per-component and 64 MiB catalog limits remain unchanged;
the five original components are still not admitted. The complete five-source
closure, transformation, loader and runtime parity remain open.


## Original-object schema candidate checkpoint, 2026-10-31

[Normal CI 41117640666](https://github.com/harupipipipi/tobkiri/actions/runs/41117640666)
at source `1cbd05f75` passed actual native compilation, all 64 schema comparisons
and 18 additional API groups. Its original one-shot Wasm call still exhausted
2B fuel, preserving the deadline. The separately identified progress diagnostic
returned nine cases and remained incomplete. These are experimental engine
observations; no product validator or acceptance checkbox is replaced by them.

The [scope map](schema-port-scope.md) now distinguishes actual caller/SDK
contracts from additional private-implementation hypotheses in the new API
checks. It keeps all current gates unchanged. These additional groups do not
replace the original 839 migration units or the full 20 acceptance conditions.
