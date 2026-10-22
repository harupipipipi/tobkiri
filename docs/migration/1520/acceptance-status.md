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

The source oracle in
[`python_provider_compat`](../../../tools/quality/python_provider_compat/)
executed the original adapters under CPython 3.13.9 / Unicode 15.1.0. A native
Rust differential replay matched all 94 observations: 41 usage-cost and 53
stream-normalizer cases, including the retained deep-input cases. The 24 scalar
observations and strict codec boundary tests also passed. The unchanged
historical CPython 3.13.5 oracle remains a separate reference.

Release component-build trials produced actual Rust usage-cost, stream and
finalizer Wasm components. Native finalizer tests explicitly distinguish its
two dependency-free observations from eight unavailable imported-dependency
cases; those eight are **not** parity successes. Fresh Core/Gate replays and
five-role composition remain required, as do rebuilds after later source edits.

The separate original-v2
[`tool-bridge oracle`](../../../tools/quality/python_tool_bridge_compat/)
now records 81 fresh source observations. Its 33 historical records contribute
inputs only; their v1 expectations are not reused as v2 expectations. Rust
bridge execution against these observations has not yet passed validation.

The terminal-owner candidate prepays metadata custody before source grant
consumption and retains the original Join through cancellation. Source review
found the consumed-but-unpublished custody gap corrected. Its first unit run
passed four tests, then exposed a test driver awaiting one retained observer
without polling the other. The driver was corrected to join both observers;
the full Core regression run is still pending. A further reviewed candidate
retains the actual reaper Join and its result separately from metadata delivery;
its fourteen regression tests remain unexecuted. Terminal metadata delivery
alone is not proof of the reaper's independent physical cleanup.
Disk capacity currently prevents further heavy validation. No checklist item,
inventory unit, production activation, complete GUI flow, or VM retirement is
accepted by this checkpoint.

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
