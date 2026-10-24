# Issue #1520 acceptance status

Audit date: 2026-10-18. The 20 items below follow the acceptance list in
[issue #1520](https://github.com/harupipipipi/tobkiri/issues/1520), fetched on this
date. The issue is normative; [resource boundaries](resource-boundaries.md)
record the later implementation decisions. This is a checklist, not a new
execution receipt or permission to remove legacy code.

The [migration inventory](../../../migration/inventory.json) has **837 units:
1 `core+vectors`, 836 `todo`**. Only the legacy global-contracts canonical module
is closed. Source and test links establish where implementation and coverage
live, not that those tests passed on the current tree. An unchecked item means
this audit does not establish its complete current acceptance, not that all of
its implementation is missing.

Focused checks after this slice: foundation gate **0 failures**; complete gate
**1,044 failures**. Gate regressions **13**, CI-helper regressions **7**, and
inventory tests **9** passed. Inventory coverage reports 837 units and
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

The next source candidate copies unescaped UTF-8 JSON string runs in bulk
instead of growing/re-encoding them one byte/code point at a time. Escapes,
surrogate validation, strict duplicate detection, object order, number
spelling, size/depth bounds and every authority lookup are unchanged. Two
regressions cover all ASCII control escapes, mixed Unicode and escaped runs,
invalid/truncated strings and escaped duplicate keys across all three codecs.
The existing Python byte/digest/seal corpora remain the acceptance oracle.
Formatting, the lightweight foundation boundary check and diff checks pass;
the actual Rust regressions and native performance remain pending CI. No
local Rust/guest build or model server was started. The early artifact makes
no pass or authority claim; production GUI/Setup and all acceptance items
remain open.

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

1. [ ] **CI rejects Pack IDs and Pack-owned shape names in core and hosts.**
   [Checker](../../../tools/quality/issue_1520_gate.py),
   [negative fixtures](../../../tools/quality/test_issue_1520_gate.py), and
   [CI](../../../.github/workflows/issue-1520-rust.yml) exist. The focused
   foundation gate passes without weakening production checks; the final host
   scan still includes substantial legacy Pack knowledge.

2. [ ] **Core-defined shapes are restricted to R1.2.**
   [WIT registry and tests](../../../crates/tobkiri-wit/src/lib.rs) enforce the
   exact 17 interfaces and minimal world; [manifest tests](../../../crates/tobkiri-manifest/tests/contracts.rs)
   reject unknown Core versions/imports. Record a current exact-ABI replay;
   existence of a WIT declaration does not prove its physical backend works.

3. [ ] **A non-Defaults Pack set can chat.**
   [Chat E2E](../../../crates/tobkiri-e2e/tests/chat.rs) includes
   `entirely_different_pack_set_chats_and_persists_after_core_restart` using Echo
   Packs. Rebuild/admit current components and execute the current Core path;
   historical component receipts do not certify later runtime edits.

4. [ ] **CLI alone creates, starts, publishes, and imports Profiles.**
   [Commands](../../../crates/tobkiri-cli/src/commands.rs),
   [input tests](../../../crates/tobkiri-cli/src/profile_input_tests.rs), and
   [Core import tests](../../../crates/tobkiri-core/src/core_tests.rs) exist.
   A current real CLI round trip with ordinary approvals and clean shutdown is
   still needed; parser/library tests are not that operational proof.

5. [ ] **All input, including chat, traverses Flow; editing Flow changes behavior.**
   [Native input E2E](../../../crates/tobkiri-e2e/tests/native_input.rs),
   [chat E2E](../../../crates/tobkiri-e2e/tests/chat.rs), and
   [input forwarding tests](../../../crates/tobkiri-core/src/input_forwarding_guest_tests.rs)
   exercise the new entry path. Legacy fixed routes and old engines remain;
   native Send/save and all migrated input surfaces need current end-to-end proof.

6. [ ] **Foreign Packs insert, replace, or delete by shape only where allowed.**
   [Profile compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs)
   cover open roles, published Flow shapes, and permissions;
   [Flow tests](../../../crates/tobkiri-flow/tests/engine.rs) exercise `next`.
   Re-execute all three overlay modes and closed-target denial through actual
   current components, not only compiler fixtures.

7. [ ] **Users can overlay any step.**
   [Compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs) include
   `user_overlay_can_override_private_closed_flow` and user-overlay witnesses.
   Complete current host/CLI configuration and execution coverage while retaining
   the separately approved User policy and original Pack authority for `next`.

8. [ ] **Neither Pack nor user overlays bypass the Gate.**
   [Authority tests](../../../crates/tobkiri-store/tests/authority.rs) include
   `user_overlay_direct_effect_always_reaches_trusted_prompt`;
   [compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs) retain
   separate original authorities. Current runtime denial, cancellation, and
   epoch-change replays are required; overlay composition alone is insufficient.

9. [ ] **Profiles without default loop bounds fail validation.**
   [Compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs) include
   `default_loop_bounds_and_explicit_error_policy_keys_are_required`;
   [engine tests](../../../crates/tobkiri-flow/tests/engine.rs) reject missing or
   widened limits. This is a focused, deterministic current replay candidate.

10. [ ] **Ambiguous single ports/unordered overlays reject changes, preserve the
    old configuration, and offer a repair Pack.**
    [Compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs),
    [Core save/remove/inspect](../../../crates/tobkiri-core/src/core.rs), and
    [CLI repair](../../../crates/tobkiri-cli/src/commands.rs) implement pieces.
    Prove one integrated conflict-to-repair flow, unchanged head on failure,
    stale original-hash rejection, and no automatic permission expansion.

11. [ ] **A port without WIT is rejected unless explicitly `untyped`.**
    [Manifest validation](../../../crates/tobkiri-manifest/src/package.rs) and
    [compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs) distinguish
    explicit untyped ports from typed required slots. Execute current malformed
    manifest/type fixtures and verify the user-facing unverified-connection label.

12. [ ] **Compatible minor versions connect; different majors do not.**
    [Manifest tests](../../../crates/tobkiri-manifest/tests/contracts.rs) cover
    trailing-option projection and enum directionality;
    [compiler tests](../../../crates/tobkiri-profile/tests/compiler.rs) cover
    verified minor snapshots and major mismatch. Replay current typed components;
    do not treat arbitrary unknown minors as automatically compatible.

13. [ ] **Updates approve only added permissions; removal cannot break required ports.**
    [Permission diff](../../../crates/tobkiri-gate/src/permissions.rs),
    [update authority tests](../../../crates/tobkiri-store/tests/authority.rs), and
    [Core removal](../../../crates/tobkiri-core/src/core.rs) exist. Add/run the
    complete update/removal transaction regression, including unchanged head on
    denial and preservation of orphaned data.

14. [ ] **Three storage tiers are Profile-isolated; public writes approve once
    per writer Pack/store pair.**
    [Tiered Store](../../../crates/tobkiri-store/src/tiered.rs),
    [security tests](../../../crates/tobkiri-store/tests/security.rs), and
    [native quota tests](../../../crates/tobkiri-store/tests/native_quota.rs)
    implement/check these mechanisms. Current guest/runtime replay and migration
    of existing Profile workspaces remain distinct unfinished work.

15. [ ] **Native Packs and cross-Profile storage require explicit dangerous permission.**
    [Gate permissions](../../../crates/tobkiri-gate/src/permissions.rs),
    [process launch tests](../../../crates/tobkiri-store/tests/process_launch.rs),
    and [global storage tests](../../../crates/tobkiri-store/tests/authority.rs)
    cover denial/approval foundations. Physical Proc/PTY is still
    [Unavailable](../../../crates/tobkiri-wasm/src/effects.rs); safe denial is not
    a functioning, approved native execution implementation.

16. [ ] **Published Profiles exclude approvals, data, and secrets.**
    [Published allowlist](../../../crates/tobkiri-profile/src/lib.rs),
    [serialization tests](../../../crates/tobkiri-profile/tests/compiler.rs), and
    [fresh import approval tests](../../../crates/tobkiri-core/src/core_tests.rs)
    exist. Verify a current CLI export/import against populated stores and vaults,
    including all tiers and user-global data, without exposing private payloads.

17. [ ] **Pack UI is isolated and can access data only through gated ports.**
    [Core UI E2E](../../../crates/tobkiri-e2e/tests/ui.rs),
    [native probes](../../../packs/native-acceptance/verify-source.test.mjs), and
    [host integration](../../../tobkiri_launcher/src-tauri/src/core_host.rs)
    cover different layers. Actual WebView origin/IPC/navigation denial plus
    ordinary Send, persistence, and truthful Stop/Quit remain unaccepted; source
    checks, Node tests, and Core stream tests cannot substitute for that run.

18. [ ] **At least one unchanged pi extension runs through an adapter Pack.**
    [Pi E2E](../../../crates/tobkiri-e2e/tests/pi.rs) has two explicitly ignored
    tests requiring separately built components; [compatibility scope](../../../packs/local.pi-adapter/COMPAT.md)
    identifies unchanged upstream sources. Rebuild and explicitly execute the
    wrapper/adapter/Gate path. Guard decisions alone do not establish protected
    file effects or complete Harness tool-loop integration.

19. [ ] **Python runtime and PackVM are removed; distributions contain no Python.**
    This is **blocked**, not closeable by changing inventory labels.
    [Legacy runtime](../../../tobkiri_runtime/core_runtime),
    [Launcher Python support](../../../tobkiri_launcher/src-tauri/src/python_env.rs),
    and legacy mechanisms remain. All 836 open units need replacement parity or
    authorized retirement; then run the [deletion gate](../../../migration/inventory.py)
    and inspect actual distribution contents. Development-only compilers do not
    justify shipping a Python product runtime.

20. [ ] **Canonical/seal/digest match frozen Python vectors byte-for-byte.**
    [Vector tests](../../../crates/tobkiri-canon/tests/vectors.rs),
    [additional seal families](../../../crates/tobkiri-canon/tests/extra_seals.rs),
    and [canonical evidence](../../../crates/tobkiri-canon/evidence/global-contracts-canonical-20261015.json)
    establish substantial source-bound coverage. Only one legacy inventory unit
    is closed; remaining persisted recipes/signature differences need explicit
    mappings and current replay. Never rewrite frozen expected bytes to fit Rust.

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
