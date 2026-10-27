# Original flight ownership and the next Rust boundary

This is a source-backed responsibility map, not a completed migration or a
resident-instance design. Keep the original GUI, all 839 original units plus
the private helper TODO, R1–R11 and all 20 acceptance conditions in scope.

## Execution evidence which constrains this boundary

The `48ab3ee5` diagnostic used the declared pre-initialized stdlib configuration.
In actual Python 3.13.16/32-bit execution, local `ContextVar` isolation returned
`unset → separate-context → unset`, leaving the parent value `parent`. Both
scenarios then reached `thread-start`, where the real new `Thread.start()`
raised `RuntimeError("can't start new thread")`. They returned guest JSON without
an unreachable trap or native deadline failure. They did not prove a worker,
thread-context custody, waiter departure, thread termination or physical Store
cleanup. The isolated child processes were reaped.

The memoryless-u32 same-task canonical reentry constraint passed separately.
It does not prove CPython/dynamic-string reentry, independent operations,
individual cancellation or an original RuntimeSurface worker. Full source,
initialization conditions and measurements are in
[execution-base evidence](native-execution-capabilities.md).

## Actual ownership

| State or operation | Original owner and lifetime | Consequence for translation |
| --- | --- | --- |
| Profile implementation | `profile_runtime_composition` installs a fixed singleton. Its `activation_store()` constructs a **new** `ActivationStore` each call. | Do not introduce an activation factory-once/result memo contract. |
| Store capture | Host `capture_active_profile()` creates the Store inside the Authority context and obtains its own authenticated snapshot. | Keep each caller's Authority and graph checks. |
| In-progress artifact verification | `service.py` has one module-global flight dictionary and lock. A flight contains Event, actual exception object and waiter count; no successful result object. | A generic native notification primitive must not invent shared results or exception reconstruction. |
| Read gates | A separate module-global dictionary retains a gate by `state_root`. | Do not confuse this publication/process-lock queue with hash-flight notification. |
| Snapshot reuse | Host ContextVar scope cache reuses the same captured snapshot object while its pointer signature matches. Nested scopes share the dictionary; `fresh=True` makes a new dictionary; exit restores the actual token. | Context-local snapshot identity is separate from hash-flight sharing. |
| RuntimeSurface jobs | Application composition supplies a RuntimeSurface which creates and retains real Python Threads, observes `is_alive()`, copies the actual Context and keeps jobs until work returns after owner departure. | These are real translation obligations, not evidence that Rust task metadata can replace Python Thread/Context semantics. |
| Current native bootstrap | A funded native Work retains source `S` and one original invocation. Python initialization creates the original singleton/registry, not an ActivationStore. | The current short-lived Store is not an activation service or a long-lived runtime. |

Source anchors:

- [Original factory](../../../tobkiri_runtime/ecosystem/defaultspack/defaultspack/profile_runtime_composition.py#L453),
  [Host capture](../../../tobkiri_runtime/core_runtime/bootstrap/profile_capture.py#L1070).
- [Flight state and dictionaries](../../../tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py#L125),
  [read/verification sequence](../../../tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py#L1038).
- [Context scopes](../../../tobkiri_runtime/core_runtime/bootstrap/profile_capture.py#L68),
  [snapshot cache](../../../tobkiri_runtime/core_runtime/bootstrap/profile_capture.py#L1033).
- [Application composition](../../../tobkiri_runtime/ecosystem/defaultspack/defaultspack/runtime_composition.py#L459),
  [actual worker construction](../../../tobkiri_runtime/ecosystem/defaultspack/domain/runtime_surface_v4.py#L173),
  [worker cleanup](../../../tobkiri_runtime/ecosystem/defaultspack/domain/runtime_surface_v4.py#L232),
  [Context transfer](../../../tobkiri_runtime/ecosystem/defaultspack/domain/runtime_surface_v4.py#L946).

## Preserve the exact flight behavior

The original key has seven fields, with **Path values** in its first two slots:

```text
(state_root, workspace_root, profile_id,
 str(profile_revision), str(activation_id), str(plan_digest), str(lock_digest))
```

The native primitive can treat a key as opaque data. Its meaning and Path-value
equality remain the adapter's responsibility. A digest, physical allocation
domain, file descriptor, key match or metadata counter is not authority.

1. Each caller reads its own authenticated graph under the publication lock.
2. Outside that lock, it joins the module-global in-progress hash flight.
3. The leader verifies actual selected artifact bytes. On failure it retains
   its actual exception in `flight.error` and re-raises that same object.
4. In `finally`, the leader removes only its own current registration and sets
   the completion Event. Existing waiters retain their local flight references.
5. A waiter which reaches its Event timeout decrements the original waiter
   count and raises `ArtifactVerificationTimeout`. It does not stop the leader
   or remove its registration. This API has no independent cancel token.
6. On notification, **only if the leader failed**, a waiter independently
   verifies again and obtains its own typed outcome. It does not re-raise the
   leader's saved exception. Successful notification does not cause an extra
   hash pass.
7. Each caller reads its own authenticated graph again under the publication
   lock and rejects a changed graph. Later callers start a fresh flight.

The original counter is incremented on join and decremented on timeout; do not
silently redefine it as a reference count. The process-lock retry loop is a
different mechanism (nonblocking retry, sleep capped at 10 ms).

[Exact implementation](../../../tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py#L1088).

## Existing native funding and operation lifetimes

`NativeSetupAccount` validates the actual process and retained native root proof
and meters shared physical payload up to 64 MiB. `begin()` captures a genuine
operation identity and original start before metadata admission. Invocation
clones cannot create another Store attempt or renew the absolute 30 seconds.
The Wasm Store, allocator owners, DATA IO, cancellation and retirement currently
retain that one invocation.

```mermaid
flowchart TD
    R[Actual process and descriptor-pinned root lease] --> A[NativeSetupAccount: shared physical capacity]
    A --> O1[begin: real operation 1 and original clock]
    A --> O2[begin: real operation 2 and original clock]
    O1 --> S[Current one-invocation Store / DATA IO / retirement]
    A -. proposed capacity-only ownership; not implemented .-> F[Native shared notification state]
    O1 -. genuine operation custody required .-> F
    O2 -. genuine operation custody required .-> F
```

The candidate notification state needs explicit root-funded ownership separate
from execution scope. The existing reservation carries real invocation
provenance; do not invent invocation zero, reuse counters as custody or label
an old invocation as a persistent instance. Account identity must be checked
through actual retained ownership rather than serialized generation/domain
values. Creating state must not start/reset another execution clock or grant
Profile, native IO, filesystem or cancellation rights.

The first implementation candidate is a separate account-owned shared-state
reservation type. It retains a real account and ledger record, prepays the
same 64 MiB payload account and original control ledger, and carries no invocation provenance or
execution rights. A future coordinator must retain those funds through actual
work and waiter handles, even after its outer handle is dropped. Matching an
operation to that account requires live native ownership identity. This API
is implemented as `NativeSharedStateReservation`. The complete supported-platform
[7ddace963 actual-root CI](https://github.com/harupipipipi/tobkiri/actions/runs/40448775896/job/122006277066)
passed all four added cases, the eight existing accounting cases and strict
Clippy. This proves actual account membership, denial before publication and
capacity retention through real work departure. The first run rejected the
fixture's existing temporary directory at the unchanged privacy check; the
correction lets the normal descriptor-relative creator make its own 0700 child.
The first coordinator is now an explicit `native-shared-state` Core feature,
awaiting its own actual CI. Default product composition, WIT and IPC do not
expose it. It retains complete opaque key bytes and prepays registry replacement
and key/carrier storage in the same actual account. A new registration appears
only after all pending allocations succeed. Existing and replacement vector
storage coexist under distinct reservations until the former is freed.

`run_or_join` holds the registry lock only for enrollment/publication. The
leader's synchronous callback retains the original operation; a private guard
removes that actual flight identity and notifies only after callback return or
unwind. Trusted callbacks MUST finish and Join every owned native worker on
both paths before their scope ends; detached work is unsupported. Neither
waiter timeout/drop nor operation deadline removes running work. Notification
has no outcome/success flag or effect authority. Old waiters retain their own
physical flight after same-key re-registration. Successful-waiter counts retain
the original non-refcount semantics; timeout/abandonment removes only that
enrollment. The opt-in candidate's eight actual-root tests include scoped-work
cleanup during panic, timeout, old/new registration races, foreign actual
accounts, exact keys/namespaces and admission rollback. These tests are not yet
executed evidence. The added eighth case waits out the real original 30 seconds
and proves that this deadline caps the waiter and refuses new work without
relabeling unfinished native work as complete. Original realm binding, Pack adapter and product integration
remain unimplemented.

`FnOnce` itself does not mechanically enforce native Join custody. The scoped
thread case tests one real implementation of that contract. Before product
admission, each fixed callback still needs an audited scope/owner enforcing
termination on both return and unwind; this generic interface alone is not
proof of that condition or of arbitrary background-work termination.

Native sources: [account/begin](../../../crates/tobkiri-gate/src/native_setup_capacity.rs#L206),
[invocation/claim](../../../crates/tobkiri-gate/src/native_setup_capacity.rs#L275),
[reservation](../../../crates/tobkiri-gate/src/native_setup_capacity.rs#L335),
[Store binding](../../../crates/tobkiri-wasm/src/native_setup_store.rs#L178),
[DATA IO custody](../../../crates/tobkiri-wasm/src/native_data_io.rs#L144).

The old sharing scope is a module in a Python interpreter/process, not an
arbitrary native root partition. Before production binding, establish which
native owner represents that original realm. A narrower partition is not
proved equivalent by tests in one root. Generic notification completion must
not itself attest verified bytes, authorize effects or become a caller-controlled
verification-success flag.

## First vertical boundary and open product boundary

The first candidate is a Pack-independent native primitive for in-progress
registration, join, wait, notification and original waiter accounting. Keep the
seven-field interpretation, authenticated graph checks, retry decision, actual
exception-object retention and typed outcomes in the responsible adapter. This
does not require a persistent Wasm Store. It still needs two genuine operations
and correctly funded shared state; a passing standalone primitive does not close
the adapter, RuntimeSurface or complete migration.

Use the original reference cases:

- [Coalescing](../../../tobkiri_runtime/tests/test_defaultspack_runtime_v4.py#L1653).
- [Failure is not shared; own retry](../../../tobkiri_runtime/tests/test_defaultspack_runtime_v4.py#L1770).
- [HTTP waiter timeout](../../../tobkiri_runtime/tests/test_production_frontend_contract_http.py#L7443).
- [Context sharing and token restoration](../../../tobkiri_runtime/tests/test_dispatch_profile_capture_scope.py#L106).
- [Owner departure and later reuse](../../../tobkiri_runtime/tests/test_runtime_surface_v4.py#L1481).

Before translating RuntimeSurface, identify external observers of actual Thread
objects/identity and all values/readers carried by `copy_context()`. Actual
Python ContextVar readers require a genuine Python Context execution boundary;
a Rust map of values is not equivalent. A real Rust translation remains a
candidate, with generic execution/lifetime mechanisms in Core and domain rules
outside it. No fake Thread API, serial mutex, fresh-Store replacement, renewed
invocation, whole-Store cancellation or new native-Pack privilege is adopted by
this document. Product connection stays gated by original behavior and the full
acceptance scope.

The canonical-runtime/reference search found Thread objects retained and
observed inside the executor, with integers and Future objects returned to
production callers. No external production Thread-object/ident transfer was
found. Regression tests do inspect real Python Thread names/counts using
`threading.enumerate()`; this is not a license to fabricate Thread objects in a
translation. External plugins and dynamic reflection remain outside that search.

Context copying is the entire actual Context, not a fixed Host dictionary.
Constructor-provided Python callbacks can read additional ContextVars. Confirmed
readers include the Profile snapshot cache, user-data Path and a shared mutable
timing `_Trace` object. Another Host scope, `BundledHostBaselineRead`, requires
all three of active state, the same actual ContextVar object and the original
real thread ident. Copying that scope value to a worker does not make it usable
there. Whether it is already set at every ordinary RuntimeSurface call remains
unconfirmed.

Additional anchors: [thread-name regression](../../../tobkiri_runtime/tests/test_runtime_surface_v4.py#L1460),
[callback constructor](../../../tobkiri_runtime/ecosystem/defaultspack/domain/runtime_surface_v4.py#L2195),
[Host capture scope](../../../tobkiri_runtime/core_runtime/pack_control_v4.py#L584),
[timing reader](../../../tobkiri_runtime/core_runtime/profile_operation_timing.py#L139),
[baseline scope custody](../../../tobkiri_runtime/core_runtime/bundled_host_baseline_v4.py#L409).

## Historical diagnostic scheduling

The failure-only input-stack import audit and the diagnostic-only pre-init
code-cache comparison are retained with `workflow_dispatch` but no automatic
push trigger. The former deliberately cannot produce an acceptance pass; the
latter explicitly rejects candidate acceptance. Their implementation, source
checks and earlier evidence remain available. Neither has a workflow caller or
external artifact consumer in the repository. The inspected repository ruleset
list was empty and `soon` had no classic required-status-check protection.

The normal Schema workflow, synchronous native-input stack validation, original
Pack replay, whole component, native UI, producer, Installer and completion gate
remain automatic. In particular, APFS snapshot/native-IO retirement/two-Store
tests are not replaced by the initialization or thread diagnostics.
